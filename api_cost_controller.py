"""API成本控制器 - 缓存、限流、超时、故障转移"""
import time
import hashlib
import asyncio
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
from collections import OrderedDict
import threading


class APIEndpoint:
    """API端点配置"""
    
    def __init__(self, name: str, base_url: str, api_key: str,
                 cost_tier: str = "medium",  # high/medium/low/free
                 priority: int = 0,
                 rate_limit: int = 60,  # 每分钟请求数
                 timeout: float = 30.0):
        self.name = name
        self.base_url = base_url
        self.api_key = api_key
        self.cost_tier = cost_tier
        self.priority = priority
        self.rate_limit = rate_limit
        self.timeout = timeout


class CacheEntry:
    """缓存条目"""
    
    def __init__(self, key: str, value: Any, ttl: float):
        self.key = key
        self.value = value
        self.ttl = ttl
        self.created_at = time.time()
        self.hits = 0
    
    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl
    
    def touch(self):
        self.hits += 1


class ResponseCache:
    """响应缓存 - 缓存API响应和工具结果"""
    
    def __init__(self, max_size: int = 1000, default_ttl: float = 60.0):
        self.max_size = max_size
        self.default_ttl = default_ttl
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = threading.RLock()
        
        # 统计
        self._hits = 0
        self._misses = 0
    
    def _generate_key(self, *args, **kwargs) -> str:
        """生成缓存键"""
        content = str(args) + str(sorted(kwargs.items()))
        return hashlib.md5(content.encode()).hexdigest()
    
    def get(self, key: str) -> Optional[Any]:
        """获取缓存"""
        with self._lock:
            if key not in self._cache:
                self._misses += 1
                return None
            
            entry = self._cache[key]
            if entry.is_expired():
                del self._cache[key]
                self._misses += 1
                return None
            
            # 移到最后（LRU）
            self._cache.move_to_end(key)
            entry.touch()
            self._hits += 1
            return entry.value
    
    def set(self, key: str, value: Any, ttl: Optional[float] = None):
        """设置缓存"""
        with self._lock:
            # 检查大小限制
            while len(self._cache) >= self.max_size:
                self._cache.popitem(last=False)
            
            entry = CacheEntry(key, value, ttl or self.default_ttl)
            self._cache[key] = entry
            self._cache.move_to_end(key)
    
    def delete(self, key: str) -> bool:
        """删除缓存"""
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False
    
    def clear(self):
        """清空缓存"""
        with self._lock:
            self._cache.clear()
    
    def get_stats(self) -> Dict:
        """获取缓存统计"""
        with self._lock:
            total = self._hits + self._misses
            hit_rate = self._hits / total if total > 0 else 0
            
            return {
                "size": len(self._cache),
                "max_size": self.max_size,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": f"{hit_rate:.1%}"
            }


class PromptCache:
    """提示词缓存 - 缓存系统提示词基础模板"""
    
    def __init__(self):
        self._templates: Dict[str, str] = {}
        self._base_hash: Optional[str] = None
    
    def register_template(self, template_id: str, template: str):
        """注册模板"""
        self._templates[template_id] = template
    
    def get_template(self, template_id: str) -> Optional[str]:
        """获取模板"""
        return self._templates.get(template_id)
    
    def compute_delta(self, template_id: str, variables: Dict) -> Tuple[str, str]:
        """计算模板和变量的差异
        
        Returns:
            (基础模板hash, 变量部分的文本)
        """
        template = self._templates.get(template_id, "")
        # 简化：返回完整内容
        # 实际实现中可以根据API支持的缓存机制优化
        base_hash = hashlib.md5(template.encode()).hexdigest()[:8]
        var_text = str(variables)
        return base_hash, var_text


class RateLimiter:
    """速率限制器"""
    
    def __init__(self, max_requests: int = 60, window_seconds: float = 60.0):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: List[float] = []
        self._lock = threading.Lock()
    
    def can_request(self) -> bool:
        """检查是否可以发起请求"""
        with self._lock:
            now = time.time()
            cutoff = now - self.window_seconds
            
            # 清理过期记录
            self._requests = [t for t in self._requests if t > cutoff]
            
            return len(self._requests) < self.max_requests
    
    def record_request(self):
        """记录请求"""
        with self._lock:
            self._requests.append(time.time())
    
    def wait_time(self) -> float:
        """计算需要等待的时间"""
        with self._lock:
            if len(self._requests) < self.max_requests:
                return 0.0
            
            now = time.time()
            cutoff = now - self.window_seconds
            self._requests = [t for t in self._requests if t > cutoff]
            
            if len(self._requests) < self.max_requests:
                return 0.0
            
            # 等待最早的请求过期
            return self._requests[0] + self.window_seconds - now
    
    def get_usage(self) -> Dict:
        """获取使用情况"""
        with self._lock:
            now = time.time()
            cutoff = now - self.window_seconds
            self._requests = [t for t in self._requests if t > cutoff]
            
            return {
                "current": len(self._requests),
                "max": self.max_requests,
                "window_seconds": self.window_seconds,
                "remaining": self.max_requests - len(self._requests)
            }


class TimeoutController:
    """超时控制器"""
    
    def __init__(self, default_timeout: float = 30.0):
        self.default_timeout = default_timeout
        self._timeouts: Dict[str, float] = {}  # agent_id -> timeout
        self._lock = threading.Lock()
    
    def set_timeout(self, agent_id: str, timeout: float):
        """设置代理超时时间"""
        with self._lock:
            self._timeouts[agent_id] = timeout
    
    def get_timeout(self, agent_id: str) -> float:
        """获取代理超时时间"""
        with self._lock:
            return self._timeouts.get(agent_id, self.default_timeout)
    
    async def execute_with_timeout(self, agent_id: str, 
                                    coro, 
                                    timeout: Optional[float] = None) -> Tuple[Any, bool]:
        """执行协程并处理超时
        
        Returns:
            (结果, 是否超时)
        """
        actual_timeout = timeout or self.get_timeout(agent_id)
        
        try:
            result = await asyncio.wait_for(coro, timeout=actual_timeout)
            return result, False
        except asyncio.TimeoutError:
            return None, True


class APICostController:
    """API成本控制器 - 总控"""
    
    def __init__(self):
        # 缓存
        self.response_cache = ResponseCache()
        self.prompt_cache = PromptCache()
        
        # 限流器（按端点）
        self._rate_limiters: Dict[str, RateLimiter] = {}
        
        # 超时控制器
        self.timeout_controller = TimeoutController()
        
        # 端点配置
        self._endpoints: Dict[str, APIEndpoint] = {}
        self._endpoint_order: List[str] = []  # 按优先级排序
        
        # 代理到端点的映射
        self._agent_endpoints: Dict[str, str] = {}
        
        # 统计
        self._call_stats: Dict[str, Dict] = {}  # agent_id -> stats
        self._total_calls = 0
        self._total_timeouts = 0
        self._total_cached = 0
    
    # ==================== 端点管理 ====================
    
    def register_endpoint(self, endpoint: APIEndpoint):
        """注册API端点"""
        self._endpoints[endpoint.name] = endpoint
        self._rate_limiters[endpoint.name] = RateLimiter(
            max_requests=endpoint.rate_limit,
            window_seconds=60.0
        )
        
        # 更新优先级排序
        self._endpoint_order = sorted(
            self._endpoints.keys(),
            key=lambda k: self._endpoints[k].priority
        )
    
    def get_endpoint(self, endpoint_name: str) -> Optional[APIEndpoint]:
        """获取端点"""
        return self._endpoints.get(endpoint_name)
    
    def get_best_endpoint(self, cost_tier: str = None) -> Optional[APIEndpoint]:
        """获取最佳端点（考虑优先级和限流）"""
        for name in self._endpoint_order:
            endpoint = self._endpoints[name]
            if cost_tier and endpoint.cost_tier != cost_tier:
                continue
            
            limiter = self._rate_limiters.get(name)
            if limiter and limiter.can_request():
                return endpoint
        
        return None
    
    # ==================== 代理端点映射 ====================
    
    def set_agent_endpoint(self, agent_id: str, endpoint_name: str):
        """设置代理使用的端点"""
        self._agent_endpoints[agent_id] = endpoint_name
    
    def get_agent_endpoint(self, agent_id: str) -> Optional[APIEndpoint]:
        """获取代理的端点"""
        endpoint_name = self._agent_endpoints.get(agent_id)
        if endpoint_name:
            return self._endpoints.get(endpoint_name)
        return None
    
    # ==================== 缓存操作 ====================
    
    def cache_tool_result(self, tool_name: str, args: Dict, result: Any, ttl: float = 60.0):
        """缓存工具结果"""
        key = self.response_cache._generate_key(tool_name, args)
        self.response_cache.set(key, result, ttl)
    
    def get_cached_tool_result(self, tool_name: str, args: Dict) -> Optional[Any]:
        """获取缓存的工具结果"""
        key = self.response_cache._generate_key(tool_name, args)
        result = self.response_cache.get(key)
        if result is not None:
            self._total_cached += 1
        return result
    
    def cache_api_response(self, messages_hash: str, response: Any, ttl: float = 30.0):
        """缓存API响应"""
        self.response_cache.set(messages_hash, response, ttl)
    
    def get_cached_api_response(self, messages_hash: str) -> Optional[Any]:
        """获取缓存的API响应"""
        result = self.response_cache.get(messages_hash)
        if result is not None:
            self._total_cached += 1
        return result
    
    # ==================== 限流操作 ====================
    
    def check_rate_limit(self, endpoint_name: str) -> Tuple[bool, float]:
        """检查端点限流
        
        Returns:
            (是否允许, 需等待时间)
        """
        limiter = self._rate_limiters.get(endpoint_name)
        if not limiter:
            return True, 0.0
        
        if limiter.can_request():
            return True, 0.0
        
        return False, limiter.wait_time()
    
    def record_request(self, endpoint_name: str):
        """记录请求"""
        limiter = self._rate_limiters.get(endpoint_name)
        if limiter:
            limiter.record_request()
    
    # ==================== 超时操作 ====================
    
    async def execute_with_control(self, agent_id: str, 
                                    coro,
                                    use_cache: bool = True,
                                    cache_key: Optional[str] = None) -> Tuple[Any, Dict]:
        """执行请求并进行成本控制
        
        Returns:
            (结果, 元数据)
        """
        metadata = {
            "cached": False,
            "timeout": False,
            "endpoint": None,
            "wait_time": 0.0
        }
        
        # 检查缓存
        if use_cache and cache_key:
            cached = self.get_cached_api_response(cache_key)
            if cached is not None:
                metadata["cached"] = True
                return cached, metadata
        
        # 获取端点
        endpoint = self.get_agent_endpoint(agent_id) or self.get_best_endpoint()
        if endpoint:
            metadata["endpoint"] = endpoint.name
            
            # 检查限流
            allowed, wait_time = self.check_rate_limit(endpoint.name)
            if not allowed:
                metadata["wait_time"] = wait_time
                await asyncio.sleep(wait_time)
            
            # 记录请求
            self.record_request(endpoint.name)
        
        # 执行并处理超时
        result, is_timeout = await self.timeout_controller.execute_with_timeout(
            agent_id, coro
        )
        
        metadata["timeout"] = is_timeout
        
        if is_timeout:
            self._total_timeouts += 1
        
        self._total_calls += 1
        
        # 缓存结果
        if use_cache and cache_key and result and not is_timeout:
            self.cache_api_response(cache_key, result)
        
        return result, metadata
    
    # ==================== 统计 ====================
    
    def get_stats(self) -> Dict:
        """获取统计信息"""
        return {
            "total_calls": self._total_calls,
            "total_timeouts": self._total_timeouts,
            "total_cached": self._total_cached,
            "cache_stats": self.response_cache.get_stats(),
            "endpoints": {
                name: self._rate_limiters[name].get_usage()
                for name in self._endpoints.keys()
                if name in self._rate_limiters
            }
        }
    
    def clear_caches(self):
        """清空所有缓存"""
        self.response_cache.clear()


# 全局控制器
_global_controller: Optional[APICostController] = None


def get_cost_controller() -> APICostController:
    """获取全局成本控制器"""
    global _global_controller
    if _global_controller is None:
        _global_controller = APICostController()
    return _global_controller
