"""自动化测试框架 - 嵌入串行模式的测试验证"""
import asyncio
import re
import json
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Callable
from enum import Enum
from pathlib import Path


class TestType(Enum):
    """测试类型"""
    SYNTAX = "syntax"           # 语法/格式测试
    LOGIC = "logic"             # 逻辑测试
    WORKSPACE = "workspace"     # 工作区测试
    PERFORMANCE = "performance" # 性能测试
    OUTPUT = "output"           # 输出内容测试


class TestResult(Enum):
    """测试结果"""
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"
    ERROR = "error"


@dataclass
class TestCase:
    """测试用例"""
    name: str
    test_type: TestType
    description: str = ""
    
    # 语法测试
    expected_format: Optional[str] = None  # json, python, etc.
    required_fields: List[str] = field(default_factory=list)
    
    # 输出测试
    expected_pattern: Optional[str] = None  # 正则表达式
    expected_contains: List[str] = field(default_factory=list)
    expected_not_contains: List[str] = field(default_factory=list)
    
    # 工作区测试
    expected_files: List[str] = field(default_factory=list)
    file_patterns: Dict[str, str] = field(default_factory=dict)  # filename -> regex
    
    # 逻辑测试
    execute_code: Optional[str] = None  # 要执行的代码
    expected_output: Optional[str] = None
    expected_return_value: Any = None
    
    # 性能测试
    max_duration_sec: Optional[float] = None
    
    # 配置
    critical: bool = True  # 是否关键测试（失败时是否阻断）
    retry_on_fail: int = 0  # 失败时重试次数


@dataclass
class TestReport:
    """测试报告"""
    test_name: str
    result: TestResult
    message: str
    duration: float = 0
    details: Dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class TestRunner:
    """测试运行器"""
    
    def __init__(self, workspace_path: str = None):
        self.workspace_path = workspace_path or "./temp_workspaces"
        self._test_history: List[TestReport] = []
    
    async def run_test(self, test: TestCase, output: str, 
                       workspace_files: Dict[str, str] = None,
                       execution_result: Any = None) -> TestReport:
        """运行单个测试"""
        start_time = time.time()
        
        try:
            if test.test_type == TestType.SYNTAX:
                result = self._test_syntax(test, output)
            elif test.test_type == TestType.OUTPUT:
                result = self._test_output(test, output)
            elif test.test_type == TestType.WORKSPACE:
                result = self._test_workspace(test, workspace_files or {})
            elif test.test_type == TestType.LOGIC:
                result = await self._test_logic(test, execution_result)
            elif test.test_type == TestType.PERFORMANCE:
                result = self._test_performance(test, start_time)
            else:
                result = TestReport(
                    test_name=test.name,
                    result=TestResult.SKIP,
                    message=f"未知测试类型: {test.test_type}"
                )
        except Exception as e:
            result = TestReport(
                test_name=test.name,
                result=TestResult.ERROR,
                message=f"测试执行错误: {str(e)}"
            )
        
        result.duration = time.time() - start_time
        self._test_history.append(result)
        return result
    
    def _test_syntax(self, test: TestCase, output: str) -> TestReport:
        """语法/格式测试"""
        # 检查 JSON 格式
        if test.expected_format == "json":
            try:
                data = json.loads(output)
                
                # 检查必需字段
                missing = []
                for field in test.required_fields:
                    if field not in data:
                        missing.append(field)
                
                if missing:
                    return TestReport(
                        test_name=test.name,
                        result=TestResult.FAIL,
                        message=f"缺少必需字段: {missing}",
                        details={"missing_fields": missing}
                    )
                
                return TestReport(
                    test_name=test.name,
                    result=TestResult.PASS,
                    message="JSON 格式正确，包含所有必需字段"
                )
            except json.JSONDecodeError as e:
                return TestReport(
                    test_name=test.name,
                    result=TestResult.FAIL,
                    message=f"JSON 解析失败: {str(e)}"
                )
        
        # 检查 Python 语法
        elif test.expected_format == "python":
            try:
                compile(output, "<string>", "exec")
                return TestReport(
                    test_name=test.name,
                    result=TestResult.PASS,
                    message="Python 语法正确"
                )
            except SyntaxError as e:
                return TestReport(
                    test_name=test.name,
                    result=TestResult.FAIL,
                    message=f"Python 语法错误: {str(e)}"
                )
        
        # 其他格式
        return TestReport(
            test_name=test.name,
            result=TestResult.SKIP,
            message=f"不支持检查格式: {test.expected_format}"
        )
    
    def _test_output(self, test: TestCase, output: str) -> TestReport:
        """输出内容测试"""
        issues = []
        
        # 正则模式匹配
        if test.expected_pattern:
            if not re.search(test.expected_pattern, output, re.DOTALL):
                issues.append(f"输出不匹配模式: {test.expected_pattern}")
        
        # 必须包含
        for text in test.expected_contains:
            if text not in output:
                issues.append(f"输出缺少内容: {text}")
        
        # 不能包含
        for text in test.expected_not_contains:
            if text in output:
                issues.append(f"输出不应包含: {text}")
        
        if issues:
            return TestReport(
                test_name=test.name,
                result=TestResult.FAIL,
                message="; ".join(issues),
                details={"issues": issues}
            )
        
        return TestReport(
            test_name=test.name,
            result=TestResult.PASS,
            message="输出内容符合预期"
        )
    
    def _test_workspace(self, test: TestCase, 
                        workspace_files: Dict[str, str]) -> TestReport:
        """工作区测试"""
        issues = []
        
        # 检查文件是否存在
        for filename in test.expected_files:
            if filename not in workspace_files:
                # 检查实际文件系统
                filepath = Path(self.workspace_path) / filename
                if not filepath.exists():
                    issues.append(f"文件不存在: {filename}")
        
        # 检查文件内容模式
        for filename, pattern in test.file_patterns.items():
            content = workspace_files.get(filename, "")
            if not content:
                filepath = Path(self.workspace_path) / filename
                if filepath.exists():
                    content = filepath.read_text()
            
            if content and not re.search(pattern, content, re.DOTALL):
                issues.append(f"文件 {filename} 不匹配模式")
        
        if issues:
            return TestReport(
                test_name=test.name,
                result=TestResult.FAIL,
                message="; ".join(issues),
                details={"issues": issues}
            )
        
        return TestReport(
            test_name=test.name,
            result=TestResult.PASS,
            message="工作区状态符合预期"
        )
    
    async def _test_logic(self, test: TestCase, 
                         execution_result: Any) -> TestReport:
        """逻辑测试"""
        if not test.execute_code:
            return TestReport(
                test_name=test.name,
                result=TestResult.SKIP,
                message="没有要执行的代码"
            )
        
        try:
            # 在沙箱中执行代码
            local_vars = {"result": execution_result}
            exec(test.execute_code, {"__builtins__": __builtins__}, local_vars)
            
            # 检查返回值
            if test.expected_return_value is not None:
                actual = local_vars.get("test_result")
                if actual != test.expected_return_value:
                    return TestReport(
                        test_name=test.name,
                        result=TestResult.FAIL,
                        message=f"返回值不匹配: 期望 {test.expected_return_value}, 实际 {actual}"
                    )
            
            return TestReport(
                test_name=test.name,
                result=TestResult.PASS,
                message="逻辑测试通过"
            )
        except Exception as e:
            return TestReport(
                test_name=test.name,
                result=TestResult.FAIL,
                message=f"执行错误: {str(e)}"
            )
    
    def _test_performance(self, test: TestCase, start_time: float) -> TestReport:
        """性能测试"""
        duration = time.time() - start_time
        
        if test.max_duration_sec and duration > test.max_duration_sec:
            return TestReport(
                test_name=test.name,
                result=TestResult.FAIL,
                message=f"执行超时: {duration:.2f}s > {test.max_duration_sec}s",
                details={"duration": duration, "max": test.max_duration_sec}
            )
        
        return TestReport(
            test_name=test.name,
            result=TestResult.PASS,
            message=f"性能测试通过: {duration:.2f}s",
            details={"duration": duration}
        )
    
    async def run_tests(self, tests: List[TestCase], output: str,
                       workspace_files: Dict[str, str] = None,
                       execution_result: Any = None) -> List[TestReport]:
        """运行多个测试"""
        results = []
        for test in tests:
            result = await self.run_test(
                test, output, workspace_files, execution_result
            )
            results.append(result)
        return results
    
    def get_pass_rate(self, results: List[TestReport]) -> float:
        """获取通过率"""
        if not results:
            return 0.0
        passed = sum(1 for r in results if r.result == TestResult.PASS)
        return passed / len(results)
    
    def get_history(self, count: int = 20) -> List[TestReport]:
        """获取测试历史"""
        return self._test_history[-count:]
    
    def clear_history(self):
        """清空历史"""
        self._test_history.clear()


# 预定义的常用测试
BUILTIN_TESTS = {
    "json_valid": TestCase(
        name="json_valid",
        test_type=TestType.SYNTAX,
        description="验证输出是有效的 JSON",
        expected_format="json"
    ),
    
    "contains_number": TestCase(
        name="contains_number",
        test_type=TestType.OUTPUT,
        description="输出包含数字",
        expected_pattern=r'\d+'
    ),
    
    "python_syntax": TestCase(
        name="python_syntax",
        test_type=TestType.SYNTAX,
        description="验证 Python 语法正确",
        expected_format="python"
    ),
    
    "has_main": TestCase(
        name="has_main",
        test_type=TestType.OUTPUT,
        description="代码包含 main 函数",
        expected_pattern=r'def\s+main\s*\('
    ),
    
    "no_error_keywords": TestCase(
        name="no_error_keywords",
        test_type=TestType.OUTPUT,
        description="输出不包含错误关键词",
        expected_not_contains=["Error", "Exception", "失败", "错误"]
    )
}


def create_test_from_dict(data: Dict) -> TestCase:
    """从字典创建测试用例"""
    return TestCase(
        name=data.get("name", "unnamed"),
        test_type=TestType(data.get("type", "output")),
        description=data.get("description", ""),
        expected_format=data.get("expected_format"),
        required_fields=data.get("required_fields", []),
        expected_pattern=data.get("expected_pattern"),
        expected_contains=data.get("expected_contains", []),
        expected_not_contains=data.get("expected_not_contains", []),
        expected_files=data.get("expected_files", []),
        file_patterns=data.get("file_patterns", {}),
        max_duration_sec=data.get("max_duration_sec"),
        critical=data.get("critical", True),
        retry_on_fail=data.get("retry_on_fail", 0)
    )
