"""工具安全控制器 - 限流、审计、权限管理"""
import os
import json
import time
import hashlib
import asyncio
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Set, Callable
from enum import Enum
from collections import defaultdict
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


class SecurityLevel(Enum):
    """安全级别"""
    STRICT = "strict"       # 严格模式：代码执行禁用
    STANDARD = "standard"   # 标准模式：沙箱内执行
    PERMISSIVE = "permissive"  # 宽松模式：允许受限网络


class ToolCategory(Enum):
    """工具类别"""
    READ_ONLY = "read_only"         # 只读工具
    WORKSPACE_RW = "workspace_rw"    # 工作区读写
    CODE_EXEC = "code_exec"          # 代码执行
    NETWORK = "network"              # 网络工具
    SYSTEM = "system"                # 系统工具


class AccessDecision(Enum):
    """访问决策"""
    ALLOW = "allow"
    DENY = "deny"
    RATE_LIMITED = "rate_limited"
    PERMISSION_DENIED = "permission_denied"
    SECURITY_VIOLATION = "security_violation"


@dataclass
class RateLimit:
    """速率限制配置"""
    max_calls: int = 10          # 最大调用次数
    window_seconds: int = 60     # 时间窗口（秒）
    cooldown_seconds: int = 60   # 冷却时间（秒）


@dataclass
class AuditEntry:
    """审计条目"""
    timestamp: datetime
    agent_id: str
    tool_name: str
    args: Dict[str, Any]
    result_summary: str
    success: bool
    execution_time: float
    decision: AccessDecision
    error: Optional[str] = None
    
    def to_dict(self) -> Dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "agent_id": self.agent_id,
            "tool_name": self.tool_name,
            "args": {k: str(v)[:100] for k, v in self.args.items()},  # 截断参数
            "result_summary": self.result_summary[:500],
            "success": self.success,
            "execution_time": self.execution_time,
            "decision": self.decision.value,
            "error": self.error
        }


@dataclass
class AgentPermission:
    """代理权限"""
    agent_id: str
    allowed_tools: Set[str]
    denied_tools: Set[str]
    temporary_grants: Dict[str, datetime] = field(default_factory=dict)  # 工具名 -> 过期时间
    violation_count: int = 0
    rate_limit_overrides: Dict[str, RateLimit] = field(default_factory=dict)
    
    def is_tool_allowed(self, tool_name: str) -> tuple:
        """
        检查工具是否被允许
        
        Returns:
            (allowed, reason): 是否允许及原因
        """
        # 检查是否在拒绝列表
        if tool_name in self.denied_tools:
            return False, "工具在拒绝列表中"
        
        # 检查是否在允许列表
        if tool_name in self.allowed_tools:
            return True, None
        
        # 检查临时授权
        if tool_name in self.temporary_grants:
            expiry = self.temporary_grants[tool_name]
            if datetime.now() < expiry:
                return True, None
            else:
                del self.temporary_grants[tool_name]
                return False, "临时授权已过期"
        
        return False, "工具不在允许列表中"
    
    def grant_temporary(self, tool_name: str, duration_seconds: int = 3600):
        """授予临时权限"""
        expiry = datetime.now() + timedelta(seconds=duration_seconds)
        self.temporary_grants[tool_name] = expiry
        logger.info(f"代理 {self.agent_id} 获得临时权限: {tool_name}，过期时间: {expiry}")
    
    def revoke_temporary(self, tool_name: str):
        """撤销临时权限"""
        if tool_name in self.temporary_grants:
            del self.temporary_grants[tool_name]
    
    def record_violation(self):
        """记录违规"""
        self.violation_count += 1


@dataclass
class SecurityConfig:
    """安全配置"""
    # 安全级别
    level: SecurityLevel = SecurityLevel.STANDARD
    
    # 默认速率限制
    default_rate_limit: RateLimit = field(default_factory=lambda: RateLimit(
        max_calls=10,
        window_seconds=60,
        cooldown_seconds=60
    ))
    
    # 全局工具速率限制
    global_rate_limit: RateLimit = field(default_factory=lambda: RateLimit(
        max_calls=100,
        window_seconds=60,
        cooldown_seconds=30
    ))
    
    # 审计配置
    audit_enabled: bool = True
    audit_log_path: str = "./logs/tool_audit.jsonl"
    max_audit_entries: int = 10000
    
    # 异常行为检测
    anomaly_detection: bool = True
    violation_threshold: int = 5      # 违规阈值
    auto_disable_duration: int = 300  # 自动禁用时长（秒）
    
    # 网络工具
    network_tools_enabled: bool = False
    network_whitelist: List[str] = field(default_factory=list)
    
    # 工具类别映射
    tool_categories: Dict[str, ToolCategory] = field(default_factory=dict)


class ToolSecurityController:
    """工具安全控制器"""
    
    # 默认工具类别映射
    DEFAULT_TOOL_CATEGORIES = {
        # 文件操作
        "temp_file_read": ToolCategory.READ_ONLY,
        "temp_file_write": ToolCategory.WORKSPACE_RW,
        "temp_file_delete": ToolCategory.WORKSPACE_RW,
        "temp_list_files": ToolCategory.READ_ONLY,
        
        # 代码执行
        "code_execute": ToolCategory.CODE_EXEC,
        "script_run": ToolCategory.CODE_EXEC,
        
        # 网络
        "web_search": ToolCategory.NETWORK,
        "web_fetch": ToolCategory.NETWORK,
        "http_request": ToolCategory.NETWORK,
        
        # 本地工具
        "calculator": ToolCategory.READ_ONLY,
        "time_query": ToolCategory.READ_ONLY,
        "document_search": ToolCategory.READ_ONLY,
    }
    
    # 基于类别的速率限制
    CATEGORY_RATE_LIMITS = {
        ToolCategory.READ_ONLY: RateLimit(max_calls=20, window_seconds=60, cooldown_seconds=30),
        ToolCategory.WORKSPACE_RW: RateLimit(max_calls=10, window_seconds=60, cooldown_seconds=60),
        ToolCategory.CODE_EXEC: RateLimit(max_calls=5, window_seconds=60, cooldown_seconds=120),
        ToolCategory.NETWORK: RateLimit(max_calls=10, window_seconds=60, cooldown_seconds=60),
        ToolCategory.SYSTEM: RateLimit(max_calls=3, window_seconds=60, cooldown_seconds=300),
    }
    
    def __init__(self, config: SecurityConfig = None):
        """
        初始化安全控制器
        
        Args:
            config: 安全配置
        """
        self.config = config or SecurityConfig()
        self.config.tool_categories = {**self.DEFAULT_TOOL_CATEGORIES, **self.config.tool_categories}
        
        # 代理权限
        self._agent_permissions: Dict[str, AgentPermission] = {}
        
        # 速率限制追踪
        self._agent_call_history: Dict[str, List[float]] = defaultdict(list)
        self._tool_call_history: Dict[str, List[float]] = defaultdict(list)
        self._agent_cooldowns: Dict[str, Dict[str, float]] = defaultdict(dict)
        
        # 审计日志
        self._audit_log: List[AuditEntry] = []
        self._audit_file = None
        
        # 异常检测
        self._suspicious_agents: Set[str] = set()
        self._disabled_tools: Dict[str, datetime] = {}  # agent_id -> 禁用到期时间
        
        # 初始化审计日志文件
        if self.config.audit_enabled:
            self._init_audit_log()
    
    def _init_audit_log(self):
        """初始化审计日志"""
        log_dir = os.path.dirname(self.config.audit_log_path)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)
    
    def register_agent(
        self,
        agent_id: str,
        allowed_tools: List[str] = None,
        denied_tools: List[str] = None
    ):
        """
        注册代理权限
        
        Args:
            agent_id: 代理ID
            allowed_tools: 允许的工具列表
            denied_tools: 禁止的工具列表
        """
        permission = AgentPermission(
            agent_id=agent_id,
            allowed_tools=set(allowed_tools or []),
            denied_tools=set(denied_tools or [])
        )
        self._agent_permissions[agent_id] = permission
        logger.info(f"注册代理权限: {agent_id}, 允许工具: {allowed_tools}")
    
    def check_access(
        self,
        agent_id: str,
        tool_name: str,
        args: Dict[str, Any] = None
    ) -> tuple:
        """
        检查访问权限
        
        Args:
            agent_id: 代理ID
            tool_name: 工具名称
            args: 工具参数
            
        Returns:
            (decision, reason): 访问决策和原因
        """
        args = args or {}
        
        # 检查代理是否被临时禁用
        if agent_id in self._disabled_tools:
            expiry = self._disabled_tools[agent_id]
            if datetime.now() < expiry:
                return AccessDecision.DENY, f"代理被临时禁用，剩余时间: {(expiry - datetime.now()).seconds}秒"
            else:
                del self._disabled_tools[agent_id]
        
        # 获取代理权限
        permission = self._agent_permissions.get(agent_id)
        if not permission:
            return AccessDecision.PERMISSION_DENIED, f"代理 {agent_id} 未注册"
        
        # 检查工具权限
        allowed, reason = permission.is_tool_allowed(tool_name)
        if not allowed:
            return AccessDecision.PERMISSION_DENIED, reason
        
        # 检查安全级别限制
        category = self.config.tool_categories.get(tool_name, ToolCategory.READ_ONLY)
        
        if self.config.level == SecurityLevel.STRICT:
            if category == ToolCategory.CODE_EXEC:
                return AccessDecision.SECURITY_VIOLATION, "严格模式下禁用代码执行"
            if category == ToolCategory.NETWORK:
                return AccessDecision.SECURITY_VIOLATION, "严格模式下禁用网络工具"
            if category == ToolCategory.WORKSPACE_RW:
                return AccessDecision.SECURITY_VIOLATION, "严格模式下禁用文件写入"
        
        if category == ToolCategory.NETWORK and not self.config.network_tools_enabled:
            return AccessDecision.SECURITY_VIOLATION, "网络工具未启用"
        
        # 检查代理级速率限制
        if self._is_rate_limited(agent_id, tool_name, permission):
            return AccessDecision.RATE_LIMITED, f"达到速率限制"
        
        # 检查全局工具速率限制
        if self._is_global_rate_limited(tool_name):
            return AccessDecision.RATE_LIMITED, f"工具 {tool_name} 达到全局速率限制"
        
        # 检查冷却期
        if self._is_in_cooldown(agent_id, tool_name):
            return AccessDecision.RATE_LIMITED, f"工具 {tool_name} 处于冷却期"
        
        return AccessDecision.ALLOW, None
    
    def _is_rate_limited(
        self,
        agent_id: str,
        tool_name: str,
        permission: AgentPermission
    ) -> bool:
        """检查代理是否被速率限制"""
        # 获取适用的速率限制
        if tool_name in permission.rate_limit_overrides:
            rate_limit = permission.rate_limit_overrides[tool_name]
        else:
            category = self.config.tool_categories.get(tool_name, ToolCategory.READ_ONLY)
            rate_limit = self.CATEGORY_RATE_LIMITS.get(category, self.config.default_rate_limit)
        
        # 清理过期记录
        now = time.time()
        self._agent_call_history[agent_id] = [
            t for t in self._agent_call_history[agent_id]
            if now - t < rate_limit.window_seconds
        ]
        
        # 检查是否超限
        return len(self._agent_call_history[agent_id]) >= rate_limit.max_calls
    
    def _is_global_rate_limited(self, tool_name: str) -> bool:
        """检查全局速率限制"""
        now = time.time()
        self._tool_call_history[tool_name] = [
            t for t in self._tool_call_history[tool_name]
            if now - t < self.config.global_rate_limit.window_seconds
        ]
        
        return len(self._tool_call_history[tool_name]) >= self.config.global_rate_limit.max_calls
    
    def _is_in_cooldown(self, agent_id: str, tool_name: str) -> bool:
        """检查是否在冷却期"""
        if agent_id in self._agent_cooldowns:
            if tool_name in self._agent_cooldowns[agent_id]:
                expiry = self._agent_cooldowns[agent_id][tool_name]
                if time.time() < expiry:
                    return True
                else:
                    del self._agent_cooldowns[agent_id][tool_name]
        return False
    
    def record_call(
        self,
        agent_id: str,
        tool_name: str,
        args: Dict[str, Any],
        result: Any,
        success: bool,
        execution_time: float,
        decision: AccessDecision
    ):
        """
        记录工具调用
        
        Args:
            agent_id: 代理ID
            tool_name: 工具名称
            args: 参数
            result: 结果
            success: 是否成功
            execution_time: 执行时间
            decision: 访问决策
        """
        now = time.time()
        
        # 更新调用历史
        self._agent_call_history[agent_id].append(now)
        self._tool_call_history[tool_name].append(now)
        
        # 记录审计日志
        if self.config.audit_enabled:
            entry = AuditEntry(
                timestamp=datetime.now(),
                agent_id=agent_id,
                tool_name=tool_name,
                args=args,
                result_summary=str(result)[:500] if result else "",
                success=success,
                execution_time=execution_time,
                decision=decision
            )
            self._audit_log.append(entry)
            self._write_audit_entry(entry)
        
        # 异常行为检测
        if self.config.anomaly_detection:
            self._check_anomaly(agent_id, tool_name, success)
    
    def _write_audit_entry(self, entry: AuditEntry):
        """写入审计日志"""
        try:
            with open(self.config.audit_log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + '\n')
        except Exception as e:
            logger.error(f"写入审计日志失败: {e}")
    
    def _check_anomaly(self, agent_id: str, tool_name: str, success: bool):
        """检测异常行为"""
        permission = self._agent_permissions.get(agent_id)
        if not permission:
            return
        
        # 检测连续失败
        if not success:
            permission.record_violation()
            
            if permission.violation_count >= self.config.violation_threshold:
                logger.warning(f"代理 {agent_id} 违规次数达到阈值")
                self._disable_agent_temporarily(agent_id)
        
        # 检测路径遍历尝试
        if tool_name in ("temp_file_read", "temp_file_write", "temp_file_delete"):
            # 这个检查在工具执行时进行，这里只做记录
            pass
    
    def _disable_agent_temporarily(self, agent_id: str):
        """临时禁用代理"""
        expiry = datetime.now() + timedelta(seconds=self.config.auto_disable_duration)
        self._disabled_tools[agent_id] = expiry
        logger.warning(f"代理 {agent_id} 被临时禁用，到期时间: {expiry}")
    
    def grant_temporary_permission(
        self,
        agent_id: str,
        tool_name: str,
        duration_seconds: int = 3600
    ):
        """
        授予临时权限
        
        Args:
            agent_id: 代理ID
            tool_name: 工具名称
            duration_seconds: 有效期（秒）
        """
        permission = self._agent_permissions.get(agent_id)
        if permission:
            permission.grant_temporary(tool_name, duration_seconds)
    
    def revoke_permission(self, agent_id: str, tool_name: str):
        """撤销权限"""
        permission = self._agent_permissions.get(agent_id)
        if permission:
            if tool_name in permission.allowed_tools:
                permission.allowed_tools.remove(tool_name)
            permission.denied_tools.add(tool_name)
    
    def set_rate_limit(self, agent_id: str, tool_name: str, rate_limit: RateLimit):
        """设置代理级别的速率限制"""
        permission = self._agent_permissions.get(agent_id)
        if permission:
            permission.rate_limit_overrides[tool_name] = rate_limit
    
    def get_audit_log(
        self,
        agent_id: str = None,
        tool_name: str = None,
        since: datetime = None,
        limit: int = 100
    ) -> List[AuditEntry]:
        """
        获取审计日志
        
        Args:
            agent_id: 代理ID（可选）
            tool_name: 工具名称（可选）
            since: 起始时间（可选）
            limit: 最大条数
            
        Returns:
            审计条目列表
        """
        entries = self._audit_log
        
        if agent_id:
            entries = [e for e in entries if e.agent_id == agent_id]
        
        if tool_name:
            entries = [e for e in entries if e.tool_name == tool_name]
        
        if since:
            entries = [e for e in entries if e.timestamp >= since]
        
        return entries[-limit:]
    
    def get_agent_stats(self, agent_id: str) -> Dict[str, Any]:
        """获取代理统计信息"""
        permission = self._agent_permissions.get(agent_id)
        if not permission:
            return {"error": f"代理 {agent_id} 未注册"}
        
        # 计算调用统计
        recent_calls = len([
            t for t in self._agent_call_history.get(agent_id, [])
            if time.time() - t < 60
        ])
        
        # 审计统计
        agent_audit = [e for e in self._audit_log if e.agent_id == agent_id]
        success_count = sum(1 for e in agent_audit if e.success)
        fail_count = len(agent_audit) - success_count
        
        return {
            "agent_id": agent_id,
            "allowed_tools": list(permission.allowed_tools),
            "denied_tools": list(permission.denied_tools),
            "temporary_grants": {
                k: v.isoformat() for k, v in permission.temporary_grants.items()
            },
            "violation_count": permission.violation_count,
            "recent_calls_60s": recent_calls,
            "total_calls": len(agent_audit),
            "success_count": success_count,
            "fail_count": fail_count,
            "success_rate": success_count / len(agent_audit) if agent_audit else 0,
            "is_disabled": agent_id in self._disabled_tools,
        }
    
    def get_tool_stats(self, tool_name: str) -> Dict[str, Any]:
        """获取工具统计信息"""
        recent_calls = len([
            t for t in self._tool_call_history.get(tool_name, [])
            if time.time() - t < 60
        ])
        
        tool_audit = [e for e in self._audit_log if e.tool_name == tool_name]
        
        return {
            "tool_name": tool_name,
            "category": self.config.tool_categories.get(tool_name, ToolCategory.READ_ONLY).value,
            "recent_calls_60s": recent_calls,
            "total_calls": len(tool_audit),
            "unique_callers": len(set(e.agent_id for e in tool_audit)),
        }
    
    def get_security_summary(self) -> Dict[str, Any]:
        """获取安全摘要"""
        return {
            "level": self.config.level.value,
            "network_tools_enabled": self.config.network_tools_enabled,
            "anomaly_detection": self.config.anomaly_detection,
            "registered_agents": len(self._agent_permissions),
            "total_audit_entries": len(self._audit_log),
            "disabled_agents": list(self._disabled_tools.keys()),
            "suspicious_agents": list(self._suspicious_agents),
        }
    
    def clear_audit_log(self):
        """清空审计日志"""
        self._audit_log.clear()
        if os.path.exists(self.config.audit_log_path):
            os.remove(self.config.audit_log_path)
    
    def emergency_disable(self, agent_id: str):
        """紧急禁用代理"""
        self._disable_agent_temporarily(agent_id)
        permission = self._agent_permissions.get(agent_id)
        if permission:
            permission.denied_tools.update(permission.allowed_tools)
            permission.allowed_tools.clear()
        logger.critical(f"紧急禁用代理: {agent_id}")


class ToolSecurityMiddleware:
    """工具安全中间件 - 包装工具执行"""
    
    def __init__(self, controller: ToolSecurityController):
        self.controller = controller
    
    async def wrap_execution(
        self,
        agent_id: str,
        tool_name: str,
        args: Dict[str, Any],
        execute_func: Callable
    ) -> tuple:
        """
        包装工具执行，添加安全检查
        
        Args:
            agent_id: 代理ID
            tool_name: 工具名称
            args: 参数
            execute_func: 执行函数
            
        Returns:
            (success, result, error): 执行结果
        """
        start_time = time.time()
        
        # 检查访问权限
        decision, reason = self.controller.check_access(agent_id, tool_name, args)
        
        if decision != AccessDecision.ALLOW:
            execution_time = time.time() - start_time
            self.controller.record_call(
                agent_id=agent_id,
                tool_name=tool_name,
                args=args,
                result=None,
                success=False,
                execution_time=execution_time,
                decision=decision
            )
            return False, None, f"访问被拒绝: {reason}"
        
        # 执行工具
        try:
            result = await execute_func(args)
            execution_time = time.time() - start_time
            
            self.controller.record_call(
                agent_id=agent_id,
                tool_name=tool_name,
                args=args,
                result=result,
                success=True,
                execution_time=execution_time,
                decision=decision
            )
            
            return True, result, None
            
        except Exception as e:
            execution_time = time.time() - start_time
            
            self.controller.record_call(
                agent_id=agent_id,
                tool_name=tool_name,
                args=args,
                result=None,
                success=False,
                execution_time=execution_time,
                decision=decision,
                error=str(e)
            )
            
            return False, None, str(e)
