"""文件操作插件"""
import os
import shutil
import hashlib
import asyncio
from pathlib import Path
from typing import Dict, Any, List, Optional

from tools.base import BaseTool, ToolResult, BasePlugin
from path_sandbox import (
    PathSandbox, PathSandboxFactory, WorkspaceLimits,
    PathSecurityError, FileCheckResult, FileType
)


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
            },
            "encoding": {
                "type": "string",
                "description": "文件编码（默认utf-8）"
            },
            "lines": {
                "type": "string",
                "description": "读取行范围，如 '1-10' 或 '5:' 或 ':10'"
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
        
        encoding = args.get("encoding", "utf-8")
        lines_spec = args.get("lines")
        
        # 使用路径沙箱进行安全检查
        sandbox = PathSandboxFactory.get_or_create(workspace_path)
        
        try:
            check_result = sandbox.check_file_read(rel_path)
            if not check_result.allowed:
                return ToolResult(success=False, result=None, error=check_result.error)
            
            full_path = sandbox.safe_path(rel_path, must_exist=True)
            
            if os.path.isdir(full_path):
                return ToolResult(success=False, result=None, error="路径是目录，不是文件")
            
            file_size = check_result.size
            
            with open(full_path, 'r', encoding=encoding, errors='replace') as f:
                if lines_spec:
                    content = self._read_lines(f, lines_spec)
                else:
                    content = f.read()
            
            result_data = {
                "path": rel_path,
                "content": content,
                "size": file_size,
                "encoding": encoding
            }
            
            if check_result.warnings:
                result_data["warnings"] = check_result.warnings
            
            return ToolResult(success=True, result=result_data)
            
        except PathSecurityError as e:
            return ToolResult(success=False, result=None, error=f"安全错误: {str(e)}")
        except UnicodeDecodeError:
            # 尝试二进制读取
            try:
                full_path = sandbox.safe_path(rel_path, must_exist=True)
                with open(full_path, 'rb') as f:
                    content = f.read()
                return ToolResult(
                    success=True,
                    result={
                        "path": rel_path,
                        "content": f"[二进制文件，大小: {len(content)} 字节]",
                        "size": len(content),
                        "binary": True
                    }
                )
            except Exception as e:
                return ToolResult(success=False, result=None, error=f"读取文件错误: {str(e)}")
        except Exception as e:
            return ToolResult(success=False, result=None, error=f"读取文件错误: {str(e)}")
    
    def _read_lines(self, f, lines_spec: str) -> str:
        """读取指定行"""
        all_lines = f.readlines()
        
        if ':' in lines_spec:
            parts = lines_spec.split(':')
            if len(parts) == 2:
                start = int(parts[0]) if parts[0] else 0
                end = int(parts[1]) if parts[1] else len(all_lines)
                return ''.join(all_lines[start:end])
        elif '-' in lines_spec:
            parts = lines_spec.split('-')
            if len(parts) == 2:
                start = int(parts[0])
                end = int(parts[1])
                return ''.join(all_lines[start-1:end])
        
        return ''.join(all_lines)


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
            },
            "mode": {
                "type": "string",
                "description": "写入模式：write(覆盖) 或 append(追加)",
                "enum": ["write", "append"]
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
        mode = args.get("mode", "write")
        
        if not rel_path:
            return ToolResult(success=False, result=None, error="文件路径不能为空")
        
        # 使用路径沙箱进行安全检查
        sandbox = PathSandboxFactory.get_or_create(workspace_path)
        content_size = len(content.encode('utf-8'))
        
        try:
            # 检查写入权限
            check_result = sandbox.check_file_write(rel_path, content_size)
            if not check_result.allowed:
                return ToolResult(success=False, result=None, error=check_result.error)
            
            full_path = sandbox.safe_path(rel_path)
            
            # 创建父目录
            parent_dir = os.path.dirname(full_path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)
            
            # 写入文件
            write_mode = 'a' if mode == "append" else 'w'
            with open(full_path, write_mode, encoding='utf-8') as f:
                f.write(content)
            
            # 更新沙箱统计
            sandbox.update_stats(rel_path, content_size)
            
            # 更新白板
            whiteboard = context.get("whiteboard")
            if whiteboard:
                file_hash = hashlib.md5(content.encode()).hexdigest()[:8]
                whiteboard.update_workspace_file(rel_path, content_size, os.path.getmtime(full_path), file_hash)
            
            result_data = {
                "path": rel_path,
                "size": content_size,
                "mode": mode,
                "message": f"文件已{'追加' if mode == 'append' else '写入'}: {rel_path}"
            }
            
            if check_result.warnings:
                result_data["warnings"] = check_result.warnings
            
            return ToolResult(success=True, result=result_data)
            
        except PathSecurityError as e:
            return ToolResult(success=False, result=None, error=f"安全错误: {str(e)}")
        except Exception as e:
            return ToolResult(success=False, result=None, error=f"写入文件错误: {str(e)}")


class TempFileDeleteTool(BaseTool):
    """临时文件删除工具"""
    
    name = "temp_file_delete"
    description = "删除临时工作区中的文件或目录"
    security_level = "medium"
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对于工作区的文件或目录路径"
            },
            "recursive": {
                "type": "boolean",
                "description": "是否递归删除目录（默认false）"
            }
        },
        "required": ["path"]
    }
    
    async def execute(self, args: Dict[str, Any], context: Dict) -> ToolResult:
        workspace_path = context.get("workspace_path")
        if not workspace_path:
            return ToolResult(success=False, result=None, error="工作区未初始化")
        
        rel_path = args.get("path", "").strip()
        recursive = args.get("recursive", False)
        
        if not rel_path:
            return ToolResult(success=False, result=None, error="文件路径不能为空")
        
        # 使用路径沙箱进行安全检查
        sandbox = PathSandboxFactory.get_or_create(workspace_path)
        
        try:
            # 检查删除权限
            check_result = sandbox.check_file_delete(rel_path)
            if not check_result.allowed:
                return ToolResult(success=False, result=None, error=check_result.error)
            
            full_path = sandbox.safe_path(rel_path, must_exist=True)
            
            # 获取文件大小用于更新统计
            is_dir = os.path.isdir(full_path)
            
            if is_dir:
                if recursive:
                    shutil.rmtree(full_path)
                else:
                    os.rmdir(full_path)  # 只能删除空目录
            else:
                file_size = os.path.getsize(full_path)
                os.remove(full_path)
                sandbox.update_stats(rel_path, file_size, is_delete=True)
            
            # 更新白板
            whiteboard = context.get("whiteboard")
            if whiteboard:
                whiteboard.remove_workspace_file(rel_path)
            
            return ToolResult(
                success=True, 
                result={"message": f"已删除: {rel_path}"}
            )
            
        except PathSecurityError as e:
            return ToolResult(success=False, result=None, error=f"安全错误: {str(e)}")
        except OSError as e:
            if "Directory not empty" in str(e):
                return ToolResult(success=False, result=None, error="目录非空，请使用 recursive=true")
            return ToolResult(success=False, result=None, error=f"删除错误: {str(e)}")
        except Exception as e:
            return ToolResult(success=False, result=None, error=f"删除文件错误: {str(e)}")


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
            },
            "show_hidden": {
                "type": "boolean",
                "description": "是否显示隐藏文件（默认false）"
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
        show_hidden = args.get("show_hidden", False)
        
        # 使用路径沙箱进行安全检查
        sandbox = PathSandboxFactory.get_or_create(workspace_path)
        
        try:
            full_path = sandbox.safe_path(rel_path, must_exist=True)
            
            if not os.path.isdir(full_path):
                return ToolResult(success=False, result=None, error=f"不是目录: {rel_path}")
            
            files = []
            
            if recursive:
                for root, dirs, filenames in os.walk(full_path):
                    # 过滤隐藏文件
                    if not show_hidden:
                        dirs[:] = [d for d in dirs if not d.startswith('.')]
                        filenames = [f for f in filenames if not f.startswith('.')]
                    
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
                            "type": "file",
                            "modified": os.path.getmtime(full_file)
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
                    if not show_hidden and item.startswith('.'):
                        continue
                    item_path = os.path.join(full_path, item)
                    if os.path.isdir(item_path):
                        files.append({
                            "name": item,
                            "type": "directory"
                        })
                    else:
                        files.append({
                            "name": item,
                            "size": os.path.getsize(item_path),
                            "type": "file",
                            "modified": os.path.getmtime(item_path)
                        })
            
            # 排序：目录在前，然后按名称
            files.sort(key=lambda x: (0 if x["type"] == "directory" else 1, x["name"]))
            
            # 添加工作区统计
            stats = sandbox.get_stats()
            
            return ToolResult(
                success=True, 
                result={
                    "path": rel_path,
                    "files": files,
                    "count": len(files),
                    "workspace_stats": {
                        "size_mb": stats["current_size_mb"],
                        "file_count": stats["file_count"],
                        "usage_percent": stats["usage_percent"]
                    }
                }
            )
            
        except PathSecurityError as e:
            return ToolResult(success=False, result=None, error=f"安全错误: {str(e)}")
        except Exception as e:
            return ToolResult(success=False, result=None, error=f"列出文件错误: {str(e)}")


class FileOpsPlugin(BasePlugin):
    """文件操作插件"""
    
    plugin_name = "file_ops"
    plugin_version = "1.1.0"
    plugin_description = "工作区文件操作工具（带安全沙箱）"
    plugin_author = "system"
    plugin_security_level = "medium"
    plugin_tags = ["workspace", "file", "io", "security"]
    
    def __init__(self):
        super().__init__()
    
    def initialize(self, config: Dict[str, Any] = None):
        """初始化插件"""
        self.register_tool(TempFileReadTool())
        self.register_tool(TempFileWriteTool())
        self.register_tool(TempFileDeleteTool())
        self.register_tool(TempListFilesTool())