"""Omnivoice 聊天界面 — 气泡格式，内容无【】"""
import time
import textwrap
from pathlib import Path

from rich.console import Console

LOG_FILE = Path.home() / ".omnivoice_chat"
MAX_WIDTH = 32

STANCE_COLORS = {
    "支持": "green", "反对": "red", "质疑": "yellow",
    "中立": "white", "补充": "cyan", "修正": "magenta",
}


def render_one(line: str) -> str | None:
    line = line.rstrip("\n")

    if line.startswith("[MSG] "):
        rest = line[6:]
        parts = rest.split("|", 2)
        if len(parts) < 3:
            return None
        aid, stance, content = parts[0], parts[1], parts[2]
        color = STANCE_COLORS.get(stance, "white")
        lines = [f"[bold {color}]{aid}[/] [{stance}]"]
        wrapped = textwrap.wrap(content, MAX_WIDTH)
        if wrapped:
            w = max(len(l) for l in wrapped) + 1
            lines.append(f"┌{'─' * w}")
            for l in wrapped:
                lines.append(f"│{l}")
        return "\n".join(lines)

    if line.startswith("[SYS] "):
        return f"[dim]{line[6:].strip()}[/]"
    if line.startswith("[RES] "):
        return f"[bold cyan]{line[6:].strip()}[/]"
    if line.startswith("[ERR] "):
        return f"[bold red]{line[6:].strip()}[/]"

    return None


def main():
    console = Console()
    if not LOG_FILE.exists():
        LOG_FILE.touch()

    last = 0
    print("聊天界面启动，另一个终端运行 python main.py\n", flush=True)

    try:
        while True:
            try:
                sz = LOG_FILE.stat().st_size
                if sz > last:
                    with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(last)
                        for line in f:
                            rendered = render_one(line)
                            if rendered:
                                console.print(rendered)
                    last = sz
            except OSError:
                pass
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
