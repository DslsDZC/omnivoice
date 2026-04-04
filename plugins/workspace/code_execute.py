"""代码执行插件 - 带安全沙箱"""
import os
import asyncio
import subprocess
import tempfile
import shutil
from typing import Dict, Any, List, Optional

from tools.base import BaseTool, ToolResult, BasePlugin
from code_sandbox import (
    CodeSandbox, SandboxConfig, ExecutionMode,
    ExecutionOutput, ExecutionResult,
    SAFE_CONFIG, DEVELOPMENT_CONFIG
)
from code_scanner import (
    CodeScanner, CodeScannerFactory, ScanResult,
    ThreatLevel, ThreatCategory
)


class CodeExecuteTool(BaseTool):
    """代码执行工具（带安全沙箱）"""
    
    name = "code_execute"
    description = "在安全沙箱中执行代码，返回stdout、stderr和exit_code"
    security_level = "medium"
    parameters_schema = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "要执行的代码"
            },
            "language": {
                "type": "string",
                "description": "编程语言：python, bash, javascript",
                "enum": ["python", "bash", "sh", "javascript", "js"]
            },
            "timeout": {
                "type": "integer",
                "description": "执行超时时间（秒），默认使用配置值"
            },
            "save_to": {
                "type": "string",
                "description": "将代码保存到指定文件（可选）"
            }
        },
        "required": ["code", "language"]
    }
    
    def __init__(self):
        super().__init__()
        self._scanner = CodeScannerFactory.get_scanner()
        self._sandbox = None
    
    def _get_sandbox(self, context: Dict) -> CodeSandbox:
        """获取沙箱实例"""
        # 从上下文获取安全配置
        security_config = context.get("security_config")
        
        if security_config:
            mode = ExecutionMode.DOCKER if security_config.execution_mode == "docker" else ExecutionMode.SUBPROCESS
            config = SandboxConfig(
                mode=mode,
                timeout_seconds=security_config.code_timeout_seconds,
                max_output_size=security_config.code_max_output_size,
                allowed_languages=security_config.allowed_languages
            )
        else:
            config = DEVELOPMENT_CONFIG
        
        return CodeSandbox(config)
    
    async def execute(self, args: Dict[str, Any], context: Dict) -> ToolResult:
        workspace_path = context.get("workspace_path")
        if not workspace_path:
            return ToolResult(success=False, result=None, error="工作区未初始化")
        
        code = args.get("code", "")
        language = args.get("language", "python").lower()
        timeout = args.get("timeout")
        save_to = args.get("save_to")
        
        if not code:
            return ToolResult(success=False, result=None, error="代码不能为空")
        
        # 标准化语言名称
        lang_map = {"js": "javascript", "sh": "bash"}
        language = lang_map.get(language, language)
        
        # 第一步：静态代码扫描
        scan_result = self._scanner.scan(code, language)
        
        if not scan_result.allowed:
            threat_summary = scan_result.get_summary()
            threat_details = []
            for t in scan_result.threats:
                if t.level in (ThreatLevel.CRITICAL, ThreatLevel.HIGH):
                    threat_details.append(f"  - {t.description} (行 {t.line_number or '?'})")
            
            error_msg = f"代码被安全扫描器拒绝:\n{threat_summary}\n"
            if threat_details:
                error_msg += "详细:\n" + "\n".join(threat_details[:5])
            
            return ToolResult(
                success=False,
                result=None,
                error=error_msg,
                metadata={"scan_result": {
                    "threats_count": len(scan_result.threats),
                    "critical": scan_result.critical_count,
                    "high": scan_result.high_count
                }}
            )
        
        # 第二步：在沙箱中执行
        sandbox = self._get_sandbox(context)
        
        # 保存代码文件（可选）
        if save_to:
            try:
                save_path = os.path.join(workspace_path, save_to)
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                with open(save_path, 'w', encoding='utf-8') as f:
                    f.write(code)
            except Exception as e:
                return ToolResult(success=False, result=None, error=f"保存代码失败: {str(e)}")
        
        # 执行
        output = await sandbox.execute(code, language, workspace_path, timeout)
        
        # 构建结果
        result = {
            "stdout": output.stdout,
            "stderr": output.stderr,
            "exit_code": output.exit_code,
            "language": language,
            "execution_time": round(output.execution_time, 3),
            "status": output.status.value
        }
        
        # 添加扫描警告
        if scan_result.warnings:
            result["scan_warnings"] = scan_result.warnings
        
        # 添加资源使用信息
        if output.resource_usage:
            result["resource_usage"] = output.resource_usage
        
        return ToolResult(
            success=output.success,
            result=result,
            error=output.error,
            metadata={
                "sandbox_mode": sandbox.config.mode.value,
                "timeout_used": timeout or sandbox.config.timeout_seconds
            }
        )


class ScriptRunTool(BaseTool):
    """脚本运行工具 - 运行工作区中的脚本文件"""
    
    name = "script_run"
    description = "运行工作区中的脚本文件（带安全检查）"
    security_level = "medium"
    parameters_schema = {
        "type": "object",
        "properties": {
            "script_path": {
                "type": "string",
                "description": "脚本文件路径（相对于工作区）"
            },
            "args": {
                "type": "array",
                "description": "脚本参数",
                "items": {"type": "string"}
            },
            "timeout": {
                "type": "integer",
                "description": "执行超时时间（秒）"
            }
        },
        "required": ["script_path"]
    }
    
    # 脚本扩展名到语言的映射
    SCRIPT_LANGUAGES = {
        ".py": "python",
        ".sh": "bash",
        ".bash": "bash",
        ".js": "javascript",
        ".rb": "ruby",
        ".pl": "perl"
    }
    
    def __init__(self):
        super().__init__()
        self._scanner = CodeScannerFactory.get_scanner()
    
    async def execute(self, args: Dict[str, Any], context: Dict) -> ToolResult:
        workspace_path = context.get("workspace_path")
        if not workspace_path:
            return ToolResult(success=False, result=None, error="工作区未初始化")
        
        script_path = args.get("script_path", "").strip()
        script_args = args.get("args", [])
        timeout = args.get("timeout", 60)
        
        if not script_path:
            return ToolResult(success=False, result=None, error="脚本路径不能为空")
        
        # 安全检查：防止路径遍历
        full_path = os.path.abspath(os.path.join(workspace_path, script_path))
        if not full_path.startswith(os.path.abspath(workspace_path)):
            return ToolResult(success=False, result=None, error="非法路径")
        
        if not os.path.exists(full_path):
            return ToolResult(success=False, result=None, error=f"脚本不存在: {script_path}")
        
        # 确定语言
        ext = os.path.splitext(full_path)[1].lower()
        language = self.SCRIPT_LANGUAGES.get(ext)
        
        if not language:
            return ToolResult(
                success=False,
                result=None,
                error=f"不支持的脚本类型: {ext}"
            )
        
        # 读取并扫描脚本
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                code = f.read()
        except Exception as e:
            return ToolResult(success=False, result=None, error=f"读取脚本失败: {str(e)}")
        
        # 安全扫描
        scan_result = self._scanner.scan(code, language)
        if not scan_result.allowed:
            return ToolResult(
                success=False,
                result=None,
                error=f"脚本被安全扫描器拒绝: {scan_result.get_summary()}"
            )
        
        # 执行脚本
        interpreter = self._get_interpreter(language)
        if not interpreter:
            return ToolResult(success=False, result=None, error=f"解释器未安装: {language}")
        
        try:
            cmd = [interpreter, full_path] + script_args
            
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
                return ToolResult(
                    success=False,
                    result=None,
                    error=f"执行超时（{timeout}秒）"
                )
            
            return ToolResult(
                success=process.returncode == 0,
                result={
                    "stdout": stdout.decode('utf-8', errors='replace'),
                    "stderr": stderr.decode('utf-8', errors='replace'),
                    "exit_code": process.returncode,
                    "script": script_path
                }
            )
            
        except FileNotFoundError:
            return ToolResult(success=False, result=None, error=f"解释器未安装: {language}")
        except Exception as e:
            return ToolResult(success=False, result=None, error=f"执行错误: {str(e)}")
    
    def _get_interpreter(self, language: str) -> Optional[str]:
        """获取解释器路径"""
        interpreters = {
            "python": "python3",
            "bash": "bash",
            "javascript": "node",
            "ruby": "ruby",
            "perl": "perl"
        }
        return interpreters.get(language)


class SecurityStatusTool(BaseTool):
    """安全状态查询工具"""
    
    name = "security_status"
    description = "查询当前安全配置和状态"
    security_level = "low"
    parameters_schema = {
        "type": "object",
        "properties": {},
        "required": []
    }
    
    async def execute(self, args: Dict[str, Any], context: Dict) -> ToolResult:
        security_config = context.get("security_config")
        
        result = {
            "scanner": {
                "max_code_length": self._scanner.max_length if hasattr(self, '_scanner') else 10000,
                "max_code_lines": self._scanner.max_lines if hasattr(self, '_scanner') else 500
            }
        }
        
        if security_config:
            result["config"] = {
                "level": security_config.level,
                "execution_mode": security_config.execution_mode,
                "timeout_seconds": security_config.code_timeout_seconds,
                "allowed_languages": security_config.allowed_languages,
                "audit_enabled": security_config.audit_enabled
            }
        
        return ToolResult(success=True, result=result)


class CodeExecutePlugin(BasePlugin):
    """代码执行插件"""
    
    plugin_name = "code_execute"
    plugin_version = "2.0.0"
    plugin_description = "代码执行和脚本运行工具（带安全沙箱和静态扫描）"
    plugin_author = "system"
    plugin_security_level = "medium"
    plugin_tags = ["workspace", "code", "execute", "script", "security"]
    
    def __init__(self):
        super().__init__()
    
    def initialize(self, config: Dict[str, Any] = None):
        """初始化插件"""
        self.register_tool(CodeExecuteTool())
        self.register_tool(ScriptRunTool())
        self.register_tool(SecurityStatusTool())