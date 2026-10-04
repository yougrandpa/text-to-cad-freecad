# Artifact Boundary 重构进度

本轮执行下载目录中的 `text-to-cad-freecad-refactor-plan.md`，先交付 Phase 1 的基础增量。Typed IR 继续作为创作源，FreeCAD 继续运行在独立 worker 内。

## 已实施

- `tcad/artifacts/manifest.py` 定义 `ArtifactSet`、`AttemptArtifact`、`PublishedArtifact` 和生命周期状态。清单记录模型、版本、attempt、IR 哈希、文件哈希与字节数。
- 提交时将实际构建输入保存到 attempt 的 `ir.json`，生成 `verifying` 清单。Gate 的新入口 `evaluate_artifact(directory)` 从清单获取版本，只读取这一目录中的构建输入、测量和导出。完整性失败时不能借用源模型的测量证据。
- Gate 通过后，提交链路将绑定该 attempt 的 `gate_report.json` 写入清单。发布器拒绝失败报告、内容损坏和输入哈希不匹配的产物。
- 发布前将产物集合原子保存到 `data/artifact_sets/<hash>/`。原有 `data/artifacts/<model>/v<N>/` 保留为兼容的版本目录；同版本重试不会删除旧 artifact_id 的查询对象。
- `geo_measure` 只读取产物中的测量，不读取 IR，也不调用 worker。省略 `artifact_id` 时要求当前版本已有清单；传入 ID 时可以查询旧构建，且不会查询当前 IR 版本。
- 新增 `GET /artifact-sets/{artifact_id}?model_id=...` 和 `GET /artifact-sets/{artifact_id}/measurements?model_id=...`。可在未启动 worker 的情况下查询。版本产物列表和提交结果暴露 `artifact_id`。

查询验证文件完整性。文件缺失、哈希不符、模型或版本不匹配均返回错误，不以旧测量代替当前构建。

这里的 artifact_id 绑定构建 attempt 和输出文件，不是计划中用于跨版本复用的 BuildDigest。不可变产物是持久证据，不能当作可随时清除的派生缓存。

## 兼容性

旧 Gate 的版本入口和无清单的文件测试仍保留。旧产物需要重新 `ir_commit` 后才能使用新的 ArtifactReader 和 `geo_measure`；不会在查询时静默重建或把旧报告升级为新验证结果。

当前仍保留原有失败 attempt 清理行为，失败详情继续存于 `gate_reports/`。尚未提供失败 attempt 的持久 artifact_id 查询。

## 下一增量

Phase 1 尚未全部完成：Web Viewer 的 mesh/render 路径、`geo_view`、按需导出和运动预览仍沿用现有实现。下一步先产出共享的 Artifact Scene，再迁移 Viewer 与 Snapshot，使查询和渲染不再重建当前 IR，并将派生图像移出发布目录。

Phase 2–5（Build Runtime、Warm Worker Pool、可重建缓存、Viewer Core/Host 分离和 Assembly 构建图）尚未实施。
