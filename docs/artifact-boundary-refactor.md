# Artifact Boundary 重构进度

按下载目录中的 `text-to-cad-freecad-refactor-plan.md`，已实现 Phase 1 的 Artifact、Gate、Measure、Viewer 和 Snapshot 查询边界。Typed IR 继续作为创作源，FreeCAD 继续运行在独立 worker 内。

## 已实施

- `tcad/artifacts/manifest.py` 定义 `ArtifactSet`、`AttemptArtifact`、`PublishedArtifact` 和生命周期状态。清单记录模型、版本、attempt、IR 哈希、文件哈希与字节数。
- 提交时将实际构建输入保存到 attempt 的 `ir.json`，生成 `verifying` 清单。Gate 的新入口 `evaluate_artifact(directory)` 从清单获取版本，只读取这一目录中的构建输入、测量和导出。完整性失败时不能借用源模型的测量证据。
- Gate 通过后，提交链路将绑定该 attempt 的 `gate_report.json` 写入清单。发布器拒绝失败报告、内容损坏和输入哈希不匹配的产物。
- 发布前将产物集合原子保存到 `data/artifact_sets/<hash>/`。原有 `data/artifacts/<model>/v<N>/` 保留为兼容的版本目录；同版本重试不会删除旧 artifact_id 的查询对象。
- `geo_measure` 只读取产物中的测量，不读取 IR，也不调用 worker。省略 `artifact_id` 时要求当前版本已有清单；传入 ID 时可以查询旧构建，且不会查询当前 IR 版本。
- 新增 `GET /artifact-sets/{artifact_id}?model_id=...` 和 `GET /artifact-sets/{artifact_id}/measurements?model_id=...`。可在未启动 worker 的情况下查询。版本产物列表和提交结果暴露 `artifact_id`。
- 构建事务生成 `scene.json` 并纳入清单。静态模型从本次导出的 FCStd 读取网格；原生装配在构建时保存求解帧。SceneModel 校验网格、运动区间、帧矩阵和时间，并与同一产物的测量核对。Scene 生成失败阻止发布。
- Web mesh/render API 和 `geo_view` 只读取保存的 Scene，不重新编译源 IR，也不启动 worker。Viewer 和 Snapshot 共用网格、运动声明和原生装配帧。Web 默认显示最近发布的构建；指定版本或 `artifact_id` 可以锁定构建。`geo_view` 省略 ID 时仍要求当前 IR 版本已经提交。
- 新增 `GET /artifact-sets/{artifact_id}/scene?model_id=...`。PNG 响应通过 `X-Artifact-ID/Version/Status` 标识构建；GPU 失败时的静态预览锁定同一个 ID。界面区分构建版本、几何验证状态和源 IR 版本。
- `geo_view` 和 Web PNG 共用 `render_snapshot`。派生图片存放于 `data/derived/snapshots/<artifact hash>/<model>/<settings hash>/`，使用独立临时目录和原子替换。删除截图缓存后可从保存的 Scene 重建。发布产物目录不写入截图。
- 普通网页查看所需的 IR、产物列表、文件和 verdict 查询也不初始化 worker。运动截图支持 `driver_angle_deg` 或 `frame_index`；这些姿态仅用于运动学预览。

查询验证文件完整性。文件缺失、哈希不符、模型或版本不匹配均返回错误，不以旧测量代替当前构建。

这里的 artifact_id 绑定构建 attempt 和输出文件，不是计划中用于跨版本复用的 BuildDigest。不可变产物是持久证据，不能当作可随时清除的派生缓存。

## 兼容性

旧 Gate 的版本入口和无清单的文件测试仍保留。无清单的旧产物需要重新 `ir_commit` 后才能使用新的 ArtifactReader 和 `geo_measure`；已有清单但缺少 `scene.json` 的旧构建也需要重新提交，才能预览。不会在查询时静默重建或把旧报告升级为新验证结果。

当前 Scene 网格精度在构建时固定为 `tolerance=0.5`。请求其他精度会返回 409；`force` 仅刷新派生响应或图片，不重新构建几何。未构建的显式版本返回 404，不借用其他版本。

当前仍保留原有失败 attempt 清理行为，失败详情继续存于 `gate_reports/`。尚未提供失败 attempt 的持久 artifact_id 查询。

## 后续范围

核心查询边界已建立。按需导出、`assembly_simulate` / `assembly_export` 和专门的运动干涉检查仍沿用原来的命令链路，未全部迁移到 Artifact；失败 attempt 的持久查询仍待实施。

下一步可以引入 BuildDigest 与可重建缓存策略，再抽象 Build Job / Scheduler。当前 `artifact_id` 不能替代 BuildDigest。WebGL 与 Python 光栅器已共用 Scene 数据，但尚未完成 Viewer Core/Host 分离和统一像素渲染实现。Warm Worker Pool、取消/合并构建和 Assembly 构建图也尚未实施。

## 验证

回归覆盖已发布版本选择、显式构建身份、损坏产物拒绝、缓存删除重建、渲染并发隔离和请求取消。真实 FreeCAD 合约测试覆盖静态、指定旋转运动、原生动态装配和原生静态装配；移除创作源并禁止 worker 请求后，Viewer、Web PNG 与 `geo_view` 仍能读取已发布产物。网页检查确认未启动 worker 时可显示已验证构建。

本轮检查：`pytest tests/unit tests/contract -q` 1412 项通过；`node --test tests/frontend/*.test.mjs` 33 项通过；`git diff --check` 通过。未调用真实模型服务 E2E。
