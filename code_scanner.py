"""静态代码扫描器 - 检测危险模式"""
import re
import ast
import keyword
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple, Set
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class ThreatLevel(Enum):
    """威胁级别"""
    CRITICAL = "critical"   # 严重 - 直接拒绝执行
    HIGH = "high"           # 高危 - 强烈建议拒绝
    MEDIUM = "medium"       # 中危 - 需要警告
    LOW = "low"             # 低危 - 仅提示
    INFO = "info"           # 信息 - 仅供参考


class ThreatCategory(Enum):
    """威胁类别"""
    CODE_INJECTION = "code_injection"       # 代码注入
    COMMAND_INJECTION = "command_injection" # 命令注入
    PATH_TRAVERSAL = "path_traversal"       # 路径遍历
    NETWORK_ACCESS = "network_access"       # 网络访问
    FILE_SYSTEM = "file_system"             # 文件系统操作
    RESOURCE_ABUSE = "resource_abuse"       # 资源滥用
    DATA_EXFILTRATION = "data_exfiltration" # 数据泄露
    PRIVILEGE_ESCALATION = "privilege_escalation"  # 权限提升
    CRYPTO_MINING = "crypto_mining"         # 加密货币挖矿
    DESTRUCTIVE = "destructive"             # 破坏性操作


@dataclass
class ThreatMatch:
    """威胁匹配结果"""
    pattern: str                    # 匹配的模式
    category: ThreatCategory        # 威胁类别
    level: ThreatLevel              # 威胁级别
    description: str                # 描述
    line_number: Optional[int] = None  # 行号
    context: Optional[str] = None      # 上下文代码
    suggestion: Optional[str] = None   # 修复建议


@dataclass
class ScanResult:
    """扫描结果"""
    allowed: bool                           # 是否允许执行
    threats: List[ThreatMatch] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    info: List[str] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def critical_count(self) -> int:
        return sum(1 for t in self.threats if t.level == ThreatLevel.CRITICAL)
    
    @property
    def high_count(self) -> int:
        return sum(1 for t in self.threats if t.level == ThreatLevel.HIGH)
    
    @property
    def medium_count(self) -> int:
        return sum(1 for t in self.threats if t.level == ThreatLevel.MEDIUM)
    
    def get_summary(self) -> str:
        """获取摘要"""
        if not self.threats:
            return "[安全] 未检测到威胁"
        
        parts = []
        if self.critical_count:
            parts.append(f"[严重] {self.critical_count}")
        if self.high_count:
            parts.append(f"[高危] {self.high_count}")
        if self.medium_count:
            parts.append(f"[中危] {self.medium_count}")
        
        return " | ".join(parts)


# Python危险模式定义
PYTHON_DANGEROUS_PATTERNS = [
    # 命令执行
    (r'\bos\.system\s*\(', ThreatCategory.COMMAND_INJECTION, ThreatLevel.CRITICAL,
     "直接执行系统命令", "使用subprocess.run并传递参数列表"),
    (r'\bos\.popen\s*\(', ThreatCategory.COMMAND_INJECTION, ThreatLevel.CRITICAL,
     "通过管道执行命令", "使用subprocess.run"),
    (r'\bos\.spawn[a-z]*\s*\(', ThreatCategory.COMMAND_INJECTION, ThreatLevel.CRITICAL,
     "创建新进程执行命令", "避免动态命令执行"),
    (r'\bsubprocess\.(call|run|Popen)\s*\([^)]*shell\s*=\s*True', 
     ThreatCategory.COMMAND_INJECTION, ThreatLevel.CRITICAL,
     "使用shell=True执行命令", "移除shell=True，使用参数列表"),
    (r'\bsubprocess\.', ThreatCategory.COMMAND_INJECTION, ThreatLevel.HIGH,
     "子进程调用", "确保不使用shell=True"),
    
    # 代码执行
    (r'\beval\s*\(', ThreatCategory.CODE_INJECTION, ThreatLevel.CRITICAL,
     "动态执行代码", "使用ast.literal_eval解析字面量"),
    (r'\bexec\s*\(', ThreatCategory.CODE_INJECTION, ThreatLevel.CRITICAL,
     "动态执行代码", "重构代码避免动态执行"),
    (r'\bcompile\s*\(', ThreatCategory.CODE_INJECTION, ThreatLevel.HIGH,
     "编译代码字符串", "避免动态编译"),
    (r'\b__import__\s*\(', ThreatCategory.CODE_INJECTION, ThreatLevel.HIGH,
     "动态导入模块", "使用import语句"),
    
    # 文件系统
    (r'\bopen\s*\([^)]*[\'"][^\'"]*\.\.', ThreatCategory.PATH_TRAVERSAL, ThreatLevel.CRITICAL,
     "路径遍历风险", "使用safe_join验证路径"),
    (r'\bshutil\.rmtree\s*\(', ThreatCategory.DESTRUCTIVE, ThreatLevel.HIGH,
     "递归删除目录", "谨慎使用，添加确认机制"),
    (r'\bos\.remove\s*\(', ThreatCategory.FILE_SYSTEM, ThreatLevel.MEDIUM,
     "删除文件", "添加安全检查"),
    (r'\bos\.unlink\s*\(', ThreatCategory.FILE_SYSTEM, ThreatLevel.MEDIUM,
     "删除文件", "添加安全检查"),
    
    # 网络访问
    (r'\bsocket\.socket\s*\(', ThreatCategory.NETWORK_ACCESS, ThreatLevel.HIGH,
     "创建网络套接字", "代码执行环境禁用网络"),
    (r'\brequests\.(get|post|put|delete|patch)\s*\(', ThreatCategory.NETWORK_ACCESS, ThreatLevel.HIGH,
     "HTTP请求", "代码执行环境禁用网络"),
    (r'\burllib\.request\.', ThreatCategory.NETWORK_ACCESS, ThreatLevel.HIGH,
     "URL请求", "代码执行环境禁用网络"),
    (r'\bhttpx\.', ThreatCategory.NETWORK_ACCESS, ThreatLevel.HIGH,
     "HTTP客户端请求", "代码执行环境禁用网络"),
    
    # 系统信息
    (r'\bos\.environ', ThreatCategory.DATA_EXFILTRATION, ThreatLevel.MEDIUM,
     "访问环境变量", "可能泄露敏感信息"),
    (r'\bplatform\.', ThreatCategory.DATA_EXFILTRATION, ThreatLevel.LOW,
     "获取平台信息", "可能泄露系统信息"),
    
    # 资源滥用
    (r'\bwhile\s+True\s*:', ThreatCategory.RESOURCE_ABUSE, ThreatLevel.MEDIUM,
     "无限循环", "添加退出条件"),
    (r'\bwhile\s+1\s*:', ThreatCategory.RESOURCE_ABUSE, ThreatLevel.MEDIUM,
     "无限循环", "添加退出条件"),
    (r'\bfor\s+_?\s*in\s+itertools\.count', ThreatCategory.RESOURCE_ABUSE, ThreatLevel.MEDIUM,
     "无限迭代", "添加终止条件"),
    (r'\bfrom\s+itertools\s+import\s+count', ThreatCategory.RESOURCE_ABUSE, ThreatLevel.LOW,
     "导入无限迭代器", "注意添加终止条件"),
    
    # 权限提升
    (r'\bos\.setuid\s*\(', ThreatCategory.PRIVILEGE_ESCALATION, ThreatLevel.CRITICAL,
     "修改用户ID", "禁止权限操作"),
    (r'\bos\.setgid\s*\(', ThreatCategory.PRIVILEGE_ESCALATION, ThreatLevel.CRITICAL,
     "修改组ID", "禁止权限操作"),
    (r'\bos\.chmod\s*\([^)]*0o777', ThreatCategory.PRIVILEGE_ESCALATION, ThreatLevel.HIGH,
     "设置完全开放权限", "使用最小权限原则"),
    
    # 破坏性操作
    (r'\brm\s+-rf', ThreatCategory.DESTRUCTIVE, ThreatLevel.CRITICAL,
     "强制递归删除命令", "禁止执行"),
    (r'\bmkfs\s+', ThreatCategory.DESTRUCTIVE, ThreatLevel.CRITICAL,
     "格式化命令", "禁止执行"),
    (r'\bdd\s+if=', ThreatCategory.DESTRUCTIVE, ThreatLevel.CRITICAL,
     "磁盘操作命令", "禁止执行"),
    (r'\b:()\s*>\s*/dev/', ThreatCategory.DESTRUCTIVE, ThreatLevel.CRITICAL,
     "写入设备文件", "禁止执行"),
    
    # 加密货币挖矿
    (r'\bhashlib\.(sha256|sha1|md5)\s*\([^)]*\)\.update', ThreatCategory.CRYPTO_MINING, ThreatLevel.LOW,
     "哈希计算循环", "检查是否为挖矿代码"),
    (r'\bsecrets\.token_', ThreatCategory.CRYPTO_MINING, ThreatLevel.LOW,
     "生成随机令牌", "检查是否滥用"),
]

# Bash危险模式
BASH_DANGEROUS_PATTERNS = [
    # 危险命令
    (r'\brm\s+(-[rf]+\s+|--no-preserve-root)', ThreatCategory.DESTRUCTIVE, ThreatLevel.CRITICAL,
     "强制删除命令", "禁止执行"),
    (r'\bmkfs\.\w+', ThreatCategory.DESTRUCTIVE, ThreatLevel.CRITICAL,
     "格式化文件系统", "禁止执行"),
    (r'\bdd\s+', ThreatCategory.DESTRUCTIVE, ThreatLevel.CRITICAL,
     "磁盘复制命令", "禁止执行"),
    (r'\bshutdown\b', ThreatCategory.DESTRUCTIVE, ThreatLevel.CRITICAL,
     "关机命令", "禁止执行"),
    (r'\breboot\b', ThreatCategory.DESTRUCTIVE, ThreatLevel.CRITICAL,
     "重启命令", "禁止执行"),
    (r'\bhalt\b', ThreatCategory.DESTRUCTIVE, ThreatLevel.CRITICAL,
     "停止系统", "禁止执行"),
    (r'\bpoweroff\b', ThreatCategory.DESTRUCTIVE, ThreatLevel.CRITICAL,
     "关闭电源", "禁止执行"),
    
    # 权限提升
    (r'\bsudo\b', ThreatCategory.PRIVILEGE_ESCALATION, ThreatLevel.CRITICAL,
     "提升权限执行", "禁止使用sudo"),
    (r'\bsu\b', ThreatCategory.PRIVILEGE_ESCALATION, ThreatLevel.CRITICAL,
     "切换用户", "禁止切换用户"),
    (r'\bchmod\s+777\b', ThreatCategory.PRIVILEGE_ESCALATION, ThreatLevel.HIGH,
     "设置完全开放权限", "使用最小权限"),
    
    # 网络操作
    (r'\bcurl\b', ThreatCategory.NETWORK_ACCESS, ThreatLevel.HIGH,
     "网络请求", "代码执行环境禁用网络"),
    (r'\bwget\b', ThreatCategory.NETWORK_ACCESS, ThreatLevel.HIGH,
     "网络下载", "代码执行环境禁用网络"),
    (r'\bnc\b', ThreatCategory.NETWORK_ACCESS, ThreatLevel.HIGH,
     "网络工具", "代码执行环境禁用网络"),
    (r'\bssh\b', ThreatCategory.NETWORK_ACCESS, ThreatLevel.HIGH,
     "SSH连接", "代码执行环境禁用网络"),
    (r'\bscp\b', ThreatCategory.NETWORK_ACCESS, ThreatLevel.HIGH,
     "远程复制", "代码执行环境禁用网络"),
    
    # 危险管道
    (r'\|\s*bash', ThreatCategory.CODE_INJECTION, ThreatLevel.CRITICAL,
     "管道执行bash", "禁止管道执行"),
    (r'\|\s*sh', ThreatCategory.CODE_INJECTION, ThreatLevel.CRITICAL,
     "管道执行shell", "禁止管道执行"),
    (r'curl.*\|\s*(bash|sh)', ThreatCategory.CODE_INJECTION, ThreatLevel.CRITICAL,
     "下载并执行脚本", "极度危险，禁止执行"),
    
    # 文件系统
    (r'>\s*/dev/', ThreatCategory.DESTRUCTIVE, ThreatLevel.CRITICAL,
     "写入设备文件", "禁止执行"),
    (r'>\s*/proc/', ThreatCategory.DESTRUCTIVE, ThreatLevel.CRITICAL,
     "写入proc文件系统", "禁止执行"),
    (r'>\s*/sys/', ThreatCategory.DESTRUCTIVE, ThreatLevel.CRITICAL,
     "写入sys文件系统", "禁止执行"),
    
    # 环境变量泄露
    (r'\benv\b', ThreatCategory.DATA_EXFILTRATION, ThreatLevel.MEDIUM,
     "打印环境变量", "可能泄露敏感信息"),
    (r'\bprintenv\b', ThreatCategory.DATA_EXFILTRATION, ThreatLevel.MEDIUM,
     "打印环境变量", "可能泄露敏感信息"),
    (r'\becho\s+\$[{(]?\w+[)}]?', ThreatCategory.DATA_EXFILTRATION, ThreatLevel.LOW,
     "输出变量值", "检查是否输出敏感信息"),
    
    # 进程管理
    (r'\bkill\s+-9\b', ThreatCategory.DESTRUCTIVE, ThreatLevel.HIGH,
     "强制终止进程", "可能影响系统稳定性"),
    (r'\bpkill\b', ThreatCategory.DESTRUCTIVE, ThreatLevel.HIGH,
     "批量终止进程", "可能影响系统稳定性"),
    (r'\bkillall\b', ThreatCategory.DESTRUCTIVE, ThreatLevel.HIGH,
     "终止所有同名进程", "可能影响系统稳定性"),
    
    # Fork炸弹
    (r':\(\)', ThreatCategory.RESOURCE_ABUSE, ThreatLevel.CRITICAL,
     "Fork炸弹函数定义", "禁止执行"),
    (r':\s*\|:', ThreatCategory.RESOURCE_ABUSE, ThreatLevel.CRITICAL,
     "Fork炸弹核心模式", "禁止执行"),
    (r'\|\s*&', ThreatCategory.RESOURCE_ABUSE, ThreatLevel.HIGH,
     "管道到后台", "可能是Fork炸弹"),
]

# JavaScript危险模式
JAVASCRIPT_DANGEROUS_PATTERNS = [
    # 代码执行
    (r'\beval\s*\(', ThreatCategory.CODE_INJECTION, ThreatLevel.CRITICAL,
     "动态执行代码", "避免使用eval"),
    (r'\bFunction\s*\(', ThreatCategory.CODE_INJECTION, ThreatLevel.CRITICAL,
     "动态创建函数", "避免动态代码执行"),
    (r'\bnew\s+Function\s*\(', ThreatCategory.CODE_INJECTION, ThreatLevel.CRITICAL,
     "动态创建函数", "避免动态代码执行"),
    
    # 子进程
    (r"require\s*\(\s*['\"]child_process['\"]\s*\)", ThreatCategory.COMMAND_INJECTION, ThreatLevel.CRITICAL,
     "导入子进程模块", "代码执行环境禁止子进程"),
    (r'\bexec\s*\(', ThreatCategory.COMMAND_INJECTION, ThreatLevel.CRITICAL,
     "执行命令", "禁止执行系统命令"),
    (r'\bspawn\s*\(', ThreatCategory.COMMAND_INJECTION, ThreatLevel.CRITICAL,
     "创建子进程", "禁止创建子进程"),
    
    # 文件系统
    (r"require\s*\(\s*['\"]fs['\"]\s*\)", ThreatCategory.FILE_SYSTEM, ThreatLevel.HIGH,
     "导入文件系统模块", "检查文件操作是否安全"),
    (r'\bfs\.(unlink|rm|rmSync)\s*\(', ThreatCategory.FILE_SYSTEM, ThreatLevel.HIGH,
     "删除文件", "添加安全检查"),
    
    # 网络
    (r"require\s*\(\s*['\"]http['\"]\s*\)", ThreatCategory.NETWORK_ACCESS, ThreatLevel.HIGH,
     "导入HTTP模块", "代码执行环境禁用网络"),
    (r"require\s*\(\s*['\"]https['\"]\s*\)", ThreatCategory.NETWORK_ACCESS, ThreatLevel.HIGH,
     "导入HTTPS模块", "代码执行环境禁用网络"),
    (r"require\s*\(\s*['\"]net['\"]\s*\)", ThreatCategory.NETWORK_ACCESS, ThreatLevel.HIGH,
     "导入网络模块", "代码执行环境禁用网络"),
    (r'\bfetch\s*\(', ThreatCategory.NETWORK_ACCESS, ThreatLevel.HIGH,
     "HTTP请求", "代码执行环境禁用网络"),
    (r'\bXMLHttpRequest', ThreatCategory.NETWORK_ACCESS, ThreatLevel.HIGH,
     "XHR请求", "代码执行环境禁用网络"),
    
    # 进程控制
    (r'\bprocess\.exit\s*\(', ThreatCategory.DESTRUCTIVE, ThreatLevel.HIGH,
     "退出进程", "禁止终止进程"),
    (r'\bprocess\.kill\s*\(', ThreatCategory.DESTRUCTIVE, ThreatLevel.HIGH,
     "终止进程", "禁止终止进程"),
    
    # 环境变量
    (r'\bprocess\.env\b', ThreatCategory.DATA_EXFILTRATION, ThreatLevel.MEDIUM,
     "访问环境变量", "可能泄露敏感信息"),
]


class CodeScanner:
    """静态代码扫描器"""
    
    # 语言到模式的映射
    LANGUAGE_PATTERNS = {
        "python": PYTHON_DANGEROUS_PATTERNS,
        "py": PYTHON_DANGEROUS_PATTERNS,
        "bash": BASH_DANGEROUS_PATTERNS,
        "sh": BASH_DANGEROUS_PATTERNS,
        "javascript": JAVASCRIPT_DANGEROUS_PATTERNS,
        "js": JAVASCRIPT_DANGEROUS_PATTERNS,
    }
    
    # 最大代码长度
    MAX_CODE_LENGTH = 10000  # 字符
    MAX_CODE_LINES = 500     # 行
    
    def __init__(self, config: Dict[str, Any] = None):
        """
        初始化代码扫描器
        
        Args:
            config: 配置选项
        """
        self.config = config or {}
        self.strict_mode = self.config.get("strict_mode", True)
        self.max_length = self.config.get("max_code_length", self.MAX_CODE_LENGTH)
        self.max_lines = self.config.get("max_code_lines", self.MAX_CODE_LINES)
    
    def scan(self, code: str, language: str) -> ScanResult:
        """
        扫描代码
        
        Args:
            code: 要扫描的代码
            language: 编程语言
            
        Returns:
            ScanResult: 扫描结果
        """
        result = ScanResult(
            allowed=True,
            stats={
                "language": language,
                "code_length": len(code),
                "code_lines": code.count('\n') + 1
            }
        )
        
        # 检查代码长度
        if len(code) > self.max_length:
            result.threats.append(ThreatMatch(
                pattern=f"code_length_{len(code)}",
                category=ThreatCategory.RESOURCE_ABUSE,
                level=ThreatLevel.MEDIUM,
                description=f"代码长度 {len(code)} 超过限制 {self.max_length}",
                suggestion="拆分代码或简化逻辑"
            ))
        
        if code.count('\n') + 1 > self.max_lines:
            result.threats.append(ThreatMatch(
                pattern=f"code_lines_{code.count(chr(10)) + 1}",
                category=ThreatCategory.RESOURCE_ABUSE,
                level=ThreatLevel.MEDIUM,
                description=f"代码行数 {code.count(chr(10)) + 1} 超过限制 {self.max_lines}",
                suggestion="拆分代码或简化逻辑"
            ))
        
        # 获取该语言的危险模式
        patterns = self.LANGUAGE_PATTERNS.get(language.lower())
        if not patterns:
            result.info.append(f"语言 '{language}' 没有预定义的危险模式")
            return result
        
        # 按行扫描
        lines = code.split('\n')
        for i, line in enumerate(lines, 1):
            self._scan_line(line, i, patterns, result)
        
        # Python AST分析（深度检测）
        if language.lower() in ("python", "py"):
            self._scan_python_ast(code, result)
        
        # 根据威胁级别决定是否允许执行
        if result.critical_count > 0:
            result.allowed = False
        elif result.high_count > 0 and self.strict_mode:
            result.allowed = False
        
        return result
    
    def _scan_line(self, line: str, line_num: int, 
                   patterns: List[Tuple], result: ScanResult):
        """扫描单行代码"""
        for pattern, category, level, description, suggestion in patterns:
            match = re.search(pattern, line, re.IGNORECASE)
            if match:
                result.threats.append(ThreatMatch(
                    pattern=pattern,
                    category=category,
                    level=level,
                    description=description,
                    line_number=line_num,
                    context=line.strip()[:100],
                    suggestion=suggestion
                ))
    
    def _scan_python_ast(self, code: str, result: ScanResult):
        """使用AST深度扫描Python代码"""
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            result.warnings.append(f"代码语法错误: {e}")
            return
        
        for node in ast.walk(tree):
            # 检测危险导入
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self._check_import(alias.name, result)
            
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    self._check_import(node.module, result)
            
            # 检测属性访问
            elif isinstance(node, ast.Attribute):
                self._check_attribute(node, result)
            
            # 检测函数调用
            elif isinstance(node, ast.Call):
                self._check_call(node, result)
    
    def _check_import(self, module: str, result: ScanResult):
        """检查导入的模块"""
        dangerous_modules = {
            "os": ("系统操作", ThreatLevel.HIGH),
            "subprocess": ("子进程", ThreatLevel.HIGH),
            "socket": ("网络套接字", ThreatLevel.HIGH),
            "requests": ("HTTP请求", ThreatLevel.HIGH),
            "urllib": ("URL处理", ThreatLevel.MEDIUM),
            "http": ("HTTP协议", ThreatLevel.MEDIUM),
            "ftplib": ("FTP协议", ThreatLevel.HIGH),
            "smtplib": ("SMTP协议", ThreatLevel.HIGH),
            "telnetlib": ("Telnet协议", ThreatLevel.HIGH),
            "paramiko": ("SSH库", ThreatLevel.HIGH),
            "pexpect": ("进程期望", ThreatLevel.MEDIUM),
            "shutil": ("文件操作", ThreatLevel.MEDIUM),
            "multiprocessing": ("多进程", ThreatLevel.MEDIUM),
        }
        
        root_module = module.split('.')[0]
        if root_module in dangerous_modules:
            desc, level = dangerous_modules[root_module]
            result.threats.append(ThreatMatch(
                pattern=f"import {module}",
                category=ThreatCategory.CODE_INJECTION,
                level=level,
                description=f"导入危险模块: {module} ({desc})",
                suggestion="检查是否必要，考虑替代方案"
            ))
    
    def _check_attribute(self, node: ast.Attribute, result: ScanResult):
        """检查属性访问"""
        # 获取属性链
        attrs = []
        current = node
        while isinstance(current, ast.Attribute):
            attrs.append(current.attr)
            current = current.value
        
        if isinstance(current, ast.Name):
            attrs.append(current.id)
        
        attrs.reverse()
        attr_chain = '.'.join(attrs)
        
        # 检查危险属性
        dangerous_attrs = {
            "os.system": ThreatLevel.CRITICAL,
            "os.popen": ThreatLevel.CRITICAL,
            "os.spawn": ThreatLevel.CRITICAL,
            "subprocess.call": ThreatLevel.HIGH,
            "subprocess.run": ThreatLevel.HIGH,
            "subprocess.Popen": ThreatLevel.HIGH,
            "sys.exit": ThreatLevel.MEDIUM,
            "os.environ": ThreatLevel.MEDIUM,
        }
        
        for pattern, level in dangerous_attrs.items():
            if attr_chain.startswith(pattern):
                result.threats.append(ThreatMatch(
                    pattern=attr_chain,
                    category=ThreatCategory.CODE_INJECTION,
                    level=level,
                    description=f"访问危险属性: {attr_chain}",
                    suggestion="检查是否必要"
                ))
    
    def _check_call(self, node: ast.Call, result: ScanResult):
        """检查函数调用"""
        # 检测eval/exec
        if isinstance(node.func, ast.Name):
            if node.func.id in ("eval", "exec", "compile"):
                result.threats.append(ThreatMatch(
                    pattern=f"{node.func.id}()",
                    category=ThreatCategory.CODE_INJECTION,
                    level=ThreatLevel.CRITICAL,
                    description=f"调用危险函数: {node.func.id}()",
                    suggestion="重构代码避免动态执行"
                ))
    
    def quick_scan(self, code: str, language: str) -> Tuple[bool, str]:
        """
        快速扫描 - 返回是否允许和摘要
        
        Args:
            code: 代码
            language: 语言
            
        Returns:
            (allowed, summary): 是否允许和摘要信息
        """
        result = self.scan(code, language)
        return result.allowed, result.get_summary()


class CodeScannerFactory:
    """代码扫描器工厂"""
    
    _instance: Optional[CodeScanner] = None
    
    @classmethod
    def get_scanner(cls, config: Dict = None) -> CodeScanner:
        """获取扫描器实例"""
        if cls._instance is None:
            cls._instance = CodeScanner(config)
        return cls._instance
    
    @classmethod
    def reset(cls):
        """重置扫描器"""
        cls._instance = None
