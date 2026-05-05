import asyncio
import time
import json
import re
import shutil
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field
from pathlib import Path
from enum import Enum

from modes.base import BaseMode, ModeResult
from agent import Agent
from whiteboard import Whiteboard
from config_loader import SerialConfig, PromptsConfig
from event_bus import EventBus, Event, EventType, get_event_bus, create_event
from test_runner import TestRunner, TestResult, create_test_from_dict


class StepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLBACK = "rollback"


@dataclass
class AgentStepResult:
    agent_id: str
    output: str
    test_passed: bool
    test_message: str
    duration: float
    retried: int = 0


@dataclass
class StepExecution:
    step_id: int
    description: str
    expected_output: str = ""
    agents_results: List[AgentStepResult] = field(default_factory=list)
    status: StepStatus = StepStatus.PENDING
    snapshot_path: Optional[str] = None
    final_output: str = ""


class EnhancedSerialMode(BaseMode):
    """增强串行模式 - 提案反馈修改循环 + 测试 + 快照 + 临时会议"""
    
    mode_name = "serial"
    
    TRIGGER_MEETING_KEYWORDS = [
        "不确定", "有分歧", "需要讨论", "无法确定", "建议",
        "多种方案", "争议", "需要帮助", "[REQUEST_MEETING]"
    ]
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.serial_config: SerialConfig = self.config.serial
        self.temp_meeting_config = self.config.temp_meeting
        self.prompts: PromptsConfig = self.config.prompts
        
        self.event_bus = get_event_bus()
        self.test_runner = TestRunner(str(self.workspace.session_path) if self.workspace else "./temp")
        
        self._steps: List[StepExecution] = []
        self._current_step_idx = 0
        self._paused = False
        self._aborted = False
        
        self._snapshots: Dict[int, str] = {}
        self._consecutive_failures = 0
        self._total_tests = 0
        self._failed_tests = 0
        
        # 会议模式传递的上下文（可选）
        self._context_proposals: List[str] = []
        self._context_agenda_conclusions: List[Dict] = []
        self._context_discussion: str = ""
        self._context_review_synthesis: str = ""  # 复盘综合结果
        
        # 临时会议状态
        self._in_temp_meeting = False
        self._temp_meeting_result: Optional[str] = None
        
        self.event_bus.subscribe_to_type(EventType.USER_INTERRUPT, self._on_user_interrupt)
    
    async def _on_user_interrupt(self, event: Event):
        content = event.content.strip().lower()
        if content == "/pause":
            self._paused = True
        elif content == "/resume":
            self._paused = False
        elif content == "/abort":
            self._aborted = True
    
    async def execute(self, question: str, proposals: List[str] = None,
                      agenda_conclusions: List[Dict] = None,
                      discussion: str = None,
                      review_synthesis: str = None) -> ModeResult:
        """执行串行模式（可独立使用或从会议模式调用）"""
        self._is_running = True
        self._start_time = time.time()
        
        # 保存上下文（来自会议模式时有效）
        self._context_proposals = proposals or []
        self._context_agenda_conclusions = agenda_conclusions or []
        self._context_discussion = discussion or ""
        self._context_review_synthesis = review_synthesis or ""  # 复盘结果
        
        try:
            agents = self.agent_pool.get_enabled_agents()
            if not agents:
                return ModeResult(success=False, final_resolution="", error="没有可用的代理")
            
            print(f"\n[串行模式] {len(agents)} 个代理参与")
            
            # === 新增：为代理生成不同风格的提示词 ===
            print("\n[风格提示词] 正在为代理生成不同风格的执行提示词...")
            await self._generate_agent_style_prompts(question)
            
            # === 新增：如果从会议模式传入讨论信息，生成总结 ===
            if self._context_discussion:
                print(f"\n[会议总结] 从会议模式接收了讨论信息，正在生成总结...")
                summary = await self._generate_conference_summary(question)
                if summary:
                    print(f"\n[会议讨论总结] {summary[:200]}...")
                    # 将总结添加到上下文，供后续步骤使用
                    self._context_review_synthesis = summary
            
            if self._context_proposals:
                print(f"  接收会议提议: {len(self._context_proposals)} 个")
            if self._context_review_synthesis:
                print(f"  接收复盘结果")
            
            # 1. 任务拆解
            print("\n[任务拆解]")
            decomposed_steps = await self._decompose_task(question)
            if not decomposed_steps:
                decomposed_steps = [{
                    "step_id": 1,
                    "description": question,
                    "expected_output": "执行结果",
                    "test_cases": []
                }]
            
            self._steps = [
                StepExecution(
                    step_id=s.get("step_id", i+1),
                    description=s.get("description", f"步骤{i+1}"),
                    expected_output=s.get("expected_output", "执行结果")
                )
                for i, s in enumerate(decomposed_steps)
            ]
            print(f"  拆解为 {len(self._steps)} 个步骤")
            
            # 2. 依次执行步骤
            for idx, step in enumerate(self._steps):
                if self._aborted:
                    break
                
                while self._paused and not self._aborted:
                    await asyncio.sleep(0.5)
                
                if self._aborted:
                    break
                
                self._current_step_idx = idx
                step.status = StepStatus.RUNNING
                
                print(f"\n[步骤 {step.step_id}] {step.description[:50]}...")
                
                # 创建快照
                if self.serial_config.enable_snapshot:
                    step.snapshot_path = await self._create_snapshot(step.step_id)
                
                # 执行步骤
                await self._execute_step(step, decomposed_steps[idx], question)
                
                # 检查是否需要临时会议
                if self._should_trigger_meeting(step.final_output):
                    meeting_result = await self._trigger_temp_meeting(step, question)
                    if meeting_result:
                        # 会议后重新执行步骤
                        step.status = StepStatus.RUNNING
                        await self._execute_step(step, decomposed_steps[idx], question)
            
            # 3. 生成结果
            result = self._generate_result()
            self.whiteboard.set_final_resolution(result)
            
            # 4. 保存会话数据
            self._save_session_data(question)
            
            return self._build_result()
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            return ModeResult(success=False, final_resolution="", error=str(e))
        finally:
            self._is_running = False
    
    async def _generate_agent_style_prompts(self, question: str):
        """为代理生成不同风格的执行提示词 - 类似会议模式的立场提示词"""
        agents = self.agent_pool.get_enabled_agents()
        if len(agents) < 2:
            return
        
        total_agents = len(agents)
        print(f"  共 {total_agents} 个代理参与风格生成")
        
        # 第一阶段：并行生成风格提示词
        print("\n[风格生成] AI动态生成独特执行风格...")
        
        async def generate_style(agent, existing_styles_text=""):
            """单个代理动态生成风格"""
            existing_info = ""
            if existing_styles_text:
                existing_info = f"""
【已生成的风格】（你应该选择不同的执行风格，形成互补）
{existing_styles_text}
"""
            
            style_prompt = f"""请为以下任务生成一个独特的执行风格提示词。

【任务】{question}

【你的性格】{agent.get_personality_prompt()}
{existing_info}
【要求】
1. 生成一句15字以内的执行风格提示词，体现你的独特执行方式
2. {"你必须与已有风格不同，形成互补" if existing_styles_text else "根据任务性质，选择严谨/创新/务实/批判等风格"}
3. 风格要鲜明，体现你的执行特点
4. 直接输出你的风格提示词，不要解释

【示例】
- "严谨细致，步步验证"
- "大胆创新，尝试新方法"
- "务实高效，关注结果"
- "批判思考，检查漏洞"
"""
            try:
                response = await agent.call_api(
                    [{"role": "user", "content": style_prompt}],
                    temperature=1.0
                )
                
                if response.success and response.content:
                    content = response.content.strip().strip('"\'""''')
                    if '\n' in content:
                        content = content.split('\n')[0].strip()
                    if len(content) > 25:
                        content = content[:25]
                    return (agent.id, content)
            except:
                pass
            return (agent.id, "独立思考，理性执行")
        
        # 第一轮：并行生成风格
        tasks = [generate_style(agent) for agent in agents]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 收集结果
        final_styles = {}
        styles_list = []
        for result in results:
            if result and not isinstance(result, Exception):
                agent_id, style = result
                final_styles[agent_id] = style
                styles_list.append({"agent_id": agent_id, "style": style})
                print(f"  [{agent_id}] {style}")
        
        # 第二阶段：检查重复并重新生成（并行）
        print("\n[风格去重] 检查并调整重复风格...")
        
        style_counts = {}
        for s in styles_list:
            key = s["style"][:10]
            style_counts[key] = style_counts.get(key, 0) + 1
        
        need_regenerate = []
        for s in styles_list:
            key = s["style"][:10]
            if style_counts[key] > 1:
                need_regenerate.append(s["agent_id"])
        
        if need_regenerate:
            print(f"  发现 {len(need_regenerate)} 个重复风格，重新生成...")
            
            # 构建已存在的风格文本
            existing_styles_text = "\n".join([
                f"  - {s['agent_id']}: {s['style']}"
                for s in styles_list
                if s['agent_id'] not in need_regenerate
            ])
            
            async def regenerate_style(agent_id):
                for agent in agents:
                    if agent.id == agent_id:
                        response = await agent.call_api(
                            [{"role": "user", "content": f"生成一个与已有风格完全不同的执行风格提示词：\n任务：{question}\n\n已有风格：\n{existing_styles_text}\n\n直接输出新风格（15字内）"}],
                            temperature=1.2
                        )
                        if response.success and response.content:
                            new_style = response.content.strip().strip('"\'""''')[:25]
                            if '\n' in new_style:
                                new_style = new_style.split('\n')[0].strip()
                            return (agent_id, new_style)
                return None
            
            regen_tasks = [regenerate_style(aid) for aid in need_regenerate]
            regen_results = await asyncio.gather(*regen_tasks, return_exceptions=True)
            
            for result in regen_results:
                if result and not isinstance(result, Exception):
                    agent_id, new_style = result
                    if agent_id:
                        final_styles[agent_id] = new_style
                        print(f"  [{agent_id}] → {new_style}")
        
        # 第三阶段：验证风格多样性
        print("\n[风格验证] 检查风格多样性...")
        
        unique_styles = set()
        for style in final_styles.values():
            unique_styles.add(style[:8])
        
        diversity_ratio = len(unique_styles) / len(final_styles) if final_styles else 0
        print(f"  风格多样性: {diversity_ratio:.1%} ({len(unique_styles)}/{len(final_styles)} 独特)")
        
        if diversity_ratio < 0.7:
            print("  [警告] 风格多样性不足")
        else:
            print("  [通过] 风格多样性满足要求")
        
        # 输出最终结果
        print("\n【风格提示词分配结果】")
        for agent in agents:
            if agent.id in final_styles:
                print(f"  {agent.id}: {final_styles[agent.id]}")
        
        # 应用风格提示词
        for agent_id, style in final_styles.items():
            for agent in agents:
                if agent.id == agent_id:
                    agent.custom_style = style
                    break
        
        print(f"\n[完成] 已为 {len(final_styles)} 个代理分配专属执行风格提示词")
    
    async def _generate_conference_summary(self, question: str) -> Optional[str]:
        """生成会议讨论总结（如果从会议模式传入讨论信息）"""
        if not self._context_discussion:
            return None
        
        agents = self.agent_pool.get_enabled_agents()
        if not agents:
            return None
        
        # 计算讨论信息量，不传递原始讨论内容
        discussion_lines = self._context_discussion.count('\n') + 1
        discussion_chars = len(self._context_discussion)
        
        # 提取讨论的前500字作为参考（用于理解主题，不是完整内容）
        discussion_preview = self._context_discussion[:500]
        
        # 构建简洁的总结提示词，不包含完整讨论
        summary_prompt = f"""【串行模式 - 最终总结】

原始问题：{question}

会议讨论情况：
- 讨论消息数：约 {discussion_lines} 条
- 讨论内容量：约 {discussion_chars} 字符
- 讨论开头预览：{discussion_preview}...

复盘结果：
{self._context_review_synthesis or "无"}

已有提议：
{chr(10).join([f"- {p[:100]}" for p in self._context_proposals[:3]]) if self._context_proposals else "无"}

请生成最终总结报告，要求：
1. 总结核心结论（50-100字）
2. 列出关键观点（3-5条）
3. 指出后续行动方向（如适用）

注意：不要输出原始讨论内容，只输出精炼的总结。"""
        
        try:
            # 选择第一个代理生成总结
            agent = agents[0]
            
            # 获取总结代理的风格提示词
            agent_style = getattr(agent, 'custom_style', '全面总结')
            
            # 在提示词中加入风格指导
            style_enhanced_summary_prompt = f"""{summary_prompt}

【你的总结风格】{agent_style}
请根据你的总结风格特点来生成会议总结。"""
            
            response = await agent.call_api(
                [{"role": "user", "content": style_enhanced_summary_prompt}],
                temperature=0.3
            )
            
            if response.success and response.content:
                return response.content
        except Exception as e:
            print(f"[错误] 生成会议总结失败: {e}")
        
        return None
    
    async def _decompose_task(self, task: str) -> List[Dict]:
        """拆解任务"""
        agents = self.agent_pool.get_enabled_agents()
        if not agents:
            return []
        
        agent = agents[0]
        prompt = self.prompts.serial_task_decomposition.format(task=task)
        
        response = await agent.call_api([{"role": "user", "content": prompt}], temperature=0.3)
        
        if response.success:
            try:
                content = response.content
                start, end = content.find("["), content.rfind("]") + 1
                if start != -1 and end > start:
                    return json.loads(content[start:end])
            except:
                pass
        return []
    
    async def _execute_step(self, step: StepExecution, step_config: Dict, question: str):
        """执行单个步骤 - 提案反馈修改循环"""
        agents = self.agent_pool.get_enabled_agents()
        
        current_proposal = ""
        max_rounds = 100
        proposer = agents[0]
        
        # 构建上下文
        context = self._build_context_info()
        
        # 第一轮：提案
        print(f"  [提案] {proposer.id}")
        
        # 获取提案者的风格提示词
        proposer_style = getattr(proposer, 'custom_style', '严谨执行')
        
        proposal_prompt = self.prompts.serial_proposal.format(
            question=question,
            step_description=step.description,
            expected_output=step.expected_output,
            context=context
        )
        
        # 在提示词中加入风格指导
        style_enhanced_prompt = f"""{proposal_prompt}

【你的执行风格】{proposer_style}
请根据你的执行风格特点来提出方案。

【白板信息】
- 当前任务步骤：{step.description}
- 预期输出：{step.expected_output}
- 已有上下文：{context[:200] if context else '暂无'}
"""
        
        # 智能获取代理可用的工具
        # 1. 常用工具有明确列表（calculator, current_time等）
        # 2. 其他工具通过关键字匹配（根据步骤描述和任务内容）
        tools = None
        if hasattr(self, '_get_tool_schemas_for_agent'):
            # 传入查询文本，智能选择相关工具
            query_text = f"{step.description} {question}"
            tools = self._get_tool_schemas_for_agent(proposer, query=query_text)
        
        # 调用代理，支持工具调用
        messages = [{"role": "user", "content": style_enhanced_prompt}]
        
        for tool_iteration in range(5):  # 最多5轮工具调用
            response = await proposer.call_api(
                messages=messages,
                tools=tools if tools else None,
                tool_choice="auto" if tools else "none",
                temperature=0.7
            )
            
            if not response.success:
                step.status = StepStatus.FAILED
                return
            
            # 如果没有工具调用，获取最终结果
            if not response.tool_calls:
                current_proposal = response.content
                break
            
            # 执行工具调用
            print(f"    [工具调用] {len(response.tool_calls)} 个工具")
            tool_results = await proposer.execute_tool_calls(
                response.tool_calls,
                self.whiteboard
            )
            
            # 将工具结果添加到消息中
            messages.append({
                "role": "assistant",
                "content": response.content,
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["arguments"])
                        }
                    }
                    for tc in response.tool_calls
                ]
            })
            
            for tr in tool_results:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tr["tool_call_id"],
                    "name": tr["name"],
                    "content": str(tr["result"])
                })
        
        if current_proposal:
            print(f"    方案: {current_proposal[:80]}...")
        else:
            step.status = StepStatus.FAILED
            return
        
        # 全员投票循环
        max_vote_rounds = 100
        for vote_round in range(1, max_vote_rounds + 1):
            print(f"\n  [投票轮次 {vote_round}] 串行投票...")
            
            # 串行投票：每个代理轮流投票，能看到之前代理的观点
            voters = [a for a in agents if a.id != proposer.id]
            
            agree_count = 1  # 提案者默认同意自己
            oppose_count = 0
            objections = []
            previous_votes = []  # 记录之前代理的投票结果
            
            for voter in voters:
                vote_prompt = self.prompts.serial_vote.format(
                    step_description=step.description,
                    proposal=current_proposal
                )
                
                # 获取投票代理的风格提示词
                agent_style = getattr(voter, 'custom_style', '理性分析')
                
                # 构建之前代理投票的上下文
                previous_context = ""
                if previous_votes:
                    previous_context = "\n【其他代理已投观点】\n" + "\n".join([
                        f"  - {v['agent_id']} [{v['vote']}]: {v['speech'][:50]}"
                        for v in previous_votes[-5:]  # 只显示最近5个，避免太长
                    ])
                    previous_context += "\n（请勿重复上述观点，提出你的独特见解）"
                
                # 在提示词中加入风格指导和之前的观点
                style_enhanced_vote_prompt = f"""{vote_prompt}

【你的执行风格】{agent_style}
请根据你的执行风格特点来投票和提出意见。
{previous_context}"""
                
                resp = await voter.call_api([{"role": "user", "content": style_enhanced_vote_prompt}], temperature=0.5)
                
                if resp.success and resp.content:
                    # 提取投票结果
                    vote_match = re.search(r'[\[【]投票[：:]?\s*([^\]】\n]+)[\]】]?', resp.content)
                    speech_match = re.search(r'[【\[]给人看[\]：:]*\s*([^\n【\[\]】]+)', resp.content)
                    reason_match = re.search(r'[【\[]理由[\]：:]*\s*([^\n【\[\]】]+)', resp.content)
                    
                    vote_result = vote_match.group(1).strip() if vote_match else "同意"
                    speech = speech_match.group(1).strip() if speech_match else ""
                    reason = reason_match.group(1).strip() if reason_match else ""
                    
                    # 清理多余的括号
                    speech = speech.strip('【】[]').strip()
                    reason = reason.strip('【】[]').strip()
                    
                    is_agree = "同意" in vote_result
                    
                    # 记录这次投票
                    previous_votes.append({
                        "agent_id": voter.id,
                        "vote": "同意" if is_agree else "反对",
                        "speech": speech,
                        "reason": reason
                    })
                    
                    # 只显示给人看的内容，不显示理由
                    if is_agree:
                        agree_count += 1
                        print(f"    {voter.id} [同意] {speech}")
                    else:
                        oppose_count += 1
                        print(f"    {voter.id} [反对] {speech}")
                        if reason:
                            objections.append(f"{voter.id}: {reason}")
                else:
                    # 投票失败，默认同意
                    agree_count += 1
                    previous_votes.append({
                        "agent_id": voter.id,
                        "vote": "同意",
                        "speech": "",
                        "reason": ""
                    })
            
            total = len(agents)
            print(f"\n  [投票结果] 同意 {agree_count}/{total}，反对 {oppose_count}/{total}")
            
            # 全员通过
            if oppose_count == 0:
                print(f"  ✓ 全员通过！")
                break
            
            # 有人反对，需要修改
            if vote_round < max_vote_rounds:
                print(f"  [修改] {proposer.id} 根据 {oppose_count} 条反对意见修改方案...")
                
                revise_prompt = self.prompts.serial_revise.format(
                    proposal=current_proposal,
                    objections="\n".join(objections),
                    oppose_count=oppose_count,
                    total_count=total
                )
                
                resp = await proposer.call_api([{"role": "user", "content": revise_prompt}], temperature=0.5)
                if resp.success and resp.content:
                    current_proposal = resp.content
                    print(f"    新方案: {current_proposal[:80]}...")
            else:
                print(f"  ✗ 达到最大投票轮次，方案未通过")
                step.status = StepStatus.FAILED
                return
        
        # 记录结果
        step.final_output = current_proposal
        step.agents_results.append(AgentStepResult(
            agent_id=proposer.id,
            output=current_proposal,
            test_passed=True,
            test_message="",
            duration=0
        ))
        step.status = StepStatus.COMPLETED
    
    def _build_context_info(self) -> str:
        """构建上下文信息 - 只包含关键信息，避免会议讨论残留"""
        parts = []
        
        # 1. 会议模式传递的核心上下文（总结好的，不是原始讨论）
        if self._context_review_synthesis:
            # 复盘总结是精炼的内容，可以包含
            parts.append("会议总结：" + self._context_review_synthesis[:200])
        
        if self._context_proposals:
            # 只显示提议的标题，不显示完整内容
            parts.append("会议提议：" + str(len(self._context_proposals)) + " 个")
        
        if self._context_agenda_conclusions:
            # 只显示议程数量
            parts.append("议程结论：" + str(len(self._context_agenda_conclusions)) + " 个")
        
        # 2. 当前决议（如果有）
        try:
            final_resolution = self.whiteboard.get_final_resolution()
            if final_resolution:
                parts.append("当前决议：" + final_resolution[:100])
        except Exception:
            pass
        
        # 3. 工作空间文件（只显示数量）
        try:
            workspace_files = self.whiteboard.get_workspace_files()
            if workspace_files:
                parts.append("工作空间文件：" + str(len(workspace_files)) + " 个")
        except Exception:
            pass
        
        # 注意：不再读取白板上的所有消息和工具结果
        # 避免会议模式的讨论内容污染串行模式
        
        return "\n".join(parts) if parts else ""
    
    def _parse_test_cases(self, configs: List[Dict]) -> List:
        return [create_test_from_dict(tc) for tc in configs if isinstance(tc, dict)]
    
    async def _create_snapshot(self, step_id: int) -> str:
        if not self.workspace or not self.workspace.session_path:
            return ""
        session_path = Path(self.workspace.session_path)
        snapshot_dir = session_path / f".snapshot_{step_id}"
        try:
            if snapshot_dir.exists():
                shutil.rmtree(snapshot_dir)
            shutil.copytree(session_path, snapshot_dir,
                          ignore=shutil.ignore_patterns('.snapshot_*'))
            self._snapshots[step_id] = str(snapshot_dir)
            return str(snapshot_dir)
        except:
            return ""
    
    async def _restore_snapshot(self, snapshot_path: str):
        if not snapshot_path or not self.workspace:
            return
        try:
            if Path(snapshot_path).exists():
                shutil.rmtree(self.workspace.session_path)
                shutil.copytree(snapshot_path, self.workspace.session_path)
        except:
            pass
    
    def _should_trigger_meeting(self, content: str) -> bool:
        """检查是否需要临时会议"""
        if not content:
            return False
        content_lower = content.lower()
        return any(kw in content_lower for kw in self.TRIGGER_MEETING_KEYWORDS)
    
    async def _trigger_temp_meeting(self, step: StepExecution, question: str) -> Optional[str]:
        """触发临时会议（串行模式独立使用时）- 代理轮流讨论直到共识"""
        print(f"\n[临时会议] 步骤 {step.step_id} 需要讨论")
        
        agents = self.agent_pool.get_enabled_agents()
        if len(agents) < 2:
            return None
        
        # 会议议题
        issue = f"步骤：{step.description}\n当前输出：{step.final_output[:300] if step.final_output else '无'}\n问题：需要讨论确定最佳方案"
        
        # 收集所有发言
        all_speeches = []
        max_rounds = 3  # 最多3轮讨论
        
        for round_num in range(1, max_rounds + 1):
            print(f"\n  [第{round_num}轮讨论]")
            round_speeches = []
            
            for agent in agents:
                # 构建上下文：之前的发言
                context = ""
                if all_speeches:
                    context = "\n".join([f"[{s['agent']}]: {s['content'][:100]}" for s in all_speeches[-5:]])
                
                prompt = f"""【临时会议】请针对以下问题发表你的看法。

{issue}

【已有发言】
{context if context else '（暂无）'}

【要求】
1. 必须针对已有观点进行回应（支持/反对/补充）
2. 提出具体建议，不能说空话
3. 如果同意某个观点，说明为什么同意
4. 如果反对，提出替代方案

直接输出你的看法（口语化，像真人讨论）："""
                
                try:
                    resp = await agent.call_api([{"role": "user", "content": prompt}], temperature=0.7)
                    if resp.success and resp.content:
                        content = resp.content.strip()
                        round_speeches.append({"agent": agent.id, "content": content})
                        all_speeches.append({"agent": agent.id, "content": content})
                        print(f"    [{agent.id}] {content[:80]}")
                except:
                    pass
            
            # 检查是否达成共识（所有人的发言都倾向一致）
            if len(round_speeches) >= 2:
                # 简单判断：如果最后2个发言都包含"同意"或"认可"，认为达成共识
                last_two = [s["content"] for s in round_speeches[-2:]]
                if all("同意" in c or "认可" in c or "支持" in c for c in last_two):
                    print(f"\n  [达成共识] 结束讨论")
                    break
        
        # 综合意见，给出结论
        if all_speeches:
            summary = "\n".join([f"[{s['agent']}]: {s['content'][:150]}" for s in all_speeches])
            final_prompt = f"""根据以下讨论，给出明确的结论和建议：

{summary}

【输出格式】
结论：（一句话总结共识）
建议：（具体可执行的建议）"""
            
            final_resp = await agents[0].call_api([{"role": "user", "content": final_prompt}], temperature=0.3)
            if final_resp.success and final_resp.content:
                print(f"\n[会议结论] {final_resp.content[:100]}")
                return final_resp.content
        
        return None
    
    def _generate_result(self) -> str:
        if not self._steps:
            return ""
        
        completed = [s for s in self._steps if s.status == StepStatus.COMPLETED]
        lines = [f"完成：{len(completed)}/{len(self._steps)}", ""]
        
        for step in self._steps:
            icon = "[OK]" if step.status == StepStatus.COMPLETED else "[FAIL]"
            lines.append(f"{icon} 步骤{step.step_id}: {step.description[:40]}")
            if step.final_output:
                lines.append(f"    {step.final_output[:80]}...")
        
        return "\n".join(lines)
    
    def _save_session_data(self, question: str):
        import os
        from datetime import datetime
        
        session_path = self.workspace.session_path if self.workspace else None
        if not session_path:
            return
        
        try:
            data = {
                "question": question,
                "timestamp": datetime.now().isoformat(),
                "steps": [
                    {
                        "step_id": s.step_id,
                        "description": s.description,
                        "status": s.status.value,
                        "output": s.final_output[:500] if s.final_output else ""
                    }
                    for s in self._steps
                ],
                "context": {
                    "proposals": self._context_proposals,
                    "agenda_conclusions": self._context_agenda_conclusions
                }
            }
            
            with open(os.path.join(session_path, "serial_result.json"), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"[保存] serial_result.json")
        except Exception as e:
            print(f"[警告] 保存失败: {e}")
    
    def _build_result(self) -> ModeResult:
        messages = []
        for step in self._steps:
            for r in step.agents_results:
                messages.append({
                    "step_id": step.step_id,
                    "agent_id": r.agent_id,
                    "content": r.output,
                    "test_passed": r.test_passed
                })
        
        final = self._steps[-1].final_output if self._steps else ""
        
        return ModeResult(
            success=all(s.status == StepStatus.COMPLETED for s in self._steps),
            final_resolution=final,
            messages=messages,
            stats={
                "total_steps": len(self._steps),
                "completed": sum(1 for s in self._steps if s.status == StepStatus.COMPLETED),
                "tests": {"total": self._total_tests, "failed": self._failed_tests}
            }
        )
    
    def pause(self):
        self._paused = True
    
    def resume(self):
        self._paused = False
    
    def abort(self):
        self._aborted = True


def format_serial_output(result: ModeResult) -> str:
    lines = ["串行模式结果", ""]
    if result.final_resolution:
        lines.append(result.final_resolution)
    if result.stats:
        lines.append(f"\n步骤：{result.stats.get('completed', 0)}/{result.stats.get('total_steps', 0)}")
        tests = result.stats.get('tests', {})
        if tests:
            lines.append(f"测试：{tests.get('total', 0)} 个，失败 {tests.get('failed', 0)} 个")
    return "\n".join(lines)
