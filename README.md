# douyin-knowledge

把自己账号中的抖音收藏转换为本地、可复核、可恢复的知识库，发布到 Obsidian 并完成确定性对账。

项目由两部分组成：

- `douyin-knowledge` CLI：负责登录、收藏同步、视频下载、本地 ASR/OCR、关键帧提取、状态管理、校验和发布。
- `douyin-knowledge` Skill：让 Codex 按安全工作流调用 CLI、阅读证据、生成知识稿，并在发布前等待确认。

默认不会批量下载、调用额外的远程模型或发布内容。AI 不直接操作数据库，也不会绕过登录、验证码、限流或平台保护。

## 主要能力

- 同步自己账号的收藏列表，并正确处理新增、取消收藏和重新收藏。
- 本地执行语音识别和画面文字识别。
- 扫描完整视频后选择关键帧，而不是只分析视频开头或固定片段。
- 将完整的脱敏 ASR、OCR、时间线和关键帧分块交给语义工作流。
- 生成结构化知识草稿，经过 schema、来源、时效性和完整性校验。
- 强制区分“执行分析”和“发布”两类确认；内部草稿不设人工批准门禁。
- 使用 SQLite、检查点和发布 journal 恢复中断任务，并对账 Obsidian 结果。
- 将旧版 `library` 成果复制、校验并整理到用户指定的人类成果库。

```text
收藏快照 -> 单条 canary -> 本地 ASR/OCR/关键帧 -> AI 候选
         -> 确定性校验 -> 发布成果库/Obsidian -> 对账验收 -> completed
                                                        -> 用户在 Obsidian 发现问题后反馈修正
```

## 支持环境

- Windows 10/11
- PowerShell 5.1 或更高版本
- Python 3.11 或 3.12
- Git
- 用户自己的抖音账号
- Obsidian Vault（仅发布时需要）

Codex + Windows + PowerShell 已完成发布级端到端验证。OpenClaw 尚未完成当前版本的发布级端到端验证，只能在通过[宿主能力检查](skills/douyin-knowledge/references/host-adapters.md)后按实际能力使用，不应视为完整支持。

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

默认私有实例位于 `%LOCALAPPDATA%\douyin-knowledge`。Cookie、下载缓存、数据库、模型、日志和中间分析都保存在私有实例中，不会写入 Git 仓库。给人翻阅的最终成果另存到首次使用时由用户明确选择的目录。

要指定其他位置，在首次安装时传入明确路径：

```powershell
.\scripts\bootstrap.ps1 `
  -InstallCodexSkill `
  -InstanceRoot 'D:\私人资料\抖音 知识库'
```

安装完成后重新打开 Codex 会话，使新 Skill 被发现。

## 成果文件放在哪里

项目把机器工作区和人类成果库分开：

- 私有实例：保存任务编号目录、下载缓存、ASR/OCR、检查点和数据库，供程序恢复任务。
- 成果库：保存按主题和标题整理的完整成果，供人直接用文件管理器翻阅。
- Obsidian Vault：保存最终阅读笔记和附件，也是日常检查与纠错入口。

首次使用时 Skill 会先询问成果库放在哪个文件夹，不会沿用任务编号目录，也不会自行猜测位置。手动配置命令为：

```powershell
$DK = (Resolve-Path .\scripts\douyin-knowledge.ps1).Path
& $DK configure results `
  --path 'D:\知识库\抖音收藏成果' `
  --confirm `
  --json
```

每条发布结果采用固定的人类目录结构：

```text
抖音收藏成果/
  00-总索引/
  AI 工具与智能体/
    一个可以直接看懂的视频标题/
      内容整理.md
      原视频.mp4
      资料信息.yml
      附件/
        完整时间轴.md
      精选关键帧/
        frame-001.jpg
        ...
```

中文和空格会保留，Windows 不允许的文件名字符会替换为短横线。同一分类下两个不同视频同名时，后一个使用 `标题 (2)`，不会覆盖前一个。条目一旦发布会复用原目录，纠错不会生成重复文件夹。成果库包含一份真实的原视频副本，因此可以独立于私有缓存保存和备份。

如果旧版本已经在私有实例的 `library` 中生成过成果，先配置新的成果库，再执行一次受控迁移：

```powershell
& $DK migrate results --confirm --json
```

如迁移报告旧成果结构不完整，可先运行只读检查：

```powershell
& $DK migrate inspect --json
```

检查结果只返回完整/不完整数量、可自动兼容数量和缺项类型数量，不公开标题、任务引用或路径。旧版本成果如果只有 `资料信息.yml` 缺失，并且能通过旧路径或原视频 SHA-256 指纹唯一对应到数据库记录，会计入 `repairable`，不视为损坏；指纹重复时不会猜测。

迁移会复制结构完整的历史成果、逐条校验来源与新副本哈希、重建 `00-总索引`，并登记新位置供后续纠错复用。对于上述旧格式成果，`资料信息.yml` 只会生成在新副本中，旧目录保持原样。同一视频存在旧副本时，以数据库原登记路径对应的成果为权威版本，其余保留在旧目录并计入 `duplicates_skipped`；没有唯一权威版本时迁移会停止。重复执行会复用已经验证的副本；如果同一成果在目标目录中被改过，命令会停止并保留两边文件，不会覆盖。迁移成功不等于视频被重新发布，也不会改写 `accepted` 或 `completed` 状态。

确认新成果库可用后，如需永久删除旧 `library`、旧索引和重复副本，可另行执行：

```powershell
& $DK migrate cleanup --confirm --json
```

清理会重新验证新副本、迁移检查点和数据库登记；任何一项不一致都会停止。删除采用可断点恢复的中间状态，保留新成果库、迁移检查点和历史发布 journal，但旧目录本身无法恢复。

成果库开始参与新发布对账后不能直接改根目录，否则历史 journal 无法确认原文件。当前迁移命令只负责从旧版 `library` 过渡到首次配置的人类成果库，不支持搬迁一个已经产生 `results/` journal 的成果库。

## 用 Skill 开始

在 Codex 中可以直接说：

```text
使用 $douyin-knowledge 同步我的抖音收藏，先处理 1 条；候选校验通过后向我确认发布，写入 Obsidian 并对账成功才算完成。
```

Skill 会依次检查环境，并在以下操作前说明影响、等待确认：

1. 打开浏览器登录抖音；
2. 同步当前收藏快照；
3. 下载并分析一条 canary 视频；
4. 读取完整证据并导入通过确定性校验的候选；
5. 在已启用发布的情况下，再次确认后写入 Obsidian 并对账。

登录、同步、分析和发布不是一次总授权。候选导入不算完成，写入但未对账也不算完成；只有发布状态为 `accepted` 时才会把视频标为 `completed`。

人工检查发生在发布之后。每篇新 Obsidian 笔记都会标记 `review_status: unreviewed`，AI 对证据的核验结果另记为 `evidence_status`。`uploaded_at` 保存首次上传到 Obsidian 的带时区时间，可用于按上传先后排序；同一笔记后续纠错重发不会改变它。你直接在 Obsidian 阅读；发现问题后告诉 Skill 即可，它会把这次反馈作为纠错请求，保留历史候选和 Obsidian 手工区，针对同一条视频生成修正版并重新发布对账。你不需要再走单独的 `approve/reject` 流程。

## 手动检查

不通过 AI 宿主时，使用仓库启动器调用 CLI。启动器会自动读取安装时绑定的私有实例，避免误操作其他目录：

```powershell
$DK = (Resolve-Path .\scripts\douyin-knowledge.ps1).Path

& $DK doctor --json
& $DK configure results --path 'D:\知识库\抖音收藏成果' --confirm --json
& $DK migrate results --confirm --json
& $DK migrate cleanup --confirm --json
& $DK login --confirm --json
& $DK model install --name small --confirm --json
& $DK sync --confirm --json
& $DK plan --limit 1 --status new --json
& $DK canary --limit 1 --status new --no-publish --confirm --json
```

- `login` 打开交互式浏览器，Cookie 只保存在私有实例。
- `sync` 只同步收藏列表，不下载视频。
- `model install` 安装本地 ASR 模型。
- `plan` 返回稳定的任务引用，不修改内容。
- `canary` 只处理一条，并停在语义证据包；它不调用额外模型，也不发布。
- `migrate results` 只复制并校验旧成果，保留旧目录，不重新处理视频。
- `migrate cleanup` 只在迁移完整且新副本重新验证通过后永久删除旧成果。

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
- 每 0.2 秒有界采样到视频尾帧，再根据首尾覆盖、时间分布、场景变化和去重选择最多 40 张关键帧。
- 40 张上限只限制最终输出，不会让扫描在视频中途提前结束。
- 摘要 packet 会报告每类证据的纳入数量和截断状态。
- 完整脱敏 ASR、OCR 和时间线会以有界 JSON 分块导出；语义 worker 必须读取全部分块。
- 语义证据会导出分析阶段选出的全部关键帧（当前最多 40 张），不会再压缩成 8 张；worker 必须逐张实际查看。
- 候选只选取 3-8 条画面结论，Library 与 Obsidian 也只发布这些被引用的 3-8 张关键帧。
- 无法查看全部导出图片的宿主不得生成视觉结论。
- 分析或证据包变化后，旧候选自动失效。

## 配置 Obsidian 发布

发布默认关闭。先选择成果库目录，再在私有实例中完成 Obsidian 和发布开关配置。

成果库使用前文的 `configure results` 命令设置，不要直接编辑保存绝对路径的私有配置文件。

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

只有 `results_root_configured` 和 `ready_for_publish` 都为 `true` 时才能进入发布确认。发布先写 publication intent，再以原子写入方式更新人类成果库与 Obsidian，最后通过哈希对账验收。任何内部草稿或单独的成果文件都不计为完成。

## 隐私与安全

- CLI 本身不会把 Cookie、原始平台 ID、URL、绝对路径、数据库或日志发送给 AI。
- 语义生成阶段会把脱敏后的文字证据和分析选出的全部关键帧提供给当前 AI 宿主；关键帧像素不会自动脱敏，适用该宿主自身的隐私与数据政策。
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
如果 `.venv` 中已有私有实例绑定，更新命令会自动沿用，不会切回默认目录；只有主动迁移私有实例时才重新传入 `-InstanceRoot`。

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
  -WheelPath .\dist\douyin_knowledge-1.4.5-py3-none-any.whl `
  -Python .\.venv\Scripts\python.exe
```

CI 在 Python 3.11 和 3.12 上运行回归测试、构建 wheel/sdist，并从 wheel 创建空虚拟环境验证 Skill、安装器以及中文和空格路径。

## 已知边界

- 当前正式支持 Windows；其他操作系统尚未完成发布级验证。
- 抖音页面和接口发生变化时，同步功能可能需要适配更新。
- 长期取消收藏的本地媒体不会自动清理。
- 关键帧流程能验证采样到可解码尾帧，但不承诺捕获持续时间短于 0.2 秒的瞬时画面；可变帧率视频仍依赖 OpenCV 返回的时间位置，后续版本会加强实际 PTS 校验。
- 当前只处理视频收藏，图文收藏尚未进入知识生成流程。
- OpenClaw 尚未完成完整的候选生成、Obsidian 发布和对账闭环验证。

## 许可证

项目使用 [Apache-2.0](LICENSE) 许可证。第三方代码及再分发说明见 [NOTICE](NOTICE)。
