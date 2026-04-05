"""配置加载器模块"""
import os
import re
import yaml
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from pathlib import Path
from enum import Enum


class StanceType(Enum):
    """立场类型"""
    PRO = "pro"           # 赞成方
    CON = "con"           # 反对方
    NEUTRAL = "neutral"   # 中立观察员
    DEVIL_ADVOCATE = "devil_advocate"  # 魔鬼代言人


class UserRole(Enum):
    """用户角色"""
    DECIDER = "decider"       # 决策者（最终拍板）
    OBSERVER = "observer"     # 观察员（不参与投票）
    PARTICIPANT = "participant"  # 普通讨论者（与代理平等）


@dataclass
class APIConfig:
    """API配置"""
    base_url: str
    api_key: str
    model: str
    # 推理模型参数
    reasoning_model: bool = False           # 是否为推理模型
    reasoning_effort: str = "medium"        # 推理努力程度: low/medium/high
    supports_tools: bool = True             # 是否支持工具调用
    supports_system_message: bool = True    # 是否支持系统消息
    max_tokens: int = 4096                  # 最大输出 tokens
    temperature: Optional[float] = None     # 温度（推理模型通常不支持）
    top_p: Optional[float] = None           # top_p（推理模型通常不支持）


@dataclass
class PersonalityConfig:
    """性格配置"""
    cautiousness: int = 5  # 谨慎度 0-10
    empathy: int = 5       # 共情度 0-10
    abstraction: int = 5   # 抽象度 0-10
    independence: int = 7  # 独立性 0-10 (0=完全顺从, 10=高度批判)
    default_stance: str = "neutral"  # 默认立场: support/oppose/question/neutral


@dataclass
class AgentConfig:
    """代理配置"""
    id: str
    api: APIConfig
    personality: PersonalityConfig
    allowed_tools: List[str]
    enabled: bool = True
    # 代理级别提示词覆盖（可选）
    custom_prompts: Dict[str, str] = field(default_factory=dict)
    # 预设立场（可选，默认由系统动态分配）
    preset_stance: Optional[str] = None  # pro/con/neutral/devil_advocate


@dataclass
class WorkspaceConfig:
    """工作区配置"""
    base_dir: str = "./temp_workspaces"
    per_session_limit_mb: int = 100
    execution_timeout_sec: int = 30
    allowed_languages: List[str] = field(default_factory=lambda: ["python", "bash"])


@dataclass
class VotingConfig:
    """投票配置"""
    agent_count: int = 3
    tie_breaker: str = "serial"  # 平局时选择：serial 或 conference


@dataclass
class RepeatConfig:
    """重复检测配置"""
    single_agent_threshold: int = 3     # 单个代理重复次数阈值
    mass_ratio_threshold: float = 0.25  # 大量重复比例阈值
    penalty_sec: int = 10               # 重复惩罚静默时间
    similarity_threshold: float = 0.85  # 重复相似度阈值


@dataclass
class InterruptConfig:
    """叫停投票配置"""
    min_participants: int = 5           # 最小参与人数
    pass_threshold: int = 3             # 通过票数阈值
    max_wait_sec: int = 60              # 指定叫停最大等待时间


@dataclass
class IntervalConfig:
    """时间间隔配置"""
    think_interval: float = 0.5         # 正常思考间隔（秒）
    mute_check_interval: float = 1      # 静音检查间隔（秒）
    idle_check_interval: float = 5      # 空闲检测间隔（秒）
    user_input_check_interval: float = 0.5  # 用户输入检查间隔（秒）


@dataclass
class AutoConvergeConfig:
    """自动收敛配置"""
    max_attempts: int = 3               # 最大收敛尝试次数


@dataclass
class ConferenceConfig:
    """会议模式配置"""
    max_rounds: int = 5
    consensus_threshold: float = 0.6
    discussion_timeout_sec: int = 300
    idle_timeout_sec: int = 10
    max_messages_per_agent: int = 8
    extract_proposals_timeout_sec: int = 120  # 提取方案超时时间
    serial_execution_keywords: List[str] = field(default_factory=lambda: [
        "执行", "步骤", "完成", "实现", "创建", "生成", "运行", "计算", "编写"
    ])
    repeat: RepeatConfig = field(default_factory=RepeatConfig)
    interrupt: InterruptConfig = field(default_factory=InterruptConfig)
    intervals: IntervalConfig = field(default_factory=IntervalConfig)
    auto_converge: AutoConvergeConfig = field(default_factory=AutoConvergeConfig)


@dataclass
class SerialConfig:
    """串行模式配置"""
    step_timeout_sec: int = 60
    max_retries: int = 2
    trigger_meeting_keywords: List[str] = field(default_factory=lambda: [
        "不确定", "有分歧", "需要讨论", "无法确定", "建议",
        "多种方案", "争议", "需要帮助", "[REQUEST_MEETING]",
        "不确定如何", "需要决策", "无法判断"
    ])


@dataclass
class TempMeetingConfig:
    """临时会议配置（串行模式触发时使用）"""
    max_nesting_depth: int = 2
    timeout_sec: int = 60
    max_agents: int = 3


@dataclass
class DebateConfig:
    """争吵模式配置"""
    max_duration_sec: int = 300         # 最大持续时间
    idle_timeout_sec: int = 15          # 空闲超时
    max_events: int = 500               # 最大事件数
    consensus_threshold: int = 3        # 叫停所需票数
    interrupt_threshold: int = 15       # 打断分数阈值
    min_speak_duration: float = 0.5     # 最小发言时间
    max_speak_tokens: int = 30          # 每次发言最大 token
    history_contribution_weight: float = 2.0  # 历史贡献权重


@dataclass
class NeutralityConfig:
    """中立性配置"""
    # 全局独立性基准（0-10）
    min_independence: int = 7
    # 立场分配模式: neutral/devil_advocate/mirror
    stance_mode: str = "devil_advocate"
    # 魔鬼代言人数量
    devil_advocate_count: int = 2
    # 立场再平衡阈值（当超过此比例的代理同意时触发）
    rebalance_threshold: float = 0.8
    # 中立性强度（0-100%，用户可调节）
    neutrality_level: int = 70
    # 用户投票权重（相对于单个代理）
    user_vote_weight: float = 1.0
    # 事实检查严格程度: loose/normal/strict
    fact_check_level: str = "normal"
    # 启用自动中立性测试
    enable_neutrality_test: bool = True


@dataclass
class UserConfig:
    """用户配置"""
    # 用户角色: decider/observer/participant
    role: str = "participant"
    # 用户投票权重（仅当 role=participant 时生效）
    vote_weight: float = 1.0
    # 用户可打断讨论
    can_interrupt: bool = True
    # 用户发言标记为"用户观点"（非事实）
    mark_as_opinion: bool = True


@dataclass
class RateLimitConfig:
    """速率限制配置"""
    max_calls: int = 10          # 最大调用次数
    window_seconds: int = 60     # 时间窗口（秒）
    cooldown_seconds: int = 60   # 冷却时间（秒）


@dataclass
class SecurityConfig:
    """安全配置"""
    # 安全级别: strict/standard/permissive
    level: str = "standard"
    
    # 代码执行模式: docker/subprocess/nsjail
    execution_mode: str = "subprocess"
    
    # 速率限制
    default_rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    
    # 审计配置
    audit_enabled: bool = True
    audit_log_path: str = "./logs/tool_audit.jsonl"
    
    # 异常行为检测
    anomaly_detection: bool = True
    violation_threshold: int = 5      # 违规阈值
    auto_disable_duration: int = 300  # 自动禁用时长（秒）
    
    # 网络工具
    network_tools_enabled: bool = False
    network_whitelist: List[str] = field(default_factory=list)
    
    # 路径沙箱
    max_file_size_mb: int = 10
    max_workspace_size_mb: int = 100
    max_file_count: int = 1000
    forbidden_extensions: List[str] = field(default_factory=lambda: [
        ".key", ".pem", ".env", ".secret", ".credentials"
    ])
    
    # 代码执行
    code_timeout_seconds: int = 30
    code_max_output_size: int = 1024 * 1024  # 1 MB
    allowed_languages: List[str] = field(default_factory=lambda: ["python", "bash"])


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
    
    # 是否需要用户确认串行→会议切换
    require_confirmation: bool = True


@dataclass
class BehaviorConfig:
    """会议行为配置"""
    # 议程设置
    enable_agenda: bool = True
    auto_agenda_from_proposal: bool = True
    
    # 时间提醒
    enable_time_reminder: bool = True
    time_warning_thresholds: List[float] = field(default_factory=lambda: [0.5, 0.75, 0.9])
    auto_timeout_signal: bool = True
    
    # 离题检测
    enable_off_topic_detection: bool = True
    off_topic_similarity_threshold: float = 0.3
    off_topic_penalty: float = 0.5
    
    # 讨论总结
    enable_summary: bool = True
    summary_interval_rounds: int = 5
    auto_summary: bool = True
    
    # 修正动议
    enable_modify_motion: bool = True
    
    # 事实核查
    enable_fact_check: bool = True
    
    # 请求外部输入
    enable_request_input: bool = True
    
    # 搁置争议
    enable_table_issue: bool = True
    table_threshold: float = 0.5
    
    # 优先级排序
    enable_priority_sort: bool = True
    
    # 方案对比
    enable_compare_options: bool = True
    min_options_for_compare: int = 2


@dataclass
class SignalKeywordsConfig:
    """信号关键词配置"""
    heavy: List[str] = field(default_factory=lambda: [
        "立即停止", "紧急会议", "严重问题", "重大分歧",
        "必须讨论", "无法继续", "critical", "urgent"
    ])
    medium: List[str] = field(default_factory=lambda: [
        "需要讨论", "不确定", "有问题", "建议开会",
        "需要确认", "不太清楚", "需要协作", "discuss", "uncertain", "not sure"
    ])
    light: List[str] = field(default_factory=lambda: [
        "有点不确定", "可能需要", "稍微有点", "minor", "slight", "possibly"
    ])


@dataclass
class CommandMessagesConfig:
    """命令响应消息配置"""
    pause: str = "已暂停所有代理发言，使用 /resume 恢复"
    resume: str = "已恢复讨论"
    vote: str = "已发起投票: {args}"
    vote_default: str = "已发起投票: 立即表决"
    stop: str = "已结束会话: {args}"
    mute: str = "已静音代理: {agent_id}"
    unmute: str = "已取消静音代理: {agent_id}"
    clear: str = "已清空白板，讨论重新开始"
    mode: str = "已切换到模式: {mode}"
    force: str = "强制打断: {args}"
    abort_vote: str = "已取消当前投票，恢复讨论"
    weights: str = "显示代理权重..."
    history: str = "显示插话历史..."
    readonly_on: str = "已切换到只读模式"
    readonly_off: str = "已退出只读模式"
    unknown: str = "未知命令: {command}"
    locked: str = "模式已锁定为 {mode}"
    unlocked: str = "已恢复自动切换"
    hysteresis_set: str = "滞后阈值宽度已设置为 {width}"
    minstay_set: str = "最小停留时间已设置为 {seconds} 秒"


@dataclass
class OscillationMessagesConfig:
    """防震荡消息配置"""
    cooldown_active: str = "冷却观察期已激活，等待 {seconds} 秒"
    cooldown_cancelled: str = "冷却观察期已取消: {reason}"
    switch_rejected: str = "切换请求被拒绝: {reason}"
    frequent_switch: str = "检测到频繁切换 ({count}次/{window}秒)，滞后阈值宽度从 {old} 增加到 {new}"
    recommendation: str = "建议增加滞后阈值宽度或延长最小停留时间"
    min_stay_not_met: str = "停留时间不足，还需等待 {remaining} 秒"
    hysteresis_not_passed: str = "{metric} {value} < 触发阈值 {threshold}"
    signal_recorded: str = "信号已记录: {severity}"
    signal_triggered: str = "中度信号累积 {count} 次，触发切换"
    heavy_signal: str = "重度信号，立即切换: {context}"


@dataclass
class InterruptMessagesConfig:
    """用户插话消息配置"""
    cooldown: str = "冷却中，剩余 {remaining} 秒"
    readonly_mode: str = "用户处于只读模式"
    reputation_low: str = "用户声誉过低 ({reputation})"
    force_limit_exceeded: str = "超过每小时强制打断限制"
    voting_restricted: str = "投票期间，已转为建议模式"
    converted_to_suggestion: str = "冷却中，已转为建议型"


@dataclass
class SystemMessagesConfig:
    """系统消息配置"""
    all_votes_failed: str = "[警告] 所有投票失败，默认使用串行模式"
    mode_vote_result: str = "模式投票: {mode} (票数: {counts})"
    consensus_reached: str = "共识已达成，支持率: {rate}"
    step_complete: str = "步骤 {step_id} 完成"
    task_complete: str = "所有任务已完成"


@dataclass
class ModesNameConfig:
    """模式名称配置"""
    conference: str = "会议"
    serial: str = "串行"
    debate: str = "争吵"


@dataclass
class TimeWindowsConfig:
    """时间窗口名称配置"""
    one_min: str = "1分钟"
    five_min: str = "5分钟"
    fifteen_min: str = "15分钟"
    one_hour: str = "1小时"


@dataclass
class LanguageConfig:
    """语言配置"""
    # 当前语言
    current: str = "zh-CN"
    
    # 模式名称
    modes: ModesNameConfig = field(default_factory=ModesNameConfig)
    
    # 信号关键词
    signal_keywords: SignalKeywordsConfig = field(default_factory=SignalKeywordsConfig)
    
    # 命令响应消息
    commands: CommandMessagesConfig = field(default_factory=CommandMessagesConfig)
    
    # 防震荡消息
    oscillation: OscillationMessagesConfig = field(default_factory=OscillationMessagesConfig)
    
    # 用户插话消息
    interrupt: InterruptMessagesConfig = field(default_factory=InterruptMessagesConfig)
    
    # 系统消息
    system: SystemMessagesConfig = field(default_factory=SystemMessagesConfig)
    
    # 时间窗口名称
    time_windows: TimeWindowsConfig = field(default_factory=TimeWindowsConfig)


# 默认提示词
DEFAULT_PROMPTS = {
    "mode_voting": """你是一个决策者。分析下面的用户问题，判断应该使用「会议」模式还是「串行」模式。

- 会议模式(conference)：适用于开放性问题、头脑风暴、多观点权衡、需要讨论达成共识的任务。会议模式内置智能争吵强度，会根据任务复杂度、观点分歧等因素自动调整讨论激烈程度。
- 串行模式(serial)：适用于有明确步骤顺序、依赖关系清晰、流水线式执行的任务。

注意：如果需要先讨论再执行，选择会议模式，系统会自动在讨论后进入执行阶段。
如果执行中遇到分歧，系统会自动触发临时会议。

用户问题：
{question}

输出格式（严格JSON）：
{{"decision": "conference", "reason": "因为问题需要多角度讨论"}}
或
{{"decision": "serial", "reason": "因为任务有明确步骤"}}

只输出JSON。""",

    "conference_discussion": """你是会议参与者，具备专家级分析能力。

身份：{identity}
性格：{personality}
主题：{topic}

=== 已有讨论 ===
{discussion_history}

=== 高级分析框架 ===

【第一阶段：苏格拉底式提问】
在发言前，先问自己三个问题：
1. 这个方案的核心假设是什么？假设是否合理？
2. 如果这个方案是错的，最可能的原因是什么？
3. 有什么反例或边界情况被忽略了？

【第二阶段：辩证分析】
正题：现有方案的优点是什么？
反题：现有方案的问题/漏洞是什么？（必须找出至少一个）
合题：如何改进或综合？

【第三阶段：多维验证】
根据问题类型，选择合适的验证维度：
- 数值类：计算验证、单位换算、边界检查
- 方案类：可行性、完整性、约束满足度
- 创意类：创新性、实用性、可实现性
- 决策类：利弊权衡、风险评估、替代方案

【第四阶段：专业视角】
自动识别问题领域，从专业角度审视：
- 识别问题所属领域（如：规划/设计/分析/评估等）
- 应用该领域的专业标准和方法论
- 指出非专业人士容易忽略的问题

=== 输出格式（严格按此格式，缺一不可）===
[立场：支持/反对/质疑/补充/修正]
【给人看】一句人类能懂的自然口语，像真人聊天说话（必须输出！这是给用户看的）
【核心观点】一句话概括你的贡献
【分析过程】一句话说明问题识别和验证结果
【建议】一句话改进方案（如有）

⚠️ 警告：【给人看】是必填项！必须用口语化的方式说一句人话！

=== 质量红线 ===
⛔ 禁止空话："我支持"、"我同意"必须附带具体理由
⛔ 禁止复制：不能复制粘贴已有内容
⛔ 禁止跳过验证：涉及数据/事实必须验证
⛔ 禁止过早结束：方案不完善时不能用[END]

=== 叫停机制 ===
[INTERRUPT] 全体叫停提议：你的提案
[INTERRUPT:@{agent_id}] 指定叫停：针对某代理
[AGENDA_END] 议程结束请求

=== [END]触发条件 ===
✓ 方案具体到可直接执行
✓ 所有约束条件已验证满足
✓ 至少有1次质疑和回应
✓ 关键信息已核实正确

请发表你的观点：""",

    "conference_voting": """表决阶段。

主题：{topic}
提案：{proposal}
提案者：{proposer}

输出JSON：{{"vote": "support/oppose/modify", "reason": "原因", "suggestion": "修改建议(可选)"}}""",

    "task_extraction": """从提案中提取执行步骤。

提案：{proposal}

输出JSON数组：
[{{"step_id": 1, "description": "步骤描述", "expected_output": "预期输出", "suggested_tools": []}}]""",

    "step_execution": """执行步骤 {step_id}: {description}

预期输出：{expected_output}
前置结果：{previous}

请执行并输出结果：""",

    "task_decomposition": """拆解任务为JSON步骤：

任务：{task}

输出：[{{"step_id": 1, "description": "...", "expected_output": "...", "suggested_tools": []}}]""",

    "serial_step_execution": """执行步骤 {step_id}: {description}

预期输出：{expected_output}
前置结果：{previous}
可用工具：{tools}

执行并输出结果：""",

    "temp_meeting": """临时会议：执行遇到问题

步骤：{step_id} - {description}
问题：{issue}
前置结果：{previous}

请快速给出解决方案。输出JSON：
{{"solution": "解决方案", "confidence": 0.0-1.0}}""",

    "vote_prompt": """对以下提案投票。

提案：{proposal}

请输出你的投票和理由：
同意/反对 [你的观点和理由]

示例：同意 我认为这个方案考虑周全，可行性高。""",

    "ranked_vote_prompt": """对以下方案排序（最好到最差）：

{proposals}

输出：方案编号排序，如 2,1,3""",

    "final_resolution_prompt": """你是会议总结专家，请根据讨论内容生成一份完整、详细的会议总结报告。

原始问题：{question}

讨论记录：
{discussion}

请按以下结构输出会议总结（每个部分至少50字）：

## 一、问题背景与讨论目标
[描述问题的核心内容和讨论的目标]

## 二、主要观点汇总
[列出讨论中提出的主要观点和方案，标注提出者]

## 三、关键论据分析
[分析支持各方观点的关键理由和依据]

## 四、讨论过程中的重要共识与分歧
[记录达成的共识和仍存在的分歧点]

## 五、最终结论与建议方案
[给出明确、具体、可执行的结论或方案]

## 六、后续行动建议（如适用）
[列出建议的后续步骤或注意事项]

---
注意：
1. 必须涵盖所有重要观点，不要遗漏
2. 结论要具体明确，避免模糊表述
3. 如有执行方案，要列出具体步骤
4. 保持客观中立，准确反映讨论内容

直接输出会议总结：""",

    "serial_task_decomposition": """将以下任务分解为具体的执行步骤。

任务：{task}

要求：
1. 每个步骤应该清晰、可执行
2. 步骤之间有明确的依赖关系
3. 每个步骤有明确的预期输出

输出JSON数组：
[{{"step_id": 1, "description": "步骤描述", "expected_output": "预期输出", "suggested_tools": []}}]""",

    "serial_step_speak": """你是步骤 {step_id} 的第 {turn} 个发言代理。

身份：{identity}
性格：{personality}

步骤描述：{description}
预期输出：{expected_output}
已有结果：{previous}

请执行并输出你的结果：""",

    "serial_quick_decision": """快速决策：步骤 {step_id} 遇到问题

问题：{issue}
可选方案：{options}

请选择最佳方案，输出方案编号：""",

    "serial_proposal": """请针对以下步骤提出解决方案：

问题：{question}
当前步骤：{step_description}
期望输出：{expected_output}
{context}

请直接输出你的方案。""",

    "serial_vote": """请对以下方案投票：

【步骤】{step_description}
【方案】{proposal}

请投票：
- 同意：方案正确可行
- 反对：方案有问题需要修改

输出格式：
[投票：同意/反对]
【给人看】一句话说明你的判断
【理由】具体的分析（如有问题请指出）""",

    "serial_revise": """请根据投票结果修改方案：

【原方案】{proposal}
【反对意见】{objections}
【反对人数】{oppose_count}/{total_count}

请修改方案，解决反对意见指出的问题。""",

    "serial_feedback": """请审议以下方案：

步骤：{step_description}
方案：{proposal}
{existing_proposals}

如果有反对意见输出JSON：{{"agree": false, "objection": "问题所在"}}
如果同意输出JSON：{{"agree": true, "reason": "同意理由"}}""",

    "serial_temp_meeting": """步骤执行遇到问题，需要临时会议讨论。

步骤：{step_description}
当前方案：{proposal}
问题：{issue}

请各位代理讨论并给出建议。""",

    "end_check_prompt": """判断讨论是否应该结束。

问题：{question}
最近讨论：{discussion}

输出JSON：
{{"should_end": true/false, "reason": "原因"}}""",

    "agenda_generation": """你正在组织一个会议，需要讨论以下问题：

问题：{question}

请分析这个问题，生成一份详细的会议议程。议程应该：
1. 将问题分解为可讨论的子议题
2. 每个议题必须包含2-4个具体的子问题（sub_questions），这些子问题是讨论时需要回答的核心问题
3. 子问题要具体、可执行、可讨论，避免抽象泛泛而谈
4. 议题之间有逻辑顺序
5. 通常3-5个议题为宜

输出JSON数组格式：
[
  {{
    "title": "议题标题",
    "description": "议题详细描述",
    "sub_questions": [
      "具体子问题1：需要回答什么？",
      "具体子问题2：需要验证什么？",
      "具体子问题3：需要决策什么？"
    ]
  }}
]

只输出JSON数组，不要其他内容。""",

    "agenda_debate": """你正在参与会议议程的讨论确定。

原始问题：{question}

目前提出的议程方案：
{agenda_summary}

你的性格特点：
- 独立性：{independence}/10（越高越倾向于质疑）
- 谨慎性：{cautiousness}/10（越高越注重细节）

请根据你的性格：
1. 对其他代理的议程方案提出你的看法（支持/质疑/补充）
2. 指出你认为最重要或遗漏的议题
3. 如果同意某方案，说明理由；如果反对，提出修改建议

注意：保持你的性格一致性。独立性高的代理应该更敢于质疑，谨慎性高的代理应该更关注细节和完整性。

请发表你的观点（100-300字）：""",

    "agenda_synthesize": """你是会议主持人，需要根据各方讨论综合出最终议程。

原始问题：{question}

各方提出的议程方案：
{agenda_summary}

各方讨论意见：
{debate_summary}

请综合各方意见，生成一个最终议程。要求：
1. 整合各方合理的建议
2. 解决讨论中提出的质疑
3. 确保议程完整且有逻辑顺序
4. 每个议题必须包含2-4个具体的子问题（sub_questions）
5. 子问题要具体、可执行、可讨论
6. 通常3-5个议题为宜

输出JSON数组格式：
[
  {{
    "title": "议题标题",
    "description": "议题详细描述",
    "sub_questions": [
      "具体子问题1：需要回答什么？",
      "具体子问题2：需要验证什么？"
    ]
  }}
]

只输出JSON数组：""",

    "agenda_vote": """请为以下议程方案投票。

问题：{question}

{agenda_options}

请选择你认为最合理的议程方案，输出方案编号（1-{total}）。只输出数字。""",

    "agenda_end_vote": """请审议当前议程是否讨论充分，可以结束。

当前议程：{agenda_title}
议程描述：{agenda_description}
讨论概况：{discussion_summary}

如果你认为当前议程已经讨论充分，可以进入下一个议程，请输出JSON：
{{"agree": true, "reason": "原因"}}

如果你认为还需要继续讨论，请输出JSON：
{{"agree": false, "reason": "原因"}}""",

    "summary_review": """你是会议审议员，请审议以下会议总结。

原始问题：{question}

当前总结：
{summary}

讨论原文参考：
{discussion}

审议要求：
1. 总结是否准确反映了讨论内容？
2. 是否有重要观点遗漏？
3. 结论是否清晰明确？

输出JSON格式：
{{"agree": true/false, "reason": "原因", "suggestion": "修改建议（反对时必填）"}}""",

    "summary_revise": """请根据修改建议完善会议总结。

当前总结：
{current_summary}

修改建议：
{suggestions}

讨论原文参考：
{discussion}

要求：
1. 认真考虑每条建议
2. 保持总结结构完整
3. 输出修改后的完整总结

请输出修改后的会议总结：""",

    "user_intervention_adjust": """请根据用户意见调整会议总结。

原始问题：{question}

当前总结：
{current_summary}

用户意见：
{user_input}

讨论原文参考：
{discussion}

要求：
1. 认真考虑用户意见
2. 保留总结的核心结构
3. 合理融入用户的修改方向
4. 输出调整后的完整总结

请输出调整后的会议总结：""",

    # ==================== 思考暂停机制提示词 ====================
    
    "think_request": """你认为当前讨论需要一段静默思考时间。

你的身份：{identity}
请求思考时间：{duration}秒

请说明你想要思考的方向：
1. 你准备深入分析什么问题？
2. 你打算使用哪些工具或资源？
3. 思考后你期望提出什么新的观点？

在发言中输出 [THINK_REQ {duration}] 来请求思考暂停。""",

    "think_private_log": """【思考日志】
代理：{agent_id}
时间：{timestamp}

思考内容：
{content}

思考类型：{think_type}
（此日志仅发起代理和用户可见）""",

    "think_resume": """思考暂停已结束。

你现在拥有优先发言权。请根据你的思考结果发表新的观点。

思考期间你做的分析：
{think_log}

要求：
1. 提出经过深思熟虑的新观点
2. 如果没有实质性新观点，请简短说明
3. 避免重复已有讨论内容

请发言：""",

    "think_priority_remind": """【系统提醒】你当前拥有优先发言权。

这是你思考暂停后获得的特权。请利用这个机会提出你的新观点。""",

    # ==================== 防卡死机制提示词 ====================
    
    "convergence_proposal": """讨论已持续较长时间，系统检测到可能的停滞情况。

停滞原因：{stall_reason}
讨论轮次：{rounds}
空闲时间：{idle_time}秒

当前讨论概况：
{discussion_summary}

请根据以上情况，快速提出一个总结或决策建议。

输出JSON：
{{"proposal": "提案内容", "confidence": 0.0-1.0, "reason": "理由"}}""",

    "force_end_warning": """【系统警告】讨论即将强制结束。

原因：{reason}
已尝试收敛次数：{attempts}

最后一次机会，请快速达成结论。""",

    "duplicate_warning": """【系统提醒】你的发言与之前内容相似度较高。

相似内容来源：{similar_agent}
相似度：{similarity:.1%}

请注意：
1. 避免重复已有观点
2. 提供新的论据或角度
3. 如同意他人观点，请说明新增理由""",

    # ==================== 模式切换提示词 ====================
    
    "mode_switch_notify": """【模式切换】
从 {from_mode} 模式切换到 {to_mode} 模式

原因：{reason}
当前进度：{progress}

请代理们调整发言策略。""",

    "serial_to_conference_trigger": """步骤执行遇到问题，需要临时会议讨论。

步骤：{step_id} - {description}
问题：{issue}
失败次数：{failures}

请代理们快速讨论解决方案。""",

    "conference_to_serial_trigger": """讨论已达成共识，准备执行。

共识内容：{consensus}
执行步骤：{steps}

现在进入串行执行模式。""",

    # ==================== 表决模式提示词 ====================
    
    "interrupt_proposal": """【叫停请求】
代理 {agent_id} 请求暂停讨论并进行表决。

提案：{proposal}

请各位代理在{timeout}秒内投票。
输出格式：[VOTE: support/oppose/abstain: 理由]""",

    "vote_timeout_warning": """【投票提醒】
投票窗口即将关闭，剩余 {remaining} 秒。

尚未投票的代理请尽快投票。""",

    "vote_result": """【投票结果】
提案：{proposal}

支持率：{support_ratio:.1%}
结果：{result}

{vote_details}""",

    # ==================== 用户控制命令提示词 ====================
    
    "cmd_skip_think": """【用户指令】跳过思考暂停
思考暂停已被用户终止。""",

    "cmd_think_disabled": """【用户指令】思考暂停功能已禁用
代理们将无法请求思考暂停。""",

    "cmd_force_convergence": """【用户指令】强制收敛
用户要求尽快结束讨论，请代理们：
1. 总结当前观点
2. 快速达成共识或结论""",

    # ==================== 大规模重复投票提示词 ====================
    
    "repetition_vote_trigger": """【系统通知】检测到大规模重复发言

统计：{duplicate_count}/{total_agents} 个代理存在重复
系统已提取去重后的不同方案供投票选择。

请从以下方案中选择你认为最优的方案：""",

    "repetition_vote_options": """【方案投票】

以下是从讨论中提取的不同方案：

{options}

请选择你认为最优的方案。

输出格式（JSON）：
{{"vote": 方案编号, "reason": "选择理由"}}""",

    "repetition_vote_result": """【投票结果】

最优方案：方案{winning_id}
提出者：{proposer}
得票：{votes}票

方案摘要：{summary}

备选方案：{backup_count}个已存入备选库，可在后续讨论中参考。""",

    "repetition_vote_remind": """【投票提醒】
大规模重复检测触发投票，请尚未投票的代理尽快投票。
投票选项：{options_summary}
投票窗口剩余：{remaining}秒""",

    "extract_proposals": """从以下讨论中提取所有不同的方案/观点（不是重复的）。

讨论内容：
{discussion}

输出JSON数组，每个元素是一个方案的核心内容（50字以内）：
["方案1内容", "方案2内容", ...]

只输出JSON数组。""",

    "proposal_ranking": """对以下方案按优先级排序（1最重要，数字越小越优先）：

{proposal_list}

输出格式：数字排序，如 "2,1,3,4" 表示方案2最重要，其次方案1...""",

    "review_debate": """议题：{question}

方案排序结果：
{proposals_text}

你是否同意这个排序结果？是否需要进一步讨论？
输出：同意 或 需要讨论（并说明原因）""",

    "final_conclusion": """请生成最终结论。

议题：{question}
{proposals_context}

讨论记录：
{discussion}

输出要求：
1. 总结核心结论（100字以内）
2. 列出关键要点（3-5条）

直接输出结论，不要其他内容。""",

    "interrupt_vote": """【全体叫停投票】

代理 {interrupter} 发起全体叫停请求，提议：
{viewpoint}

请投票决定是否同意全体叫停：
- 同意：停止当前讨论，对提议进行表决
- 反对：继续讨论

投票规则：至少需要5个代理参与投票，其中3个以上同意才能通过。

输出JSON格式：
{{"vote": "agree/disagree", "reason": "你的理由"}}"""
}


@dataclass
class PromptsConfig:
    """提示词配置"""
    mode_voting: str = DEFAULT_PROMPTS["mode_voting"]
    conference_discussion: str = DEFAULT_PROMPTS["conference_discussion"]
    conference_voting: str = DEFAULT_PROMPTS["conference_voting"]
    task_extraction: str = DEFAULT_PROMPTS["task_extraction"]
    step_execution: str = DEFAULT_PROMPTS["step_execution"]
    task_decomposition: str = DEFAULT_PROMPTS["task_decomposition"]
    serial_step_execution: str = DEFAULT_PROMPTS["serial_step_execution"]
    temp_meeting: str = DEFAULT_PROMPTS["temp_meeting"]
    vote_prompt: str = DEFAULT_PROMPTS["vote_prompt"]
    ranked_vote_prompt: str = DEFAULT_PROMPTS["ranked_vote_prompt"]
    final_resolution_prompt: str = DEFAULT_PROMPTS["final_resolution_prompt"]
    serial_task_decomposition: str = DEFAULT_PROMPTS["serial_task_decomposition"]
    serial_step_speak: str = DEFAULT_PROMPTS["serial_step_speak"]
    serial_quick_decision: str = DEFAULT_PROMPTS["serial_quick_decision"]
    serial_proposal: str = DEFAULT_PROMPTS["serial_proposal"]
    serial_vote: str = DEFAULT_PROMPTS["serial_vote"]
    serial_feedback: str = DEFAULT_PROMPTS["serial_feedback"]
    serial_revise: str = DEFAULT_PROMPTS["serial_revise"]
    serial_temp_meeting: str = DEFAULT_PROMPTS["serial_temp_meeting"]
    end_check_prompt: str = DEFAULT_PROMPTS["end_check_prompt"]
    agenda_generation: str = DEFAULT_PROMPTS["agenda_generation"]
    agenda_debate: str = DEFAULT_PROMPTS["agenda_debate"]
    agenda_synthesize: str = DEFAULT_PROMPTS["agenda_synthesize"]
    agenda_vote: str = DEFAULT_PROMPTS["agenda_vote"]
    agenda_end_vote: str = DEFAULT_PROMPTS["agenda_end_vote"]
    summary_review: str = DEFAULT_PROMPTS["summary_review"]
    summary_revise: str = DEFAULT_PROMPTS["summary_revise"]
    user_intervention_adjust: str = DEFAULT_PROMPTS["user_intervention_adjust"]
    # 思考暂停相关
    think_request: str = DEFAULT_PROMPTS["think_request"]
    think_private_log: str = DEFAULT_PROMPTS["think_private_log"]
    think_resume: str = DEFAULT_PROMPTS["think_resume"]
    think_priority_remind: str = DEFAULT_PROMPTS["think_priority_remind"]
    # 防卡死机制相关
    convergence_proposal: str = DEFAULT_PROMPTS["convergence_proposal"]
    force_end_warning: str = DEFAULT_PROMPTS["force_end_warning"]
    duplicate_warning: str = DEFAULT_PROMPTS["duplicate_warning"]
    # 模式切换相关
    mode_switch_notify: str = DEFAULT_PROMPTS["mode_switch_notify"]
    serial_to_conference_trigger: str = DEFAULT_PROMPTS["serial_to_conference_trigger"]
    conference_to_serial_trigger: str = DEFAULT_PROMPTS["conference_to_serial_trigger"]
    # 表决模式相关
    interrupt_proposal: str = DEFAULT_PROMPTS["interrupt_proposal"]
    vote_timeout_warning: str = DEFAULT_PROMPTS["vote_timeout_warning"]
    vote_result: str = DEFAULT_PROMPTS["vote_result"]
    # 用户控制命令相关
    cmd_skip_think: str = DEFAULT_PROMPTS["cmd_skip_think"]
    cmd_think_disabled: str = DEFAULT_PROMPTS["cmd_think_disabled"]
    cmd_force_convergence: str = DEFAULT_PROMPTS["cmd_force_convergence"]
    # 大规模重复投票相关
    repetition_vote_trigger: str = DEFAULT_PROMPTS["repetition_vote_trigger"]
    repetition_vote_options: str = DEFAULT_PROMPTS["repetition_vote_options"]
    repetition_vote_result: str = DEFAULT_PROMPTS["repetition_vote_result"]
    repetition_vote_remind: str = DEFAULT_PROMPTS["repetition_vote_remind"]
    # 议程投票和复盘相关
    extract_proposals: str = DEFAULT_PROMPTS["extract_proposals"]
    proposal_ranking: str = DEFAULT_PROMPTS["proposal_ranking"]
    review_debate: str = DEFAULT_PROMPTS["review_debate"]
    final_conclusion: str = DEFAULT_PROMPTS["final_conclusion"]
    interrupt_vote: str = DEFAULT_PROMPTS["interrupt_vote"]


@dataclass
class IntensityConfig:
    """争吵强度配置"""
    weight_complexity: float = 0.15
    weight_divergence: float = 0.25
    weight_time_pressure: float = 0.15
    weight_consensus: float = 0.20
    weight_emotional: float = 0.10
    weight_importance: float = 0.10
    weight_fatigue: float = 0.05
    min_intensity: float = 10.0
    max_intensity: float = 95.0
    smoothing_factor: float = 0.3


@dataclass
class GlobalConfig:
    """全局配置"""
    enable_network_tools: bool = False
    language: LanguageConfig = field(default_factory=LanguageConfig)
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    voting: VotingConfig = field(default_factory=VotingConfig)
    conference: ConferenceConfig = field(default_factory=ConferenceConfig)
    serial: SerialConfig = field(default_factory=SerialConfig)
    temp_meeting: TempMeetingConfig = field(default_factory=TempMeetingConfig)
    debate: DebateConfig = field(default_factory=DebateConfig)
    neutrality: NeutralityConfig = field(default_factory=NeutralityConfig)
    user: UserConfig = field(default_factory=UserConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    oscillation: OscillationConfig = field(default_factory=OscillationConfig)
    intensity: IntensityConfig = field(default_factory=IntensityConfig)
    behaviors: BehaviorConfig = field(default_factory=BehaviorConfig)
    prompts: PromptsConfig = field(default_factory=PromptsConfig)


@dataclass
class SystemConfig:
    """系统总配置"""
    agents: List[AgentConfig]
    global_config: GlobalConfig


def expand_env_vars(value: str) -> str:
    """展开环境变量，支持 ${VAR} 和 $VAR 格式"""
    if not isinstance(value, str):
        return value
    
    # 匹配 ${VAR} 格式
    pattern = r'\$\{([^}]+)\}'
    
    def replace(match):
        var_name = match.group(1)
        return os.environ.get(var_name, match.group(0))
    
    return re.sub(pattern, replace, value)


def expand_env_in_dict(d: Dict) -> Dict:
    """递归展开字典中的环境变量"""
    result = {}
    for key, value in d.items():
        if isinstance(value, str):
            result[key] = expand_env_vars(value)
        elif isinstance(value, dict):
            result[key] = expand_env_in_dict(value)
        elif isinstance(value, list):
            result[key] = [
                expand_env_in_dict(item) if isinstance(item, dict)
                else expand_env_vars(item) if isinstance(item, str)
                else item
                for item in value
            ]
        else:
            result[key] = value
    return result


def load_config(config_path: str) -> SystemConfig:
    """加载配置文件"""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    
    with open(path, 'r', encoding='utf-8') as f:
        raw_config = yaml.safe_load(f)
    
    # 展开环境变量
    config = expand_env_in_dict(raw_config)
    
    # 解析代理配置
    agents = []
    for agent_data in config.get('agents', []):
        api_data = agent_data['api']
        api_config = APIConfig(
            base_url=api_data['base_url'],
            api_key=api_data['api_key'],
            model=api_data['model'],
            reasoning_model=api_data.get('reasoning_model', False),
            reasoning_effort=api_data.get('reasoning_effort', 'medium'),
            supports_tools=api_data.get('supports_tools', True),
            supports_system_message=api_data.get('supports_system_message', True),
            max_tokens=api_data.get('max_tokens', 4096),
            temperature=api_data.get('temperature'),
            top_p=api_data.get('top_p')
        )
        
        personality_data = agent_data.get('personality', {})
        personality = PersonalityConfig(
            cautiousness=personality_data.get('cautiousness', 5),
            empathy=personality_data.get('empathy', 5),
            abstraction=personality_data.get('abstraction', 5),
            independence=personality_data.get('independence', 7),
            default_stance=personality_data.get('default_stance', 'neutral')
        )
        
        agent_config = AgentConfig(
            id=agent_data['id'],
            api=api_config,
            personality=personality,
            allowed_tools=agent_data.get('allowed_tools', []),
            enabled=agent_data.get('enabled', True),
            custom_prompts=agent_data.get('custom_prompts', {}),
            preset_stance=agent_data.get('preset_stance')
        )
        agents.append(agent_config)
    
    # 解析全局配置
    global_data = config.get('global', {})
    
    # 解析语言配置
    language_data = global_data.get('language', {})
    modes_data = language_data.get('modes', {})
    modes_config = ModesNameConfig(
        conference=modes_data.get('conference', '会议'),
        serial=modes_data.get('serial', '串行'),
        debate=modes_data.get('debate', '争吵')
    )
    
    signal_data = language_data.get('signal_keywords', {})
    signal_config = SignalKeywordsConfig(
        heavy=signal_data.get('heavy', [
            "立即停止", "紧急会议", "严重问题", "重大分歧",
            "必须讨论", "无法继续", "critical", "urgent"
        ]),
        medium=signal_data.get('medium', [
            "需要讨论", "不确定", "有问题", "建议开会",
            "需要确认", "不太清楚", "需要协作", "discuss", "uncertain", "not sure"
        ]),
        light=signal_data.get('light', [
            "有点不确定", "可能需要", "稍微有点", "minor", "slight", "possibly"
        ])
    )
    
    cmd_data = language_data.get('commands', {})
    commands_config = CommandMessagesConfig(
        pause=cmd_data.get('pause', '已暂停所有代理发言，使用 /resume 恢复'),
        resume=cmd_data.get('resume', '已恢复讨论'),
        vote=cmd_data.get('vote', '已发起投票: {args}'),
        vote_default=cmd_data.get('vote_default', '已发起投票: 立即表决'),
        stop=cmd_data.get('stop', '已结束会话: {args}'),
        mute=cmd_data.get('mute', '已静音代理: {agent_id}'),
        unmute=cmd_data.get('unmute', '已取消静音代理: {agent_id}'),
        clear=cmd_data.get('clear', '已清空白板，讨论重新开始'),
        mode=cmd_data.get('mode', '已切换到模式: {mode}'),
        force=cmd_data.get('force', '强制打断: {args}'),
        abort_vote=cmd_data.get('abort_vote', '已取消当前投票，恢复讨论'),
        weights=cmd_data.get('weights', '显示代理权重...'),
        history=cmd_data.get('history', '显示插话历史...'),
        readonly_on=cmd_data.get('readonly_on', '已切换到只读模式'),
        readonly_off=cmd_data.get('readonly_off', '已退出只读模式'),
        unknown=cmd_data.get('unknown', '未知命令: {command}'),
        locked=cmd_data.get('locked', '模式已锁定为 {mode}'),
        unlocked=cmd_data.get('unlocked', '已恢复自动切换'),
        hysteresis_set=cmd_data.get('hysteresis_set', '滞后阈值宽度已设置为 {width}'),
        minstay_set=cmd_data.get('minstay_set', '最小停留时间已设置为 {seconds} 秒')
    )
    
    osc_msg_data = language_data.get('oscillation', {})
    oscillation_msg_config = OscillationMessagesConfig(
        cooldown_active=osc_msg_data.get('cooldown_active', '冷却观察期已激活，等待 {seconds} 秒'),
        cooldown_cancelled=osc_msg_data.get('cooldown_cancelled', '冷却观察期已取消: {reason}'),
        switch_rejected=osc_msg_data.get('switch_rejected', '切换请求被拒绝: {reason}'),
        frequent_switch=osc_msg_data.get('frequent_switch', '检测到频繁切换 ({count}次/{window}秒)，滞后阈值宽度从 {old} 增加到 {new}'),
        recommendation=osc_msg_data.get('recommendation', '建议增加滞后阈值宽度或延长最小停留时间'),
        min_stay_not_met=osc_msg_data.get('min_stay_not_met', '停留时间不足，还需等待 {remaining} 秒'),
        hysteresis_not_passed=osc_msg_data.get('hysteresis_not_passed', '{metric} {value} < 触发阈值 {threshold}'),
        signal_recorded=osc_msg_data.get('signal_recorded', '信号已记录: {severity}'),
        signal_triggered=osc_msg_data.get('signal_triggered', '中度信号累积 {count} 次，触发切换'),
        heavy_signal=osc_msg_data.get('heavy_signal', '重度信号，立即切换: {context}')
    )
    
    int_msg_data = language_data.get('interrupt', {})
    interrupt_msg_config = InterruptMessagesConfig(
        cooldown=int_msg_data.get('cooldown', '冷却中，剩余 {remaining} 秒'),
        readonly_mode=int_msg_data.get('readonly_mode', '用户处于只读模式'),
        reputation_low=int_msg_data.get('reputation_low', '用户声誉过低 ({reputation})'),
        force_limit_exceeded=int_msg_data.get('force_limit_exceeded', '超过每小时强制打断限制'),
        voting_restricted=int_msg_data.get('voting_restricted', '投票期间，已转为建议模式'),
        converted_to_suggestion=int_msg_data.get('converted_to_suggestion', '冷却中，已转为建议型')
    )
    
    sys_msg_data = language_data.get('system', {})
    system_msg_config = SystemMessagesConfig(
        all_votes_failed=sys_msg_data.get('all_votes_failed', '[警告] 所有投票失败，默认使用串行模式'),
        mode_vote_result=sys_msg_data.get('mode_vote_result', '模式投票: {mode} (票数: {counts})'),
        consensus_reached=sys_msg_data.get('consensus_reached', '共识已达成，支持率: {rate}'),
        step_complete=sys_msg_data.get('step_complete', '步骤 {step_id} 完成'),
        task_complete=sys_msg_data.get('task_complete', '所有任务已完成')
    )
    
    time_windows_data = language_data.get('time_windows', {})
    time_windows_config = TimeWindowsConfig(
        one_min=time_windows_data.get('1min', '1分钟'),
        five_min=time_windows_data.get('5min', '5分钟'),
        fifteen_min=time_windows_data.get('15min', '15分钟'),
        one_hour=time_windows_data.get('1hour', '1小时')
    )
    
    language_config = LanguageConfig(
        current=language_data.get('current', 'zh-CN'),
        modes=modes_config,
        signal_keywords=signal_config,
        commands=commands_config,
        oscillation=oscillation_msg_config,
        interrupt=interrupt_msg_config,
        system=system_msg_config,
        time_windows=time_windows_config
    )
    
    workspace_data = global_data.get('workspace', {})
    workspace_config = WorkspaceConfig(
        base_dir=workspace_data.get('base_dir', './temp_workspaces'),
        per_session_limit_mb=workspace_data.get('per_session_limit_mb', 100),
        execution_timeout_sec=workspace_data.get('execution_timeout_sec', 30),
        allowed_languages=workspace_data.get('allowed_languages', ['python', 'bash'])
    )
    
    voting_data = global_data.get('voting', {})
    voting_config = VotingConfig(
        agent_count=voting_data.get('agent_count', 3),
        tie_breaker=voting_data.get('tie_breaker', 'hybrid')
    )
    
    conference_data = global_data.get('conference', {})
    
    # 加载重复检测配置
    repeat_data = conference_data.get('repeat', {})
    repeat_config = RepeatConfig(
        single_agent_threshold=repeat_data.get('single_agent_threshold', 3),
        mass_ratio_threshold=repeat_data.get('mass_ratio_threshold', 0.25),
        penalty_sec=repeat_data.get('penalty_sec', 10),
        similarity_threshold=repeat_data.get('similarity_threshold', 0.85)
    )
    
    # 加载叫停投票配置
    interrupt_data = conference_data.get('interrupt', {})
    interrupt_config = InterruptConfig(
        min_participants=interrupt_data.get('min_participants', 5),
        pass_threshold=interrupt_data.get('pass_threshold', 3),
        max_wait_sec=interrupt_data.get('max_wait_sec', 60)
    )
    
    # 加载时间间隔配置
    intervals_data = conference_data.get('intervals', {})
    intervals_config = IntervalConfig(
        think_interval=intervals_data.get('think_interval', 0.5),
        mute_check_interval=intervals_data.get('mute_check_interval', 1),
        idle_check_interval=intervals_data.get('idle_check_interval', 5),
        user_input_check_interval=intervals_data.get('user_input_check_interval', 0.5)
    )
    
    # 加载自动收敛配置
    auto_converge_data = conference_data.get('auto_converge', {})
    auto_converge_config = AutoConvergeConfig(
        max_attempts=auto_converge_data.get('max_attempts', 3)
    )
    
    conference_config = ConferenceConfig(
        max_rounds=conference_data.get('max_rounds', 5),
        consensus_threshold=conference_data.get('consensus_threshold', 0.6),
        discussion_timeout_sec=conference_data.get('discussion_timeout_sec', 300),
        idle_timeout_sec=conference_data.get('idle_timeout_sec', 10),
        max_messages_per_agent=conference_data.get('max_messages_per_agent', 8),
        extract_proposals_timeout_sec=conference_data.get('extract_proposals_timeout_sec', 120),
        serial_execution_keywords=conference_data.get('serial_execution_keywords', [
            "执行", "步骤", "完成", "实现", "创建", "生成", "运行", "计算", "编写"
        ]),
        repeat=repeat_config,
        interrupt=interrupt_config,
        intervals=intervals_config,
        auto_converge=auto_converge_config
    )
    
    serial_data = global_data.get('serial', {})
    serial_config = SerialConfig(
        step_timeout_sec=serial_data.get('step_timeout_sec', 60),
        max_retries=serial_data.get('max_retries', 2),
        trigger_meeting_keywords=serial_data.get('trigger_meeting_keywords', [
            "不确定", "有分歧", "需要讨论", "无法确定", "建议",
            "多种方案", "争议", "需要帮助", "[REQUEST_MEETING]",
            "不确定如何", "需要决策", "无法判断"
        ])
    )
    
    temp_meeting_data = global_data.get('temp_meeting', {})
    temp_meeting_config = TempMeetingConfig(
        max_nesting_depth=temp_meeting_data.get('max_nesting_depth', 2),
        timeout_sec=temp_meeting_data.get('timeout_sec', 60),
        max_agents=temp_meeting_data.get('max_agents', 3)
    )
    
    debate_data = global_data.get('debate', {})
    debate_config = DebateConfig(
        max_duration_sec=debate_data.get('max_duration_sec', 300),
        idle_timeout_sec=debate_data.get('idle_timeout_sec', 15),
        max_events=debate_data.get('max_events', 500),
        consensus_threshold=debate_data.get('consensus_threshold', 3),
        interrupt_threshold=debate_data.get('interrupt_threshold', 15),
        min_speak_duration=debate_data.get('min_speak_duration', 0.5),
        max_speak_tokens=debate_data.get('max_speak_tokens', 30),
        history_contribution_weight=debate_data.get('history_contribution_weight', 2.0)
    )
    
    # 解析中立性配置
    neutrality_data = global_data.get('neutrality', {})
    neutrality_config = NeutralityConfig(
        min_independence=neutrality_data.get('min_independence', 7),
        stance_mode=neutrality_data.get('stance_mode', 'devil_advocate'),
        devil_advocate_count=neutrality_data.get('devil_advocate_count', 2),
        rebalance_threshold=neutrality_data.get('rebalance_threshold', 0.8),
        neutrality_level=neutrality_data.get('neutrality_level', 70),
        user_vote_weight=neutrality_data.get('user_vote_weight', 1.0),
        fact_check_level=neutrality_data.get('fact_check_level', 'normal'),
        enable_neutrality_test=neutrality_data.get('enable_neutrality_test', True)
    )
    
    # 解析用户配置
    user_data = global_data.get('user', {})
    user_config = UserConfig(
        role=user_data.get('role', 'participant'),
        vote_weight=user_data.get('vote_weight', 1.0),
        can_interrupt=user_data.get('can_interrupt', True),
        mark_as_opinion=user_data.get('mark_as_opinion', True)
    )
    
    # 解析提示词配置
    prompts_data = global_data.get('prompts', {})
    prompts_config = PromptsConfig(
        mode_voting=prompts_data.get('mode_voting', DEFAULT_PROMPTS["mode_voting"]),
        conference_discussion=prompts_data.get('conference_discussion', DEFAULT_PROMPTS["conference_discussion"]),
        conference_voting=prompts_data.get('conference_voting', DEFAULT_PROMPTS["conference_voting"]),
        task_extraction=prompts_data.get('task_extraction', DEFAULT_PROMPTS["task_extraction"]),
        step_execution=prompts_data.get('step_execution', DEFAULT_PROMPTS["step_execution"]),
        task_decomposition=prompts_data.get('task_decomposition', DEFAULT_PROMPTS["task_decomposition"]),
        serial_step_execution=prompts_data.get('serial_step_execution', DEFAULT_PROMPTS["serial_step_execution"]),
        temp_meeting=prompts_data.get('temp_meeting', DEFAULT_PROMPTS["temp_meeting"]),
        vote_prompt=prompts_data.get('vote_prompt', DEFAULT_PROMPTS["vote_prompt"]),
        ranked_vote_prompt=prompts_data.get('ranked_vote_prompt', DEFAULT_PROMPTS["ranked_vote_prompt"]),
        final_resolution_prompt=prompts_data.get('final_resolution_prompt', DEFAULT_PROMPTS["final_resolution_prompt"]),
        serial_task_decomposition=prompts_data.get('serial_task_decomposition', DEFAULT_PROMPTS["serial_task_decomposition"]),
        serial_step_speak=prompts_data.get('serial_step_speak', DEFAULT_PROMPTS["serial_step_speak"]),
        serial_quick_decision=prompts_data.get('serial_quick_decision', DEFAULT_PROMPTS["serial_quick_decision"]),
        serial_proposal=prompts_data.get('serial_proposal', DEFAULT_PROMPTS["serial_proposal"]),
        serial_vote=prompts_data.get('serial_vote', DEFAULT_PROMPTS["serial_vote"]),
        serial_feedback=prompts_data.get('serial_feedback', DEFAULT_PROMPTS["serial_feedback"]),
        serial_revise=prompts_data.get('serial_revise', DEFAULT_PROMPTS["serial_revise"]),
        serial_temp_meeting=prompts_data.get('serial_temp_meeting', DEFAULT_PROMPTS["serial_temp_meeting"]),
        end_check_prompt=prompts_data.get('end_check_prompt', DEFAULT_PROMPTS["end_check_prompt"]),
        agenda_generation=prompts_data.get('agenda_generation', DEFAULT_PROMPTS["agenda_generation"]),
        agenda_debate=prompts_data.get('agenda_debate', DEFAULT_PROMPTS["agenda_debate"]),
        agenda_synthesize=prompts_data.get('agenda_synthesize', DEFAULT_PROMPTS["agenda_synthesize"]),
        agenda_vote=prompts_data.get('agenda_vote', DEFAULT_PROMPTS["agenda_vote"]),
        agenda_end_vote=prompts_data.get('agenda_end_vote', DEFAULT_PROMPTS["agenda_end_vote"]),
        summary_review=prompts_data.get('summary_review', DEFAULT_PROMPTS["summary_review"]),
        summary_revise=prompts_data.get('summary_revise', DEFAULT_PROMPTS["summary_revise"]),
        user_intervention_adjust=prompts_data.get('user_intervention_adjust', DEFAULT_PROMPTS["user_intervention_adjust"]),
        # 思考暂停相关
        think_request=prompts_data.get('think_request', DEFAULT_PROMPTS["think_request"]),
        think_private_log=prompts_data.get('think_private_log', DEFAULT_PROMPTS["think_private_log"]),
        think_resume=prompts_data.get('think_resume', DEFAULT_PROMPTS["think_resume"]),
        think_priority_remind=prompts_data.get('think_priority_remind', DEFAULT_PROMPTS["think_priority_remind"]),
        # 防卡死机制相关
        convergence_proposal=prompts_data.get('convergence_proposal', DEFAULT_PROMPTS["convergence_proposal"]),
        force_end_warning=prompts_data.get('force_end_warning', DEFAULT_PROMPTS["force_end_warning"]),
        duplicate_warning=prompts_data.get('duplicate_warning', DEFAULT_PROMPTS["duplicate_warning"]),
        # 模式切换相关
        mode_switch_notify=prompts_data.get('mode_switch_notify', DEFAULT_PROMPTS["mode_switch_notify"]),
        serial_to_conference_trigger=prompts_data.get('serial_to_conference_trigger', DEFAULT_PROMPTS["serial_to_conference_trigger"]),
        conference_to_serial_trigger=prompts_data.get('conference_to_serial_trigger', DEFAULT_PROMPTS["conference_to_serial_trigger"]),
        # 表决模式相关
        interrupt_proposal=prompts_data.get('interrupt_proposal', DEFAULT_PROMPTS["interrupt_proposal"]),
        vote_timeout_warning=prompts_data.get('vote_timeout_warning', DEFAULT_PROMPTS["vote_timeout_warning"]),
        vote_result=prompts_data.get('vote_result', DEFAULT_PROMPTS["vote_result"]),
        # 用户控制命令相关
        cmd_skip_think=prompts_data.get('cmd_skip_think', DEFAULT_PROMPTS["cmd_skip_think"]),
        cmd_think_disabled=prompts_data.get('cmd_think_disabled', DEFAULT_PROMPTS["cmd_think_disabled"]),
        cmd_force_convergence=prompts_data.get('cmd_force_convergence', DEFAULT_PROMPTS["cmd_force_convergence"]),
        # 大规模重复投票相关
        repetition_vote_trigger=prompts_data.get('repetition_vote_trigger', DEFAULT_PROMPTS["repetition_vote_trigger"]),
        repetition_vote_options=prompts_data.get('repetition_vote_options', DEFAULT_PROMPTS["repetition_vote_options"]),
        repetition_vote_result=prompts_data.get('repetition_vote_result', DEFAULT_PROMPTS["repetition_vote_result"]),
        repetition_vote_remind=prompts_data.get('repetition_vote_remind', DEFAULT_PROMPTS["repetition_vote_remind"]),
        # 议程投票和复盘相关
        extract_proposals=prompts_data.get('extract_proposals', DEFAULT_PROMPTS["extract_proposals"]),
        proposal_ranking=prompts_data.get('proposal_ranking', DEFAULT_PROMPTS["proposal_ranking"]),
        review_debate=prompts_data.get('review_debate', DEFAULT_PROMPTS["review_debate"]),
        final_conclusion=prompts_data.get('final_conclusion', DEFAULT_PROMPTS["final_conclusion"]),
        interrupt_vote=prompts_data.get('interrupt_vote', DEFAULT_PROMPTS["interrupt_vote"])
    )
    
    # 解析安全配置
    security_data = global_data.get('security', {})
    rate_limit_data = security_data.get('default_rate_limit', {})
    rate_limit_config = RateLimitConfig(
        max_calls=rate_limit_data.get('max_calls', 10),
        window_seconds=rate_limit_data.get('window_seconds', 60),
        cooldown_seconds=rate_limit_data.get('cooldown_seconds', 60)
    )
    security_config = SecurityConfig(
        level=security_data.get('level', 'standard'),
        execution_mode=security_data.get('execution_mode', 'subprocess'),
        default_rate_limit=rate_limit_config,
        audit_enabled=security_data.get('audit_enabled', True),
        audit_log_path=security_data.get('audit_log_path', './logs/tool_audit.jsonl'),
        anomaly_detection=security_data.get('anomaly_detection', True),
        violation_threshold=security_data.get('violation_threshold', 5),
        auto_disable_duration=security_data.get('auto_disable_duration', 300),
        network_tools_enabled=security_data.get('network_tools_enabled', False),
        network_whitelist=security_data.get('network_whitelist', []),
        max_file_size_mb=security_data.get('max_file_size_mb', 10),
        max_workspace_size_mb=security_data.get('max_workspace_size_mb', 100),
        max_file_count=security_data.get('max_file_count', 1000),
        forbidden_extensions=security_data.get('forbidden_extensions', [
            ".key", ".pem", ".env", ".secret", ".credentials"
        ]),
        code_timeout_seconds=security_data.get('code_timeout_seconds', 30),
        code_max_output_size=security_data.get('code_max_output_size', 1024 * 1024),
        allowed_languages=security_data.get('allowed_languages', ['python', 'bash'])
    )
    
    # 解析防震荡配置
    oscillation_data = global_data.get('oscillation', {})
    hysteresis_data = oscillation_data.get('hysteresis', {})
    hysteresis_config = HysteresisConfig(
        conference_to_serial_trigger=hysteresis_data.get('conference_to_serial_trigger', 0.65),
        conference_to_serial_recover=hysteresis_data.get('conference_to_serial_recover', 0.55),
        serial_to_conference_trigger=hysteresis_data.get('serial_to_conference_trigger', 0.60),
        serial_to_conference_recover=hysteresis_data.get('serial_to_conference_recover', 0.40)
    )
    oscillation_config = OscillationConfig(
        min_stay_time=oscillation_data.get('min_stay_time', 30.0),
        hysteresis=hysteresis_config,
        hysteresis_width=oscillation_data.get('hysteresis_width', 0.10),
        detection_window_size=oscillation_data.get('detection_window_size', 3),
        medium_signal_threshold=oscillation_data.get('medium_signal_threshold', 2),
        consensus_cool_down=oscillation_data.get('consensus_cool_down', 5.0),
        frequent_switch_threshold=oscillation_data.get('frequent_switch_threshold', 3),
        frequent_switch_window=oscillation_data.get('frequent_switch_window', 300.0),
        auto_adjust_increment=oscillation_data.get('auto_adjust_increment', 0.10),
        max_hysteresis_width=oscillation_data.get('max_hysteresis_width', 0.30),
        require_confirmation=oscillation_data.get('require_confirmation', True)
    )
    
    # 解析会议行为配置
    behaviors_data = global_data.get('behaviors', {})
    behaviors_config = BehaviorConfig(
        enable_agenda=behaviors_data.get('enable_agenda', True),
        auto_agenda_from_proposal=behaviors_data.get('auto_agenda_from_proposal', True),
        enable_time_reminder=behaviors_data.get('enable_time_reminder', True),
        time_warning_thresholds=behaviors_data.get('time_warning_thresholds', [0.5, 0.75, 0.9]),
        auto_timeout_signal=behaviors_data.get('auto_timeout_signal', True),
        enable_off_topic_detection=behaviors_data.get('enable_off_topic_detection', True),
        off_topic_similarity_threshold=behaviors_data.get('off_topic_similarity_threshold', 0.3),
        off_topic_penalty=behaviors_data.get('off_topic_penalty', 0.5),
        enable_summary=behaviors_data.get('enable_summary', True),
        summary_interval_rounds=behaviors_data.get('summary_interval_rounds', 5),
        auto_summary=behaviors_data.get('auto_summary', True),
        enable_modify_motion=behaviors_data.get('enable_modify_motion', True),
        enable_fact_check=behaviors_data.get('enable_fact_check', True),
        enable_request_input=behaviors_data.get('enable_request_input', True),
        enable_table_issue=behaviors_data.get('enable_table_issue', True),
        table_threshold=behaviors_data.get('table_threshold', 0.5),
        enable_priority_sort=behaviors_data.get('enable_priority_sort', True),
        enable_compare_options=behaviors_data.get('enable_compare_options', True),
        min_options_for_compare=behaviors_data.get('min_options_for_compare', 2)
    )
    
    # 解析争吵强度配置
    intensity_data = global_data.get('intensity', {})
    intensity_config = IntensityConfig(
        weight_complexity=intensity_data.get('weight_complexity', 0.15),
        weight_divergence=intensity_data.get('weight_divergence', 0.25),
        weight_time_pressure=intensity_data.get('weight_time_pressure', 0.15),
        weight_consensus=intensity_data.get('weight_consensus', 0.20),
        weight_emotional=intensity_data.get('weight_emotional', 0.10),
        weight_importance=intensity_data.get('weight_importance', 0.10),
        weight_fatigue=intensity_data.get('weight_fatigue', 0.05),
        min_intensity=intensity_data.get('min_intensity', 10.0),
        max_intensity=intensity_data.get('max_intensity', 95.0),
        smoothing_factor=intensity_data.get('smoothing_factor', 0.3)
    )
    
    global_config = GlobalConfig(
        enable_network_tools=global_data.get('enable_network_tools', False),
        workspace=workspace_config,
        voting=voting_config,
        conference=conference_config,
        serial=serial_config,
        temp_meeting=temp_meeting_config,
        debate=debate_config,
        neutrality=neutrality_config,
        user=user_config,
        security=security_config,
        oscillation=oscillation_config,
        intensity=intensity_config,
        behaviors=behaviors_config,
        language=language_config,
        prompts=prompts_config
    )
    
    system_config = SystemConfig(agents=agents, global_config=global_config)
    
    # 验证配置
    _validate_config(system_config)
    
    return system_config


def _validate_config(config: SystemConfig):
    """验证配置有效性"""
    if not config.agents:
        raise ValueError("配置文件中没有定义任何代理")
    
    # 检查是否有启用的代理
    enabled = [a for a in config.agents if a.enabled]
    if not enabled:
        raise ValueError("没有启用的代理，请将至少一个代理的 enabled 设为 true")
    
    # 检查 API 配置
    for agent in enabled:
        if not agent.api.api_key or agent.api.api_key == "${OPENAI_API_KEY}":
            # 允许从环境变量获取
            import os
            if not os.environ.get("OPENAI_API_KEY") and not os.environ.get("DEEPSEEK_API_KEY"):
                print(f"[警告] 代理 {agent.id} 的 API 密钥未设置")


def get_enabled_agents(config: SystemConfig) -> List[AgentConfig]:
    """获取所有启用的代理"""
    return [agent for agent in config.agents if agent.enabled]
