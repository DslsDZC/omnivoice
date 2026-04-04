"""争吵强度调节器 - 自动调整会议讨论激烈程度"""
import time
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class IntensityLevel(Enum):
    """争吵强度等级 - 影响发言内容激烈程度，不影响并发顺序"""
    HARMONY = "harmony"         # 和谐（0-20%）- 温和礼貌语气
    MILD = "mild"               # 温和（20-40%）- 建设性讨论
    MODERATE = "moderate"       # 适中（40-60%）- 积极表达观点
    INTENSE = "intense"         # 激烈（60-80%）- 强力反驳争论
    FIERCE = "fierce"           # 猛烈（80-100%）- 激烈交锋辩论


@dataclass
class IntensityFactors:
    """影响争吵强度的因素"""
    # 任务复杂度 (0-100)
    task_complexity: float = 50.0
    
    # 观点分歧程度 (0-100)
    opinion_divergence: float = 50.0
    
    # 时间压力 (0-100)
    time_pressure: float = 30.0
    
    # 讨论轮次（用于计算疲劳度）
    current_round: int = 1
    max_rounds: int = 5
    
    # 共识进度 (0-100，越低说明分歧越大)
    consensus_progress: float = 50.0
    
    # 情感温度（代理情绪累积）
    emotional_temperature: float = 50.0
    
    # 用户参与度 (0-100)
    user_engagement: float = 50.0
    
    # 任务重要性 (0-100)
    task_importance: float = 50.0


@dataclass
class IntensityConfig:
    """争吵强度配置"""
    # 各因素权重
    weight_complexity: float = 0.15
    weight_divergence: float = 0.25
    weight_time_pressure: float = 0.15
    weight_consensus: float = 0.20
    weight_emotional: float = 0.10
    weight_importance: float = 0.10
    weight_fatigue: float = 0.05
    
    # 强度范围限制
    min_intensity: float = 10.0
    max_intensity: float = 95.0
    
    # 强度变化平滑系数（避免突变）
    smoothing_factor: float = 0.3
    
    # 各等级阈值
    level_thresholds: Dict[str, Tuple[float, float]] = field(default_factory=lambda: {
        "harmony": (0, 20),
        "mild": (20, 40),
        "moderate": (40, 60),
        "intense": (60, 80),
        "fierce": (80, 100)
    })
    
    # 消息模板
    messages: Dict[str, str] = field(default_factory=lambda: {
        "harmony_desc": "温和礼貌语气，理性表达观点",
        "mild_desc": "建设性讨论，适当表达不同意见",
        "moderate_desc": "积极表达观点，适度反驳推进讨论",
        "intense_desc": "坚持己见，强力反驳不合理观点",
        "fierce_desc": "全力捍卫立场，毫不退让激烈辩论"
    })


@dataclass
class IntensityState:
    """争吵强度状态"""
    current_intensity: float = 50.0
    current_level: IntensityLevel = IntensityLevel.MODERATE
    history: List[Dict] = field(default_factory=list)
    last_update_time: float = 0.0
    
    # 行为参数（根据强度计算）
    # 强度影响发言内容的激烈程度，通过 temperature 和 prompt 体现
    temperature_bias: float = 0.0      # 温度偏移量
    emotional_multiplier: float = 1.0   # 情感乘数


class IntensityRegulator:
    """争吵强度调节器"""
    
    def __init__(self, config: IntensityConfig = None):
        self.config = config or IntensityConfig()
        self.state = IntensityState()
        self._factors = IntensityFactors()
    
    def update_factors(self, **kwargs) -> float:
        """
        更新影响因素并重新计算强度
        
        Returns:
            更新后的强度值
        """
        # 更新因素
        for key, value in kwargs.items():
            if hasattr(self._factors, key):
                setattr(self._factors, key, value)
        
        # 计算新强度
        new_intensity = self._calculate_intensity()
        
        # 平滑过渡
        smoothed = self._smooth_transition(
            self.state.current_intensity, 
            new_intensity
        )
        
        # 更新状态
        self.state.current_intensity = smoothed
        self.state.current_level = self._get_level(smoothed)
        self.state.last_update_time = time.time()
        
        # 更新行为参数
        self._update_behavior_params()
        
        # 记录历史
        self._record_history(kwargs)
        
        logger.info(
            f"争吵强度更新: {smoothed:.1f}% ({self.state.current_level.value}), "
            f"因素: {kwargs}"
        )
        
        return smoothed
    
    def _calculate_intensity(self) -> float:
        """计算综合强度"""
        f = self._factors
        w = self.config
        
        # 计算疲劳度（轮次越多越疲劳，强度降低）
        fatigue_factor = 100 - (f.current_round / f.max_rounds * 30)
        
        # 观点分歧越大，强度越高
        divergence_factor = f.opinion_divergence
        
        # 共识进度越低（分歧大），强度越高
        consensus_factor = 100 - f.consensus_progress
        
        # 时间压力越大，强度越高（但过大会导致焦虑）
        time_factor = min(f.time_pressure * 1.2, 100)
        
        # 任务复杂度和重要性
        complexity_factor = f.task_complexity
        importance_factor = f.task_importance
        
        # 情感温度（争论越激烈，温度越高）
        emotional_factor = f.emotional_temperature
        
        # 加权计算
        intensity = (
            w.weight_complexity * complexity_factor +
            w.weight_divergence * divergence_factor +
            w.weight_time_pressure * time_factor +
            w.weight_consensus * consensus_factor +
            w.weight_emotional * emotional_factor +
            w.weight_importance * importance_factor +
            w.weight_fatigue * fatigue_factor
        )
        
        # 限制范围
        return max(self.config.min_intensity, 
                   min(self.config.max_intensity, intensity))
    
    def _smooth_transition(self, old: float, new: float) -> float:
        """平滑过渡，避免强度突变"""
        factor = self.config.smoothing_factor
        return old * (1 - factor) + new * factor
    
    def _get_level(self, intensity: float) -> IntensityLevel:
        """根据强度值获取等级"""
        thresholds = self.config.level_thresholds
        
        if intensity < thresholds["harmony"][1]:
            return IntensityLevel.HARMONY
        elif intensity < thresholds["mild"][1]:
            return IntensityLevel.MILD
        elif intensity < thresholds["moderate"][1]:
            return IntensityLevel.MODERATE
        elif intensity < thresholds["intense"][1]:
            return IntensityLevel.INTENSE
        else:
            return IntensityLevel.FIERCE
    
    def _update_behavior_params(self):
        """根据强度更新行为参数（影响发言内容激烈程度）"""
        intensity = self.state.current_intensity
        
        # 温度偏移：强度越高，温度越高（发言更激进/创意）
        self.state.temperature_bias = intensity / 200  # 0-0.475
        
        # 情感乘数：强度越高，情感变化越剧烈
        self.state.emotional_multiplier = 0.5 + (intensity / 100) * 1.0  # 0.5-1.5
    
    def _record_history(self, trigger_factors: Dict):
        """记录历史"""
        self.state.history.append({
            "timestamp": time.time(),
            "intensity": self.state.current_intensity,
            "level": self.state.current_level.value,
            "factors": {
                "complexity": self._factors.task_complexity,
                "divergence": self._factors.opinion_divergence,
                "time_pressure": self._factors.time_pressure,
                "consensus": self._factors.consensus_progress,
                "emotional": self._factors.emotional_temperature,
                "round": self._factors.current_round
            },
            "trigger": trigger_factors
        })
        
        # 限制历史记录数量
        if len(self.state.history) > 100:
            self.state.history = self.state.history[-50:]
    
    # ========== 行为参数获取 ==========
    
    @property
    def intensity(self) -> float:
        """获取当前强度"""
        return self.state.current_intensity
    
    @property
    def level(self) -> IntensityLevel:
        """获取当前等级"""
        return self.state.current_level
    
    @property
    def temperature_bias(self) -> float:
        """获取温度偏移量（影响发言激进程度）"""
        return self.state.temperature_bias
    
    @property
    def emotional_multiplier(self) -> float:
        """获取情感乘数"""
        return self.state.emotional_multiplier
    
    @property
    def min_speak_interval(self) -> float:
        """获取最小发言间隔（固定值，并发发言不依赖强度）"""
        return 0.5  # 固定短间隔，因为并发发言
    
    # ========== 因素更新接口 ==========
    
    def update_task_complexity(self, complexity: float):
        """更新任务复杂度"""
        self.update_factors(task_complexity=complexity)
    
    def update_opinion_divergence(self, divergence: float):
        """更新观点分歧程度"""
        self.update_factors(opinion_divergence=divergence)
    
    def update_time_pressure(self, pressure: float):
        """更新时间压力"""
        self.update_factors(time_pressure=pressure)
    
    def update_consensus_progress(self, progress: float):
        """更新共识进度"""
        self.update_factors(consensus_progress=progress)
    
    def update_round(self, current: int, max_rounds: int = None):
        """更新讨论轮次"""
        kwargs = {"current_round": current}
        if max_rounds:
            kwargs["max_rounds"] = max_rounds
        self.update_factors(**kwargs)
    
    def adjust_emotional_temperature(self, delta: float):
        """调整情感温度"""
        new_temp = max(0, min(100, self._factors.emotional_temperature + delta))
        self.update_factors(emotional_temperature=new_temp)
    
    def increase_heat(self, amount: float = 10.0):
        """升温（争论更激烈）"""
        self.adjust_emotional_temperature(amount)
        self.update_factors(opinion_divergence=min(100, self._factors.opinion_divergence + 5))
    
    def decrease_heat(self, amount: float = 10.0):
        """降温（争论缓和）"""
        self.adjust_emotional_temperature(-amount)
        self.update_factors(opinion_divergence=max(0, self._factors.opinion_divergence - 5))
    
    # ========== 状态查询 ==========
    
    def get_status(self) -> Dict:
        """获取完整状态"""
        return {
            "intensity": self.state.current_intensity,
            "level": self.state.current_level.value,
            "level_description": self.config.messages.get(
                f"{self.state.current_level.value}_desc", ""
            ),
            "behavior": {
                "temperature_bias": self.state.temperature_bias,
                "emotional_multiplier": self.state.emotional_multiplier,
                "speak_mode": "concurrent"  # 所有代理并发发言
            },
            "factors": {
                "task_complexity": self._factors.task_complexity,
                "opinion_divergence": self._factors.opinion_divergence,
                "time_pressure": self._factors.time_pressure,
                "consensus_progress": self._factors.consensus_progress,
                "emotional_temperature": self._factors.emotional_temperature,
                "current_round": f"{self._factors.current_round}/{self._factors.max_rounds}"
            }
        }
    
    def get_intensity_bar(self, width: int = 20) -> str:
        """获取强度条可视化"""
        filled = int(self.state.current_intensity / 100 * width)
        bar = "#" * filled + "-" * (width - filled)
        
        level_colors = {
            IntensityLevel.HARMONY: "[和谐]",
            IntensityLevel.MILD: "[温和]",
            IntensityLevel.MODERATE: "[中等]",
            IntensityLevel.INTENSE: "[激烈]",
            IntensityLevel.FIERCE: "[狂热]"
        }
        
        level_text = level_colors.get(self.state.current_level, "[?]")
        return f"{level_text} [{bar}] {self.state.current_intensity:.0f}%"


# 全局实例
_intensity_regulator: Optional[IntensityRegulator] = None


def get_intensity_regulator(config: IntensityConfig = None) -> IntensityRegulator:
    """获取争吵强度调节器实例"""
    global _intensity_regulator
    if _intensity_regulator is None:
        _intensity_regulator = IntensityRegulator(config)
    return _intensity_regulator


def reset_intensity_regulator():
    """重置调节器"""
    global _intensity_regulator
    _intensity_regulator = None
