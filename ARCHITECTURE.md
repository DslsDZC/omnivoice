# Omnivoice 系统架构

## 整体流程

```
用户输入
    │
    ▼
┌──────────────────────────────────────────────────────────────┐
│ main.py: CLI._process_input()                                │
│ 作用: 判断是命令还是问题                                       │
│   - 斜杠命令(/help等) → 执行对应操作                           │
│   - 普通问题 → 调用 run_session()                             │
└──────────────────────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────────────────────┐
│ main.py: Omnivoice.run_session(question)                     │
│ 作用: 创建新会话，准备运行环境                                 │
│   - workspace.create_session() → 创建会话目录                  │
│   - Whiteboard(session_id) → 创建共享白板                      │
│   - memory_manager.retrieve_relevant_memories() → 注入记忆     │
└──────────────────────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────────────────────┐
│ mode_decision.py: ModeDecisionMaker.vote()                   │
│ 作用: 让代理投票选择执行模式                                   │
│   - 每个代理分析问题特性                                       │
│   - 投票选择 conference/serial                                │
│   - 返回选择结果                                               │
└──────────────────────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────────────────────┐
│ main.py: _execute_mode(mode, question)                       │
│ 作用: 创建并执行对应模式                                       │
│   - conference → ConferenceMode.execute()                     │
│   - serial → SerialMode.execute()                             │
└──────────────────────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────────────────────┐
│ main.py: _print_result(mode, result)                         │
│ 作用: 格式化输出结果                                          │
│   - 会议模式 → format_conference_output()                      │
│   - 串行模式 → format_serial_output()                          │
└──────────────────────────────────────────────────────────────┘
```

---

## 会议模式 (Conference Mode)

### 入口函数
```python
# modes/conference.py
async def execute(self, question: str) -> ModeResult
```

### 详细流程

```
ConferenceMode.execute(question)
    │
    ▼
┌───────────────────────────────────────────────────────────────┐
│ _initialize(question)                                         │
│ 文件: conference.py                                           │
│ 作用: 初始化代理状态、重置计数器                                │
│   - 创建 _agent_states 字典                                    │
│   - 重置 _should_stop, _end_votes, _ended_agents              │
└───────────────────────────────────────────────────────────────┘
    │
    ▼
┌───────────────────────────────────────────────────────────────┐
│ _assess_task_complexity(question)                             │
│ 文件: conference.py                                           │
│ 作用: 评估问题复杂度，调整讨论强度参数                           │
│   - 调用代理分析问题                                           │
│   - 更新 intensity.update_task_complexity()                   │
└───────────────────────────────────────────────────────────────┘
    │
    ▼
┌───────────────────────────────────────────────────────────────┐
│ _generate_agent_stance_prompts(question)                      │
│ 文件: conference.py                                           │
│ 作用: 为每个代理生成立场提示词                                  │
│   - 并行调用各代理生成立场                                      │
│   - 检查重复立场并去重                                         │
│   - 将立场指令存入 _agent_states[agent_id].stance_instruction  │
└───────────────────────────────────────────────────────────────┘
    │
    ▼
┌───────────────────────────────────────────────────────────────┐
│ _generate_agenda(question)                                    │
│ 文件: conference.py                                           │
│ 作用: 生成会议议程                                             │
│   - 代理并行提出议程建议                                        │
│   - 综合生成最终议程列表                                        │
│   - 返回 [{title, description, sub_questions}, ...]            │
└───────────────────────────────────────────────────────────────┘
    │
    ▼
┌───────────────────────────────────────────────────────────────┐
│ 【议程讨论循环】for each agenda item                           │
│                                                               │
│   ┌─────────────────────────────────────────────────────────┐ │
│   │ _vote_sub_questions(current_agenda, sub_questions)      │ │
│   │ 文件: conference.py                                      │ │
│   │ 作用: 投票选择要讨论的子问题                               │ │
│   │   - 代理对子问题投票                                      │ │
│   │   - 返回选中的子问题列表                                   │ │
│   └─────────────────────────────────────────────────────────┘ │
│       │                                                       │
│       ▼                                                       │
│   ┌─────────────────────────────────────────────────────────┐ │
│   │ _discussion_loop(question, current_agenda)              │ │
│   │ 文件: conference.py                                      │ │
│   │ 作用: 核心讨论循环                                        │ │
│   │   - 轮流让代理发言                                        │ │
│   │   - 检测 [INTERRUPT] 叫停信号                            │ │
│   │   - 检测 [END] 结束信号                                  │ │
│   │   - 检测 [EXPAND] 扩展子话题                             │ │
│   │   - 更新争吵强度 intensity                               │ │
│   │   - 直到投票结束或达到轮次上限                             │ │
│   └─────────────────────────────────────────────────────────┘ │
│       │                                                       │
│       ▼                                                       │
│   ┌─────────────────────────────────────────────────────────┐ │
│   │ _agenda_vote_and_review(agents, question, current_agenda)│ │
│   │ 文件: conference.py                                      │ │
│   │ 作用: 议程投票和复盘                                      │ │
│   │   - _extract_proposals() 提取方案                        │ │
│   │   - _rank_proposals() 方案排序                           │ │
│   │   - 最后议程时：                                         │ │
│   │     - _review_debate() 复盘讨论                          │ │
│   │     - _generate_final_conclusion() 进入串行模式总结       │ │
│   └─────────────────────────────────────────────────────────┘ │
│       │                                                       │
│       ▼                                                       │
│   whiteboard.advance_agenda() → 推进到下一议程                 │
│                                                               │
└───────────────────────────────────────────────────────────────┘
    │
    ▼
┌───────────────────────────────────────────────────────────────┐
│ _build_result()                                               │
│ 文件: conference.py                                           │
│ 作用: 构建返回结果                                             │
│   - 收集所有消息、观点、共识                                    │
│   - 生成复盘摘要                                               │
│   - 返回 ModeResult                                           │
└───────────────────────────────────────────────────────────────┘
```

### 核心子函数

#### _discussion_loop()
```python
# modes/conference.py
async def _discussion_loop(self, question: str, current_agenda: Dict)
```

内部流程:
```
while not _should_stop:
    │
    ├─→ _select_next_speaker()      # 选择下一个发言代理
    │
    ├─→ _agent_speak(agent, question, agenda)  # 代理发言
    │     │
    │     ├─→ _build_agent_prompt()  # 构建提示词
    │     │
    │     ├─→ agent.call_api()       # 调用 API
    │     │
    │     └─→ _process_agent_response()  # 处理响应
    │           │
    │           ├─→ 检测 [INTERRUPT]  # 叫停信号
    │           ├─→ 检测 [END]        # 结束信号
    │           ├─→ 检测 [EXPAND]     # 扩展话题
    │           └─→ 存储消息到白板
    │
    ├─→ _update_intensity()         # 更新讨论强度
    │
    └─→ _check_end_condition()      # 检查结束条件
```

#### _agent_speak()
```python
# modes/conference.py
async def _agent_speak(self, agent, question: str, current_agenda: Dict) -> str
```

作用: 让单个代理发言

#### _extract_proposals()
```python
# modes/conference.py
async def _extract_proposals(self, agents, messages) -> List[str]
```

作用: 从讨论消息中提取方案

#### _review_debate()
```python
# modes/conference.py
async def _review_debate(self, agents, ranked_proposals, question: str)
```

作用: 多代理复盘讨论，细化方案

#### _generate_final_conclusion()
```python
# modes/conference.py
async def _generate_final_conclusion(self, agents, question, current_agenda, ranked_proposals)
```

作用: 调用串行模式生成最终总结

---

## 串行模式 (Serial Mode)

### 入口函数
```python
# modes/serial.py
async def execute(self, question: str, proposals=None, 
                  agenda_conclusions=None, discussion=None,
                  review_synthesis=None) -> ModeResult
```

### 详细流程

```
SerialMode.execute(question, proposals, agenda_conclusions, 
                   discussion, review_synthesis)
    │
    ▼
┌───────────────────────────────────────────────────────────────┐
│ 保存上下文（来自会议模式时有效）                                 │
│   - _context_proposals = proposals                            │
│   - _context_agenda_conclusions = agenda_conclusions          │
│   - _context_discussion = discussion                          │
│   - _context_review_synthesis = review_synthesis              │
└───────────────────────────────────────────────────────────────┘
    │
    ▼
┌───────────────────────────────────────────────────────────────┐
│ _decompose_task(question)                                     │
│ 文件: serial.py                                               │
│ 作用: 拆解任务为步骤                                           │
│   - 调用代理分析任务                                           │
│   - 返回 [{step_id, description, expected_output}, ..]        │
└───────────────────────────────────────────────────────────────┘
    │
    ▼
┌───────────────────────────────────────────────────────────────┐
│ 【步骤执行循环】for each step in _steps:                       │
│                                                               │
│   ┌─────────────────────────────────────────────────────────┐ │
│   │ _execute_step(step, step_config, question)              │ │
│   │ 文件: serial.py                                          │ │
│   │ 作用: 执行单个步骤                                        │ │
│   │                                                          │ │
│   │   ┌───────────────────────────────────────────────────┐ │ │
│   │   │ 1. 提案阶段                                         │ │ │
│   │   │   - proposer 提出方案                               │ │ │
│   │   │   - prompt: serial_proposal                        │ │ │
│   │   └───────────────────────────────────────────────────┘ │ │
│   │       │                                                   │ │
│   │       ▼                                                   │ │
│   │   ┌───────────────────────────────────────────────────┐ │ │
│   │   │ 2. 投票循环（最多3轮）                               │ │ │
│   │   │   - 所有其他代理并行投票                             │ │ │
│   │   │   - prompt: serial_vote                            │ │ │
│   │   │   - 统计同意/反对数量                               │ │ │
│   │   │   - 通过率 >= 50% → 方案通过                        │ │ │
│   │   │   - 否则 → 修改方案重新投票                         │ │ │
│   │   └───────────────────────────────────────────────────┘ │ │
│   │       │                                                   │ │
│   │       ▼                                                   │ │
│   │   ┌───────────────────────────────────────────────────┐ │ │
│   │   │ 3. 修改阶段（反对过多时）                            │ │ │
│   │   │   - prompt: serial_revise                          │ │ │
│   │   │   - 根据反对意见修改方案                             │ │ │
│   │   │   - 重新进入投票循环                                 │ │ │
│   │   └───────────────────────────────────────────────────┘ │ │
│   │       │                                                   │ │
│   │       ▼                                                   │ │
│   │   ┌───────────────────────────────────────────────────┐ │ │
│   │   │ 4. 可选测试验证                                      │ │ │
│   │   │   - test_runner.run_tests()                        │ │ │
│   │   │   - 测试失败 → 触发临时会议                          │ │ │
│   │   └───────────────────────────────────────────────────┘ │ │
│   │                                                           │ │
│   └───────────────────────────────────────────────────────────┘ │
│       │                                                       │
│       ▼                                                       │
│   step.status = StepStatus.COMPLETED                          │
│                                                               │
└───────────────────────────────────────────────────────────────┘
    │
    ▼
┌───────────────────────────────────────────────────────────────┐
│ _generate_result()                                            │
│ 文件: serial.py                                               │
│ 作用: 生成最终结果文本                                         │
│   - 汇总所有步骤结果                                           │
│   - 返回格式化的结果字符串                                      │
└───────────────────────────────────────────────────────────────┘
    │
    ▼
┌───────────────────────────────────────────────────────────────┐
│ _build_result()                                               │
│ 文件: serial.py                                               │
│ 作用: 构建返回结果                                             │
│   - 设置最终决议到白板                                         │
│   - 返回 ModeResult                                           │
└───────────────────────────────────────────────────────────────┘
```

### 核心子函数

#### _build_context_info()
```python
# modes/serial.py
def _build_context_info(self) -> str
```

作用: 构建上下文信息（合并会议模式传递的数据）

#### _trigger_temp_meeting()
```python
# modes/serial.py
async def _trigger_temp_meeting(self, step, question) -> Optional[str]
```

作用: 步骤执行遇到问题时触发临时会议

---

## 核心数据结构

### Whiteboard (白板)
```python
# whiteboard.py
class Whiteboard:
    _messages: List[Message]          # 所有消息
    _tool_results: List[ToolResult]   # 工具调用结果
    _viewpoints: List[Dict]           # 观点列表
    _consensus: List[ConsensusItem]   # 共识列表
    _agenda: List[Dict]               # 议程列表
    _review_records: List[Dict]       # 复盘记录
    _fact_board: FactBoard            # 事实白板
```

### ModeResult (模式结果)
```python
# modes/base.py
@dataclass
class ModeResult:
    success: bool
    final_resolution: str
    messages: List[Dict]
    tool_results: List[Dict]
    stats: Dict
    metrics: Dict
    intermediate_results: Dict
    proposals: List[Dict]
    steps: List[str]
    agenda_conclusions: List[Dict]
    error: Optional[str]
```

### AgentState (代理状态)
```python
# modes/conference.py
@dataclass
class AgentState:
    speak_count: int = 0           # 发言次数
    last_speak_time: float = 0     # 最后发言时间
    stance: str = ""               # 立场
    stance_instruction: str = ""   # 立场指令
    interruption_count: int = 0    # 打断次数
```

---

## 关键信号

| 信号 | 格式 | 作用 | 处理函数 |
|------|------|------|----------|
| 叫停 | `[INTERRUPT]` 或 `[INTERRUPT:@agent_id]` | 打断讨论 | `_discussion_loop()` |
| 结束 | `[END]` | 标记讨论结束 | `_discussion_loop()` |
| 扩展 | `[EXPAND: 议题]` | 扩展子话题 | `_discussion_loop()` |
| 暂存 | `[PARK]` | 暂存当前议题 | `_discussion_loop()` |
| 恢复 | `[RESTORE id]` | 恢复暂存议题 | `_discussion_loop()` |

---

## 打断机制

### 当前实现
打断需要代理**主动输出** `[INTERRUPT]` 信号：

```python
# modes/conference.py _discussion_loop() 中
interrupt_match = re.search(r'\[INTERRUPT(?::@(\w+))?\]', result, re.IGNORECASE)
if interrupt_match:
    target_agent = interrupt_match.group(1)  # None 表示全体叫停
    if target_agent:
        # 指定叫停：让目标代理等待
        ...
    else:
        # 全体叫停：发起投票
        await self._handle_global_interrupt(...)
```

### 限制
- 代理需要主动使用 `[INTERRUPT]` 信号
- 系统不会自动检测争议并触发打断
- 代理可能比较"礼貌"，不主动打断

---

## 文件结构

```
omnivoice/
├── main.py                    # 入口、CLI、会话管理
├── agent.py                   # Agent 类、代理池
├── whiteboard.py              # 共享白板
├── workspace.py               # 工作区管理
├── mode_decision.py           # 模式投票决策
├── config_loader.py           # 配置加载、提示词定义
├── intensity_regulator.py     # 争吵强度调节
│
├── modes/
│   ├── base.py                # 模式基类、ModeResult
│   ├── conference.py          # 会议模式
│   └── serial.py              # 串行模式
│
├── tools/
│   └── base.py                # 工具路由、插件管理
│
└── plugins/
    ├── local/                 # 本地工具
    ├── network/               # 网络工具
    └── workspace/             # 工作区工具
```
