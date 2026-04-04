"""串行模式 - 代理轮流发言 + 自动化测试 + 用户插话支持"""
import asyncio
import time
import json
import re
import shutil
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass, field
from pathlib import Path
from enum import Enum

from modes.base import BaseMode, ModeResult
from agent import Agent
from whiteboard import Whiteboard, TaskStep
from config_loader import SerialConfig, PromptsConfig
from event_bus import EventBus, Event, EventType, get_event_bus, create_event
from test_runner import TestRunner, TestCase, TestResult, create_test_from_dict


class StepStatus(Enum):
    """步骤状态"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLBACK = "rollback"


@dataclass
class AgentStepResult:
    """代理步骤结果"""
    agent_id: str
    output: str
    test_passed: bool
    test_message: str
    duration: float
    retried: int = 0


@dataclass
class StepExecution:
    """步骤执行记录"""
    step_id: int
    description: str
    agents_results: List[AgentStepResult] = field(default_factory=list)
    status: StepStatus = StepStatus.PENDING
    snapshot_path: Optional[str] = None
    final_output: str = ""


@dataclass
class SerialConfig:
    """串行模式配置"""
    step_timeout_sec: int = 60
    max_retries: int = 3
    trigger_meeting_keywords: List[str] = field(default_factory=list)
    agents_per_step: int = 3        # 每步骤参与代理数
    enable_testing: bool = True     # 是否启用自动化测试
    enable_snapshot: bool = True    # 是否启用快照
    auto_switch_threshold: float = 0.5  # 测试失败率阈值，触发切换到会议模式


class EnhancedSerialMode(BaseMode):
    """增强串行模式 - 支持轮流发言、测试、快照、用户插话"""
    
    mode_name = "serial"
    
    # 触发临时会议的关键词
    TRIGGER_MEETING_KEYWORDS = [
        "不确定", "有分歧", "需要讨论", "无法确定", "建议",
        "多种方案", "争议", "需要帮助", "[REQUEST_MEETING]"
    ]
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.serial_config: SerialConfig = self.config.serial
        self.temp_meeting_config = self.config.temp_meeting
        self.prompts: PromptsConfig = self.config.prompts
        
        # 事件总线
        self.event_bus = get_event_bus()
        
        # 测试运行器
        self.test_runner = TestRunner(str(self.workspace.session_path) if self.workspace else "./temp")
        
        # 执行状态
        self._steps: List[StepExecution] = []
        self._current_step_idx = 0
        self._agent_order: List[str] = []  # 代理发言顺序
        self._paused = False
        self._aborted = False
        self._user_inputs: asyncio.Queue = None
        
        # 快照
        self._snapshots: Dict[int, str] = {}  # step_id -> snapshot_path
        
        # 测试失败计数
        self._consecutive_failures = 0
        self._total_tests = 0
        self._failed_tests = 0
        
        # 订阅用户事件
        self.event_bus.subscribe_to_type(EventType.USER_INTERRUPT, self._on_user_interrupt)
    
    async def _on_user_interrupt(self, event: Event):
        """处理用户插话"""
        content = event.content.strip().lower()
        
        if content == "/pause":
            self._paused = True
            await self.event_bus.publish_async(create_event(
                event_type=EventType.SYSTEM,
                source_id="system",
                content="[暂停] 串行执行已暂停，输入 '继续' 恢复"
            ))
        elif content == "/abort":
            self._aborted = True
            await self.event_bus.publish_async(create_event(
                event_type=EventType.SYSTEM,
                source_id="system",
                content="[中止] 串行执行已中止"
            ))
        elif content in ["继续", "/continue", "resume"]:
            self._paused = False
        else:
            # 普通用户建议，注入到上下文
            await self.event_bus.publish_async(create_event(
                event_type=EventType.SUGGESTION,
                source_id="user",
                content=content,
                target_id=None
            ))
    
    async def execute(self, question: str) -> ModeResult:
        """执行串行模式"""
        self._is_running = True
        self._start_time = time.time()
        self._user_inputs = asyncio.Queue()
        
        try:
            # 兜底：检查是否有可用代理
            agents = self.agent_pool.get_enabled_agents()
            if not agents:
                return ModeResult(
                    success=False,
                    final_resolution="",
                    error="没有可用的代理，请检查配置"
                )
            
            # 设置代理顺序（按 ID 排序或按性格排序）
            self._agent_order = [a.id for a in agents]
            
            # 1. 任务拆解
            decomposed_steps = await self._decompose_task(question)
            if not decomposed_steps:
                decomposed_steps = [{
                    "step_id": 1,
                    "description": question,
                    "expected_output": "执行结果",
                    "test_cases": []
                }]
            
            # 初始化步骤执行记录
            self._steps = [
                StepExecution(
                    step_id=s.get("step_id", i+1),
                    description=s.get("description", f"步骤{i+1}")
                )
                for i, s in enumerate(decomposed_steps)
            ]
            
            # 2. 依次执行步骤
            for idx, step in enumerate(self._steps):
                if self._aborted:
                    break
                
                # 检查暂停
                while self._paused and not self._aborted:
                    await asyncio.sleep(0.5)
                
                if self._aborted:
                    break
                
                self._current_step_idx = idx
                step.status = StepStatus.RUNNING
                
                # 创建快照
                if self.serial_config.enable_snapshot:
                    step.snapshot_path = await self._create_snapshot(step.step_id)
                
                # 执行步骤（多代理轮流）
                await self._execute_step_with_agents(step, decomposed_steps[idx], question)
                
                # 检查是否需要切换到会议模式
                if self._should_switch_to_conference():
                    await self._switch_to_conference(question)
                    break
            
            # 3. 生成结果
            result = self._generate_result()
            self.whiteboard.set_final_resolution(result)
            
            return self._build_result()
            
        except Exception as e:
            return ModeResult(success=False, final_resolution="", error=str(e))
        finally:
            self._is_running = False
    
    async def _decompose_task(self, task: str) -> List[Dict]:
        """拆解任务，包含测试用例"""
        agents = self.agent_pool.get_enabled_agents()
        if not agents:
            return []
        
        agent = agents[0]
        
        prompt = self.prompts.serial_task_decomposition.format(task=task)
        
        response = await agent.call_api(
            [{"role": "user", "content": prompt}],
            temperature=0.3
        )
        
        if response.success:
            try:
                content = response.content
                start, end = content.find("["), content.rfind("]") + 1
                if start != -1 and end > start:
                    return json.loads(content[start:end])
            except:
                pass
        
        return []
    
    async def _execute_step_with_agents(self, step: StepExecution, step_config: Dict, question: str):
        """执行单个步骤，多代理轮流发言"""
        agents = self.agent_pool.get_enabled_agents()
        agents_per_step = getattr(self.serial_config, 'agents_per_step', 3)
        
        # 选择参与此步骤的代理
        participating_agents = []
        for i in range(agents_per_step):
            idx = (step.step_id - 1 + i) % len(agents)
            participating_agents.append(agents[idx])
        
        previous_output = self._get_previous_output()
        test_cases = self._parse_test_cases(step_config.get("test_cases", []))
        
        for turn, agent in enumerate(participating_agents):
            if self._aborted:
                break
            
            # 检查暂停
            while self._paused and not self._aborted:
                await asyncio.sleep(0.5)
            
            # 执行代理发言
            result = await self._execute_agent_turn(
                agent, step, turn, previous_output, question
            )
            
            # 运行测试
            if self.serial_config.enable_testing and test_cases:
                test_results = await self.test_runner.run_tests(
                    test_cases, result.output
                )
                
                # 检查测试结果
                all_passed = all(r.result == TestResult.PASS for r in test_results)
                result.test_passed = all_passed
                result.test_message = "; ".join(
                    r.message for r in test_results if r.result != TestResult.PASS
                )
                
                self._total_tests += len(test_results)
                self._failed_tests += sum(1 for r in test_results if r.result == TestResult.FAIL)
                
                if not all_passed:
                    self._consecutive_failures += 1
                    
                    # 重试逻辑
                    max_retries = getattr(self.serial_config, 'max_retries', 3)
                    if result.retried < max_retries:
                        result.retried += 1
                        
                        # 重新执行
                        await self.event_bus.publish_async(create_event(
                            event_type=EventType.SYSTEM,
                            source_id="system",
                            content=f"[警告] 步骤 {step.step_id} 测试失败，重试 ({result.retried}/{max_retries})"
                        ))
                        
                        # 回滚到快照
                        if step.snapshot_path:
                            await self._restore_snapshot(step.snapshot_path)
                        
                        # 重新执行此代理
                        result = await self._execute_agent_turn(
                            agent, step, turn, previous_output, question
                        )
                else:
                    self._consecutive_failures = 0
            else:
                result.test_passed = True
            
            step.agents_results.append(result)
            previous_output = result.output
            
            # 发布进度
            await self.event_bus.publish_async(create_event(
                event_type=EventType.SYSTEM,
                source_id="system",
                content=f"[OK] 步骤 {step.step_id} - {agent.id}: {'通过' if result.test_passed else '失败'}"
            ))
            
            # 检测是否需要临时会议
            if result.output and self._should_trigger_meeting(result.output):
                await self._trigger_temp_meeting(step, result.output)
        
        # 汇总步骤结果
        if step.agents_results:
            step.final_output = step.agents_results[-1].output
            step.status = StepStatus.COMPLETED if all(
                r.test_passed for r in step.agents_results
            ) else StepStatus.FAILED
    
    async def _execute_agent_turn(self, agent: Agent, step: StepExecution, 
                                   turn: int, previous_output: str, question: str) -> AgentStepResult:
        """执行单个代理的发言轮次"""
        start_time = time.time()
        
        # 构建上下文
        context = self._build_context(step, turn, previous_output, question)
        
        prompt = self.prompts.serial_step_speak.format(
            step_id=step.step_id,
            turn=turn + 1,
            description=step.description,
            expected_output=step.expected_output,
            previous=context
        )
        
        messages = [{"role": "user", "content": prompt}]
        tools = self._get_tool_schemas_for_agent(agent) if agent.supports_tools else None
        
        response = await agent.call_api(messages, tools=tools)
        
        output = ""
        if response.success:
            if response.tool_calls and agent.supports_tools:
                await agent.execute_tool_calls(response.tool_calls, self.whiteboard)
            output = response.content or ""
        
        return AgentStepResult(
            agent_id=agent.id,
            output=output,
            test_passed=True,  # 默认通过，后续测试会更新
            test_message="",
            duration=time.time() - start_time
        )
    
    def _build_context(self, step: StepExecution, turn: int, 
                       previous_output: str, question: str) -> str:
        """构建代理上下文"""
        lines = [f"原始问题：{question}", ""]
        
        # 之前步骤的结果
        for prev_step in self._steps[:self._current_step_idx]:
            if prev_step.final_output:
                lines.append(f"步骤 {prev_step.step_id} 结果：{prev_step.final_output[:200]}")
        
        # 当前步骤之前的代理发言
        if turn > 0:
            lines.append("")
            lines.append("本步骤之前的发言：")
            for result in step.agents_results:
                lines.append(f"[{result.agent_id}]: {result.output[:150]}")
        
        lines.append("")
        lines.append(f"最近的输出：{previous_output[:300] if previous_output else '无'}")
        
        return "\n".join(lines)
    
    def _get_previous_output(self) -> str:
        """获取最近的输出"""
        if not self._steps:
            return ""
        
        for step in reversed(self._steps):
            if step.final_output:
                return step.final_output
        
        return ""
    
    def _parse_test_cases(self, test_configs: List[Dict]) -> List[TestCase]:
        """解析测试用例配置"""
        return [create_test_from_dict(tc) for tc in test_configs if isinstance(tc, dict)]
    
    async def _create_snapshot(self, step_id: int) -> str:
        """创建工作区快照"""
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
        except Exception as e:
            return ""
    
    async def _restore_snapshot(self, snapshot_path: str):
        """恢复快照"""
        if not snapshot_path or not Path(snapshot_path).exists():
            return
        
        try:
            snapshot_dir = Path(snapshot_path)
            workspace_dir = self.workspace.session_path
            
            # 清空当前工作区
            for item in workspace_dir.iterdir():
                if item.name.startswith('.snapshot_'):
                    continue
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
            
            # 恢复快照内容
            for item in snapshot_dir.iterdir():
                if item.name.startswith('.snapshot_'):
                    continue
                dest = workspace_dir / item.name
                if item.is_dir():
                    shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)
        except Exception as e:
            pass
    
    def _should_trigger_meeting(self, output: str) -> bool:
        """检测是否需要触发临时会议"""
        keywords = getattr(self.serial_config, 'trigger_meeting_keywords', 
                          self.TRIGGER_MEETING_KEYWORDS)
        return any(kw in output for kw in keywords)
    
    async def _trigger_temp_meeting(self, step: StepExecution, issue: str):
        """触发临时会议"""
        await self.event_bus.publish_async(create_event(
            event_type=EventType.SYSTEM,
            source_id="system",
            content=f"[临时会议] 步骤 {step.step_id} 遇到问题，触发临时会议"
        ))
        
        # 简化版临时会议：让剩余代理快速投票
        agents = self.agent_pool.get_enabled_agents()
        resolutions = []
        
        for agent in agents[:3]:  # 只取前3个代理
            prompt = self.prompts.serial_quick_decision.format(
                step_id=step.step_id,
                issue=issue[:200],
                options=""
            )
            
            try:
                response = await asyncio.wait_for(
                    agent.call_api([{"role": "user", "content": prompt}], temperature=0.5),
                    timeout=15
                )
                if response.success and response.content:
                    resolutions.append(response.content[:100])
            except:
                continue
        
        if resolutions:
            await self.event_bus.publish_async(create_event(
                event_type=EventType.SYSTEM,
                source_id="system",
                content=f"[决议] 会议决议：{resolutions[0]}"
            ))
    
    def _should_switch_to_conference(self) -> bool:
        """检测是否应该切换到会议模式"""
        threshold = getattr(self.serial_config, 'auto_switch_threshold', 0.5)
        
        if self._total_tests == 0:
            return False
        
        fail_rate = self._failed_tests / self._total_tests
        return fail_rate > threshold or self._consecutive_failures >= 3
    
    async def _switch_to_conference(self, question: str):
        """切换到会议模式"""
        await self.event_bus.publish_async(create_event(
            event_type=EventType.SYSTEM,
            source_id="system",
            content="[切换] 测试失败率过高，自动切换到会议模式讨论"
        ))
        
        # 简化版：这里只是通知，实际切换由上层处理
        # 标记当前状态
        if self._steps:
            self._steps[self._current_step_idx].status = StepStatus.FAILED
    
    def _generate_result(self) -> str:
        """生成结果"""
        lines = ["=== 串行执行结果 ===", ""]
        
        completed = [s for s in self._steps if s.status == StepStatus.COMPLETED]
        failed = [s for s in self._steps if s.status == StepStatus.FAILED]
        
        lines.append(f"完成步骤：{len(completed)}/{len(self._steps)}")
        lines.append(f"测试统计：{self._total_tests} 个，失败 {self._failed_tests} 个")
        lines.append("")
        
        for step in self._steps:
            status_icon = "[OK]" if step.status == StepStatus.COMPLETED else "[FAIL]"
            lines.append(f"{status_icon} 步骤 {step.step_id}: {step.description[:40]}...")
            
            for result in step.agents_results:
                test_icon = "[OK]" if result.test_passed else "[FAIL]"
                lines.append(f"  [{result.agent_id}] {test_icon} {result.output[:50]}...")
        
        if failed:
            lines.append("")
            lines.append("失败步骤：")
            for step in failed:
                lines.append(f"  步骤 {step.step_id}: {step.description}")
        
        return "\n".join(lines)
    
    def _build_result(self) -> ModeResult:
        """构建结果对象"""
        messages = []
        for step in self._steps:
            for result in step.agents_results:
                messages.append({
                    "step_id": step.step_id,
                    "agent_id": result.agent_id,
                    "content": result.output,
                    "test_passed": result.test_passed,
                    "duration": result.duration
                })
        
        final = self._get_previous_output()
        
        return ModeResult(
            success=all(s.status == StepStatus.COMPLETED for s in self._steps),
            final_resolution=final,
            messages=messages,
            metadata={
                "total_steps": len(self._steps),
                "completed_steps": sum(1 for s in self._steps if s.status == StepStatus.COMPLETED),
                "test_stats": {
                    "total": self._total_tests,
                    "passed": self._total_tests - self._failed_tests,
                    "failed": self._failed_tests
                }
            }
        )
    
    def pause(self):
        """暂停执行"""
        self._paused = True
    
    def resume(self):
        """恢复执行"""
        self._paused = False
    
    def abort(self):
        """中止执行"""
        self._aborted = True
    
    def _get_tool_schemas_for_agent(self, agent) -> List[Dict]:
        """获取代理可用工具模式"""
        if not self.tool_router:
            return []
        return self.tool_router.get_tool_schemas_for_agent(agent.id)


def format_serial_output(result: ModeResult) -> str:
    """格式化串行输出"""
    lines = ["[串行执行结果]", ""]
    
    step_outputs = {}
    for msg in result.messages:
        step_id = msg.get("step_id", 0)
        if step_id not in step_outputs:
            step_outputs[step_id] = []
        step_outputs[step_id].append(msg)
    
    for step_id, msgs in sorted(step_outputs.items()):
        lines.append(f"步骤 {step_id}：")
        for msg in msgs:
            status = "[通过]" if msg.get("test_passed", True) else "[失败]"
            agent = msg.get("agent_id", "?")
            content = msg.get("content", "")[:60]
            lines.append(f"  {status} [{agent}]: {content}")
    
    lines.append("")
    lines.append(f"[最终结果] {result.final_resolution[:300] if result.final_resolution else '无'}")
    
    stats = result.metadata.get("test_stats", {})
    if stats:
        lines.append(f"\n[测试统计] {stats.get('passed', 0)}/{stats.get('total', 0)} 通过")
    
    return "\n".join(lines)
