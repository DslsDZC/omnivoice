"""CCB 风格三层压缩管道 — 在每次 API 调用前执行"""
import json
import time
from typing import List, Dict, Optional, Tuple, Callable, Awaitable

from run_state import RunState

# token 估算（GLM-4-Flash 无公开 tokenizer）
_CHARS_PER_TOKEN_ZH = 1.5
_CHARS_PER_TOKEN_EN = 3.5
_OVERHEAD_PER_MSG = 8
CTX_LIMIT = 120_000        # 安全上限
AUTO_THRESHOLD = 0.75      # 75% 触发 auto-compact
RECENT_KEEP = 6            # 保留最近 N 轮


def estimate(text: str) -> int:
    if not text:
        return 0
    zh = sum(1 for c in text if '一' <= c <= '鿿')
    en = len(text) - zh
    return int(zh / _CHARS_PER_TOKEN_ZH + en / _CHARS_PER_TOKEN_EN) + 1


def estimate_msgs(msgs: List[Dict]) -> int:
    total = 0
    for m in msgs:
        total += _OVERHEAD_PER_MSG
        c = m.get("content") or ""
        if isinstance(c, str):
            total += estimate(c)
        elif isinstance(c, list):
            for item in c:
                if isinstance(item, dict):
                    total += estimate(item.get("text", ""))
        if m.get("tool_calls"):
            total += estimate(json.dumps(m["tool_calls"]))
    return total


# ── 三步压缩（每次 API 调用前按需执行） ──

def step1_micro(messages: List[Dict], max_output: int = 3000) -> tuple:
    """① Micro：截断单条过长内容"""
    saved = 0
    count = 0
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str) and len(c) > max_output:
            before = estimate(c)
            truncated = c[:max_output] + f"\n...(截断 {len(c)-max_output} 字)"
            saved += before - estimate(truncated)
            count += 1
            out.append({**m, "content": truncated})
        else:
            out.append(m)
    return out, count, saved


def step2_collapse(messages: List[Dict]) -> tuple:
    """② Collapse：移除冗余 tool 消息"""
    saved = 0
    count = 0
    out = []
    for m in messages:
        if m.get("role") == "tool":
            # 截断 tool 结果到 500 字
            c = m.get("content", "")
            if isinstance(c, str) and len(c) > 500:
                before = estimate(c)
                m = {**m, "content": c[:500] + "..."}
                saved += before - estimate(m["content"])
                count += 1
        out.append(m)
    return out, count, saved


async def step3_auto(
    messages: List[Dict],
    summarize_fn: Callable[[str, str, int], Awaitable[str]],
    state: RunState,
    keep_recent: int = RECENT_KEEP,
) -> tuple:
    """③ Auto：接近上限时压缩早期对话"""
    total = estimate_msgs(messages)
    if total < CTX_LIMIT * AUTO_THRESHOLD:
        return messages, 0, 0, False

    # 从后往前数 keep_recent 个 assistant 消息
    count = 0
    split = 0
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") in ("assistant", "user", "tool"):
            count += 1
            if count >= keep_recent:
                split = i
                break
    if split <= 1:
        return messages, 0, 0, False

    to_compress = messages[1:split]  # 保留 system(0)
    to_keep = messages[split:]
    if not to_compress:
        return messages, 0, 0, False

    # 提取文本做摘要
    lines = []
    for m in to_compress:
        r = m.get("role", "?")
        c = m.get("content", "")
        if isinstance(c, str) and c:
            lines.append(f"[{r}] {c[:200]}")
    if not lines:
        return messages, 0, 0, False

    before = estimate_msgs(messages)
    summary = "（摘要）"
    try:
        text = await summarize_fn(
            "压缩对话",
            "\n".join(lines[-30:]),
            300,
        )
        if text:
            summary = text
    except Exception:
        pass

    tombstone = {
        "role": "system",
        "content": f"[对话摘要] {summary}\n"
                   f"[TombstoneMarker] {len(to_compress)} 条已压缩，最近 {keep_recent} 轮保留。",
    }
    new_msgs = [messages[0], tombstone] + to_keep
    after = estimate_msgs(new_msgs)
    saved = before - after

    if state:
        state.mark_compact(len(to_compress), saved)

    return new_msgs, len(to_compress), saved, True


# ── 统一调用管道 ──

async def run_pipeline(
    messages: List[Dict],
    summarize_fn: Callable,
    state: Optional[RunState] = None,
) -> List[Dict]:
    """CCB 风格：API 调用前执行压缩管道"""
    msgs, _, _ = step1_micro(messages)
    msgs, _, _ = step2_collapse(msgs)
    msgs, _, _, _ = await step3_auto(msgs, summarize_fn, state or RunState())
    return msgs
