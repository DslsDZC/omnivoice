# Omnivoice

多代理协作系统 - 支持会议讨论、串行执行、智能模式切换

## 快速开始

```bash
pip install -r requirements.txt
python main.py
```

创建 `config.yaml` 配置文件（参考下方配置示例）。

## 核心功能

### 三种执行模式

| 模式 | 说明 | 适用场景 |
|------|------|----------|
| 会议模式 | 多代理讨论 + 议程管理 + 投票决议 | 开放性问题、头脑风暴、需要共识 |
| 串行模式 | 任务拆解 + 步骤执行 + 测试验证 | 有明确步骤的任务、代码生成 |
| 混合模式 | 讨论后自动切换执行 | 先讨论后执行的任务 |

### 会议模式特性

- **议程管理**：自动生成议程，代理争论确定议题优先级
- **立场分配**：支持方、反对方、中立观察员、魔鬼代言人
- **事实核查**：区分事实与观点，检测矛盾陈述
- **共识检测**：自动判断讨论是否达成共识
- **思考暂停**：代理可请求静默思考时间

### 串行模式特性

- **提案-投票循环**：方案提出后全员投票，反对意见驱动修改
- **快照回滚**：支持执行前快照，失败时可恢复
- **临时会议**：执行遇到分歧时自动触发会议讨论

### 智能特性

- **模式自动切换**：根据任务特征智能选择模式
- **防震荡机制**：滞后阈值 + 最小停留时间防止频繁切换
- **争吵强度调节**：根据复杂度、分歧度自动调整讨论激烈程度

## 信号命令

| 信号 | 说明 |
|------|------|
| `[EXPAND: 议题]` | 扩展子话题 |
| `[PARK]` | 暂存当前议题 |
| `[RESTORE id]` | 恢复暂存议题 |
| `[INTERRUPT]` | 叫停并提案 |
| `[THINK_REQ 秒数]` | 请求思考暂停 |
| `[VOTE: support/oppose]` | 投票表决 |

## CLI 命令

| 命令 | 说明 |
|------|------|
| `/help` | 显示帮助 |
| `/tools` | 列出所有工具 |
| `/agents` | 列出代理状态 |
| `/search <关键词>` | 搜索工具 |
| `/budget` | 显示预算状态 |
| `/cost` | 显示成本报告 |
| `/groups` | 显示代理分组 |
| `/review` | 显示复盘报告 |
| `/adjust <代理> <属性> <值>` | 调整代理性格 |
| `!<命令>` | 执行系统命令 |

## 配置示例

```yaml
agents:
  - id: agent_01
    api:
      base_url: "https://api.openai.com/v1"
      api_key: "${OPENAI_API_KEY}"  # 支持环境变量
      model: "gpt-4"
      max_tokens: 4096
      temperature: 0.7
    personality:
      cautiousness: 5    # 谨慎度 0-10
      empathy: 5         # 共情度 0-10
      abstraction: 5     # 抽象度 0-10
      independence: 7    # 独立性 0-10
    allowed_tools:
      - calculator
      - code_execute
      - temp_file_read
      - temp_file_write
    enabled: true

  # 推理模型示例（DeepSeek R1）
  - id: agent_reasoner
    api:
      base_url: "https://api.deepseek.com/v1"
      api_key: "${DEEPSEEK_API_KEY}"
      model: "deepseek-reasoner"
      reasoning_model: true
      reasoning_effort: "medium"
      supports_tools: false
    personality:
      cautiousness: 8
      abstraction: 9
      independence: 9
    allowed_tools: []
    enabled: true

global:
  enable_network_tools: false
  
  conference:
    max_rounds: 5
    consensus_threshold: 0.6
    discussion_timeout_sec: 300
  
  serial:
    step_timeout_sec: 60
    max_retries: 2
    enable_snapshot: true
  
  security:
    level: "standard"  # strict/standard/permissive
    execution_mode: "subprocess"  # docker/subprocess
    code_timeout_seconds: 30
  
  neutrality:
    min_independence: 7
    stance_mode: "devil_advocate"
```

## 代理属性说明

| 属性 | 范围 | 说明 |
|------|------|------|
| cautiousness | 0-10 | 高值倾向于反复验证，低值大胆尝试 |
| empathy | 0-10 | 高值注重情感，低值偏向理性分析 |
| abstraction | 0-10 | 高值擅长宏观分析，低值偏向具体实操 |
| independence | 0-10 | 高值具有批判性，低值倾向于配合 |

## 推理模型支持

支持 OpenAI o1/o3 和 DeepSeek R1 等推理模型：

```yaml
api:
  model: "deepseek-reasoner"
  reasoning_model: true
  reasoning_effort: "medium"  # low/medium/high
  supports_tools: false       # 推理模型通常不支持工具
  supports_system_message: false  # o1 不支持 system 消息
```

## 工具系统

### 内置工具

| 类别 | 工具 | 说明 |
|------|------|------|
| 计算 | calculator | 数学计算 |
| 时间 | current_time | 获取当前时间 |
| 文件 | temp_file_read/write/delete | 临时文件操作 |
| 文件 | temp_list_files | 列出文件 |
| 代码 | code_execute | 代码执行（沙箱隔离） |
| 网络 | web_search, web_fetch | 网络搜索（需启用） |

### 安全控制

- 工具权限：每个代理可配置允许的工具列表
- 速率限制：防止工具滥用
- 代码沙箱：支持 Docker 隔离执行
- 审计日志：记录所有工具调用

## 架构

```
用户输入
    ↓
模式决策器 → 会议/串行/混合
    ↓
代理池（并行调用）
    ↓
共享白板（状态同步）
    ↓
工具系统（安全执行）
    ↓
最终决议
```

## 目录结构

```
omnivoice/
├── main.py              # 入口和 CLI
├── agent.py             # 代理类和代理池
├── config_loader.py     # 配置加载
├── whiteboard.py        # 共享状态存储
├── workspace.py         # 工作区管理
├── modes/
│   ├── base.py          # 模式基类
│   ├── conference.py    # 会议模式
│   ├── serial.py        # 串行模式
│   └── debate.py        # 争吵模式
├── tools/
│   └── base.py          # 工具系统核心
├── plugins/
│   ├── local/           # 本地工具
│   ├── network/         # 网络工具
│   └── workspace/       # 工作区工具
├── tool_security.py     # 工具安全控制
├── code_sandbox.py      # 代码执行沙箱
├── memory_store.py      # 长期记忆存储
├── memory_manager.py    # 记忆管理
├── fact_checker.py      # 事实核查
├── stance_manager.py    # 立场管理
├── personality_consistency.py  # 性格一致性
├── intensity_regulator.py      # 争吵强度调节
├── oscillation_guard.py        # 防震荡
├── budget_manager.py    # 预算管理
├── concurrency_controller.py   # 并发控制
└── event_bus.py         # 事件总线
```

## 环境变量

```bash
export OPENAI_API_KEY="your-key"
export DEEPSEEK_API_KEY="your-key"
```

## 依赖

- Python 3.11+
- aiohttp（异步 HTTP）
- PyYAML（配置解析）
- certifi（SSL 证书）

MIT License
