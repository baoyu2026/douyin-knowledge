# Gateway Workflow

## 工具表

| MCP 工具 | 是否改变状态 | 用途 |
| --- | --- | --- |
| `douyin_capabilities` | 否 | 协商协议、模式和限制 |
| `douyin_doctor` | 否 | 返回安全的本地能力检查 |
| `douyin_status` | 否 | 返回收藏、资源和发布状态 |
| `douyin_plan` | 否 | 规划一至五个稳定任务引用 |
| `douyin_prepare_handoff` | 是 | 为 packet-ready 任务创建隔离 assignment |
| `douyin_get_manifest` | 仅记录读取 | 返回不可变清单和 handle |
| `douyin_read_text` | 仅记录读取 | 读取一个经过哈希验证的文本资源 |
| `douyin_open_visual` | 仅记录读取 | 以 MCP 图片内容打开一个关键帧 |
| `douyin_assignment_status` | 否 | 返回仍未读取的文本和画面 handle |
| `douyin_submit_candidate` | 是 | 原子写入并确定性导入候选对象 |
| `douyin_cleanup_assignment` | 是 | 清理已验证的 handoff 并释放语义槽 |

所有 JSON 工具都返回统一 envelope。先检查 `ok`；失败时只使用 `error.code`、
`retryable`、`preserved_checkpoint` 和 `user_action` 决定下一步。

## 固定任务

使用 `douyin_plan` 返回的稳定 `job_ref`。不要自行构造引用，不要调用动态“下一条”，
不要因为收藏变化替换原任务。Gateway 不能把任务推进到 packet；如果 prepare 返回尚未
准备，停止并让本地编排器继续同一 `job_ref`。

调用 `douyin_prepare_handoff` 前说明：

- 当前固定 `job_ref`；
- 将创建一份隔离的脱敏证据包；
- WorkBuddy 将读取全部文本和图片并提交一个候选；
- 候选不会自动发布。

用户对该范围的明确执行请求即为授权。不要要求固定口令，也不要把授权扩大到其他任务。

## 完整读取

1. 保存 prepare 返回的 `assignment_ref`。
2. 调用 `douyin_get_manifest`。
3. 读取 instruction、packet、schema 以及所有其他非视觉 handle。
4. 按 `evidence-manifest.json` 给出的顺序读取全部证据块。
5. 对清单中的每个 visual handle 调用 `douyin_open_visual` 并检查图片内容。
6. 不因画面相似、文字很长或上下文紧张而跳过任何 handle。
7. 调用 `douyin_assignment_status`，要求 `missing_text_handles=[]` 且
   `missing_visual_handles=[]`。

文本读取记录不等于理解，图片工具返回成功也不等于已检查像素。无法完成真实看图时停止。

## 候选生成和提交

严格服从 assignment 内的 instruction 和 `candidate.schema.json`。只根据证据包形成
结论，不调用 WebSearch、WebFetch 或其他外部知识源。最终候选必须：

- 是一个 JSON object，不包含 Markdown 代码围栏或解释文字；
- 满足 schema、证据引用、隐私和内容完整性要求；
- 只选择 3 至 8 个真正支持正文论点的视觉证据；
- 将选中的画面绑定到对应论证步骤；
- 不包含绝对路径、平台 URL、原始 ID 或包外事实。

将对象作为 `candidate` 参数调用 `douyin_submit_candidate`。以返回 envelope 为准。
若第一次被确定性校验拒绝，依据 `user_action` 和错误字段修正一次；第二次失败后停止，不再
创建新 assignment，也不绕过校验。

## 清理和交还

成功导入或用户明确放弃当前 assignment 后，说明清理只删除该隔离 handoff 并释放槽位，
再以同一 `assignment_ref` 调用 `douyin_cleanup_assignment`。不要请求清理 token；Gateway
私下保存它。

最后把控制权交还本地编排器。仅报告候选导入结果，不宣称已发布。发布和 reconcile 必须
通过本地 CLI 串行执行，并以最新事务达到 `accepted` 作为完成证据。
