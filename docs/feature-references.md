# 特征引用与孔径编辑

本功能落实 FreeCAD 增量借鉴方案的 P0/P1a 与 P1b：从特征树引用保存产物中的对象，把引用送入模型上下文，使用声明式工具修改同一版本，再经过 FreeCAD、Gate 与需求复核发布。Typed IR 仍是创作源；选择目录只读冻结文件，不启动几何 worker。

## 支持范围

Body、Sketch、Feature 可以作为聊天引用。导入 PartRef 只允许查看。首个自动编辑配方仅支持单个静态 Body 上的一个圆草图与最终 ThroughAll Pocket，且保存的 digest 必须测得一个同径通孔。草图需有一个可编辑的 Radius 或 Diameter 驱动约束；Block 固定的圆、下游特征覆盖的孔、多 Body、运动与装配没有该配方能力，仍可查看。

三维视图支持 Body、Face、Edge 点击引用与黄色高亮。静态多 Body、具有独立 body_id 的重复几何实例、预设运动和原生动画使用同一套顶点范围；点击与高亮都针对当前显示姿态。面、边没有已证明的 IR 特征来源，统一降级为只读引用；孔径编辑仍从特征树引用圆草图或 Pocket。worker 身份握手与资源观测留在 P2；外部宿主适配按 P3 的实际使用需求推进。

## 使用流程

1. 提交并通过当前模型的 Gate。
2. 在特征树点击圆草图或 Pocket 的“引用”，消息输入区显示名称和版本。
3. 输入“把选中孔的直径改成 8 毫米”并发送。
4. 服务端核对冻结源码、测量、产物身份及访问模式。模型使用 `cad_set_hole_diameter`，把圆半径和驱动约束一起改为 4 毫米；需要时同步用户改变的已记录需求。
5. `ir_commit` 重建并重新运行 Gate；`design_review` 复核已记录需求。发布后清除旧引用，可立即引用新版本。

chips 支持移除、清空，最多八个对象，切换会话清空。忙闲状态由一个控制器同时更新 chips 和树按钮；回合中刷新树不会让按钮永久禁用。网络失败或缺失终态后，界面清除引用并刷新当前状态。

## API 与版本约束

`GET /models/{model_id}/selection-targets` 返回当前已验证产物的引用目录和可用能力。每个 `SelectionRef` 包含 `schema_version=1`、`model_id`、`artifact_id`、`ir_version`、`body_id`、`entity_kind`，以及与种类对应的 `sketch_id`、`feature_id` 或 `local_sub_id`。面/边局部名称必须来自已保存映射，服务端重新核验；不接受客户端标签、能力或未经映射的 FaceN/EdgeN。

`POST /chat` 的 `selection_context` 是可选字段；携带引用时必须同时提供 `operation_id`。原有无引用请求继续有效。

```json
{
  "model_id": "reference_plate",
  "thread_id": "th-reference_plate",
  "kind": "modify",
  "text": "把选中孔的直径改成 8 毫米",
  "operation_id": "resize-eight-001",
  "selection_context": {
    "schema_version": 1,
    "selection_refs": [{
      "schema_version": 1,
      "model_id": "reference_plate",
      "artifact_id": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
      "ir_version": 0,
      "body_id": "plate_body",
      "entity_kind": "sketch",
      "sketch_id": "hole_circle"
    }]
  }
}
```

示例中的产物 ID 为占位值，实际请求必须原样使用目录返回的完整 ref 对象。所有引用必须来自同一模型、产物和 IR 版本；禁止重复对象。

`EditPrecondition` 保存解析目标、期望的完整 IR 和来源 ArtifactReader。每个 `ir_patch`、compact 工具及 `ir_commit` 检查版本、源码内容和来源构建身份；`base_version="current"` 不能接纳其他写者的改动。本回合成功应用的补丁才推进期望源码，包括引擎保存用户需求的补丁。选择回合不提供任意 Python 执行工具。

## 重试与中断

`request_id` 用于取消；`operation_id` 用于去重。账本保存于数据目录的 `chat_operations.sqlite3`，请求指纹包含规范化的模型、会话、正文、权限、种类和引用，不含取消 ID。

| 情况 | 结果 |
| --- | --- |
| 相同操作 ID 和请求，已有终态 | 回放原来的 start 与终态，不调用模型、不应用补丁 |
| 相同操作 ID，不同请求 | `operation_conflict` |
| 操作尚在运行，或进程退出后没有终态 | `operation_in_progress`，先检查状态 |
| 连接断开 | 取消回合，保持模型占用直至清理完成，记录 `operation_interrupted` |
| 断流后重试或重启服务后重试 | 只回放保存的中断结果 |

已落地的源码补丁不会因中断自动撤销。界面不自动重发修改；再次发送是新的用户操作。API 调用者重试同一次请求时必须保留原 `operation_id`。操作重放优先于陈旧引用检查，因为它读取原结果，不执行新编辑。

常见错误有 `stale_selection`、`revision_conflict`、`unmapped_entity`、`ambiguous_target`、`forbidden` 和 `selection_disabled`；HTTP 拒绝发生在调用模型之前。工具内拒绝沿用现有 ToolError，并带错误码与重选建议。

## 兼容与关闭

设置 `selection.enabled: false`，关闭目录与新的引用请求，并停止注册孔径配方。普通建模、旧 Scene、截图、历史产物与导出继续走原有链路。关闭开关不删除已完成操作账本，已完成请求仍可回放。

缺少已验证产物或源码已改变时，特征树继续显示，隐藏引用操作；不会为了补目录重新构建当前 IR。精细面边选择尚未开放，因此本阶段没有映射索引恢复动作。

## 可复现验收

固定 fixture：`tests/fixtures/referenced_plate.json`，40×30×6 板与中心 Ø6 通孔。

- `tests/unit/test_references.py`、`test_reference_api.py`：身份、数量、重复、版本、同版本替换、compact/commit 前置条件、访问模式与账本。
- `tests/frontend/references.test.mjs`：真实控制器按钮行为及 app 发送、刷新、解锁链路。
- `tests/contract/test_reference_delivery.py`：真实 FreeCAD、Gate、STEP 回读，以及真实 Chromium 点击引用、发送、清除和再次引用。
- `tests/contract/test_reference_disconnect.py`：补丁落盘后关闭真实 HTTP 连接，检查取消、终态、重放和重启恢复。
- `tests/unit/test_reference_solver.py`：失败的 SketchObject 求解状态不能被 FullyConstrained 标志掩盖；旧 digest 继续可读。

真实内核验收的模型回复是确定性脚本，证明工具与应用闭环；不代表真实提供商理解任意自然语言的成功率。测量验证孔径 8、深度 6、贯穿、中心不变、包围盒不变、实体数 1，以及 STEP 回读体积 `7200 − π × 4² × 6`。截图仅验证交互呈现。

运行记录、源码指纹、浏览器截图和新产物保存在 `review/reference-rewrite/`。验收使用临时数据目录，不读取或修改用户保存的模型服务密钥。


## P1b 冻结几何映射

新构建的 `scene.json` 在 schema 1 上增加可选 `pick_mapping`，旧 Scene 缺省为 null，仍能浏览与截图。映射含生成器 `freecad-face-mesh-v1`、原始顶点/三角形的 SHA-256、完整 Body 顶点范围、每个 Face 的连续三角范围与每个 Edge 的折线段索引。退化面可保留零三角形，不会产生错误的可点击区域。所有范围、索引、身份、数量和姿态范围都必须通过服务端校验。

映射参与已有 Artifact manifest 的文件哈希；自身没有 artifact_id，响应外层提供产物身份。实体由 `(body_id, entity_kind, local_sub_id)` 定位，同名 Face1 不跨 Body 合并；重建后不得复用旧选择。原生动画沿用当前 IR 的独立 Body 身份，没有另建 occurrence 命名体系。浏览和选择读取保存的 scene，不调用 worker 或重编译 IR；缺失映射时禁用拾取，重新构建可生成新映射。此阶段没有自动恢复历史产物索引。

选择工具位于三维视图顶部，可切换旋转、实体、面和边。单击选择，拖动仍用于相机控制；只有最前面的面和未被实体遮挡的边能够命中。引用上限和版本约束与特征树一致。面/边消息自动使用 `inspect` 与 `read_only`，不能调用补丁、提交或任意 Python。Body 引用仍按其已验证能力处理。快照宿主没有新增聊天或编辑能力；Web host 增补选择回调及 `setPickMode`，回调携带冻结产物身份，销毁后停止。

P1b 验收位于 `tests/unit/test_pick_mapping.py`、`tests/frontend/picking.test.mjs` 和 `tests/contract/test_geometry_picking.py`。真实 FreeCAD 检查静态重复几何、完整索引和不同边细分；前端检查三角细分、遮挡、运动与重复实例。浏览器实际点击面/边、检查高亮、移除引用并发送只读消息。证据保存在 `review/geometry-picking/`；P1a 已提交的证据保留原始版本。
