"""争吵模式 - 真正并行、可打断、可提建议的多代理争论（支持用户插话）"""
import asyncio
import time
import re
from typing import List, Dict, Optional
from dataclasses import dataclass, field

from modes.base import BaseMode, ModeResult
from event_bus import EventBus, Event, EventType, get_event_bus, create_event, EventFormatter, USER_PRIORITY
from event_agent import EventAgentPool
from speech_controller import get_speech_controller, SpeechControllerConfig
from suggestion_manager import get_suggestion_manager
from whiteboard import Whiteboard, TaskStep
from config_loader import AgentConfig

# 投票系统
from contribution_scorer import (
    ContributionScorer, ContributionConfig, get_contribution_scorer
)
from vote_manager import (
    VoteManager, VotingConfig, VoteType, VoteMode, get_vote_manager
)
from collusion_detector import (
    CollusionDetector, CollusionConfig, get_collusion_detector
)


@dataclass
class DebateConfig:
    """争吵模式配置"""
    max_duration_sec: int = 300         # 最大持续时间
    idle_timeout_sec: int = 15          # 空闲超时
    max_events: int = 500               # 最大事件数
    consensus_threshold: int = 3        # 叫停所需票数
    interrupt_threshold: int = 15       # 打断分数阈值
    user_vote_weight: float = 1.5       # 用户投票权重


DEBATE_PROMPT = """你是争吵参与者。

身份：{identity}
性格：谨慎度{cautiousness}/10，共情度{empathy}/10

主题：{topic}

规则：
1. 简短有力，每次发言不超过30字
2. 可以打断别人（如果对方说错）
3. 可以向其他代理提建议：@代理名: 建议
4. 认为讨论充分时，叫停：停！我提议：[方案]
5. 用户插话时必须认真对待并回应

近期发言：
{context}

快速发言："""


class DebateMode(BaseMode):
    """争吵模式 - 真正并行的多代理争论（支持用户插话）"""
    
    mode_name = "debate"
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # 获取配置
        self.debate_config = getattr(self.config, 'debate', DebateConfig())
        
        # 事件系统组件
        self.event_bus = get_event_bus()
        self.speech_controller = get_speech_controller(
            SpeechControllerConfig(
                interrupt_threshold=self.debate_config.interrupt_threshold
            )
        )
        self.suggestion_manager = get_suggestion_manager()
        
        # 投票系统组件
        self._contribution_scorer = get_contribution_scorer(ContributionConfig())
        self._vote_manager = get_vote_manager(
            VotingConfig(), 
            self._contribution_scorer
        )
        self._collusion_detector = get_collusion_detector(CollusionConfig())
        
        # 状态
        self._stop_votes: Dict[str, str] = {}  # agent_id -> proposal
        self._vote_weights: Dict[str, float] = {}  # agent_id -> weight
        self._consensus_reached = False
        self._final_proposal = ""
        self._debate_start_time = 0
        self._last_activity_time = 0
        self._user_participated = False
        
        # 用户输入队列（用于实时插话）
        self._user_input_queue: asyncio.Queue = None
        
        # 事件代理池
        self._event_agents: Optional[EventAgentPool] = None
    
    async def execute(self, question: str) -> ModeResult:
        """执行争吵模式"""
        self._is_running = True
        self._start_time = time.time()
        self._debate_start_time = time.time()
        self._last_activity_time = time.time()
        self._user_input_queue = asyncio.Queue()
        
        # 初始化
        self._initialize()
        
        try:
            # 发布用户问题
            await self.event_bus.publish_async(create_event(
                event_type=EventType.USER_INPUT,
                source_id="user",
                content=question
            ))
            
            # 创建并启动事件代理池
            enabled_configs = [a for a in self.config.agents if a.enabled]
            self._event_agents = EventAgentPool(enabled_configs, self.config.prompts)
            
            # 初始化投票权重
            for agent in enabled_configs:
                self._vote_weights[agent.id] = 1.0
            self._vote_weights["user"] = self.debate_config.user_vote_weight
            
            # 启动所有代理
            await self._event_agents.start_all(question)
            
            # 监控循环（同时处理用户输入）
            await self._monitor_loop(question)
            
            # 停止所有代理
            await self._event_agents.stop_all()
            
            # 生成结果
            result = self._generate_result()
            self.whiteboard.set_final_resolution(result)
            
            return self._build_result()
            
        except Exception as e:
            return ModeResult(success=False, final_resolution="", error=str(e))
        finally:
            self._is_running = False
            self._cleanup()
    
    def _initialize(self):
        """初始化"""
        # 重置事件总线
        self.event_bus.clear()
        
        # 重置发言控制器
        self.speech_controller.reset()
        
        # 重置建议管理器
        self.suggestion_manager.reset()
        
        # 初始化投票系统
        agent_ids = [a.id for a in self.agent_pool.get_enabled_agents()]
        self._contribution_scorer.initialize_session(self.whiteboard.session_id, agent_ids)
        
        # 订阅关键事件
        self.event_bus.subscribe_to_type(EventType.VOTE_REQUEST, self._on_vote_request)
        self.event_bus.subscribe_to_type(EventType.VOTE_CAST, self._on_vote_cast)
        self.event_bus.subscribe_to_type(EventType.CONSENSUS_REACHED, self._on_consensus)
        self.event_bus.subscribe_to_type(EventType.AGENT_UTTERANCE, self._on_activity)
        self.event_bus.subscribe_to_type(EventType.USER_INTERRUPT, self._on_user_interrupt)
        self.event_bus.subscribe_to_type(EventType.USER_VOTE, self._on_user_vote)
        self.event_bus.subscribe_to_type(EventType.USER_END, self._on_user_end)
    
    async def _monitor_loop(self, question: str):
        """监控循环 - 检查终止条件并处理用户输入"""
        while self._is_running and not self._consensus_reached:
            # 检查最大时长
            elapsed = time.time() - self._debate_start_time
            if elapsed > self.debate_config.max_duration_sec:
                await self._force_consensus("超时")
                break
            
            # 检查空闲超时
            idle_time = time.time() - self._last_activity_time
            if idle_time > self.debate_config.idle_timeout_sec:
                await self._force_consensus("讨论沉寂")
                break
            
            # 检查加权票数
            total_weight = sum(
                self._vote_weights.get(voter, 1.0) 
                for voter in self._stop_votes.keys()
            )
            if total_weight >= self.debate_config.consensus_threshold:
                await self._finalize_debate()
                break
            
            await asyncio.sleep(0.3)
    
    async def user_interrupt(self, content: str):
        """用户插话"""
        self._user_participated = True
        
        # 解析是否包含 @agent
        target_id = None
        match = re.match(r'@(\w+)\s*[:：]?\s*(.+)', content)
        if match:
            target_id = match.group(1)
            content = match.group(2)
        
        # 检查是否是叫停指令
        if content.strip().lower() in ['停', 'stop', '/stop', '投票', '/vote']:
            await self._trigger_vote_from_user()
            return
        
        # 检查是否是结束指令
        if content.strip().lower() in ['/end', '结束', '结束讨论']:
            await self._end_from_user(content)
            return
        
        # 通过发言控制器发送
        await self.speech_controller.user_speak(content, target_id)
    
    async def user_vote(self, support: bool, reason: str = ""):
        """用户投票"""
        event = create_event(
            event_type=EventType.USER_VOTE,
            source_id="user",
            content=reason,
            metadata={"support": support, "weight": self.debate_config.user_vote_weight}
        )
        await self.event_bus.publish_async(event)
    
    async def _trigger_vote_from_user(self):
        """用户触发投票"""
        # 获取最近的讨论内容作为提案
        recent = self.event_bus.get_events_by_type(EventType.AGENT_UTTERANCE, 3)
        proposal = "用户要求投票"
        if recent:
            proposal = recent[-1].content[:100]
        
        self._stop_votes["user"] = f"[用户叫停] {proposal}"
        
        await self.event_bus.publish_async(create_event(
            event_type=EventType.SYSTEM,
            source_id="system",
            content="[叫停] 用户叫停！进入投票阶段..."
        ))
    
    async def _end_from_user(self, content: str):
        """用户结束讨论"""
        self._final_proposal = f"[用户结束] {content}"
        self._consensus_reached = True
        
        await self.event_bus.publish_async(create_event(
            event_type=EventType.USER_END,
            source_id="user",
            content=content
        ))
    
    async def _on_activity(self, event: Event):
        """活动事件回调"""
        self._last_activity_time = time.time()
        
        # 记录到白板
        self.whiteboard.add_message(
            agent_id=event.source_id,
            content=event.content,
            message_type="debate"
        )
    
    async def _on_user_interrupt(self, event: Event):
        """用户插话回调"""
        self._last_activity_time = time.time()
        self._user_participated = True
        
        # 记录到白板
        self.whiteboard.add_message(
            agent_id="user",
            content=event.content,
            message_type="user_interrupt"
        )
    
    async def _on_vote_request(self, event: Event):
        """投票请求回调（有人叫停）"""
        proposer_id = event.source_id
        proposal = event.content
        
        # 检查是否可以叫停（冷却检查）
        can_stop, remaining = self._vote_manager.can_call_stop(proposer_id)
        if not can_stop:
            await self.event_bus.publish_async(create_event(
                event_type=EventType.SYSTEM,
                source_id="system",
                content=f"[冷却] {proposer_id} 冷却中，剩余 {remaining:.0f} 秒"
            ))
            return
        
        self._stop_votes[proposer_id] = proposal
        
        # 广播投票请求
        await self.event_bus.publish_async(create_event(
            event_type=EventType.SYSTEM,
            source_id="system",
            content=f"[叫停] {proposer_id} 叫停！提案：{proposal[:50]}..."
        ))
        
        # 开始投票会话
        agent_ids = [a.id for a in self.agent_pool.get_enabled_agents()]
        session = await self._vote_manager.start_voting(
            proposal=proposal,
            proposer_id=proposer_id,
            eligible_voters=agent_ids
        )
        
        # 在白板记录
        self.whiteboard.start_vote_session(session.session_id, proposal, proposer_id)
        
        # 更新白板权重
        weights = self._contribution_scorer.get_all_weights()
        self.whiteboard.set_agent_weights(weights)
    
    async def _on_vote_cast(self, event: Event):
        """投票回调"""
        voter_id = event.source_id
        vote_type_str = event.metadata.get("vote_type", "support")
        reason = event.metadata.get("reason", "")
        
        # 转换投票类型
        vote_type = VoteType.SUPPORT if vote_type_str == "support" else (
            VoteType.OPPOSE if vote_type_str == "oppose" else VoteType.MODIFY
        )
        
        # 提交投票
        success = await self._vote_manager.submit_vote(
            voter_id=voter_id,
            vote_type=vote_type,
            reason=reason
        )
        
        if success:
            # 记录到串通检测器
            session = self._vote_manager.get_active_session()
            if session:
                self._collusion_detector.record_vote(
                    session.session_id, 
                    voter_id, 
                    vote_type.value
                )
            
            # 记录到白板
            weight = self._contribution_scorer.get_weight(voter_id)
            self.whiteboard.record_vote(voter_id, vote_type.value, weight, reason)
            
            # 广播投票
            await self.event_bus.publish_async(create_event(
                event_type=EventType.SYSTEM,
                source_id="system",
                content=f"[投票] {voter_id} 投票: {vote_type.value}"
            ))
    
    async def _on_user_vote(self, event: Event):
        """用户投票回调"""
        self._last_activity_time = time.time()
        support = event.metadata.get("support", True)
        reason = event.metadata.get("reason", "")
        
        vote_type = VoteType.SUPPORT if support else VoteType.OPPOSE
        
        # 提交用户投票
        success = await self._vote_manager.submit_user_vote(vote_type, reason)
        
        if success:
            # 广播
            await self.event_bus.publish_async(create_event(
                event_type=EventType.SYSTEM,
                source_id="system",
                content=f"[投票] 用户投票: {vote_type.value}"
            ))
        
        # 检查是否应该结束投票
        session = self._vote_manager.get_active_session()
        if session and time.time() > session.end_time:
            await self._finalize_voting()
    
    async def _finalize_voting(self):
        """结束投票并处理结果"""
        passed, details = await self._vote_manager.end_voting()
        
        # 更新白板
        self.whiteboard.end_vote_session(
            passed=passed,
            support_ratio=details.get("support_ratio", 0),
            details=details
        )
        
        # 记录提案结果
        vote_history = self._vote_manager.get_vote_history(limit=1)
        session = vote_history[0] if vote_history else None
        if session:
            self._contribution_scorer.record_proposal_result(
                session["proposer"], passed
            )
        
        # 运行串通检测
        cases = self._collusion_detector.run_full_detection()
        if any(cases.values()):
            self._collusion_detector.apply_penalties(
                [c for case_list in cases.values() for c in case_list],
                self._contribution_scorer
            )
        
        # 广播结果
        result_str = "[通过]" if passed else "[否决]"
        await self.event_bus.publish_async(create_event(
            event_type=EventType.CONSENSUS_REACHED if passed else EventType.SYSTEM,
            source_id="system",
            content=f"[投票结果] {result_str} (支持率: {details.get('support_ratio', 0):.1%})"
        ))
        
        if passed:
            self._consensus_reached = True
            self._final_proposal = session["proposal"] if session else ""
    
    async def _on_user_end(self, event: Event):
        """用户结束回调"""
        self._consensus_reached = True
    
    async def _on_consensus(self, event: Event):
        """达成共识回调"""
        self._consensus_reached = True
        self._final_proposal = event.content
    
    async def _force_consensus(self, reason: str):
        """强制达成共识"""
        # 获取最近的发言作为决议
        recent = self.event_bus.get_events_by_type(EventType.AGENT_UTTERANCE, 5)
        if recent:
            last = recent[-1]
            self._final_proposal = f"[{reason}] 最后发言：{last.content}"
        else:
            self._final_proposal = f"[{reason}] 讨论未产生结果"
        
        self._consensus_reached = True
    
    async def _finalize_debate(self):
        """结束争论"""
        # 选择票数最多的提案
        if self._stop_votes:
            # 简单选择第一个提案
            self._final_proposal = list(self._stop_votes.values())[0]
        else:
            await self._force_consensus("投票结束")
        
        self._consensus_reached = True
        
        await self.event_bus.publish_async(create_event(
            event_type=EventType.CONSENSUS_REACHED,
            source_id="system",
            content=self._final_proposal
        ))
    
    def _generate_result(self) -> str:
        """生成结果摘要"""
        events = self.event_bus.get_recent_events(100)
        
        lines = ["=== 争吵结果 ===", ""]
        
        # 统计
        utterances = [e for e in events if e.event_type == EventType.AGENT_UTTERANCE]
        user_inputs = [e for e in events if e.event_type in (EventType.USER_INTERRUPT, EventType.USER_INPUT)]
        suggestions = [e for e in events if e.event_type == EventType.SUGGESTION]
        interrupts = [e for e in events if e.event_type == EventType.INTERRUPT]
        
        lines.append(f"代理发言：{len(utterances)} 条")
        lines.append(f"用户插话：{len(user_inputs)} 次")
        lines.append(f"建议：{len(suggestions)} 条")
        lines.append(f"打断：{len(interrupts)} 次")
        lines.append("")
        
        # 贡献分
        scores = self.speech_controller.get_contribution_scores()
        if scores:
            lines.append("贡献排名：")
            sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            for agent_id, score in sorted_scores[:5]:
                lines.append(f"  {agent_id}: {score:.1f} 分")
            lines.append("")
        
        # 最终决议
        lines.append(f"最终决议：{self._final_proposal}")
        
        if self._user_participated:
            lines.append("")
            lines.append("[用户参与了讨论]")
        
        return "\n".join(lines)
    
    def _build_result(self) -> ModeResult:
        """构建结果"""
        events = self.event_bus.get_recent_events(100)
        messages = []
        
        for event in events:
            messages.append({
                "agent_id": event.source_id,
                "content": event.content,
                "type": event.event_type.value,
                "timestamp": event.timestamp
            })
        
        return ModeResult(
            success=True,
            final_resolution=self._final_proposal,
            messages=messages,
            metadata={
                "total_events": self.event_bus.get_event_count(),
                "duration": time.time() - self._debate_start_time,
                "contribution_scores": self.speech_controller.get_contribution_scores(),
                "user_participated": self._user_participated
            }
        )
    
    def _cleanup(self):
        """清理资源"""
        if self._event_agents:
            asyncio.create_task(self._event_agents.stop_all())
    
    def stop(self):
        """停止争论"""
        self._is_running = False


def format_debate_output(result: ModeResult) -> str:
    """格式化争吵输出"""
    lines = ["[争吵实录]", ""]
    
    # 按时间排序所有消息
    for msg in result.messages[-30:]:
        msg_type = msg.get("type", "")
        agent = msg.get("agent_id", "?")
        content = msg.get("content", "")
        
        if msg_type == "user_interrupt":
            lines.append(f"[用户插话]: {content[:60]}")
        elif msg_type == "agent_utterance":
            interrupted = " [被打断]" if msg.get("interrupted") else ""
            lines.append(f"[{agent}]{interrupted}: {content[:60]}")
        elif msg_type == "suggestion":
            lines.append(f"[建议] {content[:60]}")
        elif msg_type == "system":
            lines.append(f"[系统] {content[:60]}")
    
    lines.append("")
    lines.append("=" * 40)
    lines.append(f"[最终决议] {result.final_resolution[:200] if result.final_resolution else '无'}")
    
    if result.metadata.get("user_participated"):
        lines.append("[用户参与了讨论]")
    
    return "\n".join(lines)
