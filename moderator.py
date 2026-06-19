"""MACI 风格讨论主持人 — 跟踪分歧/重叠/质量，动态推进议程"""
import time
from typing import List, Dict, Optional
from collections import Counter


class Moderator:
    """讨论主持人：跟踪讨论状态，判断是否可以推进议程"""

    def __init__(self):
        self.reset()

    def reset(self):
        self._messages: List[str] = []
        self._stances: List[str] = []
        self._agent_msgs: Dict[str, List[str]] = {}
        self._start_time = time.time()
        self._last_advance_time = 0.0
        # MACI 双拨盘
        self.contentiousness = 0.7   # 行为拨盘：对抗强度
        self.info_gate = 0.5         # 信息拨盘：证据质量门槛

    def observe(self, agent_id: str, stance: str, content: str):
        """记录一条发言"""
        self._messages.append(content)
        self._stances.append(stance)
        if agent_id not in self._agent_msgs:
            self._agent_msgs[agent_id] = []
        self._agent_msgs[agent_id].append(content)

    # ── MACI 四项跟踪信号 ──

    def disagreement(self) -> float:
        """分歧度：基于立场分布（0=完全一致，1=完全对立）"""
        if not self._stances:
            return 0.0
        c = Counter(self._stances)
        # 支持/反对的对立程度
        pro = c.get("支持", 0)
        con = c.get("反对", 0)
        total = len(self._stances)
        if total == 0:
            return 0.0
        return min(1.0, (pro + con) / total * 2 * abs(pro - con) / max(pro + con, 1))

    def overlap(self) -> float:
        """内容重叠度：最近5条消息的词袋相似度（0=全新，1=完全重复）"""
        recent = self._messages[-5:] if len(self._messages) >= 5 else self._messages
        if len(recent) < 2:
            return 0.0
        # 简单重叠检测：共享词比例
        def words(text):
            return set(text.split())
        sets = [words(m) for m in recent]
        common = set.intersection(*sets) if len(sets) > 1 else set()
        if not common:
            return 0.0
        total_unique = len(set.union(*sets))
        return len(common) / max(total_unique, 1)

    def argument_quality(self) -> float:
        """论证质量：基于平均长度和新颖度（0=低质量，1=高质量）"""
        if not self._messages:
            return 0.5
        recent = self._messages[-5:] if len(self._messages) >= 5 else self._messages
        avg_len = sum(len(m) for m in recent) / max(len(recent), 1)
        # 30~200 字为合理范围
        quality = min(1.0, max(0.0, (avg_len - 10) / 190))
        return quality

    def evidence_quality(self) -> float:
        """证据质量：是否包含具体数据/引用（0=无，1=高质量）"""
        if not self._messages:
            return 0.0
        recent = self._messages[-10:] if len(self._messages) >= 10 else self._messages
        # 简单检测：包含数字或代码 = 有具体证据
        import re
        has_number = sum(1 for m in recent if re.search(r'\d+', m))
        has_code = sum(1 for m in recent if '```' in m or 'def ' in m or 'class ' in m)
        total = len(recent)
        return min(1.0, (has_number + has_code * 2) / max(total, 1))

    # ── 议程推进决策 ──

    def should_advance(self, min_rounds: int = 4) -> bool:
        """判断是否应该推进到下一个议程（MACI plateau 检测）"""
        total_msgs = len(self._messages)
        if total_msgs < min_rounds:
            return False

        # 条件1：分歧低 + 重叠高 → 都在说一样的话 → 推进
        if self.disagreement() < 0.2 and self.overlap() > 0.5 and total_msgs >= min_rounds:
            return True

        # 条件2：分歧高但质量下降 → 争吵无意义 → 推进
        if self.disagreement() > 0.8 and self.argument_quality() < 0.3 and total_msgs >= min_rounds:
            return True

        # 条件3：连续多轮无新agent发言 → 冷场 → 推进
        active_agents = len(self._agent_msgs)
        msgs_per_agent = total_msgs / max(active_agents, 1)
        if msgs_per_agent >= 3 and self.overlap() > 0.6:
            return True

        return False

    def get_status(self) -> Dict:
        """获取主持人状态摘要"""
        return {
            "发言数": len(self._messages),
            "分歧度": f"{self.disagreement():.0%}",
            "重叠度": f"{self.overlap():.0%}",
            "论证质量": f"{self.argument_quality():.0%}",
            "信息质量": f"{self.evidence_quality():.0%}",
            "对抗强度": f"{self.contentiousness:.0%}",
        }
