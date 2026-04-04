# Omnivoice

多代理协作系统 - 会议、串行、混合三种模式

## 快速开始

```bash
pip install aiohttp pyyaml
python main.py
```

## 核心功能

- **会议模式**：自由讨论 + 投票决议
- **串行模式**：步骤拆解 + 顺序执行  
- **混合模式**：讨论与执行结合

## 信号命令

| 信号 | 说明 |
|------|------|
| `[EXPAND: 议题]` | 扩展子话题 |
| `[PARK]` | 暂存当前议题 |
| `[RESTORE id]` | 恢复暂存议题 |
| `[INTERRUPT]` | 叫停并提案 |

## 配置

编辑 `config.yaml` 添加代理：

```yaml
agents:
  - id: agent_01
    api:
      base_url: "https://api.example.com/v1"
      api_key: "your-key"
      model: "gpt-4"
    personality:
      cautiousness: 5
      empathy: 5
      abstraction: 5
    enabled: true
```

## 架构

```
用户 → 模式决策 → [会议|串行|混合] → 代理池 → 白板/工具
```

MIT License