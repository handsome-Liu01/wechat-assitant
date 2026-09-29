# 微信群软件问题日报 Agent

一个运行在 Windows 上的本地 Agent。它通过 WeChatDataAnalysis 读取指定微信群消息，提取软件发行版反馈中的重要问题，并生成每日 Excel 报表。

## 功能

- 拉取指定微信群最近的聊天消息，并在本地 SQLite 中去重保存。
- 每天 10:00 分析昨天 `00:00—24:00` 的聊天内容。
- 命中 `#重要`、`#阻塞`、`[P0]` 等关键词的消息直接收录。
- 其余消息交给 OpenAI-compatible 模型判断和归纳。
- Excel 报表包含重要问题、关键词强制收录和运行统计。
- 可将分析结果增量写入飞书多维表格，并避免重复写入同一问题。
- 可筛选飞书中“一般/轻微”问题，结合本地历史仓库生成可执行的 GitHub Issue。
- 只读取聊天数据，不发送消息或修改微信记录。

## 依赖项

- Windows 10/11
- Python 3.10—3.13
- 微信 4.x PC 客户端
- [WeChatDataAnalysis](https://github.com/LifeArchiveProject/WeChatDataAnalysis/releases/latest)
- 一个兼容 OpenAI `/chat/completions` 接口的模型服务
- 可选：用于同步日报的飞书企业自建应用和多维表格
- 可选：用于创建 Issue 的 GitHub Fine-grained personal access token

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
  view_id: "链接的 view 参数（可选）"
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

### GitHub Issue 自动分拣

该功能读取飞书表格中的低严重度问题，先从指定目录识别远程属于目标组织的仓库，再让模型
结合 README、项目清单、文件树和相关源码判断是否存在明确修复路径。只有模型能定位真实文件、
给出修复方案和验证方法，并达到置信度阈值时，才会创建 Issue。程序不会修改历史仓库或执行
模型生成的命令。

在 `config.yaml` 中配置：

```yaml
github_issues:
  enabled: true
  token_env: "GITHUB_TOKEN"
  organization: "HarnessApex"
  repositories_root: "E:/workspace/HarnessApex"
  repository_allowlist: []
  severity_field: "严重程度"
  severity_values: ["一般", "轻微"]
  title_field: "问题标题"
  status_field: "状态"
  ignored_status_values: ["已修复", "已回归"]
  min_confidence: 0.82
  max_issues_per_run: 3
  dry_run: true
```

创建一个 Fine-grained personal access token，使它只能访问需要提单的仓库，并授予仓库
`Issues: Read and write` 权限。将令牌保存在环境变量中，不要写进 YAML 或提交到 Git：

SSH Key 只用于 `git clone/pull/push`，不能用于创建 Issue 的 REST API。纯试运行不访问
GitHub，因此不需要 Token；只有 `--apply` 和自动真实提单需要配置 Token。

```powershell
[Environment]::SetEnvironmentVariable("GITHUB_TOKEN", "你的 GitHub Token", "User")
```

重新打开终端后，先执行试运行。该命令会分析并打印决策，但不会创建真实 Issue：

```powershell
.\.venv\Scripts\wechat-digest.exe github-triage
```

确认路由和判断合理后，可以执行一次真实提单测试：

```powershell
.\.venv\Scripts\wechat-digest.exe github-triage --apply
```

只重测当前飞书视图中的第 22 条记录（不会创建 Issue）：

```powershell
.\.venv\Scripts\wechat-digest.exe github-triage --record-index 22 --recheck
```

也可以用稳定的飞书记录 ID 选择问题：

```powershell
.\.venv\Scripts\wechat-digest.exe github-triage --record-id recxxxxxxxx --recheck
```

若希望每天 10:00 的自动任务真实创建 Issue，再将 `github_issues.dry_run` 改为 `false`。
程序会按飞书记录和内容去重，每次最多创建 `max_issues_per_run` 条。
状态为“已修复”或“已回归”的记录会在调用模型前直接跳过；列名和状态值可以通过
`status_field`、`ignored_status_values` 调整。

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
