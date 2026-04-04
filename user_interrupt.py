"""用户插话管理器 - 优先级控制、打断处理、冷却机制"""
import time
import asyncio
import re
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Any, Callable
from enum import Enum
from collections import deque
import logging

logger = logging.getLogger(__name__)


# 默认消息模板
DEFAULT_MSG = {
    "pause": "已暂停所有代理发言，使用 /resume 恢复",
    "resume": "已恢复讨论",
    "vote": "已发起投票: {args}",
    "vote_default": "已发起投票: 立即表决",
    "stop": "已结束会话: {args}",
    "mute": "已静音代理: {agent_id}",
    "unmute": "已取消静音代理: {agent_id}",
    "clear": "已清空白板，讨论重新开始",
    "mode": "已切换到模式: {mode}",
    "force": "强制打断: {args}",
    "abort_vote": "已取消当前投票，恢复讨论",
    "weights": "显示代理权重...",
    "history": "显示插话历史...",
    "readonly_on": "已切换到只读模式",
    "readonly_off": "已退出只读模式",
    "unknown": "未知命令: {command}",
    "cooldown": "冷却中，剩余 {remaining:.1f} 秒",
    "readonly_mode": "用户处于只读模式",
    "reputation_low": "用户声誉过低 ({reputation:.0f})",
    "force_limit": "超过每小时强制打断限制",
    "voting_restricted": "投票期间，已转为建议模式",
    "converted": "冷却中，已转为建议型",
    # 思考暂停相关
    "skip_think": "已跳过当前思考暂停",
    "skip_think_none": "当前没有进行中的思考暂停",
    "think_min_gain": "已设置思考最小收益阈值为: {threshold}",
    "think_min_gain_invalid": "无效的阈值，请输入0-100之间的数字",
    "disable_think": "已禁用思考暂停功能",
    "enable_think": "已启用思考暂停功能",
    "think_status": "思考暂停状态: {status}",
    "think_status_active": "代理 {agent_id} 正在思考，剩余 {remaining:.1f} 秒",
    "think_status_inactive": "当前没有思考暂停",
    "think_queue": "思考队列: {count} 个等待中",
    "think_history": "代理 {agent_id} 已请求 {count} 次思考暂停",
    "agenda_next": "已跳过当前议程，进入下一议程",
    "agenda_end": "当前议程已结束",
    "agenda_none": "没有更多议程",
    "mode_switch_blocked": "模式切换冷却中，剩余 {remaining:.0f} 秒"
}


class InterruptType(Enum):
    """插话类型"""
    INTERRUPT = "interrupt"      # 打断型 - 立即中断当前发言
    COMMAND = "command"          # 指令型 - 执行系统命令
    SUGGESTION = "suggestion"    # 建议型 - @代理
    FORCE = "force"              # 强制打断 - 忽略冷却


class InterruptPriority(Enum):
    """插话优先级"""
    USER_INTERRUPT = 100      # 用户打断（最高）
    USER_FORCE = 99           # 用户强制打断
    USER_SUGGESTION = 90      # 用户建议
    USER_COMMAND = 80         # 用户指令
    AGENT_INTERRUPT = 70      # 代理打断
    AGENT_SPEECH = 50         # 代理普通发言
    AGENT_SUGGESTION = 30     # 代理建议


@dataclass
class InterruptEvent:
    """插话事件"""
    event_id: str
    interrupt_type: InterruptType
    content: str
    timestamp: float
    priority: int
    target_agents: List[str] = field(default_factory=list)
    is_force: bool = False
    metadata: Dict = field(default_factory=dict)
    
    def to_dict(self) -> Dict:
        return {
            "event_id": self.event_id,
            "type": self.interrupt_type.value,
            "content": self.content[:100],
            "timestamp": datetime.fromtimestamp(self.timestamp).isoformat(),
            "priority": self.priority,
            "target_agents": self.target_agents,
            "is_force": self.is_force
        }


@dataclass
class InterruptRecord:
    """插话记录"""
    timestamp: float
    interrupt_type: InterruptType
    content: str
    target_agents: List[str]
    was_processed: bool = True
    interrupted_agent: Optional[str] = None


@dataclass
class CooldownState:
    """冷却状态"""
    last_interrupt_time: float = 0
    cooldown_seconds: float = 3.0
    force_interrupts_today: int = 0
    max_force_per_hour: int = 5
    force_reset_time: float = 0
    is_readonly: bool = False


@dataclass
class InterruptConfig:
    """插话配置"""
    # 默认冷却时间
    default_cooldown_seconds: float = 3.0
    
    # 强制打断限制
    max_force_per_hour: int = 5
    max_force_per_day: int = 10
    force_ban_duration_minutes: int = 10
    
    # 建议模式
    force_suggest_prefix: str = "@!"
    suggest_prefix: str = "@"
    all_agents_tag: str = "@all"
    
    # 用户声誉（可选）
    enable_reputation: bool = False
    default_reputation: float = 100.0
    min_reputation_for_force: float = 50.0
    
    # 投票期间限制
    restrict_during_voting: bool = True
    
    # 消息模板（可自定义语言）
    messages: Dict[str, str] = field(default_factory=lambda: DEFAULT_MSG.copy())


class UserInterruptManager:
    """用户插话管理器"""
    
    def __init__(self, config: InterruptConfig = None, messages: Dict[str, str] = None):
        self.config = config or InterruptConfig()
        self._messages = messages or self.config.messages or DEFAULT_MSG.copy()
        
        # 冷却状态（按用户ID）
        self._cooldown_states: Dict[str, CooldownState] = {}
        
        # 插话历史
        self._interrupt_history: Dict[str, List[InterruptRecord]] = {}
        
        # 当前打断状态
        self._current_interrupt: Optional[InterruptEvent] = None
        
        # 被中断的代理
        self._interrupted_agents: Set[str] = set()
        
        # 用户声誉
        self._user_reputation: Dict[str, float] = {}
        
        # 投票状态
        self._is_voting: bool = False
        
        # 命令处理器
        self._command_handlers: Dict[str, Callable] = {}
        
        # 事件计数器
        self._event_counter = 0
        
        # 思考暂停控制
        self._think_enabled: bool = True
        self._think_min_gain: int = 10  # 新观点最小收益阈值
        
        # 白板引用（用于思考暂停控制）
        self._whiteboard = None
        
        # 注册默认命令
        self._register_default_commands()
    
    def set_whiteboard(self, whiteboard):
        """设置白板引用"""
        self._whiteboard = whiteboard
    
    def _register_default_commands(self):
        """注册默认命令处理器"""
        self._command_handlers = {
            "pause": self._cmd_pause,
            "resume": self._cmd_resume,
            "vote": self._cmd_vote,
            "stop": self._cmd_stop,
            "mute": self._cmd_mute,
            "unmute": self._cmd_unmute,
            "clear": self._cmd_clear,
            "mode": self._cmd_mode,
            "force": self._cmd_force,
            "abort_vote": self._cmd_abort_vote,
            "weights": self._cmd_weights,
            "history": self._cmd_history,
            "readonly": self._cmd_readonly,
            # 思考暂停相关命令
            "skip_think": self._cmd_skip_think,
            "think_min_gain": self._cmd_think_min_gain,
            "disable_think": self._cmd_disable_think,
            "enable_think": self._cmd_enable_think,
            "think_status": self._cmd_think_status,
            "think_history": self._cmd_think_history,
            # 议程控制
            "agenda_next": self._cmd_agenda_next,
            "agenda_status": self._cmd_agenda_status,
        }
    
    def parse_input(
        self,
        user_input: str,
        user_id: str = "user"
    ) -> Tuple[InterruptType, InterruptEvent]:
        """
        解析用户输入，判断插话类型
        
        Args:
            user_input: 用户输入文本
            user_id: 用户ID
            
        Returns:
            (interrupt_type, event): 插话类型和事件
        """
        self._event_counter += 1
        event_id = f"int_{self._event_counter}_{int(time.time())}"
        
        content = user_input.strip()
        
        # 1. 检查是否为指令型（以/开头）
        if content.startswith("/"):
            return self._create_command_event(event_id, content, user_id)
        
        # 2. 检查是否为建议型（@代理）
        if self.config.suggest_prefix in content:
            return self._create_suggestion_event(event_id, content, user_id)
        
        # 3. 检查是否为强制打断（以!开头）
        if content.startswith("!") or content.startswith("/force"):
            return self._create_force_event(event_id, content, user_id)
        
        # 4. 默认为打断型
        return self._create_interrupt_event(event_id, content, user_id)
    
    def _create_command_event(
        self,
        event_id: str,
        content: str,
        user_id: str
    ) -> Tuple[InterruptType, InterruptEvent]:
        """创建指令事件"""
        # 解析命令
        command_text = content[1:].strip()  # 移除/
        parts = command_text.split(maxsplit=1)
        command = parts[0].lower() if parts else ""
        args = parts[1] if len(parts) > 1 else ""
        
        event = InterruptEvent(
            event_id=event_id,
            interrupt_type=InterruptType.COMMAND,
            content=content,
            timestamp=time.time(),
            priority=InterruptPriority.USER_COMMAND.value,
            metadata={
                "command": command,
                "args": args,
                "user_id": user_id
            }
        )
        
        return InterruptType.COMMAND, event
    
    def _create_suggestion_event(
        self,
        event_id: str,
        content: str,
        user_id: str
    ) -> Tuple[InterruptType, InterruptEvent]:
        """创建建议事件"""
        # 解析目标代理
        target_agents = []
        is_force_suggest = self.config.force_suggest_prefix in content
        
        # 匹配 @代理ID 或 @!代理ID
        pattern = r'@(?:!)?(\w+)'
        matches = re.findall(pattern, content)
        
        for match in matches:
            if match.lower() == "all":
                # @all 表示所有代理
                target_agents = ["*"]  # 特殊标记
                break
            else:
                target_agents.append(match)
        
        # 移除@标记，保留纯建议内容
        clean_content = re.sub(r'@(?:!)?\w+\s*', '', content).strip()
        
        priority = InterruptPriority.USER_SUGGESTION.value
        if is_force_suggest:
            priority = InterruptPriority.USER_FORCE.value
        
        event = InterruptEvent(
            event_id=event_id,
            interrupt_type=InterruptType.SUGGESTION,
            content=clean_content or content,
            timestamp=time.time(),
            priority=priority,
            target_agents=target_agents,
            is_force=is_force_suggest,
            metadata={
                "raw_content": content,
                "user_id": user_id
            }
        )
        
        return InterruptType.SUGGESTION, event
    
    def _create_force_event(
        self,
        event_id: str,
        content: str,
        user_id: str
    ) -> Tuple[InterruptType, InterruptEvent]:
        """创建强制打断事件"""
        # 检查强制打断限制
        can_force, reason = self._check_force_limit(user_id)
        
        if not can_force:
            # 降级为普通打断
            logger.warning(f"强制打断被拒绝: {reason}")
            return self._create_interrupt_event(event_id, content.lstrip('!'), user_id)
        
        # 记录强制打断
        self._record_force_interrupt(user_id)
        
        event = InterruptEvent(
            event_id=event_id,
            interrupt_type=InterruptType.FORCE,
            content=content.lstrip('!').lstrip('/force').strip(),
            timestamp=time.time(),
            priority=InterruptPriority.USER_FORCE.value,
            is_force=True,
            metadata={
                "user_id": user_id,
                "force_used": self._get_force_count(user_id)
            }
        )
        
        return InterruptType.FORCE, event
    
    def _create_interrupt_event(
        self,
        event_id: str,
        content: str,
        user_id: str
    ) -> Tuple[InterruptType, InterruptEvent]:
        """创建打断事件"""
        # 检查冷却
        if self._is_in_cooldown(user_id):
            # 转为建议型
            logger.info(f"用户 {user_id} 冷却中，转为建议型")
            event = InterruptEvent(
                event_id=event_id,
                interrupt_type=InterruptType.SUGGESTION,
                content=content,
                timestamp=time.time(),
                priority=InterruptPriority.USER_SUGGESTION.value,
                target_agents=["*"],
                metadata={
                    "user_id": user_id,
                    "cooldown_converted": True
                }
            )
            return InterruptType.SUGGESTION, event
        
        # 检查投票期间限制
        if self._is_voting and self.config.restrict_during_voting:
            event = InterruptEvent(
                event_id=event_id,
                interrupt_type=InterruptType.SUGGESTION,
                content=content,
                timestamp=time.time(),
                priority=InterruptPriority.USER_SUGGESTION.value,
                target_agents=["*"],
                metadata={
                    "user_id": user_id,
                    "voting_restricted": True
                }
            )
            return InterruptType.SUGGESTION, event
        
        # 记录打断并启动冷却
        self._start_cooldown(user_id)
        
        event = InterruptEvent(
            event_id=event_id,
            interrupt_type=InterruptType.INTERRUPT,
            content=content,
            timestamp=time.time(),
            priority=InterruptPriority.USER_INTERRUPT.value,
            metadata={
                "user_id": user_id
            }
        )
        
        return InterruptType.INTERRUPT, event
    
    def can_interrupt(self, user_id: str = "user") -> Tuple[bool, str]:
        """
        检查是否可以打断
        
        Returns:
            (can_interrupt, reason): 是否可以打断及原因
        """
        # 检查只读模式
        state = self._get_cooldown_state(user_id)
        if state.is_readonly:
            return False, self._msg("readonly_mode")
        
        # 检查冷却
        if self._is_in_cooldown(user_id):
            remaining = self._get_remaining_cooldown(user_id)
            return False, self._msg("cooldown", remaining=remaining)
        
        # 检查声誉（如果启用）
        if self.config.enable_reputation:
            reputation = self._user_reputation.get(user_id, self.config.default_reputation)
            if reputation < self.config.min_reputation_for_force:
                return False, self._msg("reputation_low", reputation=reputation)
        
        return True, ""
    
    def _is_in_cooldown(self, user_id: str) -> bool:
        """检查是否在冷却期"""
        state = self._get_cooldown_state(user_id)
        if state.last_interrupt_time == 0:
            return False
        
        elapsed = time.time() - state.last_interrupt_time
        return elapsed < state.cooldown_seconds
    
    def _get_remaining_cooldown(self, user_id: str) -> float:
        """获取剩余冷却时间"""
        state = self._get_cooldown_state(user_id)
        if state.last_interrupt_time == 0:
            return 0.0
        
        elapsed = time.time() - state.last_interrupt_time
        remaining = state.cooldown_seconds - elapsed
        return max(0.0, remaining)
    
    def _start_cooldown(self, user_id: str):
        """启动冷却"""
        state = self._get_cooldown_state(user_id)
        state.last_interrupt_time = time.time()
    
    def _get_cooldown_state(self, user_id: str) -> CooldownState:
        """获取冷却状态"""
        if user_id not in self._cooldown_states:
            self._cooldown_states[user_id] = CooldownState(
                cooldown_seconds=self.config.default_cooldown_seconds
            )
        return self._cooldown_states[user_id]
    
    def _check_force_limit(self, user_id: str) -> Tuple[bool, str]:
        """检查强制打断限制"""
        state = self._get_cooldown_state(user_id)
        
        # 重置小时计数
        now = time.time()
        if now - state.force_reset_time > 3600:
            state.force_interrupts_today = 0
            state.force_reset_time = now
        
        if state.force_interrupts_today >= self.config.max_force_per_hour:
            return False, self._msg("force_limit")
        
        return True, ""
    
    def _record_force_interrupt(self, user_id: str):
        """记录强制打断"""
        state = self._get_cooldown_state(user_id)
        state.force_interrupts_today += 1
    
    def _get_force_count(self, user_id: str) -> int:
        """获取强制打断次数"""
        state = self._get_cooldown_state(user_id)
        return state.force_interrupts_today
    
    def record_interrupt(
        self,
        user_id: str,
        interrupt_type: InterruptType,
        content: str,
        target_agents: List[str] = None,
        interrupted_agent: str = None
    ):
        """记录插话"""
        if user_id not in self._interrupt_history:
            self._interrupt_history[user_id] = []
        
        record = InterruptRecord(
            timestamp=time.time(),
            interrupt_type=interrupt_type,
            content=content[:200],
            target_agents=target_agents or [],
            interrupted_agent=interrupted_agent
        )
        
        self._interrupt_history[user_id].append(record)
    
    def set_voting_mode(self, is_voting: bool):
        """设置投票模式"""
        self._is_voting = is_voting
    
    def set_readonly(self, user_id: str, readonly: bool):
        """设置只读模式"""
        state = self._get_cooldown_state(user_id)
        state.is_readonly = readonly
        logger.info(f"用户 {user_id} 只读模式: {readonly}")
    
    def adjust_reputation(self, user_id: str, delta: float):
        """调整用户声誉"""
        current = self._user_reputation.get(user_id, self.config.default_reputation)
        new_value = max(0, min(100, current + delta))
        self._user_reputation[user_id] = new_value
    
    def get_user_priority(self, user_id: str) -> int:
        """获取用户当前优先级"""
        base_priority = InterruptPriority.USER_INTERRUPT.value
        
        if self.config.enable_reputation:
            reputation = self._user_reputation.get(user_id, self.config.default_reputation)
            # 声誉影响优先级：低声誉降低优先级
            if reputation < 50:
                return InterruptPriority.USER_SUGGESTION.value
        
        return base_priority
    
    def get_interrupt_history(
        self,
        user_id: str = None,
        limit: int = 20
    ) -> List[Dict]:
        """获取插话历史"""
        if user_id:
            records = self._interrupt_history.get(user_id, [])
        else:
            # 合并所有用户的记录
            all_records = []
            for records in self._interrupt_history.values():
                all_records.extend(records)
            records = sorted(all_records, key=lambda x: x.timestamp, reverse=True)
        
        return [
            {
                "timestamp": datetime.fromtimestamp(r.timestamp).isoformat(),
                "type": r.interrupt_type.value,
                "content": r.content[:100],
                "targets": r.target_agents,
                "interrupted": r.interrupted_agent
            }
            for r in records[-limit:]
        ]
    
    def get_user_stats(self, user_id: str) -> Dict:
        """获取用户插话统计"""
        state = self._get_cooldown_state(user_id)
        history = self._interrupt_history.get(user_id, [])
        
        # 统计各类型数量
        type_counts = {}
        for record in history:
            t = record.interrupt_type.value
            type_counts[t] = type_counts.get(t, 0) + 1
        
        return {
            "user_id": user_id,
            "total_interrupts": len(history),
            "by_type": type_counts,
            "force_interrupts_today": state.force_interrupts_today,
            "force_limit": self.config.max_force_per_hour,
            "is_readonly": state.is_readonly,
            "in_cooldown": self._is_in_cooldown(user_id),
            "remaining_cooldown": self._get_remaining_cooldown(user_id),
            "reputation": self._user_reputation.get(user_id, self.config.default_reputation)
        }
    
    def get_priority_queue_position(
        self,
        event: InterruptEvent,
        other_events: List[Any]
    ) -> int:
        """计算在优先级队列中的位置"""
        # 按优先级排序，优先级高的排前面
        higher_priority = sum(
            1 for e in other_events
            if getattr(e, 'priority', 0) > event.priority
        )
        return higher_priority
    
    # ========== 命令处理器 ==========
    
    def _msg(self, key: str, **kwargs) -> str:
        """获取格式化的消息"""
        template = self._messages.get(key, DEFAULT_MSG.get(key, key))
        return template.format(**kwargs)
    
    async def execute_command(
        self,
        command: str,
        args: str,
        context: Dict
    ) -> Dict:
        """执行命令"""
        handler = self._command_handlers.get(command)
        if not handler:
            return {
                "success": False,
                "error": self._msg("unknown", command=command)
            }
        
        try:
            result = await handler(args, context)
            return {"success": True, "result": result}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def _cmd_pause(self, args: str, context: Dict) -> str:
        """暂停命令"""
        return self._msg("pause")
    
    async def _cmd_resume(self, args: str, context: Dict) -> str:
        """恢复命令"""
        return self._msg("resume")
    
    async def _cmd_vote(self, args: str, context: Dict) -> str:
        """发起投票命令"""
        if args:
            return self._msg("vote", args=args)
        return self._msg("vote_default")
    
    async def _cmd_stop(self, args: str, context: Dict) -> str:
        """停止命令"""
        return self._msg("stop", args=args if args else "")
    
    async def _cmd_mute(self, args: str, context: Dict) -> str:
        """静音命令"""
        agent_id = args.strip()
        return self._msg("mute", agent_id=agent_id)
    
    async def _cmd_unmute(self, args: str, context: Dict) -> str:
        """取消静音命令"""
        agent_id = args.strip()
        return self._msg("unmute", agent_id=agent_id)
    
    async def _cmd_clear(self, args: str, context: Dict) -> str:
        """清空命令"""
        return self._msg("clear")
    
    async def _cmd_mode(self, args: str, context: Dict) -> str:
        """切换模式命令"""
        mode = args.strip().lower()
        return self._msg("mode", mode=mode)
    
    async def _cmd_force(self, args: str, context: Dict) -> str:
        """强制打断命令"""
        return self._msg("force", args=args)
    
    async def _cmd_abort_vote(self, args: str, context: Dict) -> str:
        """取消投票命令"""
        return self._msg("abort_vote")
    
    async def _cmd_weights(self, args: str, context: Dict) -> str:
        """查看权重命令"""
        return self._msg("weights")
    
    async def _cmd_history(self, args: str, context: Dict) -> str:
        """查看历史命令"""
        return self._msg("history")
    
    async def _cmd_readonly(self, args: str, context: Dict) -> str:
        """只读模式命令"""
        # 切换只读状态
        user_id = context.get("user_id", "user")
        current_readonly = self._get_cooldown_state(user_id).is_readonly
        new_readonly = not current_readonly
        self.set_readonly(user_id, new_readonly)
        return self._msg("readonly_on" if new_readonly else "readonly_off")
    
    # ==================== 思考暂停相关命令 ====================
    
    async def _cmd_skip_think(self, args: str, context: Dict) -> str:
        """跳过当前思考暂停"""
        if not self._whiteboard:
            return self._msg("skip_think_none")
        
        status = self._whiteboard.get_think_pause_status()
        if not status.get("active"):
            return self._msg("skip_think_none")
        
        # 结束思考暂停
        result = self._whiteboard.end_think_pause()
        if result.get("ended"):
            # 不授予优先发言权（用户主动跳过）
            self._whiteboard._think_priority.pop(result.get("agent_id"), None)
            return self._msg("skip_think")
        
        return self._msg("skip_think_none")
    
    async def _cmd_think_min_gain(self, args: str, context: Dict) -> str:
        """设置思考最小收益阈值"""
        try:
            threshold = int(args.strip())
            if 0 <= threshold <= 100:
                self._think_min_gain = threshold
                return self._msg("think_min_gain", threshold=threshold)
        except ValueError:
            pass
        return self._msg("think_min_gain_invalid")
    
    async def _cmd_disable_think(self, args: str, context: Dict) -> str:
        """禁用思考暂停功能"""
        self._think_enabled = False
        return self._msg("disable_think")
    
    async def _cmd_enable_think(self, args: str, context: Dict) -> str:
        """启用思考暂停功能"""
        self._think_enabled = True
        return self._msg("enable_think")
    
    async def _cmd_think_status(self, args: str, context: Dict) -> str:
        """查看思考暂停状态"""
        if not self._whiteboard:
            return self._msg("think_status_inactive")
        
        status = self._whiteboard.get_think_pause_status()
        
        if status.get("active"):
            return self._msg(
                "think_status_active",
                agent_id=status.get("agent_id", "unknown"),
                remaining=status.get("time_remaining", 0)
            )
        else:
            # 检查队列
            queue = status.get("queue", [])
            if queue:
                return self._msg("think_queue", count=len(queue))
            return self._msg("think_status_inactive")
    
    async def _cmd_think_history(self, args: str, context: Dict) -> str:
        """查看代理的思考暂停历史"""
        if not self._whiteboard:
            return "白板未连接"
        
        agent_id = args.strip() if args.strip() else None
        
        if agent_id:
            history = self._whiteboard.get_think_logs(agent_id)
            return self._msg("think_history", agent_id=agent_id, count=len(history))
        else:
            # 显示所有代理的统计
            logs = self._whiteboard.get_think_logs()
            stats = {}
            for log in logs:
                aid = log.get("agent_id", "unknown")
                stats[aid] = stats.get(aid, 0) + 1
            
            result = "思考暂停统计:\n"
            for aid, count in stats.items():
                result += f"  {aid}: {count} 次\n"
            return result.strip()
    
    # ==================== 议程控制命令 ====================
    
    async def _cmd_agenda_next(self, args: str, context: Dict) -> str:
        """跳过当前议程"""
        if not self._whiteboard:
            return "白板未连接"
        
        # 检查是否有下一议程
        agenda_status = self._whiteboard.get_agenda_status_text()
        if "最后一项" in agenda_status or "无议程" in agenda_status:
            return self._msg("agenda_none")
        
        # 强制结束当前议程
        result = self._whiteboard.advance_agenda()
        if result:
            return self._msg("agenda_next")
        return self._msg("agenda_none")
    
    async def _cmd_agenda_status(self, args: str, context: Dict) -> str:
        """查看议程状态"""
        if not self._whiteboard:
            return "白板未连接"
        
        return self._whiteboard.get_agenda_status_text()
    
    def is_think_enabled(self) -> bool:
        """检查思考暂停功能是否启用"""
        return self._think_enabled
    
    def get_think_min_gain(self) -> int:
        """获取思考最小收益阈值"""
        return self._think_min_gain
    
    def check_new_viewpoint_gain(self, old_content: str, new_content: str) -> int:
        """检查新观点的收益分数（0-100）"""
        # 简单的实现：基于新增关键词和语义差异
        old_words = set(old_content.lower().split())
        new_words = set(new_content.lower().split())
        
        # 新增的词汇
        new_terms = new_words - old_words
        # 移除的词汇
        removed_terms = old_words - new_words
        
        # 计算差异比例
        total_old = len(old_words) if old_words else 1
        diff_ratio = (len(new_terms) + len(removed_terms)) / total_old
        
        # 转换为0-100的分数
        return min(100, int(diff_ratio * 100))


# 全局实例
_interrupt_manager: Optional[UserInterruptManager] = None


def get_interrupt_manager(config: InterruptConfig = None, messages: Dict[str, str] = None) -> UserInterruptManager:
    """获取插话管理器实例"""
    global _interrupt_manager
    if _interrupt_manager is None:
        _interrupt_manager = UserInterruptManager(config, messages)
    return _interrupt_manager


def reset_interrupt_manager():
    """重置插话管理器"""
    global _interrupt_manager
    _interrupt_manager = None
