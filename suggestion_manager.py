"""建议管理器 - 处理代理之间的建议"""
import asyncio
import time
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Callable
from enum import Enum
from collections import defaultdict

from event_bus import EventBus, Event, EventType, get_event_bus, create_event


class SuggestionType(Enum):
    """建议类型"""
    CORRECTION = "correction"       # 纠正
    IMPROVEMENT = "improvement"     # 改进建议
    QUESTION = "question"           # 质疑/提问
    SUPPORT = "support"             # 支持/赞同
    ALTERNATIVE = "alternative"     # 替代方案
    GLOBAL = "global"               # 全局建议


class SuggestionResponse(Enum):
    """建议响应"""
    ACCEPT = "accept"       # 采纳
    REJECT = "reject"       # 拒绝
    IGNORE = "ignore"       # 忽略
    PENDING = "pending"     # 待处理


@dataclass
class Suggestion:
    """建议"""
    suggestion_id: str
    source_agent: str
    target_agent: Optional[str]      # None 表示全局建议
    content: str
    suggestion_type: SuggestionType
    timestamp: float = field(default_factory=time.time)
    response: SuggestionResponse = SuggestionResponse.PENDING
    response_content: Optional[str] = None
    priority: int = 0


@dataclass
class SuggestionManagerConfig:
    """建议管理器配置"""
    max_pending_suggestions: int = 50     # 最大待处理建议数
    suggestion_timeout_sec: float = 30.0  # 建议超时时间
    auto_ignore_threshold: int = 3        # 连续忽略阈值（降低贡献分）


class SuggestionManager:
    """建议管理器"""
    
    def __init__(self, config: SuggestionManagerConfig = None):
        self.config = config or SuggestionManagerConfig()
        self.event_bus = get_event_bus()
        
        # 待处理建议：agent_id -> list of suggestions
        self._pending: Dict[str, List[Suggestion]] = defaultdict(list)
        
        # 全局建议
        self._global_suggestions: List[Suggestion] = []
        
        # 所有建议历史
        self._history: List[Suggestion] = []
        
        # 代理忽略计数
        self._ignore_count: Dict[str, int] = defaultdict(int)
        
        # 建议ID计数
        self._id_counter = 0
        
        # 订阅建议事件
        self.event_bus.subscribe_to_type(EventType.SUGGESTION, self._handle_suggestion_event)
    
    def _generate_id(self) -> str:
        """生成建议ID"""
        self._id_counter += 1
        return f"sug_{self._id_counter:04d}"
    
    def parse_suggestion(self, content: str, source_agent: str) -> Optional[Suggestion]:
        """从内容中解析建议
        
        支持格式：
        - [SUGGESTION] to agent_B: 具体建议内容
        - [SUGGESTION] to all: 全局建议
        - @agent_B: 建议内容
        """
        # 格式1: [SUGGESTION] to ...
        pattern1 = r'\[SUGGESTION\]\s+to\s+(\w+):\s*(.+)'
        match = re.match(pattern1, content, re.IGNORECASE | re.DOTALL)
        if match:
            target = match.group(1)
            suggestion_content = match.group(2).strip()
            target_agent = None if target.lower() == "all" else target
            
            return self._create_suggestion(
                source_agent=source_agent,
                target_agent=target_agent,
                content=suggestion_content
            )
        
        # 格式2: @agent_id: 内容
        pattern2 = r'@(\w+):\s*(.+)'
        match = re.match(pattern2, content)
        if match:
            target = match.group(1)
            suggestion_content = match.group(2).strip()
            
            return self._create_suggestion(
                source_agent=source_agent,
                target_agent=target,
                content=suggestion_content
            )
        
        return None
    
    def _create_suggestion(self, source_agent: str, target_agent: Optional[str],
                          content: str, suggestion_type: SuggestionType = None) -> Suggestion:
        """创建建议"""
        # 自动判断建议类型
        if suggestion_type is None:
            suggestion_type = self._detect_type(content)
        
        return Suggestion(
            suggestion_id=self._generate_id(),
            source_agent=source_agent,
            target_agent=target_agent,
            content=content,
            suggestion_type=suggestion_type
        )
    
    def _detect_type(self, content: str) -> SuggestionType:
        """检测建议类型"""
        content_lower = content.lower()
        
        if any(kw in content_lower for kw in ["错误", "不对", "纠正", "应该是"]):
            return SuggestionType.CORRECTION
        elif any(kw in content_lower for kw in ["建议", "可以", "尝试", "或许"]):
            return SuggestionType.IMPROVEMENT
        elif any(kw in content_lower for kw in ["为什么", "如何", "什么", "?", "？"]):
            return SuggestionType.QUESTION
        elif any(kw in content_lower for kw in ["同意", "支持", "对", "正确"]):
            return SuggestionType.SUPPORT
        elif any(kw in content_lower for kw in ["或者", "替代", "另一种", "不如"]):
            return SuggestionType.ALTERNATIVE
        else:
            return SuggestionType.GLOBAL
    
    async def _handle_suggestion_event(self, event: Event):
        """处理建议事件"""
        suggestion = self.parse_suggestion(event.content, event.source_id)
        if suggestion:
            await self.add_suggestion(suggestion)
    
    async def add_suggestion(self, suggestion: Suggestion):
        """添加建议"""
        self._history.append(suggestion)
        
        if suggestion.target_agent:
            # 针对特定代理的建议
            self._pending[suggestion.target_agent].append(suggestion)
            # 限制队列长度
            if len(self._pending[suggestion.target_agent]) > self.config.max_pending_suggestions:
                self._pending[suggestion.target_agent].pop(0)
        else:
            # 全局建议
            self._global_suggestions.append(suggestion)
        
        # 发布到事件总线（确保所有代理收到）
        event = create_event(
            event_type=EventType.SUGGESTION,
            source_id=suggestion.source_agent,
            content=suggestion.content,
            target_id=suggestion.target_agent,
            metadata={"suggestion_id": suggestion.suggestion_id}
        )
        await self.event_bus.publish_async(event)
    
    def get_pending_suggestions(self, agent_id: str) -> List[Suggestion]:
        """获取代理的待处理建议"""
        return self._pending.get(agent_id, [])
    
    def get_global_suggestions(self, count: int = 10) -> List[Suggestion]:
        """获取全局建议"""
        return self._global_suggestions[-count:]
    
    def respond_to_suggestion(self, suggestion_id: str, agent_id: str,
                             response: SuggestionResponse, content: str = None):
        """响应建议"""
        # 查找建议
        for suggestion in self._pending.get(agent_id, []):
            if suggestion.suggestion_id == suggestion_id:
                suggestion.response = response
                suggestion.response_content = content
                
                # 从待处理移除
                self._pending[agent_id].remove(suggestion)
                
                # 更新忽略计数
                if response == SuggestionResponse.IGNORE:
                    self._ignore_count[agent_id] += 1
                else:
                    self._ignore_count[agent_id] = 0
                
                return True
        
        return False
    
    def get_ignore_count(self, agent_id: str) -> int:
        """获取代理的连续忽略次数"""
        return self._ignore_count.get(agent_id, 0)
    
    def should_auto_ignore(self, suggestion: Suggestion, agent_personality: Dict) -> bool:
        """判断是否应该自动忽略建议（基于性格）
        
        agent_personality: {cautiousness: 0-10, empathy: 0-10, abstraction: 0-10}
        """
        # 高谨慎代理可能忽略冒险建议
        if suggestion.suggestion_type == SuggestionType.ALTERNATIVE:
            if agent_personality.get("cautiousness", 5) >= 8:
                return True
        
        # 低共情代理可能忽略支持性建议
        if suggestion.suggestion_type == SuggestionType.SUPPORT:
            if agent_personality.get("empathy", 5) <= 2:
                return True
        
        return False
    
    def format_suggestion_for_context(self, suggestion: Suggestion) -> str:
        """格式化建议用于上下文"""
        target = suggestion.target_agent or "所有人"
        type_str = {
            SuggestionType.CORRECTION: "纠正",
            SuggestionType.IMPROVEMENT: "建议",
            SuggestionType.QUESTION: "提问",
            SuggestionType.SUPPORT: "支持",
            SuggestionType.ALTERNATIVE: "替代方案",
            SuggestionType.GLOBAL: "建议"
        }.get(suggestion.suggestion_type, "建议")
        
        return f"[{suggestion.source_agent}→{target} {type_str}]: {suggestion.content}"
    
    def get_statistics(self) -> Dict:
        """获取建议统计"""
        total = len(self._history)
        by_type = defaultdict(int)
        by_response = defaultdict(int)
        
        for s in self._history:
            by_type[s.suggestion_type.value] += 1
            by_response[s.response.value] += 1
        
        return {
            "total": total,
            "by_type": dict(by_type),
            "by_response": dict(by_response),
            "pending_count": sum(len(v) for v in self._pending.values())
        }
    
    def reset(self):
        """重置管理器"""
        self._pending.clear()
        self._global_suggestions.clear()
        self._history.clear()
        self._ignore_count.clear()


# 全局建议管理器
_global_manager: Optional[SuggestionManager] = None


def get_suggestion_manager(config: SuggestionManagerConfig = None) -> SuggestionManager:
    """获取全局建议管理器"""
    global _global_manager
    if _global_manager is None:
        _global_manager = SuggestionManager(config)
    return _global_manager
