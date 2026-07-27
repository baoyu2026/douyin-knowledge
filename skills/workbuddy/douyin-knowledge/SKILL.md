---
name: douyin-knowledge
description: 此技能用于在 WorkBuddy 中通过 douyin-knowledge MCP 查询抖音知识库安全状态、规划一至五条稳定任务，并为已经由本地流程准备好的隔离证据包生成和提交知识候选。用户提到抖音收藏、抖音知识库、候选生成、证据包或恢复语义任务时使用此技能。
agent_created: true
allowed-tools: mcp__douyin-knowledge
---

# 抖音知识候选

通过 `douyin-knowledge` MCP Gateway 完成受限的候选生成。始终把本地 JSON CLI
视为状态、校验和发布权威；不要把对话内容当作完成证据。

## 建立能力边界

1. 首先调用 `douyin_capabilities`。
2. 仅在返回 `mode=candidate-only` 且协议受支持时继续。
3. MCP 不可用时停止，提示用户在 WorkBuddy 的 MCP 页面连接
   `douyin-knowledge`；不要尝试读取私有目录或改用任意 Shell。
4. 不执行登录、同步、下载、本地 ASR/OCR、长时间分析、发布、对账、迁移或历史
   清理。这些操作必须交还给已验证的本地编排器。

## 查询状态

- 使用 `douyin_doctor` 检查安全能力，不读取凭证内容。
- 使用 `douyin_status` 报告计数、资源占用和发布状态。
- 使用 `douyin_plan` 规划一至五个稳定 `job_ref`，不改变本地状态。
- 只报告安全摘要、计数、布尔值、稳定引用、相对 handle 和文档化错误字段。

## 生成候选

执行任何语义任务前完整读取
[references/gateway-workflow.md](references/gateway-workflow.md)，然后严格按其中顺序：

1. 只接手一个已经准备到 packet 阶段的固定 `job_ref`。
2. 在用户明确授权该固定任务后调用 `douyin_prepare_handoff`。
3. 读取 manifest 和全部文本 handle，并按清单顺序读取证据块。
4. 打开全部 visual handle，实际检查每张图片的像素内容。
5. 要求 `douyin_assignment_status` 的两个 missing 数组均为空。
6. 根据包内指令和 schema 生成一个纯 JSON 候选，不使用网络补充信息。
7. 通过 `douyin_submit_candidate` 提交对象；不要只在对话中展示 JSON。
8. 只允许针对确定性错误修正一次，然后停止并保留现场。
9. 成功导入或明确放弃后，经用户授权调用 `douyin_cleanup_assignment`。

## 判定完成

- 只有提交工具返回确定性 ingest 成功，才报告“候选已导入”。
- 候选导入不代表视频处理完成。
- 只有本地编排器后续发布并对账为 `accepted`，视频才算完成。
- WorkBuddy 不能真实查看全部图片时，报告视觉能力缺口并停止提交。

## 保护隐私

- 不请求或输出绝对路径、Cookie、平台 URL、原始平台 ID、数据库行、日志、清理
  token 或其他任务资料。
- 不访问 manifest 未列出的内容，不进行网络富化，不拆分或转派同一证据包。
- 让同一个 WorkBuddy 执行者亲自读完全部文本并查看全部画面。
