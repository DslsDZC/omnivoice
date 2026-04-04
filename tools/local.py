"""本地只读工具"""
import math
import re
import time
from datetime import datetime
from typing import Dict, Any

from tools.base import BaseTool, ToolResult


class CalculatorTool(BaseTool):
    """计算器工具 - 安全求值数学表达式"""
    
    name = "calculator"
    description = "安全求值数学表达式，支持 + - * / ^ sqrt sin cos tan log 等运算"
    security_level = "high"
    parameters_schema = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "数学表达式，例如: 2 + 3 * 4, sqrt(16), sin(3.14159/2)"
            }
        },
        "required": ["expression"]
    }
    
    # 允许的数学函数和常量
    SAFE_FUNCTIONS = {
        'abs': abs, 'round': round, 'min': min, 'max': max,
        'sqrt': math.sqrt, 'pow': pow,
        'sin': math.sin, 'cos': math.cos, 'tan': math.tan,
        'asin': math.asin, 'acos': math.acos, 'atan': math.atan,
        'sinh': math.sinh, 'cosh': math.cosh, 'tanh': math.tanh,
        'log': math.log, 'log10': math.log10, 'log2': math.log2,
        'exp': math.exp, 'floor': math.floor, 'ceil': math.ceil,
        'factorial': math.factorial, 'gcd': math.gcd,
    }
    
    SAFE_CONSTANTS = {
        'pi': math.pi, 'e': math.e,
    }
    
    async def execute(self, args: Dict[str, Any], context: Dict) -> ToolResult:
        expression = args.get("expression", "").strip()
        
        if not expression:
            return ToolResult(success=False, result=None, error="表达式不能为空")
        
        # 安全检查：只允许数字、运算符、括号和已知的函数名
        # 替换 ^ 为 **
        expression = expression.replace("^", "**")
        
        # 构建安全的命名空间
        safe_dict = {}
        safe_dict.update(self.SAFE_FUNCTIONS)
        safe_dict.update(self.SAFE_CONSTANTS)
        
        # 检查表达式是否只包含安全字符
        allowed_pattern = r'^[\d\s\+\-\*\/\(\)\.\,a-zA-Z_]+$'
        if not re.match(allowed_pattern, expression):
            return ToolResult(
                success=False, 
                result=None, 
                error="表达式包含不允许的字符"
            )
        
        try:
            # 使用受限的eval
            result = eval(expression, {"__builtins__": {}}, safe_dict)
            return ToolResult(success=True, result=result)
        except Exception as e:
            return ToolResult(success=False, result=None, error=f"计算错误: {str(e)}")


class CurrentTimeTool(BaseTool):
    """当前时间工具"""
    
    name = "current_time"
    description = "获取当前时间，返回ISO格式时间戳"
    security_level = "high"
    parameters_schema = {
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": "时区名称（可选），例如: Asia/Shanghai, UTC"
            },
            "format": {
                "type": "string",
                "description": "时间格式（可选），例如: %Y-%m-%d %H:%M:%S"
            }
        },
        "required": []
    }
    
    async def execute(self, args: Dict[str, Any], context: Dict) -> ToolResult:
        try:
            now = datetime.now()
            
            # 格式化
            fmt = args.get("format")
            if fmt:
                result = now.strftime(fmt)
            else:
                result = now.isoformat()
            
            return ToolResult(success=True, result=result)
        except Exception as e:
            return ToolResult(success=False, result=None, error=f"获取时间错误: {str(e)}")


class LocalDocumentRetrieverTool(BaseTool):
    """本地文档检索工具（预留接口）"""
    
    name = "local_document_retriever"
    description = "从本地知识库检索相关文档（需要预先配置知识库）"
    security_level = "high"
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "检索查询"
            },
            "top_k": {
                "type": "integer",
                "description": "返回结果数量（默认5）"
            }
        },
        "required": ["query"]
    }
    
    def __init__(self, knowledge_base=None):
        self.knowledge_base = knowledge_base
    
    async def execute(self, args: Dict[str, Any], context: Dict) -> ToolResult:
        if not self.knowledge_base:
            return ToolResult(
                success=False, 
                result=None, 
                error="知识库未配置"
            )
        
        query = args.get("query", "")
        top_k = args.get("top_k", 5)
        
        try:
            # 这里需要实现实际的检索逻辑
            # 目前返回占位结果
            results = [
                {"content": f"检索结果占位: {query}", "score": 0.9}
            ]
            return ToolResult(success=True, result=results)
        except Exception as e:
            return ToolResult(success=False, result=None, error=f"检索错误: {str(e)}")
