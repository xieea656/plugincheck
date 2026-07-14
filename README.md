# PluginCheck

AstrBot 插件自动化测试 CLI —— 静态分析 + 子进程压力测试 + 全量日志导出。

**不用装插件、不用重启 AstrBot、不用手动清数据。**

## 快速开始

```bash
git clone https://github.com/xieea656/plugincheck
cd plugincheck

# 静态分析
python3 cli.py check astrbot_plugin_echoer

# 全量测试
python3 cli.py test astrbot_plugin_echoer

# 压力测试
python3 cli.py test astrbot_plugin_echoer --heavy --count 500

# 带 systemd 日志监控
python3 cli.py test astrbot_plugin_echoer --heavy --journal
```

## 命令

| 命令 | 用途 | 耗时 |
|------|------|------|
| `check <name>` | 静态分析（metadata/syntax/import/schema/deps/smells） | ~2s |
| `test <name>` | 全量（静态 + 生命周期 + 消息边界 + 命令 + hook） | ~15s |
| `test <name> --heavy` | 全量 + 并发压力 | ~60s |
| `test <name> --heavy --count N` | 自定义压力消息数 | 可调 |
| `test <name> --journal` | 附加 systemd 日志尾随 | ~15s |
| `logs` | 查看历史日志 | 瞬间 |
| `logs -n 20` | 最近 20 条 | 瞬间 |

## 测试内容

### 静态分析
- `metadata.yaml` 完整性（name/desc/version/author）
- Python 语法（所有 .py 文件 AST 解析）
- 插件可导入性
- `_conf_schema.json` 合法性
- 依赖安装状态
- 代码坏味道（bare `except:` 等）

### 运行时测试
- 生命周期：`__init__` → `initialize` → `terminate`，重复 3 次，断言无残留
- 消息边界：空消息 / 8KB 长消息 / SQL 注入 / emoji / CQ 码 等 7 种
- 命令触发：自动发现所有 `@filter.command` 并触发
- LLM hook：`on_llm_request` 注入验证
- 并发安全：50 条并发消息，断言无崩溃
- 压力测试（`--heavy`）：可配置数量，记录延迟 P50/P99 和内存增长

### 零 Token 消耗

所有 LLM 和 Embedding 调用走 **Mock Provider**，返回固定合法的 JSON / 确定性向量。5000 条压力消息也不消耗任何 API token。

## 日志

每次 `test` 自动导出日志到 `logs/<plugin>_<timestamp>.log`：

```markdown
## Report
(通过/警告/失败 完整报告)

## Raw Data
(子进程返回的原始 JSON)

## stderr
(Traceback 等标准错误)

## journalctl
(systemd 日志异常行, 仅 --journal 模式)
```

```bash
python3 cli.py logs        # 查看最近 10 条
python3 cli.py logs -n 20  # 最近 20 条
```

## 原理

```
python3 cli.py test echoer --heavy
        │
        ├─ checker.py    → 静态分析（主进程, 直接 import + ast.parse）
        │
        └─ runner.py     → spawn 子进程
              │
              ├─ import 目标插件
              ├─ Mock LLM / Embedding 注入
              ├─ 发现 handler（command / event / llm_hook / regex）
              ├─ 逐个触发 + try/except 捕获
              ├─ 并发压力（asyncio.gather, 可配置数量）
              ├─ 返回 JSON 结果 + stderr
              │
         ← 主进程收集 → 写报告 + 日志文件
         ← 清理临时目录
```

子进程隔离：插件 `__init__` 崩溃、`initialize` 死循环、handler panic —— 都不影响测试器本身。

## 放在哪

运行在 **Fedora 服务器**上（AstrBot 同机），因为需要 AstrBot 的 Python 环境来 import 插件。

从 Windows 远程触发：

```bash
ssh fedora "cd ~/plugincheck && python3 cli.py test echoer --heavy"
```

## License

MIT
