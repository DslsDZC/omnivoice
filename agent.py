"""代理模块 - 支持推理模型与立场管理"""
import asyncio
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Callable
import aiohttp

from config_loader import AgentConfig, PersonalityConfig, StanceType
from whiteboard import Whiteboard
from personality_consistency import PersonalitySnapshot, get_consistency_manager


@dataclass
class AgentResponse:
    """代理响应"""
    agent_id: str
    content: str
    reasoning_content: str = ""  # 推理过程（DeepSeek R1 等）
    tool_calls: List[Dict] = field(default_factory=list)
    raw_response: Dict = field(default_factory=dict)
    success: bool = True
    error: Optional[str] = None
    usage: Dict = field(default_factory=dict)  # token 使用情况


class Agent:
    """代理类 - 支持标准模型和推理模型"""
    
    # 已知的推理模型前缀
    REASONING_MODEL_PREFIXES = [
        'o1', 'o3',           # OpenAI o1/o3 系列
        'deepseek-reasoner',  # DeepSeek R1
        'r1',                 # DeepSeek R1 别名
    ]
    
    def __init__(self, config: AgentConfig, tool_router: Optional[Any] = None):
        self.config = config
        self.id = config.id
        self.api = config.api
        self.personality = config.personality
        self.allowed_tools = set(config.allowed_tools)
        self.enabled = config.enabled
        self.tool_router = tool_router
        
        # 状态
        self._is_busy = False
        self._last_activity = 0.0
        
        # 立场（默认中立，由 StanceManager 分配）
        self._current_stance: StanceType = StanceType.NEUTRAL
        
        # 推理模型检测
        self._detect_reasoning_model()
    
    @property
    def independence(self) -> int:
        """获取独立性参数"""
        return self.personality.independence
    
    @property
    def personality_hash(self) -> str:
        """获取性格哈希"""
        snapshot = PersonalitySnapshot(
            cautiousness=self.personality.cautiousness,
            empathy=self.personality.empathy,
            abstraction=self.personality.abstraction,
            independence=self.personality.independence
        )
        return snapshot.to_hash()
    
    @property
    def consistency_score(self) -> int:
        """获取一致性评分"""
        manager = get_consistency_manager()
        return manager.scorer.get_score(self.id)
    
    def get_personality_snapshot(self) -> PersonalitySnapshot:
        """获取性格快照"""
        return PersonalitySnapshot(
            cautiousness=self.personality.cautiousness,
            empathy=self.personality.empathy,
            abstraction=self.personality.abstraction,
            independence=self.personality.independence
        )
    
    def register_personality(self):
        """注册性格到一致性管理器"""
        manager = get_consistency_manager()
        manager.register_agent(self.id, self.get_personality_snapshot())
    
    def adjust_personality(self, trait: str, value: int) -> bool:
        """临时调整性格参数"""
        manager = get_consistency_manager()
        return manager.adjust_personality(self.id, trait, value)
    
    def reset_personality(self) -> bool:
        """重置性格为默认值"""
        manager = get_consistency_manager()
        return manager.reset_personality(self.id)
    
    def get_consistency_context(self) -> str:
        """获取一致性上下文提示"""
        manager = get_consistency_manager()
        return manager.get_context_prompt(self.id)
    
    def get_correction_prompt(self) -> Optional[str]:
        """获取纠正提示（如果需要）"""
        manager = get_consistency_manager()
        return manager.get_correction_prompt(self.id)
    
    @property
    def current_stance(self) -> StanceType:
        """获取当前立场"""
        return self._current_stance
    
    def set_stance(self, stance: StanceType):
        """设置立场"""
        self._current_stance = stance
    
    def _detect_reasoning_model(self):
        """自动检测是否为推理模型"""
        model_lower = self.api.model.lower()
        
        # 如果配置中明确设置了，使用配置
        if hasattr(self.api, 'reasoning_model'):
            self._is_reasoning_model = self.api.reasoning_model
        else:
            # 自动检测
            self._is_reasoning_model = any(
                model_lower.startswith(prefix) or prefix in model_lower
                for prefix in self.REASONING_MODEL_PREFIXES
            )
        
        # 检查是否支持工具
        if hasattr(self.api, 'supports_tools'):
            self._supports_tools = self.api.supports_tools
        else:
            # 推理模型通常不支持工具
            self._supports_tools = not self._is_reasoning_model
        
        # 检查是否支持系统消息
        if hasattr(self.api, 'supports_system_message'):
            self._supports_system_message = self.api.supports_system_message
        else:
            # o1 不支持 system 消息
            self._supports_system_message = not (
                model_lower.startswith('o1') or model_lower.startswith('o3')
            )
    
    @property
    def is_reasoning_model(self) -> bool:
        return self._is_reasoning_model
    
    @property
    def supports_tools(self) -> bool:
        return self._supports_tools
    
    @property
    def supports_system_message(self) -> bool:
        return self._supports_system_message
    
    @property
    def is_busy(self) -> bool:
        return self._is_busy
    
    @property
    def last_activity(self) -> float:
        return self._last_activity
    
    def has_tool_permission(self, tool_name: str) -> bool:
        """检查是否有工具权限"""
        return tool_name in self.allowed_tools
    
    def _prepare_messages(self, messages: List[Dict]) -> List[Dict]:
        """准备消息列表，处理推理模型的特殊要求"""
        processed = []
        system_content = None
        
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            
            if role == "system":
                if self._supports_system_message:
                    processed.append(msg)
                else:
                    # 不支持 system 消息，保存起来稍后合并
                    system_content = content
            else:
                processed.append(msg)
        
        # 如果有 system 内容需要合并
        if system_content and processed:
            # 合并到第一条 user 消息
            for i, msg in enumerate(processed):
                if msg.get("role") == "user":
                    processed[i] = {
                        "role": "user",
                        "content": f"[系统指令]\n{system_content}\n\n[用户消息]\n{msg.get('content', '')}"
                    }
                    break
        
        return processed
    
    def _build_payload(self, messages: List[Dict], 
                       tools: Optional[List[Dict]] = None,
                       tool_choice: str = "auto",
                       temperature: Optional[float] = None,
                       max_tokens: Optional[int] = None) -> Dict:
        """构建 API 请求载荷"""
        # 准备消息
        processed_messages = self._prepare_messages(messages)
        
        payload = {
            "model": self.api.model,
            "messages": processed_messages,
        }
        
        # 使用配置中的 max_tokens 或传入的值
        actual_max_tokens = max_tokens or getattr(self.api, 'max_tokens', 4096)
        payload["max_tokens"] = actual_max_tokens
        
        # 推理模型的特殊处理
        if self._is_reasoning_model:
            # 推理模型通常不支持 temperature
            # 但某些模型如 DeepSeek R1 可能支持
            
            # OpenAI o1/o3: 添加 reasoning_effort
            model_lower = self.api.model.lower()
            if model_lower.startswith('o1') or model_lower.startswith('o3'):
                reasoning_effort = getattr(self.api, 'reasoning_effort', 'medium')
                if reasoning_effort:
                    payload["reasoning_effort"] = reasoning_effort
            
            # DeepSeek R1 等可能支持 temperature
            if temperature is not None and hasattr(self.api, 'temperature'):
                payload["temperature"] = temperature
        else:
            # 标准模型
            if temperature is not None:
                payload["temperature"] = temperature
            else:
                payload["temperature"] = getattr(self.api, 'temperature', 0.7)
            
            # top_p
            top_p = getattr(self.api, 'top_p', None)
            if top_p is not None:
                payload["top_p"] = top_p
        
        # 工具调用（仅当模型支持且有工具时）
        if tools and self._supports_tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        
        return payload
    
    async def call_api(self, messages: List[Dict], 
                       tools: Optional[List[Dict]] = None,
                       tool_choice: str = "auto",
                       temperature: Optional[float] = None,
                       max_tokens: Optional[int] = None) -> AgentResponse:
        """调用 API（自动适配推理模型）"""
        self._is_busy = True
        self._last_activity = time.time()
        
        try:
            headers = {
                "Authorization": f"Bearer {self.api.api_key}",
                "Content-Type": "application/json"
            }
            
            # 构建请求载荷
            payload = self._build_payload(
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                temperature=temperature,
                max_tokens=max_tokens
            )
            
            # 推理模型需要更长的超时时间
            timeout_seconds = 300 if self._is_reasoning_model else 120
            timeout = aiohttp.ClientTimeout(total=timeout_seconds)
            
            async with aiohttp.ClientSession(timeout=timeout) as session:
                url = f"{self.api.base_url.rstrip('/')}/chat/completions"
                async with session.post(url, headers=headers, json=payload) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        return AgentResponse(
                            agent_id=self.id,
                            content="",
                            success=False,
                            error=f"API错误 {resp.status}: {error_text[:500]}"
                        )
                    
                    data = await resp.json()
            
            # 解析响应
            return self._parse_response(data)
            
        except asyncio.TimeoutError:
            return AgentResponse(
                agent_id=self.id,
                content="",
                success=False,
                error=f"API调用超时（{'推理模型可能需要更长时间' if self._is_reasoning_model else ''}）"
            )
        except aiohttp.ClientError as e:
            return AgentResponse(
                agent_id=self.id,
                content="",
                success=False,
                error=f"网络错误: {str(e)}"
            )
        except json.JSONDecodeError as e:
            return AgentResponse(
                agent_id=self.id,
                content="",
                success=False,
                error=f"响应解析错误: {str(e)}"
            )
        except Exception as e:
            return AgentResponse(
                agent_id=self.id,
                content="",
                success=False,
                error=f"API调用异常: {str(e)}"
            )
        finally:
            self._is_busy = False
            self._last_activity = time.time()
    
    def _parse_response(self, data: Dict) -> AgentResponse:
        """解析 API 响应，处理不同模型的响应格式"""
        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        
        # 内容
        content = message.get("content", "") or ""
        
        # 推理内容（DeepSeek R1 等模型）
        reasoning_content = message.get("reasoning_content", "") or ""
        
        # 工具调用
        tool_calls = []
        if "tool_calls" in message and message["tool_calls"]:
            for tc in message["tool_calls"]:
                tool_calls.append({
                    "id": tc.get("id"),
                    "name": tc.get("function", {}).get("name"),
                    "arguments": json.loads(tc.get("function", {}).get("arguments", "{}"))
                })
        
        # Token 使用情况
        usage = data.get("usage", {})
        
        return AgentResponse(
            agent_id=self.id,
            content=content,
            reasoning_content=reasoning_content,
            tool_calls=tool_calls,
            raw_response=data,
            success=True,
            usage={
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
                "reasoning_tokens": usage.get("reasoning_tokens", 0),  # o1/o3 有这个字段
            }
        )
    
    async def execute_tool_calls(self, tool_calls: List[Dict], 
                                  whiteboard: Whiteboard) -> List[Dict]:
        """执行工具调用"""
        results = []
        
        # 检查是否支持工具
        if not self._supports_tools:
            for tc in tool_calls:
                results.append({
                    "tool_call_id": tc.get("id"),
                    "name": tc.get("name"),
                    "result": "错误: 当前模型不支持工具调用",
                    "success": False
                })
            return results
        
        for tc in tool_calls:
            tool_name = tc.get("name")
            args = tc.get("arguments", {})
            
            if not self.has_tool_permission(tool_name):
                results.append({
                    "tool_call_id": tc.get("id"),
                    "name": tool_name,
                    "result": f"错误: 代理 {self.id} 没有使用工具 {tool_name} 的权限",
                    "success": False
                })
                continue
            
            if not self.tool_router:
                results.append({
                    "tool_call_id": tc.get("id"),
                    "name": tool_name,
                    "result": "错误: 工具路由器未配置",
                    "success": False
                })
                continue
            
            try:
                result = await self.tool_router.execute(tool_name, args, self.id, whiteboard)
                results.append({
                    "tool_call_id": tc.get("id"),
                    "name": tool_name,
                    "result": result,
                    "success": True
                })
                
                # 记录到白板
                whiteboard.add_tool_result(
                    caller=self.id,
                    tool=tool_name,
                    args=args,
                    result=result
                )
                
            except Exception as e:
                results.append({
                    "tool_call_id": tc.get("id"),
                    "name": tool_name,
                    "result": f"工具执行错误: {str(e)}",
                    "success": False
                })
        
        return results
    
    def get_personality_prompt(self) -> str:
        """获取性格描述提示（包含独立性和立场）"""
        cautious_desc = {
            (0, 3): "你倾向于大胆尝试，不畏风险",
            (4, 6): "你在风险和谨慎之间保持平衡",
            (7, 10): "你非常谨慎，总是反复验证"
        }
        
        empathy_desc = {
            (0, 3): "你倾向于理性分析，较少考虑情感因素",
            (4, 6): "你能平衡理性分析和情感考量",
            (7, 10): "你非常注重情感和共情，善于理解他人立场"
        }
        
        abstraction_desc = {
            (0, 3): "你偏向具体和实操，喜欢动手解决问题",
            (4, 6): "你能在抽象和具体之间灵活切换",
            (7, 10): "你擅长抽象思维，喜欢从宏观角度分析问题"
        }
        
        independence_desc = {
            (0, 3): "你倾向于配合和支持，避免冲突",
            (4, 6): "你能平衡合作与独立思考",
            (7, 10): "你具有高度批判性，会主动质疑和提出反例"
        }
        
        stance_desc = {
            StanceType.PRO: "你的立场是支持当前讨论的主流观点",
            StanceType.CON: "你的立场是质疑和挑战当前讨论的主流观点",
            StanceType.NEUTRAL: "你的立场是中立观察，客观分析",
            StanceType.DEVIL_ADVOCATE: "你的立场是魔鬼代言人，必须挑战主流观点、找出漏洞"
        }
        
        def get_desc(value: int, desc_map: Dict) -> str:
            for (low, high), desc in desc_map.items():
                if low <= value <= high:
                    return desc
            return ""
        
        parts = [
            f"你是代理 {self.id}。",
            get_desc(self.personality.cautiousness, cautious_desc),
            get_desc(self.personality.empathy, empathy_desc),
            get_desc(self.personality.abstraction, abstraction_desc),
            get_desc(self.personality.independence, independence_desc),
            stance_desc.get(self._current_stance, ""),
        ]
        
        # 加入默认立场（初始倾向，可随讨论改变）
        default_stance = getattr(self.personality, 'default_stance', None)
        if default_stance:
            stance_hints = {
                "support": "你初始倾向于支持，但遇到有力反驳时可以改变立场。",
                "oppose": "你初始倾向于反对，但如果被说服可以转变支持。",
                "question": "你初始倾向于质疑，需要看到充分证据才会接受。",
                "neutral": "你保持中立，根据论点质量决定立场。"
            }
            if default_stance in stance_hints:
                parts.append(stance_hints[default_stance])
            parts.append("你的立场可以根据讨论中有说服力的论点而改变。")
        
        return " ".join(p for p in parts if p)
    
    def get_neutrality_prompt(self) -> str:
        """获取中立性强制提示词"""
        independence = self.personality.independence
        
        base = f"【独立性要求：{independence}/10】\n"
        
        if independence >= 8:
            base += """你具有高度批判性。对于用户或任何代理的观点：
1. 首先思考"有什么反例？"、"前提是否成立？"
2. 即使同意，也必须提出至少一个质疑或补充条件
3. 如果大多数人都同意，你应主动提出潜在风险或替代方案
4. 不因用户身份或语气而改变立场

"""
        elif independence >= 5:
            base += """你保持适度的批判性：
1. 基于事实和逻辑做出判断
2. 发现明显问题时会指出
3. 可以同意他人观点，但需说明理由

"""
        else:
            base += """你倾向于支持和配合：
1. 主要任务是帮助完成任务
2. 可以提出疑问，但以建设性为主
3. 避免无意义的争执

"""
        
        # 添加立场说明
        stance_prompts = {
            StanceType.PRO: """【立场：支持方】
你的任务是支持当前讨论的主流观点。请：
1. 提供支持论据和证据
2. 补充有利的事实和数据
3. 预判反方可能的质疑并提前回应""",
            
            StanceType.CON: """【立场：反对方】
你的任务是质疑和挑战当前讨论的主流观点。请：
1. 提出反对论据和反例
2. 指出潜在的逻辑漏洞
3. 提供替代方案或边界条件""",
            
            StanceType.NEUTRAL: """【立场：中立观察员】
你的任务是客观分析，不偏向任何一方。请：
1. 平衡呈现各方观点
2. 基于事实做出判断
3. 在发现问题时主动指出，无论来自哪一方""",
            
            StanceType.DEVIL_ADVOCATE: """【立场：魔鬼代言人】
你的核心任务是挑战主流观点，找出漏洞和替代方案。
即使你个人同意，也必须提出反对意见。请：
1. 质疑每一个假设
2. 寻找边界条件和反例
3. 提出"如果...会怎样"的问题
4. 不要害怕与其他代理或用户产生分歧"""
        }
        
        base += stance_prompts.get(self._current_stance, "")
        return base
    
    def get_info(self) -> Dict:
        """获取代理信息"""
        return {
            "id": self.id,
            "model": self.api.model,
            "is_reasoning_model": self._is_reasoning_model,
            "supports_tools": self._supports_tools,
            "supports_system_message": self._supports_system_message,
            "enabled": self.enabled,
            "personality": {
                "cautiousness": self.personality.cautiousness,
                "empathy": self.personality.empathy,
                "abstraction": self.personality.abstraction,
                "independence": self.personality.independence
            },
            "personality_hash": self.personality_hash,
            "consistency_score": self.consistency_score,
            "stance": self._current_stance.value if self._current_stance else "neutral",
            "allowed_tools": list(self.allowed_tools)
        }
    
    def __repr__(self) -> str:
        model_type = "推理" if self._is_reasoning_model else "标准"
        return f"Agent({self.id}, {model_type}模型={self.api.model}, enabled={self.enabled})"


class AgentPool:
    """代理池管理器"""
    
    def __init__(self):
        self._agents: Dict[str, Agent] = {}
        self._tool_router = None
    
    def set_tool_router(self, tool_router: Any):
        """设置工具路由器"""
        self._tool_router = tool_router
        for agent in self._agents.values():
            agent.tool_router = tool_router
    
    def add_agent(self, config: AgentConfig) -> Agent:
        """添加代理"""
        agent = Agent(config, self._tool_router)
        self._agents[agent.id] = agent
        # 注册性格到一致性管理器
        agent.register_personality()
        return agent
    
    def get_agent(self, agent_id: str) -> Optional[Agent]:
        """获取代理"""
        return self._agents.get(agent_id)
    
    def get_enabled_agents(self) -> List[Agent]:
        """获取所有启用的代理"""
        return [a for a in self._agents.values() if a.enabled]
    
    def get_available_agents(self) -> List[Agent]:
        """获取所有可用（未忙碌）的代理"""
        return [a for a in self._agents.values() if a.enabled and not a.is_busy]
    
    def get_reasoning_agents(self) -> List[Agent]:
        """获取推理模型代理"""
        return [a for a in self._agents.values() if a.enabled and a.is_reasoning_model]
    
    def get_standard_agents(self) -> List[Agent]:
        """获取标准模型代理"""
        return [a for a in self._agents.values() if a.enabled and not a.is_reasoning_model]
    
    def select_agent_for_task(self, task_type: str, 
                               prefer_reasoning: bool = False) -> Optional[Agent]:
        """根据任务类型选择最适合的代理
        
        Args:
            task_type: 任务类型
            prefer_reasoning: 是否优先选择推理模型
        """
        available = self.get_available_agents()
        if not available:
            return None
        
        # 如果优先推理模型且有推理模型可用
        if prefer_reasoning:
            reasoning = [a for a in available if a.is_reasoning_model]
            if reasoning:
                return reasoning[0]
        
        # 如果任务需要工具，过滤不支持工具的代理
        tool_tasks = ["code_generation", "data_calculation", "file_operation"]
        if task_type in tool_tasks:
            tool_capable = [a for a in available if a.supports_tools]
            if tool_capable:
                available = tool_capable
        
        # 根据任务类型匹配性格
        if task_type == "code_generation":
            available.sort(key=lambda a: (
                -a.personality.cautiousness,
                a.personality.abstraction
            ))
        elif task_type == "data_calculation":
            available.sort(key=lambda a: (
                -a.personality.cautiousness,
                a.personality.abstraction if a.personality.abstraction <= 6 else 0
            ))
        elif task_type == "review":
            available.sort(key=lambda a: (
                -a.personality.cautiousness,
                a.personality.empathy
            ))
        elif task_type == "reasoning":
            # 推理任务优先推理模型
            available.sort(key=lambda a: (
                -1 if a.is_reasoning_model else 0,
                -a.personality.abstraction
            ))
        
        return available[0]
    
    def get_voting_agents(self, count: int = 3) -> List[Agent]:
        """获取投票代理（优先标准模型，因为投票需要快速响应）"""
        enabled = self.get_enabled_agents()
        
        # 优先使用标准模型进行投票（推理模型太慢）
        standard = [a for a in enabled if not a.is_reasoning_model]
        
        if len(standard) >= count:
            return standard[:count]
        
        # 标准模型不够，补充推理模型
        reasoning = [a for a in enabled if a.is_reasoning_model]
        return (standard + reasoning)[:count]
    
    def all_agents(self) -> List[Agent]:
        """获取所有代理"""
        return list(self._agents.values())
    
    def get_agents_info(self) -> List[Dict]:
        """获取所有代理信息"""
        return [a.get_info() for a in self._agents.values()]
    
    def __len__(self) -> int:
        return len(self._agents)
    
    def __contains__(self, agent_id: str) -> bool:
        return agent_id in self._agents
    
    def __iter__(self):
        return iter(self._agents.values())


def create_agent_pool(configs: List[AgentConfig], 
                      tool_router: Optional[Any] = None) -> AgentPool:
    """创建代理池"""
    pool = AgentPool()
    if tool_router:
        pool.set_tool_router(tool_router)
    for config in configs:
        pool.add_agent(config)
    return pool


# ==================== 异步轮询代理循环 ====================

class AsyncAgentLoop:
    """代理独立循环控制器 - 基于异步轮询模式"""
    
    # 信号模式
    SIGNAL_PATTERNS = {
        "interrupt": r'\[INTERRUPT\]',
        "think_req": r'\[THINK_REQ\s+(\d+)\]',
        "agenda_end": r'\[AGENDA_END\]',
        "need_meeting": r'\[NEED_MEETING\]',
        "vote": r'\[VOTE:\s*(support|oppose|abstain)(?::\s*(.+?))?\]',
    }
    
    def __init__(self, agent: Agent, whiteboard: Whiteboard,
                 speech_controller: Optional[Any] = None,
                 config: Optional[Dict] = None):
        self.agent = agent
        self.whiteboard = whiteboard
        self.speech_controller = speech_controller
        self.config = config or {}
        
        # 循环控制
        self._running = False
        self._paused = False
        self._task: Optional[asyncio.Task] = None
        
        # 休眠配置
        self._base_sleep = 0.5  # 基础休眠时间
        self._active_sleep = 0.05  # 活跃时休眠
        self._idle_sleep = 0.5  # 冷清时休眠
        
        # 初始化白板中的代理状态
        self.whiteboard.init_agent_state(
            agent.id, 
            expertise=getattr(agent.personality, 'expertise', '')
        )
    
    async def start(self):
        """启动代理循环"""
        if self._running:
            return
        
        self._running = True
        self._task = asyncio.create_task(self._loop())
    
    async def stop(self):
        """停止代理循环"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
    
    async def pause(self):
        """暂停循环"""
        self._paused = True
    
    async def resume(self):
        """恢复循环"""
        self._paused = False
    
    async def _loop(self):
        """主循环：读取白板 → 内部思考 → 生成发言 → 提交 → 休眠"""
        while self._running:
            try:
                # 检查暂停
                while self._paused and self._running:
                    await asyncio.sleep(0.5)
                
                if not self._running:
                    break
                
                # 1. 检查全局模式标志
                if self.whiteboard.is_voting_mode():
                    # 表决模式：只响应投票请求
                    await self._handle_voting_mode()
                    await asyncio.sleep(0.5)
                    continue
                
                if self.whiteboard.is_think_mode():
                    # 思考暂停模式：保持静默，可调用工具
                    think_status = self.whiteboard.get_think_pause_status()
                    if think_status["agent_id"] == self.agent.id:
                        # 自己发起的思考，可以思考并调用工具
                        await self._handle_own_think_time(think_status)
                    else:
                        # 别人的思考时间，静默等待
                        await asyncio.sleep(1.0)
                    continue
                
                # 2. 读取白板新事件
                events = self.whiteboard.get_new_events(self.agent.id)
                
                # 3. 内部思考：决定是否发言
                should_speak, reason = await self._decide_speak(events)
                
                if should_speak:
                    # 4. 生成发言
                    response = await self._generate_speech(events, reason)
                    
                    if response and response.success:
                        # 5. 提交发言
                        await self._submit_speech(response)
                else:
                    # 标记事件已读
                    self.whiteboard.mark_events_read(self.agent.id)
                
                # 6. 短暂休眠
                sleep_time = self._calculate_sleep_time()
                
                # 加上额外休眠（如果有惩罚）
                state = self.whiteboard.get_agent_state(self.agent.id)
                extra_sleep = state.get("extra_sleep", 0)
                sleep_time += extra_sleep
                
                await asyncio.sleep(sleep_time)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                # 记录错误并继续
                print(f"[{self.agent.id}] 循环错误: {e}")
                await asyncio.sleep(1.0)
    
    async def _decide_speak(self, events: Dict) -> tuple:
        """决定是否发言"""
        messages = events.get("messages", [])
        
        # 没有新消息，不发言
        if not messages:
            return False, "no_new_messages"
        
        # 检查是否被@
        last_msg = messages[-1] if messages else None
        if last_msg and f"@{self.agent.id}" in str(last_msg.get("content", "")):
            return True, "mentioned"
        
        # 检查是否有优先发言权
        if self.whiteboard.has_think_priority(self.agent.id):
            return True, "think_priority"
        
        # 基于性格决定发言概率
        independence = self.agent.personality.independence
        speak_probability = 0.3 + (independence / 20)  # 0.35 ~ 0.8
        
        import random
        if random.random() < speak_probability:
            return True, "proactive"
        
        return False, "waiting"
    
    async def _generate_speech(self, events: Dict, reason: str) -> Optional[AgentResponse]:
        """生成发言"""
        # 构建上下文消息
        context_messages = self._build_context_messages(events)
        
        if not context_messages:
            return None
        
        # 获取性格提示
        personality_prompt = self.agent.get_personality_prompt()
        neutrality_prompt = self.agent.get_neutrality_prompt()
        
        # 构建系统消息
        system_message = {
            "role": "system",
            "content": f"{personality_prompt}\n\n{neutrality_prompt}\n\n"
                       f"发言原因: {reason}\n"
                       f"请根据最新讨论内容，发表你的观点。"
        }
        
        messages = [system_message] + context_messages
        
        # 调用 API
        response = await self.agent.call_api(messages)
        
        return response
    
    def _build_context_messages(self, events: Dict) -> List[Dict]:
        """构建上下文消息"""
        messages = []
        
        for msg in events.get("messages", [])[-10:]:  # 最近10条消息
            role = "assistant" if msg.get("agent_id") else "user"
            content = msg.get("content", "")
            
            if role == "assistant":
                agent_id = msg.get("agent_id", "unknown")
                messages.append({
                    "role": "assistant",
                    "content": f"[{agent_id}]: {content}"
                })
            else:
                messages.append({
                    "role": "user",
                    "content": content
                })
        
        return messages
    
    async def _submit_speech(self, response: AgentResponse):
        """提交发言到白板"""
        content = response.content
        
        # 检测特殊信号
        import re
        signals = {}
        
        for signal_name, pattern in self.SIGNAL_PATTERNS.items():
            matches = re.findall(pattern, content, re.IGNORECASE)
            if matches:
                signals[signal_name] = matches
        
        # 检查重复内容
        dup_check = self.whiteboard.check_duplicate_content(content, self.agent.id)
        if dup_check["is_duplicate"]:
            self.whiteboard.record_duplicate(self.agent.id)
            # 不提交重复内容
            return
        
        # 重置重复计数
        self.whiteboard.reset_duplicate_count(self.agent.id)
        
        # 添加消息到白板
        msg = self.whiteboard.add_message(
            agent_id=self.agent.id,
            content=content,
            tool_calls=response.tool_calls if response.tool_calls else None
        )
        
        # 更新代理状态
        self.whiteboard.update_agent_state(
            self.agent.id,
            total_speaks=self.whiteboard.get_agent_state(self.agent.id).get("total_speaks", 0) + 1,
            last_speak_time=time.time()
        )
        
        # 增加有效轮次
        self.whiteboard.increment_round()
        
        # 重置活动计时器
        self.whiteboard.reset_activity_timer()
        
        # 标记事件已读
        self.whiteboard.mark_events_read(self.agent.id)
        
        # 计算优先级并添加到展示队列
        priority_score = self._calculate_priority(response, signals)
        self.whiteboard.add_to_display_queue(
            message_id=msg.timestamp,  # 使用时间戳作为ID
            priority_score=priority_score,
            content=content,
            agent_id=self.agent.id
        )
        
        # 处理特殊信号
        await self._handle_signals(signals, content)
    
    def _calculate_priority(self, response: AgentResponse, signals: Dict) -> float:
        """计算发言优先级分数"""
        score = 50.0  # 基础分
        
        content = response.content
        
        # 事实纠错：+40
        if any(kw in content.lower() for kw in ["纠正", "错误", "不准确", "incorrect", "error"]):
            score += 40
        
        # 被其他代理@：+30（已在_decide_speak中处理）
        if f"@{self.agent.id}" in content:
            score += 30
        
        # 包含工具调用结果：+20
        if response.tool_calls:
            score += 20
        
        # 发言者历史贡献分（0-20）
        state = self.whiteboard.get_agent_state(self.agent.id)
        contribution = state.get("contribution_score", 1.0)
        score += min(20, contribution * 2)
        
        # 有思考优先权：+40
        if self.whiteboard.has_think_priority(self.agent.id):
            score += 40
        
        # INTERRUPT信号：高优先级
        if "interrupt" in signals:
            score += 50
        
        # THINK_REQ信号
        if "think_req" in signals:
            score += 10
        
        # 发言长度惩罚：每超过30 token扣1分
        tokens = len(content.split())
        if tokens > 30:
            score -= (tokens - 30) / 30
        
        return max(0, score)
    
    async def _handle_signals(self, signals: Dict, content: str):
        """处理特殊信号"""
        # INTERRUPT信号
        if "interrupt" in signals:
            # 触发表决模式
            self.whiteboard.set_voting_mode(True)
        
        # THINK_REQ信号
        if "think_req" in signals:
            duration = int(signals["think_req"][0])
            result = self.whiteboard.request_think_pause(self.agent.id, duration)
            if not result.get("approved"):
                # 请求被拒绝，通知代理
                self.whiteboard.add_message(
                    agent_id="system",
                    content=f"[系统] 代理 {self.agent.id} 的思考请求被拒绝: {result.get('reason')}",
                    message_type="system"
                )
        
        # AGENDA_END信号
        if "agenda_end" in signals:
            # 触发议程结束投票
            self.whiteboard.vote_end_current_agenda(self.agent.id, True)
        
        # NEED_MEETING信号（串行模式下）
        if "need_meeting" in signals:
            if self.whiteboard.is_serial_mode():
                # 切换到会议模式
                self.whiteboard.set_serial_mode(False)
    
    async def _handle_voting_mode(self):
        """处理表决模式"""
        # 检查当前投票会话
        vote_session = self.whiteboard.get_current_vote_session()
        
        if not vote_session:
            return
        
        # 检查是否已投票
        if self.agent.id in vote_session.get("votes", {}):
            return
        
        # 生成投票
        vote_prompt = f"""当前投票议题: {vote_session.get('topic', '未知议题')}
        
请根据你的立场和判断，输出你的投票。
格式: [VOTE: support/oppose/abstain: 理由]

作为代理 {self.agent.id}，你的性格:
- 独立性: {self.agent.personality.independence}/10
- 谨慎性: {self.agent.personality.cautiousness}/10
"""
        
        messages = [{"role": "user", "content": vote_prompt}]
        response = await self.agent.call_api(messages)
        
        if response.success:
            content = response.content
            
            # 解析投票
            import re
            match = re.search(r'\[VOTE:\s*(support|oppose|abstain)(?::\s*(.+?))?\]', content, re.IGNORECASE)
            
            if match:
                vote_type = match.group(1).lower()
                reason = match.group(2) or ""
                
                self.whiteboard.submit_vote(
                    session_id=vote_session["id"],
                    agent_id=self.agent.id,
                    vote=vote_type,
                    reason=reason
                )
    
    async def _handle_own_think_time(self, think_status: Dict):
        """处理自己的思考时间"""
        time_remaining = think_status.get("time_remaining", 0)
        
        if time_remaining <= 0:
            # 思考时间结束
            result = self.whiteboard.end_think_pause()
            return
        
        # 在思考时间内，可以：
        # 1. 调用工具
        # 2. 检索记忆
        # 3. 内部推理（不公开发言）
        
        # 记录思考日志
        self.whiteboard.add_think_log(
            self.agent.id,
            f"思考中... 剩余时间: {time_remaining:.1f}秒",
            "thought"
        )
        
        # 等待一段时间
        await asyncio.sleep(min(5, time_remaining))
    
    def _calculate_sleep_time(self) -> float:
        """计算休眠时间"""
        # 检查讨论活跃度
        idle_time = self.whiteboard.get_idle_time()
        
        if idle_time < 5:
            # 活跃讨论
            return self._active_sleep
        elif idle_time > 15:
            # 冷清讨论
            return self._idle_sleep
        else:
            return self._base_sleep


class AgentLoopManager:
    """管理所有代理的独立循环"""
    
    def __init__(self, whiteboard: Whiteboard, speech_controller: Optional[Any] = None):
        self.whiteboard = whiteboard
        self.speech_controller = speech_controller
        self._loops: Dict[str, AsyncAgentLoop] = {}
    
    def add_agent(self, agent: Agent) -> AsyncAgentLoop:
        """添加代理循环"""
        loop = AsyncAgentLoop(agent, self.whiteboard, self.speech_controller)
        self._loops[agent.id] = loop
        return loop
    
    async def start_all(self):
        """启动所有代理循环"""
        for loop in self._loops.values():
            await loop.start()
    
    async def stop_all(self):
        """停止所有代理循环"""
        for loop in self._loops.values():
            await loop.stop()
    
    async def pause_agent(self, agent_id: str):
        """暂停特定代理"""
        if agent_id in self._loops:
            await self._loops[agent_id].pause()
    
    async def resume_agent(self, agent_id: str):
        """恢复特定代理"""
        if agent_id in self._loops:
            await self._loops[agent_id].resume()
    
    def get_loop(self, agent_id: str) -> Optional[AsyncAgentLoop]:
        """获取代理循环"""
        return self._loops.get(agent_id)