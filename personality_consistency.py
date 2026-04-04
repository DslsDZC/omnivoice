"""代理个性一致性保障模块 - 评分、立场追踪、纠正机制"""
import hashlib
import time
import re
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
from collections import deque


class ConsistencyRule(Enum):
    """一致性规则"""
    RISK_MATCH = "risk_match"           # 风险匹配：高谨慎应分析风险
    EMPATHY_MATCH = "empathy_match"     # 共情匹配：低共情不应过度情感化
    ABSTRACTION_MATCH = "abstraction_match"  # 抽象匹配：高抽象应提供宏观视角
    INDEPENDENCE_MATCH = "independence_match"  # 独立性匹配：高独立应质疑
    TEMPORAL_CONSISTENCY = "temporal_consistency"  # 前后一致性


@dataclass
class AgentStance:
    """代理立场记录"""
    topic: str           # 议题关键词
    stance: str          # support/oppose/neutral
    confidence: float    # 置信度
    evidence: str        # 理由/证据
    timestamp: float
    round_num: int


@dataclass
class UtteranceRecord:
    """发言记录"""
    content: str
    timestamp: float
    round_num: int
    personality_hash: str
    detected_stances: List[AgentStance] = field(default_factory=list)


@dataclass
class ConsistencyViolation:
    """一致性违规记录"""
    rule: ConsistencyRule
    score_penalty: int
    reason: str
    timestamp: float
    utterance: str


@dataclass
class PersonalitySnapshot:
    """性格快照"""
    cautiousness: int
    empathy: int
    abstraction: int
    independence: int
    
    def to_hash(self) -> str:
        """生成性格哈希"""
        data = f"{self.cautiousness}-{self.empathy}-{self.abstraction}-{self.independence}"
        return hashlib.md5(data.encode()).hexdigest()[:6]
    
    def to_dict(self) -> Dict:
        return {
            "cautiousness": self.cautiousness,
            "empathy": self.empathy,
            "abstraction": self.abstraction,
            "independence": self.independence
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> "PersonalitySnapshot":
        return cls(
            cautiousness=data.get("cautiousness", 5),
            empathy=data.get("empathy", 5),
            abstraction=data.get("abstraction", 5),
            independence=data.get("independence", 5)
        )


class ConsistencyScorer:
    """一致性评分器"""
    
    # 初始分数
    INITIAL_SCORE = 100
    
    # 阈值
    WARNING_THRESHOLD = 70
    SUSPEND_THRESHOLD = 60
    
    # 扣分规则
    PENALTY_RULES = {
        ConsistencyRule.RISK_MATCH: 5,
        ConsistencyRule.EMPATHY_MATCH: 5,
        ConsistencyRule.ABSTRACTION_MATCH: 5,
        ConsistencyRule.INDEPENDENCE_MATCH: 5,
        ConsistencyRule.TEMPORAL_CONSISTENCY: 10,
    }
    
    def __init__(self):
        # 代理ID -> 一致性分数
        self._scores: Dict[str, int] = {}
        # 代理ID -> 违规记录
        self._violations: Dict[str, List[ConsistencyViolation]] = {}
        # 代理ID -> 是否已暂停
        self._suspended: Dict[str, bool] = {}
    
    def init_agent(self, agent_id: str):
        """初始化代理评分"""
        if agent_id not in self._scores:
            self._scores[agent_id] = self.INITIAL_SCORE
            self._violations[agent_id] = []
            self._suspended[agent_id] = False
    
    def get_score(self, agent_id: str) -> int:
        """获取代理分数"""
        return self._scores.get(agent_id, self.INITIAL_SCORE)
    
    def is_suspended(self, agent_id: str) -> bool:
        """检查代理是否被暂停"""
        return self._suspended.get(agent_id, False)
    
    def record_violation(self, agent_id: str, rule: ConsistencyRule,
                         reason: str, utterance: str) -> int:
        """记录违规并扣分"""
        self.init_agent(agent_id)
        
        penalty = self.PENALTY_RULES.get(rule, 5)
        
        violation = ConsistencyViolation(
            rule=rule,
            score_penalty=penalty,
            reason=reason,
            timestamp=time.time(),
            utterance=utterance[:200]
        )
        
        self._violations[agent_id].append(violation)
        self._scores[agent_id] = max(0, self._scores[agent_id] - penalty)
        
        # 检查是否需要暂停
        if self._scores[agent_id] < self.SUSPEND_THRESHOLD:
            self._suspended[agent_id] = True
        
        return self._scores[agent_id]
    
    def reset_score(self, agent_id: str):
        """重置分数（性格调整后）"""
        self._scores[agent_id] = self.INITIAL_SCORE
        self._violations[agent_id] = []
        self._suspended[agent_id] = False
    
    def get_violations(self, agent_id: str, limit: int = 10) -> List[ConsistencyViolation]:
        """获取违规记录"""
        return self._violations.get(agent_id, [])[-limit:]
    
    def get_status(self, agent_id: str) -> Dict:
        """获取代理状态"""
        return {
            "agent_id": agent_id,
            "score": self.get_score(agent_id),
            "is_suspended": self.is_suspended(agent_id),
            "violation_count": len(self._violations.get(agent_id, [])),
            "status": self._get_status_text(agent_id)
        }
    
    def _get_status_text(self, agent_id: str) -> str:
        score = self.get_score(agent_id)
        if self.is_suspended(agent_id):
            return "已暂停"
        elif score < self.WARNING_THRESHOLD:
            return "警告"
        else:
            return "正常"


class StanceTracker:
    """立场追踪器"""
    
    # 立场关键词模式
    SUPPORT_PATTERNS = [
        r"支持", r"同意", r"赞成", r"认可", r"倾向(于)?选择",
        r"推荐", r"建议(采用|选择)", r"我认为.*好",
        r"support", r"agree", r"recommend"
    ]
    
    OPPOSE_PATTERNS = [
        r"反对", r"不(同意|赞成|认可)", r"不推荐", r"不建议",
        r"拒绝", r"否定", r"拒绝接受",
        r"oppose", r"disagree", r"reject", r"against"
    ]
    
    def __init__(self):
        # 代理ID -> 议题 -> 立场列表
        self._stances: Dict[str, Dict[str, List[AgentStance]]] = {}
        # 代理ID -> 最近发言记录
        self._recent_utterances: Dict[str, deque] = {}
        # 最大保留发言数
        self.max_recent = 5
    
    def init_agent(self, agent_id: str):
        """初始化代理追踪"""
        if agent_id not in self._stances:
            self._stances[agent_id] = {}
        if agent_id not in self._recent_utterances:
            self._recent_utterances[agent_id] = deque(maxlen=self.max_recent)
    
    def record_utterance(self, agent_id: str, content: str,
                         round_num: int, personality_hash: str) -> List[AgentStance]:
        """记录发言并检测立场"""
        self.init_agent(agent_id)
        
        # 检测立场
        detected_stances = self._detect_stances(content, round_num)
        
        # 存储发言记录
        record = UtteranceRecord(
            content=content,
            timestamp=time.time(),
            round_num=round_num,
            personality_hash=personality_hash,
            detected_stances=detected_stances
        )
        self._recent_utterances[agent_id].append(record)
        
        # 存储立场
        for stance in detected_stances:
            if stance.topic not in self._stances[agent_id]:
                self._stances[agent_id][stance.topic] = []
            self._stances[agent_id][stance.topic].append(stance)
        
        return detected_stances
    
    def _detect_stances(self, content: str, round_num: int) -> List[AgentStance]:
        """检测发言中的立场"""
        stances = []
        content_lower = content.lower()
        
        # 检测支持立场
        for pattern in self.SUPPORT_PATTERNS:
            matches = re.finditer(pattern, content_lower)
            for match in matches:
                # 提取议题（简化：取匹配位置前后的词）
                start = max(0, match.start() - 20)
                end = min(len(content), match.end() + 20)
                context = content[start:end]
                
                # 提取议题关键词
                topic = self._extract_topic(context, match.group())
                if topic:
                    stances.append(AgentStance(
                        topic=topic,
                        stance="support",
                        confidence=0.7,
                        evidence=context[:100],
                        timestamp=time.time(),
                        round_num=round_num
                    ))
        
        # 检测反对立场
        for pattern in self.OPPOSE_PATTERNS:
            matches = re.finditer(pattern, content_lower)
            for match in matches:
                start = max(0, match.start() - 20)
                end = min(len(content), match.end() + 20)
                context = content[start:end]
                
                topic = self._extract_topic(context, match.group())
                if topic:
                    stances.append(AgentStance(
                        topic=topic,
                        stance="oppose",
                        confidence=0.7,
                        evidence=context[:100],
                        timestamp=time.time(),
                        round_num=round_num
                    ))
        
        return stances
    
    def _extract_topic(self, context: str, matched: str) -> Optional[str]:
        """从上下文提取议题关键词"""
        # 提取名词性关键词
        words = re.findall(r'[\u4e00-\u9fa5]{2,4}|[a-zA-Z]{3,}', context)
        
        # 过滤停用词和匹配词
        stopwords = {"支持", "同意", "反对", "不", "的", "是", "在", "有", "我", "你", "他"}
        matched_clean = matched.strip()
        
        for word in words:
            if word not in stopwords and word != matched_clean:
                return word
        
        return None
    
    def get_stances(self, agent_id: str, topic: Optional[str] = None) -> List[AgentStance]:
        """获取代理立场"""
        self.init_agent(agent_id)
        
        if topic:
            return self._stances[agent_id].get(topic, [])
        
        # 返回所有立场
        all_stances = []
        for topic_stances in self._stances[agent_id].values():
            all_stances.extend(topic_stances)
        return all_stances
    
    def get_recent_utterances(self, agent_id: str, limit: int = 3) -> List[UtteranceRecord]:
        """获取最近发言"""
        self.init_agent(agent_id)
        return list(self._recent_utterances[agent_id])[-limit:]
    
    def check_stance_conflict(self, agent_id: str, new_stance: AgentStance) -> Optional[AgentStance]:
        """检查立场冲突"""
        self.init_agent(agent_id)
        
        topic_stances = self._stances[agent_id].get(new_stance.topic, [])
        
        for old_stance in topic_stances:
            # 如果新立场与旧立场相反
            if old_stance.stance != new_stance.stance and old_stance.stance != "neutral":
                return old_stance
        
        return None
    
    def get_stance_summary(self, agent_id: str) -> str:
        """获取立场摘要（用于提示词）"""
        stances = self.get_stances(agent_id)
        if not stances:
            return ""
        
        lines = ["你最近的发言立场："]
        for i, s in enumerate(stances[-5:], 1):  # 最近5个
            stance_text = {"support": "支持", "oppose": "反对", "neutral": "中立"}
            lines.append(f"- 第{s.round_num}轮：你{stance_text.get(s.stance, s.stance)}了「{s.topic}」")
        
        lines.append("请确保后续发言与上述立场保持一致，或提供新证据说明立场变化。")
        
        return "\n".join(lines)


class BehaviorAnalyzer:
    """行为分析器 - 检测行为是否符合性格"""
    
    # 风险分析关键词
    RISK_KEYWORDS = ["风险", "危险", "问题", "隐患", "不确定性", "可能失败", "谨慎", "小心", "risk"]
    
    # 情感关键词
    EMOTION_KEYWORDS = ["理解", "关心", "同情", "难过", "开心", "抱歉", "遗憾", "安慰", "心疼"]
    
    # 抽象概念关键词
    ABSTRACT_KEYWORDS = ["总体", "整体", "宏观", "战略", "原则", "模式", "框架", "理念", "长远"]
    
    # 质疑关键词
    CHALLENGE_KEYWORDS = ["但是", "然而", "不过", "质疑", "反例", "问题", "风险", "不确定性", "真的吗", "确定"]
    
    # 同意关键词
    AGREE_KEYWORDS = ["同意", "赞同", "认可", "你说得对", "我同意", "确实", "正是如此"]
    
    def analyze_utterance(self, content: str, personality: PersonalitySnapshot) -> List[Tuple[ConsistencyRule, str]]:
        """分析发言是否符合性格
        
        Returns:
            [(违规规则, 原因), ...]
        """
        violations = []
        content_lower = content.lower()
        
        # 规则1：风险匹配（高谨慎应分析风险）
        if personality.cautiousness >= 7:
            has_risk_analysis = any(kw in content_lower for kw in self.RISK_KEYWORDS)
            if not has_risk_analysis and len(content) > 50:
                # 检查是否在提出建议但没有风险分析
                if any(kw in content_lower for kw in ["建议", "应该", "可以", "推荐", "选择"]):
                    violations.append((
                        ConsistencyRule.RISK_MATCH,
                        "高谨慎代理提出建议时未进行风险分析"
                    ))
        
        # 规则2：共情匹配（低共情不应过度情感化）
        if personality.empathy <= 3:
            emotion_count = sum(1 for kw in self.EMOTION_KEYWORDS if kw in content_lower)
            if emotion_count >= 3:
                violations.append((
                    ConsistencyRule.EMPATHY_MATCH,
                    f"低共情代理使用了过多情感词汇（{emotion_count}个）"
                ))
        
        # 规则3：抽象匹配（高抽象应提供宏观视角）
        if personality.abstraction >= 7:
            has_abstract = any(kw in content_lower for kw in self.ABSTRACT_KEYWORDS)
            if not has_abstract and len(content) > 100:
                # 检查是否只讨论细节
                detail_words = ["具体", "细节", "步骤", "第", "首先", "然后"]
                if any(kw in content_lower for kw in detail_words):
                    violations.append((
                        ConsistencyRule.ABSTRACTION_MATCH,
                        "高抽象代理只讨论细节，未提供宏观视角"
                    ))
        
        # 规则4：独立性匹配（高独立应质疑）
        if personality.independence >= 7:
            has_challenge = any(kw in content_lower for kw in self.CHALLENGE_KEYWORDS)
            has_agree = any(kw in content_lower for kw in self.AGREE_KEYWORDS)
            
            if has_agree and not has_challenge:
                violations.append((
                    ConsistencyRule.INDEPENDENCE_MATCH,
                    "高独立性代理盲目同意，未提出质疑"
                ))
        
        return violations


class PersonalityConsistencyManager:
    """个性一致性管理器 - 总控"""
    
    def __init__(self):
        self.scorer = ConsistencyScorer()
        self.stance_tracker = StanceTracker()
        self.analyzer = BehaviorAnalyzer()
        
        # 性格注册表
        self._personality_registry: Dict[str, PersonalitySnapshot] = {}
        # 临时调整（会话级别）
        self._temp_adjustments: Dict[str, PersonalitySnapshot] = {}
        # 当前轮次
        self._current_round = 0
    
    def register_agent(self, agent_id: str, personality: PersonalitySnapshot):
        """注册代理性格"""
        self._personality_registry[agent_id] = personality
        self.scorer.init_agent(agent_id)
        self.stance_tracker.init_agent(agent_id)
    
    def get_personality(self, agent_id: str) -> Optional[PersonalitySnapshot]:
        """获取代理性格（优先返回临时调整）"""
        if agent_id in self._temp_adjustments:
            return self._temp_adjustments[agent_id]
        return self._personality_registry.get(agent_id)
    
    def get_personality_hash(self, agent_id: str) -> str:
        """获取性格哈希"""
        personality = self.get_personality(agent_id)
        if personality:
            return personality.to_hash()
        return ""
    
    def verify_hash(self, agent_id: str, hash_value: str) -> bool:
        """验证性格哈希"""
        expected = self.get_personality_hash(agent_id)
        return expected == hash_value
    
    def adjust_personality(self, agent_id: str, trait: str, value: int) -> bool:
        """临时调整性格参数
        
        Args:
            agent_id: 代理ID
            trait: cautiousness/empathy/abstraction/independence
            value: 新值 (0-10)
        """
        current = self.get_personality(agent_id)
        if not current:
            return False
        
        # 创建新快照
        new_snapshot = PersonalitySnapshot(
            cautiousness=current.cautiousness,
            empathy=current.empathy,
            abstraction=current.abstraction,
            independence=current.independence
        )
        
        # 调整参数
        if trait == "cautiousness":
            new_snapshot.cautiousness = max(0, min(10, value))
        elif trait == "empathy":
            new_snapshot.empathy = max(0, min(10, value))
        elif trait == "abstraction":
            new_snapshot.abstraction = max(0, min(10, value))
        elif trait == "independence":
            new_snapshot.independence = max(0, min(10, value))
        else:
            return False
        
        self._temp_adjustments[agent_id] = new_snapshot
        
        # 重置评分
        self.scorer.reset_score(agent_id)
        
        return True
    
    def reset_personality(self, agent_id: str) -> bool:
        """重置性格为默认值"""
        if agent_id in self._temp_adjustments:
            del self._temp_adjustments[agent_id]
            self.scorer.reset_score(agent_id)
            return True
        return False
    
    def start_round(self):
        """开始新轮次"""
        self._current_round += 1
    
    def analyze_utterance(self, agent_id: str, content: str,
                          personality_hash: str) -> Dict:
        """分析发言并更新评分
        
        Returns:
            分析结果，包含违规信息和当前分数
        """
        personality = self.get_personality(agent_id)
        if not personality:
            return {"error": "代理未注册"}
        
        # 验证哈希
        hash_valid = self.verify_hash(agent_id, personality_hash)
        
        # 记录发言和立场
        detected_stances = self.stance_tracker.record_utterance(
            agent_id, content, self._current_round, personality_hash
        )
        
        # 检查立场冲突
        stance_conflicts = []
        for stance in detected_stances:
            conflict = self.stance_tracker.check_stance_conflict(agent_id, stance)
            if conflict:
                stance_conflicts.append((conflict, stance))
                # 记录前后不一致违规
                self.scorer.record_violation(
                    agent_id,
                    ConsistencyRule.TEMPORAL_CONSISTENCY,
                    f"立场冲突：第{conflict.round_num}轮{conflict.stance}「{conflict.topic}」，现在{stance.stance}",
                    content
                )
        
        # 分析行为一致性
        violations = self.analyzer.analyze_utterance(content, personality)
        
        # 记录违规
        for rule, reason in violations:
            self.scorer.record_violation(agent_id, rule, reason, content)
        
        return {
            "agent_id": agent_id,
            "hash_valid": hash_valid,
            "current_score": self.scorer.get_score(agent_id),
            "is_suspended": self.scorer.is_suspended(agent_id),
            "new_violations": [{"rule": v[0].value, "reason": v[1]} for v in violations],
            "stance_conflicts": [
                {
                    "old_stance": c[0].stance,
                    "old_topic": c[0].topic,
                    "old_round": c[0].round_num,
                    "new_stance": c[1].stance,
                    "new_topic": c[1].topic
                }
                for c in stance_conflicts
            ],
            "detected_stances": [
                {"topic": s.topic, "stance": s.stance, "round": s.round_num}
                for s in detected_stances
            ]
        }
    
    def get_context_prompt(self, agent_id: str) -> str:
        """获取上下文提示词（历史发言 + 立场摘要）"""
        parts = []
        
        # 最近发言
        recent = self.stance_tracker.get_recent_utterances(agent_id, limit=3)
        if recent:
            parts.append("你最近的发言历史：")
            for i, r in enumerate(recent, 1):
                parts.append(f"- 第{r.round_num}轮：{r.content[:100]}...")
            parts.append("请确保后续发言与上述立场和风格保持一致。")
        
        # 立场摘要
        stance_summary = self.stance_tracker.get_stance_summary(agent_id)
        if stance_summary:
            parts.append("")
            parts.append(stance_summary)
        
        return "\n".join(parts) if parts else ""
    
    def get_correction_prompt(self, agent_id: str) -> Optional[str]:
        """获取纠正提示词（如果需要）"""
        score = self.scorer.get_score(agent_id)
        
        if score < 70:
            violations = self.scorer.get_violations(agent_id, limit=3)
            personality = self.get_personality(agent_id)
            
            lines = [
                "[警告] 你的发言已偏离你的性格参数。",
                f"\n你的性格参数：谨慎度={personality.cautiousness}/10，共情度={personality.empathy}/10，",
                f"抽象度={personality.abstraction}/10，独立性={personality.independence}/10。",
                f"\n当前一致性评分：{score}/100",
                "\n最近的违规记录："
            ]
            
            for v in violations:
                lines.append(f"- [{v.rule.value}] {v.reason}")
            
            lines.append("\n请在下一轮发言中纠正，确保符合你的性格设定。")
            
            return "\n".join(lines)
        
        return None
    
    def get_all_statuses(self) -> List[Dict]:
        """获取所有代理状态"""
        return [
            self.scorer.get_status(agent_id)
            for agent_id in self._personality_registry.keys()
        ]
    
    def get_visible_personalities(self) -> Dict[str, Dict]:
        """获取所有代理性格（用于白板公开）"""
        return {
            agent_id: personality.to_dict()
            for agent_id, personality in self._personality_registry.items()
        }


# 全局实例
_global_manager: Optional[PersonalityConsistencyManager] = None


def get_consistency_manager() -> PersonalityConsistencyManager:
    """获取全局一致性管理器"""
    global _global_manager
    if _global_manager is None:
        _global_manager = PersonalityConsistencyManager()
    return _global_manager
