"""贡献分计算模块 - 动态权重系统"""
import time
import math
import hashlib
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Any
from enum import Enum
import logging
import asyncio

logger = logging.getLogger(__name__)


class ContributionType(Enum):
    """贡献类型"""
    CITATION = "citation"           # 被引用
    TOOL_USAGE = "tool_usage"       # 工具结果被使用
    PROPOSAL_PASSED = "proposal_passed"  # 提案通过
    VALID_SPEECH = "valid_speech"   # 有效发言
    CONSENSUS_BUILT = "consensus_built"  # 促进共识


@dataclass
class ContributionRecord:
    """贡献记录"""
    agent_id: str
    contribution_type: ContributionType
    value: float          # 贡献分值
    source_agent: Optional[str] = None  # 来源代理（引用者）
    context: str = ""     # 上下文描述
    timestamp: float = field(default_factory=time.time)
    session_id: str = ""


@dataclass
class AgentWeight:
    """代理权重"""
    agent_id: str
    base_weight: float = 1.0
    contribution_coefficient: float = 1.0  # 0.5 ~ 2.0
    total_weight: float = 1.0
    
    # 贡分明细
    citation_count: int = 0
    tool_usage_count: int = 0
    proposal_passed_count: int = 0
    proposal_total_count: int = 0
    valid_speech_count: int = 0
    
    # 防作弊状态
    is_collusion_suspected: bool = False
    frozen_until: Optional[float] = None
    penalty_factor: float = 1.0
    
    def calculate_total(self):
        """计算总权重"""
        coefficient = self.contribution_coefficient * self.penalty_factor
        coefficient = max(0.5, min(2.0, coefficient))  # 限制在0.5-2.0
        self.total_weight = self.base_weight * coefficient
        return self.total_weight
    
    def to_dict(self) -> Dict:
        return {
            "agent_id": self.agent_id,
            "base_weight": self.base_weight,
            "contribution_coefficient": round(self.contribution_coefficient, 3),
            "total_weight": round(self.total_weight, 3),
            "citation_count": self.citation_count,
            "tool_usage_count": self.tool_usage_count,
            "proposal_passed_count": self.proposal_passed_count,
            "proposal_total_count": self.proposal_total_count,
            "proposal_pass_rate": (
                self.proposal_passed_count / self.proposal_total_count 
                if self.proposal_total_count > 0 else 0
            ),
            "is_collusion_suspected": self.is_collusion_suspected,
            "penalty_factor": self.penalty_factor
        }


@dataclass
class ContributionConfig:
    """贡献分配置"""
    # 单次贡献上限（防刷分）
    max_gain_per_round: float = 0.1
    
    # 权重范围
    min_coefficient: float = 0.5
    max_coefficient: float = 2.0
    
    # 各类贡献的基础分值
    citation_score: float = 0.02      # 被引用一次
    tool_usage_score: float = 0.03    # 工具结果被使用
    proposal_passed_score: float = 0.05  # 提案通过
    valid_speech_score: float = 0.01  # 有效发言
    
    # 串通惩罚
    collusion_penalty: float = 0.5    # 权重系数
    mutual_citation_freeze_hours: int = 24  # 互相引用冻结时间
    
    # 相似度阈值
    citation_similarity_threshold: float = 0.8


class ContributionScorer:
    """贡献分计算器"""
    
    def __init__(self, config: ContributionConfig = None):
        self.config = config or ContributionConfig()
        
        # 代理权重表
        self._weights: Dict[str, AgentWeight] = {}
        
        # 贡献历史（当前会话）
        self._contribution_history: List[ContributionRecord] = []
        
        # 引用关系图（用于检测互相引用）
        self._citation_graph: Dict[str, Set[str]] = {}  # cited_by -> set of citers
        
        # 每轮贡献累计（防刷分）
        self._round_gains: Dict[str, float] = {}
        
        # 会话ID
        self._session_id = ""
    
    def initialize_session(self, session_id: str, agent_ids: List[str]):
        """初始化会话"""
        self._session_id = session_id
        self._weights.clear()
        self._contribution_history.clear()
        self._citation_graph.clear()
        self._round_gains.clear()
        
        for agent_id in agent_ids:
            self._weights[agent_id] = AgentWeight(agent_id=agent_id)
        
        logger.info(f"贡献分系统初始化，代理数: {len(agent_ids)}")
    
    def new_round(self):
        """开始新一轮（重置轮次累计）"""
        self._round_gains.clear()
    
    def record_citation(
        self,
        cited_agent: str,
        citing_agent: str,
        similarity: float,
        context: str = ""
    ) -> float:
        """
        记录引用
        
        Args:
            cited_agent: 被引用的代理
            citing_agent: 引用者
            similarity: 相似度
            context: 上下文
            
        Returns:
            获得的贡献分
        """
        # 不允许自引用
        if cited_agent == citing_agent:
            return 0.0
        
        # 相似度阈值检查
        if similarity < self.config.citation_similarity_threshold:
            return 0.0
        
        # 检查是否在冻结期
        if self._is_frozen(cited_agent):
            logger.debug(f"代理 {cited_agent} 贡献分被冻结")
            return 0.0
        
        # 检测互相引用串通
        self._check_mutual_citation(cited_agent, citing_agent)
        
        # 记录引用关系
        if cited_agent not in self._citation_graph:
            self._citation_graph[cited_agent] = set()
        self._citation_graph[cited_agent].add(citing_agent)
        
        # 计算贡献分（相似度加权）
        score = self.config.citation_score * similarity
        
        # 应用轮次上限
        score = self._apply_round_limit(cited_agent, score)
        
        # 更新权重
        if score > 0:
            self._add_contribution(cited_agent, ContributionType.CITATION, score, citing_agent, context)
        
        return score
    
    def record_tool_usage(
        self,
        tool_owner: str,
        user_agent: str,
        tool_name: str,
        dependency_chain: List[str] = None
    ) -> float:
        """
        记录工具结果被使用
        
        Args:
            tool_owner: 工具调用者
            user_agent: 使用结果的代理
            tool_name: 工具名
            dependency_chain: 依赖链
            
        Returns:
            获得的贡献分
        """
        if tool_owner == user_agent:
            return 0.0
        
        if self._is_frozen(tool_owner):
            return 0.0
        
        # 验证依赖链（防止伪造）
        if dependency_chain:
            if not self._verify_dependency_chain(tool_owner, tool_name, dependency_chain):
                logger.warning(f"依赖链验证失败: {tool_owner} -> {tool_name}")
                return 0.0
        
        score = self.config.tool_usage_score
        score = self._apply_round_limit(tool_owner, score)
        
        if score > 0:
            self._add_contribution(
                tool_owner, 
                ContributionType.TOOL_USAGE, 
                score, 
                user_agent, 
                f"工具: {tool_name}"
            )
        
        return score
    
    def record_proposal_result(
        self,
        proposer_id: str,
        passed: bool
    ) -> float:
        """
        记录提案结果
        
        Args:
            proposer_id: 提案者
            passed: 是否通过
            
        Returns:
            获得的贡献分
        """
        if proposer_id not in self._weights:
            return 0.0
        
        weight = self._weights[proposer_id]
        weight.proposal_total_count += 1
        
        if passed:
            weight.proposal_passed_count += 1
            if not self._is_frozen(proposer_id):
                score = self.config.proposal_passed_score
                score = self._apply_round_limit(proposer_id, score)
                
                if score > 0:
                    self._add_contribution(
                        proposer_id,
                        ContributionType.PROPOSAL_PASSED,
                        score,
                        context=f"提案通过率: {weight.proposal_passed_count}/{weight.proposal_total_count}"
                    )
                    return score
        
        return 0.0
    
    def record_valid_speech(self, agent_id: str, is_redundant: bool = False) -> float:
        """
        记录有效发言
        
        Args:
            agent_id: 代理ID
            is_redundant: 是否冗余
            
        Returns:
            获得的贡献分
        """
        if is_redundant or self._is_frozen(agent_id):
            return 0.0
        
        weight = self._weights.get(agent_id)
        if weight:
            weight.valid_speech_count += 1
        
        score = self.config.valid_speech_score
        score = self._apply_round_limit(agent_id, score)
        
        if score > 0:
            self._add_contribution(agent_id, ContributionType.VALID_SPEECH, score)
        
        return score
    
    def apply_collusion_penalty(self, agent_ids: List[str], penalty_factor: float = None):
        """
        应用串通惩罚
        
        Args:
            agent_ids: 涉及的代理
            penalty_factor: 惩罚系数
        """
        penalty = penalty_factor or self.config.collusion_penalty
        
        for agent_id in agent_ids:
            if agent_id in self._weights:
                weight = self._weights[agent_id]
                weight.is_collusion_suspected = True
                weight.penalty_factor = penalty
                weight.calculate_total()
                
                logger.warning(f"代理 {agent_id} 被标记为串通嫌疑，惩罚系数: {penalty}")
    
    def freeze_contribution(self, agent_id: str, hours: int = None):
        """冻结代理的贡献分"""
        if agent_id in self._weights:
            hours = hours or self.config.mutual_citation_freeze_hours
            self._weights[agent_id].frozen_until = time.time() + hours * 3600
            logger.info(f"代理 {agent_id} 贡献分冻结 {hours} 小时")
    
    def _is_frozen(self, agent_id: str) -> bool:
        """检查是否被冻结"""
        if agent_id not in self._weights:
            return True
        
        frozen_until = self._weights[agent_id].frozen_until
        if frozen_until and time.time() < frozen_until:
            return True
        
        return False
    
    def _check_mutual_citation(self, cited_agent: str, citing_agent: str):
        """检测互相引用"""
        # 检查是否存在 A引用B 且 B引用A
        if citing_agent in self._citation_graph:
            if cited_agent in self._citation_graph[citing_agent]:
                # 检测到互相引用
                logger.warning(
                    f"检测到互相引用: {cited_agent} <-> {citing_agent}"
                )
                self.freeze_contribution(cited_agent)
                self.freeze_contribution(citing_agent)
    
    def _verify_dependency_chain(
        self, 
        tool_owner: str, 
        tool_name: str, 
        chain: List[str]
    ) -> bool:
        """验证依赖链"""
        # 简化验证：检查链中是否包含工具调用者和工具名
        expected_prefix = f"{tool_owner}:{tool_name}"
        for item in chain:
            if item.startswith(expected_prefix):
                return True
        return False
    
    def _apply_round_limit(self, agent_id: str, score: float) -> float:
        """应用轮次限制"""
        current_gain = self._round_gains.get(agent_id, 0.0)
        remaining = self.config.max_gain_per_round - current_gain
        
        if remaining <= 0:
            return 0.0
        
        actual_score = min(score, remaining)
        self._round_gains[agent_id] = current_gain + actual_score
        
        return actual_score
    
    def _add_contribution(
        self,
        agent_id: str,
        contribution_type: ContributionType,
        value: float,
        source_agent: str = None,
        context: str = ""
    ):
        """添加贡献记录"""
        # 记录历史
        record = ContributionRecord(
            agent_id=agent_id,
            contribution_type=contribution_type,
            value=value,
            source_agent=source_agent,
            context=context,
            session_id=self._session_id
        )
        self._contribution_history.append(record)
        
        # 更新权重
        weight = self._weights.get(agent_id)
        if weight:
            weight.contribution_coefficient += value
            weight.contribution_coefficient = max(
                self.config.min_coefficient,
                min(self.config.max_coefficient, weight.contribution_coefficient)
            )
            weight.calculate_total()
            
            # 更新计数
            if contribution_type == ContributionType.CITATION:
                weight.citation_count += 1
            elif contribution_type == ContributionType.TOOL_USAGE:
                weight.tool_usage_count += 1
    
    def get_weight(self, agent_id: str) -> float:
        """获取代理权重"""
        weight = self._weights.get(agent_id)
        return weight.total_weight if weight else 1.0
    
    def get_all_weights(self) -> Dict[str, float]:
        """获取所有代理权重"""
        return {
            agent_id: weight.total_weight
            for agent_id, weight in self._weights.items()
        }
    
    def get_weight_details(self, agent_id: str) -> Optional[Dict]:
        """获取权重详情"""
        weight = self._weights.get(agent_id)
        return weight.to_dict() if weight else None
    
    def set_weight_override(self, agent_id: str, total_weight: float):
        """用户覆盖权重"""
        if agent_id in self._weights:
            self._weights[agent_id].total_weight = total_weight
            logger.info(f"用户覆盖权重: {agent_id} -> {total_weight}")
    
    def reset_weight(self, agent_id: str):
        """重置代理权重"""
        if agent_id in self._weights:
            self._weights[agent_id] = AgentWeight(agent_id=agent_id)
            logger.info(f"重置权重: {agent_id}")
    
    def get_contribution_history(self, agent_id: str = None) -> List[Dict]:
        """获取贡献历史"""
        records = self._contribution_history
        if agent_id:
            records = [r for r in records if r.agent_id == agent_id]
        
        return [
            {
                "agent_id": r.agent_id,
                "type": r.contribution_type.value,
                "value": round(r.value, 4),
                "source": r.source_agent,
                "context": r.context,
                "timestamp": datetime.fromtimestamp(r.timestamp).isoformat()
            }
            for r in records
        ]
    
    def get_summary(self) -> Dict:
        """获取贡献分系统摘要"""
        return {
            "session_id": self._session_id,
            "agents": {
                agent_id: weight.to_dict()
                for agent_id, weight in self._weights.items()
            },
            "total_contributions": len(self._contribution_history),
            "citation_graph_size": sum(len(v) for v in self._citation_graph.values())
        }


# 全局实例
_contribution_scorer: Optional[ContributionScorer] = None


def get_contribution_scorer(config: ContributionConfig = None) -> ContributionScorer:
    """获取贡献分计算器实例"""
    global _contribution_scorer
    if _contribution_scorer is None:
        _contribution_scorer = ContributionScorer(config)
    return _contribution_scorer


def reset_contribution_scorer():
    """重置贡献分计算器"""
    global _contribution_scorer
    _contribution_scorer = None
