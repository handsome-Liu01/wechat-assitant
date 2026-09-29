# 微信群软件问题日报 Agent

一个运行在 Windows 上的本地 Agent。它通过 WeChatDataAnalysis 读取指定微信群消息，提取软件发行版反馈中的重要问题，并生成每日 Excel 报表。

## 功能

- 拉取指定微信群最近的聊天消息，并在本地 SQLite 中去重保存。
- 每天 10:00 分析昨天 `00:00—24:00` 的聊天内容。
- 命中 `#重要`、`#阻塞`、`[P0]` 等关键词的消息直接收录。
- 其余消息交给 OpenAI-compatible 模型判断和归纳。
- Excel 报表包含重要问题、关键词强制收录和运行统计。
- 可将分析结果增量写入飞书多维表格，并避免重复写入同一问题。
- 只读取聊天数据，不发送消息或修改微信记录。

## 依赖项

- Windows 10/11
- Python 3.10—3.13
- 微信 4.x PC 客户端
- [WeChatDataAnalysis](https://github.com/LifeArchiveProject/WeChatDataAnalysis/releases/latest)
- 一个兼容 OpenAI `/chat/completions` 接口的模型服务
- 可选：用于同步日报的飞书企业自建应用和多维表格

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

如需同步飞书多维表格，在 `config.yaml` 中启用并填写链接里的 `app_token` 和
`table_id`，再将应用凭证保存为用户环境变量（不要把 Secret 写进配置或提交到 Git）：

1. 在飞书开放平台创建企业自建应用，启用“机器人”能力。
2. 为应用开通“查看、评论、编辑和管理多维表格”（`bitable:app`）权限，并发布版本。
3. 在目标多维表格中将该文档应用添加为协作者，并授予“可编辑”权限。
4. 从表格链接取得 `app_token`（`/base/` 后的部分）与 `table_id`（`table` 参数）。

```yaml
feishu:
  enabled: true
  app_id_env: "FEISHU_APP_ID"
  app_secret_env: "FEISHU_APP_SECRET"
  app_token: "多维表格链接中 /base/ 后面的值"
  table_id: "链接的 table 参数"
  timeout_seconds: 30
  # 表格字段不是默认名称时，在这里设置“程序字段: 表格字段”。
  field_mapping: {}
```

```powershell
[Environment]::SetEnvironmentVariable("FEISHU_APP_ID", "你的 App ID", "User")
[Environment]::SetEnvironmentVariable("FEISHU_APP_SECRET", "你的 App Secret", "User")
```

设置后需要重新打开 PowerShell，让新进程读取环境变量。

## 用法

检查模型、飞书只读连接、WeChatDataAnalysis 和目标群：

```powershell
.\.venv\Scripts\wechat-digest.exe doctor
```

向飞书写入一条明确标注的接入测试记录：

```powershell
.\.venv\Scripts\wechat-digest.exe feishu-test
```

`doctor` 只读取飞书字段；`feishu-test` 才会实际新增一条记录。如果写入返回 403，检查
应用是否已发布 `bitable:app` 权限，以及文档应用是否拥有该表格的“可编辑”权限。

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

Excel 默认输出到 `reports`，运行日志保存在 `logs/agent.log`。启用飞书后，`analyze`
和每天 10:00 的自动任务会在生成 Excel 后将新问题同步到配置的数据表。运行期间需要保持
WeChatDataAnalysis 可用。
