"""路径沙箱模块 - 防止路径遍历攻击"""
import os
import re
import stat
import shutil
import hashlib
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, List, Tuple, Dict, Any
from enum import Enum
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class PathSecurityError(Exception):
    """路径安全错误"""
    pass


class FileType(Enum):
    """文件类型"""
    TEXT = "text"
    BINARY = "binary"
    EXECUTABLE = "executable"
    SYMLINK = "symlink"
    DIRECTORY = "directory"
    UNKNOWN = "unknown"


@dataclass
class FileCheckResult:
    """文件检查结果"""
    allowed: bool
    file_type: FileType = FileType.UNKNOWN
    error: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    size: int = 0
    mime_type: Optional[str] = None


@dataclass
class WorkspaceLimits:
    """工作区限制配置"""
    max_file_size: int = 10 * 1024 * 1024  # 10 MB
    max_total_size: int = 100 * 1024 * 1024  # 100 MB
    max_file_count: int = 1000
    allowed_extensions: List[str] = field(default_factory=lambda: [
        ".txt", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml",
        ".html", ".css", ".xml", ".csv", ".log", ".sh", ".bash",
        ".java", ".c", ".cpp", ".h", ".hpp", ".rs", ".go", ".rb",
        ".php", ".sql", ".toml", ".ini", ".cfg", ".conf"
    ])
    forbidden_extensions: List[str] = field(default_factory=lambda: [
        ".key", ".pem", ".env", ".secret", ".credentials",
        ".exe", ".dll", ".so", ".dylib", ".bin",
        ".shd", ".shadow", ".passwd"
    ])
    forbidden_patterns: List[str] = field(default_factory=lambda: [
        r"^\.env",           # .env 文件
        r"^\.git",           # .git 目录
        r"^\.ssh",           # .ssh 目录
        r"^\.aws",           # .aws 目录
        r"^\.gcp",           # .gcp 目录
        r".*\.key$",         # 密钥文件
        r".*\.pem$",         # PEM证书
        r".*\.p12$",         # PKCS12证书
        r".*\.pfx$",         # PFX证书
        r"^id_rsa",          # SSH私钥
        r"^id_ed25519",      # Ed25519私钥
        r"^\.htpasswd",      # Apache密码文件
    ])
    allow_hidden_files: bool = False


class PathSandbox:
    """路径沙箱 - 防止路径遍历和文件系统攻击"""
    
    def __init__(self, workspace_root: str, limits: WorkspaceLimits = None):
        """
        初始化路径沙箱
        
        Args:
            workspace_root: 工作区根目录
            limits: 工作区限制配置
        """
        self.workspace_root = os.path.abspath(workspace_root)
        self.limits = limits or WorkspaceLimits()
        self._current_size = 0
        self._file_count = 0
        self._size_cache: Dict[str, int] = {}
        
        # 确保工作区存在
        self._ensure_workspace()
        
        # 初始化时计算工作区大小
        self._calculate_workspace_size()
    
    def _ensure_workspace(self):
        """确保工作区存在并设置正确权限"""
        if not os.path.exists(self.workspace_root):
            os.makedirs(self.workspace_root, mode=0o700)
            logger.info(f"创建工作区: {self.workspace_root}")
        else:
            # 检查权限
            self._check_workspace_permissions()
    
    def _check_workspace_permissions(self):
        """检查工作区权限"""
        stat_info = os.stat(self.workspace_root)
        mode = stat_info.st_mode
        
        # 检查是否为目录
        if not stat.S_ISDIR(mode):
            raise PathSecurityError(f"工作区路径不是目录: {self.workspace_root}")
        
        # 检查权限（应该只有所有者可读写执行）
        expected_mode = 0o700
        current_mode = stat.S_IMODE(mode)
        
        if current_mode != expected_mode:
            logger.warning(
                f"工作区权限 {oct(current_mode)} 不符合建议 {oct(expected_mode)}"
            )
    
    def _calculate_workspace_size(self):
        """计算工作区当前大小"""
        self._current_size = 0
        self._file_count = 0
        self._size_cache.clear()
        
        for root, dirs, files in os.walk(self.workspace_root):
            for f in files:
                file_path = os.path.join(root, f)
                try:
                    size = os.path.getsize(file_path)
                    rel_path = os.path.relpath(file_path, self.workspace_root)
                    self._size_cache[rel_path] = size
                    self._current_size += size
                    self._file_count += 1
                except OSError:
                    pass
        
        logger.debug(f"工作区大小: {self._current_size} bytes, 文件数: {self._file_count}")
    
    def safe_path(self, user_path: str, must_exist: bool = False) -> str:
        """
        安全路径解析 - 防止路径遍历攻击
        
        Args:
            user_path: 用户提供的相对路径
            must_exist: 路径是否必须存在
            
        Returns:
            解析后的绝对路径
            
        Raises:
            PathSecurityError: 路径不安全时抛出
        """
        if not user_path:
            raise PathSecurityError("路径不能为空")
        
        # 清理路径
        user_path = user_path.strip()
        
        # 检查危险模式
        dangerous_patterns = [
            '..',           # 路径遍历
            '~',            # 用户目录
            '\x00',         # 空字节
            '\n',           # 换行
            '\r',           # 回车
        ]
        
        for pattern in dangerous_patterns:
            if pattern in user_path:
                raise PathSecurityError(f"路径包含危险字符: {pattern}")
        
        # 检查绝对路径
        if os.path.isabs(user_path):
            raise PathSecurityError("不允许使用绝对路径")
        
        # 规范化路径
        try:
            # 拼接路径
            full_path = os.path.normpath(os.path.join(self.workspace_root, user_path))
        except Exception as e:
            raise PathSecurityError(f"路径规范化失败: {e}")
        
        # 解析符号链接
        try:
            real_path = os.path.realpath(full_path)
        except Exception as e:
            raise PathSecurityError(f"无法解析路径: {e}")
        
        # 验证路径在工作区内
        if not real_path.startswith(self.workspace_root):
            raise PathSecurityError(
                f"路径遍历攻击检测: {user_path} 解析为 {real_path}"
            )
        
        # 检查路径是否在禁止列表中
        self._check_forbidden_path(user_path)
        
        # 检查符号链接目标
        if os.path.islink(full_path):
            link_target = os.readlink(full_path)
            if os.path.isabs(link_target):
                raise PathSecurityError("符号链接不能指向绝对路径")
            if '..' in link_target:
                raise PathSecurityError("符号链接不能包含父目录引用")
        
        # 检查路径是否存在
        if must_exist and not os.path.exists(real_path):
            raise PathSecurityError(f"路径不存在: {user_path}")
        
        return real_path
    
    def _check_forbidden_path(self, rel_path: str):
        """检查路径是否在禁止列表中"""
        filename = os.path.basename(rel_path)
        
        # 检查隐藏文件
        if not self.limits.allow_hidden_files and filename.startswith('.'):
            raise PathSecurityError(f"不允许访问隐藏文件: {filename}")
        
        # 检查禁止的扩展名
        ext = os.path.splitext(filename)[1].lower()
        if ext in self.limits.forbidden_extensions:
            raise PathSecurityError(f"禁止的文件类型: {ext}")
        
        # 检查禁止的模式
        for pattern in self.limits.forbidden_patterns:
            if re.match(pattern, filename, re.IGNORECASE):
                raise PathSecurityError(f"禁止的文件名模式: {pattern}")
    
    def check_file_write(self, rel_path: str, content_size: int) -> FileCheckResult:
        """
        检查是否允许写入文件
        
        Args:
            rel_path: 相对路径
            content_size: 内容大小
            
        Returns:
            FileCheckResult: 检查结果
        """
        warnings = []
        
        # 检查路径安全性
        try:
            full_path = self.safe_path(rel_path)
        except PathSecurityError as e:
            return FileCheckResult(
                allowed=False,
                error=str(e),
                warnings=warnings
            )
        
        # 检查文件大小限制
        if content_size > self.limits.max_file_size:
            return FileCheckResult(
                allowed=False,
                error=f"文件大小 {content_size} 超过限制 {self.limits.max_file_size}",
                warnings=warnings
            )
        
        # 检查工作区总大小
        existing_size = self._size_cache.get(rel_path, 0)
        new_total = self._current_size - existing_size + content_size
        
        if new_total > self.limits.max_total_size:
            return FileCheckResult(
                allowed=False,
                error=f"工作区总大小将超过限制 {self.limits.max_total_size}",
                warnings=warnings
            )
        
        # 检查文件数量限制
        if rel_path not in self._size_cache:
            if self._file_count >= self.limits.max_file_count:
                return FileCheckResult(
                    allowed=False,
                    error=f"文件数量超过限制 {self.limits.max_file_count}",
                    warnings=warnings
                )
        
        # 检查文件扩展名
        ext = os.path.splitext(rel_path)[1].lower()
        if ext and ext not in self.limits.allowed_extensions:
            warnings.append(f"文件扩展名 {ext} 不在推荐列表中")
        
        # 检查是否为可执行二进制
        file_type = self._detect_file_type(content_size, ext, None)
        if file_type == FileType.EXECUTABLE:
            return FileCheckResult(
                allowed=False,
                error="不允许写入可执行二进制文件",
                file_type=file_type,
                warnings=warnings
            )
        
        return FileCheckResult(
            allowed=True,
            file_type=file_type,
            warnings=warnings,
            size=content_size
        )
    
    def check_file_read(self, rel_path: str) -> FileCheckResult:
        """
        检查是否允许读取文件
        
        Args:
            rel_path: 相对路径
            
        Returns:
            FileCheckResult: 检查结果
        """
        warnings = []
        
        # 检查路径安全性
        try:
            full_path = self.safe_path(rel_path, must_exist=True)
        except PathSecurityError as e:
            return FileCheckResult(
                allowed=False,
                error=str(e),
                warnings=warnings
            )
        
        # 检查是否为目录
        if os.path.isdir(full_path):
            return FileCheckResult(
                allowed=True,
                file_type=FileType.DIRECTORY,
                warnings=warnings
            )
        
        # 获取文件信息
        try:
            stat_info = os.stat(full_path)
            file_size = stat_info.st_size
        except OSError as e:
            return FileCheckResult(
                allowed=False,
                error=f"无法获取文件信息: {e}",
                warnings=warnings
            )
        
        # 检查文件大小
        if file_size > self.limits.max_file_size:
            return FileCheckResult(
                allowed=False,
                error=f"文件大小 {file_size} 超过读取限制 {self.limits.max_file_size}",
                size=file_size,
                warnings=warnings
            )
        
        # 检查是否为符号链接
        if os.path.islink(full_path):
            warnings.append("文件是符号链接")
            file_type = FileType.SYMLINK
        else:
            # 检测文件类型
            ext = os.path.splitext(rel_path)[1].lower()
            file_type = self._detect_file_type_from_path(full_path, ext)
        
        return FileCheckResult(
            allowed=True,
            file_type=file_type,
            size=file_size,
            warnings=warnings
        )
    
    def check_file_delete(self, rel_path: str) -> FileCheckResult:
        """
        检查是否允许删除文件
        
        Args:
            rel_path: 相对路径
            
        Returns:
            FileCheckResult: 检查结果
        """
        warnings = []
        
        # 检查路径安全性
        try:
            full_path = self.safe_path(rel_path, must_exist=True)
        except PathSecurityError as e:
            return FileCheckResult(
                allowed=False,
                error=str(e),
                warnings=warnings
            )
        
        # 防止删除工作区根目录
        if full_path == self.workspace_root:
            return FileCheckResult(
                allowed=False,
                error="不允许删除工作区根目录",
                warnings=warnings
            )
        
        # 检查是否为目录
        is_dir = os.path.isdir(full_path)
        file_type = FileType.DIRECTORY if is_dir else FileType.UNKNOWN
        
        return FileCheckResult(
            allowed=True,
            file_type=file_type,
            warnings=warnings
        )
    
    def _detect_file_type(self, size: int, ext: str, content_sample: bytes) -> FileType:
        """检测文件类型"""
        # 可执行文件扩展名
        executable_exts = {'.exe', '.dll', '.so', '.dylib', '.bin', '.out'}
        if ext.lower() in executable_exts:
            return FileType.EXECUTABLE
        
        # 文本文件扩展名
        text_exts = {
            '.txt', '.md', '.py', '.js', '.ts', '.json', '.yaml', '.yml',
            '.html', '.css', '.xml', '.csv', '.log', '.sh', '.bash',
            '.java', '.c', '.cpp', '.h', '.hpp', '.rs', '.go', '.rb'
        }
        if ext.lower() in text_exts:
            return FileType.TEXT
        
        # 如果有内容样本，尝试检测
        if content_sample:
            # 检查ELF魔数
            if content_sample[:4] == b'\x7fELF':
                return FileType.EXECUTABLE
            # 检查PE魔数
            if content_sample[:2] == b'MZ':
                return FileType.EXECUTABLE
            # 检查是否为文本
            try:
                content_sample.decode('utf-8')
                return FileType.TEXT
            except:
                return FileType.BINARY
        
        return FileType.UNKNOWN
    
    def _detect_file_type_from_path(self, path: str, ext: str) -> FileType:
        """从文件路径检测文件类型"""
        # 可执行文件扩展名
        executable_exts = {'.exe', '.dll', '.so', '.dylib', '.bin', '.out'}
        if ext.lower() in executable_exts:
            return FileType.EXECUTABLE
        
        # 文本文件扩展名
        text_exts = {
            '.txt', '.md', '.py', '.js', '.ts', '.json', '.yaml', '.yml',
            '.html', '.css', '.xml', '.csv', '.log', '.sh', '.bash',
            '.java', '.c', '.cpp', '.h', '.hpp', '.rs', '.go', '.rb'
        }
        if ext.lower() in text_exts:
            return FileType.TEXT
        
        # 读取文件头部检测
        try:
            with open(path, 'rb') as f:
                header = f.read(8)
            return self._detect_file_type(0, ext, header)
        except:
            return FileType.UNKNOWN
    
    def update_stats(self, rel_path: str, size: int, is_delete: bool = False):
        """
        更新工作区统计信息
        
        Args:
            rel_path: 相对路径
            size: 文件大小
            is_delete: 是否为删除操作
        """
        if is_delete:
            if rel_path in self._size_cache:
                self._current_size -= self._size_cache[rel_path]
                del self._size_cache[rel_path]
                self._file_count -= 1
        else:
            old_size = self._size_cache.get(rel_path, 0)
            self._current_size += size - old_size
            self._size_cache[rel_path] = size
            if old_size == 0:
                self._file_count += 1
    
    def get_stats(self) -> Dict[str, Any]:
        """获取工作区统计信息"""
        return {
            "workspace_root": self.workspace_root,
            "current_size": self._current_size,
            "current_size_mb": round(self._current_size / (1024 * 1024), 2),
            "max_size": self.limits.max_total_size,
            "max_size_mb": round(self.limits.max_total_size / (1024 * 1024), 2),
            "usage_percent": round(self._current_size / self.limits.max_total_size * 100, 2),
            "file_count": self._file_count,
            "max_file_count": self.limits.max_file_count,
            "is_within_limits": (
                self._current_size <= self.limits.max_total_size and
                self._file_count <= self.limits.max_file_count
            )
        }
    
    def clear_workspace(self):
        """清空工作区"""
        for item in os.listdir(self.workspace_root):
            item_path = os.path.join(self.workspace_root, item)
            try:
                if os.path.isdir(item_path):
                    shutil.rmtree(item_path)
                else:
                    os.remove(item_path)
            except Exception as e:
                logger.error(f"清理工作区失败: {e}")
        
        self._current_size = 0
        self._file_count = 0
        self._size_cache.clear()
        logger.info(f"工作区已清空: {self.workspace_root}")


class PathSandboxFactory:
    """路径沙箱工厂"""
    
    _instances: Dict[str, PathSandbox] = {}
    
    @classmethod
    def get_or_create(cls, workspace_root: str, limits: WorkspaceLimits = None) -> PathSandbox:
        """获取或创建沙箱实例"""
        canonical_root = os.path.abspath(workspace_root)
        
        if canonical_root not in cls._instances:
            cls._instances[canonical_root] = PathSandbox(canonical_root, limits)
        
        return cls._instances[canonical_root]
    
    @classmethod
    def remove(cls, workspace_root: str):
        """移除沙箱实例"""
        canonical_root = os.path.abspath(workspace_root)
        if canonical_root in cls._instances:
            del cls._instances[canonical_root]
    
    @classmethod
    def get_all_stats(cls) -> Dict[str, Dict]:
        """获取所有沙箱的统计信息"""
        return {
            root: sandbox.get_stats()
            for root, sandbox in cls._instances.items()
        }
