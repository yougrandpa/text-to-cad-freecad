# 阶段 C 状态核对：会话与运行生命周期

更新日期：2026-10-04。代码基线：`9bf00e0`。

原记录把可恢复运行和完整会话管理写成已交付，但当前仓库没有
`tcad/server/runs.py`、RunHub、运行重连路由或这些会话管理接口。
本文按当前代码纠正状态；完整交付范围见 [DELIVERY.md](DELIVERY.md)。后续需求复核新增 `draft` 终态与
`completion_review` 结果，构建通过后不会自动停止；原始需求保存和旧会话回填
经回归覆盖。该功能不等同于运行重连或恢复，见 [功能验收说明](docs/functional-acceptance.md)。

## 已实现

| 能力 | 当前实现与证据 |
| --- | --- |
| 新建及切换会话 | `POST /sessions` 同时创建模型和会话；会话不能换绑模型。`tests/unit/test_server_sessions.py` 覆盖创建、列表、拒绝覆盖和模型绑定 |
| 消息持久化 | SQLite 保存会话、回合和文本消息；`GET /threads/{id}/messages` 读取。`tests/unit/test_store_session_db.py` 覆盖持久化与列表排序 |
| 回合互斥 | 同会话或模型的在飞回合拒绝新请求；注册边界再次检查。`tests/unit/test_server_turn_exclusion.py` 覆盖 HTTP 拒绝及 SSE 注册竞态 |
| 停止 | `POST /chat/interrupt` 接收 `request_id`；运行中取消任务，提前停止请求在注册时生效。`tests/unit/test_server_interrupt.py` 覆盖 aborted 结果和停止竞态 |
| 断连清理 | SSE 生成器清理时取消引擎任务，直到取消清理完成才释放模型占用。`test_disconnect_keeps_model_busy_until_cancellation_cleanup_finishes` 覆盖该行为 |
| 会话切换隔离 | 前端切换会话会断开当前请求；会话标记拒绝迟到视口响应。`tests/frontend/app_viewport.test.mjs` 覆盖异步响应竞态 |

客户端连接目前仍拥有回合的生命周期。刷新、关页或切换会话会断开流并取消任务；
消息历史可以再次读取，但这不恢复工具轨迹或被取消回合的执行。
停止接口返回 `interrupted / stage`，没有 `saved / reaches_engine` 字段。
同会话或模型的第二个回合被拒绝，没有默认并发为 1 的排队器。

## 尚未实现

- Run / RunHub、`GET /runs*`、`GET /threads/{id}/events`、事件游标和只读重连。
- 断开连接后继续运行、刷新后跟随原运行、重启后恢复或标记可重试运行。
- 重命名、置顶、归档、回收站、恢复、永久删除、批量操作和搜索接口。
- `title / pinned / archived_at / trashed_at / last_read_at` 管理列、
  `run_events / pending_cleanup` 表，以及这些新增字段的旧库迁移。
- 阶段 D 的参数表单、对象引用、模型与特征树双向选择。

当前会话标题和摘要由消息历史派生，不表示已经实现可编辑标题或搜索。
不能根据消息持久化测试推断完整旧库迁移、文件清理或重启恢复已通过验收。

## 验证记录

2026-10-04 对基线代码运行全量 Python 测试：**1258 passed**，76.58 秒；
Node 前端运行时测试：**17 passed**；compileall 与空白检查通过。
本次文档核对没有执行新的浏览器像素验收。

原记录中的 `928 passed, 2 skipped`、手动会话管理 14/14、断连继续运行、
游标重连及旧库迁移验收，没有对应的当前实现或脚本，撤回其作为当前完成证据的引用。
交互预览历史验收保留在 [验收记录](docs/interactive-preview-acceptance.md)，
其中的内核和测试环境不应当作为本次环境的默认结论。
