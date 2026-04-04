"""工作区管理模块"""
import os
import shutil
import uuid
import time
import hashlib
from pathlib import Path
from typing import Optional, Dict, List, Any
from dataclasses import dataclass

from config_loader import WorkspaceConfig


@dataclass
class WorkspaceStats:
    """工作区统计信息"""
    total_size: int
    file_count: int
    dir_count: int
    created_at: float
    last_modified: float


class WorkspaceManager:
    """工作区管理器"""
    
    def __init__(self, config: WorkspaceConfig):
        self.config = config
        self._session_id: Optional[str] = None
        self._session_path: Optional[str] = None
        self._created_at: Optional[float] = None
    
    @property
    def session_id(self) -> Optional[str]:
        return self._session_id
    
    @property
    def session_path(self) -> Optional[str]:
        return self._session_path
    
    def create_session(self, session_id: Optional[str] = None) -> str:
        """创建新的会话工作区"""
        self._session_id = session_id or str(uuid.uuid4())[:8]
        
        # 确保基础目录存在
        base_dir = os.path.abspath(self.config.base_dir)
        os.makedirs(base_dir, exist_ok=True)
        
        # 创建会话目录
        self._session_path = os.path.join(base_dir, f"session_{self._session_id}")
        os.makedirs(self._session_path, exist_ok=True)
        
        self._created_at = time.time()
        
        return self._session_id
    
    def get_session_path(self) -> Optional[str]:
        """获取当前会话的工作区路径"""
        return self._session_path
    
    def close_session(self, cleanup: bool = True) -> bool:
        """关闭会话"""
        if cleanup and self._session_path and os.path.exists(self._session_path):
            try:
                shutil.rmtree(self._session_path)
            except Exception:
                pass
        
        self._session_id = None
        self._session_path = None
        self._created_at = None
        return True
    
    def check_size_limit(self, additional_bytes: int = 0) -> bool:
        """检查是否超过大小限制"""
        if not self._session_path or not os.path.exists(self._session_path):
            return True
        
        current_size = self._get_directory_size(self._session_path)
        limit = self.config.per_session_limit_mb * 1024 * 1024
        
        return (current_size + additional_bytes) <= limit
    
    def get_stats(self) -> Optional[WorkspaceStats]:
        """获取工作区统计信息"""
        if not self._session_path or not os.path.exists(self._session_path):
            return None
        
        total_size = self._get_directory_size(self._session_path)
        file_count = 0
        dir_count = 0
        last_modified = self._created_at or time.time()
        
        for root, dirs, files in os.walk(self._session_path):
            dir_count += len(dirs)
            file_count += len(files)
            for f in files:
                fpath = os.path.join(root, f)
                mtime = os.path.getmtime(fpath)
                if mtime > last_modified:
                    last_modified = mtime
        
        return WorkspaceStats(
            total_size=total_size,
            file_count=file_count,
            dir_count=dir_count,
            created_at=self._created_at or time.time(),
            last_modified=last_modified
        )
    
    def list_files(self) -> List[Dict[str, Any]]:
        """列出工作区中的所有文件"""
        if not self._session_path or not os.path.exists(self._session_path):
            return []
        
        files = []
        for root, _, filenames in os.walk(self._session_path):
            for f in filenames:
                fpath = os.path.join(root, f)
                rel_path = os.path.relpath(fpath, self._session_path)
                files.append({
                    "path": rel_path,
                    "size": os.path.getsize(fpath),
                    "mtime": os.path.getmtime(fpath)
                })
        
        return files
    
    def file_exists(self, rel_path: str) -> bool:
        """检查文件是否存在"""
        if not self._session_path:
            return False
        full_path = os.path.join(self._session_path, rel_path)
        return os.path.exists(full_path)
    
    def get_file_path(self, rel_path: str) -> Optional[str]:
        """获取文件的完整路径（带安全检查）"""
        if not self._session_path:
            return None
        
        full_path = os.path.abspath(os.path.join(self._session_path, rel_path))
        if full_path.startswith(self._session_path):
            return full_path
        return None
    
    def cleanup_old_sessions(self, keep_count: int = 3) -> int:
        """清理旧会话，保留最近N个"""
        base_dir = os.path.abspath(self.config.base_dir)
        if not os.path.exists(base_dir):
            return 0
        
        # 获取所有会话目录
        sessions = []
        for item in os.listdir(base_dir):
            if item.startswith("session_"):
                item_path = os.path.join(base_dir, item)
                if os.path.isdir(item_path):
                    mtime = os.path.getmtime(item_path)
                    sessions.append((item_path, mtime))
        
        # 按修改时间排序（最新的在前）
        sessions.sort(key=lambda x: x[1], reverse=True)
        
        # 删除超出保留数量的会话
        removed = 0
        for item_path, _ in sessions[keep_count:]:
            # 不删除当前会话
            if item_path == self._session_path:
                continue
            try:
                shutil.rmtree(item_path)
                removed += 1
            except Exception:
                pass
        
        return removed
    
    def _get_directory_size(self, path: str) -> int:
        """计算目录总大小"""
        total = 0
        for root, _, files in os.walk(path):
            for f in files:
                fpath = os.path.join(root, f)
                try:
                    total += os.path.getsize(fpath)
                except OSError:
                    pass
        return total
    
    def create_snapshot(self) -> Optional[Dict[str, Any]]:
        """创建工作区快照信息"""
        stats = self.get_stats()
        if not stats:
            return None
        
        return {
            "session_id": self._session_id,
            "path": self._session_path,
            "total_size": stats.total_size,
            "file_count": stats.file_count,
            "created_at": stats.created_at,
            "files": self.list_files()
        }


class WorkspacePool:
    """工作区池管理器（多会话支持）"""
    
    def __init__(self, config: WorkspaceConfig):
        self.config = config
        self._sessions: Dict[str, WorkspaceManager] = {}
    
    def create_session(self, session_id: Optional[str] = None) -> WorkspaceManager:
        """创建新会话"""
        ws = WorkspaceManager(self.config)
        sid = ws.create_session(session_id)
        self._sessions[sid] = ws
        return ws
    
    def get_session(self, session_id: str) -> Optional[WorkspaceManager]:
        """获取会话"""
        return self._sessions.get(session_id)
    
    def close_session(self, session_id: str, cleanup: bool = True) -> bool:
        """关闭会话"""
        ws = self._sessions.pop(session_id, None)
        if ws:
            return ws.close_session(cleanup)
        return False
    
    def list_sessions(self) -> List[str]:
        """列出所有会话ID"""
        return list(self._sessions.keys())
    
    def cleanup_all(self):
        """清理所有会话"""
        for ws in list(self._sessions.values()):
            ws.close_session(cleanup=True)
        self._sessions.clear()
