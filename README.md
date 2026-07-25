# douyin-knowledge

把自己账号中的抖音收藏转换为本地、可复核、可恢复的知识库，并在人工批准后发布到 Obsidian。

项目由两部分组成：

- `douyin-knowledge` CLI：负责登录、收藏同步、视频下载、本地 ASR/OCR、关键帧提取、状态管理、校验和发布。
- `douyin-knowledge` Skill：让 Codex 按安全工作流调用 CLI、阅读证据、生成知识草稿，并在审核和发布节点停下来等待确认。

默认不会批量下载、调用额外的远程模型或发布内容。AI 不直接操作数据库，不自行批准草稿，也不会绕过登录、验证码、限流或平台保护。

## 主要能力

- 同步自己账号的收藏列表，并正确处理新增、取消收藏和重新收藏。
- 本地执行语音识别和画面文字识别。
- 扫描完整视频后选择关键帧，而不是只分析视频开头或固定片段。
- 将完整的脱敏 ASR、OCR、时间线和关键帧分块交给语义工作流。
- 生成结构化知识草稿，经过 schema、来源、时效性和完整性校验。
- 强制区分“执行分析”“批准草稿”和“发布”三次不同的确认。
- 使用 SQLite、检查点和发布 journal 恢复中断任务，并对账 Obsidian 结果。

```text
收藏快照 -> 单条 canary -> 本地 ASR/OCR/关键帧 -> AI 草稿
         -> 人工 approve/reject -> 发布 Library/Obsidian -> 对账验收
```

## 支持环境

- Windows 10/11
- PowerShell 5.1 或更高版本
- Python 3.11 或 3.12
- Git
- 用户自己的抖音账号
- Obsidian Vault（仅发布时需要）

Codex + Windows + PowerShell 已完成发布级端到端验证。OpenClaw 尚未完成 1.1 版本的发布级端到端验证，只能在通过[宿主能力检查](skills/douyin-knowledge/references/host-adapters.md)后按实际能力使用，不应视为完整支持。

## 快速安装

在 PowerShell 中运行：

```powershell
git clone https://github.com/baoyu2026/douyin-knowledge.git
Set-Location douyin-knowledge
.\scripts\bootstrap.ps1 -InstallCodexSkill
```

安装脚本会：

- 创建项目专用的 `.venv`；
- 安装 Python 依赖和 Playwright Chromium；
- 初始化独立于 Git 仓库的私有实例；
- 将 Skill 安装到 `$HOME/.codex/skills/douyin-knowledge`；
- 运行一次 `doctor` 环境检查。

默认私有实例位于 `%LOCALAPPDATA%\douyin-knowledge`。Cookie、视频、数据库、模型、日志和生成内容都保存在私有实例中，不会写入 Git 仓库。

要指定其他位置，在首次安装时传入明确路径：

```powershell
.\scripts\bootstrap.ps1 `
  -InstallCodexSkill `
  -InstanceRoot 'D:\私人资料\抖音 知识库'
```

安装完成后重新打开 Codex 会话，使新 Skill 被发现。

## 用 Skill 开始

在 Codex 中可以直接说：

```text
使用 $douyin-knowledge 同步我的抖音收藏，先处理 1 条，生成草稿后停下来，不要发布。
```

Skill 会依次检查环境，并在以下操作前说明影响、等待确认：

1. 打开浏览器登录抖音；
2. 同步当前收藏快照；
3. 下载并分析一条 canary 视频；
4. 展示生成的草稿供人工批准或拒绝；
5. 在已启用发布的情况下，再次确认后写入 Obsidian。

登录、同步、分析和发布不是一次总授权。尤其是 `approve` 只批准当前草稿，发布仍需要新的明确确认。

## 手动检查

不通过 AI 宿主时，使用仓库启动器调用 CLI。启动器会自动读取安装时绑定的私有实例，避免误操作其他目录：

```powershell
$DK = (Resolve-Path .\scripts\douyin-knowledge.ps1).Path

& $DK doctor --json
& $DK login --confirm --json
& $DK model install --name small --confirm --json
& $DK sync --confirm --json
& $DK plan --limit 1 --json
& $DK canary --limit 1 --no-publish --confirm --json
```

- `login` 打开交互式浏览器，Cookie 只保存在私有实例。
- `sync` 只同步收藏列表，不下载视频。
- `model install` 安装本地 ASR 模型。
- `plan` 返回稳定的任务引用，不修改内容。
- `canary` 只处理一条，并停在语义证据包；它不调用额外模型，也不发布。

首次安装 Chromium 和本地模型需要一定时间与磁盘空间。任何阶段中断后，先运行 `status --json` 和 `doctor --json`，再使用原任务引用恢复，不要另选一条替代。

## 收藏持续变化时怎么处理

每次 `sync` 都会读取一次完整收藏列表，建立快照，并按稳定的来源标识进行对账：

- 新收藏：标记为 `new`，进入后续处理计划；
- 仍在收藏：保留已有状态和有效产物，不重复分析；
- 已取消收藏：标记为 `uncollected`，但不删除视频、分析结果或已发布知识；
- 重新收藏：恢复为活跃状态，并复用仍然有效的历史产物；
- 同步失败或分页不完整：不会据此判断取消收藏，防止网络异常造成误删。

如果已经配置 Obsidian，`sync` 还会更新已发布笔记的 `favorite_state`。Vault 配置或笔记状态异常时，收藏快照仍会保留成功结果，但 JSON 中的 `favorite_state_sync` 会返回 `blocked`；此时运行 `doctor --json` 修复发布目标。

这是“快照对账 + 软删除”策略。当前不会自动清理长期取消收藏的媒体，因此私有实例的磁盘占用可能随时间增长。

## 内容完整性

1.1 版本针对“只截到部分视频内容”的问题增加了完整性约束：

- 优先选择平台返回的最高有效分辨率档位，并检查视频尾部可解码性和 ASR 处理时长。
- 每 0.5 秒扫描完整视频，再根据首尾覆盖、时间分布、场景变化和去重选择最多 40 张关键帧。
- 40 张上限只限制最终输出，不会让扫描在视频中途提前结束。
- 摘要 packet 会报告每类证据的纳入数量和截断状态。
- 完整脱敏 ASR、OCR 和时间线会以有界 JSON 分块导出；语义 worker 必须读取全部分块。
- 语义 worker 必须实际查看导出的关键帧；无法看图的宿主不得生成视觉结论。
- 分析、证据包或候选内容变化后，旧候选和旧批准自动失效。

## 配置 Obsidian 发布

发布默认关闭。先在私有实例中完成两项配置。

编辑 `config/obsidian.yml`，指向一个已经存在且包含 `.obsidian` 目录的 Vault：

```yaml
vault: 'D:/知识库/My Vault'
```

然后编辑 `config/config.yml`，仅在确认发布目标后启用发布：

```yaml
publishing:
  enabled: true
  require_confirmation: true
```

再次检查：

```powershell
& $DK doctor --json
```

只有 `ready_for_publish` 为 `true` 时才能进入发布确认。发布先写 intent 和备份，再写入本地 Library 与 Obsidian，最后通过哈希对账验收。

## 隐私与安全

- CLI 本身不会把 Cookie、原始平台 ID、URL、绝对路径、数据库或日志发送给 AI。
- 语义生成阶段会把脱敏后的文字证据和精选关键帧提供给当前 AI 宿主；关键帧像素不会自动脱敏，适用该宿主自身的隐私与数据政策。
- AI 只能通过版本化 JSON 协议和相对 handle 操作，不能直接读写 SQLite。
- `.gitignore` 排除了私有配置、Cookie、媒体、模型、数据库、日志、候选文件和生成知识。
- 本项目只用于处理用户有权访问和保存的内容，请遵守抖音平台规则及当地法律。

安全问题请按 [SECURITY.md](SECURITY.md) 的方式私下报告，不要在公开 Issue 中附带 Cookie、视频、原始 ID、日志或本机路径。

## 更新

在仓库目录中运行：

```powershell
git pull
.\scripts\bootstrap.ps1 -InstallCodexSkill -ForceSkill
```

更新 Skill 时，安装器会先备份内容不同的旧副本。私有实例位于仓库之外，不会被 `git pull` 覆盖。

完整安装、实例重绑定和其他宿主说明见：

- [安装与配置](skills/douyin-knowledge/references/installation-and-configuration.md)
- [CLI 契约](skills/douyin-knowledge/references/cli-contract.md)
- [安全与恢复](skills/douyin-knowledge/references/safety-and-recovery.md)
- [宿主适配边界](skills/douyin-knowledge/references/host-adapters.md)

## 开发与验证

```powershell
.\scripts\bootstrap.ps1 -WithDev -SkipBrowser
.\.venv\Scripts\python.exe -m pytest --cov=douyin_knowledge --cov-report=term-missing
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m compileall -q app src
.\.venv\Scripts\python.exe -m build
.\scripts\test-distribution.ps1 `
  -WheelPath .\dist\douyin_knowledge-1.1.0-py3-none-any.whl `
  -Python .\.venv\Scripts\python.exe
```

CI 在 Python 3.11 和 3.12 上运行回归测试、构建 wheel/sdist，并从 wheel 创建空虚拟环境验证 Skill、安装器以及中文和空格路径。

## 已知边界

- 当前正式支持 Windows；其他操作系统尚未完成发布级验证。
- 抖音页面和接口发生变化时，同步功能可能需要适配更新。
- 长期取消收藏的本地媒体不会自动清理。
- OpenClaw 尚未完成完整的候选生成、人工复核和发布闭环验证。

## 许可证

项目使用 [Apache-2.0](LICENSE) 许可证。第三方代码及再分发说明见 [NOTICE](NOTICE)。
