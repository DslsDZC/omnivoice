"""记忆管理器 - 检索、注入、更新操作"""
import time
import re
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime

from memory_store import (
    MemoryStore, MemoryItem, MemoryType, MemorySource, MemoryPriority,
    MemorySearchResult, get_memory_store
)


@dataclass
class InjectedMemory:
    """注入到会话的记忆"""
    memory_id: str
    type: str
    content: str
    source: str
    timestamp: str
    relevance: float
    is_readonly: bool = True
    
    def to_dict(self) -> Dict:
        return {
            "memory_id": self.memory_id,
            "type": self.type,
            "content": self.content,
            "source": self.source,
            "timestamp": self.timestamp,
            "relevance": self.relevance,
            "is_readonly": self.is_readonly
        }
    
    def format_for_prompt(self) -> str:
        """格式化为提示词"""
        type_names = {
            "user_preference": "用户偏好",
            "history_conclusion": "历史结论",
            "fact_knowledge": "事实知识",
            "task_state": "任务状态",
            "project_info": "项目信息"
        }
        type_name = type_names.get(self.type, self.type)
        return f"[{type_name}] {self.content} (来源: {self.source})"


@dataclass
class MemoryCommandResult:
    """记忆命令执行结果"""
    success: bool
    message: str
    memory_id: Optional[str] = None
    memories: List[MemoryItem] = field(default_factory=list)


class MemoryManager:
    """记忆管理器 - 处理记忆的检索、注入和更新"""
    
    # 相关性阈值
    RELEVANCE_THRESHOLD = 0.5
    
    # 最大注入记忆数
    MAX_INJECTED_MEMORIES = 10
    
    # 命令模式
    COMMAND_PATTERNS = {
        "remember": r'^/remember\s+(.+)$',
        "remember_fact": r'^/remember_fact\s+(.+)$',
        "forget": r'^/forget\s+(.+)$',
        "recall": r'^/recall\s*(.*)$',
        "memories": r'^/memories\s*(.*)$',
        "clear_memories": r'^/clear_memories$',
    }
    
    def __init__(self, store: Optional[MemoryStore] = None):
        self.store = store or get_memory_store()
        self._pending_saves: List[Dict] = []  # 待确认的自动记忆
    
    # ==================== 命令解析 ====================
    
    def parse_command(self, text: str) -> Optional[Tuple[str, Dict]]:
        """解析记忆命令
        
        Returns:
            (command_type, params) 或 None
        """
        text = text.strip()
        
        for cmd_type, pattern in self.COMMAND_PATTERNS.items():
            match = re.match(pattern, text, re.IGNORECASE)
            if match:
                if cmd_type == "remember":
                    return cmd_type, {"content": match.group(1).strip()}
                elif cmd_type == "remember_fact":
                    return cmd_type, {"content": match.group(1).strip()}
                elif cmd_type == "forget":
                    return cmd_type, {"keyword": match.group(1).strip()}
                elif cmd_type == "recall":
                    return cmd_type, {"keyword": match.group(1).strip() if match.group(1) else ""}
                elif cmd_type == "memories":
                    return cmd_type, {"filter": match.group(1).strip() if match.group(1) else ""}
                elif cmd_type == "clear_memories":
                    return cmd_type, {}
        
        return None
    
    # ==================== 命令执行 ====================
    
    def execute_command(self, text: str, user_id: str,
                        project_id: Optional[str] = None) -> Optional[MemoryCommandResult]:
        """执行记忆命令"""
        parsed = self.parse_command(text)
        if not parsed:
            return None
        
        cmd_type, params = parsed
        
        if cmd_type == "remember":
            return self._cmd_remember(params["content"], user_id, project_id)
        elif cmd_type == "remember_fact":
            return self._cmd_remember_fact(params["content"], user_id, project_id)
        elif cmd_type == "forget":
            return self._cmd_forget(params["keyword"], user_id)
        elif cmd_type == "recall":
            return self._cmd_recall(params["keyword"], user_id)
        elif cmd_type == "memories":
            return self._cmd_memories(params["filter"], user_id)
        elif cmd_type == "clear_memories":
            return self._cmd_clear_memories(user_id)
        
        return None
    
    def _cmd_remember(self, content: str, user_id: str,
                      project_id: Optional[str]) -> MemoryCommandResult:
        """执行 /remember 命令"""
        memory = self.store.add_memory(
            content=content,
            memory_type=MemoryType.USER_PREFERENCE,
            user_id=user_id,
            source=MemorySource.USER_COMMAND,
            project_id=project_id,
            priority=MemoryPriority.HIGH
        )
        
        return MemoryCommandResult(
            success=True,
            message=f"[OK] 已记住：{content}",
            memory_id=memory.id
        )
    
    def _cmd_remember_fact(self, content: str, user_id: str,
                           project_id: Optional[str]) -> MemoryCommandResult:
        """执行 /remember_fact 命令"""
        memory = self.store.add_memory(
            content=content,
            memory_type=MemoryType.FACT_KNOWLEDGE,
            user_id=user_id,
            source=MemorySource.USER_COMMAND,
            project_id=project_id,
            priority=MemoryPriority.HIGH,
            metadata={"is_fact": True}
        )
        
        return MemoryCommandResult(
            success=True,
            message=f"[OK] 已记录事实：{content}",
            memory_id=memory.id
        )
    
    def _cmd_forget(self, keyword: str, user_id: str) -> MemoryCommandResult:
        """执行 /forget 命令"""
        # 搜索匹配的记忆
        results = self.store.search_by_keyword(keyword, user_id, limit=10)
        
        if not results:
            return MemoryCommandResult(
                success=False,
                message=f"未找到包含 '{keyword}' 的记忆"
            )
        
        # 如果只有一个匹配，直接删除
        if len(results) == 1:
            memory = results[0].memory
            self.store.delete_memory(memory.id)
            return MemoryCommandResult(
                success=True,
                message=f"[OK] 已删除：{memory.content[:50]}...",
                memory_id=memory.id
            )
        
        # 多个匹配，返回列表让用户选择
        return MemoryCommandResult(
            success=True,
            message=f"找到 {len(results)} 条匹配记忆，请指定更精确的关键词：",
            memories=[r.memory for r in results]
        )
    
    def _cmd_recall(self, keyword: str, user_id: str) -> MemoryCommandResult:
        """执行 /recall 命令"""
        if not keyword:
            # 无关键词，返回最近记忆
            memories = self.store.get_recent_memories(user_id, days=30, limit=10)
            if not memories:
                return MemoryCommandResult(
                    success=True,
                    message="暂无长期记忆"
                )
            return MemoryCommandResult(
                success=True,
                message=f"最近 {len(memories)} 条记忆：",
                memories=memories
            )
        
        # 有关键词，搜索
        results = self.store.search_by_keyword(keyword, user_id, limit=10)
        
        if not results:
            return MemoryCommandResult(
                success=False,
                message=f"未找到与 '{keyword}' 相关的记忆"
            )
        
        return MemoryCommandResult(
            success=True,
            message=f"找到 {len(results)} 条相关记忆：",
            memories=[r.memory for r in results]
        )
    
    def _cmd_memories(self, filter_type: str, user_id: str) -> MemoryCommandResult:
        """执行 /memories 命令"""
        if filter_type:
            # 按类型过滤
            type_map = {
                "preference": MemoryType.USER_PREFERENCE,
                "conclusion": MemoryType.HISTORY_CONCLUSION,
                "fact": MemoryType.FACT_KNOWLEDGE,
                "task": MemoryType.TASK_STATE,
                "project": MemoryType.PROJECT_INFO,
            }
            memory_type = type_map.get(filter_type.lower())
            if memory_type:
                memories = self.store.get_user_memories(user_id, memory_type)
            else:
                memories = self.store.get_user_memories(user_id)
        else:
            memories = self.store.get_user_memories(user_id)
        
        if not memories:
            return MemoryCommandResult(
                success=True,
                message="暂无长期记忆"
            )
        
        return MemoryCommandResult(
            success=True,
            message=f"共 {len(memories)} 条长期记忆：",
            memories=memories
        )
    
    def _cmd_clear_memories(self, user_id: str) -> MemoryCommandResult:
        """执行 /clear_memories 命令"""
        count = self.store.clear_user_memories(user_id)
        return MemoryCommandResult(
            success=True,
            message=f"[OK] 已清除 {count} 条记忆"
        )
    
    # ==================== 记忆注入 ====================
    
    def retrieve_relevant_memories(self, question: str, user_id: str,
                                    project_id: Optional[str] = None,
                                    limit: int = None) -> List[InjectedMemory]:
        """检索与当前问题相关的记忆
        
        Args:
            question: 当前问题
            user_id: 用户ID
            project_id: 项目ID（可选）
            limit: 最大数量
        
        Returns:
            注入记忆列表
        """
        limit = limit or self.MAX_INJECTED_MEMORIES
        injected = []
        
        # 1. 关键词搜索
        # 提取关键词（简化：分词）
        keywords = self._extract_keywords(question)
        
        for kw in keywords[:3]:  # 最多用3个关键词搜索
            results = self.store.search_by_keyword(kw, user_id, limit=5)
            for result in results:
                if result.relevance_score >= self.RELEVANCE_THRESHOLD:
                    injected.append(self._to_injected(result))
        
        # 2. 获取最近记忆
        recent = self.store.get_recent_memories(user_id, days=7, limit=5)
        for memory in recent:
            if not any(im.memory_id == memory.id for im in injected):
                injected.append(self._to_injected_from_memory(memory, relevance=0.4))
        
        # 3. 获取最常用记忆
        most_used = self.store.get_most_used_memories(user_id, limit=3)
        for memory in most_used:
            if not any(im.memory_id == memory.id for im in injected):
                injected.append(self._to_injected_from_memory(memory, relevance=0.3))
        
        # 4. 项目记忆（如果有）
        if project_id:
            project_memories = self.store.get_project_memories(project_id)
            for memory in project_memories[:3]:
                if not any(im.memory_id == memory.id for im in injected):
                    injected.append(self._to_injected_from_memory(memory, relevance=0.5))
        
        # 去重并排序
        seen = set()
        unique = []
        for im in injected:
            if im.memory_id not in seen:
                seen.add(im.memory_id)
                unique.append(im)
        
        # 按相关性排序
        unique.sort(key=lambda x: x.relevance, reverse=True)
        
        return unique[:limit]
    
    def _extract_keywords(self, text: str) -> List[str]:
        """提取关键词（简化实现）"""
        # 移除常见停用词
        stopwords = {"的", "是", "在", "有", "和", "了", "我", "你", "他", "她", "它",
                    "这", "那", "就", "也", "都", "会", "能", "要", "不", "没", "很",
                    "the", "a", "an", "is", "are", "was", "were", "be", "been",
                    "have", "has", "had", "do", "does", "did", "will", "would",
                    "could", "should", "may", "might", "must", "can", "to", "of",
                    "in", "for", "on", "with", "at", "by", "from", "as", "into"}
        
        # 简单分词（中英文混合）
        words = re.findall(r'[\u4e00-\u9fa5]+|[a-zA-Z]+', text.lower())
        
        # 过滤停用词和短词
        keywords = [w for w in words if w not in stopwords and len(w) >= 2]
        
        return keywords
    
    def _to_injected(self, result: MemorySearchResult) -> InjectedMemory:
        """转换搜索结果为注入记忆"""
        memory = result.memory
        return InjectedMemory(
            memory_id=memory.id,
            type=memory.type.value,
            content=memory.content,
            source=memory.source.value,
            timestamp=datetime.fromtimestamp(memory.timestamp).strftime("%Y-%m-%d"),
            relevance=result.relevance_score
        )
    
    def _to_injected_from_memory(self, memory: MemoryItem, 
                                  relevance: float) -> InjectedMemory:
        """转换记忆条目为注入记忆"""
        return InjectedMemory(
            memory_id=memory.id,
            type=memory.type.value,
            content=memory.content,
            source=memory.source.value,
            timestamp=datetime.fromtimestamp(memory.timestamp).strftime("%Y-%m-%d"),
            relevance=relevance
        )
    
    def format_memories_for_prompt(self, memories: List[InjectedMemory]) -> str:
        """格式化记忆为提示词"""
        if not memories:
            return ""
        
        lines = ["=== 长期记忆（只读）==="]
        lines.append("以下是你的长期记忆，请参考但不盲目遵循。")
        lines.append("如果记忆与当前事实冲突，优先相信当前讨论。\n")
        
        for i, mem in enumerate(memories, 1):
            lines.append(f"{i}. {mem.format_for_prompt()}")
        
        lines.append("\n注意：这些记忆是只读的，不可修改。")
        lines.append("如需更新，请使用 /remember 或 /forget 命令。")
        
        return "\n".join(lines)
    
    # ==================== 自动记忆 ====================
    
    def extract_session_memories(self, final_resolution: str,
                                  messages: List[Dict],
                                  tool_results: List[Dict],
                                  user_id: str) -> List[Dict]:
        """从会话中提取可保存的记忆
        
        Returns:
            待确认的记忆列表
        """
        candidates = []
        
        # 1. 提取最终决议
        if final_resolution and len(final_resolution) > 20:
            candidates.append({
                "type": MemoryType.HISTORY_CONCLUSION,
                "content": f"最终结论：{final_resolution[:200]}",
                "source": MemorySource.SYSTEM_AUTO,
                "priority": MemoryPriority.MEDIUM,
                "reason": "会话最终决议"
            })
        
        # 2. 提取重要发言（包含 [IMPORTANT] 标记）
        for msg in messages:
            content = msg.get("content", "")
            if "[IMPORTANT]" in content or "[重要]" in content:
                candidates.append({
                    "type": MemoryType.HISTORY_CONCLUSION,
                    "content": content.replace("[IMPORTANT]", "").replace("[重要]", "").strip()[:200],
                    "source": MemorySource.SYSTEM_AUTO,
                    "priority": MemoryPriority.MEDIUM,
                    "reason": "标记为重要的发言"
                })
        
        # 3. 提取多次引用的工具结果
        tool_usage_count = {}
        for tr in tool_results:
            tool_name = tr.get("tool", "")
            if tool_name:
                tool_usage_count[tool_name] = tool_usage_count.get(tool_name, 0) + 1
        
        for tool_name, count in tool_usage_count.items():
            if count >= 2:
                # 找到该工具的结果
                for tr in tool_results:
                    if tr.get("tool") == tool_name:
                        result = tr.get("result", "")
                        if result and len(str(result)) > 10:
                            candidates.append({
                                "type": MemoryType.FACT_KNOWLEDGE,
                                "content": f"{tool_name} 结果：{str(result)[:150]}",
                                "source": MemorySource.SYSTEM_AUTO,
                                "priority": MemoryPriority.LOW,
                                "reason": f"工具 {tool_name} 被调用 {count} 次"
                            })
                            break
        
        # 4. 检测代理请求记忆
        for msg in messages:
            content = msg.get("content", "")
            if "[SAVE_MEMORY]" in content:
                # 提取请求内容
                match = re.search(r'\[SAVE_MEMORY\]\s*(.+)', content, re.DOTALL)
                if match:
                    candidates.append({
                        "type": MemoryType.USER_PREFERENCE,
                        "content": match.group(1).strip()[:200],
                        "source": MemorySource.AGENT_REQUEST,
                        "priority": MemoryPriority.LOW,
                        "reason": f"代理 {msg.get('agent_id', '?')} 请求保存"
                    })
        
        return candidates
    
    def add_pending_save(self, candidate: Dict, user_id: str):
        """添加待确认的自动记忆"""
        self._pending_saves.append({
            **candidate,
            "user_id": user_id,
            "timestamp": time.time()
        })
    
    def confirm_pending_save(self, index: int, user_id: str) -> Optional[MemoryItem]:
        """确认保存待定记忆"""
        if 0 <= index < len(self._pending_saves):
            pending = self._pending_saves.pop(index)
            if pending["user_id"] == user_id:
                return self.store.add_memory(
                    content=pending["content"],
                    memory_type=pending["type"],
                    user_id=user_id,
                    source=pending["source"],
                    priority=pending["priority"],
                    metadata={"reason": pending.get("reason", "")}
                )
        return None
    
    def reject_pending_save(self, index: int, user_id: str) -> bool:
        """拒绝保存待定记忆"""
        if 0 <= index < len(self._pending_saves):
            pending = self._pending_saves.pop(index)
            return pending.get("user_id") == user_id
        return False
    
    def get_pending_saves(self, user_id: str) -> List[Dict]:
        """获取待确认的记忆"""
        return [p for p in self._pending_saves if p.get("user_id") == user_id]
    
    def clear_pending_saves(self, user_id: str):
        """清除待确认的记忆"""
        self._pending_saves = [p for p in self._pending_saves if p.get("user_id") != user_id]
    
    # ==================== 记忆验证 ====================
    
    def check_conflict(self, new_content: str, user_id: str) -> List[MemoryItem]:
        """检查新记忆是否与已有记忆冲突"""
        keywords = self._extract_keywords(new_content)
        conflicts = []
        
        for kw in keywords[:2]:
            results = self.store.search_by_keyword(kw, user_id, limit=5)
            for result in results:
                if result.relevance_score > 0.8:
                    # 高相关性，可能是冲突
                    conflicts.append(result.memory)
        
        return conflicts
    
    def get_memory_stats(self, user_id: str) -> Dict:
        """获取记忆统计"""
        return self.store.get_stats(user_id)


# 全局记忆管理器实例
_global_manager: Optional[MemoryManager] = None


def get_memory_manager(store: Optional[MemoryStore] = None) -> MemoryManager:
    """获取全局记忆管理器实例"""
    global _global_manager
    if _global_manager is None:
        _global_manager = MemoryManager(store)
    return _global_manager
