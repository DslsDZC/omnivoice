"""长期记忆存储模块 - 持久化存储层"""
import json
import time
import hashlib
import os
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from enum import Enum
import threading


class MemoryType(Enum):
    """记忆类型"""
    USER_PREFERENCE = "user_preference"   # 用户偏好
    HISTORY_CONCLUSION = "history_conclusion"  # 历史结论
    FACT_KNOWLEDGE = "fact_knowledge"     # 事实知识
    TASK_STATE = "task_state"             # 任务状态
    PROJECT_INFO = "project_info"         # 项目信息


class MemorySource(Enum):
    """记忆来源"""
    USER_COMMAND = "user_command"         # 用户命令 (/remember)
    SYSTEM_AUTO = "system_auto"           # 系统自动提取
    AGENT_REQUEST = "agent_request"       # 代理请求
    PROJECT_SHARED = "project_shared"     # 项目共享
    GLOBAL_PUBLIC = "global_public"       # 全局公共


class MemoryPriority(Enum):
    """记忆优先级"""
    HIGH = 3      # 用户显式记忆
    MEDIUM = 2    # 系统自动记忆
    LOW = 1       # 代理请求记忆


@dataclass
class MemoryItem:
    """记忆条目"""
    id: str
    type: MemoryType
    content: str
    source: MemorySource
    priority: MemoryPriority
    user_id: str
    project_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    access_count: int = 0
    tags: List[str] = field(default_factory=list)
    embedding: Optional[List[float]] = None  # 向量嵌入（用于语义检索）
    metadata: Dict = field(default_factory=dict)
    is_verified: bool = False
    is_expired: bool = False
    expires_at: Optional[float] = None  # 过期时间戳
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            "id": self.id,
            "type": self.type.value,
            "content": self.content,
            "source": self.source.value,
            "priority": self.priority.value,
            "user_id": self.user_id,
            "project_id": self.project_id,
            "timestamp": self.timestamp,
            "last_accessed": self.last_accessed,
            "access_count": self.access_count,
            "tags": self.tags,
            "is_verified": self.is_verified,
            "is_expired": self.is_expired,
            "expires_at": self.expires_at,
            "metadata": self.metadata
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> "MemoryItem":
        """从字典创建"""
        return cls(
            id=data["id"],
            type=MemoryType(data["type"]),
            content=data["content"],
            source=MemorySource(data["source"]),
            priority=MemoryPriority(data["priority"]),
            user_id=data["user_id"],
            project_id=data.get("project_id"),
            timestamp=data.get("timestamp", time.time()),
            last_accessed=data.get("last_accessed", time.time()),
            access_count=data.get("access_count", 0),
            tags=data.get("tags", []),
            is_verified=data.get("is_verified", False),
            is_expired=data.get("is_expired", False),
            expires_at=data.get("expires_at"),
            metadata=data.get("metadata", {})
        )
    
    def touch(self):
        """更新访问时间和计数"""
        self.last_accessed = time.time()
        self.access_count += 1


@dataclass
class MemorySearchResult:
    """记忆搜索结果"""
    memory: MemoryItem
    relevance_score: float  # 相关性分数 0-1
    match_type: str  # semantic/keyword/tag


class MemoryStore:
    """记忆存储 - 持久化存储层"""
    
    # 时间衰减阈值（天）
    TIME_DECAY_THRESHOLD = 30
    
    def __init__(self, storage_path: str = "./memory_store"):
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        
        # 内存缓存
        self._cache: Dict[str, MemoryItem] = {}
        self._user_index: Dict[str, List[str]] = {}  # user_id -> [memory_ids]
        self._project_index: Dict[str, List[str]] = {}  # project_id -> [memory_ids]
        self._tag_index: Dict[str, List[str]] = {}  # tag -> [memory_ids]
        
        # 线程锁
        self._lock = threading.RLock()
        
        # 加载已有记忆
        self._load_all()
    
    def _get_user_file(self, user_id: str) -> Path:
        """获取用户存储文件路径"""
        safe_user_id = hashlib.md5(user_id.encode()).hexdigest()[:16]
        return self.storage_path / f"user_{safe_user_id}.json"
    
    def _get_project_file(self, project_id: str) -> Path:
        """获取项目存储文件路径"""
        safe_project_id = hashlib.md5(project_id.encode()).hexdigest()[:16]
        return self.storage_path / f"project_{safe_project_id}.json"
    
    def _get_global_file(self) -> Path:
        """获取全局存储文件路径"""
        return self.storage_path / "global_public.json"
    
    def _load_all(self):
        """加载所有记忆"""
        # 加载用户记忆
        for file_path in self.storage_path.glob("user_*.json"):
            self._load_file(file_path)
        
        # 加载项目记忆
        for file_path in self.storage_path.glob("project_*.json"):
            self._load_file(file_path)
        
        # 加载全局记忆
        global_file = self._get_global_file()
        if global_file.exists():
            self._load_file(global_file)
    
    def _load_file(self, file_path: Path):
        """加载单个文件"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            for item_data in data.get("memories", []):
                memory = MemoryItem.from_dict(item_data)
                self._cache[memory.id] = memory
                
                # 更新索引
                if memory.user_id:
                    if memory.user_id not in self._user_index:
                        self._user_index[memory.user_id] = []
                    self._user_index[memory.user_id].append(memory.id)
                
                if memory.project_id:
                    if memory.project_id not in self._project_index:
                        self._project_index[memory.project_id] = []
                    self._project_index[memory.project_id].append(memory.id)
                
                for tag in memory.tags:
                    if tag not in self._tag_index:
                        self._tag_index[tag] = []
                    self._tag_index[tag].append(memory.id)
        
        except Exception as e:
            print(f"加载记忆文件失败 {file_path}: {e}")
    
    def _save_user_memories(self, user_id: str):
        """保存用户记忆到文件"""
        file_path = self._get_user_file(user_id)
        memory_ids = self._user_index.get(user_id, [])
        memories = [self._cache[mid].to_dict() for mid in memory_ids if mid in self._cache]
        
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump({"user_id": user_id, "memories": memories}, f, ensure_ascii=False, indent=2)
    
    def _save_project_memories(self, project_id: str):
        """保存项目记忆到文件"""
        file_path = self._get_project_file(project_id)
        memory_ids = self._project_index.get(project_id, [])
        memories = [self._cache[mid].to_dict() for mid in memory_ids if mid in self._cache]
        
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump({"project_id": project_id, "memories": memories}, f, ensure_ascii=False, indent=2)
    
    def _generate_id(self, content: str, user_id: str) -> str:
        """生成记忆ID"""
        hash_input = f"{user_id}:{content}:{time.time()}"
        return hashlib.md5(hash_input.encode()).hexdigest()[:12]
    
    # ==================== 写入操作 ====================
    
    def add_memory(self, content: str, memory_type: MemoryType,
                   user_id: str, source: MemorySource = MemorySource.USER_COMMAND,
                   project_id: Optional[str] = None,
                   tags: Optional[List[str]] = None,
                   priority: MemoryPriority = MemoryPriority.HIGH,
                   metadata: Optional[Dict] = None) -> MemoryItem:
        """添加记忆"""
        with self._lock:
            memory_id = self._generate_id(content, user_id)
            
            memory = MemoryItem(
                id=memory_id,
                type=memory_type,
                content=content,
                source=source,
                priority=priority,
                user_id=user_id,
                project_id=project_id,
                tags=tags or [],
                metadata=metadata or {}
            )
            
            # 存入缓存
            self._cache[memory_id] = memory
            
            # 更新索引
            if user_id not in self._user_index:
                self._user_index[user_id] = []
            self._user_index[user_id].append(memory_id)
            
            if project_id:
                if project_id not in self._project_index:
                    self._project_index[project_id] = []
                self._project_index[project_id].append(memory_id)
            
            for tag in (tags or []):
                if tag not in self._tag_index:
                    self._tag_index[tag] = []
                self._tag_index[tag].append(memory_id)
            
            # 持久化
            if project_id:
                self._save_project_memories(project_id)
            else:
                self._save_user_memories(user_id)
            
            return memory
    
    def update_memory(self, memory_id: str, content: Optional[str] = None,
                      tags: Optional[List[str]] = None,
                      metadata: Optional[Dict] = None) -> Optional[MemoryItem]:
        """更新记忆"""
        with self._lock:
            if memory_id not in self._cache:
                return None
            
            memory = self._cache[memory_id]
            
            if content is not None:
                memory.content = content
            
            if tags is not None:
                # 更新标签索引
                old_tags = set(memory.tags)
                new_tags = set(tags)
                
                for tag in old_tags - new_tags:
                    if tag in self._tag_index and memory_id in self._tag_index[tag]:
                        self._tag_index[tag].remove(memory_id)
                
                for tag in new_tags - old_tags:
                    if tag not in self._tag_index:
                        self._tag_index[tag] = []
                    self._tag_index[tag].append(memory_id)
                
                memory.tags = tags
            
            if metadata is not None:
                memory.metadata.update(metadata)
            
            # 持久化
            if memory.project_id:
                self._save_project_memories(memory.project_id)
            else:
                self._save_user_memories(memory.user_id)
            
            return memory
    
    def delete_memory(self, memory_id: str) -> bool:
        """删除记忆"""
        with self._lock:
            if memory_id not in self._cache:
                return False
            
            memory = self._cache[memory_id]
            
            # 从索引中移除
            if memory.user_id and memory_id in self._user_index.get(memory.user_id, []):
                self._user_index[memory.user_id].remove(memory_id)
            
            if memory.project_id and memory_id in self._project_index.get(memory.project_id, []):
                self._project_index[memory.project_id].remove(memory_id)
            
            for tag in memory.tags:
                if tag in self._tag_index and memory_id in self._tag_index[tag]:
                    self._tag_index[tag].remove(memory_id)
            
            # 从缓存中移除
            del self._cache[memory_id]
            
            # 持久化
            if memory.project_id:
                self._save_project_memories(memory.project_id)
            else:
                self._save_user_memories(memory.user_id)
            
            return True
    
    def clear_user_memories(self, user_id: str) -> int:
        """清除用户所有记忆"""
        with self._lock:
            memory_ids = list(self._user_index.get(user_id, []))
            count = 0
            
            for mid in memory_ids:
                if self.delete_memory(mid):
                    count += 1
            
            # 删除存储文件
            file_path = self._get_user_file(user_id)
            if file_path.exists():
                file_path.unlink()
            
            return count
    
    # ==================== 检索操作 ====================
    
    def get_memory(self, memory_id: str) -> Optional[MemoryItem]:
        """获取单个记忆"""
        memory = self._cache.get(memory_id)
        if memory:
            memory.touch()
        return memory
    
    def get_user_memories(self, user_id: str, 
                          memory_type: Optional[MemoryType] = None) -> List[MemoryItem]:
        """获取用户所有记忆"""
        memory_ids = self._user_index.get(user_id, [])
        memories = []
        
        for mid in memory_ids:
            if mid in self._cache:
                memory = self._cache[mid]
                if memory_type is None or memory.type == memory_type:
                    memories.append(memory)
        
        return memories
    
    def get_project_memories(self, project_id: str) -> List[MemoryItem]:
        """获取项目所有记忆"""
        memory_ids = self._project_index.get(project_id, [])
        return [self._cache[mid] for mid in memory_ids if mid in self._cache]
    
    def search_by_keyword(self, keyword: str, user_id: str,
                          limit: int = 10) -> List[MemorySearchResult]:
        """关键词搜索"""
        results = []
        keyword_lower = keyword.lower()
        
        memory_ids = self._user_index.get(user_id, [])
        
        for mid in memory_ids:
            if mid not in self._cache:
                continue
            
            memory = self._cache[mid]
            
            # 检查内容匹配
            if keyword_lower in memory.content.lower():
                score = 0.8
                results.append(MemorySearchResult(
                    memory=memory,
                    relevance_score=score,
                    match_type="keyword"
                ))
            
            # 检查标签匹配
            elif any(keyword_lower in tag.lower() for tag in memory.tags):
                score = 0.6
                results.append(MemorySearchResult(
                    memory=memory,
                    relevance_score=score,
                    match_type="tag"
                ))
        
        # 按相关性排序
        results.sort(key=lambda x: x.relevance_score, reverse=True)
        return results[:limit]
    
    def search_by_tags(self, tags: List[str], user_id: str,
                       limit: int = 10) -> List[MemorySearchResult]:
        """标签搜索"""
        results = []
        memory_ids = self._user_index.get(user_id, [])
        tags_set = set(t.lower() for t in tags)
        
        for mid in memory_ids:
            if mid not in self._cache:
                continue
            
            memory = self._cache[mid]
            memory_tags = set(t.lower() for t in memory.tags)
            
            # 计算标签重叠
            overlap = len(tags_set & memory_tags)
            if overlap > 0:
                score = overlap / max(len(tags_set), len(memory_tags))
                results.append(MemorySearchResult(
                    memory=memory,
                    relevance_score=score,
                    match_type="tag"
                ))
        
        results.sort(key=lambda x: x.relevance_score, reverse=True)
        return results[:limit]
    
    def get_recent_memories(self, user_id: str, days: int = 7,
                            limit: int = 20) -> List[MemoryItem]:
        """获取最近记忆"""
        cutoff = time.time() - (days * 86400)
        memory_ids = self._user_index.get(user_id, [])
        
        memories = []
        for mid in memory_ids:
            if mid in self._cache:
                memory = self._cache[mid]
                if memory.timestamp >= cutoff and not memory.is_expired:
                    memories.append(memory)
        
        # 按时间排序
        memories.sort(key=lambda x: x.timestamp, reverse=True)
        return memories[:limit]
    
    def get_most_used_memories(self, user_id: str, 
                                limit: int = 10) -> List[MemoryItem]:
        """获取最常用记忆"""
        memory_ids = self._user_index.get(user_id, [])
        
        memories = [self._cache[mid] for mid in memory_ids if mid in self._cache]
        memories.sort(key=lambda x: x.access_count, reverse=True)
        
        return memories[:limit]
    
    # ==================== 维护操作 ====================
    
    def apply_time_decay(self, user_id: str) -> List[str]:
        """应用时间衰减，标记过期记忆"""
        cutoff = time.time() - (self.TIME_DECAY_THRESHOLD * 86400)
        memory_ids = self._user_index.get(user_id, [])
        expired = []
        
        for mid in memory_ids:
            if mid not in self._cache:
                continue
            
            memory = self._cache[mid]
            
            # 如果超过30天未访问且优先级低
            if (memory.last_accessed < cutoff and 
                memory.priority == MemoryPriority.LOW and
                memory.access_count < 3):
                memory.is_expired = True
                expired.append(mid)
        
        return expired
    
    def cleanup_expired(self, user_id: str) -> int:
        """清理过期记忆"""
        memory_ids = list(self._user_index.get(user_id, []))
        count = 0
        
        for mid in memory_ids:
            if mid in self._cache:
                memory = self._cache[mid]
                if memory.is_expired:
                    self.delete_memory(mid)
                    count += 1
        
        return count
    
    def get_stats(self, user_id: str) -> Dict:
        """获取记忆统计"""
        memory_ids = self._user_index.get(user_id, [])
        memories = [self._cache[mid] for mid in memory_ids if mid in self._cache]
        
        type_counts = {}
        for m in memories:
            t = m.type.value
            type_counts[t] = type_counts.get(t, 0) + 1
        
        return {
            "total": len(memories),
            "by_type": type_counts,
            "expired": sum(1 for m in memories if m.is_expired),
            "verified": sum(1 for m in memories if m.is_verified)
        }


# 全局记忆存储实例
_global_store: Optional[MemoryStore] = None


def get_memory_store(storage_path: str = "./memory_store") -> MemoryStore:
    """获取全局记忆存储实例"""
    global _global_store
    if _global_store is None:
        _global_store = MemoryStore(storage_path)
    return _global_store
