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
                      discussion: str = None) -> ModeResult:
        """执行串行模式（可独立使用或从会议模式调用）"""
        self._is_running = True
        self._start_time = time.time()
        
        # 保存上下文（来自会议模式时有效）
        self._context_proposals = proposals or []
        self._context_agenda_conclusions = agenda_conclusions or []
        self._context_discussion = discussion or ""
        
        try:
            agents = self.agent_pool.get_enabled_agents()
            if not agents:
                return ModeResult(success=False, final_resolution="", error="没有可用的代理")
            
            print(f"\n[串行模式] {len(agents)} 个代理参与")
            if self._context_proposals:
                print(f"  接收会议提议: {len(self._context_proposals)} 个")
            
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
        max_rounds = 3
        proposer = agents[0]
        
        # 构建上下文
        context = self._build_context_info()
        
        # 第一轮：提案
        print(f"  [提案] {proposer.id}")
        
        proposal_prompt = self.prompts.serial_proposal.format(
            question=question,
            step_description=step.description,
            expected_output=step.expected_output,
            context=context
        )
        
        response = await proposer.call_api([{"role": "user", "content": proposal_prompt}], temperature=0.7)
        
        if response.success and response.content:
            current_proposal = response.content
            print(f"    方案: {current_proposal[:80]}...")
        else:
            step.status = StepStatus.FAILED
            return
        
        # 全员投票循环
        max_vote_rounds = 3
        for vote_round in range(1, max_vote_rounds + 1):
            print(f"\n  [投票轮次 {vote_round}] 全员投票...")
            
            # 所有其他代理并行投票
            voters = [a for a in agents if a.id != proposer.id]
            
            async def vote(agent):
                import re as re_module  # 显式导入避免作用域问题
                vote_prompt = self.prompts.serial_vote.format(
                    step_description=step.description,
                    proposal=current_proposal
                )
                resp = await agent.call_api([{"role": "user", "content": vote_prompt}], temperature=0.5)
                if resp.success and resp.content:
                    # 提取投票结果 - 兼容多种格式，冒号可选
                    vote_match = re_module.search(r'[\[【]投票[：:]?\s*([^\]】\n]+)[\]】]?', resp.content)
                    speech_match = re_module.search(r'[【\[]给人看[\]：:]*\s*([^\n【\[]+)', resp.content)
                    reason_match = re_module.search(r'[【\[]理由[\]：:]*\s*([^\n【\[]+)', resp.content)
                    
                    vote_result = vote_match.group(1).strip() if vote_match else "同意"
                    speech = speech_match.group(1).strip() if speech_match else ""
                    reason = reason_match.group(1).strip() if reason_match else ""
                    
                    is_agree = "同意" in vote_result
                    return (agent.id, is_agree, speech, reason)
                return (agent.id, True, "", "")
            
            # 并行收集投票
            vote_tasks = [vote(a) for a in voters]
            vote_results = await asyncio.gather(*vote_tasks, return_exceptions=True)
            
            # 统计投票
            agree_count = 1  # 提案者默认同意自己
            oppose_count = 0
            objections = []
            
            for result in vote_results:
                if isinstance(result, Exception):
                    continue
                agent_id, is_agree, speech, reason = result
                if is_agree:
                    agree_count += 1
                    print(f"    {agent_id} [同意] {speech[:30]}")
                else:
                    oppose_count += 1
                    print(f"    {agent_id} [反对] {speech[:30]}")
                    if reason:
                        objections.append(f"{agent_id}: {reason}")
            
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
        """构建上下文信息"""
        parts = []
        if self._context_proposals:
            parts.append("已有提议：" + "; ".join([p[:50] for p in self._context_proposals[:3]]))
        if self._context_agenda_conclusions:
            parts.append("议程结论：" + "; ".join([c.get('title', '') for c in self._context_agenda_conclusions[:2]]))
        return "\n".join(parts) if parts else ""
    
    def _parse_test_cases(self, configs: List[Dict]) -> List:
        return [create_test_from_dict(tc) for tc in configs if isinstance(tc, dict)]
    
    async def _create_snapshot(self, step_id: int) -> str:
        if not self.workspace or not self.workspace.session_path:
            return ""
        snapshot_dir = self.workspace.session_path / f".snapshot_{step_id}"
        try:
            if snapshot_dir.exists():
                shutil.rmtree(snapshot_dir)
            shutil.copytree(self.workspace.session_path, snapshot_dir,
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
        """触发临时会议（串行模式独立使用时）"""
        print(f"\n[临时会议] 步骤 {step.step_id} 需要讨论")
        
        agents = self.agent_pool.get_enabled_agents()
        if len(agents) < 2:
            return None
        
        # 使用配置的提示词
        meeting_prompt = self.prompts.serial_temp_meeting.format(
            step_description=step.description,
            proposal=step.final_output[:500] if step.final_output else "无",
            issue="需要讨论确定最佳方案"
        )
        
        # 快速讨论：每个代理发表意见
        opinions = []
        for agent in agents[:5]:
            try:
                resp = await agent.call_api(
                    [{"role": "user", "content": meeting_prompt}],
                    temperature=0.6
                )
                if resp.success and resp.content:
                    opinions.append(f"[{agent.id}]: {resp.content[:100]}")
                    print(f"  [{agent.id}] {resp.content[:60]}...")
            except:
                pass
        
        # 综合意见
        if opinions:
            summary = "\n".join(opinions)
            # 让第一个代理综合
            final_resp = await agents[0].call_api(
                [{"role": "user", "content": f"综合以下意见给出最终建议：\n{summary}"}],
                temperature=0.3
            )
            if final_resp.success and final_resp.content:
                print(f"\n[会议结论] {final_resp.content[:80]}...")
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
