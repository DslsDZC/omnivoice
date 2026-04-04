"""立场管理器 - 动态分配代理立场，确保观点多样性"""
import random
import time
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum

from config_loader import StanceType, NeutralityConfig


@dataclass
class AgentStance:
    """代理立场记录"""
    agent_id: str
    stance: StanceType
    assigned_at: float
    reason: str = ""  # 分配原因
    switched_count: int = 0  # 立场切换次数
    confidence: float = 0.5  # 当前立场的置信度


@dataclass
class StanceDistribution:
    """立场分布统计"""
    pro_count: int = 0
    con_count: int = 0
    neutral_count: int = 0
    devil_advocate_count: int = 0
    
    @property
    def total(self) -> int:
        return self.pro_count + self.con_count + self.neutral_count + self.devil_advocate_count
    
    def to_dict(self) -> Dict:
        return {
            "pro": self.pro_count,
            "con": self.con_count,
            "neutral": self.neutral_count,
            "devil_advocate": self.devil_advocate_count
        }
    
    def is_balanced(self, threshold: float = 0.3) -> bool:
        """检查立场是否平衡"""
        if self.total < 2:
            return True
        
        # 计算赞成/反对比例
        total_pro_con = self.pro_count + self.con_count
        if total_pro_con == 0:
            return True
        
        pro_ratio = self.pro_count / total_pro_con
        # 如果一方占比超过阈值，则不平衡
        return 0.3 <= pro_ratio <= 0.7


class StanceManager:
    """立场管理器 - 确保讨论的观点多样性"""
    
    def __init__(self, config: NeutralityConfig):
        self.config = config
        self._agent_stances: Dict[str, AgentStance] = {}
        self._discussion_topic: str = ""
        self._user_viewpoint: Optional[str] = None
        self._switch_history: List[Dict] = []
    
    def initialize_stances(self, agent_ids: List[str], 
                          user_viewpoint: Optional[str] = None) -> Dict[str, StanceType]:
        """初始化代理立场
        
        Args:
            agent_ids: 代理ID列表
            user_viewpoint: 用户观点（如果有）
        
        Returns:
            代理ID -> 立场的映射
        """
        self._user_viewpoint = user_viewpoint
        result = {}
        
        mode = self.config.stance_mode
        
        if mode == "neutral":
            # 默认中立模式：所有代理不预设立场
            for agent_id in agent_ids:
                result[agent_id] = StanceType.NEUTRAL
                self._agent_stances[agent_id] = AgentStance(
                    agent_id=agent_id,
                    stance=StanceType.NEUTRAL,
                    assigned_at=time.time(),
                    reason="默认中立模式"
                )
        
        elif mode == "devil_advocate":
            # 魔鬼代言人模式：随机选择代理作为反对派
            result = self._assign_devil_advocates(agent_ids)
        
        elif mode == "mirror" and user_viewpoint:
            # 用户立场镜像模式：一半支持、一半反对
            result = self._assign_mirror_stances(agent_ids, user_viewpoint)
        
        else:
            # 默认：魔鬼代言人模式
            result = self._assign_devil_advocates(agent_ids)
        
        return result
    
    def _assign_devil_advocates(self, agent_ids: List[str]) -> Dict[str, StanceType]:
        """分配魔鬼代言人"""
        result = {}
        n_devils = min(self.config.devil_advocate_count, len(agent_ids) // 3 + 1)
        n_devils = max(1, n_devils)
        
        # 随机选择魔鬼代言人
        devil_indices = set(random.sample(range(len(agent_ids)), n_devils))
        
        for i, agent_id in enumerate(agent_ids):
            if i in devil_indices:
                stance = StanceType.DEVIL_ADVOCATE
                reason = "系统分配为魔鬼代言人，挑战主流观点"
            else:
                stance = StanceType.NEUTRAL
                reason = "中立观察员"
            
            result[agent_id] = stance
            self._agent_stances[agent_id] = AgentStance(
                agent_id=agent_id,
                stance=stance,
                assigned_at=time.time(),
                reason=reason
            )
        
        return result
    
    def _assign_mirror_stances(self, agent_ids: List[str], 
                               user_viewpoint: str) -> Dict[str, StanceType]:
        """分配镜像立场（一半支持、一半反对用户）"""
        result = {}
        n = len(agent_ids)
        half = n // 2
        
        # 随机打乱顺序
        shuffled = list(agent_ids)
        random.shuffle(shuffled)
        
        for i, agent_id in enumerate(shuffled):
            if i < half:
                stance = StanceType.PRO
                reason = "支持用户观点"
            else:
                stance = StanceType.CON
                reason = "反对用户观点（保证辩论平衡）"
            
            result[agent_id] = stance
            self._agent_stances[agent_id] = AgentStance(
                agent_id=agent_id,
                stance=stance,
                assigned_at=time.time(),
                reason=reason
            )
        
        return result
    
    def get_stance(self, agent_id: str) -> StanceType:
        """获取代理当前立场"""
        if agent_id in self._agent_stances:
            return self._agent_stances[agent_id].stance
        return StanceType.NEUTRAL
    
    def get_stance_info(self, agent_id: str) -> Optional[AgentStance]:
        """获取代理立场详情"""
        return self._agent_stances.get(agent_id)
    
    def set_stance(self, agent_id: str, stance: StanceType, reason: str = ""):
        """手动设置代理立场"""
        old_stance = self._agent_stances.get(agent_id)
        
        self._agent_stances[agent_id] = AgentStance(
            agent_id=agent_id,
            stance=stance,
            assigned_at=time.time(),
            reason=reason,
            switched_count=(old_stance.switched_count + 1) if old_stance else 0
        )
        
        # 记录切换历史
        self._switch_history.append({
            "agent_id": agent_id,
            "from_stance": old_stance.stance.value if old_stance else None,
            "to_stance": stance.value,
            "reason": reason,
            "timestamp": time.time()
        })
    
    def check_and_rebalance(self, agreement_ratio: float) -> List[str]:
        """检查并重新平衡立场
        
        Args:
            agreement_ratio: 当前同意比例（0-1）
        
        Returns:
            被切换立场的代理ID列表
        """
        switched = []
        
        # 如果同意比例过高，触发立场再平衡
        if agreement_ratio > self.config.rebalance_threshold:
            # 找到当前最活跃的赞成方代理，切换为反对派
            pro_agents = [
                aid for aid, s in self._agent_stances.items()
                if s.stance in [StanceType.PRO, StanceType.NEUTRAL]
            ]
            
            if pro_agents:
                # 选择1-2个代理切换
                n_switch = min(2, len(pro_agents))
                to_switch = random.sample(pro_agents, n_switch)
                
                for agent_id in to_switch:
                    self.set_stance(
                        agent_id, 
                        StanceType.CON,
                        f"立场再平衡：同意比例过高 ({agreement_ratio:.0%})"
                    )
                    switched.append(agent_id)
        
        return switched
    
    def get_distribution(self) -> StanceDistribution:
        """获取当前立场分布"""
        dist = StanceDistribution()
        
        for stance_info in self._agent_stances.values():
            if stance_info.stance == StanceType.PRO:
                dist.pro_count += 1
            elif stance_info.stance == StanceType.CON:
                dist.con_count += 1
            elif stance_info.stance == StanceType.NEUTRAL:
                dist.neutral_count += 1
            elif stance_info.stance == StanceType.DEVIL_ADVOCATE:
                dist.devil_advocate_count += 1
        
        return dist
    
    def get_stance_prompt_suffix(self, agent_id: str) -> str:
        """获取立场相关的提示词后缀"""
        stance = self.get_stance(agent_id)
        stance_info = self.get_stance_info(agent_id)
        
        prompts = {
            StanceType.PRO: """
【立场：支持方】
你的任务是支持当前讨论的主流观点。请：
1. 提供支持论据和证据
2. 补充有利的事实和数据
3. 预判反方可能的质疑并提前回应""",
            
            StanceType.CON: """
【立场：反对方】
你的任务是质疑和挑战当前讨论的主流观点。请：
1. 提出反对论据和反例
2. 指出潜在的逻辑漏洞
3. 提供替代方案或边界条件""",
            
            StanceType.NEUTRAL: """
【立场：中立观察员】
你的任务是客观分析，不偏向任何一方。请：
1. 平衡呈现各方观点
2. 基于事实做出判断
3. 在发现问题时主动指出，无论来自哪一方""",
            
            StanceType.DEVIL_ADVOCATE: """
【立场：魔鬼代言人】
你的核心任务是挑战主流观点，找出漏洞和替代方案。
即使你个人同意，也必须提出反对意见。请：
1. 质疑每一个假设
2. 寻找边界条件和反例
3. 提出"如果...会怎样"的问题
4. 不要害怕与其他代理或用户产生分歧"""
        }
        
        return prompts.get(stance, "")
    
    def get_neutrality_enforcement_prompt(self, agent_id: str, independence: int) -> str:
        """获取中立性强制提示词
        
        Args:
            agent_id: 代理ID
            independence: 独立性参数（0-10）
        """
        base = f"""
【独立性要求：{independence}/10】
"""
        
        if independence >= 8:
            base += """
你具有高度批判性。对于用户或任何代理的观点：
1. 首先思考"有什么反例？"、"前提是否成立？"
2. 即使同意，也必须提出至少一个质疑或补充条件
3. 如果大多数人都同意，你应主动提出潜在风险或替代方案
4. 不因用户身份或语气而改变立场
"""
        elif independence >= 5:
            base += """
你保持适度的批判性：
1. 基于事实和逻辑做出判断
2. 发现明显问题时会指出
3. 可以同意他人观点，但需说明理由
"""
        else:
            base += """
你倾向于支持和配合：
1. 主要任务是帮助完成任务
2. 可以提出疑问，但以建设性为主
3. 避免无意义的争执
"""
        
        # 添加立场相关内容
        stance_suffix = self.get_stance_prompt_suffix(agent_id)
        return base + stance_suffix
    
    def get_all_stances(self) -> Dict[str, StanceType]:
        """获取所有代理立场"""
        return {aid: s.stance for aid, s in self._agent_stances.items()}
    
    def get_switch_history(self) -> List[Dict]:
        """获取立场切换历史"""
        return list(self._switch_history)
    
    def reset(self):
        """重置所有立场"""
        self._agent_stances.clear()
        self._user_viewpoint = None
        self._switch_history.clear()
    
    def get_summary(self) -> Dict:
        """获取立场管理摘要"""
        dist = self.get_distribution()
        return {
            "distribution": dist.to_dict(),
            "is_balanced": dist.is_balanced(),
            "switch_count": len(self._switch_history),
            "stance_mode": self.config.stance_mode,
            "neutrality_level": self.config.neutrality_level
        }
