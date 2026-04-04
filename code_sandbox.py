"""代码执行沙箱 - Docker隔离执行"""
import os
import json
import asyncio
import subprocess
import tempfile
import shutil
import hashlib
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple
from enum import Enum
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class ExecutionMode(Enum):
    """执行模式"""
    DOCKER = "docker"           # Docker容器（推荐）
    SUBPROCESS = "subprocess"    # 子进程（开发模式）
    NSJAIL = "nsjail"           # nsjail沙箱


class ExecutionResult(Enum):
    """执行结果状态"""
    SUCCESS = "success"
    TIMEOUT = "timeout"
    ERROR = "error"
    KILLED = "killed"
    DENIED = "denied"


@dataclass
class SandboxConfig:
    """沙箱配置"""
    # 执行模式
    mode: ExecutionMode = ExecutionMode.SUBPROCESS
    
    # 资源限制
    cpu_limit: float = 0.5          # CPU核心数
    memory_limit: str = "512m"       # 内存限制
    pids_limit: int = 100            # 进程数限制
    timeout_seconds: float = 30.0    # 执行超时
    
    # 输出限制
    max_output_size: int = 1024 * 1024  # 1 MB
    max_stderr_size: int = 1024 * 1024  # 1 MB
    
    # 安全选项
    network_disabled: bool = True    # 禁用网络
    read_only_root: bool = True      # 只读根文件系统
    no_new_privileges: bool = True   # 禁止提升权限
    drop_capabilities: bool = True   # 剥离所有能力
    
    # 语言配置
    allowed_languages: List[str] = field(default_factory=lambda: ["python", "bash", "javascript"])
    
    # Docker镜像
    docker_image: str = "python:3.11-slim"
    
    # 用户映射
    run_as_user: str = "1000:1000"   # 非root用户


@dataclass
class ExecutionOutput:
    """执行输出"""
    success: bool
    status: ExecutionResult
    stdout: str
    stderr: str
    exit_code: int
    execution_time: float
    resource_usage: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    
    def to_dict(self) -> Dict:
        return {
            "success": self.success,
            "status": self.status.value,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "execution_time": self.execution_time,
            "resource_usage": self.resource_usage,
            "error": self.error
        }


class CodeSandbox:
    """代码执行沙箱"""
    
    # 语言解释器配置
    INTERPRETERS = {
        "python": {
            "cmd": ["python3", "-c"],
            "ext": ".py",
            "docker_image": "python:3.11-slim"
        },
        "bash": {
            "cmd": ["bash", "-c"],
            "ext": ".sh",
            "docker_image": "bash:5"
        },
        "sh": {
            "cmd": ["sh", "-c"],
            "ext": ".sh",
            "docker_image": "alpine:latest"
        },
        "javascript": {
            "cmd": ["node", "-e"],
            "ext": ".js",
            "docker_image": "node:18-slim"
        },
        "js": {
            "cmd": ["node", "-e"],
            "ext": ".js",
            "docker_image": "node:18-slim"
        },
    }
    
    def __init__(self, config: SandboxConfig = None):
        """
        初始化代码沙箱
        
        Args:
            config: 沙箱配置
        """
        self.config = config or SandboxConfig()
        self._docker_available = None
        
    def check_docker_available(self) -> bool:
        """检查Docker是否可用"""
        if self._docker_available is not None:
            return self._docker_available
        
        try:
            result = subprocess.run(
                ["docker", "--version"],
                capture_output=True,
                timeout=5
            )
            self._docker_available = result.returncode == 0
            if self._docker_available:
                logger.info("Docker可用，将使用容器隔离执行")
            else:
                logger.warning("Docker不可用，将使用子进程执行")
        except Exception as e:
            logger.warning(f"Docker检查失败: {e}，将使用子进程执行")
            self._docker_available = False
        
        return self._docker_available
    
    async def execute(
        self,
        code: str,
        language: str,
        workspace_path: str = None,
        timeout: float = None
    ) -> ExecutionOutput:
        """
        执行代码
        
        Args:
            code: 要执行的代码
            language: 编程语言
            workspace_path: 工作区路径（可选）
            timeout: 超时时间（可选）
            
        Returns:
            ExecutionOutput: 执行结果
        """
        start_time = datetime.now()
        timeout = timeout or self.config.timeout_seconds
        
        # 验证语言
        if language.lower() not in self.INTERPRETERS:
            return ExecutionOutput(
                success=False,
                status=ExecutionResult.DENIED,
                stdout="",
                stderr="",
                exit_code=-1,
                execution_time=0,
                error=f"不支持的语言: {language}"
            )
        
        if language.lower() not in self.config.allowed_languages:
            return ExecutionOutput(
                success=False,
                status=ExecutionResult.DENIED,
                stdout="",
                stderr="",
                exit_code=-1,
                execution_time=0,
                error=f"语言 '{language}' 未被允许"
            )
        
        # 选择执行方式
        if self.config.mode == ExecutionMode.DOCKER and self.check_docker_available():
            output = await self._execute_docker(code, language, workspace_path, timeout)
        else:
            output = await self._execute_subprocess(code, language, workspace_path, timeout)
        
        output.execution_time = (datetime.now() - start_time).total_seconds()
        return output
    
    async def _execute_docker(
        self,
        code: str,
        language: str,
        workspace_path: str,
        timeout: float
    ) -> ExecutionOutput:
        """使用Docker执行代码"""
        interpreter = self.INTERPRETERS[language.lower()]
        
        # 构建Docker命令
        docker_cmd = [
            "docker", "run",
            "--rm",  # 执行后删除容器
            "--network", "none" if self.config.network_disabled else "bridge",
            "--cpus", str(self.config.cpu_limit),
            "--memory", self.config.memory_limit,
            "--pids-limit", str(self.config.pids_limit),
            "--user", self.config.run_as_user,
        ]
        
        # 安全选项
        if self.config.no_new_privileges:
            docker_cmd.extend(["--security-opt", "no-new-privileges"])
        
        if self.config.drop_capabilities:
            docker_cmd.extend(["--cap-drop", "ALL"])
        
        # 只读根文件系统
        if self.config.read_only_root:
            docker_cmd.append("--read-only")
        
        # 挂载工作区
        if workspace_path:
            docker_cmd.extend([
                "-v", f"{os.path.abspath(workspace_path)}:/workspace:rw"
            ])
        
        # 使用镜像
        docker_image = interpreter.get("docker_image", self.config.docker_image)
        docker_cmd.append(docker_image)
        
        # 执行命令
        docker_cmd.extend(interpreter["cmd"])
        docker_cmd.append(code)
        
        logger.debug(f"Docker命令: {' '.join(docker_cmd[:10])}...")
        
        try:
            process = await asyncio.create_subprocess_exec(
                *docker_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                # Docker容器会自动被--rm删除
                try:
                    process.kill()
                    await process.wait()
                except:
                    pass
                
                return ExecutionOutput(
                    success=False,
                    status=ExecutionResult.TIMEOUT,
                    stdout="",
                    stderr=f"执行超时（{timeout}秒）",
                    exit_code=-1,
                    execution_time=timeout,
                    error="timeout"
                )
            
            # 截断输出
            stdout_str = self._truncate_output(stdout.decode('utf-8', errors='replace'))
            stderr_str = self._truncate_output(stderr.decode('utf-8', errors='replace'))
            
            return ExecutionOutput(
                success=process.returncode == 0,
                status=ExecutionResult.SUCCESS if process.returncode == 0 else ExecutionResult.ERROR,
                stdout=stdout_str,
                stderr=stderr_str,
                exit_code=process.returncode
            )
            
        except FileNotFoundError:
            logger.error("Docker命令未找到")
            # 回退到子进程执行
            return await self._execute_subprocess(code, language, workspace_path, timeout)
        except Exception as e:
            logger.error(f"Docker执行错误: {e}")
            return ExecutionOutput(
                success=False,
                status=ExecutionResult.ERROR,
                stdout="",
                stderr=str(e),
                exit_code=-1,
                execution_time=0,
                error=str(e)
            )
    
    async def _execute_subprocess(
        self,
        code: str,
        language: str,
        workspace_path: str,
        timeout: float
    ) -> ExecutionOutput:
        """使用子进程执行代码（开发模式）"""
        interpreter = self.INTERPRETERS[language.lower()]
        cmd = interpreter["cmd"] + [code]
        
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace_path
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                try:
                    process.kill()
                    await process.wait()
                except:
                    pass
                
                return ExecutionOutput(
                    success=False,
                    status=ExecutionResult.TIMEOUT,
                    stdout="",
                    stderr=f"执行超时（{timeout}秒）",
                    exit_code=-1,
                    execution_time=timeout,
                    error="timeout"
                )
            
            # 截断输出
            stdout_str = self._truncate_output(stdout.decode('utf-8', errors='replace'))
            stderr_str = self._truncate_output(stderr.decode('utf-8', errors='replace'))
            
            return ExecutionOutput(
                success=process.returncode == 0,
                status=ExecutionResult.SUCCESS if process.returncode == 0 else ExecutionResult.ERROR,
                stdout=stdout_str,
                stderr=stderr_str,
                exit_code=process.returncode,
                execution_time=0  # 会被外层更新
            )
            
        except FileNotFoundError:
            return ExecutionOutput(
                success=False,
                status=ExecutionResult.ERROR,
                stdout="",
                stderr=f"解释器未安装: {language}",
                exit_code=-1,
                execution_time=0,
                error=f"interpreter not found: {language}"
            )
        except Exception as e:
            return ExecutionOutput(
                success=False,
                status=ExecutionResult.ERROR,
                stdout="",
                stderr=str(e),
                exit_code=-1,
                execution_time=0,
                error=str(e)
            )
    
    def _truncate_output(self, output: str) -> str:
        """截断输出"""
        if len(output) > self.config.max_output_size:
            return output[:self.config.max_output_size] + "\n... (输出已截断)"
        return output
    
    async def execute_file(
        self,
        file_path: str,
        language: str,
        args: List[str] = None,
        timeout: float = None
    ) -> ExecutionOutput:
        """
        执行文件
        
        Args:
            file_path: 文件路径
            language: 语言
            args: 参数列表
            timeout: 超时时间
            
        Returns:
            ExecutionOutput: 执行结果
        """
        if not os.path.exists(file_path):
            return ExecutionOutput(
                success=False,
                status=ExecutionResult.DENIED,
                stdout="",
                stderr="",
                exit_code=-1,
                execution_time=0,
                error=f"文件不存在: {file_path}"
            )
        
        # 读取文件内容
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                code = f.read()
        except Exception as e:
            return ExecutionOutput(
                success=False,
                status=ExecutionResult.DENIED,
                stdout="",
                stderr="",
                exit_code=-1,
                execution_time=0,
                error=f"读取文件失败: {e}"
            )
        
        # 执行
        workspace_path = os.path.dirname(file_path)
        return await self.execute(code, language, workspace_path, timeout)
    
    def get_stats(self) -> Dict[str, Any]:
        """获取沙箱统计信息"""
        return {
            "mode": self.config.mode.value,
            "docker_available": self._docker_available,
            "allowed_languages": self.config.allowed_languages,
            "timeout_seconds": self.config.timeout_seconds,
            "memory_limit": self.config.memory_limit,
            "cpu_limit": self.config.cpu_limit,
            "network_disabled": self.config.network_disabled
        }


class SandboxPool:
    """沙箱池 - 管理多个沙箱实例"""
    
    _instances: Dict[str, CodeSandbox] = {}
    
    @classmethod
    def get_sandbox(cls, config: SandboxConfig = None) -> CodeSandbox:
        """获取沙箱实例"""
        config_key = hashlib.md5(str(config.__dict__).encode()).hexdigest() if config else "default"
        
        if config_key not in cls._instances:
            cls._instances[config_key] = CodeSandbox(config)
        
        return cls._instances[config_key]
    
    @classmethod
    def get_all_stats(cls) -> Dict[str, Dict]:
        """获取所有沙箱统计"""
        return {
            key: sandbox.get_stats()
            for key, sandbox in cls._instances.items()
        }


# 预设的安全配置
SAFE_CONFIG = SandboxConfig(
    mode=ExecutionMode.DOCKER,
    cpu_limit=0.5,
    memory_limit="256m",
    pids_limit=50,
    timeout_seconds=15.0,
    network_disabled=True,
    read_only_root=True,
    no_new_privileges=True,
    drop_capabilities=True,
    allowed_languages=["python"]
)

DEVELOPMENT_CONFIG = SandboxConfig(
    mode=ExecutionMode.SUBPROCESS,
    cpu_limit=1.0,
    memory_limit="1g",
    pids_limit=200,
    timeout_seconds=60.0,
    network_disabled=False,
    read_only_root=False,
    no_new_privileges=False,
    drop_capabilities=False,
    allowed_languages=["python", "bash", "javascript"]
)

PERMISSIVE_CONFIG = SandboxConfig(
    mode=ExecutionMode.SUBPROCESS,
    cpu_limit=2.0,
    memory_limit="2g",
    pids_limit=500,
    timeout_seconds=120.0,
    network_disabled=False,
    read_only_root=False,
    no_new_privileges=False,
    drop_capabilities=False,
    allowed_languages=["python", "bash", "javascript", "ruby", "perl"]
)
