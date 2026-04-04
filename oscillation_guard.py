"""防震荡管理器 - 自动模式切换的震荡避免方案"""
import time
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Callable, Any
from enum import Enum
from collections import deque
import logging

logger = logging.getLogger(__name__)


# 默认信号关键词（当配置未提供时使用）
DEFAULT_HEAVY_KEYWORDS = [
    "立即停止", "紧急停止", "紧急会议", "严重问题", "重大问题", "重大分歧",
    "必须讨论", "无法继续", "critical", "urgent", "emergency",
    "stop now", "严重错误", "致命问题", "崩溃"
]
DEFAULT_MEDIUM_KEYWORDS = [
    "需要讨论", "不确定", "有问题", "建议开会",
    "需要确认", "不太清楚", "需要协作", "discuss",
    "uncertain", "not sure", "need help", "confused",
    "分歧", "争议", "无法判断", "不确定如何"
]
DEFAULT_LIGHT_KEYWORDS = [
    "有点不确定", "可能需要", "稍微有点", "minor",
    "slight", "possibly", "maybe", "perhaps",
    "或者", "可能", "大概"
]

# 默认消息模板
DEFAULT_MESSAGES = {
    "min_stay_not_met": "停留时间不足，还需等待 {remaining:.1f} 秒",
    "switching_state": "正在切换中，忽略新的切换请求",
    "already_in_mode": "已在目标模式中",
    "locked_mode": "模式已锁定为 {mode}",
    "support_rate_trigger": "支持率 {value:.1%} >= 触发阈值 {trigger:.1%}",
    "support_rate_below": "支持率 {value:.1%} < 触发阈值 {trigger:.1%}",
    "test_failure_trigger": "测试失败率 {value:.1%} >= 触发阈值 {trigger:.1%}",
    "test_failure_below": "测试失败率 {value:.1%} < 触发阈值 {trigger:.1%}",
    "unknown_direction": "未知切换方向: {direction}",
    "light_signal": "轻度信号，仅记录: {context}",
    "heavy_signal": "重度信号，立即切换: {context}",
    "medium_signal_triggered": "中度信号累积 {count} 次，触发切换",
    "medium_signal_recorded": "中度信号已记录，等待累积",
    "mode_switch_log": "模式切换: {from_mode} -> {to_mode}, 原因: {reason}",
    "frequent_switch_warning": "检测到频繁切换 ({count}次/{window}秒)，滞后阈值宽度从 {old:.1%} 增加到 {new:.1%}",
    "cool_down_started": "开始冷却观察期 {seconds}秒",
    "cool_down_cancelled": "冷却观察期已取消: {reason}",
    "force_switch": "强制切换",
    "hysteresis_width_set": "滞后阈值宽度已设置为 {width:.1%}",
    "min_stay_time_set": "最小停留时间已设置为 {seconds} 秒",
    "hysteresis_width_invalid": "滞后阈值宽度必须在 0-0.5 之间",
    "min_stay_time_invalid": "最小停留时间不能为负数",
    "unknown_mode": "未知模式: {mode}",
    "locked": "已锁定 {mode} 模式",
    "unlocked": "已恢复自动切换",
    "recommendation": "建议增加滞后阈值宽度或延长最小停留时间"
}


class ModeState(Enum):
    """模式状态"""
    CONFERENCE = "conference"
    SERIAL = "serial"
    DEBATE = "debate"
    SWITCHING = "switching"  # 正在切换中


class SignalSeverity(Enum):
    """触发信号严重程度"""
    LIGHT = "light"      # 轻度：记录但不触发
    MEDIUM = "medium"    # 中度：累积计数
    HEAVY = "heavy"      # 重度：立即切换


class LockState(Enum):
    """锁定状态"""
    UNLOCKED = "unlocked"           # 允许自动切换
    LOCKED_CONFERENCE = "locked_conference"  # 锁定会议模式
    LOCKED_SERIAL = "locked_serial"          # 锁定串行模式
    LOCKED_DEBATE = "locked_debate"          # 锁定争吵模式


@dataclass
class SwitchRecord:
    """切换记录"""
    timestamp: float
    from_mode: str
    to_mode: str
    trigger_reason: str
    support_rate_before: Optional[float] = None
    support_rate_after: Optional[float] = None
    test_failure_rate: Optional[float] = None
    user_confirmed: bool = False


@dataclass
class HysteresisConfig:
    """滞后阈值配置"""
    # 会议→串行：触发阈值65%，恢复阈值55%
    conference_to_serial_trigger: float = 0.65
    conference_to_serial_recover: float = 0.55
    
    # 串行→会议：触发阈值60%，恢复阈值40%
    serial_to_conference_trigger: float = 0.60
    serial_to_conference_recover: float = 0.40


@dataclass
class OscillationConfig:
    """防震荡配置"""
    # 最小停留时间（秒）
    min_stay_time: float = 30.0
    
    # 滞后阈值配置
    hysteresis: HysteresisConfig = field(default_factory=HysteresisConfig)
    
    # 滞后阈值宽度（默认10%）
    hysteresis_width: float = 0.10
    
    # 连续检测窗口大小
    detection_window_size: int = 3
    
    # 触发切换所需的中度信号数量
    medium_signal_threshold: int = 2
    
    # 会议共识冷却观察期（秒）
    consensus_cool_down: float = 5.0
    
    # 频繁切换检测阈值
    frequent_switch_threshold: int = 3  # 5分钟内超过3次
    frequent_switch_window: float = 300.0  # 5分钟
    
    # 自动调整滞后阈值增量
    auto_adjust_increment: float = 0.10
    
    # 最大滞后阈值宽度
    max_hysteresis_width: float = 0.30


@dataclass
class SignalWindow:
    """信号窗口"""
    signals: deque = field(default_factory=lambda: deque(maxlen=3))
    
    def add_signal(self, severity: SignalSeverity) -> bool:
        """添加信号，返回是否应该触发切换"""
        self.signals.append({
            "severity": severity,
            "timestamp": time.time()
        })
        return self._should_trigger()
    
    def _should_trigger(self) -> bool:
        """检查是否应该触发切换"""
        # 重度信号立即触发
        for sig in self.signals:
            if sig["severity"] == SignalSeverity.HEAVY:
                return True
        
        # 中度信号累积触发
        medium_count = sum(
            1 for sig in self.signals 
            if sig["severity"] == SignalSeverity.MEDIUM
        )
        return medium_count >= 2
    
    def clear(self):
        """清空窗口"""
        self.signals.clear()


class OscillationGuard:
    """防震荡管理器"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self, config: Optional[OscillationConfig] = None):
        if hasattr(self, '_initialized') and self._initialized:
            return
        
        self._initialized = True
        self.config = config or OscillationConfig()
        
        # 当前状态
        self._current_state = ModeState.CONFERENCE
        self._lock_state = LockState.UNLOCKED
        
        # 时间追踪
        self._state_enter_time = time.time()
        
        # 信号窗口
        self._signal_window = SignalWindow()
        
        # 切换历史
        self._switch_history: List[SwitchRecord] = []
        
        # 滞后阈值宽度（可动态调整）
        self._current_hysteresis_width = self.config.hysteresis_width
        
        # 用户确认回调
        self._confirmation_callback: Optional[Callable] = None
        
        # 切换进行中标志
        self._is_switching = False
        
        # 观察期定时器
        self._cool_down_timer: Optional[threading.Timer] = None
        self._pending_switch: Optional[Tuple[str, str]] = None  # (from_mode, to_mode)
        
    @property
    def current_state(self) -> ModeState:
        """获取当前状态"""
        return self._current_state
    
    @property
    def lock_state(self) -> LockState:
        """获取锁定状态"""
        return self._lock_state
    
    @property
    def time_in_current_state(self) -> float:
        """在当前状态停留的时间"""
        return time.time() - self._state_enter_time
    
    def can_switch(self, target_mode: str, force: bool = False) -> Tuple[bool, str]:
        """
        检查是否允许切换
        
        Args:
            target_mode: 目标模式
            force: 是否强制切换（忽略防震荡条件）
            
        Returns:
            (是否允许切换, 原因说明)
        """
        # 强制切换
        if force:
            return True, "强制切换"
        
        # 检查锁定状态
        if self._lock_state != LockState.UNLOCKED:
            locked_mode = self._lock_state.value.replace("locked_", "")
            if target_mode != locked_mode:
                return False, f"模式已锁定为 {locked_mode}"
        
        # 检查是否正在切换中
        if self._is_switching or self._current_state == ModeState.SWITCHING:
            return False, "正在切换中，忽略新的切换请求"
        
        # 检查最小停留时间
        if self.time_in_current_state < self.config.min_stay_time:
            remaining = self.config.min_stay_time - self.time_in_current_state
            return False, f"停留时间不足，还需等待 {remaining:.1f} 秒"
        
        # 检查是否在同一模式
        if self._current_state.value == target_mode:
            return False, "已在目标模式中"
        
        return True, "允许切换"
    
    def check_hysteresis(self, value: float, direction: str) -> Tuple[bool, str]:
        """
        检查滞后阈值
        
        Args:
            value: 当前值（支持率或测试失败率）
            direction: 切换方向 ("conference_to_serial" 或 "serial_to_conference")
            
        Returns:
            (是否通过阈值检查, 原因说明)
        """
        h = self.config.hysteresis
        epsilon = 1e-9  # 浮点数精度容差
        
        if direction == "conference_to_serial":
            # 会议→串行：支持率需要>=触发阈值
            trigger = h.conference_to_serial_trigger + self._current_hysteresis_width / 2
            if value >= trigger - epsilon:
                return True, f"支持率 {value:.1%} >= 触发阈值 {trigger:.1%}"
            return False, f"支持率 {value:.1%} < 触发阈值 {trigger:.1%}"
        
        elif direction == "serial_to_conference":
            # 串行→会议：测试失败率需要>=触发阈值
            trigger = h.serial_to_conference_trigger + self._current_hysteresis_width / 2
            if value >= trigger - epsilon:
                return True, f"测试失败率 {value:.1%} >= 触发阈值 {trigger:.1%}"
            return False, f"测试失败率 {value:.1%} < 触发阈值 {trigger:.1%}"
        
        return False, f"未知切换方向: {direction}"
    
    def can_switch_back(self, value: float, direction: str) -> bool:
        """
        检查是否允许切回（恢复阈值）
        
        Args:
            value: 当前值
            direction: 反向切换方向
            
        Returns:
            是否允许切回
        """
        h = self.config.hysteresis
        
        if direction == "serial_to_conference":
            # 从串行切回会议：支持率需要<=恢复阈值
            recover = h.conference_to_serial_recover - self._current_hysteresis_width / 2
            return value <= recover
        
        elif direction == "conference_to_serial":
            # 从会议切回串行：测试失败率需要<=恢复阈值
            recover = h.serial_to_conference_recover - self._current_hysteresis_width / 2
            return value <= recover
        
        return True
    
    def add_signal(self, severity: SignalSeverity, context: str = "") -> Tuple[bool, str]:
        """
        添加触发信号
        
        Args:
            severity: 信号严重程度
            context: 上下文描述
            
        Returns:
            (是否应该触发切换, 原因说明)
        """
        logger.info(f"收到信号: {severity.value}, 上下文: {context}")
        
        # 轻度信号只记录
        if severity == SignalSeverity.LIGHT:
            return False, f"轻度信号，仅记录: {context}"
        
        # 重度信号立即触发
        if severity == SignalSeverity.HEAVY:
            return True, f"重度信号，立即切换: {context}"
        
        # 中度信号累积判断
        should_trigger = self._signal_window.add_signal(severity)
        
        if should_trigger:
            medium_count = sum(
                1 for sig in self._signal_window.signals 
                if sig["severity"] == SignalSeverity.MEDIUM
            )
            return True, f"中度信号累积 {medium_count} 次，触发切换"
        
        return False, "中度信号已记录，等待累积"
    
    def start_switch(self, target_mode: str, reason: str, 
                     support_rate: Optional[float] = None,
                     test_failure_rate: Optional[float] = None,
                     require_confirmation: bool = False) -> Tuple[bool, str]:
        """
        开始模式切换
        
        Args:
            target_mode: 目标模式
            reason: 切换原因
            support_rate: 当前的支持率
            test_failure_rate: 当前的测试失败率
            require_confirmation: 是否需要用户确认
            
        Returns:
            (是否开始切换, 原因说明)
        """
        # 检查是否允许切换
        can_switch, msg = self.can_switch(target_mode)
        if not can_switch:
            return False, msg
        
        # 需要用户确认
        if require_confirmation and self._confirmation_callback:
            self._pending_switch = (self._current_state.value, target_mode)
            # 这里应该触发异步确认请求
            return False, "等待用户确认"
        
        # 执行切换
        self._execute_switch(target_mode, reason, support_rate, test_failure_rate)
        return True, f"已切换到 {target_mode}"
    
    def _execute_switch(self, target_mode: str, reason: str,
                        support_rate: Optional[float] = None,
                        test_failure_rate: Optional[float] = None,
                        user_confirmed: bool = False):
        """执行切换"""
        from_mode = self._current_state.value
        
        # 记录切换
        record = SwitchRecord(
            timestamp=time.time(),
            from_mode=from_mode,
            to_mode=target_mode,
            trigger_reason=reason,
            support_rate_before=support_rate,
            test_failure_rate=test_failure_rate,
            user_confirmed=user_confirmed
        )
        self._switch_history.append(record)
        
        # 更新状态
        self._current_state = ModeState(target_mode)
        self._state_enter_time = time.time()
        self._signal_window.clear()
        
        # 检测频繁切换
        self._check_frequent_switches()
        
        logger.info(f"模式切换: {from_mode} -> {target_mode}, 原因: {reason}")
    
    def _check_frequent_switches(self):
        """检测频繁切换并自动调整"""
        now = time.time()
        window_start = now - self.config.frequent_switch_window
        
        # 统计窗口内的切换次数
        recent_switches = [
            r for r in self._switch_history 
            if r.timestamp >= window_start
        ]
        
        if len(recent_switches) > self.config.frequent_switch_threshold:
            # 自动增加滞后阈值宽度
            old_width = self._current_hysteresis_width
            self._current_hysteresis_width = min(
                self._current_hysteresis_width + self.config.auto_adjust_increment,
                self.config.max_hysteresis_width
            )
            logger.warning(
                f"检测到频繁切换 ({len(recent_switches)}次/{self.config.frequent_switch_window}秒)，"
                f"滞后阈值宽度从 {old_width:.1%} 增加到 {self._current_hysteresis_width:.1%}"
            )
    
    def start_cool_down(self, target_mode: str, reason: str,
                        callback: Optional[Callable] = None):
        """
        开始冷却观察期（会议共识后）
        
        Args:
            target_mode: 目标模式
            reason: 切换原因
            callback: 冷却期结束后的回调
        """
        # 取消之前的定时器
        if self._cool_down_timer:
            self._cool_down_timer.cancel()
        
        self._pending_switch = (self._current_state.value, target_mode)
        
        def on_cool_down_end():
            if self._pending_switch:
                from_mode, to_mode = self._pending_switch
                if callback:
                    callback(to_mode, reason)
                self._pending_switch = None
        
        self._cool_down_timer = threading.Timer(
            self.config.consensus_cool_down,
            on_cool_down_end
        )
        self._cool_down_timer.start()
        
        logger.info(f"开始冷却观察期 {self.config.consensus_cool_down}秒")
    
    def cancel_cool_down(self, reason: str = ""):
        """取消冷却观察期"""
        if self._cool_down_timer:
            self._cool_down_timer.cancel()
            self._cool_down_timer = None
        self._pending_switch = None
        logger.info(f"冷却观察期已取消: {reason}")
    
    def is_in_cool_down(self) -> bool:
        """是否在冷却观察期"""
        return self._cool_down_timer is not None and self._cool_down_timer.is_alive()
    
    # ========== 用户控制接口 ==========
    
    def lock_mode(self, mode: str) -> Tuple[bool, str]:
        """锁定模式"""
        mode_map = {
            "conference": LockState.LOCKED_CONFERENCE,
            "serial": LockState.LOCKED_SERIAL,
            "debate": LockState.LOCKED_DEBATE
        }
        
        if mode not in mode_map:
            return False, f"未知模式: {mode}"
        
        self._lock_state = mode_map[mode]
        return True, f"已锁定 {mode} 模式"
    
    def unlock(self) -> Tuple[bool, str]:
        """解锁模式"""
        self._lock_state = LockState.UNLOCKED
        return True, "已恢复自动切换"
    
    def force_switch(self, target_mode: str) -> Tuple[bool, str]:
        """强制切换（忽略所有防震荡条件）"""
        can_switch, msg = self.can_switch(target_mode, force=True)
        if not can_switch:
            return False, msg
        
        self._execute_switch(target_mode, "用户强制切换", user_confirmed=True)
        return True, f"已强制切换到 {target_mode}"
    
    def set_hysteresis_width(self, width: float) -> Tuple[bool, str]:
        """设置滞后阈值宽度"""
        if width < 0 or width > 0.5:
            return False, "滞后阈值宽度必须在 0-0.5 之间"
        
        self._current_hysteresis_width = width
        return True, f"滞后阈值宽度已设置为 {width:.1%}"
    
    def set_min_stay_time(self, seconds: float) -> Tuple[bool, str]:
        """设置最小停留时间"""
        if seconds < 0:
            return False, "最小停留时间不能为负数"
        
        self.config.min_stay_time = seconds
        return True, f"最小停留时间已设置为 {seconds} 秒"
    
    def set_confirmation_callback(self, callback: Callable):
        """设置用户确认回调"""
        self._confirmation_callback = callback
    
    # ========== 监控与日志 ==========
    
    def get_switch_history(self, count: int = 10) -> List[Dict]:
        """获取切换历史"""
        history = self._switch_history[-count:]
        return [
            {
                "timestamp": r.timestamp,
                "from_mode": r.from_mode,
                "to_mode": r.to_mode,
                "trigger_reason": r.trigger_reason,
                "support_rate_before": r.support_rate_before,
                "test_failure_rate": r.test_failure_rate,
                "user_confirmed": r.user_confirmed
            }
            for r in history
        ]
    
    def get_stats(self) -> Dict:
        """获取统计信息"""
        return {
            "current_state": self._current_state.value,
            "lock_state": self._lock_state.value,
            "time_in_state": self.time_in_current_state,
            "hysteresis_width": self._current_hysteresis_width,
            "min_stay_time": self.config.min_stay_time,
            "total_switches": len(self._switch_history),
            "is_in_cool_down": self.is_in_cool_down()
        }
    
    def analyze_oscillation(self) -> Dict:
        """分析震荡情况"""
        now = time.time()
        
        # 统计各时间窗口的切换次数
        windows = {
            "1分钟": 60,
            "5分钟": 300,
            "15分钟": 900,
            "1小时": 3600
        }
        
        analysis = {
            "windows": {},
            "frequent_switch_detected": False,
            "recommendation": None
        }
        
        for window_name, window_seconds in windows.items():
            count = sum(
                1 for r in self._switch_history 
                if r.timestamp >= now - window_seconds
            )
            analysis["windows"][window_name] = count
        
        # 检测频繁切换
        if analysis["windows"]["5分钟"] > 3:
            analysis["frequent_switch_detected"] = True
            analysis["recommendation"] = "建议增加滞后阈值宽度或延长最小停留时间"
        
        return analysis


# 单例获取函数
_oscillation_guard: Optional[OscillationGuard] = None

def get_oscillation_guard(config: Optional[OscillationConfig] = None) -> OscillationGuard:
    """获取防震荡管理器单例"""
    global _oscillation_guard
    if _oscillation_guard is None:
        _oscillation_guard = OscillationGuard(config)
    return _oscillation_guard


def classify_signal(text: str, 
                    heavy_keywords: List[str] = None,
                    medium_keywords: List[str] = None,
                    light_keywords: List[str] = None) -> SignalSeverity:
    """
    根据文本内容分类信号严重程度
    
    Args:
        text: 代理输出文本
        heavy_keywords: 重度信号关键词列表（可选）
        medium_keywords: 中度信号关键词列表（可选）
        light_keywords: 轻度信号关键词列表（可选）
        
    Returns:
        信号严重程度
    """
    text_lower = text.lower()
    
    # 使用提供的关键词或默认值
    heavy_kw = heavy_keywords or DEFAULT_HEAVY_KEYWORDS
    medium_kw = medium_keywords or DEFAULT_MEDIUM_KEYWORDS
    light_kw = light_keywords or DEFAULT_LIGHT_KEYWORDS
    
    # 重度信号关键词
    for kw in heavy_kw:
        if kw.lower() in text_lower:
            return SignalSeverity.HEAVY
    
    # 中度信号关键词
    for kw in medium_kw:
        if kw.lower() in text_lower:
            return SignalSeverity.MEDIUM
    
    # 轻度信号关键词
    for kw in light_kw:
        if kw.lower() in text_lower:
            return SignalSeverity.LIGHT
    
    return SignalSeverity.LIGHT
