"""工作区工具 - 临时文件操作和代码执行"""
import os
import subprocess
import hashlib
import asyncio
import shutil
from pathlib import Path
from typing import Dict, Any, List, Optional

from tools.base import BaseTool, ToolResult


class TempFileReadTool(BaseTool):
    """临时文件读取工具"""
    
    name = "temp_file_read"
    description = "读取临时工作区中的文件内容"
    security_level = "medium"
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对于工作区的文件路径"
            }
        },
        "required": ["path"]
    }
    
    async def execute(self, args: Dict[str, Any], context: Dict) -> ToolResult:
        workspace_path = context.get("workspace_path")
        if not workspace_path:
            return ToolResult(success=False, result=None, error="工作区未初始化")
        
        rel_path = args.get("path", "").strip()
        if not rel_path:
            return ToolResult(success=False, result=None, error="文件路径不能为空")
        
        # 安全检查：防止路径遍历攻击
        full_path = self._safe_join(workspace_path, rel_path)
        if full_path is None:
            return ToolResult(success=False, result=None, error="非法路径")
        
        try:
            if not os.path.exists(full_path):
                return ToolResult(success=False, result=None, error=f"文件不存在: {rel_path}")
            
            # 限制文件大小（10MB）
            file_size = os.path.getsize(full_path)
            if file_size > 10 * 1024 * 1024:
                return ToolResult(success=False, result=None, error="文件过大（超过10MB）")
            
            with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            
            return ToolResult(success=True, result={
                "path": rel_path,
                "content": content,
                "size": file_size
            })
        except Exception as e:
            return ToolResult(success=False, result=None, error=f"读取文件错误: {str(e)}")
    
    def _safe_join(self, base: str, rel: str) -> Optional[str]:
        """安全拼接路径，防止路径遍历"""
        base = os.path.abspath(base)
        full = os.path.abspath(os.path.join(base, rel))
        if full.startswith(base):
            return full
        return None


class TempFileWriteTool(BaseTool):
    """临时文件写入工具"""
    
    name = "temp_file_write"
    description = "在临时工作区中创建或覆盖文件"
    security_level = "medium"
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对于工作区的文件路径"
            },
            "content": {
                "type": "string",
                "description": "文件内容"
            }
        },
        "required": ["path", "content"]
    }
    
    async def execute(self, args: Dict[str, Any], context: Dict) -> ToolResult:
        workspace_path = context.get("workspace_path")
        if not workspace_path:
            return ToolResult(success=False, result=None, error="工作区未初始化")
        
        rel_path = args.get("path", "").strip()
        content = args.get("content", "")
        
        if not rel_path:
            return ToolResult(success=False, result=None, error="文件路径不能为空")
        
        # 安全检查
        full_path = self._safe_join(workspace_path, rel_path)
        if full_path is None:
            return ToolResult(success=False, result=None, error="非法路径")
        
        try:
            # 检查工作区大小限制
            workspace_manager = context.get("workspace_manager")
            if workspace_manager:
                new_size = len(content.encode('utf-8'))
                if not workspace_manager.check_size_limit(new_size):
                    return ToolResult(success=False, result=None, error="超过工作区大小限制")
            
            # 创建父目录
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            
            # 写入文件
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(content)
            
            # 更新白板
            whiteboard = context.get("whiteboard")
            if whiteboard:
                file_size = len(content.encode('utf-8'))
                file_hash = hashlib.md5(content.encode()).hexdigest()[:8]
                whiteboard.update_workspace_file(rel_path, file_size, os.path.getmtime(full_path), file_hash)
            
            return ToolResult(success=True, result={
                "path": rel_path,
                "size": len(content.encode('utf-8')),
                "message": f"文件已写入: {rel_path}"
            })
        except Exception as e:
            return ToolResult(success=False, result=None, error=f"写入文件错误: {str(e)}")
    
    def _safe_join(self, base: str, rel: str) -> Optional[str]:
        base = os.path.abspath(base)
        full = os.path.abspath(os.path.join(base, rel))
        if full.startswith(base):
            return full
        return None


class TempFileDeleteTool(BaseTool):
    """临时文件删除工具"""
    
    name = "temp_file_delete"
    description = "删除临时工作区中的文件"
    security_level = "medium"
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对于工作区的文件路径"
            }
        },
        "required": ["path"]
    }
    
    async def execute(self, args: Dict[str, Any], context: Dict) -> ToolResult:
        workspace_path = context.get("workspace_path")
        if not workspace_path:
            return ToolResult(success=False, result=None, error="工作区未初始化")
        
        rel_path = args.get("path", "").strip()
        if not rel_path:
            return ToolResult(success=False, result=None, error="文件路径不能为空")
        
        # 安全检查
        full_path = self._safe_join(workspace_path, rel_path)
        if full_path is None:
            return ToolResult(success=False, result=None, error="非法路径")
        
        try:
            if not os.path.exists(full_path):
                return ToolResult(success=False, result=None, error=f"文件不存在: {rel_path}")
            
            if os.path.isdir(full_path):
                shutil.rmtree(full_path)
            else:
                os.remove(full_path)
            
            # 更新白板
            whiteboard = context.get("whiteboard")
            if whiteboard:
                whiteboard.remove_workspace_file(rel_path)
            
            return ToolResult(success=True, result={"message": f"已删除: {rel_path}"})
        except Exception as e:
            return ToolResult(success=False, result=None, error=f"删除文件错误: {str(e)}")
    
    def _safe_join(self, base: str, rel: str) -> Optional[str]:
        base = os.path.abspath(base)
        full = os.path.abspath(os.path.join(base, rel))
        if full.startswith(base):
            return full
        return None


class TempListFilesTool(BaseTool):
    """列出工作区文件"""
    
    name = "temp_list_files"
    description = "列出临时工作区中的文件和目录"
    security_level = "medium"
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对于工作区的目录路径（默认为根目录）"
            },
            "recursive": {
                "type": "boolean",
                "description": "是否递归列出子目录（默认false）"
            }
        },
        "required": []
    }
    
    async def execute(self, args: Dict[str, Any], context: Dict) -> ToolResult:
        workspace_path = context.get("workspace_path")
        if not workspace_path:
            return ToolResult(success=False, result=None, error="工作区未初始化")
        
        rel_path = args.get("path", "").strip() or "."
        recursive = args.get("recursive", False)
        
        # 安全检查
        full_path = self._safe_join(workspace_path, rel_path)
        if full_path is None:
            return ToolResult(success=False, result=None, error="非法路径")
        
        try:
            if not os.path.exists(full_path):
                return ToolResult(success=False, result=None, error=f"目录不存在: {rel_path}")
            
            files = []
            if recursive:
                for root, dirs, filenames in os.walk(full_path):
                    rel_root = os.path.relpath(root, full_path)
                    for f in filenames:
                        full_file = os.path.join(root, f)
                        if rel_root == ".":
                            file_rel = f
                        else:
                            file_rel = os.path.join(rel_root, f)
                        files.append({
                            "name": file_rel,
                            "size": os.path.getsize(full_file),
                            "type": "file"
                        })
                    for d in dirs:
                        if rel_root == ".":
                            dir_rel = d
                        else:
                            dir_rel = os.path.join(rel_root, d)
                        files.append({
                            "name": dir_rel,
                            "type": "directory"
                        })
            else:
                for item in os.listdir(full_path):
                    item_path = os.path.join(full_path, item)
                    if os.path.isdir(item_path):
                        files.append({"name": item, "type": "directory"})
                    else:
                        files.append({
                            "name": item,
                            "size": os.path.getsize(item_path),
                            "type": "file"
                        })
            
            return ToolResult(success=True, result={"files": files, "count": len(files)})
        except Exception as e:
            return ToolResult(success=False, result=None, error=f"列出文件错误: {str(e)}")
    
    def _safe_join(self, base: str, rel: str) -> Optional[str]:
        base = os.path.abspath(base)
        full = os.path.abspath(os.path.join(base, rel))
        if full.startswith(base):
            return full
        return None


class CodeExecuteTool(BaseTool):
    """代码执行工具"""
    
    name = "code_execute"
    description = "在临时工作区中执行代码，返回stdout、stderr和exit_code"
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
                "description": "编程语言：python, bash, node",
                "enum": ["python", "bash", "node"]
            }
        },
        "required": ["code", "language"]
    }
    
    # 允许的语言及其解释器
    INTERPRETERS = {
        "python": ["python3", "-c"],
        "bash": ["bash", "-c"],
        "node": ["node", "-e"]
    }
    
    async def execute(self, args: Dict[str, Any], context: Dict) -> ToolResult:
        workspace_path = context.get("workspace_path")
        if not workspace_path:
            return ToolResult(success=False, result=None, error="工作区未初始化")
        
        code = args.get("code", "")
        language = args.get("language", "python")
        
        if not code:
            return ToolResult(success=False, result=None, error="代码不能为空")
        
        # 检查语言是否被允许
        workspace_manager = context.get("workspace_manager")
        if workspace_manager:
            allowed = workspace_manager.config.allowed_languages
            if language not in allowed:
                return ToolResult(
                    success=False, 
                    result=None, 
                    error=f"语言 '{language}' 不被允许。允许的语言: {allowed}"
                )
        
        if language not in self.INTERPRETERS:
            return ToolResult(
                success=False, 
                result=None, 
                error=f"不支持的语言: {language}"
            )
        
        try:
            interpreter = self.INTERPRETERS[language]
            cmd = interpreter + [code]
            
            # 获取超时设置
            timeout = 30
            if workspace_manager:
                timeout = workspace_manager.config.execution_timeout_sec
            
            # 执行代码
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
                process.kill()
                return ToolResult(
                    success=False, 
                    result=None, 
                    error=f"执行超时（{timeout}秒）"
                )
            
            result = {
                "stdout": stdout.decode('utf-8', errors='replace'),
                "stderr": stderr.decode('utf-8', errors='replace'),
                "exit_code": process.returncode
            }
            
            return ToolResult(success=True, result=result)
        except Exception as e:
            return ToolResult(success=False, result=None, error=f"执行错误: {str(e)}")
