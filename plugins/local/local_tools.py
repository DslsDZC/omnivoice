"""计算器插件"""
import math
import re
from typing import Dict, Any

from tools.base import BaseTool, ToolResult, BasePlugin


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
        
        # 替换 ^ 为 **
        expression = expression.replace("^", "**")
        
        # 安全检查：只允许数字、运算符、括号和字母
        allowed_pattern = r'^[\d\s\+\-\*\/\(\)\.\,a-zA-Z_]+$'
        if not re.match(allowed_pattern, expression):
            return ToolResult(
                success=False, 
                result=None, 
                error="表达式包含不允许的字符"
            )
        
        # 检查是否有危险的函数调用
        dangerous_patterns = ['__', 'import', 'exec', 'eval', 'compile', 'open', 'file']
        for pattern in dangerous_patterns:
            if pattern in expression.lower():
                return ToolResult(
                    success=False,
                    result=None,
                    error=f"表达式包含危险内容: {pattern}"
                )
        
        # 构建安全的命名空间
        safe_dict = {}
        safe_dict.update(self.SAFE_FUNCTIONS)
        safe_dict.update(self.SAFE_CONSTANTS)
        
        try:
            result = eval(expression, {"__builtins__": {}}, safe_dict)
            
            # 格式化结果
            if isinstance(result, float):
                # 处理浮点数精度
                if abs(result - round(result)) < 1e-10:
                    result = int(round(result))
                else:
                    result = round(result, 10)
            
            return ToolResult(
                success=True, 
                result=result,
                metadata={"expression": args.get("expression")}
            )
        except ZeroDivisionError:
            return ToolResult(success=False, result=None, error="除零错误")
        except ValueError as e:
            return ToolResult(success=False, result=None, error=f"值错误: {str(e)}")
        except SyntaxError as e:
            return ToolResult(success=False, result=None, error=f"语法错误: {str(e)}")
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
        from datetime import datetime
        import time as time_module
        
        try:
            now = datetime.now()
            
            # 格式化
            fmt = args.get("format")
            if fmt:
                try:
                    result = now.strftime(fmt)
                except Exception:
                    result = now.isoformat()
            else:
                result = now.isoformat()
            
            return ToolResult(
                success=True, 
                result={
                    "iso_format": now.isoformat(),
                    "formatted": result,
                    "unix_timestamp": time_module.time()
                }
            )
        except Exception as e:
            return ToolResult(success=False, result=None, error=f"获取时间错误: {str(e)}")


class RandomNumberTool(BaseTool):
    """随机数生成工具"""
    
    name = "random_number"
    description = "生成随机数，支持整数和浮点数"
    security_level = "high"
    parameters_schema = {
        "type": "object",
        "properties": {
            "min": {
                "type": "number",
                "description": "最小值（默认0）"
            },
            "max": {
                "type": "number",
                "description": "最大值（默认100）"
            },
            "integer": {
                "type": "boolean",
                "description": "是否生成整数（默认true）"
            },
            "count": {
                "type": "integer",
                "description": "生成数量（默认1）"
            }
        },
        "required": []
    }
    
    async def execute(self, args: Dict[str, Any], context: Dict) -> ToolResult:
        import random
        
        min_val = args.get("min", 0)
        max_val = args.get("max", 100)
        integer = args.get("integer", True)
        count = args.get("count", 1)
        
        if count < 1:
            count = 1
        if count > 1000:
            return ToolResult(success=False, result=None, error="数量超过限制(1000)")
        
        try:
            results = []
            for _ in range(count):
                if integer:
                    results.append(random.randint(int(min_val), int(max_val)))
                else:
                    results.append(random.uniform(min_val, max_val))
            
            if count == 1:
                return ToolResult(success=True, result=results[0])
            else:
                return ToolResult(success=True, result=results)
        except Exception as e:
            return ToolResult(success=False, result=None, error=str(e))


class LocalToolsPlugin(BasePlugin):
    """本地工具插件"""
    
    plugin_name = "local_tools"
    plugin_version = "1.0.0"
    plugin_description = "本地只读工具集合"
    plugin_author = "system"
    plugin_security_level = "high"
    plugin_tags = ["local", "readonly", "math", "time"]
    
    def __init__(self):
        super().__init__()
    
    def initialize(self, config: Dict[str, Any] = None):
        """初始化插件"""
        self.register_tool(CalculatorTool())
        self.register_tool(CurrentTimeTool())
        self.register_tool(RandomNumberTool())
