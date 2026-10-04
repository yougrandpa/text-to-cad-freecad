# 阶段 C 交付状态：会话管理与可恢复运行

> 本轮目标见用户给出的《对话式 3D CAD 工作台》任务书。本文只记录**本阶段**
> （§7 会话管理、§8 任务生命周期）的完成情况与证据，并如实区分
> 已实现并验证 / 已实现但受环境阻塞 / 尚未实现。

## 一、这一阶段改变了什么

### 1. 运行不再等于连接（§8 的核心）

改造前：`/chat` 的生成器**拥有**引擎任务，`finally` 里 `task.cancel()`。
后果是关掉标签页、刷新页面、切到别的会话，都会**静默杀掉正在跑的回合**。
"重连"只能靠重发 `/chat`，而 `/chat` 会写入用户消息——于是重连变成了重复建模。

改造后引入了 `tcad/server/runs.py`：

| 概念 | 位置 | 职责 |
| --- | --- | --- |
| `Run` | `tcad/server/runs.py` | 拥有工作、事件序号和事件日志；连接只是它的观众 |
| `RunHub` | 同上 | 进程内的运行注册表；并发上限（默认 1，`loop.max_concurrent_turns`）；排队 |
| `Run.follow(after_seq)` | 同上 | 从游标重放，然后跟随实时事件 |
| `Run.request_stop()` | 同上 | 真正取消引擎任务；停止前先记录 `saved` 摘要 |
| `Run.snapshot()` / `snapshot_of()` | 同上 | 权威状态；重启后从数据库回答"已中断，可重试" |

具体行为：

- **断开连接不取消任务**：`/chat` 的 SSE 生成器只做 `run.follow()`，客户端离开
  只移除观众。事件继续写入日志，刷新即可从游标读回。
- **重连是一次读取**：`GET /runs/{id}/stream?after_seq=N`、`GET /runs/{id}/events`、
  `GET /runs/{id}`。前端 `reattachRun()` 走的就是这三个只读接口，**不重发 `/chat`**。
- **停止是独立动作**：`POST /chat/interrupt` 返回 `stage`、`reaches_engine` 和
  `saved`（已保存的消息数、基线 IR 版本、Gate 结论），不承诺不存在的回滚。
- **一模型一写者**：同一会话已有运行 → 409，并在消息里给出重连地址。
- **连接中断 ≠ 失败**：事件里只有 `result`/`error`/终态 `state` 才算结论；
  断流时前端明确提示"连接中断不是任务失败"。
- **状态字段不等于恢复执行**：进程启动后第一次读取会话/运行时，把数据库里
  残留的 `running` 回合标成 `interrupted`（已跳过本进程持有的运行），
  前端显示"已中断"。

### 2. 会话管理（§7）

- 存储层（`tcad/store/session_db.py`）：`title / pinned / archived_at / trashed_at /
  last_read_at` 五个新列 + `run_events` + `pending_cleanup` 表；
  `_migrate()` 用 `PRAGMA table_info` + `ALTER TABLE` 给**已有数据库**补列。
- 视图语义用两个时间列表达，所以"先归档再删除，恢复后回到归档区"是自然结果。
- 搜索：标题 + 消息正文，用 `instr(lower(...))` 而不是 `LIKE`，
  所以搜 `100%` 就是找百分号本身。
- 永久删除：`confirm` 必须回填会话 id；返回逐表删除行数、要清理的文件、
  以及**模型是否被其它会话共享**（共享则拒绝删除 IR 快照与产物）。
- 文件清理失败会记进 `pending_cleanup`，`POST /maintenance/cleanup` 可重试，
  不默认自动清理。
- 直接链接不能绕过状态：回收站里的会话，`GET /threads/{id}/messages` 返回 410。
- 前端侧栏：活动/已归档/回收站三个入口 + 搜索框 + 每行 `⋯` 菜单
  （重命名/置顶/归档/移入回收站/恢复/永久删除），行本身可 Tab 聚焦、Enter 打开，
  菜单 `role="menu"`、Esc 关闭并把焦点还给触发按钮。

## 二、已实现并验证

| 项目 | 证据 |
| --- | --- |
| 单元测试全绿 | `.venv/bin/python -m pytest tests/unit -q` → **928 passed, 2 skipped**（其中 `test_session_management.py` 22 项、`test_server_session_management.py` 11 项、`test_server_ui.py` 新增 6 项） |
| 旧库迁移不丢数据 | `test_an_old_database_opens_with_its_sessions_intact`：手工造一份旧 schema 的 sqlite，用新代码打开后列已补齐、会话/消息/运行状态仍在；重复打开幂等 |
| 真实服务 + 真实重启 | `tests/manual/live_session_management.py`：起**真的 uvicorn 进程**（独立 data dir），HTTP 走完重命名/置顶/归档/回收站/永久删除/批量归档，然后**杀掉进程再起一个**，14/14 检查通过（含"重启后活动/归档视图完全一致"、"重启后置顶仍是置顶"、"重启后仍能按标题搜索到"） |
| 断开不等于停止 | `test_a_client_that_leaves_does_not_stop_the_turn`：断开后模型调用**没有**被取消，运行仍 live，`replay()` 能从游标读回（`after_seq=1` → 下一条 `seq==2`） |
| 停止真的到达引擎 | `test_interrupting_a_running_turn_ends_the_stream_with_an_aborted_verdict`：`result` 帧 `state == "aborted"`，`blocking.cancelled` 置位，随后快照 `state == "stopped"` |
| 重连不写入 | `test_a_cursor_reconnect_does_not_write_to_the_session`：读运行状态前后消息数不变；重复 request_id 得到 409 且提示 `/runs/{id}/stream` |
| 前端确实是读而不是重发 | `test_a_refresh_reattaches_by_reading_and_never_re_sends_chat` 检查 `reattachRun()` 内不含 `streamChat`、含 `after_seq=` |
| 前端语法 | `node --check tcad/server/ui/app.js` 通过 |

未复现的旧行为（已随改造删除的断言）：`test_client_disconnect_cancels_the_running_turn`
已被改写为相反的契约——那是本阶段的目标，不是回归。

## 三、已实现但受环境阻塞（BLOCKED）

| 项目 | 阻塞原因 | 已做到哪一步 |
| --- | --- | --- |
| 浏览器像素级验收（面板拖拽、1366/1440/1920 断点、视口遮挡判断、截图） | 自动化浏览器上报 0×0 视口，无法取到真实渲染结果 | 代码与源级测试就位；**未声称**截图或视觉结论 |
| 真实图片→CAD（§6 验收第 7 条） | 没有可用的视觉模型服务/额度 | 能力探测、真实像素入请求、拒绝时的显式提示均已用真实 HTTP 证据验证（阶段 B 完成） |
| FreeCAD 真机重建 | 本阶段未依赖它；阶段 A/B 的网格链路未改动 | 未触碰 |

## 四、尚未实现（下一步，阶段 D）

- 对象/特征树 ↔ 模型双向选择、显示/隐藏/隔离、面/边选择的诚实降级（§9）。
- 参数表单带单位、按明确版本提交并触发后端重建与重验证（§9）。
- 成果卡片/版本列表的最终修整与浏览器回归（§10）。
- 完整交付文档：接口说明汇总、快捷键文档、浏览器测试记录、真实 CAD 样例。

## 五、需要注意的两处设计取舍

1. **并发默认是 1**（`loop.max_concurrent_turns`）。所有回合共用一个 FreeCAD worker
   进程，两个回合交错下发命令是真实的损坏风险。第二个回合被**排队**而不是拒绝，
   并在事件里说明"排队中"。
2. **永久删除会删模型产物**，但只在没有其它会话引用该模型时；被引用时返回
   `model_files_removed: []` 并在 `note` 里说明原因。删除的目录限于
   `data/{models,artifacts,gate_reports}/<model_id>`，名字里带 model_id 但不在
   已知目录中的文件**只报告不删除**。
