"""异常检测与恢复模块

检测：讨论循环、僵局、代理失控、工具失败、资源耗尽
恢复：自动干预 → 降级 → 请求用户 → 强制结束
"""
import time
import threading
from typing import Dict, List, Optional, Tuple, Callable
from dataclasses import dataclass, field
from enum import Enum
import difflib


class ExceptionType(Enum):
    """异常类型"""
    DISCUSSION_LOOP = "discussion_loop"       # 观点相似度90%重复3次
    DEADLOCK = "deadlock"                      # 两轮表决支持率45%-55%
    AGENT_OUT_OF_CONTROL = "agent_out_of_control"  # 被频繁标记
    TOOL_FAILURE = "tool_failure"              # 工具调用失败
    RESOURCE_EXHAUSTED = "resource_exhausted"  # 资源耗尽


class RecoveryLevel(Enum):
    """恢复级别：软→硬"""
    AUTO_INTERVENE = 1      # 自动干预
    DOWNGRADE = 2           # 降级处理
    REQUEST_USER = 3        # 请求用户
    FORCE_END = 4           # 强制结束


@dataclass
class ExceptionRecord:
    """异常记录"""
    exception_type: ExceptionType
    detected_at: float
    details: Dict
    recovery_level: RecoveryLevel
    recovery_action: str
    recovery_result: str
    resolved: bool = False


@dataclass
class AgentBehaviorRecord:
    """代理行为记录"""
    agent_id: str
    speak_count: int = 0
    interrupted_count: int = 0
    marked_by_others: int = 0
    user_reports: int = 0
    tool_failures: int = 0
    tool_successes: int = 0
    proposals_made: int = 0
    proposals_passed: int = 0
    last_active_time: float = 0


class ExceptionHandler:
    """异常检测与恢复处理器"""
    
    def __init__(self, whiteboard):
        self.whiteboard = whiteboard
        
        # 异常记录
        self._exception_records: List[ExceptionRecord] = []
        
        # 代理行为记录
        self._agent_records: Dict[str, AgentBehaviorRecord] = {}
        
        # 观点历史（用于循环检测）
        self._viewpoint_history: List[str] = []
        self._viewpoint_similarity_threshold = 0.9
        self._loop_detection_window = 3
        
        # 表决历史（用于僵局检测）
        self._vote_history: List[float] = []  # 支持率历史
        self._deadlock_range = (0.45, 0.55)
        self._deadlock_rounds = 2
        
        # 恢复回调
        self._recovery_callbacks: Dict[ExceptionType, Callable] = {}
        
        # 锁
        self._lock = threading.RLock()
    
    def record_agent_speak(self, agent_id: str):
        """记录代理发言"""
        with self._lock:
            if agent_id not in self._agent_records:
                self._agent_records[agent_id] = AgentBehaviorRecord(agent_id=agent_id)
            self._agent_records[agent_id].speak_count += 1
            self._agent_records[agent_id].last_active_time = time.time()
    
    def record_agent_marked(self, agent_id: str, by_agent: str):
        """记录代理被标记"""
        with self._lock:
            if agent_id not in self._agent_records:
                self._agent_records[agent_id] = AgentBehaviorRecord(agent_id=agent_id)
            self._agent_records[agent_id].marked_by_others += 1
    
    def record_user_report(self, agent_id: str):
        """记录用户举报"""
        with self._lock:
            if agent_id not in self._agent_records:
                self._agent_records[agent_id] = AgentBehaviorRecord(agent_id=agent_id)
            self._agent_records[agent_id].user_reports += 1
    
    def record_tool_result(self, agent_id: str, success: bool):
        """记录工具调用结果"""
        with self._lock:
            if agent_id not in self._agent_records:
                self._agent_records[agent_id] = AgentBehaviorRecord(agent_id=agent_id)
            if success:
                self._agent_records[agent_id].tool_successes += 1
            else:
                self._agent_records[agent_id].tool_failures += 1
    
    def record_proposal(self, agent_id: str, passed: bool):
        """记录提案结果"""
        with self._lock:
            if agent_id not in self._agent_records:
                self._agent_records[agent_id] = AgentBehaviorRecord(agent_id=agent_id)
            self._agent_records[agent_id].proposals_made += 1
            if passed:
                self._agent_records[agent_id].proposals_passed += 1
    
    def record_vote_result(self, support_rate: float):
        """记录表决结果"""
        with self._lock:
            self._vote_history.append(support_rate)
    
    # ==================== 异常检测 ====================
    
    def check_discussion_loop(self, new_viewpoint: str) -> Optional[ExceptionRecord]:
        """检测讨论循环：观点相似度90%重复3次"""
        with self._lock:
            self._viewpoint_history.append(new_viewpoint)
            
            if len(self._viewpoint_history) < self._loop_detection_window:
                return None
            
            # 检查最近N个观点的相似度
            recent = self._viewpoint_history[-self._loop_detection_window:]
            similarities = []
            
            for i in range(len(recent) - 1):
                for j in range(i + 1, len(recent)):
                    sim = difflib.SequenceMatcher(None, recent[i], recent[j]).ratio()
                    similarities.append(sim)
            
            avg_sim = sum(similarities) / len(similarities) if similarities else 0
            
            if avg_sim >= self._viewpoint_similarity_threshold:
                return ExceptionRecord(
                    exception_type=ExceptionType.DISCUSSION_LOOP,
                    detected_at=time.time(),
                    details={"avg_similarity": avg_sim, "viewpoints": recent},
                    recovery_level=RecoveryLevel.AUTO_INTERVENE,
                    recovery_action="自动叫停并引入外部知识",
                    recovery_result=""
                )
            
            return None
    
    def check_deadlock(self) -> Optional[ExceptionRecord]:
        """检测僵局：两轮表决支持率在45%-55%"""
        with self._lock:
            if len(self._vote_history) < self._deadlock_rounds:
                return None
            
            recent_rates = self._vote_history[-self._deadlock_rounds:]
            
            # 检查是否都在僵局范围内
            in_range = all(
                self._deadlock_range[0] <= rate <= self._deadlock_range[1]
                for rate in recent_rates
            )
            
            if in_range:
                return ExceptionRecord(
                    exception_type=ExceptionType.DEADLOCK,
                    detected_at=time.time(),
                    details={"support_rates": recent_rates},
                    recovery_level=RecoveryLevel.DOWNGRADE,
                    recovery_action="降低共识阈值或请求用户裁决",
                    recovery_result=""
                )
            
            return None
    
    def check_agent_out_of_control(self, agent_id: str) -> Optional[ExceptionRecord]:
        """检测代理失控：被频繁标记或用户举报"""
        with self._lock:
            if agent_id not in self._agent_records:
                return None
            
            record = self._agent_records[agent_id]
            
            # 失控阈值
            mark_threshold = 3
            report_threshold = 1
            
            if record.marked_by_others >= mark_threshold or record.user_reports >= report_threshold:
                return ExceptionRecord(
                    exception_type=ExceptionType.AGENT_OUT_OF_CONTROL,
                    detected_at=time.time(),
                    details={
                        "agent_id": agent_id,
                        "marked_count": record.marked_by_others,
                        "user_reports": record.user_reports
                    },
                    recovery_level=RecoveryLevel.AUTO_INTERVENE,
                    recovery_action="自动静音并重置状态",
                    recovery_result=""
                )
            
            return None
    
    def check_tool_failure(self, agent_id: str, consecutive_failures: int) -> Optional[ExceptionRecord]:
        """检测工具调用失败"""
        failure_threshold = 3
        
        if consecutive_failures >= failure_threshold:
            return ExceptionRecord(
                exception_type=ExceptionType.TOOL_FAILURE,
                detected_at=time.time(),
                details={
                    "agent_id": agent_id,
                    "consecutive_failures": consecutive_failures
                },
                recovery_level=RecoveryLevel.DOWNGRADE,
                recovery_action="降级处理或跳过工具调用",
                recovery_result=""
            )
        
        return None
    
    def check_resource_exhausted(self, memory_usage: float, api_calls: int, limits: Dict) -> Optional[ExceptionRecord]:
        """检测资源耗尽"""
        memory_limit = limits.get("memory_mb", 500)
        api_limit = limits.get("api_calls_per_hour", 1000)
        
        if memory_usage > memory_limit * 0.9 or api_calls > api_limit * 0.9:
            return ExceptionRecord(
                exception_type=ExceptionType.RESOURCE_EXHAUSTED,
                detected_at=time.time(),
                details={
                    "memory_usage": memory_usage,
                    "api_calls": api_calls,
                    "limits": limits
                },
                recovery_level=RecoveryLevel.FORCE_END,
                recovery_action="紧急存档并通知用户",
                recovery_result=""
            )
        
        return None
    
    # ==================== 恢复策略 ====================
    
    def register_recovery_callback(self, exception_type: ExceptionType, callback: Callable):
        """注册恢复回调"""
        self._recovery_callbacks[exception_type] = callback
    
    async def recover(self, exception: ExceptionRecord) -> Tuple[bool, str]:
        """执行恢复策略：软→硬"""
        with self._lock:
            self._exception_records.append(exception)
        
        # 记录到白板
        self.whiteboard.add_message(
            agent_id="system",
            content=f"[异常] {exception.exception_type.value}: {exception.recovery_action}",
            message_type="system"
        )
        
        # 按恢复级别执行
        if exception.recovery_level == RecoveryLevel.AUTO_INTERVENE:
            return await self._auto_intervene(exception)
        elif exception.recovery_level == RecoveryLevel.DOWNGRADE:
            return await self._downgrade(exception)
        elif exception.recovery_level == RecoveryLevel.REQUEST_USER:
            return await self._request_user(exception)
        else:
            return await self._force_end(exception)
    
    async def _auto_intervene(self, exception: ExceptionRecord) -> Tuple[bool, str]:
        """自动干预"""
        if exception.exception_type == ExceptionType.DISCUSSION_LOOP:
            # 自动叫停，引入外部知识
            return True, "已自动叫停讨论，建议引入外部知识"
        
        elif exception.exception_type == ExceptionType.AGENT_OUT_OF_CONTROL:
            # 自动静音代理
            agent_id = exception.details.get("agent_id")
            if agent_id:
                return True, f"已静音代理 {agent_id}，状态已重置"
            return False, "未找到失控代理"
        
        return False, "未处理的异常类型"
    
    async def _downgrade(self, exception: ExceptionRecord) -> Tuple[bool, str]:
        """降级处理"""
        if exception.exception_type == ExceptionType.DEADLOCK:
            # 降低共识阈值
            return True, "已降低共识阈值至50%"
        
        elif exception.exception_type == ExceptionType.TOOL_FAILURE:
            # 跳过工具调用
            return True, "已跳过工具调用，使用默认处理"
        
        return False, "降级失败"
    
    async def _request_user(self, exception: ExceptionRecord) -> Tuple[bool, str]:
        """请求用户决策"""
        # 这需要用户输入系统支持
        return True, "等待用户决策..."
    
    async def _force_end(self, exception: ExceptionRecord) -> Tuple[bool, str]:
        """强制结束"""
        if exception.exception_type == ExceptionType.RESOURCE_EXHAUSTED:
            # 紧急存档
            return True, "已紧急存档会话数据，讨论强制结束"
        
        return True, "讨论已强制结束"
    
    # ==================== 报告 ====================
    
    def get_exception_summary(self) -> Dict:
        """获取异常摘要"""
        with self._lock:
            total = len(self._exception_records)
            by_type = {}
            for r in self._exception_records:
                t = r.exception_type.value
                by_type[t] = by_type.get(t, 0) + 1
            
            return {
                "total_exceptions": total,
                "by_type": by_type,
                "recent": [
                    {
                        "type": r.exception_type.value,
                        "detected_at": r.detected_at,
                        "resolved": r.resolved
                    }
                    for r in self._exception_records[-5:]
                ]
            }
    
    def get_agent_behavior_summary(self) -> Dict:
        """获取代理行为摘要"""
        with self._lock:
            return {
                agent_id: {
                    "speak_count": r.speak_count,
                    "marked_count": r.marked_by_others,
                    "user_reports": r.user_reports,
                    "tool_success_rate": r.tool_successes / (r.tool_successes + r.tool_failures) 
                        if (r.tool_successes + r.tool_failures) > 0 else 1.0,
                    "proposal_pass_rate": r.proposals_passed / r.proposals_made 
                        if r.proposals_made > 0 else 0
                }
                for agent_id, r in self._agent_records.items()
            }
