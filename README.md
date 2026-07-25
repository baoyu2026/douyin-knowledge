# douyin-knowledge

将自己账号中的抖音收藏转换为本地、可复核、可恢复的知识库。项目由两部分组成：

- `douyin-knowledge` Python CLI：负责身份绑定、下载、本地 ASR/OCR、SQLite、检查点、校验、渲染、发布 journal 和验收。
- `skills/douyin-knowledge`：供 Codex、OpenClaw 等宿主理解意图、请求确认并编排一次受限的 JSON 候选生成。

AI 不直接操作数据库，也不直接发布。默认不登录、不批量下载、不调用 AI 模型、不发布。

## 环境

- Windows 10/11
- Python 3.11 或 3.12
- PowerShell 5.1+
- FFmpeg（本地分析）
- 用户自己的抖音账号和 Obsidian Vault（发布时需要）

请遵守平台规则，仅处理自己有权访问和保存的内容。本项目不提供验证码、签名、登录风控或平台保护绕过能力。

## 安装

```powershell
git clone <repository-url> douyin-knowledge
Set-Location douyin-knowledge
.\scripts\bootstrap.ps1 -InstallCodexSkill
```

脚本会创建 `.venv`、安装包和 Playwright Chromium，并初始化用户级私有目录。也可手动安装：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\playwright.exe install chromium
.\.venv\Scripts\douyin-knowledge.exe init --json
```

通过 `DOUYIN_KNOWLEDGE_ROOT` 或全局 `--root` 指定私有运行目录。不要把运行目录放进 Git 仓库。

## 首次使用

```powershell
douyin-knowledge doctor --json
douyin-knowledge login --confirm --json
douyin-knowledge model install --name small --confirm --json
douyin-knowledge sync --confirm --json
douyin-knowledge plan --limit 1 --json
douyin-knowledge canary --limit 1 --no-publish --confirm --json
```

`login` 会打开交互式浏览器并在私有目录保存 Cookie。`sync` 只同步收藏快照，不下载视频。`canary` 固定处理一条并停在语义 packet，不调用 AI、不发布。

## 单条知识工作流

```powershell
douyin-knowledge run --job-ref <job_ref> --stop-after packet --confirm --json
douyin-knowledge packet export --job-ref <job_ref> --json
# 宿主严格按 worker-instructions.md 和 candidate.schema.json 写 candidate-v1.json
douyin-knowledge candidate import --job-ref <job_ref> --input <relative-candidate-path> --json
douyin-knowledge review record --job-ref <job_ref> --decision approve --json
douyin-knowledge publish --job-ref <job_ref> --confirm --json
douyin-knowledge reconcile --job-ref <job_ref> --json
```

候选导入失败时运行 `candidate repair-contract`。只有契约标记为可修复时才允许一次受限修复；同一阶段相同失败两次后停止。

## 安装 Skill

将 `skills/douyin-knowledge` 目录安装或链接到宿主的 Skill 目录。Codex 可运行 `.\scripts\install-skill.ps1`，默认安装到 `$HOME/.codex/skills/douyin-knowledge`；更新已有副本时显式传 `-Force`，旧副本会先备份。Skill 的 `agents/openai.yaml` 已包含发现元数据。

不同宿主的能力边界见 [host-adapters.md](skills/douyin-knowledge/references/host-adapters.md)。不能可靠写入纯 JSON 文件的宿主只应声明 candidate-only 支持。

## 安全模型

- CLI stdout 只返回版本化 JSON envelope、相对 handle、稳定 `job_ref` 和安全摘要。
- Cookie、原始平台 ID、URL、绝对路径、媒体、数据库、日志和 reviewer note 不进入模型上下文。
- 发布先写 intent，再封存并对账 Library/Vault 哈希；验收前 registry 保持 `analyzed`。
- `.gitignore` 默认排除所有运行数据、模型、媒体、日志和凭据。

安全问题与披露方式见 [SECURITY.md](SECURITY.md)。

## 开发验证

```powershell
.\.venv\Scripts\python.exe -m pytest --cov=douyin_knowledge --cov-report=term-missing
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m compileall -q app src
```

覆盖率门槛针对新增公共包 `douyin_knowledge`（至少 80%）；同时必须运行全部历史引擎与公共层回归测试。

第三方来源与再分发边界见 [NOTICE](NOTICE)。
