"""会议行为管理器 - 实现议程管理、时间提醒、离题检测等智能会议行为

所有行为直接服务于决策，通过系统机制或工具调用实现，
与事件总线、白板、发言流控制器和表决机制无缝集成。
"""
import time
import re
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Set, Callable, Any
from enum import Enum
from collections import deque
import logging

logger = logging.getLogger(__name__)


class BehaviorType(Enum):
    """会议行为类型"""
    SET_AGENDA = "set_agenda"           # 设置议程
    TIME_REMINDER = "time_reminder"     # 时间提醒
    OFF_TOPIC = "off_topic"             # 离题拉回
    SUMMARY = "summary"                 # 总结
    MODIFY_MOTION = "modify_motion"     # 修正动议
    FACT_CHECK = "fact_check"           # 请求事实核查
    REQUEST_INPUT = "request_input"     # 请求外部输入
    TABLE_ISSUE = "table_issue"         # 搁置争议
    PRIORITY_SORT = "priority_sort"     # 优先级排序
    COMPARE_OPTIONS = "compare_options" # 方案对比


class BehaviorPriority(Enum):
    """行为优先级"""
    CRITICAL = 100   # 必须立即处理（如时间超限）
    HIGH = 80        # 高优先级（如离题检测）
    NORMAL = 50      # 正常优先级（如总结）
    LOW = 30         # 低优先级（如方案对比）


@dataclass
class AgendaItem:
    """议程项"""
    id: str
    title: str
    description: str
    status: str = "pending"  # pending, discussing, resolved, tabled
    priority: int = 0
    created_at: float = 0.0
    started_at: Optional[float] = None
    resolved_at: Optional[float] = None
    related_proposals: List[str] = field(default_factory=list)


@dataclass
class PendingIssue:
    """搁置的问题"""
    id: str
    content: str
    tabled_at: float
    tabled_by: str
    reason: str
    revisit_after: Optional[float] = None


@dataclass
class TimeState:
    """时间状态"""
    start_time: float = 0.0
    total_timeout: float = 300.0
    warning_thresholds: List[float] = field(default_factory=lambda: [0.5, 0.75, 0.9])
    last_warning_time: float = 0.0
    warnings_sent: int = 0


@dataclass
class SummaryRecord:
    """总结记录"""
    round_number: int
    content: str
    key_points: List[str]
    agreements: List[str]
    disagreements: List[str]
    timestamp: float = 0.0


@dataclass
class ProposalOption:
    """提案选项（用于方案对比）"""
    id: str
    title: str
    description: str
    pros: List[str] = field(default_factory=list)
    cons: List[str] = field(default_factory=list)
    supporter_ids: List[str] = field(default_factory=list)
    score: float = 0.0


@dataclass
class BehaviorEvent:
    """行为事件"""
    behavior_type: BehaviorType
    priority: int
    content: str
    source_id: str
    target_ids: List[str] = field(default_factory=list)
    metadata: Dict = field(default_factory=dict)
    timestamp: float = 0.0


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
    off_topic_penalty: float = 0.5  # 发言权重惩罚
    
    # 总结
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


class ConferenceBehaviorManager:
    """会议行为管理器"""
    
    def __init__(self, config: BehaviorConfig = None, whiteboard=None, event_bus=None):
        self.config = config or BehaviorConfig()
        self.whiteboard = whiteboard
        self.event_bus = event_bus
        
        # 议程管理
        self._agenda: List[AgendaItem] = []
        self._current_agenda_index: int = 0
        
        # 搁置问题
        self._pending_issues: List[PendingIssue] = []
        
        # 时间状态
        self._time_state = TimeState()
        
        # 总结记录
        self._summaries: List[SummaryRecord] = []
        
        # 提案选项
        self._proposal_options: List[ProposalOption] = []
        
        # 行为事件队列
        self._behavior_queue: List[BehaviorEvent] = []
        
        # 离题历史（用于计算累积惩罚）
        self._off_topic_history: Dict[str, int] = {}
        
        # 当前轮次
        self._current_round: int = 0
        
        # 行为处理器
        self._handlers: Dict[BehaviorType, Callable] = {
            BehaviorType.SET_AGENDA: self._handle_set_agenda,
            BehaviorType.TIME_REMINDER: self._handle_time_reminder,
            BehaviorType.OFF_TOPIC: self._handle_off_topic,
            BehaviorType.SUMMARY: self._handle_summary,
            BehaviorType.MODIFY_MOTION: self._handle_modify_motion,
            BehaviorType.FACT_CHECK: self._handle_fact_check,
            BehaviorType.REQUEST_INPUT: self._handle_request_input,
            BehaviorType.TABLE_ISSUE: self._handle_table_issue,
            BehaviorType.PRIORITY_SORT: self._handle_priority_sort,
            BehaviorType.COMPARE_OPTIONS: self._handle_compare_options,
        }
    
    # ========== 初始化与启动 ==========
    
    def start_session(self, timeout: float = 300.0):
        """开始会议会话"""
        self._time_state.start_time = time.time()
        self._time_state.total_timeout = timeout
        self._time_state.warnings_sent = 0
        self._current_round = 0
    
    def set_round(self, round_num: int):
        """设置当前轮次"""
        self._current_round = round_num
        
        # 检查是否需要自动总结
        if (self.config.enable_summary and 
            self.config.auto_summary and 
            round_num > 0 and 
            round_num % self.config.summary_interval_rounds == 0):
            self.queue_behavior(BehaviorEvent(
                behavior_type=BehaviorType.SUMMARY,
                priority=BehaviorPriority.LOW.value,
                content="自动阶段性总结",
                source_id="system",
                timestamp=time.time()
            ))
    
    # ========== 议程管理 ==========
    
    def set_agenda(self, items: List[Dict], source_id: str = "system"):
        """设置议程"""
        if not self.config.enable_agenda:
            return
        
        self._agenda = []
        for i, item in enumerate(items):
            agenda_item = AgendaItem(
                id=f"agenda_{i+1}",
                title=item.get("title", f"议题{i+1}"),
                description=item.get("description", ""),
                priority=item.get("priority", 0),
                created_at=time.time()
            )
            self._agenda.append(agenda_item)
        
        self._current_agenda_index = 0
        
        logger.info(f"议程已设置: {len(self._agenda)} 个议题")
        
        # 同步到白板
        if self.whiteboard:
            self.whiteboard.set_metadata("agenda", [
                {"id": a.id, "title": a.title, "status": a.status}
                for a in self._agenda
            ])
    
    def get_current_agenda(self) -> Optional[AgendaItem]:
        """获取当前议程项"""
        if self._current_agenda_index < len(self._agenda):
            return self._agenda[self._current_agenda_index]
        return None
    
    def advance_agenda(self) -> bool:
        """推进到下一个议程项"""
        current = self.get_current_agenda()
        if current:
            current.status = "resolved"
            current.resolved_at = time.time()
        
        self._current_agenda_index += 1
        
        if self._current_agenda_index < len(self._agenda):
            next_item = self.get_current_agenda()
            if next_item:
                next_item.status = "discussing"
                next_item.started_at = time.time()
            return True
        
        return False  # 所有议程已完成
    
    def agenda_from_proposal(self, proposal: str, proposer_id: str) -> List[AgendaItem]:
        """从提案提取议程项"""
        if not self.config.auto_agenda_from_proposal:
            return []
        
        # 简单提取：按句号或数字分割
        items = []
        
        # 尝试匹配编号列表
        numbered = re.findall(r'(?:^|\n)\s*(\d+)[.、]\s*(.+?)(?=(?:\n\s*\d+[.、])|$)', proposal, re.DOTALL)
        
        if numbered:
            for num, content in numbered:
                items.append({
                    "title": content.strip()[:50],
                    "description": content.strip(),
                    "priority": int(num)
                })
        else:
            # 按句号分割
            sentences = re.split(r'[。！？\n]+', proposal)
            for i, s in enumerate(sentences):
                s = s.strip()
                if len(s) > 10:
                    items.append({
                        "title": s[:50],
                        "description": s,
                        "priority": i + 1
                    })
        
        if items:
            self.set_agenda(items, proposer_id)
        
        return self._agenda
    
    # ========== 时间提醒 ==========
    
    def check_time(self) -> Optional[BehaviorEvent]:
        """检查时间，生成提醒事件"""
        if not self.config.enable_time_reminder:
            return None
        
        # 如果没有设置超时，跳过时间检查
        if self._time_state.total_timeout <= 0:
            return None
        
        elapsed = time.time() - self._time_state.start_time
        ratio = elapsed / self._time_state.total_timeout
        
        # 检查是否达到阈值
        for threshold in self.config.time_warning_thresholds:
            if ratio >= threshold and self._time_state.warnings_sent < len(self.config.time_warning_thresholds):
                remaining = self._time_state.total_timeout - elapsed
                
                event = BehaviorEvent(
                    behavior_type=BehaviorType.TIME_REMINDER,
                    priority=BehaviorPriority.CRITICAL.value,
                    content=f"⏰ 时间提醒：已用 {ratio*100:.0f}%，剩余 {remaining:.0f} 秒",
                    source_id="system",
                    metadata={"remaining_seconds": remaining, "elapsed_ratio": ratio},
                    timestamp=time.time()
                )
                
                self._time_state.warnings_sent += 1
                self._time_state.last_warning_time = time.time()
                
                return event
        
        # 超时自动信号
        if ratio >= 1.0 and self.config.auto_timeout_signal:
            return BehaviorEvent(
                behavior_type=BehaviorType.TIME_REMINDER,
                priority=BehaviorPriority.CRITICAL.value,
                content="⏰ 讨论已超时，建议立即结束或延长",
                source_id="system",
                metadata={"timeout": True},
                timestamp=time.time()
            )
        
        return None
    
    def get_remaining_time(self) -> float:
        """获取剩余时间"""
        elapsed = time.time() - self._time_state.start_time
        return max(0, self._time_state.total_timeout - elapsed)
    
    # ========== 离题检测 ==========
    
    def check_off_topic(self, content: str, agenda_title: str, 
                        speaker_id: str) -> Optional[BehaviorEvent]:
        """检测发言是否离题"""
        if not self.config.enable_off_topic_detection:
            return None
        
        if not agenda_title:
            return None
        
        # 计算语义相似度（简化版：关键词重叠）
        similarity = self._calculate_similarity(content, agenda_title)
        
        if similarity < self.config.off_topic_similarity_threshold:
            # 记录离题历史
            self._off_topic_history[speaker_id] = self._off_topic_history.get(speaker_id, 0) + 1
            
            return BehaviorEvent(
                behavior_type=BehaviorType.OFF_TOPIC,
                priority=BehaviorPriority.HIGH.value,
                content=f"📌 发言可能与当前议题「{agenda_title}」相关性较低 (相似度: {similarity:.0%})",
                source_id="system",
                target_ids=[speaker_id],
                metadata={
                    "similarity": similarity,
                    "speaker_id": speaker_id,
                    "penalty": self.config.off_topic_penalty
                },
                timestamp=time.time()
            )
        
        return None
    
    def _calculate_similarity(self, text1: str, text2: str) -> float:
        """计算文本相似度（简化版：关键词重叠）"""
        # 提取关键词
        def extract_keywords(text: str) -> Set[str]:
            # 移除标点和停用词
            text = re.sub(r'[^\w\s]', ' ', text.lower())
            words = text.split()
            # 过滤短词
            return set(w for w in words if len(w) > 1)
        
        kw1 = extract_keywords(text1)
        kw2 = extract_keywords(text2)
        
        if not kw1 or not kw2:
            return 0.0
        
        # Jaccard 相似度
        intersection = len(kw1 & kw2)
        union = len(kw1 | kw2)
        
        return intersection / union if union > 0 else 0.0
    
    def get_off_topic_penalty(self, speaker_id: str) -> float:
        """获取离题惩罚系数"""
        count = self._off_topic_history.get(speaker_id, 0)
        return max(0.1, 1.0 - count * self.config.off_topic_penalty)
    
    # ========== 总结 ==========
    
    def generate_summary(self, messages: List[Dict]) -> SummaryRecord:
        """生成讨论总结"""
        if not messages:
            return SummaryRecord(
                round_number=self._current_round,
                content="暂无讨论内容",
                key_points=[],
                agreements=[],
                disagreements=[],
                timestamp=time.time()
            )
        
        # 提取关键点
        key_points = self._extract_key_points(messages)
        
        # 识别共识和分歧
        agreements, disagreements = self._identify_agreements_disagreements(messages)
        
        # 生成总结内容
        content = self._compose_summary_content(key_points, agreements, disagreements)
        
        summary = SummaryRecord(
            round_number=self._current_round,
            content=content,
            key_points=key_points,
            agreements=agreements,
            disagreements=disagreements,
            timestamp=time.time()
        )
        
        self._summaries.append(summary)
        
        return summary
    
    def _extract_key_points(self, messages: List[Dict]) -> List[str]:
        """提取关键点"""
        key_points = []
        
        # 查找带有关键词的发言
        keywords = ["关键", "核心", "重点", "首先", "其次", "最后", "结论", "建议"]
        
        for msg in messages[-20:]:  # 最近20条
            content = msg.get("content", "")
            for kw in keywords:
                if kw in content:
                    # 提取包含关键词的句子
                    sentences = re.split(r'[。！？\n]', content)
                    for s in sentences:
                        if kw in s and len(s) > 5:
                            key_points.append(s.strip()[:100])
                    break
        
        return key_points[:5]  # 最多5个关键点
    
    def _identify_agreements_disagreements(self, messages: List[Dict]) -> Tuple[List[str], List[str]]:
        """识别共识和分歧"""
        agreements = []
        disagreements = []
        
        agree_keywords = ["同意", "支持", "认可", "一致", "没问题"]
        disagree_keywords = ["反对", "不同意", "质疑", "问题是", "但是", "然而"]
        
        for msg in messages:
            content = msg.get("content", "")
            
            for kw in agree_keywords:
                if kw in content:
                    agreements.append(f"{msg.get('agent_id', '?')}: {content[:50]}...")
                    break
            
            for kw in disagree_keywords:
                if kw in content:
                    disagreements.append(f"{msg.get('agent_id', '?')}: {content[:50]}...")
                    break
        
        return agreements[:5], disagreements[:5]
    
    def _compose_summary_content(self, key_points: List[str], 
                                  agreements: List[str], 
                                  disagreements: List[str]) -> str:
        """组合总结内容"""
        parts = [f"[第 {self._current_round} 轮讨论总结]"]
        
        if key_points:
            parts.append("\n[关键点]")
            for i, p in enumerate(key_points, 1):
                parts.append(f"  {i}. {p}")
        
        if agreements:
            parts.append("\n[共识]")
            for a in agreements[:3]:
                parts.append(f"  - {a}")
        
        if disagreements:
            parts.append("\n[分歧]")
            for d in disagreements[:3]:
                parts.append(f"  - {d}")
        
        return "\n".join(parts)
    
    # ========== 修正动议 ==========
    
    def process_modify_motion(self, original_proposal: str, 
                               modification: str, 
                               proposer_id: str) -> ProposalOption:
        """处理修正动议"""
        if not self.config.enable_modify_motion:
            return None
        
        # 创建修改后的选项
        option = ProposalOption(
            id=f"modified_{int(time.time())}",
            title=f"修改方案（由 {proposer_id} 提出）",
            description=modification,
            supporter_ids=[proposer_id]
        )
        
        self._proposal_options.append(option)
        
        return option
    
    # ========== 事实核查 ==========
    
    def request_fact_check(self, claim: str, requester_id: str) -> Dict:
        """请求事实核查"""
        if not self.config.enable_fact_check:
            return {"status": "disabled"}
        
        return {
            "status": "requested",
            "claim": claim,
            "requester_id": requester_id,
            "tools": ["calculator", "web_search", "local_search"],
            "timestamp": time.time()
        }
    
    # ========== 请求外部输入 ==========
    
    def request_external_input(self, question: str, 
                                requester_id: str,
                                target: str = "user") -> BehaviorEvent:
        """请求外部输入"""
        if not self.config.enable_request_input:
            return None
        
        target_ids = [target] if target != "all" else []
        
        return BehaviorEvent(
            behavior_type=BehaviorType.REQUEST_INPUT,
            priority=BehaviorPriority.HIGH.value,
            content=f"📥 {requester_id} 请求输入：{question}",
            source_id=requester_id,
            target_ids=target_ids,
            metadata={"question": question, "target": target},
            timestamp=time.time()
        )
    
    # ========== 搁置争议 ==========
    
    def table_issue(self, content: str, proposer_id: str, reason: str) -> PendingIssue:
        """搁置争议问题"""
        if not self.config.enable_table_issue:
            return None
        
        issue = PendingIssue(
            id=f"tabled_{int(time.time())}",
            content=content,
            tabled_at=time.time(),
            tabled_by=proposer_id,
            reason=reason
        )
        
        self._pending_issues.append(issue)
        
        # 同步到白板
        if self.whiteboard:
            pending = self.whiteboard.get_metadata("pending_issues") or []
            pending.append({
                "id": issue.id,
                "content": content[:100],
                "reason": reason
            })
            self.whiteboard.set_metadata("pending_issues", pending)
        
        logger.info(f"问题已搁置: {content[:50]}...")
        
        return issue
    
    def get_pending_issues(self) -> List[PendingIssue]:
        """获取搁置的问题列表"""
        return self._pending_issues
    
    def revisit_issue(self, issue_id: str) -> Optional[PendingIssue]:
        """重新讨论搁置的问题"""
        for issue in self._pending_issues:
            if issue.id == issue_id:
                self._pending_issues.remove(issue)
                return issue
        return None
    
    # ========== 优先级排序 ==========
    
    def sort_by_priority(self, items: List[Dict]) -> List[Dict]:
        """按优先级排序"""
        if not self.config.enable_priority_sort:
            return items
        
        # 根据投票或权重排序
        def get_score(item):
            # 综合评分：优先级 + 支持数 * 0.5 + 紧急度
            priority = item.get("priority", 0)
            supporters = len(item.get("supporter_ids", []))
            urgency = item.get("urgency", 0)
            return priority + supporters * 0.5 + urgency
        
        return sorted(items, key=get_score, reverse=True)
    
    # ========== 方案对比 ==========
    
    def add_proposal_option(self, title: str, description: str, proposer_id: str):
        """添加提案选项"""
        if not self.config.enable_compare_options:
            return
        
        option = ProposalOption(
            id=f"option_{len(self._proposal_options) + 1}",
            title=title,
            description=description,
            supporter_ids=[proposer_id]
        )
        
        self._proposal_options.append(option)
    
    def generate_comparison_table(self) -> str:
        """生成方案对比表"""
        if len(self._proposal_options) < self.config.min_options_for_compare:
            return "方案数量不足，无法对比"
        
        lines = ["[方案对比表]", ""]
        lines.append("| 方案 | 描述 | 优点 | 缺点 | 支持者 |")
        lines.append("|------|------|------|------|--------|")
        
        for opt in self._proposal_options:
            pros = "、".join(opt.pros[:3]) or "-"
            cons = "、".join(opt.cons[:3]) or "-"
            supporters = "、".join(opt.supporter_ids[:3]) or "-"
            
            lines.append(f"| {opt.title[:15]} | {opt.description[:20]}... | {pros} | {cons} | {supporters} |")
        
        return "\n".join(lines)
    
    def get_options_for_voting(self) -> List[Dict]:
        """获取用于投票的选项列表"""
        return [
            {
                "id": opt.id,
                "title": opt.title,
                "description": opt.description,
                "score": opt.score
            }
            for opt in self._proposal_options
        ]
    
    # ========== 行为队列处理 ==========
    
    def queue_behavior(self, event: BehaviorEvent):
        """将行为事件加入队列"""
        self._behavior_queue.append(event)
        # 按优先级排序
        self._behavior_queue.sort(key=lambda e: e.priority, reverse=True)
    
    def get_next_behavior(self) -> Optional[BehaviorEvent]:
        """获取下一个待处理的行为"""
        if self._behavior_queue:
            return self._behavior_queue.pop(0)
        return None
    
    def process_behavior(self, event: BehaviorEvent) -> Any:
        """处理行为事件"""
        handler = self._handlers.get(event.behavior_type)
        if handler:
            return handler(event)
        return None
    
    # ========== 行为处理器 ==========
    
    def _handle_set_agenda(self, event: BehaviorEvent) -> Dict:
        """处理设置议程"""
        items = event.metadata.get("items", [])
        self.set_agenda(items, event.source_id)
        return {"status": "agenda_set", "count": len(self._agenda)}
    
    def _handle_time_reminder(self, event: BehaviorEvent) -> Dict:
        """处理时间提醒"""
        # 发布到事件总线
        if self.event_bus:
            from event_bus import EventType
            self.event_bus.publish(Event(
                event_type=EventType.SYSTEM,
                source_id="time_manager",
                content=event.content,
                metadata=event.metadata
            ))
        return {"status": "reminder_sent", "content": event.content}
    
    def _handle_off_topic(self, event: BehaviorEvent) -> Dict:
        """处理离题检测"""
        speaker_id = event.metadata.get("speaker_id", "")
        penalty = self.get_off_topic_penalty(speaker_id)
        
        # 降权发言
        if self.whiteboard:
            current_weight = self.whiteboard.get_agent_contribution(speaker_id)
            self.whiteboard.set_agent_contribution(speaker_id, current_weight * penalty)
        
        return {"status": "penalty_applied", "speaker_id": speaker_id, "penalty": penalty}
    
    def _handle_summary(self, event: BehaviorEvent) -> Dict:
        """处理总结请求"""
        messages = event.metadata.get("messages", [])
        if self.whiteboard:
            messages = [
                {"agent_id": m.agent_id, "content": m.content}
                for m in self.whiteboard.get_messages()[-30:]
            ]
        
        summary = self.generate_summary(messages)
        
        # 记录到白板
        if self.whiteboard:
            self.whiteboard.add_message(
                agent_id="system",
                content=summary.content,
                message_type="summary"
            )
        
        return {"status": "summary_generated", "summary": summary.content}
    
    def _handle_modify_motion(self, event: BehaviorEvent) -> Dict:
        """处理修正动议"""
        original = event.metadata.get("original_proposal", "")
        modification = event.metadata.get("modification", "")
        
        option = self.process_modify_motion(original, modification, event.source_id)
        
        return {"status": "motion_modified", "option_id": option.id if option else None}
    
    def _handle_fact_check(self, event: BehaviorEvent) -> Dict:
        """处理事实核查"""
        claim = event.metadata.get("claim", "")
        return self.request_fact_check(claim, event.source_id)
    
    def _handle_request_input(self, event: BehaviorEvent) -> Dict:
        """处理请求外部输入"""
        # 暂停讨论，等待输入
        return {"status": "waiting_for_input", "question": event.metadata.get("question", "")}
    
    def _handle_table_issue(self, event: BehaviorEvent) -> Dict:
        """处理搁置争议"""
        content = event.metadata.get("content", "")
        reason = event.metadata.get("reason", "")
        
        issue = self.table_issue(content, event.source_id, reason)
        
        return {"status": "issue_tabled", "issue_id": issue.id if issue else None}
    
    def _handle_priority_sort(self, event: BehaviorEvent) -> Dict:
        """处理优先级排序"""
        items = event.metadata.get("items", [])
        sorted_items = self.sort_by_priority(items)
        
        return {"status": "sorted", "items": sorted_items}
    
    def _handle_compare_options(self, event: BehaviorEvent) -> Dict:
        """处理方案对比"""
        table = self.generate_comparison_table()
        
        if self.whiteboard:
            self.whiteboard.add_message(
                agent_id="system",
                content=table,
                message_type="comparison"
            )
        
        return {"status": "comparison_generated", "table": table}
    
    # ========== 信号解析 ==========
    
    def parse_behavior_signal(self, content: str, speaker_id: str) -> Optional[BehaviorEvent]:
        """从发言内容解析行为信号"""
        # [INTERRUPT] 叫停信号
        if "[INTERRUPT]" in content.upper():
            return None  # 叫停由表决机制处理
        
        # [TIMEOUT] 超时信号
        if "[TIMEOUT]" in content.upper():
            return BehaviorEvent(
                behavior_type=BehaviorType.TIME_REMINDER,
                priority=BehaviorPriority.CRITICAL.value,
                content="代理请求结束讨论",
                source_id=speaker_id,
                timestamp=time.time()
            )
        
        # [OFF_TOPIC] 离题信号
        match = re.search(r'\[OFF_TOPIC\]\s*(.+)', content, re.IGNORECASE)
        if match:
            return BehaviorEvent(
                behavior_type=BehaviorType.OFF_TOPIC,
                priority=BehaviorPriority.HIGH.value,
                content=f"离题提醒: {match.group(1)}",
                source_id=speaker_id,
                timestamp=time.time()
            )
        
        # [SUMMARY] 总结信号
        if "[SUMMARY]" in content.upper():
            return BehaviorEvent(
                behavior_type=BehaviorType.SUMMARY,
                priority=BehaviorPriority.NORMAL.value,
                content="请求生成总结",
                source_id=speaker_id,
                timestamp=time.time()
            )
        
        # [MODIFY] 修正动议
        match = re.search(r'\[MODIFY\]\s*(.+)', content, re.IGNORECASE)
        if match:
            return BehaviorEvent(
                behavior_type=BehaviorType.MODIFY_MOTION,
                priority=BehaviorPriority.NORMAL.value,
                content=f"修正动议: {match.group(1)}",
                source_id=speaker_id,
                metadata={"modification": match.group(1)},
                timestamp=time.time()
            )
        
        # [FACT_CHECK] 事实核查
        match = re.search(r'\[FACT_CHECK\]\s*(.+)', content, re.IGNORECASE)
        if match:
            return BehaviorEvent(
                behavior_type=BehaviorType.FACT_CHECK,
                priority=BehaviorPriority.HIGH.value,
                content=f"请求事实核查: {match.group(1)}",
                source_id=speaker_id,
                metadata={"claim": match.group(1)},
                timestamp=time.time()
            )
        
        # [NEED_INPUT] 请求外部输入
        match = re.search(r'\[NEED_INPUT\]\s*(.+)', content, re.IGNORECASE)
        if match:
            return BehaviorEvent(
                behavior_type=BehaviorType.REQUEST_INPUT,
                priority=BehaviorPriority.HIGH.value,
                content=f"请求输入: {match.group(1)}",
                source_id=speaker_id,
                metadata={"question": match.group(1)},
                timestamp=time.time()
            )
        
        # [TABLE] 搁置争议
        match = re.search(r'\[TABLE\]\s*(.+)', content, re.IGNORECASE)
        if match:
            return BehaviorEvent(
                behavior_type=BehaviorType.TABLE_ISSUE,
                priority=BehaviorPriority.NORMAL.value,
                content=f"搁置争议: {match.group(1)}",
                source_id=speaker_id,
                metadata={"content": match.group(1), "reason": "代理提议搁置"},
                timestamp=time.time()
            )
        
        # [PRIORITY] 优先级排序
        if "[PRIORITY]" in content.upper():
            return BehaviorEvent(
                behavior_type=BehaviorType.PRIORITY_SORT,
                priority=BehaviorPriority.NORMAL.value,
                content="请求优先级排序",
                source_id=speaker_id,
                timestamp=time.time()
            )
        
        # [COMPARE] 方案对比
        if "[COMPARE]" in content.upper():
            return BehaviorEvent(
                behavior_type=BehaviorType.COMPARE_OPTIONS,
                priority=BehaviorPriority.LOW.value,
                content="请求方案对比",
                source_id=speaker_id,
                timestamp=time.time()
            )
        
        return None
    
    # ========== 状态查询 ==========
    
    def get_status(self) -> Dict:
        """获取当前状态"""
        return {
            "agenda": {
                "total": len(self._agenda),
                "current_index": self._current_agenda_index,
                "current_item": self.get_current_agenda().title if self.get_current_agenda() else None
            },
            "time": {
                "elapsed": time.time() - self._time_state.start_time,
                "remaining": self.get_remaining_time(),
                "warnings_sent": self._time_state.warnings_sent
            },
            "pending_issues": len(self._pending_issues),
            "summaries": len(self._summaries),
            "proposal_options": len(self._proposal_options),
            "queued_behaviors": len(self._behavior_queue),
            "off_topic_violations": sum(self._off_topic_history.values())
        }


# 全局实例
_behavior_manager: Optional[ConferenceBehaviorManager] = None


def get_behavior_manager(config: BehaviorConfig = None, 
                         whiteboard=None, 
                         event_bus=None) -> ConferenceBehaviorManager:
    """获取会议行为管理器实例"""
    global _behavior_manager
    if _behavior_manager is None:
        _behavior_manager = ConferenceBehaviorManager(config, whiteboard, event_bus)
    return _behavior_manager
