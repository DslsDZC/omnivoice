"""事件驱动代理 - 支持真正并行、可打断的代理模型"""
import asyncio
import time
import json
import re
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, field
from enum import Enum

from event_bus import EventBus, Event, EventType, get_event_bus, create_event, EventFormatter
from speech_controller import SpeechController, SpeechState, get_speech_controller, SpeechRequest
from suggestion_manager import SuggestionManager, SuggestionResponse, get_suggestion_manager, Suggestion
from config_loader import AgentConfig, PromptsConfig


class AgentAction(Enum):
    """代理行动类型"""
    SPEAK = "speak"                 # 发言
    INTERRUPT = "interrupt"         # 打断
    SUGGEST = "suggest"             # 提建议
    CALL_TOOL = "call_tool"         # 调用工具
    VOTE = "vote"                   # 投票
    CALL_STOP = "call_stop"         # 叫停
    IDLE = "idle"                   # 空闲
    WAIT = "wait"                   # 等待


@dataclass
class AgentDecision:
    """代理决策结果"""
    action: AgentAction
    content: str = ""
    target_id: Optional[str] = None
    priority: int = 0
    confidence: float = 0.5
    reason: str = ""


# 默认决策提示词
DECISION_PROMPT = """你是一个参与争论的代理。根据最近的讨论，决定你的下一步行动。

你的身份：{identity}
你的性格：谨慎度{cautiousness}/10，共情度{empathy}/10，抽象度{abstraction}/10

最近讨论：
{context}

可选行动（输出JSON）：
1. 发言：{{"action": "speak", "content": "你的发言内容（30字内）"}}
2. 打断：{{"action": "interrupt", "content": "打断理由", "target": "目标代理ID"}}
3. 建议：{{"action": "suggest", "content": "建议内容", "target": "目标代理ID"}}
4. 等待：{{"action": "wait", "reason": "等待原因"}}
5. 叫停：{{"action": "call_stop", "content": "你的最终提案"}}

请选择一个行动并输出JSON："""


class EventAgent:
    """事件驱动代理"""
    
    def __init__(self, config: AgentConfig, prompts: PromptsConfig = None):
        self.config = config
        self.id = config.id
        self.personality = config.personality
        self.prompts = prompts or PromptsConfig()
        
        # 事件系统
        self.event_bus = get_event_bus()
        self.speech_controller = get_speech_controller()
        self.suggestion_manager = get_suggestion_manager()
        
        # 代理状态
        self._running = False
        self._current_task: Optional[asyncio.Task] = None
        self._last_event_time = 0
        self._speak_count = 0
        self._last_speak_time = 0
        
        # API 客户端（复用现有逻辑）
        from agent import Agent
        self._api_agent = Agent(config)
        
        # 订阅事件
        self.event_bus.subscribe(self.id, self._on_event)
    
    async def _on_event(self, event: Event):
        """事件回调"""
        self._last_event_time = time.time()
        
        # 处理打断事件
        if event.event_type == EventType.INTERRUPT and event.target_id == self.id:
            # 被打断，记录状态
            pass
        
        # 处理建议事件
        elif event.event_type == EventType.SUGGESTION and event.target_id == self.id:
            await self._handle_suggestion(event)
    
    async def _handle_suggestion(self, event: Event):
        """处理收到的建议"""
        suggestions = self.suggestion_manager.get_pending_suggestions(self.id)
        if suggestions:
            latest = suggestions[-1]
            
            # 基于性格决定是否自动忽略
            personality = {
                "cautiousness": self.personality.cautiousness,
                "empathy": self.personality.empathy,
                "abstraction": self.personality.abstraction
            }
            
            if self.suggestion_manager.should_auto_ignore(latest, personality):
                self.suggestion_manager.respond_to_suggestion(
                    latest.suggestion_id, self.id, SuggestionResponse.IGNORE
                )
                return
            
            # 否则在下一轮决策中处理
    
    async def start(self, initial_context: str = ""):
        """启动代理循环"""
        self._running = True
        self._current_task = asyncio.create_task(self._agent_loop(initial_context))
    
    async def stop(self):
        """停止代理"""
        self._running = False
        if self._current_task:
            self._current_task.cancel()
            try:
                await self._current_task
            except asyncio.CancelledError:
                pass
    
    async def _agent_loop(self, initial_context: str = ""):
        """代理主循环"""
        while self._running:
            try:
                # 1. 获取最近事件作为上下文
                recent_events = self.event_bus.get_recent_events(20)
                context = EventFormatter.format_for_context(recent_events, self.id)
                
                # 2. 检查是否有待处理的建议
                pending_suggestions = self.suggestion_manager.get_pending_suggestions(self.id)
                if pending_suggestions:
                    context = self._prepend_suggestions(context, pending_suggestions)
                
                # 3. 决策
                decision = await self._make_decision(context)
                
                # 4. 执行决策
                await self._execute_decision(decision)
                
                # 5. 动态休眠
                sleep_time = self._calculate_sleep_time()
                await asyncio.sleep(sleep_time)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                # 发布错误事件
                await self.event_bus.publish_async(create_event(
                    event_type=EventType.AGENT_ERROR,
                    source_id=self.id,
                    content=str(e)
                ))
                await asyncio.sleep(1)
    
    def _prepend_suggestions(self, context: str, suggestions: List[Suggestion]) -> str:
        """将建议添加到上下文前面"""
        lines = ["你有以下待处理的建议："]
        for s in suggestions[:3]:
            lines.append(self.suggestion_manager.format_suggestion_for_context(s))
        lines.append("")
        lines.append(context)
        return "\n".join(lines)
    
    async def _make_decision(self, context: str) -> AgentDecision:
        """决策下一步行动"""
        # 获取决策提示词
        prompt = self._get_decision_prompt()
        
        formatted = prompt.format(
            identity=self.id,
            cautiousness=self.personality.cautiousness,
            empathy=self.personality.empathy,
            abstraction=self.personality.abstraction,
            context=context[:1000]
        )
        
        # 调用 API
        response = await self._api_agent.call_api(
            [{"role": "user", "content": formatted}],
            temperature=0.9,
            max_tokens=100
        )
        
        if not response.success:
            return AgentDecision(action=AgentAction.WAIT, reason="API 调用失败")
        
        # 解析决策
        return self._parse_decision(response.content)
    
    def _get_decision_prompt(self) -> str:
        """获取决策提示词"""
        # 可以从配置中获取自定义提示词
        return DECISION_PROMPT
    
    def _parse_decision(self, content: str) -> AgentDecision:
        """解析决策结果"""
        try:
            # 提取 JSON
            start = content.find("{")
            end = content.rfind("}") + 1
            if start != -1 and end > start:
                data = json.loads(content[start:end])
                
                action_str = data.get("action", "wait").lower()
                action_map = {
                    "speak": AgentAction.SPEAK,
                    "interrupt": AgentAction.INTERRUPT,
                    "suggest": AgentAction.SUGGEST,
                    "call_tool": AgentAction.CALL_TOOL,
                    "vote": AgentAction.VOTE,
                    "call_stop": AgentAction.CALL_STOP,
                    "wait": AgentAction.WAIT,
                    "idle": AgentAction.IDLE
                }
                
                action = action_map.get(action_str, AgentAction.WAIT)
                
                return AgentDecision(
                    action=action,
                    content=data.get("content", ""),
                    target_id=data.get("target"),
                    confidence=data.get("confidence", 0.5)
                )
        except:
            pass
        
        # 尝试从文本中推断
        if "[INTERRUPT]" in content.upper():
            return AgentDecision(action=AgentAction.INTERRUPT, content=content)
        elif "[SUGGESTION]" in content.upper():
            return AgentDecision(action=AgentAction.SUGGEST, content=content)
        elif "停" in content and "提案" in content:
            return AgentDecision(action=AgentAction.CALL_STOP, content=content)
        
        return AgentDecision(action=AgentAction.SPEAK, content=content[:50])
    
    async def _execute_decision(self, decision: AgentDecision):
        """执行决策"""
        if decision.action == AgentAction.SPEAK:
            await self._do_speak(decision.content, decision.priority)
        
        elif decision.action == AgentAction.INTERRUPT:
            await self._do_interrupt(decision.target_id, decision.content)
        
        elif decision.action == AgentAction.SUGGEST:
            await self._do_suggest(decision.target_id, decision.content)
        
        elif decision.action == AgentAction.CALL_STOP:
            await self._do_call_stop(decision.content)
        
        elif decision.action == AgentAction.WAIT:
            pass  # 什么都不做
    
    async def _do_speak(self, content: str, priority: int = 0):
        """发言"""
        # 检查发言频率限制
        if self._speak_count > 0 and time.time() - self._last_speak_time < 1.0:
            return
        
        request = SpeechRequest(
            agent_id=self.id,
            content=content[:100],  # 限制长度
            priority=priority
        )
        
        success = await self.speech_controller.request_speak(request)
        
        if success:
            self._speak_count += 1
            self._last_speak_time = time.time()
            
            # 模拟发言时长后释放
            await asyncio.sleep(len(content) * 0.05)  # 约 50ms/字
            await self.speech_controller.release_speech(self.id)
    
    async def _do_interrupt(self, target_id: str, reason: str):
        """打断"""
        current_speaker = self.speech_controller.get_current_speaker()
        if current_speaker and current_speaker != self.id:
            request = SpeechRequest(
                agent_id=self.id,
                content=f"[打断] {reason[:50]}",
                priority=50,  # 打断有较高优先级
                is_correction=True
            )
            await self.speech_controller.request_speak(request)
    
    async def _do_suggest(self, target_id: str, content: str):
        """提建议"""
        # 格式化建议
        if target_id:
            formatted = f"[SUGGESTION] to {target_id}: {content}"
        else:
            formatted = f"[SUGGESTION] to all: {content}"
        
        # 发布建议事件
        await self.event_bus.publish_async(create_event(
            event_type=EventType.SUGGESTION,
            source_id=self.id,
            content=content,
            target_id=target_id
        ))
    
    async def _do_call_stop(self, proposal: str):
        """叫停并提议"""
        await self.event_bus.publish_async(create_event(
            event_type=EventType.VOTE_REQUEST,
            source_id=self.id,
            content=proposal
        ))
    
    def _calculate_sleep_time(self) -> float:
        """计算动态休眠时间"""
        # 基于活跃度调整
        recent_count = len(self.event_bus.get_events_since(time.time() - 5))
        
        if recent_count > 10:
            return 0.05  # 高活跃，快速响应
        elif recent_count > 5:
            return 0.2
        else:
            return 0.5  # 低活跃，减少空转
    
    @property
    def is_running(self) -> bool:
        return self._running
    
    @property
    def state(self) -> SpeechState:
        return self.speech_controller.get_state(self.id)


class EventAgentPool:
    """事件驱动代理池"""
    
    def __init__(self, configs: List[AgentConfig], prompts: PromptsConfig = None):
        self.configs = configs
        self.prompts = prompts
        self._agents: Dict[str, EventAgent] = {}
        
        for config in configs:
            if config.enabled:
                self._agents[config.id] = EventAgent(config, prompts)
    
    async def start_all(self, initial_context: str = ""):
        """启动所有代理"""
        tasks = [agent.start(initial_context) for agent in self._agents.values()]
        await asyncio.gather(*tasks)
    
    async def stop_all(self):
        """停止所有代理"""
        tasks = [agent.stop() for agent in self._agents.values()]
        await asyncio.gather(*tasks)
    
    def get_agent(self, agent_id: str) -> Optional[EventAgent]:
        return self._agents.get(agent_id)
    
    def get_all_agents(self) -> List[EventAgent]:
        return list(self._agents.values())
    
    def get_running_count(self) -> int:
        return sum(1 for a in self._agents.values() if a.is_running)
    
    def __len__(self):
        return len(self._agents)
