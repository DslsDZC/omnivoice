"""发言流控制器 - 管理并行发言、优先级和打断"""
import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Callable, Any
from enum import Enum
from collections import defaultdict

from event_bus import EventBus, Event, EventType, get_event_bus, create_event, USER_PRIORITY


class SpeechState(Enum):
    """发言状态"""
    IDLE = "idle"           # 空闲
    SPEAKING = "speaking"   # 正在发言
    INTERRUPTED = "interrupted"  # 被打断
    WAITING = "waiting"     # 等待中
    USER_SPEAKING = "user_speaking"  # 用户发言中（不可打断）


@dataclass
class SpeechRequest:
    """发言请求"""
    agent_id: str
    content: str
    priority: int                   # 紧迫分数 (0-100)
    timestamp: float = field(default_factory=time.time)
    target_id: Optional[str] = None # 是否针对某个代理
    is_correction: bool = False     # 是否是纠正
    is_reply: bool = False          # 是否是回复
    is_suggestion: bool = False     # 是否是建议
    is_user: bool = False           # 是否是用户发言
    max_tokens: int = 30            # 最大token数


@dataclass
class SpeakingSlot:
    """当前发言槽位"""
    agent_id: str
    content: str
    priority: int
    start_time: float
    is_active: bool = True
    interrupted_by: Optional[str] = None


@dataclass
class SpeechControllerConfig:
    """发言控制器配置"""
    interrupt_threshold: int = 15       # 打断阈值（分数差）
    min_speak_duration: float = 0.5     # 最小发言时间（秒）
    max_speak_duration: float = 5.0     # 最大发言时间（秒）
    max_concurrent_requests: int = 10   # 最大并发请求数
    history_contribution_weight: float = 2.0  # 历史贡献权重


class SpeechController:
    """发言流控制器"""
    
    def __init__(self, config: SpeechControllerConfig = None):
        self.config = config or SpeechControllerConfig()
        self.event_bus = get_event_bus()
        
        # 当前发言状态
        self._current_speaker: Optional[SpeakingSlot] = None
        self._pending_requests: List[SpeechRequest] = []
        self._agent_states: Dict[str, SpeechState] = {}
        
        # 代理贡献分（历史）
        self._contribution_scores: Dict[str, float] = defaultdict(float)
        
        # 旁侧观点（被暂存的发言）
        self._sideline_views: List[Dict] = []
        
        # 锁
        self._lock = asyncio.Lock()
        
        # 订阅事件
        self.event_bus.subscribe_to_type(EventType.INTERRUPT, self._handle_interrupt)
        self.event_bus.subscribe_to_type(EventType.USER_INTERRUPT, self._handle_user_interrupt)
    
    async def _handle_interrupt(self, event: Event):
        """处理打断事件"""
        if self._current_speaker and self._current_speaker.agent_id == event.target_id:
            self._current_speaker.is_active = False
            self._current_speaker.interrupted_by = event.source_id
            self._agent_states[event.target_id] = SpeechState.INTERRUPTED
    
    async def _handle_user_interrupt(self, event: Event):
        """处理用户插话 - 最高优先级，立即打断当前发言"""
        async with self._lock:
            if self._current_speaker and self._current_speaker.is_active:
                # 打断当前发言者
                self._current_speaker.is_active = False
                self._current_speaker.interrupted_by = "user"
                if self._current_speaker.agent_id in self._agent_states:
                    self._agent_states[self._current_speaker.agent_id] = SpeechState.INTERRUPTED
                
                # 记录旁侧观点
                self._sideline_views.append({
                    "agent_id": self._current_speaker.agent_id,
                    "content": self._current_speaker.content,
                    "interrupted": True,
                    "interrupted_by": "user"
                })
            
            # 设置用户发言状态
            self._current_speaker = SpeakingSlot(
                agent_id="user",
                content=event.content,
                priority=USER_PRIORITY,
                start_time=time.time(),
                is_active=True
            )
            self._agent_states["user"] = SpeechState.USER_SPEAKING
    
    async def user_speak(self, content: str, target_id: str = None) -> bool:
        """用户发言 - 最高优先级，总是成功"""
        request = SpeechRequest(
            agent_id="user",
            content=content,
            priority=USER_PRIORITY,
            target_id=target_id,
            is_user=True
        )
        
        # 发布用户插话事件
        event = create_event(
            event_type=EventType.USER_INTERRUPT,
            source_id="user",
            content=content,
            target_id=target_id,
            priority=USER_PRIORITY
        )
        await self.event_bus.publish_async(event)
        
        return True
    
    def calculate_priority(self, request: SpeechRequest, 
                          recent_events: List[Event]) -> int:
        """计算发言优先级分数"""
        # 用户消息总是最高优先级
        if request.is_user:
            return USER_PRIORITY
        
        score = 50  # 基础分
        
        # 是否针对上一条发言
        if request.is_reply and recent_events:
            last_speech = None
            for event in reversed(recent_events):
                if event.event_type == EventType.AGENT_UTTERANCE:
                    last_speech = event
                    break
            if last_speech:
                score += 30
        
        # 是否包含事实纠正
        if request.is_correction:
            score += 40
        
        # 是否被其他代理 @
        for event in recent_events[-10:]:
            if (event.event_type == EventType.SUGGESTION and 
                event.target_id == request.agent_id):
                score += 20
                break
        
        # 历史贡献分
        contribution = self._contribution_scores.get(request.agent_id, 0)
        score += min(20, contribution * self.config.history_contribution_weight)
        
        # 性格加权（由代理外部传入）
        score += request.priority  # agent 可以自带额外分数
        
        return min(100, max(0, score))
    
    async def request_speak(self, request: SpeechRequest) -> bool:
        """请求发言"""
        async with self._lock:
            # 获取最近事件计算优先级
            recent_events = self.event_bus.get_recent_events(20)
            priority = self.calculate_priority(request, recent_events)
            request.priority = priority
            
            # 如果当前无人发言，直接获得发言权
            if self._current_speaker is None or not self._current_speaker.is_active:
                return await self._grant_speech(request)
            
            # 检查是否可以打断
            current_priority = self._current_speaker.priority
            if priority >= current_priority + self.config.interrupt_threshold:
                return await self._interrupt_and_speak(request)
            
            # 否则加入等待队列或旁侧观点
            self._add_to_pending(request)
            return False
    
    async def _grant_speech(self, request: SpeechRequest) -> bool:
        """授予发言权"""
        self._current_speaker = SpeakingSlot(
            agent_id=request.agent_id,
            content=request.content,
            priority=request.priority,
            start_time=time.time()
        )
        self._agent_states[request.agent_id] = SpeechState.SPEAKING
        
        # 发布发言事件
        event = create_event(
            event_type=EventType.AGENT_UTTERANCE,
            source_id=request.agent_id,
            content=request.content,
            priority=request.priority,
            metadata={"max_tokens": request.max_tokens}
        )
        await self.event_bus.publish_async(event)
        
        return True
    
    async def _interrupt_and_speak(self, request: SpeechRequest) -> bool:
        """打断当前发言者并发言"""
        if self._current_speaker:
            # 发送打断信号
            interrupt_event = create_event(
                event_type=EventType.INTERRUPT,
                source_id=request.agent_id,
                content="",
                target_id=self._current_speaker.agent_id
            )
            await self.event_bus.publish_async(interrupt_event)
            
            # 记录被打断的发言
            self._sideline_views.append({
                "agent_id": self._current_speaker.agent_id,
                "content": self._current_speaker.content,
                "interrupted": True,
                "interrupted_by": request.agent_id
            })
        
        # 授予新发言权
        return await self._grant_speech(request)
    
    def _add_to_pending(self, request: SpeechRequest):
        """添加到等待队列或旁侧观点"""
        # 作为旁侧观点暂存
        self._sideline_views.append({
            "agent_id": request.agent_id,
            "content": request.content,
            "priority": request.priority,
            "interrupted": False
        })
        self._agent_states[request.agent_id] = SpeechState.WAITING
    
    async def release_speech(self, agent_id: str):
        """释放发言权"""
        async with self._lock:
            if self._current_speaker and self._current_speaker.agent_id == agent_id:
                # 更新贡献分
                self._contribution_scores[agent_id] += 1
                
                # 清除当前发言者
                self._current_speaker = None
                self._agent_states[agent_id] = SpeechState.IDLE
                
                # 尝试处理下一个等待的请求
                if self._pending_requests:
                    next_request = self._pending_requests.pop(0)
                    await self._grant_speech(next_request)
    
    def get_state(self, agent_id: str) -> SpeechState:
        """获取代理发言状态"""
        return self._agent_states.get(agent_id, SpeechState.IDLE)
    
    def get_current_speaker(self) -> Optional[str]:
        """获取当前发言者"""
        if self._current_speaker and self._current_speaker.is_active:
            return self._current_speaker.agent_id
        return None
    
    def get_sideline_views(self, count: int = 5) -> List[Dict]:
        """获取旁侧观点"""
        return self._sideline_views[-count:]
    
    def get_contribution_scores(self) -> Dict[str, float]:
        """获取所有代理的贡献分"""
        return dict(self._contribution_scores)
    
    def update_contribution(self, agent_id: str, delta: float):
        """更新代理贡献分"""
        self._contribution_scores[agent_id] += delta
    
    def reset(self):
        """重置控制器状态"""
        self._current_speaker = None
        self._pending_requests.clear()
        self._agent_states.clear()
        self._sideline_views.clear()


# 全局发言控制器
_global_controller: Optional[SpeechController] = None


def get_speech_controller(config: SpeechControllerConfig = None) -> SpeechController:
    """获取全局发言控制器"""
    global _global_controller
    if _global_controller is None:
        _global_controller = SpeechController(config)
    return _global_controller
