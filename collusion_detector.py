"""串通检测器 - 防作弊系统"""
import time
import math
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Any
from enum import Enum
from collections import defaultdict
import logging

logger = logging.getLogger(__name__)


class CollusionType(Enum):
    """串通类型"""
    VOTING_ALLIANCE = "voting_alliance"       # 投票联盟
    MUTUAL_CITATION = "mutual_citation"        # 互相引用刷分
    PROPOSAL_VOTE_LOOP = "proposal_vote_loop"  # 提案-投票循环
    COORDINATED_SPEECH = "coordinated_speech"  # 协同发言


class CollusionSeverity(Enum):
    """串通严重程度"""
    LOW = "low"          # 可疑
    MEDIUM = "medium"    # 疑似
    HIGH = "high"        # 确认
    CRITICAL = "critical"  # 严重


@dataclass
class CollusionCase:
    """串通案件"""
    case_id: str
    collusion_type: CollusionType
    involved_agents: List[str]
    severity: CollusionSeverity
    evidence: Dict[str, Any]
    detected_at: float
    resolved: bool = False
    action_taken: str = ""
    
    def to_dict(self) -> Dict:
        return {
            "case_id": self.case_id,
            "type": self.collusion_type.value,
            "involved_agents": self.involved_agents,
            "severity": self.severity.value,
            "evidence": self.evidence,
            "detected_at": datetime.fromtimestamp(self.detected_at).isoformat(),
            "resolved": self.resolved,
            "action_taken": self.action_taken
        }


@dataclass
class VotingPattern:
    """投票模式"""
    agent_id: str
    vote_history: List[Tuple[str, str]]  # (session_id, vote_type)
    
    def similarity_with(self, other: "VotingPattern") -> float:
        """计算与另一个投票模式的相似度"""
        if not self.vote_history or not other.vote_history:
            return 0.0
        
        # 找出共同的投票会话
        self_sessions = {s[0]: s[1] for s in self.vote_history}
        other_sessions = {s[0]: s[1] for s in other.vote_history}
        
        common_sessions = set(self_sessions.keys()) & set(other_sessions.keys())
        
        if not common_sessions:
            return 0.0
        
        # 计算一致率
        matches = sum(
            1 for s in common_sessions
            if self_sessions[s] == other_sessions[s]
        )
        
        return matches / len(common_sessions)


@dataclass
class CitationPattern:
    """引用模式"""
    agent_id: str
    citations_made: Dict[str, int] = field(default_factory=dict)  # cited_agent -> count
    citations_received: Dict[str, int] = field(default_factory=dict)  # from_agent -> count
    
    def mutual_citation_ratio(self, other_agent: str) -> float:
        """计算与另一个代理的互相引用率"""
        made_to_other = self.citations_made.get(other_agent, 0)
        received_from_other = self.citations_received.get(other_agent, 0)
        
        total_made = sum(self.citations_made.values())
        total_received = sum(self.citations_received.values())
        
        if total_made == 0 or total_received == 0:
            return 0.0
        
        # 双向引用比例
        return (made_to_other / total_made + received_from_other / total_received) / 2


@dataclass
class CollusionConfig:
    """串通检测配置"""
    # 投票联盟检测
    voting_similarity_threshold: float = 0.9    # 投票相似度阈值
    min_common_votes: int = 5                   # 最少共同投票次数
    alliance_weight_penalty: float = 0.5        # 联盟惩罚系数
    
    # 互相引用检测
    mutual_citation_threshold: float = 0.5      # 互相引用率阈值
    min_total_citations: int = 10               # 最少总引用次数
    freeze_hours: int = 24                      # 冻结时长
    
    # 提案-投票循环检测
    proposal_vote_threshold: float = 0.8        # 提案-支持率阈值
    min_proposals: int = 3                      # 最少提案次数
    
    # 协同发言检测
    speech_similarity_threshold: float = 0.85   # 发言相似度阈值
    min_speeches: int = 5                       # 最少发言次数
    
    # 审计
    audit_sample_rate: float = 0.1              # 随机审计采样率
    external_model_check: bool = False          # 是否使用外部模型校验


class CollusionDetector:
    """串通检测器"""
    
    def __init__(self, config: CollusionConfig = None):
        self.config = config or CollusionConfig()
        
        # 投票模式记录
        self._voting_patterns: Dict[str, VotingPattern] = {}
        
        # 引用模式记录
        self._citation_patterns: Dict[str, CitationPattern] = {}
        
        # 提案-投票记录
        self._proposal_votes: Dict[str, List[Tuple[str, str]]] = defaultdict(list)  # proposer -> [(voter, vote)]
        
        # 发言记录（用于协同检测）
        self._speech_records: Dict[str, List[str]] = defaultdict(list)  # agent_id -> [speech_hashes]
        
        # 检测到的案件
        self._cases: List[CollusionCase] = []
        
        # 已标记的代理对
        self._flagged_pairs: Set[Tuple[str, str]] = set()
        
        # 惩罚状态
        self._penalties: Dict[str, Dict] = {}  # agent_id -> penalty_info
        
        # 案件计数器
        self._case_counter = 0
    
    def record_vote(self, session_id: str, agent_id: str, vote_type: str):
        """记录投票"""
        if agent_id not in self._voting_patterns:
            self._voting_patterns[agent_id] = VotingPattern(
                agent_id=agent_id,
                vote_history=[]
            )
        
        self._voting_patterns[agent_id].vote_history.append((session_id, vote_type))
    
    def record_citation(self, citing_agent: str, cited_agent: str):
        """记录引用"""
        # 记录引用者发出的引用
        if citing_agent not in self._citation_patterns:
            self._citation_patterns[citing_agent] = CitationPattern(agent_id=citing_agent)
        self._citation_patterns[citing_agent].citations_made[cited_agent] = \
            self._citation_patterns[citing_agent].citations_made.get(cited_agent, 0) + 1
        
        # 记录被引用者收到的引用
        if cited_agent not in self._citation_patterns:
            self._citation_patterns[cited_agent] = CitationPattern(agent_id=cited_agent)
        self._citation_patterns[cited_agent].citations_received[citing_agent] = \
            self._citation_patterns[cited_agent].citations_received.get(citing_agent, 0) + 1
    
    def record_proposal_vote(self, proposer: str, voter: str, vote_type: str):
        """记录提案投票关系"""
        self._proposal_votes[proposer].append((voter, vote_type))
    
    def record_speech(self, agent_id: str, content: str):
        """记录发言（用于协同检测）"""
        # 使用内容哈希简化存储
        content_hash = hash(content[:100])  # 取前100字符
        self._speech_records[agent_id].append(content_hash)
    
    def detect_voting_alliance(self) -> List[CollusionCase]:
        """
        检测投票联盟
        
        Returns:
            检测到的案件列表
        """
        cases = []
        agents = list(self._voting_patterns.keys())
        
        for i, agent_a in enumerate(agents):
            for agent_b in agents[i+1:]:
                pattern_a = self._voting_patterns[agent_a]
                pattern_b = self._voting_patterns[agent_b]
                
                # 检查共同投票次数
                sessions_a = {s[0] for s in pattern_a.vote_history}
                sessions_b = {s[0] for s in pattern_b.vote_history}
                common_count = len(sessions_a & sessions_b)
                
                if common_count < self.config.min_common_votes:
                    continue
                
                # 计算相似度
                similarity = pattern_a.similarity_with(pattern_b)
                
                if similarity >= self.config.voting_similarity_threshold:
                    case = self._create_case(
                        CollusionType.VOTING_ALLIANCE,
                        [agent_a, agent_b],
                        CollusionSeverity.HIGH if similarity > 0.95 else CollusionSeverity.MEDIUM,
                        {
                            "similarity": round(similarity, 3),
                            "common_votes": common_count,
                            "agent_a_votes": len(pattern_a.vote_history),
                            "agent_b_votes": len(pattern_b.vote_history)
                        }
                    )
                    cases.append(case)
                    
                    self._flagged_pairs.add((agent_a, agent_b))
                    
                    logger.warning(
                        f"检测到投票联盟: {agent_a} <-> {agent_b}, "
                        f"相似度: {similarity:.1%}"
                    )
        
        self._cases.extend(cases)
        return cases
    
    def detect_mutual_citation(self) -> List[CollusionCase]:
        """
        检测互相引用刷分
        
        Returns:
            检测到的案件列表
        """
        cases = []
        
        for agent_id, pattern in self._citation_patterns.items():
            for other_agent, ratio in pattern.citations_made.items():
                if other_agent not in self._citation_patterns:
                    continue
                
                other_pattern = self._citation_patterns[other_agent]
                
                # 检查双向引用
                mutual_ratio = pattern.mutual_citation_ratio(other_agent)
                
                if mutual_ratio >= self.config.mutual_citation_threshold:
                    total_citations = (
                        sum(pattern.citations_made.values()) +
                        sum(other_pattern.citations_made.values())
                    )
                    
                    if total_citations >= self.config.min_total_citations:
                        case = self._create_case(
                            CollusionType.MUTUAL_CITATION,
                            [agent_id, other_agent],
                            CollusionSeverity.HIGH,
                            {
                                "mutual_ratio": round(mutual_ratio, 3),
                                "total_citations": total_citations,
                                "a_to_b": pattern.citations_made.get(other_agent, 0),
                                "b_to_a": other_pattern.citations_made.get(agent_id, 0)
                            }
                        )
                        cases.append(case)
                        
                        self._flagged_pairs.add((agent_id, other_agent))
                        
                        logger.warning(
                            f"检测到互相引用刷分: {agent_id} <-> {other_agent}, "
                            f"互相引用率: {mutual_ratio:.1%}"
                        )
        
        self._cases.extend(cases)
        return cases
    
    def detect_proposal_vote_loop(self) -> List[CollusionCase]:
        """
        检测提案-投票循环
        
        Returns:
            检测到的案件列表
        """
        cases = []
        
        for proposer, votes in self._proposal_votes.items():
            if len(votes) < self.config.min_proposals:
                continue
            
            # 统计每个投票者对提案者的支持率
            voter_support: Dict[str, Tuple[int, int]] = defaultdict(lambda: (0, 0))
            
            for voter, vote_type in votes:
                support, total = voter_support[voter]
                total += 1
                if vote_type == "support":
                    support += 1
                voter_support[voter] = (support, total)
            
            # 检测异常高支持率
            for voter, (support, total) in voter_support.items():
                if voter == proposer:
                    continue
                
                support_rate = support / total if total > 0 else 0
                
                if support_rate >= self.config.proposal_vote_threshold and total >= self.config.min_proposals:
                    case = self._create_case(
                        CollusionType.PROPOSAL_VOTE_LOOP,
                        [proposer, voter],
                        CollusionSeverity.MEDIUM,
                        {
                            "support_rate": round(support_rate, 3),
                            "total_votes": total,
                            "support_votes": support
                        }
                    )
                    cases.append(case)
                    
                    logger.warning(
                        f"检测到提案-投票循环: {proposer} -> {voter}, "
                        f"支持率: {support_rate:.1%}"
                    )
        
        self._cases.extend(cases)
        return cases
    
    def run_full_detection(self) -> Dict[str, List[CollusionCase]]:
        """
        运行完整的串通检测
        
        Returns:
            各类串通的检测结果
        """
        return {
            "voting_alliance": self.detect_voting_alliance(),
            "mutual_citation": self.detect_mutual_citation(),
            "proposal_vote_loop": self.detect_proposal_vote_loop()
        }
    
    def get_penalty(self, agent_id: str) -> Optional[Dict]:
        """获取代理的惩罚状态"""
        return self._penalties.get(agent_id)
    
    def apply_penalties(self, cases: List[CollusionCase], contribution_scorer=None):
        """
        应用惩罚
        
        Args:
            cases: 案件列表
            contribution_scorer: 贡献分计算器
        """
        for case in cases:
            for agent_id in case.involved_agents:
                penalty_factor = self._calculate_penalty_factor(case.severity)
                
                self._penalties[agent_id] = {
                    "case_id": case.case_id,
                    "penalty_factor": penalty_factor,
                    "applied_at": time.time(),
                    "reason": case.collusion_type.value
                }
                
                # 应用到贡献分系统
                if contribution_scorer:
                    contribution_scorer.apply_collusion_penalty([agent_id], penalty_factor)
                
                case.resolved = True
                case.action_taken = f"权重系数降低至 {penalty_factor}"
    
    def _calculate_penalty_factor(self, severity: CollusionSeverity) -> float:
        """计算惩罚系数"""
        factors = {
            CollusionSeverity.LOW: 0.8,
            CollusionSeverity.MEDIUM: 0.6,
            CollusionSeverity.HIGH: 0.4,
            CollusionSeverity.CRITICAL: 0.2
        }
        return factors.get(severity, 0.5)
    
    def _create_case(
        self,
        collusion_type: CollusionType,
        involved_agents: List[str],
        severity: CollusionSeverity,
        evidence: Dict
    ) -> CollusionCase:
        """创建案件"""
        self._case_counter += 1
        case_id = f"case_{self._case_counter}_{int(time.time())}"
        
        return CollusionCase(
            case_id=case_id,
            collusion_type=collusion_type,
            involved_agents=involved_agents,
            severity=severity,
            evidence=evidence,
            detected_at=time.time()
        )
    
    def get_cases(self, unresolved_only: bool = False) -> List[CollusionCase]:
        """获取案件列表"""
        cases = self._cases
        if unresolved_only:
            cases = [c for c in cases if not c.resolved]
        return cases
    
    def get_case(self, case_id: str) -> Optional[CollusionCase]:
        """获取特定案件"""
        for case in self._cases:
            if case.case_id == case_id:
                return case
        return None
    
    def resolve_case(self, case_id: str, action: str):
        """解决案件"""
        case = self.get_case(case_id)
        if case:
            case.resolved = True
            case.action_taken = action
    
    def is_flagged_pair(self, agent_a: str, agent_b: str) -> bool:
        """检查是否为已标记的代理对"""
        return (agent_a, agent_b) in self._flagged_pairs or \
               (agent_b, agent_a) in self._flagged_pairs
    
    def get_summary(self) -> Dict:
        """获取检测摘要"""
        return {
            "total_cases": len(self._cases),
            "unresolved": sum(1 for c in self._cases if not c.resolved),
            "by_type": {
                t.value: sum(1 for c in self._cases if c.collusion_type == t)
                for t in CollusionType
            },
            "by_severity": {
                s.value: sum(1 for c in self._cases if c.severity == s)
                for s in CollusionSeverity
            },
            "flagged_pairs": len(self._flagged_pairs),
            "penalized_agents": len(self._penalties)
        }
    
    def export_audit_data(self) -> Dict:
        """导出审计数据"""
        return {
            "cases": [c.to_dict() for c in self._cases],
            "voting_patterns": {
                agent: {
                    "vote_count": len(p.vote_history),
                    "recent_votes": p.vote_history[-10:]
                }
                for agent, p in self._voting_patterns.items()
            },
            "citation_patterns": {
                agent: {
                    "citations_made": dict(p.citations_made),
                    "citations_received": dict(p.citations_received)
                }
                for agent, p in self._citation_patterns.items()
            },
            "penalties": self._penalties
        }


# 全局实例
_collusion_detector: Optional[CollusionDetector] = None


def get_collusion_detector(config: CollusionConfig = None) -> CollusionDetector:
    """获取串通检测器实例"""
    global _collusion_detector
    if _collusion_detector is None:
        _collusion_detector = CollusionDetector(config)
    return _collusion_detector


def reset_collusion_detector():
    """重置串通检测器"""
    global _collusion_detector
    _collusion_detector = None
