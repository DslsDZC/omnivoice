"""事实检查层 - 共享事实白板与事实仲裁"""
import time
import re
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum


class FactStatus(Enum):
    """事实状态"""
    VERIFIED = "verified"       # 已验证
    DISPUTED = "disputed"       # 有争议
    UNVERIFIED = "unverified"   # 未验证
    USER_OPINION = "user_opinion"  # 用户观点（非事实）


class FactCategory(Enum):
    """事实类别"""
    DATA = "data"               # 数据/统计
    DEFINITION = "definition"   # 定义/概念
    EVENT = "event"             # 事件/历史
    CALCULATION = "calculation" # 计算结果
    EXTERNAL = "external"       # 外部来源（搜索/API）
    USER_INPUT = "user_input"   # 用户输入


@dataclass
class FactItem:
    """事实条目"""
    id: str
    content: str
    category: FactCategory
    status: FactStatus
    source: str  # 来源：工具名/代理ID/用户
    timestamp: float
    verified_by: Optional[str] = None  # 验证者
    confidence: float = 1.0
    metadata: Dict = field(default_factory=dict)  # 额外元数据


@dataclass
class ConflictRecord:
    """冲突记录"""
    fact_id: str
    conflicting_claim: str
    claimed_by: str
    timestamp: float
    resolution: Optional[str] = None
    resolved: bool = False


@dataclass
class UserOpinion:
    """用户观点记录"""
    content: str
    timestamp: float
    marked_as_opinion: bool = True
    conflicts_with_facts: List[str] = field(default_factory=list)


class FactBoard:
    """事实白板 - 存储已验证的客观事实"""
    
    def __init__(self):
        self._facts: Dict[str, FactItem] = {}
        self._user_opinions: List[UserOpinion] = []
        self._conflicts: List[ConflictRecord] = []
        self._fact_counter = 0
    
    def add_fact(self, content: str, category: FactCategory,
                 source: str, status: FactStatus = FactStatus.UNVERIFIED,
                 confidence: float = 1.0,
                 metadata: Optional[Dict] = None) -> FactItem:
        """添加事实条目"""
        self._fact_counter += 1
        fact_id = f"fact_{self._fact_counter}"
        
        fact = FactItem(
            id=fact_id,
            content=content,
            category=category,
            status=status,
            source=source,
            timestamp=time.time(),
            confidence=confidence,
            metadata=metadata or {}
        )
        
        self._facts[fact_id] = fact
        return fact
    
    def add_verified_fact(self, content: str, source: str,
                          category: FactCategory = FactCategory.DATA,
                          verified_by: str = "system") -> FactItem:
        """添加已验证的事实"""
        return self.add_fact(
            content=content,
            category=category,
            source=source,
            status=FactStatus.VERIFIED,
            verified_by=verified_by,
            confidence=1.0
        )
    
    def add_user_opinion(self, content: str, 
                         mark_as_opinion: bool = True) -> UserOpinion:
        """添加用户观点"""
        opinion = UserOpinion(
            content=content,
            timestamp=time.time(),
            marked_as_opinion=mark_as_opinion
        )
        
        # 检查是否与已知事实冲突
        conflicts = self._check_fact_conflicts(content)
        opinion.conflicts_with_facts = conflicts
        
        self._user_opinions.append(opinion)
        return opinion
    
    def _check_fact_conflicts(self, content: str) -> List[str]:
        """检查内容是否与已知事实冲突"""
        conflicts = []
        
        for fact_id, fact in self._facts.items():
            if fact.status == FactStatus.VERIFIED:
                # 简单的关键词冲突检测
                # 实际实现可以使用更复杂的 NLP
                if self._is_contradictory(content, fact.content):
                    conflicts.append(fact_id)
        
        return conflicts
    
    def _is_contradictory(self, claim1: str, claim2: str) -> bool:
        """检测两个陈述是否矛盾（简化版）"""
        # 关键的矛盾指示词
        contradiction_patterns = [
            (r'不是', r'是'),
            (r'不', r'是'),
            (r'错误', r'正确'),
            (r'假', r'真'),
            (r'没有', r'有'),
            (r'不能', r'能'),
            (r'不会', r'会'),
            (r'不可能', r'可能'),
        ]
        
        claim1_lower = claim1.lower()
        claim2_lower = claim2.lower()
        
        for neg, pos in contradiction_patterns:
            # 简化检测：一个包含否定，一个包含肯定
            if (neg in claim1_lower and pos in claim2_lower) or \
               (pos in claim1_lower and neg in claim2_lower):
                # 检查是否涉及相同主题（简化：共享关键词）
                words1 = set(re.findall(r'\w+', claim1_lower))
                words2 = set(re.findall(r'\w+', claim2_lower))
                common = words1 & words2
                if len(common) >= 2:  # 至少有2个共同词
                    return True
        
        return False
    
    def report_conflict(self, fact_id: str, conflicting_claim: str,
                        claimed_by: str) -> ConflictRecord:
        """报告事实冲突"""
        conflict = ConflictRecord(
            fact_id=fact_id,
            conflicting_claim=conflicting_claim,
            claimed_by=claimed_by,
            timestamp=time.time()
        )
        self._conflicts.append(conflict)
        return conflict
    
    def resolve_conflict(self, conflict_idx: int, resolution: str):
        """解决冲突"""
        if 0 <= conflict_idx < len(self._conflicts):
            self._conflicts[conflict_idx].resolution = resolution
            self._conflicts[conflict_idx].resolved = True
    
    def get_fact(self, fact_id: str) -> Optional[FactItem]:
        """获取事实"""
        return self._facts.get(fact_id)
    
    def get_verified_facts(self) -> List[FactItem]:
        """获取所有已验证的事实"""
        return [f for f in self._facts.values() if f.status == FactStatus.VERIFIED]
    
    def get_disputed_facts(self) -> List[FactItem]:
        """获取有争议的事实"""
        return [f for f in self._facts.values() if f.status == FactStatus.DISPUTED]
    
    def get_user_opinions(self) -> List[UserOpinion]:
        """获取所有用户观点"""
        return list(self._user_opinions)
    
    def get_conflicts(self, unresolved_only: bool = True) -> List[ConflictRecord]:
        """获取冲突列表"""
        if unresolved_only:
            return [c for c in self._conflicts if not c.resolved]
        return list(self._conflicts)
    
    def verify_fact(self, fact_id: str, verified_by: str):
        """验证事实"""
        if fact_id in self._facts:
            self._facts[fact_id].status = FactStatus.VERIFIED
            self._facts[fact_id].verified_by = verified_by
    
    def dispute_fact(self, fact_id: str):
        """标记事实为有争议"""
        if fact_id in self._facts:
            self._facts[fact_id].status = FactStatus.DISPUTED
    
    def get_context_for_agent(self, include_user_opinions: bool = True) -> str:
        """为代理生成事实上下文"""
        lines = ["=== 已验证事实 ==="]
        
        verified = self.get_verified_facts()
        if verified:
            for fact in verified:
                lines.append(f"- [{fact.category.value}] {fact.content}")
        else:
            lines.append("（暂无已验证事实）")
        
        if include_user_opinions and self._user_opinions:
            lines.append("\n=== 用户观点（仅供参考，非客观事实）===")
            for opinion in self._user_opinions:
                conflict_note = ""
                if opinion.conflicts_with_facts:
                    conflict_note = " [警告:与事实冲突]"
                lines.append(f"- {opinion.content}{conflict_note}")
        
        return "\n".join(lines)
    
    def get_summary(self) -> Dict:
        """获取事实白板摘要"""
        return {
            "total_facts": len(self._facts),
            "verified": len(self.get_verified_facts()),
            "disputed": len(self.get_disputed_facts()),
            "user_opinions": len(self._user_opinions),
            "conflicts": len(self._conflicts),
            "unresolved_conflicts": len(self.get_conflicts(True))
        }
    
    def clear(self):
        """清空事实白板"""
        self._facts.clear()
        self._user_opinions.clear()
        self._conflicts.clear()
        self._fact_counter = 0


class FactChecker:
    """事实检查器 - 验证代理发言与事实的一致性"""
    
    def __init__(self, fact_board: FactBoard, strictness: str = "normal"):
        self.fact_board = fact_board
        self.strictness = strictness  # loose/normal/strict
    
    def check_statement(self, statement: str, 
                        speaker_id: str) -> Tuple[bool, List[str]]:
        """检查陈述是否与已知事实冲突
        
        Returns:
            (是否通过, 冲突的事实ID列表)
        """
        conflicts = self.fact_board._check_fact_conflicts(statement)
        
        if conflicts:
            # 记录冲突
            for fact_id in conflicts:
                self.fact_board.report_conflict(
                    fact_id=fact_id,
                    conflicting_claim=statement[:200],
                    claimed_by=speaker_id
                )
        
        # 根据严格程度决定是否通过
        if self.strictness == "strict":
            return len(conflicts) == 0, conflicts
        elif self.strictness == "normal":
            # 允许冲突，但记录警告
            return True, conflicts
        else:  # loose
            return True, []
    
    def get_conflict_warning(self, conflicts: List[str]) -> str:
        """获取冲突警告消息"""
        if not conflicts:
            return ""
        
        facts = [self.fact_board.get_fact(fid) for fid in conflicts]
        facts = [f for f in facts if f]
        
        if not facts:
            return ""
        
        lines = ["[警告] 你的陈述可能与以下已验证事实冲突："]
        for fact in facts[:3]:  # 最多显示3个
            lines.append(f"  - {fact.content}")
        
        lines.append("\n请基于事实回应，不要盲目同意与事实冲突的观点。")
        
        return "\n".join(lines)


class FactArbiter:
    """事实仲裁者 - 解决事实争议"""
    
    def __init__(self, fact_board: FactBoard, tool_router=None):
        self.fact_board = fact_board
        self.tool_router = tool_router  # 用于调用验证工具
    
    async def verify_with_tools(self, claim: str, 
                                 tools: List[str] = None) -> Optional[FactItem]:
        """使用工具验证声明
        
        Args:
            claim: 待验证的声明
            tools: 可用的验证工具列表（如 search, calculate）
        """
        tools = tools or ["search", "calculate"]
        
        # 简化实现：记录为需要验证
        # 实际实现中会调用工具进行验证
        fact = self.fact_board.add_fact(
            content=claim,
            category=FactCategory.EXTERNAL,
            source="arbiter",
            status=FactStatus.UNVERIFIED,
            confidence=0.5,
            metadata={"pending_verification": True, "tools": tools}
        )
        
        return fact
    
    async def resolve_dispute(self, fact_id: str, 
                              agent_opinions: Dict[str, str]) -> str:
        """解决事实争议
        
        Args:
            fact_id: 有争议的事实ID
            agent_opinions: 代理ID -> 意见的映射
        
        Returns:
            仲裁结果
        """
        fact = self.fact_board.get_fact(fact_id)
        if not fact:
            return "事实不存在"
        
        # 统计意见
        support_count = sum(1 for op in agent_opinions.values() if "同意" in op or "正确" in op)
        oppose_count = sum(1 for op in agent_opinions.values() if "反对" in op or "错误" in op)
        
        if support_count > oppose_count * 2:
            # 压倒性支持，标记为已验证
            self.fact_board.verify_fact(fact_id, "arbiter")
            return f"仲裁结果：声明已验证为真。支持方 {support_count}，反对方 {oppose_count}"
        elif oppose_count > support_count * 2:
            # 压倒性反对，标记为有争议
            self.fact_board.dispute_fact(fact_id)
            return f"仲裁结果：声明存在争议。支持方 {support_count}，反对方 {oppose_count}"
        else:
            # 分歧较大，保持未验证状态
            return f"仲裁结果：无法确定。需要更多证据。支持方 {support_count}，反对方 {oppose_count}"
    
    def get_arbiter_prompt(self) -> str:
        """获取仲裁者提示词"""
        return """你是事实仲裁者。你的职责是：
1. 当代理之间出现事实争议时，验证事实真伪
2. 使用可用工具（搜索、计算）获取客观数据
3. 你的输出不可被反驳，除非用户手动更正
4. 保持中立，只关注事实，不参与观点辩论"""


def create_fact_system(tool_router=None, 
                       strictness: str = "normal") -> Tuple[FactBoard, FactChecker, FactArbiter]:
    """创建完整的事实检查系统"""
    fact_board = FactBoard()
    fact_checker = FactChecker(fact_board, strictness)
    fact_arbiter = FactArbiter(fact_board, tool_router)
    
    return fact_board, fact_checker, fact_arbiter
