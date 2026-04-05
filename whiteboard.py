"""共享白板模块"""
import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
from copy import deepcopy

from fact_checker import FactBoard, FactItem, FactStatus, UserOpinion


@dataclass
class Message:
    """消息结构"""
    agent_id: str
    content: str
    timestamp: float
    tool_calls: List[Dict] = field(default_factory=list)
    message_type: str = "normal"  # normal, interrupt, request_meeting, vote


@dataclass
class ToolResult:
    """工具调用结果"""
    caller: str
    tool: str
    args: Dict
    result: Any
    timestamp: float
    success: bool = True


@dataclass
class WorkspaceFileInfo:
    """工作区文件信息"""
    size: int
    mtime: float
    hash: Optional[str] = None


@dataclass
class ConsensusItem:
    """共识条目"""
    content: str
    timestamp: float
    supporters: List[str]
    weight: float


@dataclass
class TaskStep:
    """任务步骤"""
    step_id: int
    description: str
    expected_output: str
    suggested_tools: List[str]
    assigned_agent: Optional[str] = None
    status: str = "pending"  # pending, in_progress, completed, failed
    result: Optional[str] = None


class Whiteboard:
    """共享白板 - 全局状态存储"""
    
    def __init__(self, session_id: Optional[str] = None):
        self.session_id = session_id or str(uuid.uuid4())[:8]
        
        # 核心数据存储
        self._messages: List[Message] = []
        self._tool_results: List[ToolResult] = []
        self._workspace_files: Dict[str, WorkspaceFileInfo] = {}
        self._consensus: List[ConsensusItem] = []
        self._pending_issues: List[Dict] = []
        self._task_queue: List[TaskStep] = []
        self._final_resolution: Optional[str] = None
        
        # 议程管理
        self._agenda: List[Dict] = []
        self._current_agenda_index: int = 0
        
        # 元数据存储（用于会议行为管理器）
        self._metadata: Dict[str, Any] = {}
        
        # 事实白板（独立于讨论白板）
        self._fact_board: FactBoard = FactBoard()
        
        # 用户观点记录（与事实分离）
        self._user_viewpoints: List[Dict] = []
        
        # 长期记忆区域（只读，会话开始时注入）
        self._long_term_memories: List[Dict] = []
        self._user_id: Optional[str] = None
        self._project_id: Optional[str] = None
        
        # 状态标志
        self._pause_flag: bool = False
        self._current_mode: Optional[str] = None
        self._meeting_phase: str = ""  # discussion, voting, etc.
        
        # 代理贡献系数
        self._agent_contributions: Dict[str, float] = {}
        
        # 代理性格公开信息（供其他代理参考）
        self._agent_personalities: Dict[str, Dict] = {}
        
        # 观点追踪（取代方案追踪）
        # viewpoints: [{id, content, agent_id, timestamp, type, topic_id, references, 
        #               support_count, oppose_count, arguments, status}]
        self._viewpoints: List[Dict] = []
        
        # 观点类型枚举
        self._viewpoint_types = [
            "support",    # 支持
            "oppose",     # 反对
            "modify",     # 修改建议
            "question",   # 质疑
            "neutral",    # 中立
            "inquiry"     # 提问
        ]
        
        # 观点图谱（引用关系）
        self._viewpoint_graph: Dict[str, List[str]] = {}  # viewpoint_id -> [referenced_ids]
        
        # 问题扩展（主问题→子问题）
        self._main_topic: str = ""
        self._sub_topics: List[Dict] = []  # [{id, content, status, parent_id, created_by}]
        
        # 议题队列
        self._active_issues: List[Dict] = []      # 活跃议题
        self._pending_issues: List[Dict] = []     # 待处理议题
        self._issue_counter: int = 0
        
        # 暂存问题（待复盘）
        self._shelved_issues: List[Dict] = []  # [{id, content, reason, shelved_at, related_viewpoints}]
        
        # 当前投票中的观点
        self._voting_viewpoint: Optional[Dict] = None
        
        # 复盘记录
        self._review_records: List[Dict] = []
        
        # 异常记录
        self._exception_records: List[Dict] = []
        
        # 演化数据
        self._evolution_data: Dict = {}
        
        # 会话统计
        self._session_stats: Dict = {
            "total_sessions": 0,
            "total_messages": 0,
            "total_votes": 0,
            "start_time": None
        }
        
        # ==================== 思考暂停机制 ====================
        # 当前思考暂停状态
        self._think_pause: Dict = {
            "active": False,           # 是否有思考暂停正在进行
            "agent_id": None,          # 发起思考的代理ID
            "start_time": None,        # 开始时间
            "duration": 0,             # 请求的持续时间
            "queue": [],               # 排队等待的思考请求
        }
        
        # 思考暂停历史记录（用于限制频率）
        self._think_history: Dict[str, List[float]] = {}  # agent_id -> [timestamps]
        
        # 全局思考暂停计数（用于限制全局频率）
        self._global_think_timestamps: List[float] = []
        
        # 私有思考日志（仅发起代理可见，用户可查看）
        self._think_private_log: List[Dict] = []  # [{agent_id, content, type, timestamp}]
        
        # 思考恢复后的优先发言权
        self._think_priority: Dict[str, float] = {}  # agent_id -> priority_boost_expiry
        
        # 思考期间累积的叫停请求
        self._pending_interrupts: List[Dict] = []  # [{agent_id, content, timestamp}]
        
        # 投票记录
        self._vote_sessions: List[Dict] = []  # 投票会话历史
        self._current_vote_session: Optional[Dict] = None  # 当前投票会话
        self._agent_weights: Dict[str, float] = {}  # 代理权重（公开）
        self._contribution_records: List[Dict] = []  # 贡献记录
        
        # ==================== 异步轮询新增数据结构 ====================
        
        # 代理实时状态表
        # {agent_id: {last_round, contribution_score, efficiency_score, expertise, 
        #             is_thinking, last_read_index, total_speaks, last_speak_time}}
        self._agent_states: Dict[str, Dict] = {}
        
        # 会后行动项
        # [{id, description, assignee, due_time, status, created_at}]
        self._action_items: List[Dict] = []
        
        # 全局模式标志
        self._voting_mode: bool = False        # 是否处于表决阶段
        self._think_mode: bool = False         # 是否处于思考暂停
        self._serial_mode: bool = False        # 是否处于串行模式
        
        # 消息展示队列（按优先级排序）
        # [{message_id, priority_score, is_displayed, display_time}]
        self._display_queue: List[Dict] = []
        
        # 展示窗口大小（同时展示的消息数）
        self._display_window_size: int = 2
        
        # 有效轮次计数
        self._effective_rounds: int = 0
        
        # 系统全局计时器（用于防卡死）
        self._last_activity_time: float = time.time()
        
        # 自然冷却超时阈值（秒）
        self._idle_timeout: float = 30.0
        
        # 轮次上限
        self._max_rounds: int = 50
        
        # 模式切换冷却（防震荡）
        self._last_mode_switch_time: float = 0.0
        self._min_mode_stay_time: float = 30.0  # 最小停留时间
        
        # 消息去重缓存（用于语义相似度检测）
        self._message_embeddings: List[Dict] = []  # [{msg_id, embedding, content}]
        
        # 线程安全锁
        self._lock = threading.RLock()
        
        # 版本号（用于乐观锁）
        self._version = 0
    
    @property
    def version(self) -> int:
        with self._lock:
            return self._version
    
    # ==================== 消息操作 ====================
    
    def add_message(self, agent_id: str, content: str, 
                    tool_calls: Optional[List[Dict]] = None,
                    message_type: str = "normal") -> Message:
        """添加消息"""
        with self._lock:
            msg = Message(
                agent_id=agent_id,
                content=content,
                timestamp=time.time(),
                tool_calls=tool_calls or [],
                message_type=message_type
            )
            self._messages.append(msg)
            self._version += 1
            return msg
    
    def get_messages(self, since_index: int = 0) -> List[Message]:
        """获取消息列表"""
        with self._lock:
            return deepcopy(self._messages[since_index:])
    
    def get_messages_by_agent(self, agent_id: str) -> List[Message]:
        """获取特定代理的消息"""
        with self._lock:
            return [m for m in self._messages if m.agent_id == agent_id]
    
    def get_last_message_time(self) -> float:
        """获取最后一条消息的时间"""
        with self._lock:
            if self._messages:
                return self._messages[-1].timestamp
            return 0.0
    
    # ==================== 工具结果操作 ====================
    
    def add_tool_result(self, caller: str, tool: str, 
                        args: Dict, result: Any, success: bool = True) -> ToolResult:
        """添加工具调用结果"""
        with self._lock:
            tr = ToolResult(
                caller=caller,
                tool=tool,
                args=args,
                result=result,
                timestamp=time.time(),
                success=success
            )
            self._tool_results.append(tr)
            self._version += 1
            return tr
    
    def get_tool_results(self, since_index: int = 0) -> List[ToolResult]:
        """获取工具结果列表"""
        with self._lock:
            return deepcopy(self._tool_results[since_index:])
    
    def get_tool_results_by_agent(self, agent_id: str) -> List[ToolResult]:
        """获取特定代理的工具结果"""
        with self._lock:
            return [tr for tr in self._tool_results if tr.caller == agent_id]
    
    # ==================== 工作区文件操作 ====================
    
    def update_workspace_file(self, filename: str, size: int, 
                               mtime: float, file_hash: Optional[str] = None):
        """更新工作区文件信息"""
        with self._lock:
            self._workspace_files[filename] = WorkspaceFileInfo(
                size=size,
                mtime=mtime,
                hash=file_hash
            )
            self._version += 1
    
    def remove_workspace_file(self, filename: str):
        """移除工作区文件记录"""
        with self._lock:
            if filename in self._workspace_files:
                del self._workspace_files[filename]
                self._version += 1
    
    def get_workspace_files(self) -> Dict[str, WorkspaceFileInfo]:
        """获取工作区文件列表"""
        with self._lock:
            return deepcopy(self._workspace_files)
    
    # ==================== 共识操作 ====================
    
    def add_consensus(self, content: str, supporters: List[str], weight: float):
        """添加共识"""
        with self._lock:
            consensus = ConsensusItem(
                content=content,
                timestamp=time.time(),
                supporters=supporters,
                weight=weight
            )
            self._consensus.append(consensus)
            self._version += 1
    
    def get_consensus_list(self) -> List[ConsensusItem]:
        """获取共识列表"""
        with self._lock:
            return deepcopy(self._consensus)
    
    def clear_consensus(self):
        """清空共识"""
        with self._lock:
            self._consensus.clear()
            self._version += 1
    
    def clear_discussion_messages(self):
        """清空讨论消息，保留系统消息（议程设置等）"""
        with self._lock:
            # 只保留系统消息和议程相关消息
            self._messages = [
                msg for msg in self._messages 
                if msg.message_type in ("system", "agenda", "agenda_set", "main_topic")
            ]
            # 清除代理贡献记录
            self._agent_contributions.clear()
            # 清除待解决问题
            self._pending_issues.clear()
            self._version += 1
    
    # ==================== 代理性格操作 ====================
    
    def set_agent_personalities(self, personalities: Dict[str, Dict]):
        """设置代理性格公开信息"""
        with self._lock:
            self._agent_personalities = personalities
            self._version += 1
    
    def get_agent_personalities(self) -> Dict[str, Dict]:
        """获取所有代理性格信息"""
        with self._lock:
            return deepcopy(self._agent_personalities)
    
    def get_agent_personality(self, agent_id: str) -> Optional[Dict]:
        """获取特定代理的性格信息"""
        with self._lock:
            return deepcopy(self._agent_personalities.get(agent_id))
    
    def update_agent_personality(self, agent_id: str, personality: Dict):
        """更新单个代理的性格信息"""
        with self._lock:
            self._agent_personalities[agent_id] = personality
            self._version += 1
    
    # ==================== 事实白板操作 ====================
    
    def get_fact_board(self) -> FactBoard:
        """获取事实白板"""
        return self._fact_board
    
    def add_verified_fact(self, content: str, source: str,
                          category: str = "data") -> FactItem:
        """添加已验证事实"""
        from fact_checker import FactCategory
        cat_map = {
            "data": FactCategory.DATA,
            "definition": FactCategory.DEFINITION,
            "event": FactCategory.EVENT,
            "calculation": FactCategory.CALCULATION,
            "external": FactCategory.EXTERNAL,
        }
        category_enum = cat_map.get(category, FactCategory.DATA)
        
        with self._lock:
            fact = self._fact_board.add_verified_fact(
                content=content,
                source=source,
                category=category_enum
            )
            self._version += 1
            return fact
    
    def add_user_viewpoint(self, content: str, 
                           mark_as_opinion: bool = True) -> UserOpinion:
        """添加用户观点（与事实分离存储）"""
        with self._lock:
            opinion = self._fact_board.add_user_opinion(
                content=content,
                mark_as_opinion=mark_as_opinion
            )
            self._user_viewpoints.append({
                "content": content,
                "timestamp": time.time(),
                "conflicts": opinion.conflicts_with_facts
            })
            self._version += 1
            return opinion
    
    def get_verified_facts(self) -> List[FactItem]:
        """获取已验证事实列表"""
        return self._fact_board.get_verified_facts()
    
    def get_user_viewpoints(self) -> List[Dict]:
        """获取用户观点列表"""
        with self._lock:
            return deepcopy(self._user_viewpoints)
    
    def check_fact_conflicts(self, statement: str) -> List[str]:
        """检查陈述是否与事实冲突"""
        return self._fact_board._check_fact_conflicts(statement)
    
    def get_fact_context(self) -> str:
        """获取事实上下文（供代理参考）"""
        return self._fact_board.get_context_for_agent()
    
    # ==================== 长期记忆操作 ====================
    
    def set_user_context(self, user_id: str, project_id: Optional[str] = None):
        """设置用户上下文（用于记忆检索）"""
        with self._lock:
            self._user_id = user_id
            self._project_id = project_id
    
    def inject_long_term_memories(self, memories: List[Dict]):
        """注入长期记忆（会话开始时调用）"""
        with self._lock:
            self._long_term_memories = memories
            self._version += 1
    
    def get_long_term_memories(self) -> List[Dict]:
        """获取长期记忆列表"""
        with self._lock:
            return deepcopy(self._long_term_memories)
    
    def get_long_term_memory_prompt(self) -> str:
        """获取长期记忆提示词（供代理参考）"""
        if not self._long_term_memories:
            return ""
        
        lines = ["=== 长期记忆（只读）==="]
        lines.append("以下是你的长期记忆，请参考但不盲目遵循。")
        lines.append("如果记忆与当前事实冲突，优先相信当前讨论。\n")
        
        for i, mem in enumerate(self._long_term_memories, 1):
            type_names = {
                "user_preference": "用户偏好",
                "history_conclusion": "历史结论",
                "fact_knowledge": "事实知识",
                "task_state": "任务状态",
                "project_info": "项目信息"
            }
            type_name = type_names.get(mem.get("type", ""), mem.get("type", ""))
            content = mem.get("content", "")
            source = mem.get("source", "")
            lines.append(f"{i}. [{type_name}] {content} (来源: {source})")
        
        lines.append("\n注意：这些记忆是只读的，不可修改。")
        lines.append("如需更新，请使用 /remember 或 /forget 命令。")
        
        return "\n".join(lines)
    
    def get_memory_ids(self) -> List[str]:
        """获取已注入记忆的ID列表"""
        with self._lock:
            return [m.get("memory_id") for m in self._long_term_memories if m.get("memory_id")]
    
    # ==================== 待解决问题操作 ====================
    
    def add_pending_issue(self, issue: Dict):
        """添加待解决问题"""
        with self._lock:
            self._pending_issues.append(issue)
            self._version += 1
    
    def get_pending_issues(self) -> List[Dict]:
        """获取待解决问题列表"""
        with self._lock:
            return deepcopy(self._pending_issues)
    
    def resolve_pending_issue(self, index: int, resolution: str):
        """解决待解决问题"""
        with self._lock:
            if 0 <= index < len(self._pending_issues):
                self._pending_issues[index]['resolution'] = resolution
                self._pending_issues[index]['resolved'] = True
                self._version += 1
    
    # ==================== 任务队列操作 ====================
    
    def set_task_queue(self, steps: List[TaskStep]):
        """设置任务队列"""
        with self._lock:
            self._task_queue = steps
            self._version += 1
    
    def get_task_queue(self) -> List[TaskStep]:
        """获取任务队列"""
        with self._lock:
            return deepcopy(self._task_queue)
    
    def update_task_status(self, step_id: int, status: str, result: Optional[str] = None):
        """更新任务状态"""
        with self._lock:
            for step in self._task_queue:
                if step.step_id == step_id:
                    step.status = status
                    if result:
                        step.result = result
                    self._version += 1
                    break
    
    def get_next_pending_task(self) -> Optional[TaskStep]:
        """获取下一个待执行任务"""
        with self._lock:
            for step in self._task_queue:
                if step.status == "pending":
                    return deepcopy(step)
            return None
    
    def get_current_task(self) -> Optional[TaskStep]:
        """获取当前正在执行的任务"""
        with self._lock:
            for step in self._task_queue:
                if step.status == "in_progress":
                    return deepcopy(step)
            return None
    
    # ==================== 最终决议 ====================
    
    def set_final_resolution(self, resolution: str):
        """设置最终决议"""
        with self._lock:
            self._final_resolution = resolution
            self._version += 1
    
    def get_final_resolution(self) -> Optional[str]:
        """获取最终决议"""
        with self._lock:
            return self._final_resolution
    
    # ==================== 观点追踪 ====================
    
    def add_viewpoint(self, content: str, agent_id: str, 
                       viewpoint_type: str = "neutral",
                       topic_id: str = None,
                       references: List[str] = None,
                       arguments: List[str] = None) -> Dict:
        """添加观点（支持类型标注和关联）"""
        with self._lock:
            viewpoint = {
                "id": f"vp_{int(time.time() * 1000)}",
                "content": content,
                "agent_id": agent_id,
                "timestamp": time.time(),
                "type": viewpoint_type,  # support/oppose/modify/question/neutral/inquiry
                "topic_id": topic_id or self._main_topic,
                "references": references or [],  # 引用的其他观点ID
                "arguments": arguments or [],     # 论据列表
                "support_count": 0,
                "oppose_count": 0,
                "supporters": [],
                "opposers": [],
                "status": "active"  # active, voting, passed, rejected
            }
            self._viewpoints.append(viewpoint)
            
            # 更新观点图谱
            if references:
                self._viewpoint_graph[viewpoint["id"]] = references
            
            self._version += 1
            return viewpoint
    
    def get_viewpoints(self, status: str = None, 
                        viewpoint_type: str = None,
                        topic_id: str = None) -> List[Dict]:
        """获取观点列表（支持多条件过滤）"""
        with self._lock:
            result = [v.copy() for v in self._viewpoints]
            
            if status:
                result = [v for v in result if v.get("status") == status]
            if viewpoint_type:
                result = [v for v in result if v.get("type") == viewpoint_type]
            if topic_id:
                result = [v for v in result if v.get("topic_id") == topic_id]
            
            return result
    
    def get_viewpoint_types_summary(self, topic_id: str = None) -> Dict:
        """获取观点类型分布摘要"""
        with self._lock:
            viewpoints = self._viewpoints
            if topic_id:
                viewpoints = [v for v in viewpoints if v.get("topic_id") == topic_id]
            
            summary = {t: 0 for t in self._viewpoint_types}
            for vp in viewpoints:
                vp_type = vp.get("type", "neutral")
                if vp_type in summary:
                    summary[vp_type] += 1
            
            return summary
    
    def get_viewpoint_graph(self) -> Dict:
        """获取观点引用图谱"""
        with self._lock:
            return self._viewpoint_graph.copy()
    
    def get_viewpoint_chain(self, viewpoint_id: str) -> List[Dict]:
        """获取观点引用链（追溯所有引用）"""
        with self._lock:
            chain = []
            visited = set()
            
            def traverse(vid):
                if vid in visited:
                    return
                visited.add(vid)
                
                vp = next((v for v in self._viewpoints if v["id"] == vid), None)
                if vp:
                    chain.append(vp)
                    for ref in vp.get("references", []):
                        traverse(ref)
            
            traverse(viewpoint_id)
            return chain
    
    def vote_viewpoint(self, viewpoint_id: str, agent_id: str, vote: str, new_viewpoint: str = ""):
        """对观点投票（vote: support/oppose，反对时可以提新观点）"""
        with self._lock:
            for vp in self._viewpoints:
                if vp["id"] == viewpoint_id:
                    if vote == "support":
                        if agent_id not in vp["supporters"]:
                            vp["supporters"].append(agent_id)
                            vp["support_count"] += 1
                    elif vote == "oppose":
                        if agent_id not in vp["opposers"]:
                            vp["opposers"].append(agent_id)
                            vp["oppose_count"] += 1
                            # 反对时记录新观点
                            if new_viewpoint:
                                self.add_viewpoint(new_viewpoint, agent_id, 
                                                  viewpoint_type="oppose",
                                                  references=[viewpoint_id])
                    self._version += 1
                    return True
            return False
    
    def get_active_viewpoints(self) -> List[Dict]:
        """获取活跃观点"""
        with self._lock:
            return [v.copy() for v in self._viewpoints if v.get("status") == "active"]
    
    def aggregate_viewpoints(self, topic_id: str = None) -> Dict:
        """聚合观点摘要"""
        with self._lock:
            viewpoints = self._viewpoints
            if topic_id:
                viewpoints = [v for v in viewpoints if v.get("topic_id") == topic_id]
            
            # 按类型分组
            by_type = {}
            for vp in viewpoints:
                vp_type = vp.get("type", "neutral")
                if vp_type not in by_type:
                    by_type[vp_type] = []
                by_type[vp_type].append(vp)
            
            # 提取高频关键词
            all_content = " ".join([v["content"] for v in viewpoints])
            keywords = self._extract_keywords(all_content, top_n=10)
            
            # 代表性发言（被引用最多的）
            cited = sorted(viewpoints, key=lambda v: len(v.get("references", [])), reverse=True)[:5]
            
            return {
                "total": len(viewpoints),
                "by_type": {k: len(v) for k, v in by_type.items()},
                "keywords": keywords,
                "representative": [v["content"][:100] for v in cited]
            }
    
    def _extract_keywords(self, text: str, top_n: int = 10) -> List[str]:
        """提取关键词（简单实现）"""
        import re
        # 移除常见词
        stopwords = {"的", "是", "在", "和", "有", "我", "他", "她", "它", "这", "那", "了", "吗", "吧"}
        
        # 分词（简单按空格和标点）
        words = re.findall(r'[\u4e00-\u9fa5]+|[a-zA-Z]+', text)
        
        # 统计频率
        freq = {}
        for w in words:
            if len(w) > 1 and w not in stopwords:
                freq[w] = freq.get(w, 0) + 1
        
        # 返回top N
        return sorted(freq.keys(), key=lambda x: freq.get(x, 0), reverse=True)[:top_n]
    
    # ==================== 问题扩展 ====================
    
    def set_main_topic(self, topic: str):
        """设置主话题"""
        with self._lock:
            self._main_topic = topic
            # 同时作为第一个活跃议题
            if not self._active_issues:
                self._issue_counter += 1
                self._active_issues.append({
                    "id": f"issue_{self._issue_counter}",
                    "content": topic,
                    "status": "active",
                    "priority": 100,
                    "created_at": time.time(),
                    "parent_id": None
                })
            self._version += 1
    
    def get_main_topic(self) -> str:
        """获取主话题"""
        with self._lock:
            return self._main_topic
    
    def expand_issue(self, content: str, created_by: str, 
                      parent_issue_id: str = None) -> Dict:
        """扩展新议题（[EXPAND]触发）"""
        with self._lock:
            self._issue_counter += 1
            new_issue = {
                "id": f"issue_{self._issue_counter}",
                "content": content,
                "parent_id": parent_issue_id,
                "created_by": created_by,
                "created_at": time.time(),
                "status": "pending_vote",  # pending_vote → active/pending
                "priority": 50,            # 默认优先级
                "votes": {},               # {agent_id: support_weight}
                "viewpoints": [],
                "resolution": None
            }
            self._pending_issues.append(new_issue)
            self._version += 1
            return new_issue
    
    def vote_issue_expansion(self, issue_id: str, agent_id: str, 
                               support: bool, weight: float = 1.0) -> bool:
        """对新议题扩展进行表决"""
        with self._lock:
            for issue in self._pending_issues:
                if issue["id"] == issue_id and issue["status"] == "pending_vote":
                    issue["votes"][agent_id] = weight if support else 0
                    
                    # 检查是否达到阈值
                    total_weight = sum(issue["votes"].values())
                    voter_count = len([v for v in issue["votes"].values() if v > 0])
                    threshold = 0.5  # 50%支持率
                    
                    if voter_count / max(1, len(issue["votes"])) >= threshold:
                        # 接受扩展
                        issue["status"] = "active"
                        self._pending_issues.remove(issue)
                        self._active_issues.append(issue)
                        self._version += 1
                        return True
                    
                    return False
            return False
    
    def get_active_issues(self) -> List[Dict]:
        """获取活跃议题"""
        with self._lock:
            return [i.copy() for i in self._active_issues if i.get("status") == "active"]
    
    def get_pending_issues(self, include_resolved: bool = False) -> List[Dict]:
        """获取待处理议题"""
        with self._lock:
            if include_resolved:
                return [i.copy() for i in self._pending_issues]
            return [i.copy() for i in self._pending_issues if i.get("status") not in ["resolved", "shelved"]]
    
    def get_current_issue(self) -> Optional[Dict]:
        """获取当前讨论的议题"""
        with self._lock:
            if self._active_issues:
                return self._active_issues[0].copy()
            return None
    
    def suspend_issue(self, issue_id: str, reason: str = "") -> bool:
        """挂起当前议题（切换到子议题时）"""
        with self._lock:
            for issue in self._active_issues:
                if issue["id"] == issue_id:
                    issue["status"] = "suspended"
                    issue["suspend_reason"] = reason
                    issue["suspended_at"] = time.time()
                    self._version += 1
                    return True
            return False
    
    def resume_issue(self, issue_id: str) -> bool:
        """恢复挂起的议题"""
        with self._lock:
            for issue in self._active_issues:
                if issue["id"] == issue_id and issue.get("status") == "suspended":
                    issue["status"] = "active"
                    issue["resumed_at"] = time.time()
                    # 移到队列前面
                    self._active_issues.remove(issue)
                    self._active_issues.insert(0, issue)
                    self._version += 1
                    return True
            return False
    
    def prioritize_issues(self, votes: Dict[str, Dict[str, float]]):
        """议题优先级投票（每个代理分配点数）"""
        with self._lock:
            # votes: {agent_id: {issue_id: points}}
            issue_scores = {}
            
            for agent_id, issue_votes in votes.items():
                for issue_id, points in issue_votes.items():
                    if issue_id not in issue_scores:
                        issue_scores[issue_id] = 0
                    issue_scores[issue_id] += points
            
            # 按分数排序
            sorted_issues = sorted(
                self._active_issues,
                key=lambda x: issue_scores.get(x["id"], 0),
                reverse=True
            )
            
            for i, issue in enumerate(sorted_issues):
                issue["priority"] = issue_scores.get(issue["id"], 0)
            
            self._active_issues = sorted_issues
            self._version += 1
    
    def add_sub_topic(self, content: str, created_by: str, parent_id: str = None) -> Dict:
        """添加子话题（兼容旧接口）"""
        return self.expand_issue(content, created_by, parent_id)
    
    def get_sub_topics(self, status: str = None) -> List[Dict]:
        """获取子话题列表（兼容旧接口）"""
        return self.get_pending_issues() if status != "active" else self.get_active_issues()
    
    def update_sub_topic_status(self, sub_topic_id: str, status: str, resolution: str = None):
        """更新子话题状态"""
        with self._lock:
            for issue in self._active_issues + self._pending_issues:
                if issue.get("id") == sub_topic_id:
                    issue["status"] = status
                    if resolution:
                        issue["resolution"] = resolution
                    self._version += 1
                    return True
            return False
    
    # ==================== 暂存机制 ====================
    
    def park_issue(self, issue_id: str, reason: str = "", 
                    proposed_by: str = "system") -> Dict:
        """暂存议题（[PARK]触发或自动暂存）"""
        with self._lock:
            # 找到当前议题
            issue = None
            for i in self._active_issues:
                if i["id"] == issue_id:
                    issue = i
                    break
            
            if not issue:
                for i in self._pending_issues:
                    if i["id"] == issue_id:
                        issue = i
                        break
            
            if not issue:
                return None
            
            # 收集暂存内容
            parked = {
                "id": f"parked_{int(time.time() * 1000)}",
                "original_issue_id": issue_id,
                "content": issue.get("content", ""),
                "reason": reason,
                "proposed_by": proposed_by,
                "parked_at": time.time(),
                "status": "parked",  # parked, restored, merged
                
                # 保存讨论状态
                "viewpoints_summary": self._get_viewpoints_summary_for_issue(issue_id),
                "temp_consensus": self._get_temp_consensus(issue_id),
                "unresolved_points": self._get_unresolved_points(issue_id),
                "last_position": len(self._messages),  # 恢复位置
                
                # 统计
                "discussion_rounds": issue.get("discussion_rounds", 0),
                "participant_count": len(set(v.get("agent_id") for v in self._viewpoints 
                                            if v.get("topic_id") == issue_id))
            }
            
            # 移动到暂存列表
            issue["status"] = "parked"
            if issue in self._active_issues:
                self._active_issues.remove(issue)
            
            self._shelved_issues.append(parked)
            self._version += 1
            return parked
    
    def _get_viewpoints_summary_for_issue(self, issue_id: str) -> Dict:
        """获取议题的观点摘要"""
        viewpoints = [v for v in self._viewpoints if v.get("topic_id") == issue_id]
        
        by_type = {}
        for vp in viewpoints:
            vp_type = vp.get("type", "neutral")
            if vp_type not in by_type:
                by_type[vp_type] = []
            by_type[vp_type].append(vp["content"][:100])
        
        return {
            "total": len(viewpoints),
            "by_type": {k: len(v) for k, v in by_type.items()},
            "samples": {k: v[:3] for k, v in by_type.items()}
        }
    
    def _get_temp_consensus(self, issue_id: str) -> List[str]:
        """获取临时共识"""
        # 查找已通过的观点
        passed = [v for v in self._viewpoints 
                  if v.get("topic_id") == issue_id and v.get("status") == "passed"]
        return [v["content"][:100] for v in passed]
    
    def _get_unresolved_points(self, issue_id: str) -> List[str]:
        """获取未解决的分歧点"""
        # 查找争议观点（支持反对都多）
        controversial = []
        for v in self._viewpoints:
            if v.get("topic_id") == issue_id:
                if v.get("support_count", 0) > 0 and v.get("oppose_count", 0) > 0:
                    controversial.append(v["content"][:100])
        return controversial[:5]
    
    def restore_issue(self, parked_id: str) -> Optional[Dict]:
        """恢复暂存议题（[RESTORE]触发）"""
        with self._lock:
            for parked in self._shelved_issues:
                if parked["id"] == parked_id and parked.get("status") == "parked":
                    # 恢复到活跃队列
                    restored = {
                        "id": parked["original_issue_id"],
                        "content": parked["content"],
                        "status": "active",
                        "restored_from": parked_id,
                        "restored_at": time.time(),
                        "previous_viewpoints": parked.get("viewpoints_summary", {}),
                        "temp_consensus": parked.get("temp_consensus", []),
                        "unresolved_points": parked.get("unresolved_points", [])
                    }
                    
                    self._active_issues.insert(0, restored)
                    parked["status"] = "restored"
                    parked["restored_at"] = time.time()
                    self._version += 1
                    return restored
            return None
    
    def get_parked_issues(self, status: str = "parked") -> List[Dict]:
        """获取暂存议题列表"""
        with self._lock:
            if status:
                return [i.copy() for i in self._shelved_issues if i.get("status") == status]
            return [i.copy() for i in self._shelved_issues]
    
    def merge_parked_issues(self, parked_ids: List[str]) -> Optional[Dict]:
        """合并相似的暂存议题"""
        with self._lock:
            to_merge = []
            for parked in self._shelved_issues:
                if parked["id"] in parked_ids and parked.get("status") == "parked":
                    to_merge.append(parked)
            
            if len(to_merge) < 2:
                return None
            
            # 合并内容
            merged = {
                "id": f"merged_{int(time.time() * 1000)}",
                "merged_from": parked_ids,
                "content": " | ".join([p["content"][:50] for p in to_merge]),
                "status": "parked",
                "merged_at": time.time(),
                "viewpoints_summary": {},
                "reason": "系统自动合并相似议题"
            }
            
            # 合并观点摘要
            for p in to_merge:
                vs = p.get("viewpoints_summary", {})
                for k, v in vs.get("by_type", {}).items():
                    if k not in merged["viewpoints_summary"]:
                        merged["viewpoints_summary"][k] = 0
                    merged["viewpoints_summary"][k] += v
            
            # 标记原议题为已合并
            for p in to_merge:
                p["status"] = "merged"
                p["merged_into"] = merged["id"]
            
            self._shelved_issues.append(merged)
            self._version += 1
            return merged
    
    def auto_park_check(self, timeout_minutes: int = 10, 
                          no_new_viewpoint_rounds: int = 3) -> Optional[Dict]:
        """自动暂存检查（超时或无新观点）"""
        with self._lock:
            current = self.get_current_issue()
            if not current:
                return None
            
            # 检查超时
            created_at = current.get("created_at", time.time())
            if time.time() - created_at > timeout_minutes * 60:
                return self.park_issue(current["id"], "讨论超时，自动暂存")
            
            # 检查无新观点（需要外部调用更新）
            if current.get("no_new_viewpoint_rounds", 0) >= no_new_viewpoint_rounds:
                return self.park_issue(current["id"], "连续多轮无新观点，自动暂存")
            
            return None
    
    # 兼容旧接口
    def shelve_issue(self, content: str, reason: str = "", related_viewpoint_ids: List[str] = None):
        """暂存问题（兼容旧接口）"""
        return self.park_issue(content, reason, "system")
    
    def get_shelved_issues(self, status: str = None) -> List[Dict]:
        """获取暂存问题（兼容旧接口）"""
        return self.get_parked_issues(status or "parked")
    
    def resolve_shelved_issue(self, issue_id: str, resolution: str):
        """解决暂存问题（兼容旧接口）"""
        with self._lock:
            for issue in self._shelved_issues:
                if issue.get("id") == issue_id or issue.get("original_issue_id") == issue_id:
                    issue["status"] = "resolved"
                    issue["resolution"] = resolution
                    issue["resolved_at"] = time.time()
                    self._version += 1
                    return True
            return False
    
    # ==================== 投票管理 ====================
    
    def start_viewpoint_vote(self, viewpoint_id: str):
        """开始观点投票"""
        with self._lock:
            for vp in self._viewpoints:
                if vp["id"] == viewpoint_id:
                    vp["status"] = "voting"
                    self._voting_viewpoint = vp.copy()
                    self._version += 1
                    return True
            return False
    
    def get_voting_viewpoint(self) -> Optional[Dict]:
        """获取正在投票的观点"""
        with self._lock:
            return self._voting_viewpoint.copy() if self._voting_viewpoint else None
    
    def end_viewpoint_vote(self, passed: bool):
        """结束观点投票"""
        with self._lock:
            if self._voting_viewpoint:
                for vp in self._viewpoints:
                    if vp["id"] == self._voting_viewpoint["id"]:
                        vp["status"] = "passed" if passed else "rejected"
                        break
                self._voting_viewpoint = None
                self._version += 1
    
    # ==================== 排序投票（IRV/波达计数） ====================
    
    def start_ranked_vote(self, options: List[str], method: str = "irv") -> str:
        """开始排序投票"""
        with self._lock:
            vote_id = f"ranked_{int(time.time() * 1000)}"
            
            self._current_vote_session = {
                "vote_id": vote_id,
                "type": "ranked",
                "method": method,  # irv 或 borda
                "options": options,
                "rankings": {},    # {agent_id: [option1, option2, ...]}
                "started_at": time.time(),
                "status": "collecting",
                "anonymous": False  # 投票透明度
            }
            
            self._version += 1
            return vote_id
    
    def submit_ranking(self, agent_id: str, ranking: List[str]) -> bool:
        """提交排序（1st, 2nd, 3rd...）"""
        with self._lock:
            if not self._current_vote_session:
                return False
            
            if self._current_vote_session["status"] != "collecting":
                return False
            
            # 验证排序包含所有选项
            options = set(self._current_vote_session["options"])
            if set(ranking) != options:
                return False
            
            self._current_vote_session["rankings"][agent_id] = ranking
            self._version += 1
            return True
    
    def calculate_ranked_result(self) -> Dict:
        """计算排序投票结果"""
        with self._lock:
            if not self._current_vote_session:
                return {}
            
            method = self._current_vote_session.get("method", "irv")
            rankings = self._current_vote_session.get("rankings", {})
            options = self._current_vote_session.get("options", [])
            
            if method == "borda":
                return self._borda_count(options, rankings)
            else:
                return self._instant_runoff(options, rankings)
    
    def _borda_count(self, options: List[str], rankings: Dict) -> Dict:
        """波达计数法"""
        scores = {opt: 0 for opt in options}
        n = len(options)
        
        for agent_id, ranking in rankings.items():
            for i, opt in enumerate(ranking):
                # 第1名得n-1分，第2名得n-2分...
                scores[opt] += n - 1 - i
        
        sorted_results = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        
        return {
            "method": "borda",
            "winner": sorted_results[0][0] if sorted_results else None,
            "rankings": sorted_results,
            "scores": scores
        }
    
    def _instant_runoff(self, options: List[str], rankings: Dict) -> Dict:
        """即时决选投票（IRV）"""
        remaining = list(options)
        rounds = []
        
        while len(remaining) > 1:
            # 统计第一选择
            first_choices = {opt: 0 for opt in remaining}
            
            for agent_id, ranking in rankings.items():
                for opt in ranking:
                    if opt in remaining:
                        first_choices[opt] += 1
                        break
            
            total = sum(first_choices.values())
            rounds.append({
                "remaining": remaining.copy(),
                "votes": first_choices.copy()
            })
            
            # 检查是否有人过半
            for opt, count in first_choices.items():
                if total > 0 and count / total > 0.5:
                    return {
                        "method": "irv",
                        "winner": opt,
                        "rounds": rounds,
                        "final_vote": first_choices
                    }
            
            # 淘汰得票最少的
            min_votes = min(first_choices.values())
            for opt in remaining:
                if first_choices[opt] == min_votes:
                    remaining.remove(opt)
                    break
        
        return {
            "method": "irv",
            "winner": remaining[0] if remaining else None,
            "rounds": rounds
        }
    
    def set_vote_anonymous(self, anonymous: bool):
        """设置投票透明度"""
        with self._lock:
            if self._current_vote_session:
                self._current_vote_session["anonymous"] = anonymous
                self._version += 1
    
    def get_vote_report(self) -> Dict:
        """生成投票报告"""
        with self._lock:
            if not self._current_vote_session:
                return {}
            
            session = self._current_vote_session.copy()
            
            # 计算结果
            result = self.calculate_ranked_result()
            
            # 构建报告
            report = {
                "vote_id": session.get("vote_id"),
                "method": session.get("method"),
                "options": session.get("options"),
                "total_voters": len(session.get("rankings", {})),
                "anonymous": session.get("anonymous", False),
                "result": result,
                "timestamp": time.time()
            }
            
            # 非匿名时显示详细投票
            if not session.get("anonymous"):
                report["details"] = session.get("rankings", {})
            
            return report
    
    def end_ranked_vote(self) -> Dict:
        """结束排序投票"""
        with self._lock:
            if not self._current_vote_session:
                return {}
            
            self._current_vote_session["status"] = "completed"
            self._current_vote_session["ended_at"] = time.time()
            
            report = self.get_vote_report()
            
            # 保存到历史
            self._vote_sessions.append(self._current_vote_session.copy())
            self._current_vote_session = None
            
            self._version += 1
            return report
    
    # ==================== 复盘功能 ====================
    
    def add_review_record(self, content: str, review_type: str = "summary"):
        """添加复盘记录"""
        with self._lock:
            record = {
                "id": f"rv_{int(time.time() * 1000)}",
                "content": content,
                "type": review_type,  # summary, conclusion, action_item, annotation
                "created_at": time.time()
            }
            self._review_records.append(record)
            self._version += 1
            return record
    
    def get_review_records(self) -> List[Dict]:
        """获取复盘记录"""
        with self._lock:
            return [r.copy() for r in self._review_records]
    
    def generate_review_summary(self) -> str:
        """生成复盘摘要"""
        with self._lock:
            lines = ["=== 会议复盘 ===\n"]
            
            # 主话题
            if self._main_topic:
                lines.append(f"主话题: {self._main_topic}\n")
            
            # 已通过的观点
            passed_vps = [v for v in self._viewpoints if v.get("status") == "passed"]
            if passed_vps:
                lines.append("已通过观点:")
                for vp in passed_vps:
                    lines.append(f"  - {vp['content'][:100]} (支持: {vp['support_count']})")
                lines.append("")
            
            # 议题状态
            all_issues = self._active_issues + self._pending_issues
            if all_issues:
                lines.append("议题状态:")
                for issue in all_issues[:10]:
                    status_cn = {"active": "讨论中", "suspended": "已挂起", "resolved": "已解决", "parked": "已暂存"}
                    lines.append(f"  - [{status_cn.get(issue['status'], issue['status'])}] {issue['content'][:50]}")
                lines.append("")
            
            # 暂存问题
            parked = [i for i in self._shelved_issues if i.get("status") == "parked"]
            if parked:
                lines.append("待处理暂存问题:")
                for issue in parked:
                    lines.append(f"  - {issue['content'][:50]}")
                lines.append("")
            
            # 复盘记录
            if self._review_records:
                lines.append("复盘总结:")
                for r in self._review_records:
                    lines.append(f"  - {r['content'][:100]}")
            
            return "\n".join(lines)
    
    def generate_full_review_report(self) -> Dict:
        """生成完整复盘报告"""
        with self._lock:
            report = {
                "generated_at": time.time(),
                "main_topic": self._main_topic
            }
            
            # 1. 讨论摘要（时间线）
            report["timeline"] = self._build_timeline()
            
            # 2. 代理表现评估
            report["agent_performance"] = self._evaluate_agents()
            
            # 3. 无效争论检测
            report["invalid_arguments"] = self._detect_invalid_arguments()
            
            # 4. 决策路径回溯
            report["decision_path"] = self._build_decision_path()
            
            # 5. 观点分析
            report["viewpoint_analysis"] = self.aggregate_viewpoints()
            
            # 6. 异常摘要
            report["exception_summary"] = self.get_exception_summary()
            
            return report
    
    def _build_timeline(self) -> List[Dict]:
        """构建讨论时间线"""
        timeline = []
        
        for msg in self._messages:
            if msg.message_type in ["normal", "interrupt", "vote"]:
                timeline.append({
                    "time": msg.timestamp,
                    "agent": msg.agent_id,
                    "type": msg.message_type,
                    "content": msg.content[:100]
                })
        
        timeline.sort(key=lambda x: x["time"])
        return timeline
    
    def _evaluate_agents(self) -> Dict:
        """评估代理表现"""
        evaluations = {}
        
        for vp in self._viewpoints:
            agent_id = vp.get("agent_id")
            if agent_id not in evaluations:
                evaluations[agent_id] = {
                    "speak_count": 0,
                    "cited_count": 0,
                    "interrupt_count": 0,
                    "proposals_made": 0,
                    "proposals_passed": 0,
                    "vote_consistency": 0
                }
            
            evaluations[agent_id]["speak_count"] += 1
            
            if vp.get("support_count", 0) > 0:
                evaluations[agent_id]["cited_count"] += 1
            
            if vp.get("type") in ["support", "modify"]:
                evaluations[agent_id]["proposals_made"] += 1
                if vp.get("status") == "passed":
                    evaluations[agent_id]["proposals_passed"] += 1
        
        for msg in self._messages:
            if msg.message_type == "interrupt":
                agent_id = msg.agent_id
                if agent_id in evaluations:
                    evaluations[agent_id]["interrupt_count"] += 1
        
        for agent_id, stats in evaluations.items():
            stats["score"] = (
                stats["speak_count"] * 0.2 +
                stats["cited_count"] * 0.3 +
                stats["proposals_passed"] * 0.3 +
                stats["proposals_made"] * 0.1 -
                stats["interrupt_count"] * 0.1
            )
        
        return evaluations
    
    def _detect_invalid_arguments(self) -> List[Dict]:
        """检测无效争论"""
        invalid = []
        
        content_map = {}
        for vp in self._viewpoints:
            content_key = vp["content"][:50]
            if content_key in content_map:
                if len(content_map[content_key]) > 1:
                    invalid.append({
                        "type": "repetition",
                        "agents": [v["agent_id"] for v in content_map[content_key]],
                        "content": content_key
                    })
            else:
                content_map[content_key] = [vp]
        
        for vp1 in self._viewpoints:
            for ref_id in vp1.get("references", []):
                vp2 = next((v for v in self._viewpoints if v["id"] == ref_id), None)
                if vp2 and vp1["id"] in vp2.get("references", []):
                    invalid.append({
                        "type": "circular_argument",
                        "agents": [vp1["agent_id"], vp2["agent_id"]],
                        "content": f"{vp1['content'][:30]} <-> {vp2['content'][:30]}"
                    })
        
        return invalid
    
    def _build_decision_path(self) -> List[Dict]:
        """构建决策路径"""
        path = []
        
        resolution = self._final_resolution
        if resolution:
            passed_vps = [v for v in self._viewpoints if v.get("status") == "passed"]
            
            path.append({
                "step": "final_resolution",
                "content": resolution,
                "based_on": [v["id"] for v in passed_vps]
            })
            
            for vp in passed_vps:
                chain = self.get_viewpoint_chain(vp["id"])
                for i, v in enumerate(chain):
                    path.append({
                        "step": f"viewpoint_{i}",
                        "id": v["id"],
                        "agent": v["agent_id"],
                        "type": v.get("type"),
                        "content": v["content"][:80],
                        "status": v.get("status")
                    })
        
        rejected_vps = [v for v in self._viewpoints if v.get("status") == "rejected"]
        for vp in rejected_vps:
            path.append({
                "step": "rejected",
                "id": vp["id"],
                "agent": vp["agent_id"],
                "content": vp["content"][:80],
                "reason": f"支持{vp['support_count']} vs 反对{vp['oppose_count']}"
            })
        
        return path
    
    def add_user_annotation(self, agent_id: str, annotation: str, score_delta: float = 0):
        """添加用户注释"""
        with self._lock:
            record = {
                "id": f"ann_{int(time.time() * 1000)}",
                "type": "user_annotation",
                "agent_id": agent_id,
                "annotation": annotation,
                "score_delta": score_delta,
                "created_at": time.time()
            }
            self._review_records.append(record)
            
            if score_delta != 0 and agent_id in self._agent_contributions:
                self._agent_contributions[agent_id] += score_delta
            
            self._version += 1
            return record
    
    # ==================== 异常记录 ====================
    
    def add_exception_record(self, exception_type: str, details: Dict, 
                              recovery_action: str, recovery_result: str):
        """添加异常记录"""
        with self._lock:
            record = {
                "id": f"ex_{int(time.time() * 1000)}",
                "type": exception_type,
                "details": details,
                "recovery_action": recovery_action,
                "recovery_result": recovery_result,
                "timestamp": time.time()
            }
            self._exception_records.append(record)
            self._version += 1
            return record
    
    def get_exception_records(self, limit: int = 50) -> List[Dict]:
        """获取异常记录"""
        with self._lock:
            return [r.copy() for r in self._exception_records[-limit:]]
    
    def get_exception_summary(self) -> Dict:
        """获取异常摘要"""
        with self._lock:
            by_type = {}
            for r in self._exception_records:
                t = r["type"]
                by_type[t] = by_type.get(t, 0) + 1
            
            return {
                "total": len(self._exception_records),
                "by_type": by_type
            }
    
    # ==================== 演化数据 ====================
    
    def set_evolution_data(self, data: Dict):
        """设置演化数据"""
        with self._lock:
            self._evolution_data = data
            self._version += 1
    
    def get_evolution_data(self) -> Dict:
        """获取演化数据"""
        with self._lock:
            return self._evolution_data.copy()
    
    def add_evolution_record(self, generation: int, records: List[Dict]):
        """添加演化记录"""
        with self._lock:
            if "history" not in self._evolution_data:
                self._evolution_data["history"] = []
            
            self._evolution_data["history"].append({
                "generation": generation,
                "records": records,
                "timestamp": time.time()
            })
            self._version += 1
    
    # ==================== 会话统计 ====================
    
    def update_session_stats(self, messages_delta: int = 0, votes_delta: int = 0):
        """更新会话统计"""
        with self._lock:
            if self._session_stats["start_time"] is None:
                self._session_stats["start_time"] = time.time()
                self._session_stats["total_sessions"] = 1
            
            self._session_stats["total_messages"] += messages_delta
            self._session_stats["total_votes"] += votes_delta
            self._version += 1
    
    def get_session_stats(self) -> Dict:
        """获取会话统计"""
        with self._lock:
            stats = self._session_stats.copy()
            if stats["start_time"]:
                stats["duration"] = time.time() - stats["start_time"]
            return stats
    
    def generate_review_summary(self) -> str:
        """生成复盘摘要"""
        with self._lock:
            lines = ["=== 会议复盘 ===\n"]
            
            # 主话题
            if self._main_topic:
                lines.append(f"主话题: {self._main_topic}\n")
            
            # 已通过的观点
            passed_vps = [v for v in self._viewpoints if v.get("status") == "passed"]
            if passed_vps:
                lines.append("已通过观点:")
                for vp in passed_vps:
                    lines.append(f"  - {vp['content'][:100]} (支持: {vp['support_count']})")
                lines.append("")
            
            # 子话题状态
            if self._sub_topics:
                lines.append("子话题:")
                for st in self._sub_topics:
                    status_cn = {"pending": "待讨论", "discussing": "讨论中", "resolved": "已解决", "shelved": "已暂存"}
                    lines.append(f"  - [{status_cn.get(st['status'], st['status'])}] {st['content'][:50]}")
                lines.append("")
            
            # 暂存问题
            shelved = [i for i in self._shelved_issues if i.get("status") == "shelved"]
            if shelved:
                lines.append("待处理暂存问题:")
                for issue in shelved:
                    lines.append(f"  - {issue['content'][:50]}")
                lines.append("")
            
            # 复盘记录
            if self._review_records:
                lines.append("复盘总结:")
                for r in self._review_records:
                    lines.append(f"  - {r['content'][:100]}")
            
            return "\n".join(lines)
    
    # ==================== 状态标志操作 ====================
    
    def set_pause_flag(self, paused: bool):
        """设置暂停标志"""
        with self._lock:
            self._pause_flag = paused
            self._version += 1
    
    def is_paused(self) -> bool:
        """检查是否暂停"""
        with self._lock:
            return self._pause_flag
    
    def set_current_mode(self, mode: str):
        """设置当前模式"""
        with self._lock:
            self._current_mode = mode
            self._version += 1
    
    def get_current_mode(self) -> Optional[str]:
        """获取当前模式"""
        with self._lock:
            return self._current_mode
    
    def set_meeting_phase(self, phase: str):
        """设置会议阶段"""
        with self._lock:
            self._meeting_phase = phase
    
    def get_meeting_phase(self) -> str:
        """获取会议阶段"""
        with self._lock:
            return self._meeting_phase
    
    # ==================== 代理贡献系数 ====================
    
    def init_agent_contribution(self, agent_id: str):
        """初始化代理贡献系数"""
        with self._lock:
            if agent_id not in self._agent_contributions:
                self._agent_contributions[agent_id] = 1.0
    
    def update_agent_contribution(self, agent_id: str, delta: float):
        """更新代理贡献系数"""
        with self._lock:
            current = self._agent_contributions.get(agent_id, 1.0)
            # 限制范围 0.5 ~ 2.0
            self._agent_contributions[agent_id] = max(0.5, min(2.0, current + delta))
    
    def get_agent_contribution(self, agent_id: str) -> float:
        """获取代理贡献系数"""
        with self._lock:
            return self._agent_contributions.get(agent_id, 1.0)
    
    def get_all_contributions(self) -> Dict[str, float]:
        """获取所有代理贡献系数"""
        with self._lock:
            return deepcopy(self._agent_contributions)
    
    # ==================== 快照操作 ====================
    
    def get_snapshot(self) -> Dict:
        """获取白板快照"""
        with self._lock:
            return {
                'session_id': self.session_id,
                'version': self._version,
                'message_count': len(self._messages),
                'tool_result_count': len(self._tool_results),
                'workspace_files': list(self._workspace_files.keys()),
                'consensus_count': len(self._consensus),
                'pending_issue_count': len(self._pending_issues),
                'task_queue_length': len(self._task_queue),
                'final_resolution': self._final_resolution,
                'current_mode': self._current_mode,
                'pause_flag': self._pause_flag,
                'agent_contributions': deepcopy(self._agent_contributions)
            }
    
    def clear_session_data(self):
        """清空会话数据（保留session_id）"""
        with self._lock:
            self._messages.clear()
            self._tool_results.clear()
            self._workspace_files.clear()
            self._consensus.clear()
            self._pending_issues.clear()
            self._task_queue.clear()
            self._final_resolution = None
            self._pause_flag = False
            self._agent_contributions.clear()
            # 清空事实白板
            self._fact_board.clear()
            self._user_viewpoints.clear()
            # 清空投票记录
            self._vote_sessions.clear()
            self._current_vote_session = None
            self._agent_weights.clear()
            self._contribution_records.clear()
            self._version += 1
    
    # ==================== 投票记录相关 ====================
    
    def start_vote_session(self, session_id: str, proposal: str, proposer: str):
        """开始投票会话"""
        with self._lock:
            self._current_vote_session = {
                "session_id": session_id,
                "proposal": proposal,
                "proposer": proposer,
                "start_time": time.time(),
                "votes": {},
                "status": "collecting"
            }
            self._version += 1
    
    def record_vote(self, voter_id: str, vote_type: str, weight: float, reason: str = ""):
        """记录投票"""
        with self._lock:
            if self._current_vote_session:
                self._current_vote_session["votes"][voter_id] = {
                    "type": vote_type,
                    "weight": weight,
                    "reason": reason,
                    "timestamp": time.time()
                }
                self._version += 1
    
    def end_vote_session(self, passed: bool, support_ratio: float, details: Dict = None):
        """结束投票会话"""
        with self._lock:
            if self._current_vote_session:
                self._current_vote_session["end_time"] = time.time()
                self._current_vote_session["passed"] = passed
                self._current_vote_session["support_ratio"] = support_ratio
                self._current_vote_session["status"] = "completed"
                if details:
                    self._current_vote_session["details"] = details
                
                # 保存到历史
                self._vote_sessions.append(self._current_vote_session.copy())
                self._current_vote_session = None
                self._version += 1
    
    def get_current_vote_session(self) -> Optional[Dict]:
        """获取当前投票会话"""
        with self._lock:
            return self._current_vote_session.copy() if self._current_vote_session else None
    
    def get_vote_history(self, limit: int = 10) -> List[Dict]:
        """获取投票历史"""
        with self._lock:
            return self._vote_sessions[-limit:]
    
    def set_agent_weights(self, weights: Dict[str, float]):
        """设置代理权重（公开）"""
        with self._lock:
            self._agent_weights = weights.copy()
            self._version += 1
    
    def get_agent_weights(self) -> Dict[str, float]:
        """获取代理权重"""
        with self._lock:
            return self._agent_weights.copy()
    
    def add_contribution_record(self, record: Dict):
        """添加贡献记录"""
        with self._lock:
            self._contribution_records.append({
                **record,
                "timestamp": time.time()
            })
            self._version += 1
    
    def record_contribution(self, agent_id: str, contribution_value: float):
        """记录代理贡献值"""
        with self._lock:
            if agent_id not in self._agent_contributions:
                self._agent_contributions[agent_id] = 0.0
            self._agent_contributions[agent_id] += contribution_value
            self._version += 1
    
    def init_agent_contribution(self, agent_id: str):
        """初始化代理贡献记录"""
        with self._lock:
            if agent_id not in self._agent_contributions:
                self._agent_contributions[agent_id] = 0.0
            self._version += 1
    
    def get_agent_contributions(self) -> Dict[str, float]:
        """获取所有代理贡献值"""
        with self._lock:
            return self._agent_contributions.copy()
    
    def get_contribution_records(self, agent_id: str = None, limit: int = 50) -> List[Dict]:
        """获取贡献记录"""
        with self._lock:
            records = self._contribution_records
            if agent_id:
                records = [r for r in records if r.get("agent_id") == agent_id]
            return records[-limit:]
    
    def get_voting_summary(self) -> Dict:
        """获取投票摘要"""
        with self._lock:
            total = len(self._vote_sessions)
            passed = sum(1 for s in self._vote_sessions if s.get("passed"))
            
            return {
                "total_votes": total,
                "passed": passed,
                "rejected": total - passed,
                "pass_rate": round(passed / total * 100, 1) if total > 0 else 0,
                "current_session": self._current_vote_session["session_id"] if self._current_vote_session else None,
                "agent_weights": self._agent_weights.copy()
            }
    
    def get_context_for_agent(self, agent_id: str, include_all_messages: bool = True) -> str:
        """为代理生成上下文字符串"""
        with self._lock:
            lines = [f"=== 会话 {self.session_id} ==="]
            
            # 添加消息
            if self._messages:
                lines.append("\n--- 讨论记录 ---")
                for msg in self._messages:
                    time_str = datetime.fromtimestamp(msg.timestamp).strftime("%H:%M:%S")
                    prefix = f"[{time_str}] {msg.agent_id}"
                    if msg.message_type == "interrupt":
                        prefix = f"[INTERRUPT] {msg.agent_id}"
                    elif msg.message_type == "request_meeting":
                        prefix = f"[REQUEST_MEETING] {msg.agent_id}"
                    lines.append(f"{prefix}: {msg.content}")
            
            # 添加工具结果摘要
            if self._tool_results:
                lines.append("\n--- 工具调用记录 ---")
                for tr in self._tool_results[-10:]:  # 最近10条
                    status = "[OK]" if tr.success else "[FAIL]"
                    lines.append(f"{status} {tr.caller} 调用 {tr.tool}: {tr.result}")
            
            # 添加共识
            if self._consensus:
                lines.append("\n--- 已达成共识 ---")
                for c in self._consensus:
                    lines.append(f"- {c.content} (支持者: {', '.join(c.supporters)})")
            
            # 添加待解决问题
            if self._pending_issues:
                lines.append("\n--- 待解决问题 ---")
                for i, issue in enumerate(self._pending_issues):
                    resolved = issue.get('resolved', False)
                    status = "[已解决]" if resolved else "[待解决]"
                    lines.append(f"{status} {i}. {issue}")
            
            # 添加议程
            if self._agenda:
                lines.append("\n--- 议程 ---")
                for i, item in enumerate(self._agenda):
                    current = "> " if i == self._current_agenda_index else "  "
                    status = item.get('status', 'pending')
                    status_icon = {"pending": "[待处理]", "discussing": "[讨论中]", "resolved": "[已解决]", "tabled": "[搁置]"}.get(status, "[?]")
                    lines.append(f"{current}{status_icon} {item.get('title', '未知议题')}")
            
            return "\n".join(lines)
    
    # ==================== 议程管理 ====================
    
    def set_agenda(self, items: List[Dict]):
        """设置议程"""
        with self._lock:
            self._agenda = []
            for i, item in enumerate(items):
                agenda_item = {
                    "id": f"agenda_{i+1}",
                    "title": item.get("title", f"议程{i+1}"),
                    "description": item.get("description", ""),
                    "status": "pending" if i > 0 else "discussing",  # 第一个设为讨论中
                    "end_votes": [],  # 结束投票记录
                    "conclusion": None,  # 该议程的结论
                    "created_at": time.time()
                }
                self._agenda.append(agenda_item)
            self._current_agenda_index = 0
            self._version += 1
    
    def get_agenda(self) -> List[Dict]:
        """获取议程"""
        with self._lock:
            return [a.copy() for a in self._agenda]
    
    def get_current_agenda_item(self) -> Optional[Dict]:
        """获取当前议程项"""
        with self._lock:
            if self._current_agenda_index < len(self._agenda):
                return self._agenda[self._current_agenda_index].copy()
            return None
    
    def vote_end_current_agenda(self, agent_id: str, agree: bool, reason: str = "") -> Dict:
        """投票结束当前议程"""
        with self._lock:
            if self._current_agenda_index >= len(self._agenda):
                return {"success": False, "error": "没有当前议程"}
            
            current = self._agenda[self._current_agenda_index]
            
            # 检查是否已投票
            for vote in current["end_votes"]:
                if vote["agent_id"] == agent_id:
                    return {"success": False, "error": "已投票"}
            
            # 记录投票
            current["end_votes"].append({
                "agent_id": agent_id,
                "agree": agree,
                "reason": reason,
                "timestamp": time.time()
            })
            self._version += 1
            
            # 统计结果
            total_agents = len(self._agent_contributions) or 1
            agree_count = sum(1 for v in current["end_votes"] if v["agree"])
            agree_ratio = agree_count / total_agents
            
            return {
                "success": True,
                "agree_count": agree_count,
                "total_agents": total_agents,
                "agree_ratio": agree_ratio,
                "should_end": agree_ratio >= 0.5  # 50%同意则结束
            }
    
    def check_agenda_end_consensus(self) -> Tuple[bool, float]:
        """检查当前议程是否达成结束共识"""
        with self._lock:
            if self._current_agenda_index >= len(self._agenda):
                return False, 0.0
            
            current = self._agenda[self._current_agenda_index]
            total_agents = len(self._agent_contributions) or 1
            agree_count = sum(1 for v in current["end_votes"] if v["agree"])
            agree_ratio = agree_count / total_agents
            
            return agree_ratio >= 0.5, agree_ratio
    
    def advance_agenda(self, conclusion: str = None) -> bool:
        """推进到下一个议程项"""
        with self._lock:
            if self._current_agenda_index < len(self._agenda):
                # 保存当前议程结论
                if conclusion:
                    self._agenda[self._current_agenda_index]['conclusion'] = conclusion
                self._agenda[self._current_agenda_index]['status'] = 'resolved'
                self._agenda[self._current_agenda_index]['resolved_at'] = time.time()
            
            self._current_agenda_index += 1
            self._version += 1
            
            if self._current_agenda_index < len(self._agenda):
                self._agenda[self._current_agenda_index]['status'] = 'discussing'
                return True
            return False
    
    def update_agenda_item(self, index: int, updates: Dict):
        """更新议程项"""
        with self._lock:
            if 0 <= index < len(self._agenda):
                self._agenda[index].update(updates)
                self._version += 1
    
    def get_agenda_progress(self) -> Dict:
        """获取议程进度"""
        with self._lock:
            total = len(self._agenda)
            resolved = sum(1 for a in self._agenda if a.get('status') == 'resolved')
            current_item = self._agenda[self._current_agenda_index] if self._current_agenda_index < total else None
            
            return {
                "total": total,
                "current_index": self._current_agenda_index,
                "current_title": current_item.get("title") if current_item else None,
                "resolved": resolved,
                "progress": f"{resolved}/{total}" if total > 0 else "0/0",
                "is_last": self._current_agenda_index >= total - 1
            }
    
    def set_agenda_conclusion(self, conclusion: str):
        """设置当前议程结论"""
        with self._lock:
            if self._current_agenda_index < len(self._agenda):
                self._agenda[self._current_agenda_index]['conclusion'] = conclusion
                self._version += 1
    
    def store_conclusion(self, agenda_title: str, conclusion: str, ranked_proposals: List[tuple] = None):
        """存储结论到历史记录"""
        with self._lock:
            if not hasattr(self, '_history_conclusions'):
                self._history_conclusions = []
            
            record = {
                "agenda_title": agenda_title,
                "conclusion": conclusion,
                "ranked_proposals": [p[0] for p in ranked_proposals] if ranked_proposals else [],
                "timestamp": time.time(),
                "agenda_index": self._current_agenda_index
            }
            self._history_conclusions.append(record)
            self._version += 1
    
    def get_history_conclusions(self) -> List[Dict]:
        """获取所有历史结论"""
        with self._lock:
            return getattr(self, '_history_conclusions', [])
    
    def get_last_conclusion(self) -> Optional[Dict]:
        """获取最近一条结论"""
        with self._lock:
            conclusions = getattr(self, '_history_conclusions', [])
            return conclusions[-1] if conclusions else None
    
    def get_agenda_status_text(self) -> str:
        """获取议程状态文本（供代理阅读）"""
        with self._lock:
            if not self._agenda:
                return "无议程"
            
            lines = ["=== 会议议程 ==="]
            for i, item in enumerate(self._agenda):
                if i == self._current_agenda_index:
                    lines.append(f"▶ [{item['status']}] {item['title']}")
                    if item.get("description"):
                        lines.append(f"   描述：{item['description']}")
                    # 显示结束投票进度
                    total_agents = len(self._agent_contributions) or 1
                    agree_count = sum(1 for v in item["end_votes"] if v["agree"])
                    lines.append(f"   结束意向：{agree_count}/{total_agents}")
                else:
                    status_icon = {"pending": "○", "discussing": "▶", "resolved": "✓"}.get(item['status'], "?")
                    lines.append(f"{status_icon} {item['title']}")
                    if item.get("conclusion"):
                        lines.append(f"   结论：{item['conclusion'][:50]}...")
            
            return "\n".join(lines)
    
    # ==================== 元数据管理 ====================
    
    def set_metadata(self, key: str, value: Any):
        """设置元数据"""
        with self._lock:
            self._metadata[key] = value
            self._version += 1
    
    def get_metadata(self, key: str, default: Any = None) -> Any:
        """获取元数据"""
        with self._lock:
            return self._metadata.get(key, default)
    
    def get_all_metadata(self) -> Dict:
        """获取所有元数据"""
        with self._lock:
            return self._metadata.copy()
    
    def clear_metadata(self):
        """清空元数据"""
        with self._lock:
            self._metadata.clear()
            self._version += 1
    
    # ==================== 搁置问题管理 ====================
    
    def add_pending_issue(self, content: str, reason: str = "", proposer: str = ""):
        """添加搁置问题"""
        with self._lock:
            issue = {
                "id": f"issue_{int(time.time())}",
                "content": content,
                "reason": reason,
                "proposer": proposer,
                "tabled_at": time.time(),
                "resolved": False
            }
            self._pending_issues.append(issue)
            self._version += 1
            return issue
    
    def resolve_pending_issue(self, issue_id: str, resolution: str = ""):
        """解决搁置问题"""
        with self._lock:
            for issue in self._pending_issues:
                if issue.get("id") == issue_id:
                    issue["resolved"] = True
                    issue["resolution"] = resolution
                    issue["resolved_at"] = time.time()
                    self._version += 1
                    return True
            return False
    
    def get_pending_issues(self, include_resolved: bool = False) -> List[Dict]:
        """获取搁置问题列表"""
        with self._lock:
            if include_resolved:
                return self._pending_issues.copy()
            return [i for i in self._pending_issues if not i.get("resolved")]
    
    # ==================== 发言权重调整 ====================
    
    def set_agent_contribution(self, agent_id: str, weight: float):
        """设置代理贡献权重"""
        with self._lock:
            self._agent_contributions[agent_id] = max(0.1, min(2.0, weight))
            self._version += 1
    
    def adjust_agent_weight(self, agent_id: str, factor: float):
        """调整代理权重（乘以因子）"""
        with self._lock:
            current = self._agent_contributions.get(agent_id, 1.0)
            self._agent_contributions[agent_id] = max(0.1, min(2.0, current * factor))
            self._version += 1

    # ==================== 代理状态管理（异步轮询版） ====================
    
    def init_agent_state(self, agent_id: str, expertise: str = ""):
        """初始化代理状态"""
        with self._lock:
            if agent_id not in self._agent_states:
                self._agent_states[agent_id] = {
                    "last_round": 0,
                    "contribution_score": 1.0,
                    "efficiency_score": 1.0,
                    "expertise": expertise,
                    "is_thinking": False,
                    "last_read_index": 0,
                    "last_tool_read_index": 0,
                    "total_speaks": 0,
                    "last_speak_time": 0.0,
                    "consecutive_duplicates": 0,  # 连续重复次数
                    "extra_sleep": 0.0,  # 额外休眠时间（惩罚）
                }
                self._version += 1
    
    def update_agent_state(self, agent_id: str, **kwargs):
        """更新代理状态"""
        with self._lock:
            if agent_id not in self._agent_states:
                self.init_agent_state(agent_id)
            self._agent_states[agent_id].update(kwargs)
            self._version += 1
    
    def get_agent_state(self, agent_id: str) -> Dict:
        """获取代理状态"""
        with self._lock:
            return self._agent_states.get(agent_id, {}).copy()
    
    def get_all_agent_states(self) -> Dict[str, Dict]:
        """获取所有代理状态"""
        with self._lock:
            return {k: v.copy() for k, v in self._agent_states.items()}
    
    def get_agent_last_read_index(self, agent_id: str) -> int:
        """获取代理上次读取的消息索引"""
        with self._lock:
            state = self._agent_states.get(agent_id, {})
            return state.get("last_read_index", 0)
    
    def update_agent_last_read_index(self, agent_id: str, index: int):
        """更新代理上次读取的消息索引"""
        with self._lock:
            if agent_id not in self._agent_states:
                self.init_agent_state(agent_id)
            self._agent_states[agent_id]["last_read_index"] = index
            self._version += 1
    
    # ==================== 增量读取方法 ====================
    
    def get_new_events(self, agent_id: str) -> Dict:
        """获取自上次读取以来的新事件（消息、工具结果、系统事件）"""
        with self._lock:
            state = self._agent_states.get(agent_id, {})
            last_msg_idx = state.get("last_read_index", 0)
            last_tool_idx = state.get("last_tool_read_index", 0)
            
            new_messages = self._messages[last_msg_idx:]
            new_tool_results = self._tool_results[last_tool_idx:]
            
            return {
                "messages": [m.to_dict() if hasattr(m, 'to_dict') else str(m) for m in new_messages],
                "tool_results": [t.to_dict() if hasattr(t, 'to_dict') else t for t in new_tool_results],
                "voting_mode": self._voting_mode,
                "think_mode": self._think_mode,
                "serial_mode": self._serial_mode,
                "current_agenda": self._agenda[self._current_agenda_index] if self._agenda else None,
            }
    
    def mark_events_read(self, agent_id: str):
        """标记事件已读"""
        with self._lock:
            if agent_id not in self._agent_states:
                self.init_agent_state(agent_id)
            self._agent_states[agent_id]["last_read_index"] = len(self._messages)
            self._agent_states[agent_id]["last_tool_read_index"] = len(self._tool_results)
            self._version += 1
    
    # ==================== 行动项管理 ====================
    
    def add_action_item(self, description: str, assignee: str = "", 
                        due_time: Optional[float] = None) -> Dict:
        """添加行动项"""
        with self._lock:
            item = {
                "id": f"action_{int(time.time() * 1000)}",
                "description": description,
                "assignee": assignee,
                "due_time": due_time,
                "status": "pending",
                "created_at": time.time(),
            }
            self._action_items.append(item)
            self._version += 1
            return item
    
    def update_action_item(self, item_id: str, status: str = None, **kwargs):
        """更新行动项"""
        with self._lock:
            for item in self._action_items:
                if item.get("id") == item_id:
                    if status:
                        item["status"] = status
                    item.update(kwargs)
                    item["updated_at"] = time.time()
                    self._version += 1
                    return True
            return False
    
    def get_action_items(self, status: Optional[str] = None) -> List[Dict]:
        """获取行动项"""
        with self._lock:
            if status:
                return [i.copy() for i in self._action_items if i.get("status") == status]
            return [i.copy() for i in self._action_items]
    
    # ==================== 全局模式标志 ====================
    
    def set_voting_mode(self, active: bool):
        """设置表决模式"""
        with self._lock:
            self._voting_mode = active
            self._version += 1
    
    def set_think_mode(self, active: bool):
        """设置思考暂停模式"""
        with self._lock:
            self._think_mode = active
            self._version += 1
    
    def set_serial_mode(self, active: bool):
        """设置串行模式"""
        with self._lock:
            now = time.time()
            # 防震荡：检查最小停留时间
            if self._last_mode_switch_time > 0:
                elapsed = now - self._last_mode_switch_time
                if elapsed < self._min_mode_stay_time:
                    return False  # 拒绝切换
            self._serial_mode = active
            self._last_mode_switch_time = now
            self._version += 1
            return True
    
    def is_voting_mode(self) -> bool:
        """检查是否处于表决模式"""
        with self._lock:
            return self._voting_mode
    
    def is_think_mode(self) -> bool:
        """检查是否处于思考暂停模式"""
        with self._lock:
            return self._think_mode
    
    def is_serial_mode(self) -> bool:
        """检查是否处于串行模式"""
        with self._lock:
            return self._serial_mode
    
    def can_switch_mode(self) -> bool:
        """检查是否可以切换模式"""
        with self._lock:
            if self._last_mode_switch_time == 0:
                return True
            elapsed = time.time() - self._last_mode_switch_time
            return elapsed >= self._min_mode_stay_time
    
    # ==================== 展示队列管理 ====================
    
    def add_to_display_queue(self, message_id: str, priority_score: float,
                             content: str = "", agent_id: str = ""):
        """添加消息到展示队列"""
        with self._lock:
            entry = {
                "message_id": message_id,
                "priority_score": priority_score,
                "content": content,
                "agent_id": agent_id,
                "is_displayed": False,
                "display_time": None,
                "queued_at": time.time(),
            }
            # 按优先级插入（降序）
            inserted = False
            for i, item in enumerate(self._display_queue):
                if priority_score > item["priority_score"]:
                    self._display_queue.insert(i, entry)
                    inserted = True
                    break
            if not inserted:
                self._display_queue.append(entry)
            self._version += 1
    
    def get_display_queue(self, include_displayed: bool = False) -> List[Dict]:
        """获取展示队列"""
        with self._lock:
            if include_displayed:
                return [e.copy() for e in self._display_queue]
            return [e.copy() for e in self._display_queue if not e["is_displayed"]]
    
    def get_top_display_items(self, count: int = None) -> List[Dict]:
        """获取优先级最高的展示项"""
        with self._lock:
            count = count or self._display_window_size
            undisplayed = [e for e in self._display_queue if not e["is_displayed"]]
            return [e.copy() for e in undisplayed[:count]]
    
    def mark_displayed(self, message_id: str):
        """标记消息已展示"""
        with self._lock:
            for entry in self._display_queue:
                if entry["message_id"] == message_id:
                    entry["is_displayed"] = True
                    entry["display_time"] = time.time()
                    self._version += 1
                    return True
            return False
    
    def clear_display_queue(self):
        """清空展示队列"""
        with self._lock:
            self._display_queue.clear()
            self._version += 1
    
    # ==================== 防卡死机制 ====================
    
    def reset_activity_timer(self):
        """重置活动计时器"""
        with self._lock:
            self._last_activity_time = time.time()
    
    def check_idle_timeout(self) -> bool:
        """检查是否空闲超时"""
        with self._lock:
            elapsed = time.time() - self._last_activity_time
            return elapsed >= self._idle_timeout
    
    def get_idle_time(self) -> float:
        """获取空闲时间"""
        with self._lock:
            return time.time() - self._last_activity_time
    
    def increment_round(self):
        """增加有效轮次"""
        with self._lock:
            self._effective_rounds += 1
            self._last_activity_time = time.time()
            self._version += 1
    
    def get_round_count(self) -> int:
        """获取有效轮次"""
        with self._lock:
            return self._effective_rounds
    
    def is_round_limit_reached(self) -> bool:
        """检查是否达到轮次上限"""
        with self._lock:
            return self._effective_rounds >= self._max_rounds
    
    def set_idle_timeout(self, seconds: float):
        """设置空闲超时阈值"""
        with self._lock:
            self._idle_timeout = max(10.0, seconds)
    
    def set_max_rounds(self, max_rounds: int):
        """设置轮次上限"""
        with self._lock:
            self._max_rounds = max(10, max_rounds)
    
    # ==================== 观点重复检测 ====================
    
    def check_duplicate_content(self, content: str, agent_id: str, 
                                 similarity_threshold: float = 0.85) -> Dict:
        """检查内容是否与最近消息重复"""
        with self._lock:
            # 获取最近20条消息
            recent_messages = self._messages[-20:] if len(self._messages) >= 20 else self._messages
            
            for msg in recent_messages:
                # 跳过同一代理的消息
                if msg.agent_id == agent_id:
                    continue
                
                # 简单的文本相似度检测（Jaccard相似度）
                similarity = self._calculate_similarity(content, msg.content)
                if similarity > similarity_threshold:
                    return {
                        "is_duplicate": True,
                        "similarity": similarity,
                        "similar_to": msg.agent_id,
                        "similar_content": msg.content[:100],
                    }
            
            return {"is_duplicate": False, "similarity": 0.0}
    
    def _calculate_similarity(self, text1: str, text2: str) -> float:
        """计算文本相似度（Jaccard）"""
        # 简单的词集合Jaccard相似度
        words1 = set(text1.lower().split())
        words2 = set(text2.lower().split())
        
        if not words1 or not words2:
            return 0.0
        
        intersection = words1 & words2
        union = words1 | words2
        
        return len(intersection) / len(union) if union else 0.0
    
    def record_duplicate(self, agent_id: str):
        """记录代理重复发言"""
        with self._lock:
            if agent_id not in self._agent_states:
                self.init_agent_state(agent_id)
            
            state = self._agent_states[agent_id]
            state["consecutive_duplicates"] = state.get("consecutive_duplicates", 0) + 1
            
            # 连续3次重复，增加额外休眠时间
            if state["consecutive_duplicates"] >= 3:
                state["extra_sleep"] = state.get("extra_sleep", 0) + 1.0
                state["consecutive_duplicates"] = 0  # 重置计数
            
            self._version += 1
    
    def reset_duplicate_count(self, agent_id: str):
        """重置重复计数（有价值的发言后）"""
        with self._lock:
            if agent_id in self._agent_states:
                self._agent_states[agent_id]["consecutive_duplicates"] = 0
                self._agent_states[agent_id]["extra_sleep"] = 0
    
    # ==================== 全局重复检测（大规模重复触发议程跳过） ====================
    
    def check_massive_repetition(self, window_size: int = 10,
                                   duplicate_threshold: float = 0.85,
                                   agent_ratio_threshold: float = 0.5) -> Dict:
        """
        检测大规模重复情况
        
        Args:
            window_size: 检测窗口大小（最近N条消息）
            duplicate_threshold: 重复相似度阈值
            agent_ratio_threshold: 重复代理比例阈值（默认50%）
            
        Returns:
            {
                "massive_repetition": bool,  # 是否发生大规模重复
                "duplicate_agents": list,    # 重复的代理列表
                "duplicate_ratio": float,    # 重复比例
                "total_agents": int,         # 参与发言的代理总数
                "should_skip_agenda": bool   # 是否应该跳过当前议程
            }
        """
        with self._lock:
            # 获取窗口内的消息
            recent_messages = self._messages[-window_size:] if len(self._messages) >= window_size else self._messages
            
            if len(recent_messages) < window_size:
                return {
                    "massive_repetition": False,
                    "duplicate_agents": [],
                    "duplicate_ratio": 0.0,
                    "total_agents": 0,
                    "should_skip_agenda": False,
                    "reason": "not_enough_messages"
                }
            
            # 统计每个代理的消息
            agent_messages: Dict[str, List[str]] = {}
            for msg in recent_messages:
                if msg.agent_id not in agent_messages:
                    agent_messages[msg.agent_id] = []
                agent_messages[msg.agent_id].append(msg.content)
            
            # 找出参与发言的代理
            speaking_agents = set(agent_messages.keys())
            total_agents = len(speaking_agents)
            
            if total_agents < 2:
                return {
                    "massive_repetition": False,
                    "duplicate_agents": [],
                    "duplicate_ratio": 0.0,
                    "total_agents": total_agents,
                    "should_skip_agenda": False,
                    "reason": "not_enough_agents"
                }
            
            # 检测重复的代理
            duplicate_agents = set()
            all_messages = [(msg.agent_id, msg.content) for msg in recent_messages]
            
            for i, (agent_id1, content1) in enumerate(all_messages):
                for j, (agent_id2, content2) in enumerate(all_messages):
                    if i >= j or agent_id1 == agent_id2:
                        continue
                    
                    similarity = self._calculate_similarity(content1, content2)
                    if similarity > duplicate_threshold:
                        duplicate_agents.add(agent_id1)
                        duplicate_agents.add(agent_id2)
            
            duplicate_count = len(duplicate_agents)
            duplicate_ratio = duplicate_count / total_agents if total_agents > 0 else 0.0
            
            # 判断是否应该跳过议程
            should_skip = (
                len(recent_messages) >= window_size and
                duplicate_count >= max(2, int(total_agents * agent_ratio_threshold))
            )
            
            return {
                "massive_repetition": should_skip,
                "duplicate_agents": list(duplicate_agents),
                "duplicate_ratio": duplicate_ratio,
                "total_agents": total_agents,
                "should_skip_agenda": should_skip,
                "duplicate_count": duplicate_count,
                "window_size": len(recent_messages)
            }
    
    def get_global_duplicate_stats(self) -> Dict:
        """获取全局重复统计"""
        with self._lock:
            stats = {
                "agent_duplicate_counts": {},
                "total_duplicates": 0,
                "agents_with_duplicates": 0
            }
            
            for agent_id, state in self._agent_states.items():
                dup_count = state.get("consecutive_duplicates", 0)
                if dup_count > 0:
                    stats["agent_duplicate_counts"][agent_id] = dup_count
                    stats["agents_with_duplicates"] += 1
                    stats["total_duplicates"] += dup_count
            
            return stats
    
    def reset_all_duplicate_counts(self):
        """重置所有代理的重复计数"""
        with self._lock:
            for agent_id in self._agent_states:
                self._agent_states[agent_id]["consecutive_duplicates"] = 0
                self._agent_states[agent_id]["extra_sleep"] = 0
            self._version += 1
    
    def extract_unique_viewpoints(self, window_size: int = 10,
                                    similarity_threshold: float = 0.7) -> List[Dict]:
        """
        从最近消息中提取去重后的观点/方案
        
        Args:
            window_size: 检测窗口大小
            similarity_threshold: 观点相似度阈值（低于此值视为不同观点）
            
        Returns:
            去重后的观点列表 [{content, agent_id, supporters: [agent_ids]}]
        """
        with self._lock:
            recent_messages = self._messages[-window_size:] if len(self._messages) >= window_size else self._messages
            
            if not recent_messages:
                return []
            
            # 过滤系统消息
            agent_messages = [
                (msg.agent_id, msg.content)
                for msg in recent_messages
                if msg.agent_id != "system" and msg.message_type != "system"
            ]
            
            if not agent_messages:
                return []
            
            # 聚类相似观点
            viewpoints = []
            
            for agent_id, content in agent_messages:
                # 检查是否与已有观点相似
                found_similar = False
                for vp in viewpoints:
                    similarity = self._calculate_similarity(content, vp["content"])
                    if similarity > similarity_threshold:
                        # 相似，加入支持者
                        if agent_id not in vp["supporters"]:
                            vp["supporters"].append(agent_id)
                        found_similar = True
                        break
                
                if not found_similar:
                    # 新观点
                    viewpoints.append({
                        "content": content,
                        "agent_id": agent_id,  # 原始提出者
                        "supporters": [agent_id]
                    })
            
            # 按支持者数量排序
            viewpoints.sort(key=lambda x: len(x["supporters"]), reverse=True)
            
            return viewpoints
    
    def trigger_voting_by_repetition(self, 
                                       duplicate_count: int,
                                       total_agents: int,
                                       window_size: int = 10) -> Dict:
        """
        因大规模重复触发投票选出最优方案
        
        Returns:
            {
                "triggered": bool,
                "viewpoints": list,      # 去重后的观点列表
                "voting_session": dict,  # 投票会话信息
                "backup_viewpoints": list # 备选方案
            }
        """
        with self._lock:
            # 提取去重后的观点
            viewpoints = self.extract_unique_viewpoints(window_size)
            
            if len(viewpoints) < 2:
                # 只有一个观点，无需投票
                return {
                    "triggered": False,
                    "reason": "only_one_viewpoint",
                    "viewpoints": viewpoints,
                    "voting_session": None,
                    "backup_viewpoints": []
                }
            
            # 记录到白板
            self.add_message(
                agent_id="system",
                content=f"[系统] 检测到大规模重复发言（{duplicate_count}/{total_agents}个代理重复），触发投票选出最优方案",
                message_type="system"
            )
            
            # 创建投票会话
            session_id = f"repetition_vote_{int(time.time())}"
            
            # 构建投票选项
            vote_options = []
            for i, vp in enumerate(viewpoints[:5], 1):  # 最多5个选项
                vote_options.append({
                    "id": i,
                    "summary": vp["content"][:200] + ("..." if len(vp["content"]) > 200 else ""),
                    "proposer": vp["agent_id"],
                    "supporters": vp["supporters"]
                })
            
            # 创建投票会话
            vote_session = {
                "id": session_id,
                "type": "repetition_resolution",
                "options": vote_options,
                "created_at": time.time(),
                "status": "active",
                "votes": {},
                "winning_option": None,
                "backup_options": []  # 未中选的方案
            }
            
            # 设置当前投票会话
            self._current_vote_session = vote_session
            self._voting_mode = True
            
            # 重置重复计数
            self.reset_all_duplicate_counts()
            
            return {
                "triggered": True,
                "reason": "massive_repetition",
                "viewpoints": viewpoints,
                "voting_session": vote_session,
                "vote_options": vote_options,
                "backup_viewpoints": viewpoints[5:] if len(viewpoints) > 5 else []  # 超过5个的作为备选
            }
    
    def submit_repetition_vote(self, agent_id: str, option_id: int, 
                                reason: str = "") -> Dict:
        """
        提交重复投票
        
        Args:
            agent_id: 投票代理ID
            option_id: 选择的选项ID
            reason: 理由
            
        Returns:
            投票结果
        """
        with self._lock:
            if not self._current_vote_session:
                return {"success": False, "error": "no_active_voting"}
            
            session = self._current_vote_session
            
            if session.get("type") != "repetition_resolution":
                return {"success": False, "error": "wrong_voting_type"}
            
            # 记录投票
            session["votes"][agent_id] = {
                "option_id": option_id,
                "reason": reason,
                "timestamp": time.time()
            }
            
            self._version += 1
            
            return {"success": True, "vote_recorded": True}
    
    def finalize_repetition_vote(self) -> Dict:
        """
        结束重复投票，选出最优方案，其他进入备选
        
        Returns:
            {
                "winning_option": dict,
                "backup_options": list,
                "vote_summary": dict
            }
        """
        with self._lock:
            if not self._current_vote_session:
                return {"success": False, "error": "no_active_voting"}
            
            session = self._current_vote_session
            
            # 统计票数
            vote_counts = {}
            for vote in session["votes"].values():
                opt_id = vote["option_id"]
                vote_counts[opt_id] = vote_counts.get(opt_id, 0) + 1
            
            # 找出获胜选项
            winning_id = None
            max_votes = 0
            for opt_id, count in vote_counts.items():
                if count > max_votes:
                    max_votes = count
                    winning_id = opt_id
            
            # 获取获胜选项详情
            winning_option = None
            backup_options = []
            
            for opt in session["options"]:
                if opt["id"] == winning_id:
                    winning_option = opt
                else:
                    backup_options.append(opt)
            
            # 更新会话状态
            session["winning_option"] = winning_option
            session["backup_options"] = backup_options
            session["status"] = "completed"
            
            # 清除投票模式
            self._voting_mode = False
            
            # 保存到历史
            self._vote_sessions.append(session)
            self._current_vote_session = None
            
            # 记录结果到白板
            if winning_option:
                self.add_message(
                    agent_id="system",
                    content=f"[系统] 投票结果：最优方案由 {winning_option['proposer']} 提出，获得 {max_votes} 票。\n"
                            f"方案摘要：{winning_option['summary'][:100]}...\n"
                            f"备选方案：{len(backup_options)} 个已存入备选库。",
                    message_type="system"
                )
                
                # 将备选方案存入待决问题
                for i, opt in enumerate(backup_options):
                    self.add_pending_issue(
                        content=f"备选方案{i+1}: {opt['summary'][:100]}",
                        reason=f"投票未中选，得票{vote_counts.get(opt['id'], 0)}票",
                        proposer=opt['proposer']
                    )
            
            self._version += 1
            
            return {
                "success": True,
                "winning_option": winning_option,
                "backup_options": backup_options,
                "vote_summary": {
                    "total_votes": len(session["votes"]),
                    "vote_counts": vote_counts,
                    "winning_votes": max_votes
                }
            }
    
    def get_repetition_vote_status(self) -> Optional[Dict]:
        """获取当前重复投票状态"""
        with self._lock:
            if not self._current_vote_session:
                return None
            
            session = self._current_vote_session
            if session.get("type") != "repetition_resolution":
                return None
            
            return {
                "session_id": session["id"],
                "status": session["status"],
                "options": session["options"],
                "votes_count": len(session["votes"]),
                "created_at": session["created_at"]
            }
    
    # ==================== 思考暂停机制 ====================
    
    def request_think_pause(self, agent_id: str, duration: int,
                            per_agent_limit: int = 2,
                            per_agent_window: float = 600.0,
                            global_limit: int = 5,
                            global_window: float = 300.0,
                            min_interval: float = 60.0) -> Dict:
        """请求思考暂停（带频率限制）"""
        with self._lock:
            now = time.time()
            
            # 检查是否已有思考暂停在进行
            if self._think_pause["active"]:
                # 加入队列
                self._think_pause["queue"].append({
                    "agent_id": agent_id,
                    "duration": duration,
                    "requested_at": now,
                })
                return {"approved": False, "reason": "already_in_progress", "queued": True}
            
            # 检查单个代理频率限制
            if agent_id not in self._think_history:
                self._think_history[agent_id] = []
            
            # 清理过期记录
            self._think_history[agent_id] = [
                t for t in self._think_history[agent_id]
                if now - t < per_agent_window
            ]
            
            if len(self._think_history[agent_id]) >= per_agent_limit:
                return {"approved": False, "reason": "per_agent_limit_exceeded"}
            
            # 检查全局频率限制
            self._global_think_timestamps = [
                t for t in self._global_think_timestamps
                if now - t < global_window
            ]
            
            if len(self._global_think_timestamps) >= global_limit:
                return {"approved": False, "reason": "global_limit_exceeded"}
            
            # 检查最小间隔
            if self._think_history[agent_id]:
                last_think = max(self._think_history[agent_id])
                if now - last_think < min_interval:
                    wait_time = min_interval - (now - last_think)
                    return {"approved": False, "reason": "min_interval_not_reached", "wait": wait_time}
            
            # 批准思考暂停
            self._think_pause["active"] = True
            self._think_pause["agent_id"] = agent_id
            self._think_pause["start_time"] = now
            self._think_pause["duration"] = duration
            
            # 记录历史
            self._think_history[agent_id].append(now)
            self._global_think_timestamps.append(now)
            
            # 设置思考模式标志
            self._think_mode = True
            
            self._version += 1
            
            return {
                "approved": True,
                "duration": duration,
                "end_time": now + duration,
            }
    
    def end_think_pause(self) -> Dict:
        """结束思考暂停，授予优先发言权"""
        with self._lock:
            if not self._think_pause["active"]:
                return {"ended": False, "reason": "not_active"}
            
            agent_id = self._think_pause["agent_id"]
            duration = self._think_pause["duration"]
            
            # 授予优先发言权（持续时间为思考时间的2倍）
            priority_duration = duration * 2
            self._think_priority[agent_id] = time.time() + priority_duration
            
            # 清除思考暂停状态
            self._think_pause["active"] = False
            self._think_pause["agent_id"] = None
            self._think_pause["start_time"] = None
            self._think_pause["duration"] = 0
            
            # 清除思考模式标志
            self._think_mode = False
            
            # 处理队列中的下一个请求
            next_request = None
            if self._think_pause["queue"]:
                next_request = self._think_pause["queue"].pop(0)
            
            self._version += 1
            
            return {
                "ended": True,
                "agent_id": agent_id,
                "priority_expiry": self._think_priority[agent_id],
                "next_request": next_request,
            }
    
    def get_think_pause_status(self) -> Dict:
        """获取思考暂停状态"""
        with self._lock:
            status = self._think_pause.copy()
            status["time_remaining"] = 0.0
            
            if status["active"] and status["start_time"]:
                elapsed = time.time() - status["start_time"]
                status["time_remaining"] = max(0, status["duration"] - elapsed)
            
            return status
    
    def has_think_priority(self, agent_id: str) -> bool:
        """检查代理是否有思考优先发言权"""
        with self._lock:
            if agent_id not in self._think_priority:
                return False
            return time.time() < self._think_priority[agent_id]
    
    def get_think_priority_expiry(self, agent_id: str) -> Optional[float]:
        """获取思考优先发言权过期时间"""
        with self._lock:
            return self._think_priority.get(agent_id)
    
    def add_think_log(self, agent_id: str, content: str, log_type: str = "thought"):
        """添加私有思考日志"""
        with self._lock:
            entry = {
                "agent_id": agent_id,
                "content": content,
                "type": log_type,  # thought, tool_call, memory_access
                "timestamp": time.time(),
            }
            self._think_private_log.append(entry)
            self._version += 1
    
    def get_think_logs(self, agent_id: Optional[str] = None) -> List[Dict]:
        """获取思考日志（用户可查看所有，代理只能查看自己的）"""
        with self._lock:
            if agent_id:
                return [e.copy() for e in self._think_private_log if e["agent_id"] == agent_id]
            return [e.copy() for e in self._think_private_log]
    
    def add_pending_interrupt(self, agent_id: str, content: str):
        """添加思考期间的叫停请求"""
        with self._lock:
            self._pending_interrupts.append({
                "agent_id": agent_id,
                "content": content,
                "timestamp": time.time(),
            })
    
    def get_pending_interrupts(self, clear: bool = False) -> List[Dict]:
        """获取并可选清除累积的叫停请求"""
        with self._lock:
            interrupts = [i.copy() for i in self._pending_interrupts]
            if clear:
                self._pending_interrupts.clear()
            return interrupts
