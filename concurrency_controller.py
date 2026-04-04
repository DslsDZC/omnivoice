"""并发控制器 - 代理分组、休眠唤醒、并行执行"""
import asyncio
import time
from typing import Dict, List, Optional, Set, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict
import threading


class AgentGroup(Enum):
    """代理分组"""
    HIGH_FREQ = "high_freq"     # 高频组：每轮都发言
    MID_FREQ = "mid_freq"       # 中频组：每2轮发言
    LOW_FREQ = "low_freq"       # 低频组：每3轮发言
    VOTER_ONLY = "voter_only"   # 只投票不发言


@dataclass
class AgentSchedule:
    """代理调度配置"""
    agent_id: str
    group: AgentGroup
    cost_tier: str = "medium"     # high/medium/low/free
    speak_interval: int = 1        # 发言间隔（轮次）
    last_speak_round: int = 0      # 上次发言轮次
    skip_count: int = 0            # 连续跳过次数


@dataclass
class AgentState:
    """代理运行状态"""
    agent_id: str
    is_sleeping: bool = False      # 是否休眠
    sleep_reason: str = ""         # 休眠原因
    wake_keywords: List[str] = field(default_factory=list)  # 唤醒关键词
    consecutive_silent: int = 0    # 连续沉默轮次
    last_active_time: float = 0.0  # 最后活跃时间
    total_calls: int = 0           # 总调用次数
    total_timeouts: int = 0        # 超时次数


class ConcurrencyController:
    """并发控制器"""
    
    # 默认休眠阈值（连续沉默轮次）
    DEFAULT_SLEEP_THRESHOLD = 5
    
    # 最大并发数
    MAX_CONCURRENT = 16
    
    def __init__(self, max_concurrent: int = 16):
        self.max_concurrent = max_concurrent
        
        # 调度配置
        self._schedules: Dict[str, AgentSchedule] = {}
        
        # 运行状态
        self._states: Dict[str, AgentState] = {}
        
        # 当前轮次
        self._current_round: int = 0
        
        # 分组默认配置
        self._group_config = {
            AgentGroup.HIGH_FREQ: {"interval": 1, "count": 4},
            AgentGroup.MID_FREQ: {"interval": 2, "count": 6},
            AgentGroup.LOW_FREQ: {"interval": 3, "count": 6},
            AgentGroup.VOTER_ONLY: {"interval": 999, "count": 0},
        }
        
        # 并发控制
        self._semaphore: Optional[asyncio.Semaphore] = None
        
        # 锁
        self._lock = threading.RLock()
        
        # 统计
        self._round_stats: List[Dict] = []
    
    # ==================== 代理注册 ====================
    
    def register_agent(self, agent_id: str, group: AgentGroup = AgentGroup.MID_FREQ,
                        cost_tier: str = "medium", wake_keywords: List[str] = None):
        """注册代理"""
        with self._lock:
            interval = self._group_config.get(group, {}).get("interval", 1)
            
            self._schedules[agent_id] = AgentSchedule(
                agent_id=agent_id,
                group=group,
                cost_tier=cost_tier,
                speak_interval=interval
            )
            
            self._states[agent_id] = AgentState(
                agent_id=agent_id,
                wake_keywords=wake_keywords or []
            )
    
    def unregister_agent(self, agent_id: str):
        """注销代理"""
        with self._lock:
            self._schedules.pop(agent_id, None)
            self._states.pop(agent_id, None)
    
    # ==================== 分组管理 ====================
    
    def set_agent_group(self, agent_id: str, group: AgentGroup):
        """设置代理分组"""
        with self._lock:
            if agent_id in self._schedules:
                self._schedules[agent_id].group = group
                interval = self._group_config.get(group, {}).get("interval", 1)
                self._schedules[agent_id].speak_interval = interval
    
    def get_agents_by_group(self, group: AgentGroup) -> List[str]:
        """获取指定分组的代理"""
        with self._lock:
            return [
                aid for aid, schedule in self._schedules.items()
                if schedule.group == group
            ]
    
    def auto_distribute_groups(self, agent_ids: List[str],
                                high_count: int = 4,
                                mid_count: int = 6,
                                low_count: int = 6):
        """自动分配代理到各组"""
        with self._lock:
            n = len(agent_ids)
            
            # 高频组
            for i, aid in enumerate(agent_ids[:high_count]):
                if aid in self._schedules:
                    self._schedules[aid].group = AgentGroup.HIGH_FREQ
                    self._schedules[aid].speak_interval = 1
            
            # 中频组
            for i, aid in enumerate(agent_ids[high_count:high_count + mid_count]):
                if aid in self._schedules:
                    self._schedules[aid].group = AgentGroup.MID_FREQ
                    self._schedules[aid].speak_interval = 2
            
            # 低频组
            for i, aid in enumerate(agent_ids[high_count + mid_count:]):
                if aid in self._schedules:
                    self._schedules[aid].group = AgentGroup.LOW_FREQ
                    self._schedules[aid].speak_interval = 3
    
    # ==================== 轮次管理 ====================
    
    def start_round(self) -> List[str]:
        """开始新轮次，返回应该发言的代理列表"""
        with self._lock:
            self._current_round += 1
            speaking_agents = []
            
            for agent_id, schedule in self._schedules.items():
                state = self._states.get(agent_id)
                if not state:
                    continue
                
                # 检查是否休眠
                if state.is_sleeping:
                    continue
                
                # 检查发言间隔
                rounds_since_last = self._current_round - schedule.last_speak_round
                if rounds_since_last >= schedule.speak_interval:
                    speaking_agents.append(agent_id)
            
            # 记录统计
            self._round_stats.append({
                "round": self._current_round,
                "speaking_count": len(speaking_agents),
                "sleeping_count": sum(1 for s in self._states.values() if s.is_sleeping),
                "timestamp": time.time()
            })
            
            return speaking_agents
    
    def record_speak(self, agent_id: str, success: bool = True):
        """记录代理发言"""
        with self._lock:
            if agent_id in self._schedules:
                self._schedules[agent_id].last_speak_round = self._current_round
                self._schedules[agent_id].skip_count = 0
            
            if agent_id in self._states:
                state = self._states[agent_id]
                state.last_active_time = time.time()
                state.consecutive_silent = 0
                state.total_calls += 1
                if not success:
                    state.total_timeouts += 1
    
    def record_skip(self, agent_id: str):
        """记录代理跳过发言"""
        with self._lock:
            if agent_id in self._schedules:
                self._schedules[agent_id].skip_count += 1
            
            if agent_id in self._states:
                self._states[agent_id].consecutive_silent += 1
                
                # 检查是否需要休眠
                if self._states[agent_id].consecutive_silent >= self.DEFAULT_SLEEP_THRESHOLD:
                    self._sleep_agent(agent_id, "连续沉默超过阈值")
    
    # ==================== 休眠/唤醒 ====================
    
    def _sleep_agent(self, agent_id: str, reason: str):
        """使代理休眠"""
        if agent_id in self._states:
            self._states[agent_id].is_sleeping = True
            self._states[agent_id].sleep_reason = reason
    
    def wake_agent(self, agent_id: str):
        """唤醒代理"""
        with self._lock:
            if agent_id in self._states:
                self._states[agent_id].is_sleeping = False
                self._states[agent_id].sleep_reason = ""
                self._states[agent_id].consecutive_silent = 0
    
    def check_wake_keywords(self, content: str) -> List[str]:
        """检查内容中的唤醒关键词，返回需要唤醒的代理列表"""
        woken = []
        content_lower = content.lower()
        
        with self._lock:
            for agent_id, state in self._states.items():
                if not state.is_sleeping:
                    continue
                
                for keyword in state.wake_keywords:
                    if keyword.lower() in content_lower:
                        self.wake_agent(agent_id)
                        woken.append(agent_id)
                        break
        
        return woken
    
    def is_agent_sleeping(self, agent_id: str) -> bool:
        """检查代理是否休眠"""
        state = self._states.get(agent_id)
        return state.is_sleeping if state else False
    
    # ==================== 并发执行 ====================
    
    async def execute_parallel(self, agents_and_coros: List[Tuple[str, Any]],
                                timeout: float = 30.0) -> Dict[str, Any]:
        """并行执行多个代理的请求
        
        Args:
            agents_and_coros: [(agent_id, coroutine), ...]
            timeout: 单个请求超时时间
        
        Returns:
            {agent_id: result}
        """
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrent)
        
        async def _execute_one(agent_id: str, coro) -> Tuple[str, Any, bool]:
            """执行单个请求"""
            async with self._semaphore:
                try:
                    result = await asyncio.wait_for(coro, timeout=timeout)
                    self.record_speak(agent_id, success=True)
                    return agent_id, result, False
                except asyncio.TimeoutError:
                    self.record_speak(agent_id, success=False)
                    return agent_id, None, True
                except Exception as e:
                    self.record_speak(agent_id, success=False)
                    return agent_id, str(e), True
        
        # 并行执行所有请求
        tasks = [
            _execute_one(agent_id, coro)
            for agent_id, coro in agents_and_coros
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 整理结果
        output = {}
        for result in results:
            if isinstance(result, Exception):
                continue
            agent_id, value, is_error = result
            output[agent_id] = {
                "result": value,
                "error": is_error
            }
        
        return output
    
    # ==================== 统计 ====================
    
    def get_state(self, agent_id: str) -> Optional[AgentState]:
        """获取代理状态"""
        return self._states.get(agent_id)
    
    def get_all_states(self) -> Dict[str, AgentState]:
        """获取所有代理状态"""
        return dict(self._states)
    
    def get_schedule(self, agent_id: str) -> Optional[AgentSchedule]:
        """获取代理调度配置"""
        return self._schedules.get(agent_id)
    
    def get_group_distribution(self) -> Dict[str, int]:
        """获取分组分布"""
        distribution = defaultdict(int)
        for schedule in self._schedules.values():
            distribution[schedule.group.value] += 1
        return dict(distribution)
    
    def get_summary(self) -> Dict:
        """获取摘要"""
        active_count = sum(1 for s in self._states.values() if not s.is_sleeping)
        sleeping_count = sum(1 for s in self._states.values() if s.is_sleeping)
        
        return {
            "current_round": self._current_round,
            "total_agents": len(self._schedules),
            "active_agents": active_count,
            "sleeping_agents": sleeping_count,
            "group_distribution": self.get_group_distribution(),
            "max_concurrent": self.max_concurrent
        }
    
    def reset(self):
        """重置控制器"""
        with self._lock:
            self._current_round = 0
            self._round_stats.clear()
            
            for state in self._states.values():
                state.is_sleeping = False
                state.consecutive_silent = 0
                state.total_calls = 0
                state.total_timeouts = 0
            
            for schedule in self._schedules.values():
                schedule.last_speak_round = 0
                schedule.skip_count = 0


class ParallelExecutor:
    """并行执行器 - 简化的并行执行接口"""
    
    def __init__(self, controller: Optional[ConcurrencyController] = None):
        self.controller = controller or ConcurrencyController()
    
    async def execute_all(self, agent_ids: List[str], 
                          coro_factory,  # Callable[[agent_id], Coroutine]
                          filter_sleeping: bool = True,
                          filter_by_round: bool = True) -> Dict[str, Any]:
        """执行所有代理的请求
        
        Args:
            agent_ids: 代理ID列表
            coro_factory: 创建协程的工厂函数
            filter_sleeping: 是否过滤休眠代理
            filter_by_round: 是否根据轮次过滤
        """
        # 获取应该发言的代理
        if filter_by_round:
            speaking_agents = self.controller.start_round()
        else:
            speaking_agents = agent_ids
        
        # 过滤休眠代理
        if filter_sleeping:
            speaking_agents = [
                aid for aid in speaking_agents
                if not self.controller.is_agent_sleeping(aid)
            ]
        
        # 创建协程
        agents_and_coros = [
            (aid, coro_factory(aid))
            for aid in speaking_agents
        ]
        
        # 并行执行
        return await self.controller.execute_parallel(agents_and_coros)


# 全局控制器
_global_controller: Optional[ConcurrencyController] = None


def get_concurrency_controller() -> ConcurrencyController:
    """获取全局并发控制器"""
    global _global_controller
    if _global_controller is None:
        _global_controller = ConcurrencyController()
    return _global_controller
