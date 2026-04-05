"""模式决策器模块 - 三选一：会议、串行或争吵（带防震荡）"""
import asyncio
import json
import time
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple

from agent import Agent
from whiteboard import Whiteboard
from config_loader import VotingConfig, PromptsConfig, OscillationConfig, DEFAULT_PROMPTS
from oscillation_guard import (
    OscillationGuard, get_oscillation_guard, 
    ModeState, SignalSeverity, classify_signal
)


@dataclass
class ComplexityAssessment:
    """问题复杂度评估结果"""
    is_simple_fact: bool  # 是否是简单事实
    has_controversy: bool  # 是否有争议
    complexity_score: float  # 复杂度分数 0-10
    reason: str
    direct_mode: Optional[str] = None  # 如果确定，直接返回模式


@dataclass
class VotingResult:
    """投票结果"""
    agent_id: str
    decision: str  # "conference", "serial" 或 "debate"
    reason: str
    raw_response: str
    success: bool
    error: Optional[str] = None


@dataclass
class ModeDecision:
    """模式决策结果"""
    selected_mode: str  # "conference", "serial" 或 "debate"
    votes: List[VotingResult]
    vote_counts: Dict[str, int]
    tie_breaker_used: bool
    final_reason: str


class ModeDecisionMaker:
    """模式决策器 - 三选一"""
    
    def __init__(self, config: VotingConfig, prompts: Optional[PromptsConfig] = None):
        self.config = config
        self.prompts = prompts or PromptsConfig()
    
    async def _assess_question_complexity(self, agents: List[Agent], 
                                           question: str) -> ComplexityAssessment:
        """评估问题特征 - 决定模式"""
        print(f"\n[问题特征分析] 分析问题性质...")
        
        import re
        question_lower = question.lower()
        
        # ========== 第一层：用户明确意图检测 ==========
        
        # 用户要求直接回答的关键词
        direct_answer_keywords = [
            "直接回答", "直接告诉我", "直接说", "简单说", 
            "只说答案", "不要讨论", "快速回答", "简短回答",
            "一句话", "直接给答案", "不用讨论"
        ]
        
        # 用户要求讨论/分析/评估的关键词
        discussion_keywords = [
            "讨论", "分析", "评估", "辩论", "多角度",
            "从不同角度", "各方观点", "深入分析", "详细分析",
            "大家讨论", "一起讨论", "分析一下", "评估一下"
        ]
        
        # 检测用户明确要求直接回答
        for kw in direct_answer_keywords:
            if kw in question_lower:
                print(f"  [用户意图] 检测到「{kw}」→ 串行模式")
                return ComplexityAssessment(
                    is_simple_fact=True,
                    has_controversy=False,
                    complexity_score=2.0,
                    reason="用户明确要求直接回答",
                    direct_mode="serial"
                )
        
        # 检测用户明确要求讨论分析
        for kw in discussion_keywords:
            if kw in question_lower:
                print(f"  [用户意图] 检测到「{kw}」→ 会议模式")
                return ComplexityAssessment(
                    is_simple_fact=False,
                    has_controversy=True,
                    complexity_score=8.0,
                    reason="用户明确要求讨论分析",
                    direct_mode="conference"
                )
        
        # ========== 第二层：事实性 vs 开放性判断 ==========
        
        # 明显的事实性问题模式（答案唯一）
        fact_patterns = [
            r'^\s*\d+\s*[\+\-\*\/\^]\s*\d+\s*=\s*\?*\s*$',  # 算术
            r'^\s*\d+\s*[\+\-\*\/\^]\s*\d+',  # 计算表达式
            r'今天.*星期',  # 日期
            r'现在.*时间',  # 时间
            r'^计算\s+',  # 计算
            r'等于多少',  # 等于多少
            r'是多少\?*$',  # 是多少
            r'什么定义',  # 定义
            r'什么意思',  # 含义
            r'是谁\?*$',  # 人物
            r'在哪里\?*$',  # 地点
            r'什么时候\?*$',  # 时间点
        ]
        
        for pattern in fact_patterns:
            if re.search(pattern, question, re.IGNORECASE):
                print(f"  [问题类型] 事实性问题（答案唯一）→ 串行模式")
                return ComplexityAssessment(
                    is_simple_fact=True,
                    has_controversy=False,
                    complexity_score=2.0,
                    reason="事实性问题，答案唯一无争议",
                    direct_mode="serial"
                )
        
        # 开放性问题关键词（需要权衡）
        open_ended_keywords = [
            "应该", "是否", "好不好", "对不对", "值得", "利弊",
            "看法", "观点", "选择", "比较", "哪个好", "更好",
            "优缺点", "有人认为", "有人说", "一部分人", "不同意见",
            "建议", "推荐", "如何选择", "怎么办", "怎样处理",
            "评价", "怎么看", "分析下", "权衡"
        ]
        
        # 检测开放性问题
        open_keywords_found = [kw for kw in open_ended_keywords if kw in question_lower]
        if open_keywords_found:
            print(f"  [问题类型] 开放性问题（需权衡：{open_keywords_found[0]}）→ 会议模式")
            return ComplexityAssessment(
                is_simple_fact=False,
                has_controversy=True,
                complexity_score=7.0,
                reason=f"开放性问题，需要权衡多种可能",
                direct_mode="conference"
            )
        
        # ========== 无法判断，进入投票 ==========
        print(f"  [问题类型] 无法快速判断 → 进入投票决策")
        return ComplexityAssessment(
            is_simple_fact=False,
            has_controversy=False,
            complexity_score=5.0,
            reason="问题性质不明确，需投票决定",
            direct_mode=None  # None表示需要投票
        )
    
    async def vote(self, agents: List[Agent], question: str,
                   whiteboard: Optional[Whiteboard] = None,
                   timeout: float = 60.0) -> ModeDecision:
        """执行投票 - 展示决策过程"""
        # 选择投票代理（优先标准模型）
        voting_agents = self._select_voting_agents(agents)
        
        print(f"[模式决策] 开始")
        print(f"  问题: {question[:60]}...")
        
        # 第一步：评估问题复杂度
        assessment = await self._assess_question_complexity(voting_agents, question)
        
        # 如果问题性质明确，直接返回
        if assessment.direct_mode:
            mode_names = {"conference": "会议", "serial": "串行"}
            selected_name = mode_names.get(assessment.direct_mode, assessment.direct_mode)
            print(f"\n[直接决策] {selected_name}模式")
            print(f"  理由: {assessment.reason}")
            
            return ModeDecision(
                selected_mode=assessment.direct_mode,
                votes=[],
                vote_counts={"conference": 0, "serial": 1 if assessment.direct_mode == "serial" else 0},
                tie_breaker_used=False,
                final_reason=assessment.reason
            )
        
        # 第二步：复杂度不明确，进行投票
        print(f"\n[投票决策] 参与投票代理: {len(voting_agents)} 个")
        
        # 并行调用API
        print("\n[投票进行中...]")
        tasks = [
            self._get_agent_vote(agent, question, timeout)
            for agent in voting_agents
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 解析结果
        votes = []
        for i, result in enumerate(results):
            agent = voting_agents[i]
            if isinstance(result, Exception):
                votes.append(VotingResult(
                    agent_id=agent.id,
                    decision="serial",
                    reason="",
                    raw_response="",
                    success=False,
                    error=str(result)
                ))
            else:
                votes.append(result)
        
        # 显示每个代理的投票结果
        print("\n[投票详情]")
        mode_names = {"conference": "会议", "serial": "串行"}
        for v in votes:
            mode_name = mode_names.get(v.decision, v.decision)
            if v.success:
                reason_short = v.reason[:40] + "..." if len(v.reason) > 40 else v.reason
                print(f"  [{v.agent_id}] → {mode_name}模式")
                print(f"      理由: {reason_short}")
            else:
                print(f"  [{v.agent_id}] → 投票失败 ({v.error})")
        
        # 统计投票
        vote_counts = self._count_votes(votes)
        
        print(f"\n[票数统计]")
        conf_count = vote_counts.get("conference", 0)
        serial_count = vote_counts.get("serial", 0)
        total = conf_count + serial_count
        if total > 0:
            conf_pct = conf_count / total * 100
            serial_pct = serial_count / total * 100
            print(f"  会议模式: {conf_count} 票 ({conf_pct:.1f}%)")
            print(f"  串行模式: {serial_count} 票 ({serial_pct:.1f}%)")
        
        # 兜底：所有投票都失败时，默认使用串行模式
        all_failed = all(not v.success for v in votes)
        if all_failed:
            print(f"\n[警告] 所有投票失败，默认使用串行模式")
            if whiteboard:
                whiteboard.add_message(
                    agent_id="system",
                    content="[警告] 所有投票失败，默认使用串行模式",
                    message_type="system"
                )
            return ModeDecision(
                selected_mode="serial",
                votes=votes,
                vote_counts={"conference": 0, "serial": 1},
                tie_breaker_used=True,
                final_reason="所有投票失败，使用串行模式兜底"
            )
        
        # 选择最终模式
        selected_mode, tie_breaker_used = self._select_mode(vote_counts)
        
        # 显示最终决策
        selected_name = mode_names.get(selected_mode, selected_mode)
        print(f"\n[最终决策] 选择 {selected_name}模式")
        if tie_breaker_used:
            print(f"  (平局决胜)")
        
        # 获取最终理由
        final_reason = self._get_final_reason(votes, selected_mode)
        print(f"  主要理由: {final_reason[:60]}...")
        
        # 记录到白板
        if whiteboard:
            whiteboard.add_message(
                agent_id="system",
                content=f"模式投票: {selected_mode} (票数: {vote_counts})",
                message_type="system"
            )
        
        return ModeDecision(
            selected_mode=selected_mode,
            votes=votes,
            vote_counts=vote_counts,
            tie_breaker_used=tie_breaker_used,
            final_reason=final_reason
        )
    
    def _select_voting_agents(self, agents: List[Agent]) -> List[Agent]:
        """选择投票代理（优先标准模型，推理模型太慢）"""
        enabled = [a for a in agents if a.enabled]
        
        # 兜底：没有可用代理
        if not enabled:
            raise ValueError("没有可用的代理，请检查配置文件中至少启用一个代理")
        
        # 优先标准模型
        standard = [a for a in enabled if not a.is_reasoning_model]
        reasoning = [a for a in enabled if a.is_reasoning_model]
        
        # 兜底：全是推理模型时，使用推理模型
        if not standard:
            # 推理模型投票数量减半（因为较慢）
            count = max(1, self.config.agent_count // 2)
            return reasoning[:count]
        
        # 标准模型优先
        candidates = standard + reasoning
        
        count = min(self.config.agent_count, len(candidates))
        return candidates[:count]
    
    async def _get_agent_vote(self, agent: Agent, question: str,
                               timeout: float) -> VotingResult:
        """获取单个代理的投票"""
        prompt = self.prompts.mode_voting.format(question=question)
        
        # 根据代理性格调整温度，增加投票多样性
        # 独立性高的代理用更高温度，更可能有不同观点
        base_temp = 0.3
        independence = agent.personality.independence if hasattr(agent, 'personality') else 5
        temperature = base_temp + (independence - 5) * 0.05  # 范围 0.05-0.55
        temperature = max(0.1, min(0.7, temperature))
        
        messages = [{"role": "user", "content": prompt}]
        
        try:
            response = await asyncio.wait_for(
                agent.call_api(messages, temperature=temperature),
                timeout=timeout
            )
            
            if not response.success:
                return VotingResult(
                    agent_id=agent.id,
                    decision="serial",
                    reason="",
                    raw_response="",
                    success=False,
                    error=response.error
                )
            
            # 解析JSON响应
            content = response.content.strip()
            json_str = self._extract_json(content)
            
            if json_str:
                data = json.loads(json_str)
                decision = data.get("decision", "serial").lower()
                reason = data.get("reason", "")
                
                # 只接受 conference 或 serial
                if decision not in ["conference", "serial"]:
                    decision = "serial"
                
                return VotingResult(
                    agent_id=agent.id,
                    decision=decision,
                    reason=reason,
                    raw_response=content,
                    success=True
                )
            else:
                return VotingResult(
                    agent_id=agent.id,
                    decision="serial",
                    reason="",
                    raw_response=content,
                    success=False,
                    error="无法解析JSON"
                )
                
        except asyncio.TimeoutError:
            return VotingResult(
                agent_id=agent.id,
                decision="serial",
                reason="",
                raw_response="",
                success=False,
                error="投票超时"
            )
        except Exception as e:
            return VotingResult(
                agent_id=agent.id,
                decision="serial",
                reason="",
                raw_response="",
                success=False,
                error=str(e)
            )
    
    def _extract_json(self, text: str) -> Optional[str]:
        """从文本中提取JSON"""
        import re
        text = text.strip()
        
        # 找 JSON 对象
        match = re.search(r'\{[^{}]*"decision"[^{}]*\}', text, re.DOTALL)
        if match:
            return match.group(0)
        
        return None
    
    def _count_votes(self, votes: List[VotingResult]) -> Dict[str, int]:
        """统计投票"""
        counts = {"conference": 0, "serial": 0}
        
        for vote in votes:
            if vote.success and vote.decision in counts:
                counts[vote.decision] += 1
        
        return counts
    
    def _select_mode(self, vote_counts: Dict[str, int]) -> Tuple[str, bool]:
        """选择最终模式"""
        conf = vote_counts.get("conference", 0)
        serial = vote_counts.get("serial", 0)
        
        # 找出最高票
        max_votes = max(conf, serial)
        
        # 统计有多少模式获得最高票
        top_modes = []
        if conf == max_votes:
            top_modes.append("conference")
        if serial == max_votes:
            top_modes.append("serial")
        
        # 如果只有一个最高票
        if len(top_modes) == 1:
            return top_modes[0], False
        
        # 平局，使用 tie_breaker
        tie_breaker = self.config.tie_breaker
        if tie_breaker in top_modes:
            return tie_breaker, True
        else:
            # 默认选择会议模式
            return "conference", True
    
    def _get_final_reason(self, votes: List[VotingResult], 
                          selected_mode: str) -> str:
        """获取最终理由"""
        for vote in votes:
            if vote.success and vote.decision == selected_mode and vote.reason:
                return vote.reason
        
        mode_names = {"conference": "会议模式（内置智能争吵强度）", "serial": "串行模式"}
        return f"选择{mode_names.get(selected_mode, selected_mode)}"


def format_voting_result(decision: ModeDecision) -> str:
    """格式化投票结果输出"""
    lines = ["[模式投票结果]"]
    
    mode_names = {"conference": "会议", "serial": "串行"}
    
    for vote in decision.votes:
        mode_name = mode_names.get(vote.decision, vote.decision)
        if vote.success:
            reason_display = vote.reason[:50] + "..." if len(vote.reason) > 50 else vote.reason
            lines.append(f"- 代理 {vote.agent_id}：{mode_name} ({reason_display})")
        else:
            lines.append(f"- 代理 {vote.agent_id}：投票失败 ({vote.error})")
    
    selected_name = mode_names.get(decision.selected_mode, decision.selected_mode)
    tie_info = " (平局决胜)" if decision.tie_breaker_used else ""
    lines.append(f"\n-> 最终选择：{selected_name}模式{tie_info}")
    
    return "\n".join(lines)


@dataclass
class SwitchCheckResult:
    """模式切换检查结果"""
    should_switch: bool
    target_mode: Optional[str]
    reason: str
    requires_confirmation: bool = False
    hysteresis_passed: bool = False
    min_stay_satisfied: bool = True


class ModeSwitchManager:
    """模式切换管理器 - 处理自动模式切换与防震荡"""
    
    def __init__(self, oscillation_config: Optional[OscillationConfig] = None):
        self._guard = get_oscillation_guard(
            self._convert_config(oscillation_config) if oscillation_config else None
        )
    
    def _convert_config(self, config: OscillationConfig):
        """转换配置格式"""
        from oscillation_guard import OscillationConfig as OGConfig, HysteresisConfig
        return OGConfig(
            min_stay_time=config.min_stay_time,
            hysteresis=HysteresisConfig(
                conference_to_serial_trigger=config.hysteresis.conference_to_serial_trigger,
                conference_to_serial_recover=config.hysteresis.conference_to_serial_recover,
                serial_to_conference_trigger=config.hysteresis.serial_to_conference_trigger,
                serial_to_conference_recover=config.hysteresis.serial_to_conference_recover
            ),
            hysteresis_width=config.hysteresis_width,
            detection_window_size=config.detection_window_size,
            medium_signal_threshold=config.medium_signal_threshold,
            consensus_cool_down=config.consensus_cool_down,
            frequent_switch_threshold=config.frequent_switch_threshold,
            frequent_switch_window=config.frequent_switch_window,
            auto_adjust_increment=config.auto_adjust_increment,
            max_hysteresis_width=config.max_hysteresis_width,
            require_confirmation=config.require_confirmation
        )
    
    @property
    def current_mode(self) -> str:
        """获取当前模式"""
        return self._guard.current_state.value
    
    @property
    def guard(self) -> OscillationGuard:
        """获取防震荡管理器"""
        return self._guard
    
    def check_conference_to_serial(self, support_rate: float, 
                                    whiteboard: Optional[Whiteboard] = None) -> SwitchCheckResult:
        """
        检查是否应该从会议切换到串行
        
        Args:
            support_rate: 当前共识支持率
            whiteboard: 白板实例（用于检查步骤列表完整性）
            
        Returns:
            切换检查结果
        """
        # 检查是否在正确的模式
        if self._guard.current_state != ModeState.CONFERENCE:
            return SwitchCheckResult(
                should_switch=False,
                target_mode=None,
                reason=f"当前不是会议模式（{self._guard.current_state.value}）"
            )
        
        # 检查滞后阈值
        hysteresis_passed, hysteresis_reason = self._guard.check_hysteresis(
            support_rate, "conference_to_serial"
        )
        
        if not hysteresis_passed:
            return SwitchCheckResult(
                should_switch=False,
                target_mode="serial",
                reason=hysteresis_reason,
                hysteresis_passed=False
            )
        
        # 检查最小停留时间
        can_switch, switch_reason = self._guard.can_switch("serial")
        if not can_switch:
            return SwitchCheckResult(
                should_switch=False,
                target_mode="serial",
                reason=switch_reason,
                hysteresis_passed=True,
                min_stay_satisfied=False
            )
        
        # 检查步骤列表完整性
        if whiteboard:
            steps = whiteboard.get_task_steps()
            if not steps or len(steps) == 0:
                return SwitchCheckResult(
                    should_switch=False,
                    target_mode="serial",
                    reason="白板中没有明确的任务步骤，需要继续讨论",
                    hysteresis_passed=True
                )
        
        return SwitchCheckResult(
            should_switch=True,
            target_mode="serial",
            reason=f"共识支持率 {support_rate:.1%}，准备切换到串行执行",
            hysteresis_passed=True,
            min_stay_satisfied=True
        )
    
    def check_serial_to_conference(self, test_failure_rate: float,
                                    agent_output: str,
                                    require_confirmation: bool = True) -> SwitchCheckResult:
        """
        检查是否应该从串行切换到会议
        
        Args:
            test_failure_rate: 当前测试失败率
            agent_output: 代理输出文本（用于关键词检测）
            require_confirmation: 是否需要用户确认
            
        Returns:
            切换检查结果
        """
        # 检查是否在正确的模式
        if self._guard.current_state != ModeState.SERIAL:
            return SwitchCheckResult(
                should_switch=False,
                target_mode=None,
                reason=f"当前不是串行模式（{self._guard.current_state.value}）"
            )
        
        # 先检测信号严重程度
        signal_severity = classify_signal(agent_output)
        
        # 重度信号立即触发
        if signal_severity == SignalSeverity.HEAVY:
            can_switch, switch_reason = self._guard.can_switch("conference", force=True)
            if can_switch:
                return SwitchCheckResult(
                    should_switch=True,
                    target_mode="conference",
                    reason=f"检测到重度信号，立即切换到会议模式",
                    requires_confirmation=False,
                    hysteresis_passed=True
                )
        
        # 检查滞后阈值
        hysteresis_passed, hysteresis_reason = self._guard.check_hysteresis(
            test_failure_rate, "serial_to_conference"
        )
        
        # 添加信号到窗口
        signal_trigger, signal_reason = self._guard.add_signal(signal_severity, agent_output[:100])
        
        if not hysteresis_passed and not signal_trigger:
            return SwitchCheckResult(
                should_switch=False,
                target_mode="conference",
                reason=f"{hysteresis_reason}；{signal_reason}",
                hysteresis_passed=False
            )
        
        # 检查最小停留时间
        can_switch, switch_reason = self._guard.can_switch("conference")
        if not can_switch:
            return SwitchCheckResult(
                should_switch=False,
                target_mode="conference",
                reason=switch_reason,
                hysteresis_passed=True,
                min_stay_satisfied=False
            )
        
        return SwitchCheckResult(
            should_switch=True,
            target_mode="conference",
            reason=f"测试失败率 {test_failure_rate:.1%}，{signal_reason}",
            requires_confirmation=require_confirmation and self._guard.config.require_confirmation,
            hysteresis_passed=True,
            min_stay_satisfied=True
        )
    
    def execute_switch(self, target_mode: str, reason: str,
                       support_rate: Optional[float] = None,
                       test_failure_rate: Optional[float] = None,
                       force: bool = False) -> Tuple[bool, str]:
        """
        执行模式切换
        
        Args:
            target_mode: 目标模式
            reason: 切换原因
            support_rate: 当前支持率
            test_failure_rate: 当前测试失败率
            force: 是否强制切换
            
        Returns:
            (是否成功, 消息)
        """
        return self._guard.start_switch(
            target_mode=target_mode,
            reason=reason,
            support_rate=support_rate,
            test_failure_rate=test_failure_rate,
            require_confirmation=False
        )
    
    def start_consensus_cool_down(self, target_mode: str, reason: str,
                                   callback: Optional[callable] = None):
        """开始共识冷却观察期"""
        self._guard.start_cool_down(target_mode, reason, callback)
    
    def cancel_cool_down(self, reason: str = ""):
        """取消冷却观察期"""
        self._guard.cancel_cool_down(reason)
    
    def is_in_cool_down(self) -> bool:
        """是否在冷却观察期"""
        return self._guard.is_in_cool_down()
    
    def lock_mode(self, mode: str) -> Tuple[bool, str]:
        """锁定模式"""
        return self._guard.lock_mode(mode)
    
    def unlock(self) -> Tuple[bool, str]:
        """解锁模式"""
        return self._guard.unlock()
    
    def force_switch(self, target_mode: str) -> Tuple[bool, str]:
        """强制切换"""
        return self._guard.force_switch(target_mode)
    
    def get_switch_history(self, count: int = 10) -> List[Dict]:
        """获取切换历史"""
        return self._guard.get_switch_history(count)
    
    def get_stats(self) -> Dict:
        """获取统计信息"""
        return self._guard.get_stats()
    
    def analyze_oscillation(self) -> Dict:
        """分析震荡情况"""
        return self._guard.analyze_oscillation()


# 模式切换管理器单例
_mode_switch_manager: Optional[ModeSwitchManager] = None

def get_mode_switch_manager(config: Optional[OscillationConfig] = None) -> ModeSwitchManager:
    """获取模式切换管理器单例"""
    global _mode_switch_manager
    if _mode_switch_manager is None:
        _mode_switch_manager = ModeSwitchManager(config)
    return _mode_switch_manager