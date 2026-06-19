"""CCB 风格统一状态管理 — 每代理/每步骤的运行状态"""
import time
from typing import Optional, Dict, Any
from dataclasses import dataclass, field


@dataclass
class RunState:
    """单个运行单元的状态（一个 agent 或一个 step）"""

    # 压缩追踪
    compact_count: int = 0
    last_compact_at: float = 0.0
    total_compacted_messages: int = 0

    # 重试追踪
    retry_count: int = 0
    last_retry_at: float = 0.0
    consecutive_failures: int = 0
    max_retries: int = 3
    backoff_base: float = 1.0

    # 降级追踪
    downgraded: bool = False           # 是否已降级（model/max_tokens）
    downgrade_level: int = 0           # 降级层次 0=原始 1=减半max_tokens 2=降模型
    max_output_recovery: int = 0       # 恢复尝试次数

    # token 预算
    tokens_input: int = 0
    tokens_output: int = 0
    token_budget_remaining: int = 0    # 跨压缩边界累积

    # 统计
    call_count: int = 0
    tool_call_count: int = 0
    tool_failures: int = 0
    started_at: float = 0.0

    def should_retry(self) -> tuple:
        """检查是否应该重试，返回 (是否重试, 等待秒数)"""
        if self.retry_count >= self.max_retries:
            return False, 0.0
        backoff = self.backoff_base * (2.0 ** self.retry_count)
        self.retry_count += 1
        self.last_retry_at = time.time()
        self.consecutive_failures += 1
        return True, backoff

    def retry_success(self):
        """重试成功，重置计数"""
        self.retry_count = 0
        self.consecutive_failures = 0

    def should_downgrade(self) -> bool:
        """连续失败超过上限，需要降级"""
        return self.consecutive_failures > self.max_retries and not self.downgraded

    def apply_downgrade(self) -> Dict:
        """应用降级策略，返回降级后的配置变更"""
        self.downgrade_level += 1
        self.downgraded = True
        self.consecutive_failures = 0
        self.retry_count = 0
        changes = {}
        if self.downgrade_level == 1:
            changes["max_tokens"] = 2048  # 减半
        elif self.downgrade_level >= 2:
            changes["temperature"] = 0.1  # 更低温度更确定性
        return changes

    def mark_compact(self, count: int, saved: int):
        """记录一次压缩"""
        self.compact_count += 1
        self.last_compact_at = time.time()
        self.total_compacted_messages += count
        self.token_budget_remaining += saved

    def start(self):
        """开始计时"""
        self.started_at = time.time()

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at if self.started_at else 0.0

    def to_dict(self) -> Dict:
        return {
            "compact_count": self.compact_count,
            "retry_count": self.retry_count,
            "consecutive_failures": self.consecutive_failures,
            "downgraded": self.downgraded,
            "call_count": self.call_count,
            "tool_call_count": self.tool_call_count,
            "tool_failures": self.tool_failures,
            "elapsed": f"{self.elapsed:.1f}s",
        }
