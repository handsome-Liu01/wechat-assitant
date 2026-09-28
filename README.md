# 微信群软件问题日报 Agent

一个运行在 Windows 上的本地 Agent。它通过 WeChatDataAnalysis 读取指定微信群消息，提取软件发行版反馈中的重要问题，并生成每日 Excel 报表。

## 功能

- 拉取指定微信群最近的聊天消息，并在本地 SQLite 中去重保存。
- 每天 10:00 分析昨天 `00:00—24:00` 的聊天内容。
- 命中 `#重要`、`#阻塞`、`[P0]` 等关键词的消息直接收录。
- 其余消息交给 OpenAI-compatible 模型判断和归纳。
- Excel 报表包含重要问题、关键词强制收录和运行统计。
- 只读取聊天数据，不发送消息或修改微信记录。

## 依赖项

- Windows 10/11
- Python 3.10—3.13
- 微信 4.x PC 客户端
- [WeChatDataAnalysis](https://github.com/LifeArchiveProject/WeChatDataAnalysis/releases/latest)
- 一个兼容 OpenAI `/chat/completions` 接口的模型服务

Python 包依赖由安装脚本自动安装，主要包括 `httpx`、`openpyxl`、`PyYAML` 和 `tzdata`。

## 安装方法

1. 从 WeChatDataAnalysis 的 [Release 页面](https://github.com/LifeArchiveProject/WeChatDataAnalysis/releases/latest) 下载并安装 Windows `Setup.exe`。
2. 启动 WeChatDataAnalysis，完成微信数据读取，并确认目标群可以正常打开。
3. 在本项目目录运行：

```powershell
.\scripts\setup.ps1
```

4. 编辑 `config.yaml`：

```yaml
group_name: "你的群聊名称"

collector:
  driver: "wechat_data_analysis"
  wda_base_url: "http://127.0.0.1:10392"
  wda_account: ""
  wda_session_id: ""  # 同名群时填写 xxx@chatroom
  wda_source: "auto"

keywords:
  force_include:
    - "#重要"
    - "#阻塞"
    - "[P0]"
    - "[P1]"

llm:
  base_url: "https://你的模型服务/v1"
  api_key_env: "LLM_API_KEY"
  model: "模型名称"
```

5. 设置模型密钥：

```powershell
$env:LLM_API_KEY = "你的模型密钥"
```

如需让 Windows 计划任务读取密钥，请同时保存为用户环境变量：

```powershell
[Environment]::SetEnvironmentVariable("LLM_API_KEY", "你的模型密钥", "User")
```

## 用法

检查配置、WeChatDataAnalysis 和目标群：

```powershell
.\.venv\Scripts\wechat-digest.exe doctor
```

拉取一次消息：

```powershell
.\.venv\Scripts\wechat-digest.exe collect
```

生成昨天的完整日报：

```powershell
.\.venv\Scripts\wechat-digest.exe analyze --date yesterday
```

只导出关键词消息，不调用模型：

```powershell
.\.venv\Scripts\wechat-digest.exe analyze --date yesterday --rules-only
```

持续运行，并在每天 10:00 自动生成昨天的日报：

```powershell
.\.venv\Scripts\wechat-digest.exe run
```

安装登录后自动启动的 Windows 计划任务：

```powershell
.\scripts\install-task.ps1
```

Excel 默认输出到 `reports`，运行日志保存在 `logs/agent.log`。运行期间需要保持 WeChatDataAnalysis 可用。
