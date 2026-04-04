"""代理策略演化模块

每个代理维护可微调的策略参数集：
- 发言阈值
- 打断倾向
- 冒险指数
- 合作倾向
- 工具调用偏好

演化数据来源：
- 发言频率
- 被引用频率
- 提案通过率
- 共识一致性
- 工具成功率
- 复盘指标（无效争论次数、打断成功率等）
"""
import time
import math
import random
import threading
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
import json


@dataclass
class StrategyParameters:
    """策略参数集"""
    # 发言相关
    speak_threshold: float = 0.5       # 发言阈值（0-1，越高越谨慎）
    interrupt_tendency: float = 0.3    # 打断倾向（0-1，越高越爱打断）
    
    # 决策相关
    risk_index: float = 0.5            # 冒险指数（0-1，越高越激进）
    cooperation_tendency: float = 0.5  # 合作倾向（0-1，越高越合作）
    
    # 工具相关
    tool_preference: float = 0.5       # 工具调用偏好（0-1，越高越爱用工具）
    
    # 参数范围限制
    param_ranges: Dict = field(default_factory=lambda: {
        "speak_threshold": (0.1, 0.9),
        "interrupt_tendency": (0.1, 0.9),
        "risk_index": (0.1, 0.9),
        "cooperation_tendency": (0.1, 0.9),
        "tool_preference": (0.1, 0.9)
    })
    
    def clamp(self):
        """确保参数在有效范围内"""
        for param, (min_val, max_val) in self.param_ranges.items():
            value = getattr(self, param)
            setattr(self, param, max(min_val, min(max_val, value)))
    
    def to_dict(self) -> Dict:
        return {
            "speak_threshold": self.speak_threshold,
            "interrupt_tendency": self.interrupt_tendency,
            "risk_index": self.risk_index,
            "cooperation_tendency": self.cooperation_tendency,
            "tool_preference": self.tool_preference
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> "StrategyParameters":
        return cls(**{k: v for k, v in data.items() if k != "param_ranges"})


@dataclass
class PerformanceMetrics:
    """性能指标"""
    # 发言相关
    speak_frequency: float = 0.0       # 发言频率（每小时）
    citation_count: int = 0            # 被其他代理引用次数
    citation_rate: float = 0.0         # 被引用率
    
    # 提案相关
    proposals_made: int = 0            # 提案数
    proposals_passed: int = 0          # 通过数
    proposal_pass_rate: float = 0.0    # 通过率
    
    # 共识相关
    consensus_consistency: float = 0.0  # 共识一致性（与最终结论的一致程度）
    
    # 工具相关
    tool_calls: int = 0                # 工具调用次数
    tool_successes: int = 0            # 成功次数
    tool_success_rate: float = 0.0     # 成功率
    tool_efficiency: float = 0.0       # 效率（成功率/调用次数）
    
    # 复盘指标
    invalid_arguments: int = 0         # 无效争论次数
    successful_interrupts: int = 0     # 成功打断次数
    failed_interrupts: int = 0         # 失败打断次数
    interrupt_success_rate: float = 0.0
    
    # 综合评分
    overall_score: float = 0.0
    
    def calculate_overall_score(self):
        """计算综合评分"""
        # 权重
        weights = {
            "citation_rate": 0.2,
            "proposal_pass_rate": 0.25,
            "consensus_consistency": 0.2,
            "tool_success_rate": 0.15,
            "interrupt_success_rate": 0.1,
            "invalid_arguments_penalty": -0.1
        }
        
        score = 0
        score += self.citation_rate * weights["citation_rate"]
        score += self.proposal_pass_rate * weights["proposal_pass_rate"]
        score += self.consensus_consistency * weights["consensus_consistency"]
        score += self.tool_success_rate * weights["tool_success_rate"]
        score += self.interrupt_success_rate * weights["interrupt_success_rate"]
        score -= min(self.invalid_arguments * 0.05, 0.2)  # 惩罚无效争论
        
        self.overall_score = max(0, min(1, score))
        return self.overall_score


@dataclass
class EvolutionRecord:
    """演化记录"""
    agent_id: str
    generation: int
    old_params: Dict
    new_params: Dict
    performance_before: float
    performance_after: float
    reason: str
    timestamp: float = field(default_factory=time.time)


class EvolutionEngine:
    """演化引擎"""
    
    def __init__(self, whiteboard):
        self.whiteboard = whiteboard
        
        # 代理策略参数
        self._agent_params: Dict[str, StrategyParameters] = {}
        
        # 代理性能指标
        self._agent_metrics: Dict[str, PerformanceMetrics] = {}
        
        # 演化历史
        self._evolution_history: List[EvolutionRecord] = []
        
        # 会话计数
        self._session_count = 0
        self._evolution_interval = 10  # 每10次会话演化一次
        
        # 演化参数
        self._mutation_rate = 0.1       # 变异率
        self._mutation_strength = 0.1   # 变异强度
        
        # 锁
        self._lock = threading.RLock()
    
    def init_agent(self, agent_id: str, personality: Dict):
        """初始化代理策略参数（从性格参数）"""
        with self._lock:
            # 从性格参数映射到策略参数
            cautiousness = personality.get("cautiousness", 5) / 10  # 0-1
            empathy = personality.get("empathy", 5) / 10
            abstraction = personality.get("abstraction", 5) / 10
            
            params = StrategyParameters(
                speak_threshold=0.3 + cautiousness * 0.5,      # 谨慎→高阈值
                interrupt_tendency=0.5 - empathy * 0.3,        # 共情→少打断
                risk_index=0.8 - cautiousness * 0.6,           # 谨慎→低冒险
                cooperation_tendency=0.3 + empathy * 0.5,      # 共情→高合作
                tool_preference=0.3 + abstraction * 0.4        # 抽象→爱用工具
            )
            params.clamp()
            
            self._agent_params[agent_id] = params
            self._agent_metrics[agent_id] = PerformanceMetrics()
    
    def get_params(self, agent_id: str) -> StrategyParameters:
        """获取代理策略参数"""
        with self._lock:
            if agent_id not in self._agent_params:
                return StrategyParameters()
            return self._agent_params[agent_id]
    
    def update_params(self, agent_id: str, **kwargs):
        """更新策略参数"""
        with self._lock:
            if agent_id not in self._agent_params:
                self._agent_params[agent_id] = StrategyParameters()
            
            for key, value in kwargs.items():
                if hasattr(self._agent_params[agent_id], key):
                    setattr(self._agent_params[agent_id], key, value)
            
            self._agent_params[agent_id].clamp()
    
    # ==================== 性能记录 ====================
    
    def record_speak(self, agent_id: str, hour_window: float = 1.0):
        """记录发言"""
        with self._lock:
            if agent_id not in self._agent_metrics:
                self._agent_metrics[agent_id] = PerformanceMetrics()
            self._agent_metrics[agent_id].speak_frequency += 1 / hour_window
    
    def record_citation(self, agent_id: str):
        """记录被引用"""
        with self._lock:
            if agent_id not in self._agent_metrics:
                self._agent_metrics[agent_id] = PerformanceMetrics()
            self._agent_metrics[agent_id].citation_count += 1
    
    def record_proposal(self, agent_id: str, passed: bool):
        """记录提案结果"""
        with self._lock:
            if agent_id not in self._agent_metrics:
                self._agent_metrics[agent_id] = PerformanceMetrics()
            self._agent_metrics[agent_id].proposals_made += 1
            if passed:
                self._agent_metrics[agent_id].proposals_passed += 1
    
    def record_tool_call(self, agent_id: str, success: bool):
        """记录工具调用"""
        with self._lock:
            if agent_id not in self._agent_metrics:
                self._agent_metrics[agent_id] = PerformanceMetrics()
            self._agent_metrics[agent_id].tool_calls += 1
            if success:
                self._agent_metrics[agent_id].tool_successes += 1
    
    def record_interrupt(self, agent_id: str, success: bool):
        """记录打断结果"""
        with self._lock:
            if agent_id not in self._agent_metrics:
                self._agent_metrics[agent_id] = PerformanceMetrics()
            if success:
                self._agent_metrics[agent_id].successful_interrupts += 1
            else:
                self._agent_metrics[agent_id].failed_interrupts += 1
    
    def record_invalid_argument(self, agent_id: str):
        """记录无效争论"""
        with self._lock:
            if agent_id not in self._agent_metrics:
                self._agent_metrics[agent_id] = PerformanceMetrics()
            self._agent_metrics[agent_id].invalid_arguments += 1
    
    def set_consensus_consistency(self, agent_id: str, consistency: float):
        """设置共识一致性"""
        with self._lock:
            if agent_id not in self._agent_metrics:
                self._agent_metrics[agent_id] = PerformanceMetrics()
            self._agent_metrics[agent_id].consensus_consistency = consistency
    
    def update_metrics(self, agent_id: str):
        """更新性能指标计算"""
        with self._lock:
            if agent_id not in self._agent_metrics:
                return
            
            m = self._agent_metrics[agent_id]
            
            # 计算各项率
            if m.speak_frequency > 0:
                m.citation_rate = m.citation_count / max(1, m.speak_frequency)
            
            if m.proposals_made > 0:
                m.proposal_pass_rate = m.proposals_passed / m.proposals_made
            
            if m.tool_calls > 0:
                m.tool_success_rate = m.tool_successes / m.tool_calls
                m.tool_efficiency = m.tool_success_rate / max(1, m.tool_calls) * 10
            
            total_interrupts = m.successful_interrupts + m.failed_interrupts
            if total_interrupts > 0:
                m.interrupt_success_rate = m.successful_interrupts / total_interrupts
            
            # 计算综合评分
            m.calculate_overall_score()
    
    # ==================== 演化算法 ====================
    
    def on_session_end(self):
        """会话结束回调"""
        with self._lock:
            self._session_count += 1
            
            # 更新所有代理的指标
            for agent_id in self._agent_metrics:
                self.update_metrics(agent_id)
            
            # 检查是否需要演化
            if self._session_count % self._evolution_interval == 0:
                return self.run_evolution()
            
            return None
    
    def run_evolution(self) -> Dict:
        """运行演化回合"""
        generation = self._session_count // self._evolution_interval
        evolution_records = []
        
        print(f"\n=== 演化回合 #{generation} ===")
        
        for agent_id, params in self._agent_params.items():
            old_params = params.to_dict()
            old_score = self._agent_metrics.get(agent_id, PerformanceMetrics()).overall_score
            
            # 变异
            new_params = self._mutate(params, agent_id)
            
            # 记录演化
            record = EvolutionRecord(
                agent_id=agent_id,
                generation=generation,
                old_params=old_params,
                new_params=new_params.to_dict(),
                performance_before=old_score,
                performance_after=0,  # 下一轮更新
                reason=self._get_evolution_reason(agent_id)
            )
            evolution_records.append(record)
            self._evolution_history.append(record)
            
            # 更新参数
            self._agent_params[agent_id] = new_params
        
        # 生成演化报告
        report = self._generate_evolution_report(generation, evolution_records)
        
        # 记录到白板
        self.whiteboard.add_message(
            agent_id="system",
            content=f"[演化] 第{generation}轮演化完成，共{len(evolution_records)}个代理参数已更新",
            message_type="system"
        )
        
        return {
            "generation": generation,
            "records": [r.__dict__ for r in evolution_records],
            "report": report
        }
    
    def _mutate(self, params: StrategyParameters, agent_id: str) -> StrategyParameters:
        """变异操作"""
        metrics = self._agent_metrics.get(agent_id, PerformanceMetrics())
        
        # 基于性能调整变异方向
        adjustments = {}
        
        # 如果提案通过率低，降低发言阈值（更积极参与）
        if metrics.proposal_pass_rate < 0.3:
            adjustments["speak_threshold"] = -self._mutation_strength
        elif metrics.proposal_pass_rate > 0.7:
            adjustments["speak_threshold"] = self._mutation_strength * 0.5
        
        # 如果打断成功率低，降低打断倾向
        if metrics.interrupt_success_rate < 0.3:
            adjustments["interrupt_tendency"] = -self._mutation_strength
        elif metrics.interrupt_success_rate > 0.7:
            adjustments["interrupt_tendency"] = self._mutation_strength * 0.5
        
        # 如果工具成功率低，降低工具偏好
        if metrics.tool_success_rate < 0.5 and metrics.tool_calls > 0:
            adjustments["tool_preference"] = -self._mutation_strength
        
        # 如果无效争论多，提高发言阈值
        if metrics.invalid_arguments > 3:
            adjustments["speak_threshold"] = self._mutation_strength
        
        # 随机变异
        for param in ["speak_threshold", "interrupt_tendency", "risk_index", 
                      "cooperation_tendency", "tool_preference"]:
            if random.random() < self._mutation_rate:
                if param not in adjustments:
                    adjustments[param] = random.uniform(-self._mutation_strength, 
                                                        self._mutation_strength)
        
        # 应用调整
        new_params = StrategyParameters(**params.to_dict())
        for param, delta in adjustments.items():
            current = getattr(new_params, param)
            setattr(new_params, param, current + delta)
        
        new_params.clamp()
        return new_params
    
    def _get_evolution_reason(self, agent_id: str) -> str:
        """获取演化原因"""
        metrics = self._agent_metrics.get(agent_id, PerformanceMetrics())
        reasons = []
        
        if metrics.proposal_pass_rate < 0.3:
            reasons.append("提案通过率低")
        if metrics.interrupt_success_rate < 0.3:
            reasons.append("打断成功率低")
        if metrics.tool_success_rate < 0.5 and metrics.tool_calls > 0:
            reasons.append("工具成功率低")
        if metrics.invalid_arguments > 3:
            reasons.append("无效争论过多")
        if metrics.citation_rate > 0.5:
            reasons.append("观点被频繁引用")
        
        return "; ".join(reasons) if reasons else "常规演化"
    
    def _generate_evolution_report(self, generation: int, records: List[EvolutionRecord]) -> str:
        """生成演化报告"""
        lines = [
            f"=== 演化报告 第{generation}轮 ===\n",
            f"会话数: {self._session_count}",
            f"演化代理数: {len(records)}\n"
        ]
        
        for r in records:
            lines.append(f"[{r.agent_id}]")
            lines.append(f"  原因: {r.reason}")
            
            # 参数变化
            for param in r.old_params:
                old_v = r.old_params[param]
                new_v = r.new_params[param]
                if abs(old_v - new_v) > 0.01:
                    direction = "+" if new_v > old_v else "-"
                    lines.append(f"  {param}: {old_v:.2f} → {new_v:.2f} ({direction})")
            
            lines.append(f"  综合评分: {r.performance_before:.2f}")
            lines.append("")
        
        return "\n".join(lines)
    
    # ==================== 导出/导入 ====================
    
    def export_state(self) -> Dict:
        """导出状态"""
        with self._lock:
            return {
                "session_count": self._session_count,
                "agent_params": {
                    aid: p.to_dict() 
                    for aid, p in self._agent_params.items()
                },
                "agent_metrics": {
                    aid: m.__dict__ 
                    for aid, m in self._agent_metrics.items()
                },
                "evolution_history": [r.__dict__ for r in self._evolution_history[-50:]]
            }
    
    def import_state(self, state: Dict):
        """导入状态"""
        with self._lock:
            self._session_count = state.get("session_count", 0)
            
            for aid, params in state.get("agent_params", {}).items():
                self._agent_params[aid] = StrategyParameters.from_dict(params)
            
            for aid, metrics in state.get("agent_metrics", {}).items():
                m = PerformanceMetrics()
                for k, v in metrics.items():
                    if hasattr(m, k):
                        setattr(m, k, v)
                self._agent_metrics[aid] = m
