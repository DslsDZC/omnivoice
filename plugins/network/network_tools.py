"""网络搜索插件"""
import asyncio
import json
from typing import Dict, Any, Optional, List

from tools.base import BaseTool, ToolResult, BasePlugin

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
                "description": "返回结果数量（默认5，最大10）"
            },
            "engine": {
                "type": "string",
                "description": "搜索引擎：duckduckgo, google（默认duckduckgo）",
                "enum": ["duckduckgo", "google"]
            }
        },
        "required": ["query"]
    }
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key
    
    async def execute(self, args: Dict[str, Any], context: Dict) -> ToolResult:
        if not HAS_AIOHTTP:
            return ToolResult(
                success=False, 
                result=None, 
                error="aiohttp未安装，无法执行网络请求"
            )
        
        query = args.get("query", "")
        num_results = min(args.get("num_results", 5), 10)
        engine = args.get("engine", "duckduckgo")
        
        if not query:
            return ToolResult(success=False, result=None, error="搜索查询不能为空")
        
        try:
            if engine == "duckduckgo":
                return await self._search_duckduckgo(query, num_results)
            elif engine == "google":
                return await self._search_google(query, num_results)
            else:
                return ToolResult(
                    success=False,
                    result=None,
                    error=f"不支持的搜索引擎: {engine}"
                )
        except Exception as e:
            return ToolResult(success=False, result=None, error=f"搜索错误: {str(e)}")
    
    async def _search_duckduckgo(self, query: str, num_results: int) -> ToolResult:
        """使用DuckDuckGo搜索"""
        try:
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
                        "title": topic.get("FirstURL", "").split("/")[-1].replace("_", " ") if topic.get("FirstURL") else "",
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
            
            return ToolResult(
                success=True, 
                result={
                    "query": query,
                    "results": results,
                    "source": "DuckDuckGo"
                }
            )
        except Exception as e:
            return ToolResult(success=False, result=None, error=str(e))
    
    async def _search_google(self, query: str, num_results: int) -> ToolResult:
        """使用Google搜索（需要API Key）"""
        if not self.api_key:
            return ToolResult(
                success=False,
                result=None,
                error="Google搜索需要配置API Key"
            )
        
        # 这里需要实现Google Custom Search API调用
        # 简化返回
        return ToolResult(
            success=False,
            result=None,
            error="Google搜索暂未实现，请使用duckduckgo"
        )


class WikipediaQueryTool(BaseTool):
    """维基百科查询工具"""
    
    name = "wikipedia_query"
    description = "查询维基百科获取知识"
    security_level = "low"
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "查询词条"
            },
            "lang": {
                "type": "string",
                "description": "语言代码：en, zh, ja 等（默认en）"
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
        lang = args.get("lang", "en")
        
        if not query:
            return ToolResult(success=False, result=None, error="查询内容不能为空")
        
        try:
            # 使用Wikipedia API
            base_url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/"
            url = base_url + query.replace(" ", "_")
            
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return ToolResult(
                            success=True,
                            result={
                                "title": data.get("title", ""),
                                "extract": data.get("extract", ""),
                                "url": data.get("content_urls", {}).get("desktop", {}).get("page", ""),
                                "thumbnail": data.get("thumbnail", {}).get("source", "")
                            }
                        )
                    elif resp.status == 404:
                        return ToolResult(
                            success=False,
                            result=None,
                            error=f"未找到词条: {query}"
                        )
                    else:
                        return ToolResult(
                            success=False,
                            result=None,
                            error=f"查询失败: HTTP {resp.status}"
                        )
        except Exception as e:
            return ToolResult(success=False, result=None, error=f"查询错误: {str(e)}")


class HTTPRequestTool(BaseTool):
    """HTTP请求工具"""
    
    name = "http_request"
    description = "发送HTTP请求获取数据"
    security_level = "low"
    parameters_schema = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "请求URL"
            },
            "method": {
                "type": "string",
                "description": "HTTP方法：GET, POST, PUT, DELETE",
                "enum": ["GET", "POST", "PUT", "DELETE"]
            },
            "headers": {
                "type": "object",
                "description": "请求头"
            },
            "body": {
                "type": "string",
                "description": "请求体（POST/PUT时使用）"
            },
            "timeout": {
                "type": "integer",
                "description": "超时时间（秒，默认30）"
            }
        },
        "required": ["url"]
    }
    
    async def execute(self, args: Dict[str, Any], context: Dict) -> ToolResult:
        if not HAS_AIOHTTP:
            return ToolResult(
                success=False,
                result=None,
                error="aiohttp未安装"
            )
        
        url = args.get("url", "")
        method = args.get("method", "GET").upper()
        headers = args.get("headers", {})
        body = args.get("body")
        timeout = args.get("timeout", 30)
        
        if not url:
            return ToolResult(success=False, result=None, error="URL不能为空")
        
        # 安全检查：限制某些URL
        blocked_patterns = ["localhost", "127.0.0.1", "0.0.0.0", "192.168.", "10.", "172."]
        for pattern in blocked_patterns:
            if pattern in url:
                return ToolResult(
                    success=False,
                    result=None,
                    error="不允许访问内部网络地址"
                )
        
        try:
            client_timeout = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(timeout=client_timeout) as session:
                request_args = {"headers": headers}
                if body and method in ["POST", "PUT"]:
                    request_args["data"] = body
                
                async with session.request(method, url, **request_args) as resp:
                    response_body = await resp.text()
                    
                    return ToolResult(
                        success=resp.status < 400,
                        result={
                            "status_code": resp.status,
                            "headers": dict(resp.headers),
                            "body": response_body[:10000]  # 限制响应大小
                        }
                    )
        except asyncio.TimeoutError:
            return ToolResult(success=False, result=None, error="请求超时")
        except Exception as e:
            return ToolResult(success=False, result=None, error=f"请求错误: {str(e)}")


class NetworkToolsPlugin(BasePlugin):
    """网络工具插件"""
    
    plugin_name = "network_tools"
    plugin_version = "1.0.0"
    plugin_description = "网络搜索和HTTP请求工具"
    plugin_author = "system"
    plugin_security_level = "low"
    plugin_tags = ["network", "web", "search", "http"]
    
    def __init__(self, config: Dict[str, Any] = None):
        super().__init__()
        self.config = config or {}
    
    def initialize(self, config: Dict[str, Any] = None):
        """初始化插件"""
        config = config or self.config
        
        # 注册工具
        web_search = WebSearchTool(api_key=config.get("search_api_key"))
        self.register_tool(web_search)
        self.register_tool(WikipediaQueryTool())
        self.register_tool(HTTPRequestTool())
