"""插件系统核心"""
import os
import json
import importlib.util
import asyncio
import inspect
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional, Callable, Type
from pathlib import Path
from datetime import datetime
import hashlib


@dataclass
class PluginMetadata:
    """插件元数据"""
    name: str
    version: str = "1.0.0"
    description: str = ""
    author: str = ""
    security_level: str = "medium"  # high, medium, low
    requires_auth: bool = False
    dependencies: List[str] = field(default_factory=list)
    enabled: bool = True
    priority: int = 100  # 加载优先级，数字越小越先加载
    tags: List[str] = field(default_factory=list)


@dataclass
class ToolResult:
    """工具执行结果"""
    success: bool
    result: Any
    error: Optional[str] = None
    metadata: Dict = field(default_factory=dict)
    
    def to_dict(self) -> Dict:
        return {
            "success": self.success,
            "result": self.result,
            "error": self.error,
            "metadata": self.metadata
        }
    
    def __str__(self) -> str:
        if self.success:
            return str(self.result)
        return f"错误: {self.error}"


class BaseTool(ABC):
    """工具基类"""
    
    name: str = "base_tool"
    description: str = "基础工具"
    parameters_schema: Dict = {}
    security_level: str = "medium"
    metadata: Optional[PluginMetadata] = None
    
    @abstractmethod
    async def execute(self, args: Dict[str, Any], context: Dict[str, Any]) -> ToolResult:
        """执行工具"""
        pass
    
    def get_openai_tool_schema(self) -> Dict:
        """获取OpenAI工具调用格式的schema"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema
            }
        }
    
    def validate_args(self, args: Dict[str, Any]) -> tuple:
        """验证参数，返回 (is_valid, error_message)"""
        if not self.parameters_schema:
            return True, None
        
        required = self.parameters_schema.get("required", [])
        properties = self.parameters_schema.get("properties", {})
        
        # 检查必需参数
        for req in required:
            if req not in args:
                return False, f"缺少必需参数: {req}"
        
        # 类型检查
        for key, value in args.items():
            if key not in properties:
                continue
            
            prop_type = properties[key].get("type")
            type_valid = self._check_type(value, prop_type)
            if not type_valid:
                return False, f"参数 '{key}' 类型错误，期望 {prop_type}"
        
        return True, None
    
    def _check_type(self, value: Any, expected_type: str) -> bool:
        """检查值类型"""
        if expected_type == "string":
            return isinstance(value, str)
        elif expected_type == "number":
            return isinstance(value, (int, float))
        elif expected_type == "integer":
            return isinstance(value, int)
        elif expected_type == "boolean":
            return isinstance(value, bool)
        elif expected_type == "array":
            return isinstance(value, list)
        elif expected_type == "object":
            return isinstance(value, dict)
        return True
    
    def get_info(self) -> Dict:
        """获取工具信息"""
        return {
            "name": self.name,
            "description": self.description,
            "security_level": self.security_level,
            "parameters": self.parameters_schema,
            "metadata": {
                "version": self.metadata.version if self.metadata else "1.0.0",
                "author": self.metadata.author if self.metadata else "",
            }
        }


class BasePlugin(ABC):
    """插件基类"""
    
    # 插件信息
    plugin_name: str = "base_plugin"
    plugin_version: str = "1.0.0"
    plugin_description: str = "基础插件"
    plugin_author: str = ""
    plugin_security_level: str = "medium"
    plugin_tags: List[str] = []
    
    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}
        self._metadata: Optional[PluginMetadata] = None
    
    @property
    def metadata(self) -> PluginMetadata:
        if self._metadata is None:
            self._metadata = PluginMetadata(
                name=self.plugin_name,
                version=self.plugin_version,
                description=self.plugin_description,
                author=self.plugin_author,
                security_level=self.plugin_security_level,
                tags=self.plugin_tags
            )
        return self._metadata
    
    def register_tool(self, tool: BaseTool):
        """注册工具"""
        tool.metadata = self.metadata
        self._tools[tool.name] = tool
    
    def get_tools(self) -> Dict[str, BaseTool]:
        """获取所有工具"""
        return self._tools
    
    def initialize(self, config: Dict[str, Any] = None):
        """初始化插件（子类可重写）"""
        pass
    
    def shutdown(self):
        """关闭插件（子类可重写）"""
        pass


class PluginLoader:
    """插件加载器"""
    
    def __init__(self, plugin_dirs: List[str] = None):
        self.plugin_dirs = plugin_dirs or ["plugins"]
        self._loaded_plugins: Dict[str, BasePlugin] = {}
        self._load_errors: Dict[str, str] = {}
    
    def discover_plugins(self) -> List[Dict]:
        """发现所有可用插件"""
        discovered = []
        
        for plugin_dir in self.plugin_dirs:
            plugin_path = Path(plugin_dir)
            if not plugin_path.exists():
                continue
            
            # 扫描子目录
            for category_dir in plugin_path.iterdir():
                if not category_dir.is_dir():
                    continue
                
                # 扫描插件文件
                for item in category_dir.iterdir():
                    if item.is_file() and item.suffix == '.py' and not item.name.startswith('_'):
                        plugin_info = {
                            "path": str(item),
                            "category": category_dir.name,
                            "name": item.stem
                        }
                        
                        # 检查是否有配置文件
                        config_path = item.parent / f"{item.stem}_config.json"
                        if config_path.exists():
                            plugin_info["config_path"] = str(config_path)
                        
                        discovered.append(plugin_info)
        
        return discovered
    
    def load_plugin(self, plugin_path: str, config: Dict = None) -> Optional[BasePlugin]:
        """加载单个插件"""
        try:
            # 动态导入模块
            spec = importlib.util.spec_from_file_location("plugin_module", plugin_path)
            if not spec or not spec.loader:
                return None
            
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            
            # 查找插件类
            plugin_class = None
            for name, obj in inspect.getmembers(module):
                if inspect.isclass(obj) and issubclass(obj, BasePlugin) and obj is not BasePlugin:
                    plugin_class = obj
                    break
            
            if not plugin_class:
                self._load_errors[plugin_path] = "未找到插件类"
                return None
            
            # 实例化并初始化
            plugin = plugin_class()
            plugin.initialize(config or {})
            
            # 记录
            self._loaded_plugins[plugin.plugin_name] = plugin
            
            return plugin
            
        except Exception as e:
            self._load_errors[plugin_path] = str(e)
            return None
    
    def load_all(self, config: Dict[str, Dict] = None) -> Dict[str, BasePlugin]:
        """加载所有插件"""
        config = config or {}
        discovered = self.discover_plugins()
        
        # 按优先级排序
        # 注意：这里需要先加载元数据才能排序，简化处理
        
        for plugin_info in discovered:
            plugin_name = plugin_info["name"]
            plugin_config = config.get(plugin_name, {})
            self.load_plugin(plugin_info["path"], plugin_config)
        
        return self._loaded_plugins
    
    def get_plugin(self, name: str) -> Optional[BasePlugin]:
        """获取插件"""
        return self._loaded_plugins.get(name)
    
    def get_all_tools(self) -> Dict[str, BaseTool]:
        """获取所有已加载的工具"""
        tools = {}
        for plugin in self._loaded_plugins.values():
            tools.update(plugin.get_tools())
        return tools
    
    def get_load_errors(self) -> Dict[str, str]:
        """获取加载错误"""
        return self._load_errors.copy()


class PluginManager:
    """插件管理器"""
    
    # 常用工具列表（优先展示）
    COMMON_TOOLS = [
        "calculator",
        "current_time",
        "temp_file_read",
        "temp_file_write", 
        "temp_file_delete",
        "temp_list_files",
        "code_execute"
    ]
    
    def __init__(self, plugin_dirs: List[str] = None):
        self.loader = PluginLoader(plugin_dirs)
        self._tools: Dict[str, BaseTool] = {}
        self._network_tools_enabled = False
        self._initialized = False
        self._tool_keywords: Dict[str, List[str]] = {}  # 工具关键词索引
    
    def initialize(self, config: Dict[str, Dict] = None):
        """初始化插件系统"""
        if self._initialized:
            return
        
        # 加载所有插件
        self.loader.load_all(config)
        
        # 收集所有工具
        self._tools = self.loader.get_all_tools()
        
        # 构建关键词索引
        self._build_keyword_index()
        
        self._initialized = True
    
    def _build_keyword_index(self):
        """构建工具关键词索引"""
        for name, tool in self._tools.items():
            keywords = [name]
            
            # 从描述提取关键词
            desc = tool.description.lower()
            keywords.extend(desc.split())
            
            # 从参数名提取
            props = tool.parameters_schema.get("properties", {})
            keywords.extend(props.keys())
            
            # 从标签提取
            if tool.metadata and tool.metadata.tags:
                keywords.extend(tool.metadata.tags)
            
            self._tool_keywords[name] = [k.lower() for k in keywords if len(k) > 1]
    
    def search_tools_by_keyword(self, keyword: str, limit: int = 5) -> List[Dict]:
        """按关键词搜索工具"""
        keyword = keyword.lower()
        results = []
        
        for name, keywords in self._tool_keywords.items():
            # 计算匹配度
            score = 0
            for kw in keywords:
                if keyword in kw:
                    score += 1
                    if kw == keyword:
                        score += 2  # 精确匹配加分
            
            if score > 0:
                tool = self._tools.get(name)
                results.append({
                    "name": name,
                    "description": tool.description if tool else "",
                    "score": score
                })
        
        # 按分数排序
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]
    
    def get_common_tools_schema(self, allowed_tools: List[str] = None) -> List[Dict]:
        """获取常用工具schema"""
        schemas = []
        for name in self.COMMON_TOOLS:
            if name not in self._tools:
                continue
            if allowed_tools and name not in allowed_tools:
                continue
            
            tool = self._tools[name]
            if tool.security_level == "low" and not self._network_tools_enabled:
                continue
            
            schemas.append(tool.get_openai_tool_schema())
        
        return schemas
    
    def get_tools_for_query(self, query: str, allowed_tools: List[str] = None) -> List[Dict]:
        """根据查询智能选择工具"""
        # 1. 先返回常用工具
        common = self.get_common_tools_schema(allowed_tools)
        
        # 2. 按关键词搜索匹配的工具
        matched = self.search_tools_by_keyword(query)
        
        # 3. 添加匹配但不常用的工具
        for m in matched:
            name = m["name"]
            if name in self.COMMON_TOOLS:
                continue  # 已包含在常用中
            
            if allowed_tools and name not in allowed_tools:
                continue
            
            tool = self._tools.get(name)
            if tool:
                if tool.security_level == "low" and not self._network_tools_enabled:
                    continue
                common.append(tool.get_openai_tool_schema())
        
        return common
    
    def register_builtin_tool(self, tool: BaseTool):
        """手动注册内置工具"""
        self._tools[tool.name] = tool
        # 更新关键词索引
        self._build_keyword_index()
    
    def get_tool(self, name: str) -> Optional[BaseTool]:
        """获取工具"""
        return self._tools.get(name)
    
    def list_tools(self) -> List[str]:
        """列出所有工具名称"""
        return list(self._tools.keys())
    
    def get_tools_by_security_level(self, level: str) -> List[BaseTool]:
        """按安全级别获取工具"""
        return [t for t in self._tools.values() if t.security_level == level]
    
    def get_all_tools_info(self) -> List[Dict]:
        """获取所有工具信息"""
        return [t.get_info() for t in self._tools.values()]
    
    def set_network_tools_enabled(self, enabled: bool):
        """设置是否启用网络工具"""
        self._network_tools_enabled = enabled
    
    def is_tool_available(self, tool_name: str, agent_tools: List[str] = None) -> tuple:
        """检查工具是否可用，返回 (available, reason)"""
        tool = self._tools.get(tool_name)
        if not tool:
            return False, f"工具 '{tool_name}' 未注册"
        
        # 检查安全级别
        if tool.security_level == "low" and not self._network_tools_enabled:
            return False, "网络工具未启用"
        
        # 检查代理权限
        if agent_tools and tool_name not in agent_tools:
            return False, f"代理无权使用工具 '{tool_name}'"
        
        return True, None
    
    def get_openai_tools_schema(self, allowed_tools: List[str] = None) -> List[Dict]:
        """获取OpenAI工具调用格式的schema列表"""
        schemas = []
        for name, tool in self._tools.items():
            # 检查权限
            if allowed_tools and name not in allowed_tools:
                continue
            # 检查网络工具
            if tool.security_level == "low" and not self._network_tools_enabled:
                continue
            schemas.append(tool.get_openai_tool_schema())
        return schemas
    
    async def execute(self, tool_name: str, args: Dict, 
                      context: Dict) -> ToolResult:
        """执行工具"""
        tool = self.get_tool(tool_name)
        if not tool:
            return ToolResult(
                success=False,
                result=None,
                error=f"工具 '{tool_name}' 未注册"
            )
        
        # 安全检查
        if tool.security_level == "low" and not self._network_tools_enabled:
            return ToolResult(
                success=False,
                result=None,
                error="网络工具未启用"
            )
        
        # 参数验证
        is_valid, error = tool.validate_args(args)
        if not is_valid:
            return ToolResult(
                success=False,
                result=None,
                error=error
            )
        
        try:
            # 记录执行开始
            start_time = datetime.now()
            
            # 执行工具
            result = await tool.execute(args, context)
            
            # 添加执行元数据
            result.metadata["tool_name"] = tool_name
            result.metadata["execution_time"] = (datetime.now() - start_time).total_seconds()
            
            return result
            
        except Exception as e:
            return ToolResult(
                success=False,
                result=None,
                error=f"工具执行异常: {str(e)}"
            )
    
    def shutdown(self):
        """关闭所有插件"""
        for plugin in self.loader._loaded_plugins.values():
            try:
                plugin.shutdown()
            except Exception:
                pass


class ToolRouter:
    """工具路由器"""
    
    def __init__(self, plugin_manager: PluginManager, workspace_manager=None, 
                 agent_pool=None, api_call_func=None):
        self.plugin_manager = plugin_manager
        self.workspace_manager = workspace_manager
        self.agent_pool = agent_pool
        self.api_call_func = api_call_func  # 模型调用函数
    
    def set_api_call_func(self, func: Callable):
        """设置模型调用函数"""
        self.api_call_func = func
    
    async def call_model(self, prompt: str, system_prompt: str = "", 
                          temperature: float = 0.3) -> Optional[str]:
        """调用模型（供工具使用）"""
        if not self.api_call_func:
            return None
        
        try:
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            
            result = await self.api_call_func(messages, temperature=temperature)
            return result
        except Exception as e:
            return None
    
    async def select_tools_intelligently(self, query: str, 
                                           allowed_tools: List[str] = None) -> List[Dict]:
        """智能选择工具
        
        1. 常用工具直接包含
        2. 不常用工具按关键字匹配
        3. 如果匹配多个，让模型选择最合适的
        """
        # 获取基于查询的工具
        tools = self.plugin_manager.get_tools_for_query(query, allowed_tools)
        
        # 如果工具数量合理，直接返回
        if len(tools) <= 5:
            return tools
        
        # 工具太多时，让模型选择
        tool_names = [t["function"]["name"] for t in tools]
        tool_descs = "\n".join([
            f"- {t['function']['name']}: {t['function']['description'][:50]}"
            for t in tools
        ])
        
        select_prompt = f"""选择适合以下任务的工具（最多3个）：

任务：{query}

可用工具：
{tool_descs}

输出JSON数组：["tool_name1", "tool_name2"]"""

        try:
            result = await self.call_model(select_prompt, temperature=0.1)
            if result:
                # 解析JSON
                import json
                import re
                match = re.search(r'\[.*?\]', result, re.DOTALL)
                if match:
                    selected = json.loads(match.group())
                    # 过滤并返回选中的工具
                    return [t for t in tools if t["function"]["name"] in selected]
        except:
            pass
        
        # 解析失败，返回前5个
        return tools[:5]
    
    async def execute(self, tool_name: str, args: Dict, 
                      agent_id: str, whiteboard) -> Any:
        """执行工具调用"""
        # 构建上下文
        context = {
            "agent_id": agent_id,
            "whiteboard": whiteboard,
            "workspace_manager": self.workspace_manager,
            "workspace_path": None,
            "call_model": self.call_model,  # 注入模型调用能力
            "tool_router": self  # 注入路由器引用
        }
        
        # 添加工作区路径
        if self.workspace_manager:
            context["workspace_path"] = self.workspace_manager.get_session_path()
        
        result = await self.plugin_manager.execute(tool_name, args, context)
        
        # 记录到白板
        if whiteboard:
            whiteboard.add_tool_result(
                caller=agent_id,
                tool=tool_name,
                args=args,
                result=result.result if result.success else result.error,
                success=result.success
            )
        
        if result.success:
            return result.result
        else:
            raise RuntimeError(result.error)
    
    def get_available_tools_for_agent(self, agent_allowed_tools: List[str] = None) -> List[Dict]:
        """获取代理可用的工具schema"""
        return self.plugin_manager.get_openai_tools_schema(agent_allowed_tools)
    
    def get_common_tools_for_agent(self, agent_allowed_tools: List[str] = None) -> List[Dict]:
        """获取代理可用的常用工具schema"""
        return self.plugin_manager.get_common_tools_schema(agent_allowed_tools)
    
    def list_available_tools(self, agent_tools: List[str] = None) -> List[str]:
        """列出可用工具"""
        tools = []
        for name in self.plugin_manager.list_tools():
            available, _ = self.plugin_manager.is_tool_available(name, agent_tools)
            if available:
                tools.append(name)
        return tools
    
    def search_tools(self, keyword: str, limit: int = 5) -> List[Dict]:
        """搜索工具"""
        return self.plugin_manager.search_tools_by_keyword(keyword, limit)