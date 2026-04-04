"""Token预算管理器 - 会话级预算、计数、超限处理"""
import time
import hashlib
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict
import threading


class BudgetState(Enum):
    """预算状态"""
    NORMAL = "normal"           # 正常
    WARNING = "warning"         # 警告（剩余<10%）
    EXCEEDED = "exceeded"       # 超限
    DISABLED = "disabled"       # 禁用


class TokenType(Enum):
    """Token类型"""
    INPUT = "input"             # 输入token
    OUTPUT = "output"           # 输出token
    TOOL = "tool"               # 工具调用消耗
    SYSTEM = "system"           # 系统提示词


@dataclass
class TokenUsage:
    """Token使用记录"""
    agent_id: str
    token_type: TokenType
    count: int
    timestamp: float
    round_num: int
    detail: str = ""  # 详细说明


@dataclass
class AgentBudgetStats:
    """代理预算统计"""
    agent_id: str
    total_input: int = 0
    total_output: int = 0
    total_tool: int = 0
    call_count: int = 0
    last_call_time: float = 0.0
    avg_output_per_call: float = 0.0
    
    @property
    def total(self) -> int:
        return self.total_input + self.total_output + self.total_tool


@dataclass
class SessionBudget:
    """会话预算配置"""
    total_budget: int = 200000          # 总预算（token）
    warning_threshold: float = 0.1      # 警告阈值（10%）
    max_output_per_call: int = 30       # 单次发言最大输出token
    max_input_context: int = 2000       # 最大输入上下文token
    max_rounds: int = 100               # 最大轮次
    max_agent_daily_limit: int = 10000  # 单代理每日上限


class BudgetManager:
    """预算管理器"""
    
    def __init__(self, config: Optional[SessionBudget] = None):
        self.config = config or SessionBudget()
        
        # 预算状态
        self._used_tokens: int = 0
        self._state: BudgetState = BudgetState.NORMAL
        
        # 使用记录
        self._usage_history: List[TokenUsage] = []
        self._agent_stats: Dict[str, AgentBudgetStats] = {}
        
        # 当前轮次
        self._current_round: int = 0
        
        # 代理限制
        self._agent_limits: Dict[str, int] = {}
        self._agent_daily_used: Dict[str, int] = defaultdict(int)
        
        # 锁
        self._lock = threading.RLock()
        
        # 回调
        self._on_warning_callbacks: List = []
        self._on_exceeded_callbacks: List = []
    
    # ==================== 预算查询 ====================
    
    @property
    def remaining(self) -> int:
        """剩余预算"""
        return max(0, self.config.total_budget - self._used_tokens)
    
    @property
    def used(self) -> int:
        """已用预算"""
        return self._used_tokens
    
    @property
    def state(self) -> BudgetState:
        """预算状态"""
        return self._state
    
    @property
    def usage_percentage(self) -> float:
        """使用百分比"""
        return self._used_tokens / self.config.total_budget if self.config.total_budget > 0 else 0
    
    def can_spend(self, amount: int) -> bool:
        """检查是否可以消耗指定数量"""
        return self.remaining >= amount and self._state != BudgetState.DISABLED
    
    def is_agent_limited(self, agent_id: str) -> bool:
        """检查代理是否达到限制"""
        limit = self._agent_limits.get(agent_id, self.config.max_agent_daily_limit)
        used = self._agent_daily_used.get(agent_id, 0)
        return used >= limit
    
    # ==================== Token消耗 ====================
    
    def record_usage(self, agent_id: str, input_tokens: int, output_tokens: int,
                     tool_tokens: int = 0, detail: str = "") -> bool:
        """记录Token消耗
        
        Returns:
            是否成功（预算不足时返回False）
        """
        total = input_tokens + output_tokens + tool_tokens
        
        with self._lock:
            # 检查预算
            if not self.can_spend(total):
                return False
            
            # 检查代理限制
            if self.is_agent_limited(agent_id):
                return False
            
            # 记录使用
            self._used_tokens += total
            
            # 记录详情
            timestamp = time.time()
            
            if input_tokens > 0:
                self._usage_history.append(TokenUsage(
                    agent_id=agent_id,
                    token_type=TokenType.INPUT,
                    count=input_tokens,
                    timestamp=timestamp,
                    round_num=self._current_round,
                    detail=detail
                ))
            
            if output_tokens > 0:
                self._usage_history.append(TokenUsage(
                    agent_id=agent_id,
                    token_type=TokenType.OUTPUT,
                    count=output_tokens,
                    timestamp=timestamp,
                    round_num=self._current_round,
                    detail=detail
                ))
            
            if tool_tokens > 0:
                self._usage_history.append(TokenUsage(
                    agent_id=agent_id,
                    token_type=TokenType.TOOL,
                    count=tool_tokens,
                    timestamp=timestamp,
                    round_num=self._current_round,
                    detail=detail
                ))
            
            # 更新代理统计
            if agent_id not in self._agent_stats:
                self._agent_stats[agent_id] = AgentBudgetStats(agent_id=agent_id)
            
            stats = self._agent_stats[agent_id]
            stats.total_input += input_tokens
            stats.total_output += output_tokens
            stats.total_tool += tool_tokens
            stats.call_count += 1
            stats.last_call_time = timestamp
            stats.avg_output_per_call = stats.total_output / stats.call_count
            
            # 更新每日使用
            self._agent_daily_used[agent_id] += total
            
            # 检查状态
            self._check_state()
            
            return True
    
    def _check_state(self):
        """检查预算状态"""
        remaining_ratio = self.remaining / self.config.total_budget
        
        if self._used_tokens >= self.config.total_budget:
            self._state = BudgetState.EXCEEDED
            self._trigger_exceeded()
        elif remaining_ratio <= self.config.warning_threshold:
            self._state = BudgetState.WARNING
            self._trigger_warning()
        else:
            self._state = BudgetState.NORMAL
    
    # ==================== 限制设置 ====================
    
    def set_agent_limit(self, agent_id: str, limit: int):
        """设置代理Token限制"""
        self._agent_limits[agent_id] = limit
    
    def set_budget(self, total: int):
        """设置总预算"""
        self.config.total_budget = total
        self._check_state()
    
    def set_max_output(self, max_output: int):
        """设置单次最大输出"""
        self.config.max_output_per_call = max_output
    
    # ==================== 轮次管理 ====================
    
    def start_round(self):
        """开始新轮次"""
        with self._lock:
            self._current_round += 1
    
    @property
    def current_round(self) -> int:
        return self._current_round
    
    # ==================== 统计报告 ====================
    
    def get_agent_stats(self, agent_id: str) -> Optional[AgentBudgetStats]:
        """获取代理统计"""
        return self._agent_stats.get(agent_id)
    
    def get_all_stats(self) -> Dict[str, AgentBudgetStats]:
        """获取所有代理统计"""
        return dict(self._agent_stats)
    
    def get_usage_by_round(self, round_num: int) -> List[TokenUsage]:
        """获取指定轮次的使用记录"""
        return [u for u in self._usage_history if u.round_num == round_num]
    
    def get_top_consumers(self, limit: int = 5) -> List[AgentBudgetStats]:
        """获取消耗最多的代理"""
        sorted_stats = sorted(
            self._agent_stats.values(),
            key=lambda s: s.total,
            reverse=True
        )
        return sorted_stats[:limit]
    
    def get_report(self) -> Dict:
        """获取详细报告"""
        return {
            "budget": {
                "total": self.config.total_budget,
                "used": self._used_tokens,
                "remaining": self.remaining,
                "usage_percentage": f"{self.usage_percentage:.1%}",
                "state": self._state.value
            },
            "rounds": {
                "current": self._current_round,
                "max": self.config.max_rounds
            },
            "agents": {
                "total_count": len(self._agent_stats),
                "top_consumers": [
                    {
                        "agent_id": s.agent_id,
                        "total": s.total,
                        "input": s.total_input,
                        "output": s.total_output,
                        "calls": s.call_count
                    }
                    for s in self.get_top_consumers()
                ]
            },
            "config": {
                "max_output_per_call": self.config.max_output_per_call,
                "max_input_context": self.config.max_input_context
            }
        }
    
    # ==================== 回调注册 ====================
    
    def on_warning(self, callback):
        """注册警告回调"""
        self._on_warning_callbacks.append(callback)
    
    def on_exceeded(self, callback):
        """注册超限回调"""
        self._on_exceeded_callbacks.append(callback)
    
    def _trigger_warning(self):
        """触发警告回调"""
        for callback in self._on_warning_callbacks:
            try:
                callback(self.get_report())
            except Exception:
                pass
    
    def _trigger_exceeded(self):
        """触发超限回调"""
        for callback in self._on_exceeded_callbacks:
            try:
                callback(self.get_report())
            except Exception:
                pass
    
    # ==================== 重置 ====================
    
    def reset(self):
        """重置预算管理器"""
        with self._lock:
            self._used_tokens = 0
            self._state = BudgetState.NORMAL
            self._usage_history.clear()
            self._agent_stats.clear()
            self._agent_daily_used.clear()
            self._current_round = 0
    
    def reset_daily(self):
        """重置每日限制"""
        self._agent_daily_used.clear()


class TokenCounter:
    """Token计数器 - 估算token数量"""
    
    # 平均每个字符的token数（粗略估计）
    CHARS_PER_TOKEN_EN = 4      # 英文约4字符/token
    CHARS_PER_TOKEN_ZH = 1.5    # 中文约1.5字符/token
    
    @classmethod
    def estimate(cls, text: str) -> int:
        """估算文本token数量"""
        if not text:
            return 0
        
        # 简单估算：区分中英文
        chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        other_chars = len(text) - chinese_chars
        
        tokens = int(chinese_chars / cls.CHARS_PER_TOKEN_ZH) + \
                 int(other_chars / cls.CHARS_PER_TOKEN_EN)
        
        return max(1, tokens)
    
    @classmethod
    def estimate_messages(cls, messages: List[Dict]) -> int:
        """估算消息列表token数量"""
        total = 0
        for msg in messages:
            # 角色开销
            total += 4  # 每条消息约4 token开销
            
            content = msg.get("content", "")
            total += cls.estimate(content)
            
            # 工具调用
            if "tool_calls" in msg:
                for tc in msg["tool_calls"]:
                    total += cls.estimate(str(tc))
        
        return total
    
    @classmethod
    def truncate_to_budget(cls, text: str, max_tokens: int) -> str:
        """截断文本到指定token数"""
        if not text:
            return ""
        
        estimated = cls.estimate(text)
        if estimated <= max_tokens:
            return text
        
        # 粗略截断
        ratio = max_tokens / estimated
        target_chars = int(len(text) * ratio * 0.9)  # 留一点余量
        
        return text[:target_chars] + "..."


class ContextPruner:
    """上下文剪裁器"""
    
    def __init__(self, max_tokens: int = 2000):
        self.max_tokens = max_tokens
        self.counter = TokenCounter()
    
    def prune_messages(self, messages: List[Dict], 
                       keep_first: int = 1) -> List[Dict]:
        """剪裁消息列表
        
        Args:
            messages: 消息列表
            keep_first: 保留前N条消息（通常是系统提示词）
        """
        if not messages:
            return []
        
        # 保留前面的系统消息
        kept = messages[:keep_first]
        remaining = messages[keep_first:]
        
        # 从后向前保留，直到达到预算
        result = list(kept)
        current_tokens = self.counter.estimate_messages(result)
        
        for msg in reversed(remaining):
            msg_tokens = self.counter.estimate_messages([msg])
            if current_tokens + msg_tokens <= self.max_tokens:
                result.append(msg)
                current_tokens += msg_tokens
            else:
                break
        
        # 重新排序（保持时间顺序）
        if len(result) > keep_first:
            system_part = result[:keep_first]
            history_part = result[keep_first:]
            # history_part 已经是反向添加的，需要反转
            history_part = list(reversed(history_part))
            result = system_part + history_part
        
        return result
    
    def prune_tool_result(self, result: Any, max_chars: int = 200) -> str:
        """剪裁工具结果"""
        result_str = str(result)
        if len(result_str) <= max_chars:
            return result_str
        return result_str[:max_chars] + "...[结果过长已截断]"
    
    def generate_summary(self, messages: List[Dict], 
                         summary_length: int = 500) -> str:
        """生成消息摘要"""
        if not messages:
            return ""
        
        # 提取关键内容
        key_points = []
        for msg in messages:
            content = msg.get("content", "")
            if len(content) > 10:
                # 提取前50字符作为要点
                point = content[:50].replace("\n", " ")
                key_points.append(f"- {point}...")
        
        summary = "\n".join(key_points[-10:])  # 最近10条
        
        if len(summary) > summary_length:
            summary = summary[:summary_length] + "..."
        
        return summary


# 全局预算管理器
_global_budget_manager: Optional[BudgetManager] = None


def get_budget_manager(config: Optional[SessionBudget] = None) -> BudgetManager:
    """获取全局预算管理器"""
    global _global_budget_manager
    if _global_budget_manager is None:
        _global_budget_manager = BudgetManager(config)
    return _global_budget_manager


def reset_budget_manager():
    """重置全局预算管理器"""
    global _global_budget_manager
    if _global_budget_manager:
        _global_budget_manager.reset()
