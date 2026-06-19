"""输出到日志文件（主终端静默，聊天终端读取日志显示）"""
from pathlib import Path
from rich.console import Console

LOG_FILE = Path.home() / ".omnivoice_chat"


class OmnivoiceDisplay:
    def __init__(self, quiet: bool = False, clear_log: bool = False):
        self.console = Console() if not quiet else None
        if clear_log:
            LOG_FILE.write("", encoding="utf-8")

    def _logfile(self, tag: str, text: str):
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{tag}] {text}\n")
        except Exception:
            pass

    def _console(self, text: str):
        if self.console:
            self.console.print(text)

    def log(self, text: str):
        self._console(text)
        self._logfile("SYS", text)

    def log_agent(self, aid: str, stance: str, content: str, color: str = ""):
        self._logfile("MSG", f"{aid}|{stance}|{content}")

    def update_status(self, text: str):
        pass  # 状态行不显示也不记录

    def system(self, text: str):
        self._console(text)
        self._logfile("SYS", text)

    def result(self, text: str):
        self._console(f"[bold cyan]{text}[/]")
        self._logfile("RES", text)

    def error(self, text: str):
        self._console(f"[bold red]{text}[/]")
        self._logfile("ERR", text)

    def start_discussion(self, title: str = ""): pass
    def end_discussion(self): pass
