"""停滞检测器 - 防卡死机制"""
import time
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable, Any
from enum import Enum

logger = logging.getLogger(__name__)


class StallType(Enum):
    """停滞类型"""
    IDLE_TIMEOUT = "idle_timeout"       # 自然冷却超时
    ROUND_LIMIT = "round_limit"         # 轮次上限
    REPETITION = "repetition"           # 观点重复
    NO_CONSENSUS = "no_consensus"       # 无法达成共识
    AGENT_SILENCE = "agent_silence"     # 所有代理沉默
    MASSIVE_REPETITION = "massive_repetition"  # 大规模重复（跳过议程）


@dataclass
class StallConfig:
    """停滞检测配置"""
    # 空闲超时阈值（秒）
    idle_timeout: float = 30.0
    
    # 轮次上限
    max_rounds: int = 50
    
    # 重复检测阈值（相似度）
    repetition_threshold: float = 0.85
    
    # 重复检测窗口（最近N条消息）
    repetition_window: int = 20
    
    # 连续重复次数限制
    max_consecutive_duplicates: int = 3
    
    # 强制结束前的收敛尝试次数
    max_convergence_attempts: int = 3
    
    # 收敛尝试间隔（秒）
    convergence_interval: float = 30.0
    
    # 大规模重复检测配置
    massive_repetition_window: int = 10       # 检测窗口大小
    massive_repetition_agent_ratio: float = 0.5  # 重复代理比例阈值（50%）


@dataclass
class StallEvent:
    """停滞事件"""
    stall_type: StallType
    timestamp: float
    details: Dict
    action_taken: str = ""
    
    def to_dict(self) -> Dict:
        return {
            "type": self.stall_type.value,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.timestamp)),
            "details": self.details,
            "action_taken": self.action_taken
        }


class StallDetector:
    """停滞检测器 - 检测并处理讨论停滞"""
    
    def __init__(self, whiteboard, config: StallConfig = None):
        self.whiteboard = whiteboard
        self.config = config or StallConfig()
        
        # 停滞事件历史
        self._stall_events: List[StallEvent] = []
        
        # 收敛尝试计数
        self._convergence_attempts = 0
        
        # 检测任务
        self._detection_task: Optional[asyncio.Task] = None
        self._running = False
        
        # 回调函数
        self._on_stall_detected: Optional[Callable] = None
        self._on_force_end: Optional[Callable] = None
        
        # 重复计数器
        self._duplicate_counts: Dict[str, int] = {}  # agent_id -> count
    
    def set_callbacks(self, on_stall: Callable = None, on_force_end: Callable = None):
        """设置回调函数"""
        self._on_stall_detected = on_stall
        self._on_force_end = on_force_end
    
    async def start_detection(self):
        """启动停滞检测"""
        if self._running:
            return
        
        self._running = True
        self._detection_task = asyncio.create_task(self._detection_loop())
        logger.info("停滞检测器已启动")
    
    async def stop_detection(self):
        """停止停滞检测"""
        self._running = False
        if self._detection_task:
            self._detection_task.cancel()
            try:
                await self._detection_task
            except asyncio.CancelledError:
                pass
            self._detection_task = None
        logger.info("停滞检测器已停止")
    
    async def _detection_loop(self):
        """检测循环"""
        while self._running:
            try:
                # 检测各种停滞情况
                await self._check_idle_timeout()
                await self._check_round_limit()
                await self._check_agent_silence()
                await self._check_massive_repetition()
                
                # 每5秒检测一次
                await asyncio.sleep(5)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"停滞检测错误: {e}")
                await asyncio.sleep(5)
    
    async def _check_idle_timeout(self):
        """检测空闲超时"""
        if self.whiteboard.check_idle_timeout():
            idle_time = self.whiteboard.get_idle_time()
            
            event = StallEvent(
                stall_type=StallType.IDLE_TIMEOUT,
                timestamp=time.time(),
                details={
                    "idle_time": idle_time,
                    "threshold": self.config.idle_timeout
                }
            )
            
            await self._handle_stall(event)
    
    async def _check_round_limit(self):
        """检测轮次上限"""
        if self.whiteboard.is_round_limit_reached():
            rounds = self.whiteboard.get_round_count()
            
            event = StallEvent(
                stall_type=StallType.ROUND_LIMIT,
                timestamp=time.time(),
                details={
                    "current_rounds": rounds,
                    "max_rounds": self.config.max_rounds
                }
            )
            
            await self._handle_stall(event)
    
    async def _check_agent_silence(self):
        """检测所有代理沉默"""
        states = self.whiteboard.get_all_agent_states()
        
        if not states:
            return
        
        # 检查所有代理是否都超过一定时间未发言
        now = time.time()
        silence_threshold = 60.0  # 60秒
        
        all_silent = True
        silent_agents = []
        
        for agent_id, state in states.items():
            last_speak = state.get("last_speak_time", 0)
            if now - last_speak < silence_threshold:
                all_silent = False
                break
            else:
                silent_agents.append(agent_id)
        
        if all_silent and silent_agents:
            event = StallEvent(
                stall_type=StallType.AGENT_SILENCE,
                timestamp=time.time(),
                details={
                    "silent_agents": silent_agents,
                    "silence_duration": silence_threshold
                }
            )
            
            await self._handle_stall(event)
    
    async def _check_massive_repetition(self):
        """检测大规模重复"""
        result = self.whiteboard.check_massive_repetition(
            window_size=self.config.massive_repetition_window,
            duplicate_threshold=self.config.repetition_threshold,
            agent_ratio_threshold=self.config.massive_repetition_agent_ratio
        )
        
        if result["should_skip_agenda"]:
            event = StallEvent(
                stall_type=StallType.MASSIVE_REPETITION,
                timestamp=time.time(),
                details={
                    "duplicate_agents": result["duplicate_agents"],
                    "duplicate_count": result["duplicate_count"],
                    "total_agents": result["total_agents"],
                    "duplicate_ratio": result["duplicate_ratio"],
                    "window_size": result["window_size"]
                }
            )
            
            await self._handle_stall(event)
    
    def check_repetition(self, content: str, agent_id: str) -> Dict:
        """检测内容重复"""
        result = self.whiteboard.check_duplicate_content(
            content, 
            agent_id, 
            self.config.repetition_threshold
        )
        
        if result["is_duplicate"]:
            # 更新重复计数
            self._duplicate_counts[agent_id] = self._duplicate_counts.get(agent_id, 0) + 1
            
            # 检查是否达到连续重复限制
            if self._duplicate_counts[agent_id] >= self.config.max_consecutive_duplicates:
                return {
                    "is_duplicate": True,
                    "consecutive_count": self._duplicate_counts[agent_id],
                    "action_required": "throttle",  # 需要限流
                    **result
                }
        else:
            # 重置计数
            self._duplicate_counts[agent_id] = 0
        
        return result
    
    async def _handle_stall(self, event: StallEvent):
        """处理停滞事件"""
        self._stall_events.append(event)
        logger.warning(f"检测到停滞: {event.stall_type.value}, 详情: {event.details}")
        
        # 大规模重复：直接跳过议程
        if event.stall_type == StallType.MASSIVE_REPETITION:
            await self._handle_massive_repetition(event)
            return
        
        # 调用回调
        if self._on_stall_detected:
            await self._on_stall_detected(event)
        
        # 尝试收敛
        await self._try_convergence(event)
    
    async def _handle_massive_repetition(self, event: StallEvent):
        """处理大规模重复：触发投票选出最优方案，其他进入备选"""
        logger.info(f"大规模重复检测，触发投票: {event.details}")
        
        # 触发投票选出最优方案
        result = self.whiteboard.trigger_voting_by_repetition(
            duplicate_count=event.details.get("duplicate_count", 0),
            total_agents=event.details.get("total_agents", 0),
            window_size=self.config.massive_repetition_window
        )
        
        if result.get("triggered"):
            event.action_taken = "voting_triggered"
            event.details["vote_options_count"] = len(result.get("vote_options", []))
            event.details["backup_options_count"] = len(result.get("backup_viewpoints", []))
        else:
            event.action_taken = "voting_not_needed"
            event.details["reason"] = result.get("reason", "unknown")
        
        # 重置重复计数
        self.whiteboard.reset_all_duplicate_counts()
        self._duplicate_counts.clear()
        
        # 调用回调
        if self._on_stall_detected:
            await self._on_stall_detected(event)
    
    async def _try_convergence(self, event: StallEvent):
        """尝试收敛讨论"""
        self._convergence_attempts += 1
        
        if self._convergence_attempts > self.config.max_convergence_attempts:
            # 超过最大尝试次数，强制结束
            logger.warning("收敛尝试次数超限，强制结束讨论")
            
            if self._on_force_end:
                await self._on_force_end(event)
            
            event.action_taken = "forced_end"
            return
        
        # 生成总结提案
        summary = await self._generate_summary_proposal()
        
        if summary:
            # 触发投票
            event.action_taken = f"convergence_attempt_{self._convergence_attempts}"
            logger.info(f"触发收敛投票 (尝试 {self._convergence_attempts})")
    
    async def _generate_summary_proposal(self) -> Optional[str]:
        """生成总结提案"""
        # 获取最近的讨论内容
        messages = self.whiteboard.get_messages(-10)  # 最近10条消息
        
        if not messages:
            return None
        
        # 简单总结：提取关键观点
        summary_parts = []
        for msg in messages[-5:]:
            content = getattr(msg, 'content', str(msg))[:100]
            agent_id = getattr(msg, 'agent_id', 'unknown')
            summary_parts.append(f"- {agent_id}: {content}...")
        
        return "\n".join(summary_parts)
    
    def reset_idle_timer(self):
        """重置空闲计时器"""
        self.whiteboard.reset_activity_timer()
        self._convergence_attempts = 0  # 重置收敛尝试
    
    def get_stall_history(self, limit: int = 10) -> List[Dict]:
        """获取停滞事件历史"""
        return [e.to_dict() for e in self._stall_events[-limit:]]
    
    def get_stats(self) -> Dict:
        """获取停滞检测统计"""
        return {
            "total_stalls": len(self._stall_events),
            "convergence_attempts": self._convergence_attempts,
            "by_type": self._count_by_type(),
            "duplicate_counts": dict(self._duplicate_counts),
            "is_running": self._running
        }
    
    def _count_by_type(self) -> Dict[str, int]:
        """按类型统计停滞事件"""
        counts = {}
        for event in self._stall_events:
            t = event.stall_type.value
            counts[t] = counts.get(t, 0) + 1
        return counts
    
    def reset(self):
        """重置检测器状态"""
        self._stall_events.clear()
        self._convergence_attempts = 0
        self._duplicate_counts.clear()
        self.whiteboard.reset_activity_timer()


# 全局实例
_stall_detector: Optional[StallDetector] = None


def get_stall_detector(whiteboard=None, config: StallConfig = None) -> StallDetector:
    """获取停滞检测器实例"""
    global _stall_detector
    if _stall_detector is None and whiteboard:
        _stall_detector = StallDetector(whiteboard, config)
    return _stall_detector


def reset_stall_detector():
    """重置停滞检测器"""
    global _stall_detector
    _stall_detector = None
