"""会议模式 - 智能讨论系统

会议本身就是争论，不是轮流发言。系统根据任务复杂度、观点分歧、时间压力等
因素自动调整讨论激烈程度（争吵强度），让AI行为更人性化。

集成了会议行为管理器：议程设置、时间提醒、离题检测、总结、修正动议、
事实核查、请求外部输入、搁置争议、优先级排序、方案对比。

异常处理：讨论循环、僵局、代理失控、工具失败、资源耗尽
演化算法：每N次会话后优化代理策略参数
"""
import asyncio
import sys
import time
import json
import re
import random
from typing import List, Dict, Optional, Tuple, Set
from dataclasses import dataclass, field

from modes.base import BaseMode, ModeResult
from modes.serial import EnhancedSerialMode
from agent import Agent
from whiteboard import Whiteboard, TaskStep
from config_loader import ConferenceConfig, PromptsConfig, DEFAULT_PROMPTS, IntensityConfig as ConfigIntensityConfig
from intensity_regulator import (
    IntensityRegulator, IntensityConfig, IntensityLevel, 
    get_intensity_regulator
)
from conference_behaviors import (
    ConferenceBehaviorManager, BehaviorConfig, BehaviorType, 
    BehaviorEvent, get_behavior_manager
)
from event_bus import EventBus, EventType, Event
from exception_handler import ExceptionHandler, ExceptionType, RecoveryLevel
from evolution_engine import EvolutionEngine


@dataclass
class Proposal:
    """提案"""
    proposer_id: str
    content: str
    timestamp: float
    votes_for: List[str] = field(default_factory=list)
    votes_against: List[str] = field(default_factory=list)
    votes_modify: List[Tuple[str, str]] = field(default_factory=list)
    weights_for: float = 0.0
    weights_against: float = 0.0
    status: str = "pending"


@dataclass
class AgentSpeechState:
    """代理发言状态"""
    agent_id: str
    is_speaking: bool = False
    last_speak_time: float = 0
    speak_count: int = 0
    interruption_count: int = 0
    last_content: str = ""
    stance: str = ""  # 立场：支持方/反对方/中立观察
    stance_instruction: str = ""  # 立场指令


class ConferenceMode(BaseMode):
    """会议模式 - 智能讨论"""
    
    mode_name = "conference"
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.conf_config: ConferenceConfig = self.config.conference
        self.prompts: PromptsConfig = self.config.prompts
        
        # 争吵强度调节器
        if hasattr(self.config, 'intensity') and self.config.intensity:
            # config_loader 的 IntensityConfig 转换为 intensity_regulator 的 IntensityConfig
            cfg = self.config.intensity
            intensity_config = IntensityConfig(
                weight_complexity=cfg.weight_complexity,
                weight_divergence=cfg.weight_divergence,
                weight_time_pressure=cfg.weight_time_pressure,
                weight_consensus=cfg.weight_consensus,
                weight_emotional=cfg.weight_emotional,
                weight_importance=cfg.weight_importance,
                weight_fatigue=cfg.weight_fatigue,
                min_intensity=cfg.min_intensity,
                max_intensity=cfg.max_intensity,
                smoothing_factor=cfg.smoothing_factor
            )
            self.intensity = IntensityRegulator(intensity_config)
        else:
            self.intensity = IntensityRegulator(IntensityConfig())
        
        # 会议行为管理器
        if hasattr(self.config, 'behaviors') and self.config.behaviors:
            # config_loader 已经解析为 BehaviorConfig 对象
            if hasattr(self.config.behaviors, 'enable_agenda'):
                self._behavior_config = self.config.behaviors
            else:
                self._behavior_config = self._load_behavior_config()
        else:
            self._behavior_config = BehaviorConfig()
        self.behaviors = None  # 在 execute 中初始化，需要 whiteboard
        
        # 阶段状态
        self._phase = "discussion"
        self._current_proposal: Optional[Proposal] = None
        
        # 代理发言状态
        self._agent_states: Dict[str, AgentSpeechState] = {}
        
        # 当前发言中的代理集合
        self._speaking_agents: Set[str] = set()
        
        # 自动转串行标志
        self._should_continue_serial = False
        self._extracted_steps: List[Dict] = []
        
        # 事件总线
        self._event_bus = EventBus()
        
        # 观点追踪（用于计算分歧度）
        self._opinion_clusters: Dict[str, List[str]] = {}  # 观点 -> 支持者列表
        
        # 最后活动时间
        self._last_activity_time = time.time()
        
        # 待处理的用户输入请求
        self._pending_user_input: Optional[str] = None
        
        # 异常处理器
        self._exception_handler: Optional[ExceptionHandler] = None
        
        # 演化引擎
        self._evolution_engine: Optional[EvolutionEngine] = None
        
        # 连续工具失败计数
        self._consecutive_tool_failures: Dict[str, int] = {}
    
    def _load_intensity_config(self) -> IntensityConfig:
        """加载争吵强度配置"""
        cfg = self.config.intensity if hasattr(self.config, 'intensity') else {}
        return IntensityConfig(
            weight_complexity=cfg.get('weight_complexity', 0.15),
            weight_divergence=cfg.get('weight_divergence', 0.25),
            weight_time_pressure=cfg.get('weight_time_pressure', 0.15),
            weight_consensus=cfg.get('weight_consensus', 0.20),
            weight_emotional=cfg.get('weight_emotional', 0.10),
            weight_importance=cfg.get('weight_importance', 0.10),
            weight_fatigue=cfg.get('weight_fatigue', 0.05),
            min_intensity=cfg.get('min_intensity', 10.0),
            max_intensity=cfg.get('max_intensity', 95.0),
            smoothing_factor=cfg.get('smoothing_factor', 0.3)
        )
    
    def _load_behavior_config(self) -> BehaviorConfig:
        """加载会议行为配置"""
        cfg = getattr(self.config, 'behaviors', {})
        return BehaviorConfig(
            enable_agenda=cfg.get('enable_agenda', True),
            auto_agenda_from_proposal=cfg.get('auto_agenda_from_proposal', True),
            enable_time_reminder=cfg.get('enable_time_reminder', True),
            time_warning_thresholds=cfg.get('time_warning_thresholds', [0.5, 0.75, 0.9]),
            auto_timeout_signal=cfg.get('auto_timeout_signal', True),
            enable_off_topic_detection=cfg.get('enable_off_topic_detection', True),
            off_topic_similarity_threshold=cfg.get('off_topic_similarity_threshold', 0.3),
            off_topic_penalty=cfg.get('off_topic_penalty', 0.5),
            enable_summary=cfg.get('enable_summary', True),
            summary_interval_rounds=cfg.get('summary_interval_rounds', 5),
            auto_summary=cfg.get('auto_summary', True),
            enable_modify_motion=cfg.get('enable_modify_motion', True),
            enable_fact_check=cfg.get('enable_fact_check', True),
            enable_request_input=cfg.get('enable_request_input', True),
            enable_table_issue=cfg.get('enable_table_issue', True),
            table_threshold=cfg.get('table_threshold', 0.5),
            enable_priority_sort=cfg.get('enable_priority_sort', True),
            enable_compare_options=cfg.get('enable_compare_options', True),
            min_options_for_compare=cfg.get('min_options_for_compare', 2)
        )
    
    async def execute(self, question: str) -> ModeResult:
        """执行会议模式"""
        self._is_running = True
        self._start_time = time.time()
        
        self._initialize(question)
        self.whiteboard.clear_consensus()
        
        # 初始评估任务复杂度
        await self._assess_task_complexity(question)
        
        try:
            # === 新增：专属提示词生成阶段 ===
            print("\n[专属提示词] 正在为代理生成个性化立场提示词...")
            await self._generate_agent_stance_prompts(question)
            
            # === 议程生成阶段 ===
            print("\n[议程生成] 正在讨论生成会议议程...")
            agenda_items = await self._generate_agenda(question)
            
            if agenda_items:
                self.whiteboard.set_agenda(agenda_items)
                print(f"\n[议程已设置] 共 {len(agenda_items)} 个议程项：")
                for i, item in enumerate(agenda_items, 1):
                    print(f"  {i}. {item.get('title', '未知')}")
                    if item.get('description'):
                        print(f"     {item['description'][:50]}...")
                
                # === 议程讨论循环 ===
                while True:
                    current_agenda = self.whiteboard.get_current_agenda_item()
                    if not current_agenda:
                        print("\n[所有议程已完成]")
                        break
                    
                    print(f"\n[当前议程] {current_agenda['title']}")
                    if current_agenda.get('description'):
                        print(f"  描述：{current_agenda['description']}")
                    
                    # === 子问题投票 ===
                    sub_questions = current_agenda.get('sub_questions', [])
                    if sub_questions:
                        selected_questions = await self._vote_sub_questions(
                            current_agenda, sub_questions, question
                        )
                        current_agenda['selected_questions'] = selected_questions
                    else:
                        current_agenda['selected_questions'] = []
                    
                    # 重置讨论状态
                    self._should_stop = False
                    self._end_votes = set()
                    self._ended_agents = set()  # 重置已结束代理列表
                    
                    # 清除上一个议程的记忆和状态
                    self._reset_for_new_agenda()
                    
                    # 对当前议程进行讨论
                    await self._discussion_loop(question, current_agenda)
                    
                    # 标记当前议程完成
                    self.whiteboard.advance_agenda()
                    
                    # 检查是否所有议程都完成了
                    progress = self.whiteboard.get_agenda_progress()
                    if progress["current_index"] >= progress["total"]:
                        break
            else:
                # 无议程，直接讨论
                await self._discussion_loop(question, None)
            
            # 如果没有共识，强制生成
            if not self.whiteboard.get_final_resolution():
                await self._force_resolution(question)
            
            # 检测是否需要串行执行
            if self._should_continue_serial and self._extracted_steps:
                await self._auto_serial_phase(question)
            
            # 保存会话数据到文件
            self._save_session_data(question)
            
            return self._build_result()
            
        except Exception as e:
            return ModeResult(success=False, final_resolution="", error=str(e))
        finally:
            self._is_running = False
    
    async def _generate_agent_stance_prompts(self, question: str):
        """生成代理专属立场提示词 - 并行生成，确保立场多样对立"""
        agents = self.agent_pool.get_enabled_agents()
        if len(agents) < 2:
            return
        
        total_agents = len(agents)
        print(f"  共 {total_agents} 个代理参与立场生成")
        
        # 第一阶段：并行生成立场（AI动态生成，不预分配）
        print("\n[立场生成] AI动态生成独特立场提示词...")
        
        async def generate_stance(agent, existing_stances_text=""):
            """单个代理动态生成立场"""
            existing_info = ""
            if existing_stances_text:
                existing_info = f"""
【已生成的立场】（你应该选择不同的立场，形成对比或对立）
{existing_stances_text}
"""
            
            stance_prompt = f"""请为以下问题生成一个独特的立场提示词。

【问题】{question}

【你的性格】{agent.get_personality_prompt()}
{existing_info}
【要求】
1. 生成一句15字以内的立场提示词，体现你的独特视角
2. {"你必须与已有立场不同，形成对比或对立" if existing_stances_text else "根据问题性质，选择支持/反对/质疑/中立等立场"}
3. 立场要鲜明，不要模棱两可
4. 直接输出你的立场提示词，不要解释

【示例】
- "全力支持，强调核心价值"
- "坚决反对，指出重大风险"
- "质疑假设，追问更多依据"
- "中立观望，等待更多数据"
"""
            try:
                response = await agent.call_api(
                    [{"role": "user", "content": stance_prompt}],
                    temperature=1.0
                )
                
                if response.success and response.content:
                    content = response.content.strip().strip('"\'""''')
                    if '\n' in content:
                        content = content.split('\n')[0].strip()
                    if len(content) > 25:
                        content = content[:25]
                    return (agent.id, content)
            except:
                pass
            return (agent.id, "独立思考，理性分析")
        
        # 第一轮：并行生成立场
        tasks = [generate_stance(agent) for agent in agents]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 收集结果
        final_stances = {}
        stances_list = []
        for result in results:
            if result and not isinstance(result, Exception):
                agent_id, stance = result
                final_stances[agent_id] = stance
                stances_list.append({"agent_id": agent_id, "stance": stance})
                print(f"  [{agent_id}] {stance}")
        
        # 第二阶段：检查重复并重新生成（并行）
        print("\n[立场去重] 检查并调整重复立场...")
        
        stance_counts = {}
        for s in stances_list:
            key = s["stance"][:10]
            stance_counts[key] = stance_counts.get(key, 0) + 1
        
        need_regenerate = []
        for s in stances_list:
            key = s["stance"][:10]
            if stance_counts[key] > 1:
                need_regenerate.append(s["agent_id"])
        
        if need_regenerate:
            print(f"  发现 {len(need_regenerate)} 个重复立场，重新生成...")
            
            # 构建已存在的立场文本
            existing_stances_text = "\n".join([
                f"  - {s['agent_id']}: {s['stance']}"
                for s in stances_list
                if s['agent_id'] not in need_regenerate
            ])
            
            async def regenerate_stance(agent_id):
                for agent in agents:
                    if agent.id == agent_id:
                        response = await agent.call_api(
                            [{"role": "user", "content": f"生成一个与已有立场完全不同的立场提示词：\n问题：{question}\n\n已有立场：\n{existing_stances_text}\n\n直接输出新立场（15字内）"}],
                            temperature=1.2
                        )
                        if response.success and response.content:
                            new_stance = response.content.strip().strip('"\'""''')[:25]
                            if '\n' in new_stance:
                                new_stance = new_stance.split('\n')[0].strip()
                            return (agent_id, new_stance)
                return None
            
            regen_tasks = [regenerate_stance(aid) for aid in need_regenerate]
            regen_results = await asyncio.gather(*regen_tasks, return_exceptions=True)
            
            for result in regen_results:
                if result and not isinstance(result, Exception):
                    agent_id, new_stance = result
                    if agent_id:
                        final_stances[agent_id] = new_stance
                        print(f"  [{agent_id}] → {new_stance}")
        
        # 第三阶段：验证立场多样性
        print("\n[立场验证] 检查立场多样性...")
        
        unique_stances = set()
        for stance in final_stances.values():
            unique_stances.add(stance[:8])
        
        diversity_ratio = len(unique_stances) / len(final_stances) if final_stances else 0
        print(f"  立场多样性: {diversity_ratio:.1%} ({len(unique_stances)}/{len(final_stances)} 独特)")
        
        if diversity_ratio < 0.7:
            print("  [警告] 立场多样性不足，建议重新讨论")
        else:
            print("  [通过] 立场多样性满足要求")
        
        # 输出最终结果
        print("\n【立场提示词分配结果】")
        for agent in agents:
            if agent.id in final_stances:
                print(f"  {agent.id}: {final_stances[agent.id]}")
        
        # 应用立场提示词
        for agent_id, stance in final_stances.items():
            if agent_id in self._agent_states:
                self._agent_states[agent_id].stance_instruction = stance
                for agent in agents:
                    if agent.id == agent_id:
                        agent.custom_stance = stance
                        break
        
        print(f"\n[完成] 已为 {len(final_stances)} 个代理分配专属立场提示词")
    
    async def _vote_sub_questions(self, agenda: Dict, sub_questions: List[str], question: str) -> List[str]:
        """投票选择要讨论的子问题"""
        agents = self.agent_pool.get_enabled_agents()
        
        print(f"\n[子问题投票] 议程「{agenda['title']}」有 {len(sub_questions)} 个子问题：")
        for i, sq in enumerate(sub_questions, 1):
            print(f"  {i}. {sq}")
        
        print("\n[投票中] 各代理选择要讨论的子问题...")
        
        # 每个代理投票选择子问题
        question_votes = {i: 0 for i in range(len(sub_questions))}
        vote_details = []
        
        vote_prompt = f"""请为以下议程的子问题投票，选择你认为最需要讨论的2-3个：

议程：{agenda['title']}
描述：{agenda.get('description', '')}

子问题列表：
{chr(10).join([f"{i+1}. {sq}" for i, sq in enumerate(sub_questions)])}

请输出你选择的子问题编号（2-3个），用逗号分隔，如：1,3,4
只输出编号，不要其他内容。"""
        
        for agent in agents:
            try:
                response = await agent.call_api(
                    [{"role": "user", "content": vote_prompt}],
                    temperature=0.3
                )
                if response.success and response.content:
                    # 解析投票
                    nums = re.findall(r'\d+', response.content)
                    voted = []
                    for num_str in nums[:3]:  # 最多3票
                        num = int(num_str)
                        if 1 <= num <= len(sub_questions):
                            question_votes[num - 1] += 1
                            voted.append(str(num))
                    if voted:
                        print(f"  [{agent.id}] 选择: {','.join(voted)}")
                        vote_details.append((agent.id, voted))
            except Exception as e:
                pass
        
        # 统计结果
        print(f"\n[投票结果]")
        sorted_questions = sorted(question_votes.items(), key=lambda x: x[1], reverse=True)
        
        # 选择票数最高的2-3个（至少1票）
        selected = []
        for idx, votes in sorted_questions:
            if votes > 0 and len(selected) < 3:
                selected.append(sub_questions[idx])
                print(f"  ✓ {sub_questions[idx][:40]}... ({votes}票)")
        
        if not selected:
            # 如果没人投票，选择前2个
            selected = sub_questions[:2]
            print(f"  (默认选择前2个子问题)")
        
        return selected
    
    async def _generate_agenda(self, question: str) -> List[Dict]:
        """生成会议议程 - 让代理讨论争论后达成共识"""
        agents = self.agent_pool.get_enabled_agents()
        if not agents:
            return []
        
        # 使用配置中的议程生成提示词
        agenda_prompt = self.prompts.agenda_generation.format(question=question)
        
        all_agendas = []
        
        # 第一阶段：多个代理同时提出议程建议（并行）
        print("\n[议程生成] 多个代理同时提出议程建议...")
        
        async def agent_propose_agenda(agent):
            """单个代理提出议程"""
            try:
                response = await agent.call_api(
                    [{"role": "user", "content": agenda_prompt}],
                    tools=None,
                    temperature=0.5
                )
                
                if response.success and response.content:
                    content = response.content
                    try:
                        start, end = content.find("["), content.rfind("]") + 1
                        if start != -1 and end > start:
                            agenda = json.loads(content[start:end])
                            if isinstance(agenda, list) and len(agenda) > 0:
                                return {
                                    "proposer": agent.id,
                                    "agenda": agenda,
                                    "personality": agent.personality
                                }
                    except json.JSONDecodeError:
                        pass
            except Exception as e:
                print(f"  [{agent.id}] 生成失败: {e}")
            return None
        
        # 并行调用多个代理
        propose_tasks = [agent_propose_agenda(agent) for agent in agents[:5]]
        results = await asyncio.gather(*propose_tasks, return_exceptions=True)
        
        # 收集有效结果
        for result in results:
            if result and not isinstance(result, Exception):
                all_agendas.append(result)
                print(f"  [{result['proposer']}] 提议 {len(result['agenda'])} 个议题")
        
        if not all_agendas:
            # 生成默认议程
            print("\n[默认议程] 无议程建议，使用默认议程")
            return [{"title": question[:50], "description": "讨论核心问题"}]
        
        # 第二阶段：议程争论讨论（并行）
        print(f"\n[议程争论] 收到 {len(all_agendas)} 份议程建议，多个代理同时讨论争论...")
        
        # 构建讨论上下文
        agenda_summary = "\n".join([
            f"【{p['proposer']}的方案】\n" + 
            "\n".join([f"  {i+1}. {item.get('title', '?')}: {item.get('description', '')[:50]}" 
                      for i, item in enumerate(p['agenda'][:5])])
            for p in all_agendas
        ])
        
        # 收集争论意见（并行）
        debate_messages = []
        
        async def agent_debate_agenda(agent):
            """单个代理参与议程争论"""
            try:
                # 构建个性化提示
                personal_prompt = self.prompts.agenda_debate.format(
                    question=question,
                    agenda_summary=agenda_summary,
                    independence=agent.personality.independence,
                    cautiousness=agent.personality.cautiousness
                )

                response = await agent.call_api(
                    [{"role": "user", "content": personal_prompt}],
                    tools=None,
                    temperature=0.7
                )
                
                if response.success and response.content:
                    return {
                        "agent_id": agent.id,
                        "content": response.content,
                        "independence": agent.personality.independence
                    }
            except Exception as e:
                print(f"  [{agent.id}] 讨论失败: {e}")
            return None
        
        # 并行调用多个代理进行争论
        debate_tasks = [agent_debate_agenda(agent) for agent in agents[:5]]
        debate_results = await asyncio.gather(*debate_tasks, return_exceptions=True)
        
        # 收集并显示争论结果
        for result in debate_results:
            if result and not isinstance(result, Exception):
                debate_messages.append(result)
                content_preview = result['content'][:80] + "..." if len(result['content']) > 80 else result['content']
                print(f"  [{result['agent_id']}]: {content_preview}")
                # 记录到白板
                self.whiteboard.add_message(
                    agent_id=result['agent_id'],
                    content=f"[议程讨论] {result['content']}",
                    message_type="agenda_debate"
                )
        
        # 第三阶段：根据争论结果综合议程
        print(f"\n[议程综合] 根据讨论结果综合最终议程...")
        
        # 构建争论总结
        debate_summary = "\n".join([
            f"- {m['agent_id']}: {m['content'][:150]}"
            for m in debate_messages
        ])
        
        # 选择一个独立性中等的代理来综合议程
        synthesizer = min(agents[:5], key=lambda a: abs(a.personality.independence - 5))
        
        synthesize_prompt = self.prompts.agenda_synthesize.format(
            question=question,
            agenda_summary=agenda_summary,
            debate_summary=debate_summary
        )

        try:
            response = await synthesizer.call_api(
                [{"role": "user", "content": synthesize_prompt}],
                tools=None,
                temperature=0.3
            )
            
            if response.success and response.content:
                content = response.content
                start, end = content.find("["), content.rfind("]") + 1
                if start != -1 and end > start:
                    final_agenda = json.loads(content[start:end])
                    if isinstance(final_agenda, list) and len(final_agenda) > 0:
                        print(f"\n[最终议程] 综合各方意见后确定 {len(final_agenda)} 个议题：")
                        for i, item in enumerate(final_agenda, 1):
                            print(f"  {i}. {item.get('title', '未知')}")
                        
                        # 记录到白板
                        self.whiteboard.add_message(
                            agent_id="system",
                            content=f"[议程确定] 经过讨论争论，最终议程包含 {len(final_agenda)} 个议题",
                            message_type="system"
                        )
                        
                        # 评估议程重要性，合并低重要性议程
                        final_agenda = await self._evaluate_agenda_importance(final_agenda, question)
                        
                        return final_agenda
        except Exception as e:
            print(f"  议程综合失败: {e}")
        
        # 回退：选择支持最多的议程
        print("\n[回退] 使用原始投票机制...")
        
        # 投票决定议程
        votes = {i: 0 for i in range(len(all_agendas))}
        
        agenda_options = "\n".join([
            f"方案{i+1}（{p['proposer']}）：{len(p['agenda'])}个议题 - " + 
            ", ".join([item.get('title', '?') for item in p['agenda'][:3]])
            for i, p in enumerate(all_agendas)
        ])
        
        vote_prompt = self.prompts.agenda_vote.format(
            question=question,
            agenda_options=agenda_options,
            total=len(all_agendas)
        )
        
        for agent in agents:
            try:
                response = await agent.call_api(
                    [{"role": "user", "content": vote_prompt}],
                    tools=None,
                    temperature=0.3
                )
                
                if response.success and response.content:
                    nums = re.findall(r'\d+', response.content)
                    if nums:
                        choice = int(nums[0]) - 1
                        if 0 <= choice < len(all_agendas):
                            votes[choice] += 1
            except Exception:
                pass
        
        winner_idx = max(votes.keys(), key=lambda x: votes[x])
        winner = all_agendas[winner_idx]
        
        print(f"\n[议程确定] 方案{winner_idx+1}胜出（{votes[winner_idx]}票）")
        
        # 评估议程重要性，合并低重要性议程
        final_agenda = await self._evaluate_agenda_importance(winner["agenda"], question)
        
        return final_agenda
    
    async def _evaluate_agenda_importance(self, agenda: List[Dict], question: str) -> List[Dict]:
        """评估议程重要性，将低重要性议程转化为其他议程的子问题"""
        if len(agenda) <= 1:
            return agenda
        
        agents = self.agent_pool.get_enabled_agents()
        if not agents:
            return agenda
        
        print(f"\n[议程评估] 评估 {len(agenda)} 个议程的重要性...")
        
        # 构建议程列表
        agenda_list = "\n".join([
            f"  {i+1}. {item.get('title', '未知')}: {item.get('description', '')[:60]}"
            for i, item in enumerate(agenda)
        ])
        
        # 并行让多个代理评估每个议程的重要性
        importance_scores = {i: [] for i in range(len(agenda))}
        
        async def agent_rate_importance(agent):
            """单个代理评估议程重要性"""
            rate_prompt = f"""请评估以下每个议程对核心问题的重要性。

【核心问题】{question}

【议程列表】
{agenda_list}

【评估标准】
- 重要性评分：1-10分（10分最高）
- 判断该议程是否值得独立讨论，还是可以作为其他议程的子问题

【输出格式】JSON数组
[
  {{"index": 1, "score": 8, "is_important": true}},
  {{"index": 2, "score": 4, "is_important": false, "merge_to": 1}},
  ...
]
"""
            try:
                response = await agent.call_api(
                    [{"role": "user", "content": rate_prompt}],
                    temperature=0.3
                )
                
                if response.success and response.content:
                    content = response.content
                    start, end = content.find("["), content.rfind("]") + 1
                    if start != -1 and end > start:
                        ratings = json.loads(content[start:end])
                        return ratings
            except Exception as e:
                print(f"  [{agent.id}] 评估失败: {e}")
            return []
        
        # 并行评估
        rate_tasks = [agent_rate_importance(agent) for agent in agents[:5]]
        rate_results = await asyncio.gather(*rate_tasks, return_exceptions=True)
        
        # 收集评分
        for result in rate_results:
            if result and not isinstance(result, Exception):
                for rating in result:
                    idx = rating.get("index", 0) - 1
                    if 0 <= idx < len(agenda):
                        score = rating.get("score", 5)
                        importance_scores[idx].append(score)
        
        # 计算平均重要性分数
        avg_scores = {}
        for idx, scores in importance_scores.items():
            if scores:
                avg_scores[idx] = sum(scores) / len(scores)
            else:
                avg_scores[idx] = 5.0  # 默认中等重要性
        
        # 显示评估结果
        print("\n【议程重要性评估】")
        for idx, score in sorted(avg_scores.items(), key=lambda x: x[1], reverse=True):
            item = agenda[idx]
            status = "★ 重要" if score >= 6 else "☆ 次要"
            print(f"  {idx+1}. [{score:.1f}分] {status} - {item.get('title', '未知')}")
        
        # 找出低重要性议程（分数<6）和高重要性议程
        low_importance = [idx for idx, score in avg_scores.items() if score < 6]
        high_importance = [idx for idx, score in avg_scores.items() if score >= 6]
        
        if not low_importance:
            print("\n[评估结果] 所有议程重要性达标，无需合并")
            return agenda
        
        print(f"\n[议程合并] {len(low_importance)} 个低重要性议程将被转化为子问题...")
        
        # 构建新议程（合并低重要性议程到最相关的高重要性议程）
        new_agenda = []
        
        # 先添加高重要性议程
        for idx in high_importance:
            item = agenda[idx].copy()
            # 确保有子问题列表
            if "sub_questions" not in item:
                item["sub_questions"] = []
            new_agenda.append(item)
        
        # 将低重要性议程转化为子问题
        for low_idx in low_importance:
            low_item = agenda[low_idx]
            
            # 找到最相关的高重要性议程
            best_target_idx = self._find_related_agenda(low_item, new_agenda, question)
            
            if best_target_idx is not None:
                # 转化为子问题
                sub_question = {
                    "question": low_item.get("title", "子问题"),
                    "description": low_item.get("description", ""),
                    "importance": avg_scores[low_idx]
                }
                new_agenda[best_target_idx]["sub_questions"].append(sub_question)
                print(f"  [{low_idx+1}] \"{low_item.get('title', '未知')}\" → 合并到议程 {best_target_idx+1}")
            else:
                # 没有相关议程，保留为独立议程
                item = low_item.copy()
                if "sub_questions" not in item:
                    item["sub_questions"] = []
                new_agenda.append(item)
                print(f"  [{low_idx+1}] \"{low_item.get('title', '未知')}\" → 保留为独立议程（无相关议程）")
        
        # 显示最终议程
        print(f"\n【最终议程】共 {len(new_agenda)} 个主议题：")
        for i, item in enumerate(new_agenda, 1):
            subs = item.get("sub_questions", [])
            sub_info = f"（含 {len(subs)} 个子问题）" if subs else ""
            print(f"  {i}. {item.get('title', '未知')} {sub_info}")
            for sub in subs[:3]:  # 显示前3个子问题
                print(f"     - {sub.get('question', '?')}")
        
        # 记录到白板
        self.whiteboard.add_message(
            agent_id="system",
            content=f"[议程优化] {len(low_importance)} 个低重要性议程已转化为子问题",
            message_type="system"
        )
        
        return new_agenda
    
    def _find_related_agenda(self, low_item: Dict, high_agendas: List[Dict], question: str) -> Optional[int]:
        """找到最相关的高重要性议程（基于关键词匹配）"""
        if not high_agendas:
            return None
        
        low_title = low_item.get("title", "").lower()
        low_desc = low_item.get("description", "").lower()
        low_text = f"{low_title} {low_desc}"
        
        # 简单关键词匹配
        best_idx = None
        best_score = 0
        
        for idx, item in enumerate(high_agendas):
            high_title = item.get("title", "").lower()
            high_desc = item.get("description", "").lower()
            high_text = f"{high_title} {high_desc}"
            
            # 计算关键词重叠
            low_words = set(low_text.split())
            high_words = set(high_text.split())
            overlap = len(low_words & high_words)
            
            if overlap > best_score:
                best_score = overlap
                best_idx = idx
        
        return best_idx
    
    def _reset_for_new_agenda(self):
        """重置状态，清除上一个议程的记忆，确保每个议程独立讨论"""
        # 清除白板上的讨论消息（保留议程列表和主话题）
        self.whiteboard.clear_discussion_messages()
        
        # 清除共识状态
        self.whiteboard.clear_consensus()
        
        # 重置代理状态，清除上一个议程的记忆
        for agent_id, state in self._agent_states.items():
            state.speech_count = 0
            state.total_contribution = 0.0
            state.last_speech_time = 0
            state.consecutive_agreements = 0
            state.key_points = []
            state.opinions = []
            state.references = []
        
        # 清除步骤列表
        self._extracted_steps = []
        
        # 重置投票和状态标记
        self._should_stop = False
        self._end_votes = set()
        self._ended_agents = set()
        
        print("  [状态重置] 已清除上一议程记忆")
    
    def _initialize(self, question: str):
        """初始化"""
        agents = self.agent_pool.get_enabled_agents()
        if not agents:
            raise ValueError("没有可用的代理，请检查配置")
        
        # 设置主话题
        self.whiteboard.set_main_topic(question)
        
        # 初始化异常处理器
        self._exception_handler = ExceptionHandler(self.whiteboard)
        
        # 初始化演化引擎
        self._evolution_engine = EvolutionEngine(self.whiteboard)
        
        # 先初始化代理状态
        for agent in agents:
            self.whiteboard.init_agent_contribution(agent.id)
            self._agent_states[agent.id] = AgentSpeechState(agent_id=agent.id)
            
            # 初始化演化参数（从性格）
            personality = {
                "cautiousness": agent.personality.cautiousness,
                "empathy": agent.personality.empathy,
                "abstraction": agent.personality.abstraction
            }
            self._evolution_engine.init_agent(agent.id, personality)
            
            # 初始化工具失败计数
            self._consecutive_tool_failures[agent.id] = 0
        
        # 然后为代理分配对立立场（更新已存在的状态）
        self._assign_stances(agents, question)
        
        # 重置活动时间
        self._last_activity_time = time.time()
        
        # 初始化强度因素（提高初始强度以激发讨论）
        self.intensity.update_factors(
            task_importance=70.0,  # 提高重要性
            time_pressure=20.0,    # 降低时间压力，让讨论更充分
            current_round=1,
            max_rounds=self.conf_config.max_rounds
        )
        
        # 初始化会议行为管理器
        self.behaviors = ConferenceBehaviorManager(
            self._behavior_config, 
            whiteboard=self.whiteboard,
            event_bus=self._event_bus
        )
        self.behaviors.start_session(timeout=self.conf_config.discussion_timeout_sec)
    
    def _assign_stances(self, agents: List[Agent], question: str):
        """初始化代理状态，立场在讨论中自然形成"""
        # 不预设立场，让代理自由发言
        # 立场会在讨论过程中自然涌现
        for agent in agents:
            if agent.id in self._agent_states:
                self._agent_states[agent.id].stance = ""
                self._agent_states[agent.id].stance_instruction = ""
    
    async def _assess_task_complexity(self, question: str):
        """评估任务复杂度"""
        # 基于问题长度和关键词简单评估
        length_factor = min(len(question) / 50 * 20, 30)
        
        complexity_keywords = [
            "架构", "设计", "重构", "优化", "分析", "评估", 
            "权衡", "决策", "策略", "方案", "比较"
        ]
        keyword_factor = sum(5 for kw in complexity_keywords if kw in question)
        
        complexity = min(100, 20 + length_factor + keyword_factor)
        self.intensity.update_task_complexity(complexity)
    
    async def _discussion_loop(self, question: str, current_agenda: Optional[Dict] = None):
        """并发独立思考 - 完整防卡死机制，支持议程"""
        agents = self.agent_pool.get_enabled_agents()
        min_rounds = 4
        
        # 从配置读取参数
        idle_timeout = self.conf_config.idle_timeout_sec
        repeat_threshold = self.conf_config.repeat.similarity_threshold
        stalemate_attempts = 0  # 僵局计数
        max_stalemate = 2  # 最大僵局次数
        
        # 时间间隔配置
        think_interval = self.conf_config.intervals.think_interval
        mute_check_interval = self.conf_config.intervals.mute_check_interval
        idle_check_interval = self.conf_config.intervals.idle_check_interval
        user_input_check_interval = self.conf_config.intervals.user_input_check_interval
        
        # 重复检测配置
        single_agent_repeat_threshold = self.conf_config.repeat.single_agent_threshold
        mass_repeat_ratio = self.conf_config.repeat.mass_ratio_threshold
        repeat_penalty_sec = self.conf_config.repeat.penalty_sec
        
        # 叫停配置
        interrupt_min_participants = self.conf_config.interrupt.min_participants
        interrupt_pass_threshold = self.conf_config.interrupt.pass_threshold
        interrupt_max_wait_sec = self.conf_config.interrupt.max_wait_sec
        
        # 自动收敛配置
        auto_converge_max_attempts = self.conf_config.auto_converge.max_attempts
        
        agenda_title = current_agenda.get("title", "主议题") if current_agenda else "主议题"
        self._log(f"并发讨论 ({len(agents)} 代理) - {agenda_title}")
        
        speak_counts = {a.id: 0 for a in agents}
        total_speaks = 0
        self._end_votes = set()
        self._ended_agents = set()  # 已发送终止符号的代理，不再发言
        self._agenda_end_votes = {}  # 议程结束投票
        self._voting_proposal = None
        self._vote_lock = asyncio.Lock()
        self._last_activity_time = time.time()
        self._recent_contents = []  # 最近发言内容（用于重复检测）
        self._repeat_counts = {}  # 重复计数
        self._muted_agents = {}  # 静音代理及解禁时间
        
        def show_status_bar():
            """显示简洁状态栏"""
            # 获取会议温度
            intensity_bar = self.intensity.get_intensity_bar(width=10)
            # 发言统计
            active = sum(1 for c in speak_counts.values() if c > 0)
            end_pct = len(self._end_votes) / len(agents) * 100 if agents else 0
            
            # 议程进度
            agenda_progress = self.whiteboard.get_agenda_progress()
            agenda_info = f" | 议程:{agenda_progress['resolved']+1}/{agenda_progress['total']}" if agenda_progress['total'] > 0 else ""
            
            # 简洁输出
            print(f"\n{intensity_bar} | 活跃:{active}/{len(agents)} | 结束意向:{end_pct:.0f}%{agenda_info}")
            print("-> ", end="", flush=True)
        
        async def agent_think_loop(agent: Agent):
            """单个代理的持续思考循环"""
            import re as re_module  # 显式导入避免作用域问题
            nonlocal total_speaks
            while not self._should_stop:
                # 检查该代理是否已发送终止符号
                if agent.id in self._ended_agents:
                    await asyncio.sleep(idle_check_interval)
                    continue
                
                # 检查用户中断
                user_interrupt = await self._check_user_interrupt_nonblocking()
                if user_interrupt:
                    print(f"\n[用户插话] {user_interrupt}")
                    # 记录到白板
                    self.whiteboard.add_message(
                        agent_id="user",
                        content=user_interrupt,
                        message_type="interrupt"
                    )
                    # 传递给下一个发言的代理
                    if not hasattr(self, '_pending_user_context'):
                        self._pending_user_context = {}
                    self._pending_user_context[agent.id] = user_interrupt
                
                # 检查是否被静音
                if agent.id in self._muted_agents:
                    if time.time() < self._muted_agents[agent.id]:
                        await asyncio.sleep(mute_check_interval)
                        continue
                    else:
                        del self._muted_agents[agent.id]
                
                # 检查是否有优先思考权
                has_priority = hasattr(self, '_priority_think_agents') and agent.id in self._priority_think_agents
                priority_context = None
                if has_priority:
                    print(f"\n[优先思考] {agent.id} 正在立即思考回应...")
                    self._priority_think_agents.discard(agent.id)
                    # 获取优先思考的上下文（被叫停的原因）
                    if hasattr(self, '_priority_contexts') and agent.id in self._priority_contexts:
                        priority_context = self._priority_contexts.pop(agent.id)
                
                # 合并用户中断上下文
                user_context = None
                if hasattr(self, '_pending_user_context') and agent.id in self._pending_user_context:
                    user_context = self._pending_user_context.pop(agent.id)
                
                try:
                    combined_context = priority_context or user_context
                    if priority_context and user_context:
                        combined_context = f"{priority_context}\n\n用户插话：{user_context}"
                    result = await self._agent_speak(agent, question, speak_counts[agent.id], user_message=combined_context, current_agenda=current_agenda)
                    speak_counts[agent.id] += 1
                    total_speaks += 1
                    self._last_activity_time = time.time()
                    
                    # 显示状态栏
                    show_status_bar()
                    
                    if result:
                        # 重复检测
                        is_repeat = self._check_repeat(agent.id, result)
                        if is_repeat:
                            self._repeat_counts[agent.id] = self._repeat_counts.get(agent.id, 0) + 1
                            repeat_count = self._repeat_counts[agent.id]
                            print(f"  [重复] {agent.id} ({repeat_count}/{single_agent_repeat_threshold})")
                            # 单个代理重复达到阈值才静音
                            if repeat_count >= single_agent_repeat_threshold:
                                self._muted_agents[agent.id] = time.time() + repeat_penalty_sec
                                print(f"  [静音] {agent.id} {repeat_penalty_sec}秒")
                                continue
                        else:
                            self._repeat_counts[agent.id] = 0
                        
                        # 检查是否有大量代理在重复 - 直接增加结束意向
                        repeating_agents = sum(1 for c in self._repeat_counts.values() if c >= 1)
                        repeat_ratio = repeating_agents / len(agents) if agents else 0
                        if repeat_ratio >= mass_repeat_ratio:
                            print(f"\n[大量重复] {repeating_agents}/{len(agents)} ({repeat_ratio:.0%}) 代理在重复")
                            # 直接增加结束意向
                            end_threshold = max(2, len(agents) // 4)
                            # 为每个重复的代理添加结束票
                            for aid, cnt in self._repeat_counts.items():
                                if cnt >= 1 and aid not in self._end_votes:
                                    self._end_votes.add(aid)
                            print(f"  结束意向增加到: {len(self._end_votes)}/{end_threshold}")
                            # 重置重复计数
                            self._repeat_counts = {}
                        
                        # 记录发言内容（存储更多内容以支持改进的重复检测）
                        self._recent_contents.append(result[:300])
                        if len(self._recent_contents) > 20:
                            self._recent_contents.pop(0)
                        
                        # 更新等待队列：记录发言代理
                        if hasattr(self, '_interrupt_wait_queue') and self._interrupt_wait_queue:
                            for wait_item in self._interrupt_wait_queue[:]:
                                # 如果当前发言的不是被叫停的目标，记录发言
                                if agent.id != wait_item["target"] and agent.id != wait_item["caller"]:
                                    wait_item["other_agents_spoken"].add(agent.id)
                                
                                # 检查是否所有其他代理都已发言
                                other_agents = set(a.id for a in agents if a.id != wait_item["target"] and a.id != wait_item["caller"])
                                if other_agents and other_agents.issubset(wait_item["other_agents_spoken"]):
                                    # 所有其他代理已发言，释放目标代理
                                    print(f"\n[优先思考] {wait_item['target']} 获得优先思考权")
                                    if wait_item["target"] in self._muted_agents:
                                        del self._muted_agents[wait_item["target"]]
                                    
                                    # 标记优先思考
                                    if not hasattr(self, '_priority_think_agents'):
                                        self._priority_think_agents = set()
                                    self._priority_think_agents.add(wait_item["target"])
                                    
                                    # 保存优先思考的上下文（叫停原因）
                                    if not hasattr(self, '_priority_contexts'):
                                        self._priority_contexts = {}
                                    reason_text = wait_item.get("reason", "")
                                    caller = wait_item.get("caller", "")
                                    self._priority_contexts[wait_item["target"]] = f"[被叫停] {caller} 叫停了你。原因：{reason_text if reason_text else '需要你等待其他人发言后回应'}。请立即针对此问题进行思考回应。"
                                    
                                    # 从等待队列移除
                                    self._interrupt_wait_queue.remove(wait_item)
                        
                        # 检测叫停信号 [INTERRUPT]
                        interrupt_match = re_module.search(r'\[INTERRUPT(?::@(\w+))?\]', result, re_module.IGNORECASE)
                        if interrupt_match:
                            target_agent = interrupt_match.group(1)  # None 表示全体叫停
                            if target_agent:
                                # 指定叫停：让目标代理等待其他人发言后再思考回应
                                print(f"\n[指定叫停] {agent.id} 叫停 @{target_agent}")
                                # 记录叫停请求，目标代理进入等待思考状态
                                if not hasattr(self, '_interrupt_wait_queue'):
                                    self._interrupt_wait_queue = []
                                
                                self._interrupt_wait_queue.append({
                                    "target": target_agent,
                                    "caller": agent.id,
                                    "reason": result[interrupt_match.end():].strip()[:100] if interrupt_match.end() < len(result) else "",
                                    "waiting_since": time.time(),
                                    "other_agents_spoken": set()  # 记录已发言的其他代理
                                })
                                
                                # 目标代理临时暂停
                                self._muted_agents[target_agent] = time.time() + self.conf_config.interrupt.max_wait_sec
                                
                                # 记录到白板
                                self.whiteboard.add_message(
                                    agent_id="system",
                                    content=f"[指定叫停] {agent.id} 要求 {target_agent} 等待其他人发言后再思考回应",
                                    message_type="system"
                                )
                                print(f"  {target_agent} 将在其他代理发言后获得优先思考权")
                            else:
                                # 全体叫停：进入投票
                                print(f"\n[全体叫停] {agent.id} 发起全体叫停")
                                handled = await self._handle_interrupt(agent, result, question)
                                if handled:
                                    self._should_stop = True
                                    return
                        
                        # 检测投票信号
                        vote_match = re_module.search(r'\[VOTE:\s*(.+?)\]', result)
                        if vote_match:
                            proposal = vote_match.group(1).strip()
                            async with self._vote_lock:
                                if self._voting_proposal is None:
                                    self._voting_proposal = proposal
                                    await self._run_vote(agents, question, proposal, agent.id)
                        
                        # 检测议程结束信号 [AGENDA_END]
                        agenda_end_match = re_module.search(r'\[AGENDA_END\]', result)
                        if agenda_end_match and current_agenda:
                            self._ended_agents.add(agent.id)  # 该代理不再发言
                            # 同时计入结束意向
                            if agent.id not in self._end_votes:
                                self._end_votes.add(agent.id)
                            # 记录议程结束投票
                            vote_result = self.whiteboard.vote_end_current_agenda(
                                agent.id, True, "认为当前议程讨论充分"
                            )
                            if vote_result["success"]:
                                print(f"\n[议程结束投票] {agent.id} 同意结束当前议程")
                                print(f"  支持: {vote_result['agree_count']}/{vote_result['total_agents']}")
                                print(f"  结束意向: {len(self._end_votes)}/{len(agents)}")
                                
                                # 检查是否达到多数
                                if vote_result["should_end"]:
                                    print(f"\n[议程结束] 多数同意，进入议程投票环节")
                                    self._should_stop = True
                                    self._need_agenda_vote = True  # 标记需要议程投票
                                    return
                        
                        # 检测结束信号
                        if "[END]" in result or "[STOP]" in result:
                            self._end_votes.add(agent.id)
                            self._ended_agents.add(agent.id)  # 该代理不再发言
                            end_threshold = max(2, len(agents) // 4)
                            end_rate = len(self._end_votes) / len(agents) * 100 if agents else 0
                            print(f"\n[结束请求] {agent.id} 请求结束讨论（该代理已终止发言）")
                            print(f"  当前进度: {len(self._end_votes)}/{end_threshold} (需{end_threshold}票)")
                            print(f"  结束率: {end_rate:.0f}%")
                            print(f"  已发言轮次: {total_speaks}/{len(agents) * min_rounds} (最少需{len(agents) * min_rounds}次)")
                        
                        # 结束率100%强制结束
                        end_rate = len(self._end_votes) / len(agents) * 100 if agents else 0
                        if end_rate >= 100:
                            print(f"\n[强制结束] 结束率达到100%，立即终止讨论")
                            print(f"  总发言: {total_speaks} 次")
                            self._should_stop = True
                            self._need_agenda_vote = True  # 标记需要议程投票
                            return
                        
                        # 正常结束条件
                        if total_speaks >= len(agents) * min_rounds and len(self._end_votes) >= max(2, len(agents) // 4):
                            print(f"\n[讨论结束] 达到结束条件")
                            print(f"  总发言: {total_speaks} 次")
                            print(f"  结束票: {len(self._end_votes)}/{len(agents)}")
                            self._should_stop = True
                            self._need_agenda_vote = True  # 标记需要议程投票
                            return
                            
                except Exception as e:
                    print(f"  [错误] {agent.id}: {e}")
                    await asyncio.sleep(2)  # 错误后等待
                
                await asyncio.sleep(self.conf_config.intervals.think_interval)
        
        # 空闲超时检测
        async def idle_check():
            nonlocal stalemate_attempts
            converge_attempts = 0
            
            while not self._should_stop:
                await asyncio.sleep(self.conf_config.intervals.idle_check_interval)
                
                idle_time = time.time() - self._last_activity_time
                
                # 空闲超时 - 自动收敛
                if idle_time > idle_timeout:
                    converge_attempts += 1
                    print(f"\n[空闲检测] {idle_time:.0f}秒无活动")
                    print(f"  自动收敛尝试: {converge_attempts}/{auto_converge_max_attempts}")
                    
                    if converge_attempts >= auto_converge_max_attempts:
                        print(f"\n[系统决策] 连续{auto_converge_max_attempts}次空闲超时，强制结束讨论")
                        self._should_stop = True
                        break
                    
                    # 生成提案并投票
                    await self._auto_converge(agents, question)
        
        # 用户输入监控任务
        async def user_input_monitor():
            """持续监控用户输入"""
            while not self._should_stop:
                try:
                    user_input = await self._check_user_interrupt_nonblocking()
                    if user_input:
                        print(f"\n[用户插话] {user_input}")
                        # 记录到白板
                        self.whiteboard.add_message(
                            agent_id="user",
                            content=user_input,
                            message_type="interrupt"
                        )
                        # 通知所有代理有用户插话
                        if not hasattr(self, '_pending_user_context'):
                            self._pending_user_context = {}
                        for a in agents:
                            self._pending_user_context[a.id] = user_input
                except Exception as e:
                    pass
                await asyncio.sleep(0.5)  # 每0.5秒检查一次
        
        tasks = [asyncio.create_task(agent_think_loop(a)) for a in agents]
        tasks.append(asyncio.create_task(idle_check()))
        tasks.append(asyncio.create_task(user_input_monitor()))
        
        # 初始化议程投票标志
        self._need_agenda_vote = False
        
        try:
            while not self._should_stop:
                if all(t.done() for t in tasks[:-1]):
                    break
                await asyncio.sleep(0.1)
        finally:
            for t in tasks:
                t.cancel()
        
        print(f"[讨论统计]")
        print(f"  总发言次数: {total_speaks}")
        print(f"  结束票数: {len(self._end_votes)}/{len(agents)}")
        
        # 讨论结束后，检查是否需要进行议程投票
        if self._need_agenda_vote and current_agenda:
            await self._agenda_vote_and_review(agents, question, current_agenda)
    
    async def _agenda_vote_and_review(self, agents, question: str, current_agenda: Dict):
        """议程投票环节 - 只投票，最后一议程才复盘"""
        print("[议程投票环节] 提取方案并投票排序")
        
        try:
            # 检查是否是最后一个议程（使用白板的is_last判断）
            progress = self.whiteboard.get_agenda_progress()
            is_last_agenda = progress['is_last']  # 当前议程是否是最后一个
            
            print(f"  议程进度: {progress['progress']} (索引:{progress['current_index']}, 是否最后:{is_last_agenda})")
            
            # 1. 从白板提取所有观点/方案
            messages = self.whiteboard.get_messages()
            normal_msgs = [m for m in messages if m.message_type == "normal"]
            
            # 提取关键观点
            proposals = await self._extract_proposals(agents, normal_msgs)
            
            if not proposals:
                print("  无有效方案，跳过投票")
                # 存储空结论并推进
                agenda_title = current_agenda.get("title", "未知议题") if current_agenda else "主议题"
                self.whiteboard.store_conclusion(agenda_title, "无有效方案", [])
                if current_agenda:
                    self.whiteboard.advance_agenda()
                    print(f"\n[议程推进] 进入下一个议程")
                return
            
            print(f"\n提取到 {len(proposals)} 个方案：")
            for i, p in enumerate(proposals, 1):
                print(f"  方案{i}: {p[:60]}...")
            
            # 2. 投票排序
            ranked_proposals = await self._rank_proposals(agents, proposals)
            
            print(f"\n[投票结果] 方案优先级排序：")
            for i, (p, score) in enumerate(ranked_proposals, 1):
                print(f"  第{i}名 (得分{score}): {p[:50]}...")
            
            # 3. 存储投票结果到白板
            agenda_title = current_agenda.get("title", "未知议题") if current_agenda else "主议题"
            conclusion_text = f"方案排序：\n" + "\n".join([
                f"第{i+1}名: {p[0]}" for i, p in enumerate(ranked_proposals[:5])
            ])
            self.whiteboard.store_conclusion(agenda_title, conclusion_text, ranked_proposals)
            
            # 4. 推进到下一个议程
            if current_agenda:
                self.whiteboard.advance_agenda()
                print(f"\n[议程推进] 进入下一个议程")
            
            # 5. 只有最后一个议程才复盘
            if is_last_agenda:
                print("[最终复盘] 所有议程已完成，进行最终复盘")
                
                has_debate = await self._review_debate(agents, ranked_proposals, question)
                
                if has_debate:
                    # 有争论，继续讨论
                    print("\n[复盘] 存在分歧，继续讨论...")
                    self._should_stop = False
                    self._end_votes = set()
                    self._ended_agents = set()  # 重置已结束代理列表，允许重新发言
                    # 获取下一个议程继续讨论
                    next_agenda = self.whiteboard.get_current_agenda_item()
                    if next_agenda:
                        await self._discussion_loop(question, next_agenda)
                else:
                    # 无争论，串行输出结论
                    print("\n[复盘] 达成共识，生成最终结论")
                    await self._generate_final_conclusion(agents, question, current_agenda, ranked_proposals)
                    
        except Exception as e:
            print(f"  [议程投票错误] {e}")
            import traceback
            traceback.print_exc()
            # 即使出错也要推进议程
            if current_agenda:
                self.whiteboard.advance_agenda()
                print(f"\n[议程推进] (错误恢复) 进入下一个议程")
    
    async def _extract_proposals(self, agents, messages) -> List[str]:
        """从讨论中提取方案"""
        print(f"  [提取方案] 分析 {len(messages)} 条消息...")
        
        if not messages:
            print("  [提取方案] 无消息，尝试从代理中直接提取")
            # 如果没有消息，返回默认方案
            return ["继续讨论", "需要更多信息", "暂缓决策"]
        
        # 合并讨论内容
        discussion = "\n".join([f"{m.agent_id}: {m.content[:200]}" for m in messages[-20:]])
        print(f"  [提取方案] 讨论内容长度: {len(discussion)} 字符")
        
        prompt = self.prompts.extract_proposals.format(discussion=discussion)
        
        # 从配置获取超时时间
        timeout_sec = getattr(self.conf_config, 'extract_proposals_timeout_sec', 120)
        print(f"  [提取方案] 超时设置: {timeout_sec}秒")

        # 尝试多个代理提取，增加成功率
        for i, agent in enumerate(agents[:3]):
            try:
                print(f"  [提取方案] 尝试代理 {agent.id}...")
                # 添加超时机制
                response = await asyncio.wait_for(
                    agent.call_api(
                        [{"role": "user", "content": prompt}],
                        tools=None,
                        temperature=0.3
                    ),
                    timeout=timeout_sec
                )
                
                if response.success and response.content:
                    import json
                    import re
                    json_match = re.search(r'\[.*\]', response.content, re.DOTALL)
                    if json_match:
                        proposals = json.loads(json_match.group())
                        if isinstance(proposals, list) and len(proposals) > 0:
                            print(f"  [提取方案] 成功提取 {len(proposals)} 个方案")
                            return proposals[:10]  # 最多10个方案
                    print(f"  [提取方案] 响应格式异常: {response.content[:100]}...")
                else:
                    error_msg = response.error if response else 'unknown'
                    print(f"  [提取方案] API调用失败: {error_msg}")
            except asyncio.TimeoutError:
                print(f"  [提取方案] 代理 {agent.id} 超时({timeout_sec}s)，尝试下一个...")
            except Exception as e:
                print(f"  [提取方案] 代理 {agent.id} 提取失败: {e}")
        
        # 如果所有代理都失败，从消息中提取关键词作为方案
        print("  [提取方案] 所有代理失败，使用消息摘要作为方案")
        keywords = []
        for msg in messages[-10:]:
            content = msg.content[:50]
            if len(keywords) < 5:
                keywords.append(f"观点: {content}...")
        return keywords if keywords else ["默认方案：继续讨论"]
    
    async def _rank_proposals(self, agents, proposals: List[str]) -> List[tuple]:
        """投票排序方案"""
        print(f"\n[投票排序] 开始对 {len(proposals)} 个方案投票...")
        
        if not proposals:
            print("  [警告] 无方案可投票，返回空列表")
            return []
        
        proposal_list = "\n".join([f"{i+1}. {p}" for i, p in enumerate(proposals)])
        print(f"  [投票选项]\n{proposal_list}")
        
        scores = {i+1: 0 for i in range(len(proposals))}
        vote_count = 0
        min_votes = max(3, len(agents) // 2)  # 至少3票或半数
        
        for agent in agents:
            try:
                prompt = self.prompts.proposal_ranking.format(proposal_list=proposal_list)

                response = await agent.call_api(
                    [{"role": "user", "content": prompt}],
                    tools=None,
                    temperature=0.3
                )
                
                if response.success and response.content:
                    import re
                    numbers = re.findall(r'\d+', response.content)
                    if numbers:
                        # 按顺序计分，第一名得len分，第二名得len-1分...
                        valid_votes = []
                        for rank, num in enumerate(numbers[:len(proposals)]):
                            idx = int(num)
                            if 1 <= idx <= len(proposals):
                                scores[idx] += len(proposals) - rank
                                valid_votes.append(str(idx))
                        if valid_votes:
                            vote_count += 1
                            print(f"  [{agent.id}] 投票: {','.join(valid_votes)}")
                            
                            if vote_count >= min_votes:
                                print(f"  [投票完成] 已收集 {vote_count} 票")
                                break
                    else:
                        print(f"  [{agent.id}] 响应无数字: {response.content[:50]}...")
                else:
                    print(f"  [{agent.id}] API调用失败")
            except Exception as e:
                print(f"  [{agent.id}] 投票失败: {e}")
        
        if vote_count == 0:
            print("  [警告] 无有效投票，使用默认排序（按原始顺序）")
        
        # 按得分排序
        ranked = sorted([(proposals[i-1], scores[i]) for i in scores.keys()],  
                       key=lambda x: x[1], reverse=True)
        return ranked
    
    async def _review_debate(self, agents, ranked_proposals: List[tuple], question: str) -> bool:
        """复盘讨论，返回是否有争论"""
        proposals_text = "\n".join([f"第{i+1}名: {p[0]} (得分{p[1]})" 
                                   for i, p in enumerate(ranked_proposals)])
        
        # 快速询问是否有分歧
        agree_count = 0
        disagree_count = 0
        
        for agent in agents[:min(5, len(agents))]:  # 最多问5个代理
            prompt = self.prompts.review_debate.format(question=question, proposals_text=proposals_text)

            try:
                response = await agent.call_api(
                    [{"role": "user", "content": prompt}],
                    tools=None,
                    temperature=0.3
                )
                
                if response.success and response.content:
                    if "同意" in response.content and "需要讨论" not in response.content:
                        agree_count += 1
                        print(f"  [{agent.id}] 同意排序结果")
                    else:
                        disagree_count += 1
                        print(f"  [{agent.id}] 需要讨论: {response.content[:50]}...")
            except Exception as e:
                pass
        
        # 如果超过1/3的人需要讨论，则有争论
        return disagree_count > agree_count // 2
    
    async def _generate_final_conclusion(self, agents, question: str, current_agenda: Dict, 
                                         ranked_proposals: List[tuple] = None):
        """调用串行模式生成最终结论"""
        messages = self.whiteboard.get_messages()
        discussion = "\n".join([f"{m.agent_id}: {m.content[:150]}" for m in messages[-30:]])
        
        # 收集所有提议
        proposals_context = ""
        all_proposals = []
        if ranked_proposals:
            all_proposals = [p[0] for p in ranked_proposals]
            proposals_context = "\n方案排序：\n" + "\n".join([
                f"第{i+1}名: {p[0]}" for i, p in enumerate(ranked_proposals[:5])
            ])
        
        # 收集议程结论
        agenda_conclusions = []
        agenda = self.whiteboard.get_agenda()
        if agenda:
            for item in agenda:
                if item.get('conclusion'):
                    agenda_conclusions.append({
                        "title": item.get('title', ''),
                        "conclusion": item['conclusion']
                    })
        
        print("\n[串行模式] 调用串行模式生成最终结论...")
        print(f"  传递 {len(all_proposals)} 个提议")
        print(f"  传递 {len(agenda_conclusions)} 个议程结论")
        
        try:
            # 创建串行模式实例
            serial_mode = EnhancedSerialMode(
                agent_pool=self.agent_pool,
                whiteboard=self.whiteboard,
                workspace=self.workspace,
                tool_router=self.tool_router,
                config=self.config
            )
            
            # 执行结论生成任务，传递所有上下文
            result = await serial_mode.execute(
                question=question,
                proposals=all_proposals,
                agenda_conclusions=agenda_conclusions,
                discussion=discussion[:2000]
            )
            
            if result and result.success and result.final_resolution:
                conclusion = result.final_resolution
                
                # 存储结论到白板
                agenda_title = current_agenda.get("title", "主议题") if current_agenda else "主议题"
                self.whiteboard.store_conclusion(agenda_title, conclusion, ranked_proposals)
                
                # 清晰有序输出最终结论
                print(f"\n【最终结论】")
                print(conclusion)
                print(f"\n[已存储] 结论已保存到白板")
                
                # 设置议程结论
                if current_agenda:
                    self.whiteboard.set_agenda_conclusion(conclusion)
            else:
                # 串行模式失败，回退到简单生成
                error_msg = result.error if result else "未知错误"
                print(f"  串行模式未返回结果({error_msg})，使用简单生成...")
                prompt = self.prompts.final_conclusion.format(
                    question=question,
                    proposals_context=proposals_context,
                    discussion=discussion[:2000]
                )
                response = await agents[0].call_api(
                    [{"role": "user", "content": prompt}],
                    tools=None,
                    temperature=0.3
                )
                if response.success and response.content:
                    conclusion = response.content
                    agenda_title = current_agenda.get("title", "主议题") if current_agenda else "主议题"
                    self.whiteboard.store_conclusion(agenda_title, conclusion, ranked_proposals)
                    print(f"\n[最终结论]")
                    print(conclusion)
                    
        except Exception as e:
            print(f"  生成结论失败: {e}")
    
    def _check_repeat(self, agent_id: str, content: str) -> bool:
        """检测发言是否重复 - 改进版"""
        if not self._recent_contents:
            return False
        
        # 清理内容，移除常见前缀
        clean_content = content.strip()
        prefixes_to_remove = [
            "支持以上各位提出的观点",
            "支持以上各位",
            "我认为",
            "我同意",
            "我支持",
        ]
        for prefix in prefixes_to_remove:
            if clean_content.startswith(prefix):
                clean_content = clean_content[len(prefix):].strip()
                break
        
        # 检查多个片段
        check_points = [
            clean_content[:80],      # 开头
            clean_content[80:160] if len(clean_content) > 80 else "",  # 中间
            clean_content[-80:] if len(clean_content) > 80 else clean_content,  # 结尾
        ]
        
        repeat_count = 0
        for recent in self._recent_contents[-15:]:
            recent_clean = recent.strip()
            # 同样清理前缀
            for prefix in prefixes_to_remove:
                if recent_clean.startswith(prefix):
                    recent_clean = recent_clean[len(prefix):].strip()
                    break
            
            # 检查是否有多个检查点匹配
            matches = 0
            for point in check_points:
                if point and (point in recent_clean or recent_clean in point):
                    matches += 1
            
            # 如果2个或以上检查点匹配，认为是重复
            if matches >= 2:
                repeat_count += 1
        
        # 如果超过1/3的最近内容都相似，认为是重复
        return repeat_count >= 5
    
    async def _auto_converge(self, agents, question: str):
        """自动收敛 - 生成提案并投票"""
        print("\n[自动收敛] 系统生成提案...")
        
        messages = self.whiteboard.get_messages()
        normal_msgs = [m for m in messages if m.message_type == "normal"][-10:]
        
        if not normal_msgs:
            print("  无足够讨论内容，跳过收敛")
            return
        
        # 提取最近讨论的关键内容
        content = "\n".join([m.content[:100] for m in normal_msgs])
        proposal = f"基于讨论的方案：{content[:200]}"
        
        print(f"  提案摘要: {proposal[:80]}...")
        
        await self._run_vote(agents, question, proposal, "system")
    
    async def _run_vote(self, agents, question: str, proposal: str, proposer_id: str):
        """执行投票 - 含僵局检测，展示决策过程"""
        print(f"[投票发起] {proposer_id} 提议:")
        print(f"  {proposal[:100]}")
        print("\n[投票过程]")
        
        votes = {"support": [], "oppose": []}
        
        for agent in agents:
            if agent.id == proposer_id:
                votes["support"].append(agent.id)
                print(f"  [{agent.id}] 支持 (提案者)")
                continue
            
            try:
                prompt = self.prompts.vote_prompt.format(proposal=proposal)
                
                response = await agent.call_api(
                    [{"role": "user", "content": prompt}],
                    tools=None,
                    temperature=0.3
                )
                
                if response.success and response.content:
                    vote_text = response.content.strip()
                    if "支持" in vote_text or "同意" in vote_text:
                        votes["support"].append(agent.id)
                        print(f"  [{agent.id}] 支持")
                    else:
                        votes["oppose"].append(agent.id)
                        print(f"  [{agent.id}] 反对")
            except Exception:
                votes["support"].append(agent.id)
                print(f"  [{agent.id}] 支持 (默认)")
        
        # 统计结果
        support_rate = len(votes["support"]) / len(agents) if agents else 0
        print(f"\n[投票结果]")
        print(f"  支持: {len(votes['support'])} 票 ({support_rate*100:.0f}%)")
        print(f"  反对: {len(votes['oppose'])} 票 ({(1-support_rate)*100:.0f}%)")
        
        # 僵局检测：支持率在45%-55%之间
        if 0.45 <= support_rate <= 0.55:
            if not hasattr(self, '_stalemate_count'):
                self._stalemate_count = 0
            self._stalemate_count += 1
            print(f"\n[僵局检测] 支持率接近50%，进入僵局处理 ({self._stalemate_count}/2)")
            
            if self._stalemate_count >= 2:
                print("\n[僵局破解] 启动排序投票...")
                await self._ranked_vote(agents, question)
            return
        
        if support_rate >= 0.5:
            print(f"\n[决策] 投票通过，方案生效")
            self.whiteboard.set_final_resolution(proposal)
            self._should_stop = True
        else:
            print(f"\n[决策] 投票未通过，继续讨论")
            self._voting_proposal = None
    
    async def _ranked_vote(self, agents, question: str):
        """排序投票（波达计数）- 展示决策过程"""
        messages = self.whiteboard.get_messages()
        normal_msgs = [m for m in messages if m.message_type == "normal"][-10:]
        
        # 提取候选方案
        proposals = []
        for m in normal_msgs:
            if any(k in m.content for k in ["建议", "方案", "应该", "提议"]):
                proposals.append(m.content[:100])
        
        if not proposals:
            proposals = [m.content[:100] for m in normal_msgs[-3:]]
        
        print(f"\n[排序投票] 提取到 {len(proposals)} 个候选方案:")
        for i, p in enumerate(proposals):
            print(f"  方案{i+1}: {p[:50]}...")
        
        print("\n[投票过程]")
        
        # 每个代理对方案排序打分
        scores = [0] * len(proposals)
        agent_rankings = {}  # 记录每个代理的排序
        
        for agent in agents:
            try:
                proposals_text = chr(10).join([f'{i+1}. {p}' for i, p in enumerate(proposals)])
                prompt = self.prompts.ranked_vote_prompt.format(proposals=proposals_text)
                
                response = await agent.call_api(
                    [{"role": "user", "content": prompt}],
                    tools=None,
                    temperature=0.3
                )
                
                if response.success and response.content:
                    # 解析排序
                    nums = re.findall(r'\d+', response.content)
                    ranking = []
                    for rank, num in enumerate(nums[:len(proposals)]):
                        idx = int(num) - 1
                        if 0 <= idx < len(proposals):
                            scores[idx] += len(proposals) - rank
                            ranking.append(str(idx + 1))
                    
                    agent_rankings[agent.id] = ranking
                    print(f"  [{agent.id}] 排序: {','.join(ranking)}")
            except Exception:
                pass
        
        # 显示得分
        print(f"\n[计分结果]")
        for i, (p, s) in enumerate(zip(proposals, scores)):
            print(f"  方案{i+1}: {s} 分")
        
        # 选择得分最高的
        winner_idx = scores.index(max(scores)) if scores else 0
        winner = proposals[winner_idx] if proposals else "讨论结束"
        
        print(f"\n[最终决策] 方案{winner_idx+1}胜出")
        print(f"  {winner}")
        
        self.whiteboard.set_final_resolution(winner)
        self._should_stop = True
    
    async def _run_round(self, agents: List[Agent], question: str, round_num: int):
        """运行一轮讨论（保留兼容）"""
        self._log(f"第 {round_num + 1} 轮")
        await self._concurrent_speak(agents, question, round_num)
        
        # 异常检测
        await self._check_exceptions()
    
    async def _concurrent_speak(self, agents: List[Agent], question: str, round_num: int):
        """并发发言 - 所有代理同时发言，强度影响内容和氛围"""
        # 选择要发言的代理（0表示无限制）
        available = []
        max_msg = self.conf_config.max_messages_per_agent
        for a in agents:
            if max_msg > 0 and self._agent_states[a.id].speak_count >= max_msg:
                continue
            available.append(a)
        
        if not available:
            self._log(f"没有可用代理发言")
            return
        
        self._log(f"{len(available)} 个代理开始并发发言...")
        
        # 所有代理并发发言
        tasks = [
            self._agent_speak(agent, question, round_num)
            for agent in available
        ]
        
        # 并行执行所有发言
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 统计成功/失败
        success_count = 0
        error_count = 0
        for agent, result in zip(available, results):
            if isinstance(result, Exception):
                self._log(f"[错误] {agent.id} 发言失败: {result}")
                error_count += 1
            elif result:
                success_count += 1
        
        self._log(f"发言完成: {success_count} 成功, {error_count} 失败")
        
        # 检查是否有叫停
        for agent, result in zip(available, results):
            if isinstance(result, str) and self._check_interrupt(result):
                handled = await self._handle_interrupt(agent, result, question)
                if handled:
                    return
    
    async def _agent_speak(self, agent: Agent, question: str, round_num: int, user_message: str = None, current_agenda: Dict = None) -> Optional[str]:
        """代理发言 - 支持用户插话和议程上下文"""
        import re  # 在函数开头导入
        state = self._agent_states[agent.id]
        
        # 根据强度调整提示词
        intensity_hint = self._get_intensity_hint()
        
        # 获取讨论历史
        discussion_history = self._get_recent_messages(10)
        
        # 获取议程信息
        agenda_context = ""
        if current_agenda:
            agenda_context = f"""
【当前议程】
标题：{current_agenda.get('title', '未知')}
描述：{current_agenda.get('description', '')}
"""
            # 显示选中的子问题
            selected_qs = current_agenda.get('selected_questions', [])
            if selected_qs:
                agenda_context += "\n【需要讨论的子问题】\n"
                for i, sq in enumerate(selected_qs, 1):
                    agenda_context += f"  {i}. {sq}\n"
            
            agenda_context += "\n你可以使用 [AGENDA_END] 表示你认为当前议程讨论充分，可以进入下一个议程。"
            
            # 显示议程进度
            progress = self.whiteboard.get_agenda_progress()
            agenda_context += f"\n议程进度：第 {progress['resolved']+1}/{progress['total']} 个议程"
        
        # 获取暂存问题和子话题
        shelved = self.whiteboard.get_shelved_issues(status="shelved")
        sub_topics = self.whiteboard.get_sub_topics(status="pending")
        
        context_parts = []
        if sub_topics:
            context_parts.append("待讨论子话题：" + "; ".join([s["content"][:30] for s in sub_topics[:3]]))
        if shelved:
            context_parts.append("暂存问题：" + "; ".join([s["content"][:30] for s in shelved[:3]]))
        
        extra_context = "\n".join(context_parts) if context_parts else ""
        
        # 获取最大轮次配置
        max_rounds = getattr(self.config, 'max_rounds', 5) if hasattr(self, 'config') else 5
        
        # 提取原始问题中的约束条件提醒
        constraint_reminder = self._extract_constraints(question)
        
        prompt = self.prompts.conference_discussion.format(
            identity=agent.id,
            personality=agent.get_personality_prompt(),
            topic=question,
            round=round_num + 1,
            max_rounds=max_rounds,
            discussion_history=discussion_history if discussion_history else "（暂无讨论）"
        )
        
        # 加入代理专属立场提示词
        stance_instruction = getattr(state, 'stance_instruction', None) or getattr(agent, 'custom_stance', None)
        if stance_instruction:
            prompt = f"[专属立场] {stance_instruction}\n\n{prompt}"
        
        # 加入长期记忆
        memory_prompt = self.whiteboard.get_long_term_memory_prompt()
        if memory_prompt:
            prompt = f"{memory_prompt}\n\n{prompt}"
        
        if constraint_reminder:
            prompt += f"\n\n=== 约束条件（必须验证） ===\n{constraint_reminder}"
        
        if agenda_context:
            prompt += f"\n{agenda_context}"
        
        if extra_context:
            prompt += f"\n\n{extra_context}"
        
        if intensity_hint:
            prompt += f"\n\n当前讨论氛围：{intensity_hint}"
        
        # 用户插话提示
        if user_message:
            prompt += f"\n\n[用户插话] {user_message}\n请针对用户的插话进行回应。"
        
        user_msg = f"请根据以上信息发表你的观点："
        
        # 合并消息
        combined_msg = f"[系统指令]\n{prompt}\n\n[用户消息]\n{user_msg}"
        messages = [{"role": "user", "content": combined_msg}]
        
        # 获取工具（如果代理有允许的工具）
        tools = None
        if self.tool_router and agent.allowed_tools:
            tools = self.tool_router.get_common_tools_for_agent(list(agent.allowed_tools))
        
        temperature = 0.3
        
        response = await agent.call_api(messages, tools=tools, temperature=temperature)
        
        # 处理工具调用
        if response.success and response.tool_calls:
            for tool_call in response.tool_calls:
                try:
                    tool_result = await self.tool_router.execute(
                        tool_call["name"],
                        tool_call.get("arguments", {}),
                        agent.id,
                        self.whiteboard
                    )
                    # 将工具结果加入消息
                    messages.append({"role": "assistant", "content": None, "tool_calls": [tool_call]})
                    messages.append({"role": "tool", "content": str(tool_result)})
                    # 再次调用获取最终回复
                    response = await agent.call_api(messages, tools=tools, temperature=temperature)
                except Exception as e:
                    print(f"  [工具错误] {tool_call['name']}: {e}")
        
        if response.success and response.content:
            state.speak_count += 1
            state.last_speak_time = time.time()
            state.last_content = response.content
            self._last_activity_time = time.time()
            
            # 解析扩展信号
            expand_match = re.search(r'\[EXPAND:\s*(.+?)\]', response.content)
            if expand_match:
                expand_content = expand_match.group(1).strip()
                issue = self.whiteboard.expand_issue(expand_content, agent.id)
                print(f"  [扩展议题] {expand_content[:50]} - 待投票")
                # 记录到演化引擎
                if self._evolution_engine:
                    self._evolution_engine.record_speak(agent.id)
            
            # 解析暂存信号
            park_match = re.search(r'\[PARK\]', response.content)
            if park_match:
                current = self.whiteboard.get_current_issue()
                if current:
                    parked = self.whiteboard.park_issue(current["id"], f"由{agent.id}提议")
                    print(f"  [暂存议题] {current['content'][:50]}")
            
            # 解析恢复信号
            restore_match = re.search(r'\[RESTORE\s+(\S+)\]', response.content)
            if restore_match:
                parked_id = restore_match.group(1).strip()
                restored = self.whiteboard.restore_issue(parked_id)
                if restored:
                    print(f"  [恢复议题] {restored['content'][:50]}")
            
            # 兼容旧信号
            sub_topic_match = re.search(r'\[子话题\]\s*(.+?)(?:\n|$)', response.content)
            if sub_topic_match:
                sub_topic_content = sub_topic_match.group(1).strip()
                self.whiteboard.expand_issue(sub_topic_content, agent.id)
                print(f"  [新子话题] {sub_topic_content[:50]}")
            
            msg_type = "interrupt" if "[INTERRUPT]" in response.content else "normal"
            
            # 提取立场和发言内容 - 兼容多种格式，冒号可选
            # 格式: [立场：支持] 或 【立场】支持 或 [立场]支持 或 【立场：支持】
            stance_match = re.search(r'[\[【]立场[：:]?\s*([^\]】\n]+)', response.content)
            stance = stance_match.group(1).strip() if stance_match else "中立"
            # 清理立场中的多余字符
            stance = re.sub(r'[\]：:]', '', stance).strip()
            
            # 提取所有标签内容并组合显示
            display_parts = []
            for tag in ['给人看', '核心观点', '建议', '分析过程']:
                match = re.search(rf'[【\[]{tag}[\]：:]*\s*([^\n【\[]+)', response.content)
                if match:
                    content = match.group(1).strip()
                    if content:
                        display_parts.append(content)
            
            # 组合所有内容
            if display_parts:
                display_content = " | ".join(display_parts)
            else:
                # fallback：提取立场后的第一句话
                display_content = re.sub(r'[\[【]立场[：:]?[^\]】\n]*[\]】]?\s*', '', response.content).strip()
                display_content = re.sub(r'[【\[][^】\]]*[\]：:]*\s*', '', display_content).strip()
                if '\n' in display_content:
                    display_content = display_content.split('\n')[0].strip()
            
            # 截断显示（稍微长一点）
            content_preview = display_content[:150] + "..." if len(display_content) > 150 else display_content
            
            # 立场颜色标记
            stance_colors = {
                "支持": "\033[32m", "反对": "\033[31m", "质疑": "\033[33m", 
                "补充": "\033[36m", "修正": "\033[35m", "中立": "\033[37m"
            }
            stance_color = stance_colors.get(stance[:2], "\033[37m")
            reset_color = "\033[0m"
            
            print(f"  {agent.id} {stance_color}[{stance}]{reset_color} {content_preview}")
            
            self.whiteboard.add_message(
                agent_id=agent.id,
                content=response.content,
                message_type=msg_type
            )
            
            # 更新贡献
            self.whiteboard.record_contribution(agent.id, len(response.content))
            
            # 追踪观点
            self._track_opinion(agent.id, response.content)
            
            # 更新情感温度
            self._update_emotional_temperature(response.content)
            
            return response.content
        else:
            return None
    
    def _extract_constraints(self, question: str) -> str:
        """从原始问题中提取约束条件，提醒代理验证方案是否满足"""
        import re
        constraints = []
        
        # 提取数字约束（预算、热量、数量等）
        numbers = re.findall(r'(\d+(?:\.\d+)?)\s*(元|卡|千卡|公斤|斤|克|ml|毫升|人|天|周|次)', question)
        for num, unit in numbers:
            constraints.append(f"- {num}{unit}")
        
        # 提取关键词约束
        keywords = re.findall(r'(不超过|至少|最多|最少|必须|禁止|避免|确保|保证|需要)', question)
        for kw in keywords:
            # 找关键词后面的内容
            pattern = rf'{kw}([^，。！？\n]{{1,30}})'
            match = re.search(pattern, question)
            if match:
                constraints.append(f"- {kw}{match.group(1).strip()}")
        
        if constraints:
            return "原始问题要求：\n" + "\n".join(constraints[:8]) + "\n\n注意：发言前请检查方案是否满足以上约束条件！"
        return ""
    
    def _get_intensity_hint(self) -> str:
        """获取强度提示（影响发言内容激烈程度，不影响并发顺序）"""
        level = self.intensity.level
        hints = {
            IntensityLevel.HARMONY: "保持礼貌温和的语气，理性表达观点，尊重他人意见",
            IntensityLevel.MILD: "可以适当表达不同意见，保持建设性讨论氛围",
            IntensityLevel.MODERATE: "积极表达观点，可以适度反驳，推进讨论深入",
            IntensityLevel.INTENSE: "坚持己见！强力反驳不合理观点，据理力争！",
            IntensityLevel.FIERCE: "全力捍卫你的立场！毫不退让！激烈辩论！"
        }
        return hints.get(level, "")
    
    def _track_opinion(self, agent_id: str, content: str):
        """追踪观点（用于计算分歧度，立场在讨论中自然形成）"""
        # 提取核心观点 - 寻找明确的立场表达
        import re
        
        # 匹配"我认为/建议/觉得...应该/是..."等观点句式
        opinion_patterns = [
            r'我认为[，,：:]*([^。！？\n]+)',
            r'我建议[，,：:]*([^。！？\n]+)',
            r'我(觉得|主张|提议)[，,：:]*([^。！？\n]+)',
            r'(应该|必须|需要|应当)[^。！？\n]+',
            r'(不应该|不必|无需|反对)[^。！？\n]+',
            r'问题是[，,：:]*([^。！？\n]+)',
            r'(优点|缺点|好处|坏处)是[，,：:]*([^。！？\n]+)',
        ]
        
        extracted_opinions = []
        for pattern in opinion_patterns:
            matches = re.findall(pattern, content)
            for match in matches:
                if isinstance(match, tuple):
                    match = ''.join(match)
                opinion = match.strip()[:50]  # 限制长度
                if len(opinion) > 5:
                    extracted_opinions.append(opinion)
        
        # 记录观点
        for opinion in extracted_opinions[:2]:  # 每次最多记录2个观点
            if opinion not in self._opinion_clusters:
                self._opinion_clusters[opinion] = []
            if agent_id not in self._opinion_clusters[opinion]:
                self._opinion_clusters[opinion].append(agent_id)
                
                # 更新代理的立场
                if agent_id in self._agent_states:
                    self._agent_states[agent_id].stance = opinion[:30]
    
    def _update_emotional_temperature(self, content: str):
        """更新情感温度"""
        # 根据发言内容调整温度
        heat_words = ["绝对", "必须", "完全错误", "不可接受", "强烈反对", "荒谬"]
        cool_words = ["也许", "可能", "一定程度上", "我理解", "有道理"]
        
        delta = 0
        for word in heat_words:
            if word in content:
                delta += 3
        
        for word in cool_words:
            if word in content:
                delta -= 2
        
        if delta != 0:
            self.intensity.adjust_emotional_temperature(delta)
        
        # 更新分歧度
        self._update_divergence()
    
    def _update_divergence(self):
        """更新观点分歧度"""
        if not self._opinion_clusters:
            return
        
        # 计算观点分布的熵
        total_opinions = sum(len(supporters) for supporters in self._opinion_clusters.values())
        if total_opinions == 0:
            return
        
        import math
        entropy = 0
        for supporters in self._opinion_clusters.values():
            p = len(supporters) / total_opinions
            if p > 0:
                entropy -= p * math.log2(p)
        
        # 熵越高，分歧越大
        max_entropy = math.log2(len(self._opinion_clusters)) if len(self._opinion_clusters) > 1 else 1
        divergence = (entropy / max_entropy * 100) if max_entropy > 0 else 50
        
        self.intensity.update_opinion_divergence(divergence)
    
    def _get_recent_messages(self, count: int) -> str:
        """获取最近消息"""
        messages = self.whiteboard.get_messages()
        # 过滤掉系统消息
        normal_messages = [m for m in messages if m.message_type == "normal"]
        recent = normal_messages[-count:] if len(normal_messages) > count else normal_messages
        if not recent:
            return ""
        return "\n".join([f"[{m.agent_id}]: {m.content[:200]}..." if len(m.content) > 200 else f"[{m.agent_id}]: {m.content}" for m in recent])
    
    def _check_interrupt(self, content: str) -> bool:
        """检查叫停"""
        return "[INTERRUPT]" in content.upper()
    
    async def _handle_interrupt(self, interrupter: Agent, content: str, question: str) -> bool:
        """处理全体叫停 - 需要多人投票表决"""
        
        # 提取观点
        viewpoint_content = self._extract_viewpoint(content)
        if not viewpoint_content:
            viewpoint_content = f"{interrupter.id}认为讨论已有结论"
        
        print(f"\n[全体叫停请求] {interrupter.id} 发起，提议：{viewpoint_content[:60]}...")
        
        # === 第一步：叫停投票（至少5人参与，3人以上同意）===
        agents = self.agent_pool.get_enabled_agents()
        
        # 构建投票提示
        interrupt_vote_prompt = self.prompts.interrupt_vote.format(
            interrupter=interrupter.id,
            viewpoint=viewpoint_content
        )

        votes = {"agree": 0, "disagree": 0}
        vote_details = []
        participants = 0
        min_participants = self.conf_config.interrupt.min_participants
        pass_threshold = self.conf_config.interrupt.pass_threshold
        
        print(f"\n[叫停投票] 需要{min_participants}人参与，{pass_threshold}人同意通过")
        
        for agent in agents:
            if participants >= min_participants + 2:  # 最多多收集2票
                break
                
            try:
                response = await agent.call_api(
                    [{"role": "user", "content": interrupt_vote_prompt}],
                    tools=None,
                    temperature=0.3
                )
                
                if response.success and response.content:
                    # 解析投票
                    try:
                        # 尝试提取JSON
                        json_match = re.search(r'\{[^{}]*"vote"[^{}]*\}', response.content)
                        if json_match:
                            vote_data = json.loads(json_match.group())
                            vote = vote_data.get("vote", "").lower()
                            reason = vote_data.get("reason", "")[:50]
                            
                            if vote in ["agree", "support", "同意", "支持"]:
                                votes["agree"] += 1
                                vote_type = "同意"
                            else:
                                votes["disagree"] += 1
                                vote_type = "反对"
                            
                            participants += 1
                            vote_details.append((agent.id, vote_type, reason))
                            print(f"  [{agent.id}] {vote_type}: {reason}")
                    except (json.JSONDecodeError, KeyError):
                        pass
                    
            except Exception as e:
                print(f"  [{agent.id}] 投票失败: {e}")
        
        # 检查投票结果
        print(f"\n[投票结果] 参与{participants}人，同意{votes['agree']}人，反对{votes['disagree']}人")
        
        if participants < min_participants:
            print(f"[叫停失败] 参与人数不足{min_participants}人，继续讨论")
            self.whiteboard.add_message(
                agent_id="system",
                content=f"[叫停失败] 参与人数不足（{participants}/{min_participants}），继续讨论",
                message_type="system"
            )
            return False
        
        if votes["agree"] < pass_threshold:
            print(f"[叫停失败] 同意人数不足{pass_threshold}人，继续讨论")
            self.whiteboard.add_message(
                agent_id="system",
                content=f"[叫停失败] 同意人数不足（{votes['agree']}/{pass_threshold}），继续讨论",
                message_type="system"
            )
            return False
        
        # === 第二步：叫停通过，进入观点表决 ===
        print(f"\n[叫停通过] {votes['agree']}人同意，进入观点表决")
        
        self._phase = "voting"
        
        # 降温（叫停意味着要收敛）
        self.intensity.decrease_heat(15)
        
        # 添加到白板观点列表
        viewpoint = self.whiteboard.add_viewpoint(viewpoint_content, interrupter.id)
        
        # 开始投票
        self.whiteboard.start_viewpoint_vote(viewpoint["id"])
        
        self.whiteboard.add_message(
            agent_id="system",
            content=f"[叫停通过] {votes['agree']}/{participants}人同意。观点：{viewpoint_content[:100]}",
            message_type="system"
        )
        
        print(f"\n[观点表决] 观点：{viewpoint_content[:80]}...")
        
        # 表决
        return await self._voting_phase(question)
    
    def _extract_viewpoint(self, content: str) -> str:
        """提取观点（不是方案）"""
        # 移除INTERRUPT标记
        viewpoint = re.sub(r'\[INTERRUPT\]', '', content, flags=re.IGNORECASE).strip()
        # 移除常见的开场白
        viewpoint = re.sub(r'^(停！?\s*|我认为[：:]?\s*|我提议[：:]?\s*|结论[：:]?\s*)', '', viewpoint, flags=re.IGNORECASE)
        viewpoint = re.sub(r'^结论[：:]?\s*', '', viewpoint)
        return viewpoint.strip() if len(viewpoint.strip()) > 5 else ""
    
    def _extract_proposal(self, content: str) -> str:
        """提取提案（保留兼容）"""
        match = re.search(r'\[INTERRUPT\].*?(我提议[：:]?.+|$)', content, re.IGNORECASE | re.DOTALL)
        if match:
            proposal = match.group(1).strip()
            proposal = re.sub(r'^(停！?\s*|我提议[：:]?\s*)', '', proposal, flags=re.IGNORECASE)
            return proposal if len(proposal) > 10 else ""
        return ""
    
    async def _voting_phase(self, question: str) -> bool:
        """投票阶段 - 同意/反对，反对必须提新观点"""
        voting_viewpoint = self.whiteboard.get_voting_viewpoint()
        if not voting_viewpoint:
            return False
        
        agents = self.agent_pool.get_enabled_agents()
        threshold = self.conf_config.consensus_threshold
        
        # 收集投票
        for agent in agents:
            if agent.id == voting_viewpoint["agent_id"]:
                # 提案者自动支持
                self.whiteboard.vote_viewpoint(voting_viewpoint["id"], agent.id, "support")
                continue
            
            vote_result = await self._get_vote(agent, question, voting_viewpoint)
            if vote_result:
                vote, reason, new_viewpoint = vote_result
                
                # 反对时必须提出新观点
                if vote == "oppose" and not new_viewpoint:
                    new_viewpoint = f"{agent.id}持不同意见：{reason[:50]}"
                
                self.whiteboard.vote_viewpoint(
                    voting_viewpoint["id"], 
                    agent.id, 
                    vote, 
                    new_viewpoint
                )
                
                vote_display = "同意" if vote == "support" else "反对"
                print(f"  [{agent.id}] {vote_display}: {reason[:50]}...")
                
                self.whiteboard.add_message(
                    agent_id=agent.id,
                    content=f"[投票] {vote_display}: {reason[:80]}",
                    message_type="vote"
                )
        
        # 获取更新后的观点
        viewpoints = self.whiteboard.get_viewpoints()
        current_vp = next((v for v in viewpoints if v["id"] == voting_viewpoint["id"]), None)
        
        if not current_vp:
            return False
        
        # 计算支持率
        total = current_vp["support_count"] + current_vp["oppose_count"]
        support_rate = current_vp["support_count"] / total if total > 0 else 0
        
        print(f"\n[投票结果] 支持率：{support_rate*100:.0f}% (阈值{threshold*100:.0f}%)")
        
        self.whiteboard.add_message(
            agent_id="system",
            content=f"[结果] 支持率：{support_rate*100:.0f}%",
            message_type="system"
        )
        
        if support_rate >= threshold:
            # 通过
            self.whiteboard.end_viewpoint_vote(passed=True)
            self.whiteboard.set_final_resolution(current_vp["content"])
            self._phase = "done"
            print(f"[通过] 观点已采纳")
            return True
        else:
            # 未通过，继续讨论
            self.whiteboard.end_viewpoint_vote(passed=False)
            self._phase = "discussion"
            
            # 检查是否有新观点被提出
            new_viewpoints = [v for v in self.whiteboard.get_viewpoints() 
                            if v["status"] == "active" and v["id"] != voting_viewpoint["id"]]
            
            if new_viewpoints:
                print(f"[继续] 观点未通过，有 {len(new_viewpoints)} 个新观点待讨论")
            else:
                print(f"[继续] 观点未通过，继续讨论")
            
            # 升温继续讨论
            self.intensity.increase_heat(10)
            return False
    
    async def _get_vote(self, agent: Agent, question: str, viewpoint: Dict) -> Optional[Tuple[str, str, Optional[str]]]:
        """获取投票 - 同意/反对，反对时可提新观点"""
        prompt = self.prompts.conference_voting.format(
            topic=question,
            proposal=viewpoint["content"],
            proposer=viewpoint["agent_id"]
        )

        response = await agent.call_api([{"role": "user", "content": prompt}], temperature=0.3)
        
        if not response.success:
            return ("support", "默认同意", None)
        
        try:
            content = response.content
            start, end = content.find("{"), content.rfind("}") + 1
            if start != -1 and end > start:
                data = json.loads(content[start:end])
                return (
                    data.get("vote", "support"),
                    data.get("reason", ""),
                    data.get("new_viewpoint")
                )
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
            pass
        
        return ("support", "解析失败默认同意", None)
    
    def _reach_consensus(self):
        """达成共识"""
        if not self._current_proposal:
            return
        
        self._phase = "done"
        self._current_proposal.status = "approved"
        
        # 共识时大幅降温
        self.intensity.update_factors(
            opinion_divergence=10,
            emotional_temperature=30,
            consensus_progress=100
        )
        
        self.whiteboard.add_consensus(
            content=self._current_proposal.content,
            supporters=self._current_proposal.votes_for,
            weight=self._current_proposal.weights_for
        )
        
        self.whiteboard.set_final_resolution(self._current_proposal.content)
        
        # 检查是否需要串行执行
        if self._check_need_serial_execution(self._current_proposal.content):
            self._should_continue_serial = True
    
    def _check_need_serial_execution(self, content: str) -> bool:
        """检测是否需要串行执行"""
        content_lower = content.lower()
        return any(kw in content_lower for kw in self.conf_config.serial_execution_keywords)
    
    async def _extract_steps_from_proposal(self, proposal: str) -> List[Dict]:
        """从提案中提取步骤"""
        agents = self.agent_pool.get_enabled_agents()
        if not agents:
            return []
        
        agent = agents[0]
        
        prompt = self.prompts.task_extraction.format(proposal=proposal)
        response = await agent.call_api([{"role": "user", "content": prompt}], temperature=0.3)
        
        if response.success:
            try:
                content = response.content
                start, end = content.find("["), content.rfind("]") + 1
                if start != -1 and end > start:
                    return json.loads(content[start:end])
            except (json.JSONDecodeError, AttributeError, TypeError):
                pass
        
        return [{"step_id": 1, "description": proposal[:200], "expected_output": "执行结果", "suggested_tools": []}]
    
    async def _auto_serial_phase(self, question: str):
        """自动转入串行执行"""
        self.whiteboard.add_message(
            agent_id="system",
            content=f"[系统] 自动转入串行执行：{len(self._extracted_steps)} 个步骤",
            message_type="system"
        )
        
        task_steps = []
        for i, step in enumerate(self._extracted_steps):
            task_steps.append(TaskStep(
                step_id=i + 1,
                description=step.get("description", f"步骤{i+1}"),
                expected_output=step.get("expected_output", ""),
                suggested_tools=step.get("suggested_tools", []),
                status="pending"
            ))
        
        self.whiteboard.set_task_queue(task_steps)
        
        for task in task_steps:
            if self._should_stop:
                break
            
            agents = self.agent_pool.get_enabled_agents()
            if not agents:
                self.whiteboard.update_task_status(task.step_id, "failed", "无可用代理")
                break
            
            agent = next((a for a in agents if a.supports_tools), None) or agents[0]
            
            self.whiteboard.update_task_status(task.step_id, "in_progress")
            
            result = await self._execute_step(agent, task)
            
            if result:
                self.whiteboard.update_task_status(task.step_id, "completed", result)
            else:
                self.whiteboard.update_task_status(task.step_id, "failed", "执行失败")
    
    async def _execute_step(self, agent: Agent, task: TaskStep) -> Optional[str]:
        """执行步骤"""
        previous = "\n".join([
            f"步骤{t.step_id}: {t.result[:100]}"
            for t in self.whiteboard.get_task_queue()
            if t.status == "completed" and t.result
        ])
        
        prompt = self.prompts.step_execution.format(
            step_id=task.step_id,
            description=task.description,
            expected_output=task.expected_output,
            previous=previous or "无"
        )
        
        messages = [{"role": "user", "content": prompt}]
        tools = self._get_tool_schemas_for_agent(agent) if agent.supports_tools else None
        
        response = await agent.call_api(messages, tools=tools)
        
        if response.success:
            if response.tool_calls:
                await agent.execute_tool_calls(response.tool_calls, self.whiteboard)
            return response.content
        
        return None
    
    async def _check_should_end(self, question: str) -> Tuple[bool, str]:
        """判断是否应该结束讨论"""
        messages = self.whiteboard.get_messages()
        normal_msgs = [m for m in messages if m.message_type == "normal"]
        
        # 至少5轮讨论
        if len(normal_msgs) < 15:
            return False, ""
        
        # 检查最近发言是否有叫停信号
        for m in normal_msgs[-5:]:
            if "[INTERRUPT]" in m.content.upper():
                return True, "收到叫停信号"
        
        # 简单判断：讨论足够多就结束
        if len(normal_msgs) >= 30:
            return True, "讨论已充分"
        
        return False, ""
    
    async def _force_resolution(self, question: str):
        """生成最终结论 - 使用串行模式审议总结（支持用户插话）"""
        messages = self.whiteboard.get_messages()
        normal_msgs = [m for m in messages if m.message_type == "normal"]
        
        print(f"[最终决议] 收集了 {len(normal_msgs)} 条讨论")
        
        # 收集所有讨论内容
        all_content = "\n".join([
            f"[{m.agent_id}]: {m.content}"
            for m in normal_msgs
        ])
        
        if not all_content:
            print("[警告] 讨论未产生有效结果")
            self.whiteboard.set_final_resolution("讨论未产生有效结果")
            return
        
        agents = self.agent_pool.get_enabled_agents()
        if not agents:
            print("[警告] 无可用代理")
            self.whiteboard.set_final_resolution(all_content)
            return
        
        # 初始生成总结
        print("\n[生成中] 正在汇总讨论结果...")
        current_summary = await self._generate_summary(agents[0], question, all_content)
        
        if not current_summary:
            self.whiteboard.set_final_resolution(all_content)
            return
        
        print(f"\n[初始总结预览]：{current_summary[:150]}...")
        print("\n提示：审议过程中可随时输入 /interrupt <内容> 插话修改总结方向")
        
        # 使用串行模式审议总结（支持用户插话）
        print("\n[串行审议] 代理轮流审议总结...")
        final_summary = await self._serial_review_summary(question, all_content, current_summary)
        
        # 输出最终决议
        print(f"\n【最终决议】")
        print(final_summary)
        
        self.whiteboard.set_final_resolution(final_summary)
    
    async def _serial_review_summary(self, question: str, discussion: str, 
                                       initial_summary: str) -> str:
        """使用串行模式审议总结 - 代理轮流发言修改，支持用户插话"""
        agents = self.agent_pool.get_enabled_agents()
        current_summary = initial_summary
        max_rounds = 2  # 最多2轮审议
        user_interrupt = None  # 用户插话内容
        
        # 检查用户插话的异步任务
        async def check_user_input():
            """非阻塞检查用户输入"""
            try:
                # 尝试从事件总线获取用户插话
                event = await asyncio.wait_for(
                    self._event_bus.get_event("user_interrupt"),
                    timeout=0.1
                )
                if event:
                    return event.content
            except asyncio.TimeoutError:
                pass
            return None
        
        for round_num in range(max_rounds):
            print(f"\n[审议轮次 {round_num + 1}/{max_rounds}]")
            
            votes = {"support": 0, "oppose": 0}
            suggestions = []
            
            # 串行：每个代理轮流发言
            for i, agent in enumerate(agents):
                # 检查用户插话
                user_input = await self._check_user_interrupt_nonblocking()
                if user_input:
                    print(f"\n[用户插话] {user_input}")
                    # 根据用户插话调整总结
                    current_summary = await self._apply_user_intervention(
                        question, discussion, current_summary, user_input
                    )
                    print(f"\n[已调整总结]：{current_summary[:150]}...")
                    user_interrupt = user_input
                
                print(f"\n  [{i+1}/{len(agents)}] {agent.id} 审议中...")
                
                # 构建审议提示词（包含用户插话信息）
                user_context = f"\n\n用户指示：{user_interrupt}" if user_interrupt else ""
                
                # 使用配置中的审议提示词
                review_prompt = self.prompts.summary_review.format(
                    question=question,
                    summary=current_summary,
                    discussion=discussion[:800] + user_context
                )

                try:
                    response = await agent.call_api(
                        [{"role": "user", "content": review_prompt}],
                        tools=None,
                        temperature=0.3
                    )
                    
                    if response.success and response.content:
                        content = response.content
                        try:
                            start, end = content.find("{"), content.rfind("}") + 1
                            if start != -1 and end > start:
                                data = json.loads(content[start:end])
                                agree = data.get("agree", True)
                                reason = data.get("reason", "")
                                suggestion = data.get("suggestion", "")
                                
                                if agree:
                                    votes["support"] += 1
                                    print(f"    ✓ 同意: {reason[:40]}...")
                                else:
                                    votes["oppose"] += 1
                                    print(f"    ✗ 反对: {reason[:40]}...")
                                    if suggestion:
                                        suggestions.append((agent.id, suggestion))
                                        print(f"    建议: {suggestion[:50]}...")
                        except json.JSONDecodeError:
                            votes["support"] += 1
                            print(f"    ✓ 同意（默认）")
                    else:
                        votes["support"] += 1
                        print(f"    ✓ 同意（无响应）")
                        
                except Exception as e:
                    votes["support"] += 1
                    print(f"    ✓ 同意（错误）")
            
            # 计算支持率
            support_rate = votes["support"] / len(agents)
            print(f"\n[本轮结果] 支持: {votes['support']}/{len(agents)} ({support_rate*100:.0f}%)")
            
            # 支持率>=60%通过
            if support_rate >= 0.6:
                print(f"\n[决议通过] 总结获得多数支持")
                return current_summary
            
            # 有建议则修改
            if suggestions:
                print(f"\n[修改总结] 根据 {len(suggestions)} 条建议修改...")
                suggestions_text = "\n".join([
                    f"- [{aid}]: {sug}" for aid, sug in suggestions
                ])
                
                # 如果有用户插话，加入修改提示
                user_hint = f"\n用户指示：{user_interrupt}\n" if user_interrupt else ""
                
                # 使用配置中的修改提示词
                revise_prompt = self.prompts.summary_revise.format(
                    current_summary=current_summary,
                    suggestions=suggestions_text + user_hint,
                    discussion=discussion[:1000]
                )
                
                try:
                    response = await agents[0].call_api(
                        [{"role": "user", "content": revise_prompt}],
                        tools=None,
                        temperature=0.3
                    )
                    if response.success and response.content:
                        current_summary = response.content
                        print(f"\n[新总结预览]：{current_summary[:150]}...")
                except Exception as e:
                    print(f"[错误] 修改失败: {e}")
            else:
                # 无建议但未通过，直接返回
                print(f"\n[决议完成] 无修改建议")
                return current_summary
        
        print(f"\n[决议完成] 达到最大审议轮数")
        return current_summary
    
    async def _check_user_interrupt_nonblocking(self) -> Optional[str]:
        """非阻塞检查用户插话 - 直接获取用户输入"""
        # 1. 检查白板上是否有用户插话消息
        messages = self.whiteboard.get_messages()
        for msg in reversed(messages[-10:]):
            if msg.agent_id == "user" and msg.message_type == "interrupt":
                # 标记已处理，避免重复
                return msg.content
        
        # 2. 检查 pending_user_input
        if hasattr(self, '_pending_user_input') and self._pending_user_input:
            result = self._pending_user_input
            self._pending_user_input = None
            return result
        
        # 3. 异步读取标准输入（支持左右箭头移动光标）
        try:
            import sys
            import select
            
            # 尝试启用 readline 支持（左右箭头移动光标）
            try:
                import readline
                # 启用基本功能
                readline.parse_and_bind('set editing-mode emacs')
                readline.parse_and_bind('Control-left: backward-word')
                readline.parse_and_bind('Control-right: forward-word')
            except ImportError:
                pass  # Termux 可能没有 readline，忽略
            
            # 使用asyncio异步读取
            loop = asyncio.get_event_loop()
            
            # 检查是否有输入可用（非阻塞）
            if hasattr(sys.stdin, 'buffer'):
                try:
                    # 设置超短超时检测
                    ready, _, _ = select.select([sys.stdin], [], [], 0)
                    if ready:
                        # 有输入，异步读取（使用 input() 以支持 readline）
                        def read_with_readline():
                            try:
                                # 使用 input() 可以利用 readline 的功能
                                return input()
                            except EOFError:
                                return ""
                            except Exception:
                                # 回退到 readline
                                return sys.stdin.readline().rstrip('\n')
                        
                        line = await loop.run_in_executor(None, read_with_readline)
                        if line:
                            user_input = line.strip()
                            # 忽略纯命令（以/开头的保持原有功能）
                            if user_input and not user_input.startswith('/'):
                                return user_input
                except Exception:
                    pass
        except Exception:
            pass
        
        return None
    
    async def _apply_user_intervention(self, question: str, discussion: str,
                                         current_summary: str, user_input: str) -> str:
        """根据用户插话调整总结"""
        agents = self.agent_pool.get_enabled_agents()
        if not agents:
            return current_summary
        
        # 使用配置中的用户干预调整提示词
        adjust_prompt = self.prompts.user_intervention_adjust.format(
            question=question,
            current_summary=current_summary,
            user_input=user_input,
            discussion=discussion[:800]
        )
        
        try:
            response = await agents[0].call_api(
                [{"role": "user", "content": adjust_prompt}],
                tools=None,
                temperature=0.3
            )
            if response.success and response.content:
                return response.content
        except Exception as e:
            print(f"[错误] 调整失败: {e}")
        
        return current_summary
    
    async def _generate_summary(self, summarizer: Agent, question: str, discussion: str) -> Optional[str]:
        """生成会议总结"""
        summary_prompt = self.prompts.final_resolution_prompt.format(
            question=question,
            discussion=discussion
        )
        
        try:
            response = await summarizer.call_api(
                [{"role": "user", "content": summary_prompt}],
                tools=None,
                temperature=0.3
            )
            if response.success and response.content:
                return response.content
        except Exception as e:
            print(f"[错误] 生成总结失败: {e}")
        
        return None
    
    def _extract_resolution_from_discussion(self, messages: list, question: str):
        """从讨论中提取完整方案"""
        all_content = "\n\n".join([f"[{m.agent_id}]: {m.content}" for m in messages])
        self.whiteboard.set_final_resolution(all_content)
    
    def _log(self, message: str):
        """记录日志"""
        # 打印到控制台
        print(f"  {message}")
        
        # 记录到 logging
        import logging
        logger = logging.getLogger(__name__)
        logger.info(message)
        
        # 记录到白板
        self.whiteboard.add_message(
            agent_id="system",
            content=message,
            message_type="system"
        )
    
    async def _check_exceptions(self):
        """检测异常并尝试恢复"""
        if not self._exception_handler:
            return
        
        # 检测僵局
        deadlock = self._exception_handler.check_deadlock()
        if deadlock:
            success, result = await self._exception_handler.recover(deadlock)
            if success:
                # 降低共识阈值
                self.conf_config.consensus_threshold = max(0.5, self.conf_config.consensus_threshold - 0.1)
                self._log(f"[僵局恢复] {result}")
        
        # 检测代理失控
        for agent_id in self._agent_states:
            out_of_control = self._exception_handler.check_agent_out_of_control(agent_id)
            if out_of_control:
                success, result = await self._exception_handler.recover(out_of_control)
                if success:
                    # 静音代理
                    if agent_id in self._agent_states:
                        self._agent_states[agent_id].is_speaking = False
                    self._log(f"[失控恢复] {result}")
    
    async def _handle_exception(self, exception):
        """处理异常"""
        if not self._exception_handler:
            return
        
        success, result = await self._exception_handler.recover(exception)
        
        # 记录到白板
        self.whiteboard.add_exception_record(
            exception_type=exception.exception_type.value,
            details=exception.details,
            recovery_action=exception.recovery_action,
            recovery_result=result
        )
        
        return success, result
    
    async def _handle_behavior_event(self, event: BehaviorEvent):
        """处理会议行为事件"""
        behavior_type = event.behavior_type
        
        if behavior_type == BehaviorType.TIME_REMINDER:
            # 时间提醒
            remaining = event.data.get("remaining_ratio", 0)
            self.whiteboard.add_message(
                agent_id="system",
                content=f"[时间] 剩余 {remaining*100:.0f}% 的讨论时间",
                message_type="system"
            )
            
        elif behavior_type == BehaviorType.OFF_TOPIC:
            # 离题检测
            speaker_id = event.data.get("speaker_id", "unknown")
            similarity = event.data.get("similarity", 0)
            self.whiteboard.add_message(
                agent_id="system",
                content=f"[提醒] {speaker_id} 的发言可能偏离议题（相关度 {similarity*100:.0f}%），请回归主题",
                message_type="system"
            )
            # 降低该代理的贡献分
            self.whiteboard.adjust_contribution(speaker_id, -0.1)
            
        elif behavior_type == BehaviorType.SUMMARY:
            # 讨论总结
            summary = event.data.get("summary", "")
            key_points = event.data.get("key_points", [])
            self.whiteboard.add_message(
                agent_id="system",
                content=f"[总结] {summary}\n要点：{', '.join(key_points[:5])}",
                message_type="system"
            )
            
        elif behavior_type == BehaviorType.SET_AGENDA:
            # 议程设置
            items = event.data.get("items", [])
            self.whiteboard.set_agenda(items)
            self.whiteboard.add_message(
                agent_id="system",
                content=f"[议程] 已设置：{len(items)} 个议题",
                message_type="system"
            )
            
        elif behavior_type == BehaviorType.MODIFY_MOTION:
            # 修正动议
            motion = event.data.get("motion", "")
            proposer = event.data.get("proposer", "system")
            self.whiteboard.add_message(
                agent_id="system",
                content=f"[修正] {proposer} 提出修正动议：{motion[:100]}",
                message_type="system"
            )
            
        elif behavior_type == BehaviorType.FACT_CHECK:
            # 事实核查请求
            claim = event.data.get("claim", "")
            requester = event.data.get("requester", "system")
            self.whiteboard.add_message(
                agent_id="system",
                content=f"[核查] {requester} 请求核查：{claim[:100]}",
                message_type="system"
            )
            # 触发事实核查（如果有相关工具）
            await self._trigger_fact_check(claim, requester)
            
        elif behavior_type == BehaviorType.REQUEST_INPUT:
            # 请求外部输入
            question = event.data.get("question", "")
            requester = event.data.get("requester", "system")
            self.whiteboard.add_message(
                agent_id="system",
                content=f"[请求] {requester} 请求外部输入：{question[:100]}",
                message_type="system"
            )
            # 标记需要用户输入
            self._pending_user_input = question
            
        elif behavior_type == BehaviorType.TABLE_ISSUE:
            # 搁置争议
            issue = event.data.get("issue", "")
            reason = event.data.get("reason", "")
            proposer = event.data.get("proposer", "system")
            self.whiteboard.add_pending_issue(issue, reason, proposer)
            self.whiteboard.add_message(
                agent_id="system",
                content=f"[搁置] {proposer} 建议搁置争议：{issue[:50]}（{reason}）",
                message_type="system"
            )
            
        elif behavior_type == BehaviorType.PRIORITY_SORT:
            # 优先级排序
            items = event.data.get("items", [])
            sorted_items = sorted(items, key=lambda x: x.get("priority", 0), reverse=True)
            self.whiteboard.add_message(
                agent_id="system",
                content=f"[排序] 议题优先级排序完成",
                message_type="system"
            )
            
        elif behavior_type == BehaviorType.COMPARE_OPTIONS:
            # 方案对比
            options = event.data.get("options", [])
            comparison = event.data.get("comparison", {})
            self.whiteboard.add_message(
                agent_id="system",
                content=f"[对比] 方案对比：{len(options)} 个选项",
                message_type="system"
            )
    
    async def _trigger_fact_check(self, claim: str, requester: str):
        """触发事实核查"""
        # 简单实现：记录需要核查的内容
        # 实际可以实现自动调用搜索工具等
        self.whiteboard.set_metadata(f"fact_check_{requester}", {
            "claim": claim,
            "status": "pending",
            "timestamp": time.time()
        })
    
    def _save_session_data(self, question: str):
        """保存会话数据到文件"""
        import os
        from datetime import datetime
        
        # 获取会话目录
        session_path = self.workspace.session_path if self.workspace else None
        if not session_path:
            return
        
        try:
            # 保存讨论记录
            messages = self.whiteboard.get_messages()
            if messages:
                discussion_data = {
                    "question": question,
                    "timestamp": datetime.now().isoformat(),
                    "total_messages": len(messages),
                    "messages": [
                        {
                            "agent_id": m.agent_id,
                            "content": m.content,
                            "type": m.message_type,
                            "time": m.timestamp
                        }
                        for m in messages
                    ]
                }
                discussion_file = os.path.join(session_path, "discussion.json")
                with open(discussion_file, "w", encoding="utf-8") as f:
                    json.dump(discussion_data, f, ensure_ascii=False, indent=2)
                print(f"[保存] 讨论记录 -> discussion.json")
            
            # 保存结论
            resolution = self.whiteboard.get_final_resolution()
            if resolution:
                conclusion_file = os.path.join(session_path, "conclusion.txt")
                with open(conclusion_file, "w", encoding="utf-8") as f:
                    f.write(f"问题: {question}\n\n")
                    f.write(f"结论:\n{resolution}\n")
                print(f"[保存] 最终结论 -> conclusion.txt")
            
            # 保存议程结果
            agenda = self.whiteboard.get_agenda()
            if agenda:
                agenda_data = {
                    "total_items": len(agenda),
                    "agenda": agenda
                }
                agenda_file = os.path.join(session_path, "agenda.json")
                with open(agenda_file, "w", encoding="utf-8") as f:
                    json.dump(agenda_data, f, ensure_ascii=False, indent=2)
                print(f"[保存] 议程记录 -> agenda.json")
            
            # 保存代理立场
            stance_data = {}
            for agent_id, state in self._agent_states.items():
                if hasattr(state, 'stance_instruction') and state.stance_instruction:
                    stance_data[agent_id] = state.stance_instruction
            if stance_data:
                stance_file = os.path.join(session_path, "stances.json")
                with open(stance_file, "w", encoding="utf-8") as f:
                    json.dump(stance_data, f, ensure_ascii=False, indent=2)
                print(f"[保存] 代理立场 -> stances.json")
                
        except Exception as e:
            print(f"[警告] 保存会话数据失败: {e}")
    
    def _build_result(self) -> ModeResult:
        """构建结果"""
        # 触发演化
        evolution_result = None
        if self._evolution_engine:
            evolution_result = self._evolution_engine.on_session_end()
            if evolution_result:
                self.whiteboard.set_evolution_data(evolution_result)
        
        # 获取最终决议
        resolution = self.whiteboard.get_final_resolution()
        
        # 输出最终决议
        print(f"[会议结论]")
        if resolution:
            print(resolution)
        else:
            print("讨论结束，未达成明确结论")
        
        # 生成复盘摘要
        review_summary = self.whiteboard.generate_review_summary()
        
        # 获取所有消息
        messages = self.whiteboard.get_messages()
        
        # 获取所有观点
        viewpoints = self.whiteboard.get_viewpoints()
        
        # 获取异常摘要
        exception_summary = {}
        if self._exception_handler:
            exception_summary = self._exception_handler.get_exception_summary()
        
        # 格式化输出
        output_parts = []
        
        # 最终决议
        if resolution:
            output_parts.append(f"最终结论：{resolution}")
        
        # 通过的观点
        passed_vps = [v for v in viewpoints if v.get("status") == "passed"]
        if passed_vps:
            output_parts.append(f"\n通过的观点：")
            for vp in passed_vps:
                output_parts.append(f"  - {vp['content'][:100]}")
        
        # 子话题状态
        sub_topics = self.whiteboard.get_sub_topics()
        if sub_topics:
            output_parts.append(f"\n子话题：")
            for st in sub_topics:
                status_cn = {"pending": "待讨论", "discussing": "讨论中", "resolved": "已解决", "shelved": "已暂存"}
                output_parts.append(f"  - [{status_cn.get(st['status'], st['status'])}] {st['content'][:50]}")
        
        # 暂存问题
        shelved = self.whiteboard.get_shelved_issues(status="shelved")
        if shelved:
            output_parts.append(f"\n暂存问题：")
            for issue in shelved:
                output_parts.append(f"  - {issue['content'][:50]}")
        
        formatted_output = "\n".join(output_parts)
        
        return ModeResult(
            success=True,
            final_resolution=resolution or "讨论结束",
            messages=messages,
            metrics={
                "viewpoints_count": len(viewpoints),
                "passed_viewpoints": len(passed_vps),
                "sub_topics_count": len(sub_topics),
                "shelved_issues_count": len(shelved),
                "review_summary": review_summary,
                "exceptions": exception_summary
            },
            intermediate_results={
                "viewpoints": viewpoints,
                "sub_topics": sub_topics,
                "shelved_issues": self.whiteboard.get_shelved_issues(),
                "evolution": evolution_result
            }
        )
    
    def get_status(self) -> Dict:
        """获取当前状态"""
        return {
            "phase": self._phase,
            "intensity": self.intensity.get_status(),
            "agent_states": {
                aid: {
                    "speak_count": s.speak_count,
                    "interruption_count": s.interruption_count
                }
                for aid, s in self._agent_states.items()
            },
            "behaviors": {
                "enabled": self.behaviors is not None,
                "round": getattr(self.behaviors, '_current_round', 0),
                "pending_events": len(self.behaviors._event_queue) if self.behaviors else 0,
                "agenda_index": self.whiteboard.get_current_agenda_index() if hasattr(self.whiteboard, 'get_current_agenda_index') else 0
            },
            "pending_user_input": getattr(self, '_pending_user_input', None)
        }


def format_conference_output(result: ModeResult) -> str:
    """格式化会议输出"""
    if not result.success:
        return f"[会议失败] {result.error or '未知错误'}"
    
    lines = []
    
    # 最终决议
    if result.final_resolution:
        lines.append("\n【最终决议】")
        lines.append(result.final_resolution)
    
    # 议程结论
    if result.agenda_conclusions:
        lines.append("\n【议程结论】")
        for ac in result.agenda_conclusions:
            lines.append(f"  [{ac.get('agenda', '?')}] {ac.get('conclusion', '')[:100]}")
    
    # 提案列表
    if result.proposals:
        lines.append("\n【采纳提案】")
        for i, p in enumerate(result.proposals[:3], 1):
            lines.append(f"  {i}. {p.get('title', '提案')}: {p.get('summary', '')[:80]}")
    
    # 步骤列表
    if result.steps:
        lines.append("\n【执行步骤】")
        for i, step in enumerate(result.steps[:5], 1):
            lines.append(f"  {i}. {step}")
    
    return "\n".join(lines) if lines else "[会议完成]"