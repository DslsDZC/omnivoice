"""投票管理器 - 公平透明的表决系统"""
import time
import asyncio
import hashlib
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Any, Callable
from enum import Enum
import logging
import json

logger = logging.getLogger(__name__)


class VoteType(Enum):
    """投票类型"""
    SUPPORT = "support"      # 支持
    OPPOSE = "oppose"        # 反对
    MODIFY = "modify"        # 修改建议
    ABSTAIN = "abstain"      # 弃权


class VoteStatus(Enum):
    """投票状态"""
    PENDING = "pending"      # 等待投票
    COLLECTING = "collecting"  # 收集投票
    COMPLETED = "completed"  # 已完成
    VETOED = "vetoed"        # 被否决
    INVALID = "invalid"      # 无效


class VoteMode(Enum):
    """投票模式"""
    PUBLIC = "public"        # 实名投票
    ANONYMOUS = "anonymous"  # 匿名投票


@dataclass
class Vote:
    """单个投票"""
    voter_id: str
    vote_type: VoteType
    weight: float
    timestamp: float
    reason: str = ""
    modify_suggestion: str = ""  # 修改建议
    
    def to_dict(self, anonymous: bool = False) -> Dict:
        result = {
            "vote_type": self.vote_type.value,
            "weight": round(self.weight, 3),
            "timestamp": datetime.fromtimestamp(self.timestamp).isoformat(),
            "reason": self.reason
        }
        if not anonymous:
            result["voter_id"] = self.voter_id
            if self.modify_suggestion:
                result["modify_suggestion"] = self.modify_suggestion
        return result


@dataclass
class VotingSession:
    """投票会话"""
    session_id: str
    proposal: str
    proposer_id: str
    start_time: float
    end_time: float
    vote_window_sec: float = 3.0
    mode: VoteMode = VoteMode.PUBLIC
    
    # 投票记录
    votes: Dict[str, Vote] = field(default_factory=dict)
    
    # 状态
    status: VoteStatus = VoteStatus.PENDING
    
    # 结果
    support_weight: float = 0.0
    oppose_weight: float = 0.0
    modify_weight: float = 0.0
    total_weight: float = 0.0
    support_ratio: float = 0.0
    passed: bool = False
    
    # 边缘检测（第二轮）
    is_edge_case: bool = False
    second_round_votes: Dict[str, Vote] = field(default_factory=dict)
    second_round_result: Optional[bool] = None
    
    # 用户干预
    user_vetoed: bool = False
    user_veto_reason: str = ""
    
    def add_vote(self, vote: Vote):
        """添加投票"""
        self.votes[vote.voter_id] = vote
    
    def calculate_result(self, threshold: float = 0.6) -> Tuple[bool, Dict]:
        """
        计算投票结果
        
        Args:
            threshold: 通过阈值
            
        Returns:
            (passed, details): 是否通过及详情
        """
        self.support_weight = 0.0
        self.oppose_weight = 0.0
        self.modify_weight = 0.0
        
        for vote in self.votes.values():
            if vote.vote_type == VoteType.SUPPORT:
                self.support_weight += vote.weight
            elif vote.vote_type == VoteType.OPPOSE:
                self.oppose_weight += vote.weight
            elif vote.vote_type == VoteType.MODIFY:
                self.modify_weight += vote.weight
        
        self.total_weight = self.support_weight + self.oppose_weight + self.modify_weight
        
        if self.total_weight > 0:
            self.support_ratio = self.support_weight / self.total_weight
        else:
            self.support_ratio = 0.0
        
        # 检测边缘情况
        self.is_edge_case = abs(self.support_ratio - threshold) < 0.02
        
        self.passed = self.support_ratio >= threshold
        self.status = VoteStatus.COMPLETED
        
        return self.passed, self._get_details()
    
    def _get_details(self) -> Dict:
        """获取投票详情"""
        return {
            "session_id": self.session_id,
            "proposal": self.proposal[:200],
            "proposer": self.proposer_id,
            "support_weight": round(self.support_weight, 3),
            "oppose_weight": round(self.oppose_weight, 3),
            "modify_weight": round(self.modify_weight, 3),
            "total_weight": round(self.total_weight, 3),
            "support_ratio": round(self.support_ratio * 100, 1),
            "passed": self.passed,
            "is_edge_case": self.is_edge_case,
            "vote_count": len(self.votes),
            "anonymous": self.mode == VoteMode.ANONYMOUS
        }
    
    def get_minority_report(self) -> List[Dict]:
        """获取少数派报告（反对意见）"""
        oppose_votes = [
            v for v in self.votes.values()
            if v.vote_type == VoteType.OPPOSE
        ]
        
        return [
            {
                "voter_id": v.voter_id if self.mode == VoteMode.PUBLIC else "匿名",
                "reason": v.reason,
                "weight": round(v.weight, 3)
            }
            for v in sorted(oppose_votes, key=lambda x: x.weight, reverse=True)
        ]


@dataclass
class VotingConfig:
    """投票配置"""
    # 投票窗口
    vote_window_sec: float = 3.0
    
    # 通过阈值
    pass_threshold: float = 0.6
    
    # 边缘检测阈值
    edge_margin: float = 0.02
    
    # 第二轮快速投票窗口
    second_round_window_sec: float = 2.0
    
    # 默认投票模式
    default_mode: VoteMode = VoteMode.PUBLIC
    
    # 用户投票权重
    user_vote_weight: float = 1.0  # 默认等于代理平均权重
    
    # 叫停频率限制
    call_stop_cooldown_sec: float = 300.0  # 5分钟


class VoteManager:
    """投票管理器"""
    
    def __init__(self, config: VotingConfig = None, contribution_scorer=None):
        self.config = config or VotingConfig()
        self.contribution_scorer = contribution_scorer
        
        # 活跃投票会话
        self._active_session: Optional[VotingSession] = None
        
        # 历史投票
        self._vote_history: List[VotingSession] = []
        
        # 叫停冷却（防刷）
        self._last_call_stop: Dict[str, float] = {}  # agent_id -> timestamp
        
        # 会话ID
        self._session_counter = 0
        
        # 用户投票
        self._user_vote: Optional[Vote] = None
        self._user_weight_override: Optional[float] = None
        
        # 投票回调
        self._on_vote_complete: Optional[Callable] = None
    
    def can_call_stop(self, agent_id: str) -> Tuple[bool, float]:
        """
        检查代理是否可以发起叫停
        
        Returns:
            (can_stop, remaining_cooldown): 是否可以叫停及剩余冷却时间
        """
        now = time.time()
        last_time = self._last_call_stop.get(agent_id, 0)
        cooldown = self.config.call_stop_cooldown_sec
        
        elapsed = now - last_time
        if elapsed < cooldown:
            return False, cooldown - elapsed
        
        return True, 0.0
    
    async def start_voting(
        self,
        proposal: str,
        proposer_id: str,
        eligible_voters: List[str],
        mode: VoteMode = None
    ) -> VotingSession:
        """
        开始投票
        
        Args:
            proposal: 提案内容
            proposer_id: 提案者ID
            eligible_voters: 有投票权的代理列表
            mode: 投票模式
            
        Returns:
            VotingSession: 投票会话
        """
        # 检查是否可以叫停
        can_stop, remaining = self.can_call_stop(proposer_id)
        if not can_stop:
            raise ValueError(f"提案者冷却中，剩余 {remaining:.0f} 秒")
        
        # 记录叫停时间
        self._last_call_stop[proposer_id] = time.time()
        
        # 创建会话
        self._session_counter += 1
        session_id = f"vote_{self._session_counter}_{int(time.time())}"
        
        mode = mode or self.config.default_mode
        
        session = VotingSession(
            session_id=session_id,
            proposal=proposal,
            proposer_id=proposer_id,
            start_time=time.time(),
            end_time=time.time() + self.config.vote_window_sec,
            vote_window_sec=self.config.vote_window_sec,
            mode=mode
        )
        
        self._active_session = session
        
        logger.info(
            f"投票开始: {session_id}, 提案者: {proposer_id}, "
            f"投票窗口: {self.config.vote_window_sec}秒, 模式: {mode.value}"
        )
        
        return session
    
    async def submit_vote(
        self,
        voter_id: str,
        vote_type: VoteType,
        reason: str = "",
        modify_suggestion: str = ""
    ) -> bool:
        """
        提交投票
        
        Args:
            voter_id: 投票者ID
            vote_type: 投票类型
            reason: 原因
            modify_suggestion: 修改建议
            
        Returns:
            是否成功提交
        """
        if not self._active_session:
            logger.warning("没有活跃的投票会话")
            return False
        
        session = self._active_session
        
        # 检查是否在投票窗口内
        if time.time() > session.end_time:
            logger.warning(f"投票窗口已关闭: {session.session_id}")
            return False
        
        # 获取权重
        weight = self._get_voter_weight(voter_id)
        
        vote = Vote(
            voter_id=voter_id,
            vote_type=vote_type,
            weight=weight,
            timestamp=time.time(),
            reason=reason,
            modify_suggestion=modify_suggestion
        )
        
        session.add_vote(vote)
        logger.debug(f"投票: {voter_id} -> {vote_type.value} (权重: {weight:.2f})")
        
        return True
    
    async def submit_user_vote(
        self,
        vote_type: VoteType,
        reason: str = ""
    ) -> bool:
        """
        用户提交投票
        
        Args:
            vote_type: 投票类型
            reason: 原因
            
        Returns:
            是否成功
        """
        if not self._active_session:
            return False
        
        # 获取用户权重
        weight = self._user_weight_override or self._calculate_user_weight()
        
        vote = Vote(
            voter_id="user",
            vote_type=vote_type,
            weight=weight,
            timestamp=time.time(),
            reason=reason
        )
        
        self._user_vote = vote
        self._active_session.add_vote(vote)
        
        logger.info(f"用户投票: {vote_type.value} (权重: {weight:.2f})")
        
        return True
    
    def _get_voter_weight(self, voter_id: str) -> float:
        """获取投票者权重"""
        if self.contribution_scorer:
            return self.contribution_scorer.get_weight(voter_id)
        return 1.0
    
    def _calculate_user_weight(self) -> float:
        """计算用户权重（默认为代理平均权重）"""
        if not self.contribution_scorer:
            return self.config.user_vote_weight
        
        weights = self.contribution_scorer.get_all_weights()
        if weights:
            return sum(weights.values()) / len(weights)
        
        return self.config.user_vote_weight
    
    async def end_voting(self) -> Tuple[bool, Dict]:
        """
        结束投票并计算结果
        
        Returns:
            (passed, details): 是否通过及详情
        """
        if not self._active_session:
            return False, {"error": "没有活跃的投票会话"}
        
        session = self._active_session
        
        # 计算结果
        passed, details = session.calculate_result(self.config.pass_threshold)
        
        # 边缘检测
        if session.is_edge_case:
            logger.info(f"检测到边缘情况，启动第二轮快速投票")
            second_passed = await self._second_round_voting()
            if second_passed is not None and second_passed != passed:
                logger.info(f"第二轮结果与第一轮不同，宣布无共识")
                session.passed = False
                session.status = VoteStatus.INVALID
                details["edge_case_resolved"] = False
                details["second_round_differed"] = True
            else:
                details["edge_case_resolved"] = True
        
        # 记录提案结果
        if self.contribution_scorer:
            self.contribution_scorer.record_proposal_result(
                session.proposer_id, 
                session.passed
            )
        
        # 保存历史
        self._vote_history.append(session)
        self._active_session = None
        
        logger.info(
            f"投票结束: {session.session_id}, 结果: {'通过' if passed else '否决'}, "
            f"支持率: {session.support_ratio*100:.1f}%"
        )
        
        return passed, details
    
    async def _second_round_voting(self) -> Optional[bool]:
        """第二轮快速投票"""
        if not self._active_session:
            return None
        
        session = self._active_session
        
        # 创建第二轮投票窗口
        session.end_time = time.time() + self.config.second_round_window_sec
        
        # 等待投票（实际实现中需要与代理交互）
        await asyncio.sleep(self.config.second_round_window_sec)
        
        # 计算第二轮结果
        support = 0.0
        oppose = 0.0
        
        for vote in session.second_round_votes.values():
            if vote.vote_type == VoteType.SUPPORT:
                support += vote.weight
            elif vote.vote_type == VoteType.OPPOSE:
                oppose += vote.weight
        
        total = support + oppose
        if total == 0:
            return None
        
        session.second_round_result = (support / total) >= self.config.pass_threshold
        return session.second_round_result
    
    def veto(self, reason: str = "") -> bool:
        """
        用户否决议案
        
        Args:
            reason: 否决原因
            
        Returns:
            是否成功
        """
        if not self._vote_history:
            return False
        
        last_session = self._vote_history[-1]
        if last_session.status == VoteStatus.VETOED:
            return False
        
        last_session.user_vetoed = True
        last_session.user_veto_reason = reason
        last_session.status = VoteStatus.VETOED
        
        logger.warning(f"用户否决议案: {last_session.session_id}, 原因: {reason}")
        
        return True
    
    def set_user_weight(self, weight: float):
        """设置用户投票权重"""
        self._user_weight_override = weight
    
    def get_active_session(self) -> Optional[VotingSession]:
        """获取活跃投票会话"""
        return self._active_session
    
    def get_vote_history(self, limit: int = 10) -> List[Dict]:
        """获取投票历史"""
        return [
            {
                "session_id": s.session_id,
                "proposal": s.proposal[:100],
                "proposer": s.proposer_id,
                "passed": s.passed,
                "support_ratio": round(s.support_ratio * 100, 1),
                "vote_count": len(s.votes),
                "user_vetoed": s.user_vetoed,
                "timestamp": datetime.fromtimestamp(s.start_time).isoformat()
            }
            for s in self._vote_history[-limit:]
        ]
    
    def get_session_details(self, session_id: str) -> Optional[Dict]:
        """获取会话详情"""
        for session in self._vote_history:
            if session.session_id == session_id:
                return {
                    "session_id": session.session_id,
                    "proposal": session.proposal,
                    "proposer": session.proposer_id,
                    "status": session.status.value,
                    "passed": session.passed,
                    "support_weight": round(session.support_weight, 3),
                    "oppose_weight": round(session.oppose_weight, 3),
                    "support_ratio": round(session.support_ratio * 100, 1),
                    "votes": [
                        v.to_dict(session.mode == VoteMode.ANONYMOUS)
                        for v in session.votes.values()
                    ],
                    "minority_report": session.get_minority_report(),
                    "user_vetoed": session.user_vetoed,
                    "user_veto_reason": session.user_veto_reason
                }
        
        return None
    
    def get_stats(self) -> Dict:
        """获取投票统计"""
        total = len(self._vote_history)
        passed = sum(1 for s in self._vote_history if s.passed)
        vetoed = sum(1 for s in self._vote_history if s.user_vetoed)
        edge_cases = sum(1 for s in self._vote_history if s.is_edge_case)
        
        return {
            "total_votes": total,
            "passed": passed,
            "rejected": total - passed,
            "pass_rate": round(passed / total * 100, 1) if total > 0 else 0,
            "user_vetoed": vetoed,
            "edge_cases": edge_cases
        }
    
    def export_audit_log(self) -> List[Dict]:
        """导出审计日志"""
        return [
            {
                "session_id": s.session_id,
                "proposal": s.proposal,
                "proposer": s.proposer_id,
                "votes": [
                    {
                        "voter": v.voter_id,
                        "type": v.vote_type.value,
                        "weight": v.weight,
                        "reason": v.reason,
                        "timestamp": datetime.fromtimestamp(v.timestamp).isoformat()
                    }
                    for v in s.votes.values()
                ],
                "result": {
                    "passed": s.passed,
                    "support_ratio": s.support_ratio,
                    "support_weight": s.support_weight,
                    "oppose_weight": s.oppose_weight
                },
                "user_vetoed": s.user_vetoed,
                "timestamp": datetime.fromtimestamp(s.start_time).isoformat()
            }
            for s in self._vote_history
        ]


# 全局实例
_vote_manager: Optional[VoteManager] = None


def get_vote_manager(config: VotingConfig = None, contribution_scorer=None) -> VoteManager:
    """获取投票管理器实例"""
    global _vote_manager
    if _vote_manager is None:
        _vote_manager = VoteManager(config, contribution_scorer)
    return _vote_manager


def reset_vote_manager():
    """重置投票管理器"""
    global _vote_manager
    _vote_manager = None
