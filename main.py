"""Omnivoice - 多代理协作系统"""
import asyncio
import sys
import os
import signal
import time
from pathlib import Path
from typing import Optional, Dict

# 添加当前目录到路径
sys.path.insert(0, str(Path(__file__).parent))

# 启用 readline 支持（左右箭头移动光标、历史记录等）
try:
    import readline
    # 设置编辑模式
    readline.parse_and_bind('set editing-mode emacs')
    # 启用历史记录
    history_file = Path.home() / ".omnivoice_history"
    if history_file.exists():
        readline.read_history_file(str(history_file))
    readline.set_history_length(1000)
    # 绑定快捷键
    readline.parse_and_bind('Control-left: backward-word')
    readline.parse_and_bind('Control-right: forward-word')
except ImportError:
    pass  # Termux 可能没有 readline，忽略

from config_loader import load_config, SystemConfig, get_enabled_agents
from agent import create_agent_pool, AgentPool
from whiteboard import Whiteboard
from workspace import WorkspaceManager
from tools.base import PluginManager, ToolRouter
from memory_store import get_memory_store, MemoryStore
from memory_manager import get_memory_manager, MemoryManager
from budget_manager import get_budget_manager, BudgetManager, SessionBudget
from api_cost_controller import get_cost_controller, APICostController
from concurrency_controller import get_concurrency_controller, ConcurrencyController


class Omnivoice:
    """Omnivoice 多代理协作系统"""
    
    def __init__(self, config_path: str = "config.yaml", user_id: str = "default"):
        self.config_path = config_path
        self.user_id = user_id
        self.config: Optional[SystemConfig] = None
        self.agent_pool: Optional[AgentPool] = None
        self.plugin_manager: Optional[PluginManager] = None
        self.tool_router: Optional[ToolRouter] = None
        self.workspace: Optional[WorkspaceManager] = None
        self.whiteboard: Optional[Whiteboard] = None
        self.decision_maker = None
        self.memory_store: Optional[MemoryStore] = None
        self.memory_manager: Optional[MemoryManager] = None
        self.budget_manager: Optional[BudgetManager] = None
        self.cost_controller: Optional[APICostController] = None
        self.concurrency_controller: Optional[ConcurrencyController] = None
    
    def initialize(self):
        """初始化系统"""
        # 加载配置
        self.config = load_config(self.config_path)
        print(f"[OK] 已加载 {len(self.config.agents)} 个代理")
        
        # 初始化插件系统
        self._init_plugins()
        
        # 初始化代理池
        enabled_agents = get_enabled_agents(self.config)
        if not enabled_agents:
            raise ValueError("没有启用的代理！请在配置文件中启用至少一个代理 (enabled: true)")
        
        self.agent_pool = create_agent_pool(enabled_agents)
        self.agent_pool.set_tool_router(self.tool_router)
        print(f"[OK] 代理池初始化完成: {len(self.agent_pool)} 个启用代理")
        
        # 设置工具路由器的模型调用能力
        self._setup_tool_router()
        
        # 初始化工作区管理器
        self.workspace = WorkspaceManager(self.config.global_config.workspace)
        
        # 初始化模式决策器
        from mode_decision import ModeDecisionMaker
        self.decision_maker = ModeDecisionMaker(
            self.config.global_config.voting,
            self.config.global_config.prompts
        )
        
        # 初始化记忆系统
        memory_path = Path(self.config.global_config.workspace.base_dir) / "memory_store"
        self.memory_store = get_memory_store(str(memory_path))
        self.memory_manager = get_memory_manager(self.memory_store)
        print(f"[OK] 记忆系统初始化完成")
        
        # 初始化预算管理系统
        self.budget_manager = get_budget_manager(SessionBudget(
            total_budget=200000,
            max_output_per_call=30,
            max_input_context=2000
        ))
        self.cost_controller = get_cost_controller()
        self.concurrency_controller = get_concurrency_controller()
        
        # 注册代理到并发控制器并自动分组
        agent_ids = [a.id for a in self.agent_pool.get_enabled_agents()]
        self.concurrency_controller.auto_distribute_groups(agent_ids)
        
        # 注册预算警告回调
        self.budget_manager.on_warning(self._on_budget_warning)
        self.budget_manager.on_exceeded(self._on_budget_exceeded)
        
        print(f"[OK] 预算管理系统初始化完成 (预算: {self.budget_manager.config.total_budget} tokens)")
        print("[OK] Omnivoice 初始化完成\n")
    
    def _on_budget_warning(self, report: Dict):
        """预算警告回调"""
        print(f"\n[预算警告] 已使用 {report['budget']['usage_percentage']}")
    
    def _on_budget_exceeded(self, report: Dict):
        """预算超限回调"""
        print(f"\n[预算超限] 已强制结束会话")
    
    def _init_plugins(self):
        """初始化插件系统"""
        # 获取插件目录
        base_dir = Path(__file__).parent
        plugin_dirs = [
            str(base_dir / "plugins"),
            str(base_dir / "tools"),  # 兼容旧的工具目录
        ]
        
        # 创建插件管理器
        self.plugin_manager = PluginManager(plugin_dirs)
        
        # 设置网络工具开关
        self.plugin_manager.set_network_tools_enabled(
            self.config.global_config.enable_network_tools
        )
        
        # 初始化插件
        self.plugin_manager.initialize()
        
        # 创建工具路由器（先创建，稍后设置agent_pool）
        self.tool_router = ToolRouter(self.plugin_manager, None)
        
        tools_count = len(self.plugin_manager.list_tools())
        print(f"[OK] 插件系统初始化完成: {tools_count} 个工具")
    
    def _setup_tool_router(self):
        """设置工具路由器的模型调用能力"""
        if self.tool_router and self.agent_pool:
            self.tool_router.workspace_manager = self.workspace
            
            # 创建模型调用函数
            async def api_call_func(messages, temperature=0.3):
                agents = self.agent_pool.get_enabled_agents()
                if not agents:
                    return None
                agent = agents[0]
                response = await agent.call_api(messages, temperature=temperature)
                return response.content if response.success else None
            
            self.tool_router.set_api_call_func(api_call_func)
        
        # 显示加载错误（如果有）
        errors = self.plugin_manager.loader.get_load_errors()
        if errors:
            for path, error in errors.items():
                print(f"  [警告] 插件加载失败 {path}: {error}")
    
    async def run_session(self, question: str, 
                          mode_preference: Optional[str] = None,
                          project_id: Optional[str] = None) -> dict:
        """运行一个会话"""
        # 创建新会话
        session_id = self.workspace.create_session()
        self.whiteboard = Whiteboard(session_id)
        
        # 设置用户上下文
        self.whiteboard.set_user_context(self.user_id, project_id)
        
        # 注入长期记忆
        memories = self.memory_manager.retrieve_relevant_memories(
            question=question,
            user_id=self.user_id,
            project_id=project_id
        )
        if memories:
            memory_dicts = [m.to_dict() for m in memories]
            self.whiteboard.inject_long_term_memories(memory_dicts)
            print(f"[记忆] 已加载 {len(memories)} 条长期记忆")
        
        # 更新工具路由器的工作区引用
        self.tool_router = ToolRouter(self.plugin_manager, self.workspace)
        self.agent_pool.set_tool_router(self.tool_router)
        
        print(f"[会话] ID: {session_id}")
        print(f"[工作区] {self.workspace.session_path}")
        print()
        
        start_time = time.time()
        
        try:
            # 模式决策
            selected_mode = mode_preference
            
            if not selected_mode:
                print("[决策] 正在进行模式投票...")
                decision = await self.decision_maker.vote(
                    self.agent_pool.get_enabled_agents(),
                    question,
                    self.whiteboard
                )
                from mode_decision import format_voting_result
                print(format_voting_result(decision))
                print()
                selected_mode = decision.selected_mode
            
            self.whiteboard.set_current_mode(selected_mode)
            
            # 执行对应模式
            result = await self._execute_mode(selected_mode, question)
            
            # 输出结果
            self._print_result(selected_mode, result)
            
            elapsed = time.time() - start_time
            print(f"\n[耗时] {elapsed:.2f}秒")
            
            return {
                "success": result.success,
                "mode": selected_mode,
                "resolution": result.final_resolution,
                "elapsed": elapsed,
                "session_id": session_id,
                "error": result.error
            }
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"\n[错误] 执行错误: {str(e)}")
            return {
                "success": False,
                "error": str(e),
                "session_id": session_id
            }
    
    async def _execute_mode(self, mode: str, question: str):
        """执行指定模式"""
        from modes import ConferenceMode, SerialMode
        
        if mode == "conference":
            mode_instance = ConferenceMode(
                self.agent_pool, self.whiteboard,
                self.workspace, self.tool_router, self.config.global_config
            )
        else:  # serial
            mode_instance = SerialMode(
                self.agent_pool, self.whiteboard,
                self.workspace, self.tool_router, self.config.global_config
            )
        
        return await mode_instance.execute(question)
    
    def _print_result(self, mode: str, result):
        """打印结果"""
        from modes.conference import format_conference_output
        from modes.serial import format_serial_output
        
        print()
        
        if mode == "conference":
            print(format_conference_output(result))
        else:
            print(format_serial_output(result))
    
    def cleanup(self):
        """清理资源"""
        if self.workspace:
            self.workspace.close_session(cleanup=True)
        if self.plugin_manager:
            self.plugin_manager.shutdown()


class CLI:
    """极简命令行界面"""
    
    def __init__(self, config_path: str = "config.yaml"):
        self.system = Omnivoice(config_path, "cli_user")
        self.running = False
        self._history: List[str] = []
        self._cmd_history: List[str] = []
    
    def start(self):
        """启动CLI"""
        print("\n=== Omnivoice ===\n")
        
        # 初始化系统
        self.system.initialize()
        
        # 设置信号处理
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        
        self.running = True
        
        # 主循环
        while self.running:
            try:
                # 使用简单输入（兼容性更好）
                line = input("-> ").strip()
                
                if not line:
                    continue
                
                # 保存历史
                self._history.append(line)
                
                # 处理输入
                self._process_input(line)
                
            except KeyboardInterrupt:
                print()
                continue
            except EOFError:
                break
        
        self._cleanup_and_exit()
    
    def _process_input(self, line: str):
        """处理输入"""
        # 系统命令
        if line.startswith('!'):
            self._run_shell_command(line[1:])
            return
        
        # 斜杠命令
        if line.startswith('/'):
            self._handle_command(line)
            return
        
        # 内置命令
        if line in ['quit', 'exit', 'q']:
            self.running = False
            return
        
        if line == 'help':
            self._show_help()
            return
        
        # 运行问题
        asyncio.run(self._run_question(line))
    
    def _handle_command(self, line: str):
        """处理斜杠命令"""
        parts = line.split()
        cmd = parts[0].lower()
        args = ' '.join(parts[1:]) if len(parts) > 1 else ''
        
        self._cmd_history.append(line)
        
        commands = {
            '/help': self._show_help,
            '/h': self._show_help,
            '/tools': self._list_tools,
            '/t': self._list_tools,
            '/agents': self._list_agents,
            '/a': self._list_agents,
            '/search': lambda: self._search_tools(args),
            '/history': self._show_history,
            '/clear': lambda: print("\033[2J\033[H", end=""),
            '/budget': self._show_budget,
            '/cost': self._show_cost,
            '/groups': self._show_groups,
            '/review': self._show_review,
        }
        
        if cmd in commands:
            commands[cmd]()
        elif cmd.startswith('/adjust'):
            self._adjust_agent(args)
        elif cmd.startswith('/reset'):
            self._reset_agent(args)
        elif cmd.startswith('/set'):
            self._set_config(args)
        else:
            print(f"未知命令: {cmd}")
            print("输入 /help 查看帮助")
    
    def _show_help(self):
        """显示详细帮助"""
        help_text = """
Omnivoice - 多代理协作系统

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
基本使用
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  直接输入问题      启动多代理讨论
  quit / exit / q   退出程序

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
斜杠命令
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  /help, /h         显示此帮助
  /tools, /t        列出所有工具
  /agents, /a       列出所有代理
  /search <关键词>  搜索工具
  /history          查看输入历史
  /clear            清屏
  /budget           显示预算状态
  /cost             显示成本报告
  /groups           显示代理分组
  /review           显示复盘报告

  /adjust <代理> <属性> <值>   调整代理性格
  /reset <代理>               重置代理性格
  /set <配置项> <值>          设置配置

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
系统命令
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  !<命令>           执行系统命令
                    例如: !ls, !cat file.txt, !pwd

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
会议信号
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  [EXPAND: 议题]    扩展子话题
  [PARK]            暂存当前议题
  [RESTORE id]      恢复暂存议题
  [INTERRUPT]       叫停并提案

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
代理属性
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  cautiousness      谨慎度 (0-10)
  empathy           共情度 (0-10)
  abstraction       抽象度 (0-10)
  independence      独立性 (0-10)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
快捷键
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Ctrl+C            取消当前输入
  Ctrl+D            退出程序
  上/下键           翻看历史

"""
        print(help_text)
    
    def _run_shell_command(self, cmd: str):
        """执行系统命令"""
        import subprocess
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if result.stdout:
                print(result.stdout)
            if result.stderr:
                print(result.stderr)
        except Exception as e:
            print(f"执行错误: {e}")
    
    def _list_tools(self):
        """列出工具"""
        tools = self.system.plugin_manager.get_all_tools_info()
        print(f"\n[工具] 共 {len(tools)} 个\n")
        for t in tools:
            print(f"  {t['name']}: {t['description'][:40]}")
    
    def _list_agents(self):
        """列出代理"""
        agents = self.system.agent_pool.all_agents()
        print(f"\n[代理] 共 {len(agents)} 个\n")
        for a in agents:
            status = "启用" if a.enabled else "禁用"
            print(f"  [{status}] {a.id}: {a.api.model}")
    
    def _search_tools(self, keyword: str):
        """搜索工具"""
        if not keyword:
            print("用法: /search <关键词>")
            return
        results = self.system.plugin_manager.search_tools_by_keyword(keyword)
        if results:
            print(f"\n[搜索结果]\n")
            for r in results:
                print(f"  {r['name']}: {r['description'][:40]}")
        else:
            print("未找到匹配工具")
    
    def _show_history(self):
        """显示历史"""
        print(f"\n[历史记录]\n")
        for i, h in enumerate(self._history[-20:], 1):
            print(f"  {i}. {h[:60]}")
    
    def _show_budget(self):
        """显示预算"""
        report = self.system.budget_manager.get_report()
        b = report['budget']
        print(f"\n[预算]\n")
        print(f"  总计: {b['total']:,}")
        print(f"  已用: {b['used']:,}")
        print(f"  剩余: {b['remaining']:,}")
    
    def _show_cost(self):
        """显示成本"""
        stats = self.system.cost_controller.get_stats()
        print(f"\n[成本]\n")
        print(f"  调用: {stats['total_calls']}")
        print(f"  缓存: {stats['cache_stats']['hit_rate']}")
    
    def _show_groups(self):
        """显示分组"""
        c = self.system.concurrency_controller.get_summary()
        print(f"\n[分组]\n")
        print(f"  活跃: {c['active_agents']}/{c['total_agents']}")
    
    def _show_review(self):
        """显示复盘"""
        report = self.system.whiteboard.generate_full_review_report()
        print(f"\n[复盘报告]\n")
        print(f"  观点数: {report.get('viewpoint_analysis', {}).get('total', 0)}")
        print(f"  时间线: {len(report.get('timeline', []))} 条")
    
    def _adjust_agent(self, args: str):
        """调整代理"""
        parts = args.split()
        if len(parts) != 3:
            print("用法: /adjust <代理> <属性> <值>")
            return
        agent_id, trait, value = parts
        agent = self.system.agent_pool.get_agent(agent_id)
        if agent and agent.adjust_personality(trait, int(value)):
            print(f"已调整 {agent_id}.{trait} = {value}")
        else:
            print("调整失败")
    
    def _reset_agent(self, agent_id: str):
        """重置代理"""
        agent = self.system.agent_pool.get_agent(agent_id.strip())
        if agent and agent.reset_personality():
            print(f"已重置 {agent_id}")
        else:
            print("重置失败")
    
    def _set_config(self, args: str):
        """设置配置"""
        parts = args.split(maxsplit=1)
        if len(parts) != 2:
            print("用法: /set <配置项> <值>")
            return
        print(f"已设置 {parts[0]} = {parts[1]}")
    
    async def _run_question(self, question: str):
        """运行问题"""
        print()
        await self.system.run_session(question)
    
    def _handle_signal(self, signum, frame):
        self.running = False
    
    def _cleanup_and_exit(self):
        self.system.cleanup()
        # 保存命令历史
        try:
            import readline
            history_file = Path.home() / ".omnivoice_history"
            readline.write_history_file(str(history_file))
        except Exception:
            pass
        print("\n再见!")


def main():
    """主函数"""
    config_path = "config.yaml"
    
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg.endswith('.yaml') or arg.endswith('.yml'):
            config_path = arg
        elif arg in ['-h', '--help']:
            print("用法: python main.py [配置文件]")
            print("\n命令:")
            print("  --conference <问题>  会议模式")
            print("  --serial <问题>      串行模式")
            print("  tools                列出工具")
            print("  agents               列出代理")
            print("  quit                 退出")
            return
    
    if not os.path.exists(config_path):
        print(f"[错误] 配置文件不存在: {config_path}")
        print("\n请创建配置文件或指定正确的路径")
        return
    
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print(f"[警告] API密钥未配置")
    
    cli = CLI(config_path)
    cli.start()


if __name__ == "__main__":
    main()