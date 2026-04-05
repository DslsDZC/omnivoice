"""模式基类"""
import asyncio
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional
from datetime import datetime

from agent import AgentPool
from whiteboard import Whiteboard
from workspace import WorkspaceManager
from tools.base import ToolRouter, PluginManager
from config_loader import GlobalConfig


@dataclass
class ModeResult:
    """模式执行结果"""
    success: bool
    final_resolution: str
    messages: List[Dict] = field(default_factory=list)
    tool_results: List[Dict] = field(default_factory=list)
    workspace_files: List[str] = field(default_factory=list)
    stats: Dict = field(default_factory=dict)
    metrics: Dict = field(default_factory=dict)
    intermediate_results: Dict = field(default_factory=dict)
    proposals: List[Dict] = field(default_factory=list)
    steps: List[str] = field(default_factory=list)
    agenda_conclusions: List[Dict] = field(default_factory=list)
    error: Optional[str] = None


class BaseMode(ABC):
    """模式基类"""
    
    mode_name: str = "base"
    
    def __init__(self, agent_pool: AgentPool, 
                 whiteboard: Whiteboard,
                 workspace: WorkspaceManager,
                 tool_router: ToolRouter,
                 config: GlobalConfig):
        self.agent_pool = agent_pool
        self.whiteboard = whiteboard
        self.workspace = workspace
        self.tool_router = tool_router
        self.config = config
        
        # 运行时状态
        self._is_running = False
        self._should_stop = False
        self._start_time: Optional[float] = None
    
    @abstractmethod
    async def execute(self, question: str) -> ModeResult:
        """执行模式"""
        pass
    
    def stop(self):
        """停止执行"""
        self._should_stop = True
    
    @property
    def is_running(self) -> bool:
        return self._is_running
    
    def get_elapsed_time(self) -> float:
        """获取已运行时间"""
        if self._start_time is None:
            return 0.0
        import time
        return time.time() - self._start_time
    
    def _format_context_for_agent(self, agent_id: str) -> str:
        """为代理格式化上下文"""
        return self.whiteboard.get_context_for_agent(agent_id)
    
    def _get_tool_schemas_for_agent(self, agent: Any, 
                                        query: str = None) -> List[Dict]:
        """获取代理可用的工具schema
        
        Args:
            agent: 代理对象
            query: 当前查询（用于智能工具选择）
        """
        # 如果有查询，使用智能工具选择
        if query and hasattr(self.tool_router, 'get_common_tools_for_agent'):
            # 先获取常用工具
            common = self.tool_router.get_common_tools_for_agent(agent.allowed_tools)
            
            # 如果有更多工具权限，按查询匹配
            if len(agent.allowed_tools) > len(PluginManager.COMMON_TOOLS):
                # 按关键字搜索
                for keyword in query.split()[:3]:  # 取前3个关键词
                    matched = self.tool_router.search_tools(keyword, limit=2)
                    for m in matched:
                        tool_name = m["name"]
                        if tool_name in agent.allowed_tools:
                            tool = self.tool_router.plugin_manager.get_tool(tool_name)
                            if tool:
                                common.append(tool.get_openai_tool_schema())
            
            return common
        
        # 默认：返回所有可用工具
        return self.tool_router.get_available_tools_for_agent(
            agent.allowed_tools
        )
    
    async def _call_agent_with_tools(self, agent: Any, 
                                      messages: List[Dict],
                                      max_iterations: int = 5) -> tuple:
        """调用代理并处理工具调用循环"""
        all_tool_results = []
        
        for iteration in range(max_iterations):
            tools = self._get_tool_schemas_for_agent(agent)
            
            response = await agent.call_api(
                messages=messages,
                tools=tools if tools else None,
                tool_choice="auto" if tools else "none"
            )
            
            if not response.success:
                return response, all_tool_results
            
            # 如果没有工具调用，返回结果
            if not response.tool_calls:
                return response, all_tool_results
            
            # 执行工具调用
            tool_results = await agent.execute_tool_calls(
                response.tool_calls, 
                self.whiteboard
            )
            all_tool_results.extend(tool_results)
            
            # 将工具结果添加到消息中
            messages.append({
                "role": "assistant",
                "content": response.content,
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["arguments"])
                        }
                    }
                    for tc in response.tool_calls
                ]
            })
            
            for tr in tool_results:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tr["tool_call_id"],
                    "name": tr["name"],
                    "content": str(tr["result"])
                })
        
        # 达到最大迭代次数
        return response, all_tool_results
    
    def _build_result(self) -> ModeResult:
        """构建结果对象"""
        messages = [
            {
                "agent_id": m.agent_id,
                "content": m.content,
                "timestamp": m.timestamp,
                "type": m.message_type
            }
            for m in self.whiteboard.get_messages()
        ]
        
        tool_results = [
            {
                "caller": tr.caller,
                "tool": tr.tool,
                "args": tr.args,
                "result": tr.result,
                "success": tr.success
            }
            for tr in self.whiteboard.get_tool_results()
        ]
        
        workspace_files = list(self.whiteboard.get_workspace_files().keys())
        
        import time
        stats = {
            "mode": self.mode_name,
            "elapsed_time": self.get_elapsed_time(),
            "message_count": len(messages),
            "tool_call_count": len(tool_results),
            "workspace_file_count": len(workspace_files)
        }
        
        return ModeResult(
            success=True,
            final_resolution=self.whiteboard.get_final_resolution() or "",
            messages=messages,
            tool_results=tool_results,
            workspace_files=workspace_files,
            stats=stats
        )
