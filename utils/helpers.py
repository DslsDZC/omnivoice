"""工具函数"""
import json
import hashlib
import time
from datetime import datetime
from typing import Any, Dict, List, Optional


def format_timestamp(ts: float) -> str:
    """格式化时间戳"""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def format_duration(seconds: float) -> str:
    """格式化持续时间"""
    if seconds < 60:
        return f"{seconds:.1f}秒"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}分{secs}秒"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours}小时{minutes}分"


def hash_content(content: str) -> str:
    """计算内容哈希"""
    return hashlib.md5(content.encode()).hexdigest()[:8]


def truncate_text(text: str, max_length: int = 100) -> str:
    """截断文本"""
    if len(text) <= max_length:
        return text
    return text[:max_length-3] + "..."


def safe_json_parse(text: str) -> Optional[Any]:
    """安全解析JSON"""
    try:
        return json.loads(text)
    except:
        return None


def extract_json_from_text(text: str) -> Optional[str]:
    """从文本中提取JSON"""
    import re
    
    # 尝试匹配JSON对象
    obj_match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
    if obj_match:
        return obj_match.group(0)
    
    # 尝试匹配JSON数组
    arr_match = re.search(r'\[[^\[\]]*\]', text, re.DOTALL)
    if arr_match:
        return arr_match.group(0)
    
    return None


def calculate_similarity(text1: str, text2: str) -> float:
    """计算文本相似度（简单的Jaccard相似度）"""
    words1 = set(text1.lower().split())
    words2 = set(text2.lower().split())
    
    if not words1 or not words2:
        return 0.0
    
    intersection = words1 & words2
    union = words1 | words2
    
    return len(intersection) / len(union)


def merge_dicts(base: Dict, override: Dict) -> Dict:
    """合并字典"""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


class RateLimiter:
    """简单的速率限制器"""
    
    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = period
        self.calls: List[float] = []
    
    def allow(self) -> bool:
        """检查是否允许调用"""
        now = time.time()
        
        # 移除过期的调用记录
        self.calls = [t for t in self.calls if now - t < self.period]
        
        if len(self.calls) >= self.max_calls:
            return False
        
        self.calls.append(now)
        return True
    
    def wait_time(self) -> float:
        """获取需要等待的时间"""
        if not self.calls:
            return 0.0
        
        now = time.time()
        oldest = min(self.calls)
        wait = self.period - (now - oldest)
        
        return max(0.0, wait)


class CircularBuffer:
    """环形缓冲区"""
    
    def __init__(self, capacity: int):
        self.capacity = capacity
        self.buffer: List[Any] = []
        self.index = 0
    
    def append(self, item: Any):
        """添加元素"""
        if len(self.buffer) < self.capacity:
            self.buffer.append(item)
        else:
            self.buffer[self.index] = item
            self.index = (self.index + 1) % self.capacity
    
    def get_all(self) -> List[Any]:
        """获取所有元素"""
        if len(self.buffer) < self.capacity:
            return self.buffer.copy()
        
        # 按顺序返回
        return self.buffer[self.index:] + self.buffer[:self.index]
    
    def get_latest(self, n: int) -> List[Any]:
        """获取最近的n个元素"""
        items = self.get_all()
        return items[-n:]
    
    def __len__(self) -> int:
        return len(self.buffer)
