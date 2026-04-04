"""事件总线 - 支持发布/订阅的实时事件流（带优先级队列）"""
import asyncio
import time
import uuid
import heapq
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable, Any, Set, Tuple
from enum import Enum
from collections import deque
import threading


class EventType(Enum):
    """事件类型"""
    USER_INPUT = "user_input"           # 用户输入（初始问题）
    USER_INTERRUPT = "user_interrupt"   # 用户插话（最高优先级）
    USER_VOTE = "user_vote"             # 用户投票
    USER_END = "user_end"               # 用户结束讨论
    USER_SUGGESTION = "user_suggestion" # 用户建议
    USER_COMMAND = "user_command"       # 用户指令
    AGENT_UTTERANCE = "agent_utterance" # 代理发言
    INTERRUPT = "interrupt"             # 打断信号
    INTERRUPTED = "interrupted"         # 被打断通知
    SUGGESTION = "suggestion"           # 建议
    TOOL_RESULT = "tool_result"         # 工具结果
    VOTE_REQUEST = "vote_request"       # 投票请求（叫停）
    VOTE_CAST = "vote_cast"             # 投票
    CONSENSUS_REACHED = "consensus_reached"  # 达成共识
    AGENT_ERROR = "agent_error"         # 代理错误
    SYSTEM = "system"                   # 系统消息
    # 记忆相关事件
    MEMORY_SAVE = "memory_save"         # 保存记忆请求
    MEMORY_INJECT = "memory_inject"     # 记忆注入通知
    MEMORY_UPDATE = "memory_update"     # 记忆更新通知
    MEMORY_DELETE = "memory_delete"     # 记忆删除通知
    # 预算与成本事件
    BUDGET_WARNING = "budget_warning"   # 预算警告
    BUDGET_EXCEEDED = "budget_exceeded" # 预算超限
    COST_REPORT = "cost_report"         # 成本报告
    AGENT_SLEEP = "agent_sleep"         # 代理休眠
    AGENT_WAKE = "agent_wake"           # 代理唤醒
    API_TIMEOUT = "api_timeout"         # API超时
    RATE_LIMIT_HIT = "rate_limit_hit"   # 达到速率限制


# 优先级常量
USER_PRIORITY = 100          # 用户插话（最高）
USER_FORCE_PRIORITY = 99     # 用户强制打断
USER_SUGGESTION_PRIORITY = 90  # 用户建议
USER_COMMAND_PRIORITY = 80   # 用户指令
AGENT_INTERRUPT_PRIORITY = 70  # 代理打断
AGENT_SPEECH_PRIORITY = 50   # 代理普通发言
AGENT_SUGGESTION_PRIORITY = 30  # 代理建议
SYSTEM_PRIORITY = 20         # 系统消息


@dataclass
class Event:
    """事件基类"""
    event_type: EventType
    source_id: str                      # 发送者ID
    content: Any                        # 事件内容
    timestamp: float = field(default_factory=time.time)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    target_id: Optional[str] = None     # 目标代理ID（用于建议/打断）
    priority: int = 0                   # 优先级
    metadata: Dict = field(default_factory=dict)
    
    def to_dict(self) -> Dict:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "source_id": self.source_id,
            "content": self.content,
            "timestamp": self.timestamp,
            "target_id": self.target_id,
            "priority": self.priority,
            "metadata": self.metadata
        }


class EventBus:
    """事件总线 - 单例模式（支持优先级队列）"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self, max_events: int = 200):
        if hasattr(self, '_initialized') and self._initialized:
            return
        
        self._initialized = True
        self.max_events = max_events
        
        # 历史事件队列（按时间顺序）
        self._event_queue: deque = deque(maxlen=max_events)
        
        # 优先级队列（用于实时事件分发）
        # 使用堆实现，格式: (-priority, timestamp, event)
        self._priority_queue: List[Tuple[int, float, Event]] = []
        self._queue_lock = threading.Lock()
        
        # 订阅者
        self._subscribers: Dict[str, Set[Callable]] = {}
        self._type_subscribers: Dict[EventType, Set[Callable]] = {}
        self._async_queue: asyncio.Queue = None
        self._lock = asyncio.Lock()
        
        # 事件类型默认优先级
        self._type_priorities: Dict[EventType, int] = {
            EventType.USER_INTERRUPT: USER_PRIORITY,
            EventType.USER_COMMAND: USER_COMMAND_PRIORITY,
            EventType.USER_SUGGESTION: USER_SUGGESTION_PRIORITY,
            EventType.USER_INPUT: USER_PRIORITY,
            EventType.USER_VOTE: USER_PRIORITY,
            EventType.USER_END: USER_PRIORITY,
            EventType.INTERRUPT: AGENT_INTERRUPT_PRIORITY,
            EventType.INTERRUPTED: AGENT_INTERRUPT_PRIORITY,
            EventType.AGENT_UTTERANCE: AGENT_SPEECH_PRIORITY,
            EventType.SUGGESTION: AGENT_SUGGESTION_PRIORITY,
            EventType.SYSTEM: SYSTEM_PRIORITY,
        }
        
    def get_default_priority(self, event_type: EventType) -> int:
        """获取事件类型的默认优先级"""
        return self._type_priorities.get(event_type, AGENT_SPEECH_PRIORITY)
        
    def subscribe(self, subscriber_id: str, callback: Callable):
        """订阅所有事件"""
        if subscriber_id not in self._subscribers:
            self._subscribers[subscriber_id] = set()
        self._subscribers[subscriber_id].add(callback)
    
    def subscribe_to_type(self, event_type: EventType, callback: Callable):
        """订阅特定类型事件"""
        if event_type not in self._type_subscribers:
            self._type_subscribers[event_type] = set()
        self._type_subscribers[event_type].add(callback)
    
    def unsubscribe(self, subscriber_id: str):
        """取消订阅"""
        self._subscribers.pop(subscriber_id, None)
    
    def publish(self, event: Event):
        """发布事件（同步，支持优先级）"""
        # 如果事件没有设置优先级，使用默认值
        if event.priority == 0:
            event.priority = self.get_default_priority(event.event_type)
        
        # 添加到历史队列
        self._event_queue.append(event)
        
        # 添加到优先级队列
        with self._queue_lock:
            heapq.heappush(self._priority_queue, (-event.priority, event.timestamp, event))
        
        # 通知订阅者
        self._notify_subscribers(event)
    
    def _notify_subscribers(self, event: Event):
        """通知订阅者"""
        # 通知全局订阅者
        for callback_set in self._subscribers.values():
            for callback in callback_set:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        asyncio.create_task(callback(event))
                    else:
                        callback(event)
                except Exception:
                    pass
        
        # 通知类型订阅者
        if event.event_type in self._type_subscribers:
            for callback in self._type_subscribers[event.event_type]:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        asyncio.create_task(callback(event))
                    else:
                        callback(event)
                except Exception:
                    pass
    
    def publish_high_priority(self, event: Event):
        """发布高优先级事件（用户插话等）"""
        event.priority = USER_PRIORITY
        self.publish(event)
    
    def get_next_event(self) -> Optional[Event]:
        """从优先级队列获取下一个事件"""
        with self._queue_lock:
            if self._priority_queue:
                _, _, event = heapq.heappop(self._priority_queue)
                return event
        return None
    
    def peek_next_event(self) -> Optional[Event]:
        """查看下一个事件但不移除"""
        with self._queue_lock:
            if self._priority_queue:
                return self._priority_queue[0][2]
        return None
    
    def get_queue_size(self) -> int:
        """获取优先级队列大小"""
        with self._queue_lock:
            return len(self._priority_queue)
    
    async def publish_async(self, event: Event):
        """发布事件（异步，支持优先级）"""
        # 如果事件没有设置优先级，使用默认值
        if event.priority == 0:
            event.priority = self.get_default_priority(event.event_type)
        
        async with self._lock:
            self._event_queue.append(event)
            with self._queue_lock:
                heapq.heappush(self._priority_queue, (-event.priority, event.timestamp, event))
        
        # 通知订阅者
        for callback_set in self._subscribers.values():
            for callback in callback_set:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(event)
                    else:
                        callback(event)
                except Exception:
                    pass
        
        # 通知类型订阅者
        if event.event_type in self._type_subscribers:
            for callback in self._type_subscribers[event.event_type]:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(event)
                    else:
                        callback(event)
                except Exception:
                    pass
    
    def get_recent_events(self, count: int = 20) -> List[Event]:
        """获取最近N条事件"""
        events = list(self._event_queue)
        return events[-count:] if len(events) > count else events
    
    def get_events_by_type(self, event_type: EventType, count: int = 20) -> List[Event]:
        """获取特定类型的最近事件"""
        events = [e for e in self._event_queue if e.event_type == event_type]
        return events[-count:] if len(events) > count else events
    
    def get_events_since(self, timestamp: float) -> List[Event]:
        """获取某个时间点之后的所有事件"""
        return [e for e in self._event_queue if e.timestamp > timestamp]
    
    def clear(self):
        """清空事件队列"""
        self._event_queue.clear()
    
    def get_event_count(self) -> int:
        """获取事件总数"""
        return len(self._event_queue)


class EventFormatter:
    """事件格式化器"""
    
    @staticmethod
    def format_for_context(events: List[Event], agent_id: str = None) -> str:
        """将事件列表格式化为上下文字符串"""
        lines = []
        for event in events:
            if event.event_type == EventType.USER_INPUT:
                lines.append(f"[用户]: {event.content}")
            elif event.event_type == EventType.USER_INTERRUPT:
                target = f" → {event.target_id}" if event.target_id else ""
                lines.append(f"[用户插话{target}]: {event.content}")
            elif event.event_type == EventType.USER_VOTE:
                lines.append(f"[用户投票]: {event.content}")
            elif event.event_type == EventType.USER_END:
                lines.append(f"[用户]: 结束讨论 - {event.content}")
            elif event.event_type == EventType.AGENT_UTTERANCE:
                marker = ""
                if event.metadata.get("interrupted"):
                    marker = " (被打断)"
                elif event.metadata.get("interrupting"):
                    marker = " (打断)"
                lines.append(f"[{event.source_id}]{marker}: {event.content}")
            elif event.event_type == EventType.SUGGESTION:
                target = event.target_id or "所有人"
                lines.append(f"[{event.source_id}→{target} 建议]: {event.content}")
            elif event.event_type == EventType.INTERRUPT:
                lines.append(f"[系统]: {event.source_id} 打断了 {event.target_id}")
            elif event.event_type == EventType.TOOL_RESULT:
                lines.append(f"[工具 {event.source_id}]: {event.content[:100]}...")
            elif event.event_type == EventType.VOTE_REQUEST:
                lines.append(f"[{event.source_id} 叫停]: {event.content}")
            elif event.event_type == EventType.VOTE_CAST:
                vote = event.metadata.get("vote", "?")
                lines.append(f"[{event.source_id} 投票]: {vote}")
            elif event.event_type == EventType.SYSTEM:
                lines.append(f"[系统]: {event.content}")
        
        return "\n".join(lines)
    
    @staticmethod
    def format_for_display(event: Event) -> str:
        """格式化单个事件用于显示"""
        if event.event_type == EventType.USER_INTERRUPT:
            return f"[用户插话]: {event.content}"
        elif event.event_type == EventType.USER_VOTE:
            return f"[用户投票]: {event.content}"
        elif event.event_type == EventType.USER_END:
            return f"[用户结束]: {event.content}"
        elif event.event_type == EventType.AGENT_UTTERANCE:
            prefix = "[发言]"
            if event.metadata.get("interrupted"):
                prefix = "[被打断]"
            elif event.metadata.get("interrupting"):
                prefix = "[打断]"
            return f"{prefix} [{event.source_id}]: {event.content}"
        elif event.event_type == EventType.SUGGESTION:
            return f"[建议] [{event.source_id} -> {event.target_id or '所有人'}]: {event.content}"
        elif event.event_type == EventType.INTERRUPT:
            return f"[打断] {event.source_id} 打断了 {event.target_id}"
        elif event.event_type == EventType.VOTE_REQUEST:
            return f"[叫停] [{event.source_id}]: {event.content[:50]}..."
        elif event.event_type == EventType.VOTE_CAST:
            return f"[投票] [{event.source_id}]: {event.metadata.get('vote', '?')}"
        elif event.event_type == EventType.SYSTEM:
            return f"[系统] {event.content}"
        else:
            return f"[{event.event_type.value}] {event.content}"


# 全局事件总线实例
_global_bus: Optional[EventBus] = None


def get_event_bus() -> EventBus:
    """获取全局事件总线"""
    global _global_bus
    if _global_bus is None:
        _global_bus = EventBus()
    return _global_bus


def create_event(event_type: EventType, source_id: str, content: Any,
                 target_id: str = None, priority: int = 0,
                 metadata: Dict = None) -> Event:
    """创建事件的便捷函数"""
    return Event(
        event_type=event_type,
        source_id=source_id,
        content=content,
        target_id=target_id,
        priority=priority,
        metadata=metadata or {}
    )
