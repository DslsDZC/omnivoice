"""网络工具（预留接口）"""
import asyncio
import json
from typing import Dict, Any, Optional

from tools.base import BaseTool, ToolResult

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False


class WebSearchTool(BaseTool):
    """网络搜索工具"""
    
    name = "web_search"
    description = "调用搜索引擎API返回搜索结果"
    security_level = "low"
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索查询"
            },
            "num_results": {
                "type": "integer",
                "description": "返回结果数量（默认5）"
            }
        },
        "required": ["query"]
    }
    
    def __init__(self, api_key: Optional[str] = None, 
                 search_engine: str = "google",
                 base_url: Optional[str] = None):
        self.api_key = api_key
        self.search_engine = search_engine
        self.base_url = base_url
    
    async def execute(self, args: Dict[str, Any], context: Dict) -> ToolResult:
        if not HAS_AIOHTTP:
            return ToolResult(
                success=False, 
                result=None, 
                error="aiohttp未安装，无法执行网络请求"
            )
        
        query = args.get("query", "")
        num_results = args.get("num_results", 5)
        
        if not query:
            return ToolResult(success=False, result=None, error="搜索查询不能为空")
        
        if not self.api_key:
            return ToolResult(
                success=False, 
                result=None, 
                error="搜索API未配置"
            )
        
        try:
            # 这里需要根据具体的搜索API实现
            # 示例使用DuckDuckGo或其他API
            
            if self.search_engine == "duckduckgo":
                return await self._search_duckduckgo(query, num_results)
            else:
                return ToolResult(
                    success=False, 
                    result=None, 
                    error=f"不支持的搜索引擎: {self.search_engine}"
                )
        except Exception as e:
            return ToolResult(success=False, result=None, error=f"搜索错误: {str(e)}")
    
    async def _search_duckduckgo(self, query: str, num_results: int) -> ToolResult:
        """使用DuckDuckGo搜索"""
        try:
            # DuckDuckGo Instant Answer API
            url = "https://api.duckduckgo.com/"
            params = {
                "q": query,
                "format": "json",
                "no_html": 1,
                "skip_disambig": 1
            }
            
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, params=params) as resp:
                    if resp.status != 200:
                        return ToolResult(
                            success=False, 
                            result=None, 
                            error=f"API返回错误: {resp.status}"
                        )
                    data = await resp.json()
            
            results = []
            
            # 提取相关主题
            related = data.get("RelatedTopics", [])
            for topic in related[:num_results]:
                if isinstance(topic, dict) and "Text" in topic:
                    results.append({
                        "title": topic.get("FirstURL", ""),
                        "snippet": topic.get("Text", ""),
                        "url": topic.get("FirstURL", "")
                    })
            
            # 添加摘要
            abstract = data.get("AbstractText", "")
            if abstract:
                results.insert(0, {
                    "title": "摘要",
                    "snippet": abstract,
                    "url": data.get("AbstractURL", "")
                })
            
            return ToolResult(success=True, result={
                "query": query,
                "results": results,
                "source": "DuckDuckGo"
            })
        except Exception as e:
            return ToolResult(success=False, result=None, error=str(e))


class OnlineKnowledgeTool(BaseTool):
    """在线知识库查询工具"""
    
    name = "online_knowledge"
    description = "查询维基百科或arXiv等在线知识库"
    security_level = "low"
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "查询内容"
            },
            "source": {
                "type": "string",
                "description": "知识源：wikipedia, arxiv",
                "enum": ["wikipedia", "arxiv"]
            }
        },
        "required": ["query"]
    }
    
    async def execute(self, args: Dict[str, Any], context: Dict) -> ToolResult:
        if not HAS_AIOHTTP:
            return ToolResult(
                success=False, 
                result=None, 
                error="aiohttp未安装"
            )
        
        query = args.get("query", "")
        source = args.get("source", "wikipedia")
        
        if not query:
            return ToolResult(success=False, result=None, error="查询内容不能为空")
        
        try:
            if source == "wikipedia":
                return await self._query_wikipedia(query)
            elif source == "arxiv":
                return await self._query_arxiv(query)
            else:
                return ToolResult(
                    success=False, 
                    result=None, 
                    error=f"不支持的知识源: {source}"
                )
        except Exception as e:
            return ToolResult(success=False, result=None, error=f"查询错误: {str(e)}")
    
    async def _query_wikipedia(self, query: str) -> ToolResult:
        """查询维基百科"""
        url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + query.replace(" ", "_")
        
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return ToolResult(success=True, result={
                        "title": data.get("title", ""),
                        "extract": data.get("extract", ""),
                        "url": data.get("content_urls", {}).get("desktop", {}).get("page", "")
                    })
                else:
                    return ToolResult(
                        success=False, 
                        result=None, 
                        error=f"维基百科查询失败: {resp.status}"
                    )
    
    async def _query_arxiv(self, query: str) -> ToolResult:
        """查询arXiv"""
        url = f"http://export.arxiv.org/api/query?search_query=all:{query}&max_results=5"
        
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    # arXiv返回XML，这里简化处理
                    text = await resp.text()
                    return ToolResult(success=True, result={
                        "query": query,
                        "raw_response": text[:2000],  # 截断
                        "source": "arXiv"
                    })
                else:
                    return ToolResult(
                        success=False, 
                        result=None, 
                        error=f"arXiv查询失败: {resp.status}"
                    )


# 网络工具工厂
def create_network_tools(config: Dict[str, Any] = None) -> list:
    """创建网络工具实例"""
    tools = []
    config = config or {}
    
    # WebSearchTool
    search_config = config.get("web_search", {})
    if search_config.get("enabled", False):
        tools.append(WebSearchTool(
            api_key=search_config.get("api_key"),
            search_engine=search_config.get("engine", "duckduckgo"),
            base_url=search_config.get("base_url")
        ))
    
    # OnlineKnowledgeTool
    knowledge_config = config.get("online_knowledge", {})
    if knowledge_config.get("enabled", False):
        tools.append(OnlineKnowledgeTool())
    
    return tools
