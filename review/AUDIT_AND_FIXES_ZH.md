# text-to-cad-freecad 审阅与修复报告

日期：2026-09-19 起，持续更新至 2026-09-20 · 分支 `main` · 基线提交 `00db836`
工作目录：`/Users/slg/workspace/text_to_cad`
**本次全部修改均未提交、未推送**（最后一次成功读到 `git status --porcelain` 时是 38 项，含用户先前未提交的改动，一律保留）。
**当前限制**：2026-09-20 起本机 `git` 被系统拦下——`git status`/`rev-parse` 均输出 "You have not agreed to the Xcode license agreements"（需要用户执行一次 `sudo xcodebuild -license`）。因此"工作区清洁度/条数"本轮**无法**重新核对，上面 38 项是最后一次可读到的值，不假装它还新鲜；其余结论都不依赖 git。

## 0. 怎么读这份报告

* 第 2/3/4 节是任务书 §8 要求的三张清单：**已完成并验证**、**已实现但受环境阻塞（BLOCKED）**、**尚未实现 / 未修复风险**。
* 第 5 节是缺陷明细：文件位置 → 问题 → 复现 → 根因 → 修复 → 对应测试。
* 第 6 节是任务书 §6 要求的能力矩阵，第 7 节是三层测试与运行命令，第 8 节是跨平台运行，第 9 节是可下载的验收产物。
* 判定"已验证"的标准只有两个：命令行真的跑过并贴出了结果，或磁盘上真的有产物文件。没有"应该可以"。

## 1. 环境与基线

| 项 | 实测值 |
| --- | --- |
| OS | macOS 26.6.2（Darwin 25.6.0），arm64 |
| 宿主 Python | 3.13.12（`.venv/bin/python`，Gate/编译器/服务端都跑在这里） |
| FreeCAD | `FreeCAD 26.3.0 Revision: 48708 (Git)`，可执行文件 `free-cad/FreeCAD/build/debug/bin/FreeCADCmd`（路径可被 `TCAD_FREECAD_CMD` 覆盖，见第 8 节） |
| worker 启动 | JSONL RPC over stdio，冷启动约 0.27 s |
| `api_selftest` | 39 个 API 探测全部应答，`missing=[]` |
| 模型服务 | 仓库内只有占位配置（`TCAD_LLM_BASE_URL` 默认指向 `127.0.0.1:8000`）；**未检测到可用 provider 或密钥**，本报告不复述任何密钥值 |
| 特权工具 | `tools.privileged: []` → `raw_python` 根本没有注册；`policy.allow_privileged: false` |

### 1.1 环境诊断（一条命令）

```bash
.venv/bin/python tools/doctor.py            # 退出码 0=可运行，1=有阻断项
TCAD_FREECAD_CMD=/opt/homebrew/bin/FreeCADCmd .venv/bin/python tools/doctor.py
```

`tools/doctor.py` 分三层检查：依赖与配置 → FreeCADCmd 是否存在并能起 worker → `api_selftest` 是否应答。

**本轮修掉的一个诊断假阳性**：`request_sync()` 返回的是 worker 的结果载荷本身，不是 `{"ok":…, "result":…}` 信封；旧代码按信封读，把一次健康的自检报成 FAIL 同时又把真实结果打印出来（自相矛盾的输出）。修复见第 5 节 E-1。

### 1.2 测试基线（不要引用 README 的历史数字）

```
.venv/bin/python -m pytest tests -q                          →  925 passed, 4 skipped in 36.32s
.venv/bin/python -m pytest tests/unit -q                     →  758 passed in 17.59s
.venv/bin/python -m pytest tests/contract -q                 →  167 passed in 19.80s  （真跑 FreeCADCmd）
.venv/bin/python -m pytest tests/e2e -q -rs                  →  4 skipped（缺 provider 配置，见第 3 节）
.venv/bin/python tools/build_acceptance_artifacts.py --check →  OK（验收包与当前源码树同源，见 §32）
```

这组数字是**报告末尾重跑**的结果（2026-09-20），不是从 README 或前文抄的。整包墙钟随内核实例启动在 36 s 上下波动（31.75 / 31.88 / 32.12 / 32.35 / 33.75 / 34.04 / 35.40 / 36.32 s 八次实测）；通过项数与收集数不受影响。第 5–10 节记录第一轮（`HEAD=00db836` 基线 **570 全绿** → 该轮末 **604**：528 单测 + 76 契约）；第 11–33 节是后续逐轮追加的修复，只增不删。

**§34（第二十二轮，2026-09-23）本轮重跑的基线：** 开工时 `925 passed, 4 skipped`（与上面一致），结束时

```
.venv/bin/python -m pytest tests -q     →  998 passed, 4 skipped in 66.30s
.venv/bin/python -m pytest tests/unit -q →  784 passed      （基线 758）
```

净增 73 项（6 个新测试文件 + 6 个文件扩充）。§29/§30 里出现的 875/877 是**当时**的快照，不是当前基线。
（66.30 s / 66.19 s 是前两次全量；第 3 层解封后最终为 **1007 passed, 0 skipped in 163.79s** —— 墙钟翻倍是 4 条真机 e2e 在跑真模型。）


## 2. 已完成并验证

1. **交付物识别与"必需产物"合同**（§5-C）。`roundtrip.step` 不再能顶替 STEP；`.FCStd` 在大小写敏感文件系统上也能被发现；配置要求的导出格式若缺失，`exportability` 直接阻断失败。回归：`tests/unit/test_export_discovery.py`（17 项）。
2. **Gate 报告不再吞掉"无法证明"这条独立原因**（§5-C）。回归：`tests/unit/test_verify_gate.py::test_cannot_attest_is_reported_beside_a_failing_check`。
3. **`CheckResult.measurements` 类型不一致导致 FAIL 变成 ERROR**（§5-C）。回归：`tests/unit/test_verify_checks.py::test_a_list_measurement_still_produces_a_verdict`。
4. **op 能力真相表进入运行时**（§5-A）。`tcad/ir/capability.py` 是唯一事实源：21 个 `FeatureOp` 中 `pad`/`pocket`/`revolution`/`groove`/`fillet`/`chamfer`/`mirrored`/`linear_pattern`/`polar_pattern` 九项，加上 §31 转正的六个原语（`additive_box`/`additive_cylinder`/`additive_sphere`/`subtractive_box`/`subtractive_cylinder`/`subtractive_sphere`）、§33 转正的 `draft`/`thickness`，共 **17 项 `VERIFIED`**，其余 4 项只证明"能 `addObject`"（`EXPERIMENTAL`）；校验器逐特征告警 `op_experimental`；给模型的 `ir_patch` 工具描述由同一张表生成，不再手抄。回归：`tests/unit/test_op_capability.py`（16 项，含 4 项"枚举/两套映射/能力表"漂移守卫）。
5. **孔以 BRep 实测为准**（§5-B，本目标早前轮次完成、本轮复查仍在位）。worker 从凹圆柱面测直径/轴向/圆心/深度与通-盲；`hole_diameter`、`hole_position` 只对 `digest.holes` 打分，绝不取 IR 参数；已确认却无 BRep 证据 → 阻断级 **ERROR** `required_but_unverified`。回归：`tests/contract/test_measured_holes.py`、`tests/unit/test_verify_specexpr.py`。
6. **样例 A–E 真实内核验收 + 多视图预览 + 源码树同源证明**（§7 层二 + §6 预览 + §8 产物）。`review/acceptance/` 由当前代码重建（**最新** `attempt_id = acc-20260920T132149Z-8238f16a`，manifest 带 `source_tree`：153 个 py 文件、sha256 `df1be3a49d2e5402…`，磁盘 109 文件 = 108 产物 + `manifest.json`，36 张预览；历次 attempt `…4487725c` → `…48950611` → `…cde71e3f` → `…3f77cb68` → `…8238f16a` 见 §11、§22、§23、§32）（逐文件 sha256/字节数），样例 B 含跨 worker 重启的第二轮修改，D/E 为 revolution / groove 的解析体积与改参重算；每轮附 `preview_{iso,front,top,right}.png`，网格取自 worker 对同一份 IR 的 `tessellate`，空白图会直接中止构建而不是留一张白图充数。
7. **环境诊断脚本可用且不再夸大**（§8）。
8. **被打分的产物绑定到打分它的那次构建**（§5-C）。`run_commit` 在调用 worker 之前先往产物目录写 `build_stamp.json`（`attempt_id` + `model_id` + `ir_version` + `started_at` + 交给编译器的 IR 的 sha256），GateReport 现在把 `attempt_id`/`ir_sha256` 一起带出来。新增阻断级 `provenance` 检查：digest 或 IR 快照的 model/version 与被要求者不符 → FAIL；导出文件比本次 attempt 还早（同版本重试的遗留件）→ FAIL；目录根本没有 stamp（无法判断来源）→ FAIL 而不是可忽略的 SKIP。回归：`tests/unit/test_verify_provenance.py`（9 项）+ 真内核 `tests/contract/test_wired_pipeline.py`（4 项：活构建有 stamp、同版本重试换新 attempt、遗留 STEP 不能交差、删掉 stamp 后不能白拿 PASS）。
9. **提交管线不再在事件循环上跑 FreeCAD RPC**（§5-D）。compile/export/introspect 与 `Gate.evaluate` 全部改走 `asyncio.to_thread`；此前一次最长 120 秒的编译会把整个进程里其它会话的 SSE、健康检查和取消检查点一起冻住。"在飞的调用能不能停下来"这另一半由 §26 C-26 补上。回归：`tests/unit/test_commit_event_loop.py`（3 项心跳证明；把调用改回内联阻塞，心跳从 ~40 次掉到 0 次）。
10. **中断真的覆盖到 worker 进程与暂存目录**（§5-D，R-8）。`WorkerHandle.abort_inflight` 结束进行中的调用（杀进程 + 就地回收），调用方拿到 `kind="cancelled"` 而不是超时或崩溃误报，下一次调用惰性重启；被放弃的 staging 目录丢弃，服务退出时关闭后端。**边界**：一个服务进程只有一个 worker 进程，所以一次停止也会终止正在共享它的别的调用。回归：`tests/unit/test_worker_abort.py`（5 项）、`tests/unit/test_commit_event_loop.py`（+2 项，含变异验证）、`tests/contract/test_worker_interrupt.py`（4 项真内核）。
11. **运行时 system prompt 与真实工具集对得上**（§3）。prompt 点名的每个工具都真的注册在 `build_default_registry` 里；被声明为"Read tools"的那批（含 `geo_*` 通配展开）在注册表里的 tier 真的是 `READ`；成功判据里的字段名（`passed`）真的存在于 `GateReport`。回归：`tests/unit/test_system_prompt_truth.py`（4 项，五种变异全部被抓，含变异验证暴露出的一个守卫盲点）。详见 §27。
12. **工具描述里的坐标/方向声明改为对实测负责**（§3、§5-B）。`ir_patch` 描述里的"XY -> +Z / XZ -> -Y / YZ -> +X"不再由字面量自证，而是从描述里解析箭头、再从实体内核量出的 bbox 推出实际朝向，逐平面比对；同时把描述**推荐**给模型的 pad 参数级 `reversed`/`midplane`、以及"拼错参数必须被拒"补成真内核断言。**先探针后断言**：探针实测三条声明全部为真（默认 −Y / `reversed` +Y / `midplane` 对称 / 拼错被拒），所以本轮修的是守卫强度而非产品缺陷。回归：`tests/contract/test_sketch_planes.py`、`tests/contract/test_build_failures.py`（+2 项，五种变异各自被抓）。详见 §28；未逐 op 实测的部分记在 R-9。
13. **`reversed`/`midplane` 在 revolution 与 groove 上也按实测定向**（§5-A、§5-B，R-9 收口）。这两个 op 此前只被证明"参数名被白名单允许"，没人量过它们是否真的起作用。真内核实测（90° 是最小区分场景）：回转体默认扫向 +Y、`reversed` 扫向 −Y、`midplane` 对称，三者体积恒为 680π；板上的嵌入式 90° 环槽默认切掉 700π、`reversed` 整段落空并被按名拒绝（`did not change the solid`）、`midplane` 只切掉 350π 且实体数仍为 1。两个 op 各加一项契约断言，并做了变异验证（把 `App::PropertyBool` 分支改成空操作 / 恒 False，三条测试全红，源码已还原）。详见 §30。
14. **pocket 方向与"原点绑定"两条描述声明按实测改写**（§3、§5-B）。`ir_patch` 描述里"落空的 pocket 是安静的 no-op"这句话**是旧行为**——编译器早已按名拒绝（`did not change the solid`）；另一句暗示"坐标与原点绑定矛盾会报错"与 Sketcher 的真实语义**不符**：实测是求解器把轮廓**拖到原点**、构建照样成功、切除落在没写过的地方（600 mm² 底面积切掉 3600，而不是 400 mm² 的 2400）。两段文字都按真内核探针实测改写，并把后者钉成一项契约测试（行为特征，不是产品缺陷）。详见 §29。
15. **六个原语有了世界坐标放置载体**（§6 能力矩阵最后一条、R-6）。此前 `additive_box` 之外的原语只能建在原点：模型要求"在板上加个销钉/通孔"，构建照样成功，零件却多了一个飘在旁边的实体（Compound，被 Gate 的 `solid_count` 拒绝）。现在 `FeatureSpec.placement`（`PlacementSpec(position, axis, angle)`，世界 mm、逆时针）落到 FreeCAD 的 **`Placement`** 上，六个原语 op 全部转正；其他 op 携带 placement 会被 `placement_unused` 按名拒绝。**关键实测**：`AttachmentOffset` 在 `MapMode=Deactivated` 下完全不生效，`Placement` 才移动实体——这条差别若猜错，会得到一个"构建成功、重算通过、但放置被静默忽略"的特性。详见 §31。

## 3. 已实现但受环境阻塞（BLOCKED）

| 项 | 阻塞原因 | 解锁条件 | 现状证据 |
| --- | --- | --- | --- |
| 第 3 层"真实 LLM 端到端" | **密钥存在且有效、端点可达**（`GET /models` 200，0.38 s），但**账户余额为 0** → 每次 completion 返回 `HTTP 402 Insufficient Balance`。详细复现见 §34.16 | 给该账户充值，或换一个有额度的 provider；设 `TCAD_E2E_BASE_URL` + `TCAD_E2E_MODEL`（可选 `TCAD_E2E_API_KEY`），或直接在 UI 里保存一个（§34.14 起会回退到 `data/settings.json`） | `pytest tests/e2e -q -rs` 打印 4 条明确跳过原因，且**点名 HTTP 402 与 provider 自己的原话**，不是静默通过（修复前它打印的是 4 条关于模型行为的失败断言，见 §34.14） |
| 视觉检查类验收 | 只有真正发送图像内容的调用才算视觉检查；当前 e2e 未跑 | 同上 | 未在报告中声称任何模型看过图 |

```bash
TCAD_E2E_BASE_URL=http://127.0.0.1:8000/v1 TCAD_E2E_MODEL=<model> \
  .venv/bin/python -m pytest tests/e2e -q -rs
```

## 4. 尚未实现 / 未修复风险（按危害排序）

| 编号 | 位置 | 事实 | 后果 |
| --- | --- | --- | --- |
| R-2 | ~~`commit.py`、`worker/exporters.py`~~ **已修复**，见 §12 C-9 | 每次构建先写 `artifacts/<id>/v<N>.staging-<attempt>/`，Gate 只对这个私有目录评分，通过后用两次 `os.replace` 整体换进 `v<N>/` 并写 `manifest.json`；失败则丢弃暂存、版本目录保持上一次已验证构建 | "旧文件为本次失败导出顶包"现在是**结构性不可能**（被评分的目录里只有本次尝试写的文件），不再依赖 mtime 启发式 |
| R-3 | ~~`tcad/loop/engine.py:541-546`、`:232`~~ **已修复**，见 §19 C-17 | `build_verdict(data_dir, model_id, current_version)` 要求"报告是**当前**版本"与"该版本产物**已发布**"**同时**成立；`StoreAdapter.verdict` 是唯一入口（API/前端/下一轮上下文都走它）；失效规则从硬编码名单改成"任何 write/privileged 档工具成功执行且不是 `ir_commit` 本身" | 陈旧绿灯不能再被当成"当前通过"展示；13 项回归（含"新注册的写工具也会使通过失效"） |
| R-4 | `tcad/context/assembler.py`（预算/三级降级） | ~~生产路径没有任何调用方；`engine.py` 每轮消息只有 `[system, user]`~~ **已修复**，见 §11 C-8 | 会话历史存进了 SQLite 但没进模型；任务书 §5-D 要求接入受预算约束的上下文 |
| R-5 | ~~`tcad/config/schema.py:86-90` 默认全 `None`~~ **机制已实现**；默认不设上限是**明示策略**，不是缺失 | `tcad/loop/budget.py` 的五个上限全部在引擎里真正执行（`check_step` / `add_tokens` / `check_step_timeout` / `allow_more_compile_retries`），`configs/policies/strict.yaml` 是一份把五个**全部**设满的 overlay（overlay 只收紧、不放松），`tests/unit/test_budget_no_ceiling.py` 专门钉住语义 | 默认配置下**没有工作总量上限**（有意为之，理由写在 `LoopConfig` 文档串里）：单次调用超时仍然存在，两者不要混。要跑不可信输入或不想承担失控回合，就加载 strict overlay |
| R-6 | `tcad/ir/capability.py` 剩 4 个 `EXPERIMENTAL` | `revolution`（§13 C-10）、`groove`（§22 C-22）、`fillet`/`chamfer`（§24 C-24）、`mirrored`/`linear_pattern`/`polar_pattern`（§25 C-25）、六个原语（§31 C-31）、`draft`/`thickness`（§33 C-33）已转正；`hole`/`circular_pattern`/`multi_transform`/`datum_plane` 仍只验证到"能 `addObject`"。**边引用、面引用与面/轴/平面引用都已解决**：`base_feature`+`sub_elements` 翻成 Fillet/Chamfer 的 `Base` **与 Draft/Thickness 的 `Base`（面表）**、`FeatureSpec.plane` 翻成 `MirrorPlane` **与 `NeutralPlane`**、`params.axis` 翻成 `Direction`/`Axis`（§24/§25）、`FeatureSpec.placement` 翻成原语的 `Placement`（§31）；`multi_transform` 的 `transform_mode` 仍无 IR 字段可携带（`hole` 的缺口是"没有人量过它"，不是字段）。**补充（§34.6）**：这些载体字段在 `add_feature` 路径上曾被静默丢弃，只有 `update_feature` 能用——已修，两条路径共用一份映射 | 模型可以提交这 4 个 op 并编译成功，但形状是否符合预期无人证明；每轮告警 + 工具描述里写明 |
| R-7 | ~~`worker/compiler.py` 的 `_PROFILE_OPS`~~ **已删除**（源码里只剩旧 `.pyc`）；白名单"按属性同名收录、编译器却设不进去"的问题见 §21 C-21（已修） | 现状：`_PROFILE_OPS` 在 `tcad/` 源码中 0 命中；`params` 白名单与编译器真正兑现的键由 `test_op_capability.py` 的漂移守卫钉住 | — |
| R-8 | ~~README 限制 #7~~ **已修复**，见 §26 C-26 | 进行中的 worker 调用由 `WorkerHandle.abort_inflight` 结束（杀进程 + 就地回收），调用方拿到 `kind="cancelled"`；`run_commit` 丢弃被放弃尝试的暂存目录，`tools/serve.py` 退出时关闭后端 | 中断现在覆盖模型请求、工具批次、worker 调用与子进程清理；真内核层不覆盖"在飞调用被中止"（真实编译太快），该路径在单测层用真子进程验证 |
| R-9 | ~~`reversed` / `midplane` 只在 pad 上实测~~ **已修复**，见 §30 C-30 | 四个 op（pad / pocket / revolution / groove）的同名参数现在都有真内核断言：pad 三条方向声明、pocket 的"落空被拒 + 反向切入料 + 居中切半"、revolution 的 +Y/−Y/对称扫掠、groove 的"默认咬料 / 反向落空被按名拒绝 / 居中切半"；四条都做过变异验证（`App::PropertyBool` 分支空操作 / 恒 False → 全红） | 无（`test_param_capability.py` 的白名单覆盖"参数名被允许"，本项补齐"方向已实测"） |

本轮从该表**移出以下条目**（编号不复用，避免引用失配）：**R-1**（attempt/哈希绑定）与"错误版本 digest"那一项由 §5 C-6 修复；**R-3** 由 §19 C-17 修复；**R-5** 的机制已在 `tcad/loop/budget.py` 落地并经 `test_budget_no_ceiling.py` 钉住语义；**R-7** 的 `_PROFILE_OPS` 已从源码删除、白名单漂移由 §21 C-21 修好；**R-8**（worker 调用进行中不可取消）由 §26 C-26 修复；**R-9**（`reversed`/`midplane` 只在 pad 上实测）由 §30 C-30 修复；**"六个原语只能建在原点"**由 §31 C-31 修复（`FeatureSpec.placement` → `Placement`）；**`draft`/`thickness` 无字段可携带**由 §33 C-33 修复（面表走 `base_feature`+`sub_elements`，中性面走类型化 `plane` → `NeutralPlane`）。留在表里的只剩 **R-6** 这一条真实缺口（4 个 `EXPERIMENTAL` op：`hole`/`circular_pattern`/`multi_transform`/`datum_plane`——以 `tcad/ir/capability.py` 的 `ops_by_tier()` 输出为准；`multi_transform` 缺的是 IR 里可携带的 `transform_mode` 字段，`circular_pattern` 在本机是间距 API（用 `polar_pattern`），`hole` 与 `datum_plane` 缺的是"没有人量过"而不是字段。注意 `hole` 这个 **op** 是实验性的，但"通孔/盲孔"这项**能力**由已验证的 `pocket` + 圆草图承担，BRep 证据见 §2 第 5 条与 `tests/contract/test_measured_holes.py`），外加第 3 节的环境级 BLOCKED。

## 5. 缺陷与修复明细（本轮）

### C-1 编译器诊断文件被当成正式 STEP 交付

* **位置**：`tcad/verify/context.py:_discover_exports`
* **问题**：产物目录里只要有 `roundtrip.step`，Gate 就认为 STEP 已交付。
* **复现**：`mkdir /tmp/x && echo DATA > /tmp/x/roundtrip.step`，用旧实现 `glob('*.step')[:1]` 会命中它；`tools` 现场证据是 `compile_ir` 无条件写 `worker/compiler.py:1113` 的 `roundtrip.step`，而 `export_artifacts` 可能整个失败（`worker/exporters.py:36-38` 返回 `files={}`）。
* **根因**：识别"交付物"用的是全局 glob + 取首个匹配，没有区分"谁写的、为哪个模型写的"。
* **修复**：新增 `_DIAGNOSTIC_STEMS = {"roundtrip"}`；扩展名大小写不敏感匹配；同名多文件时**优先取以本次 `model_id` 命名的那个**；`build_check_context` 改为传入 `model_id`。
* **测试**：`tests/unit/test_export_discovery.py::test_a_build_diagnostic_is_not_a_deliverable`、`test_the_models_own_step_wins_over_the_diagnostic`、`test_the_freecad_backup_of_the_previous_build_is_never_chosen`（`.FCBak` 是上一代内容，不能为本次背书）。

### C-2 `.FCStd` 在大小写敏感文件系统上永远查不到

* **位置**：同上 + `tcad/core/wiring.py`
* **问题**：FreeCAD 写的是 `<model_id>.FCStd`，模式却用 `*.fcstd`。macOS 不敏感 → 看起来正常；Linux/WSL 敏感 → 可重新打开的参数化文档从未被验收。
* **复现**：大小写敏感卷上 `glob(os.path.join(d, "*.fcstd"))` 对 `m.FCStd` 返回空。
* **根因**：把文件系统语义当成跨平台常量。
* **修复**：`os.path.splitext(n)[1].lower() == f".{fmt}"`。
* **测试**：`@pytest.mark.parametrize("name", ["bracket.FCStd", "bracket.fcstd"])` 两项都必须识别为 `fcstd`。

### C-3 缺文件时 `exportability` 静默通过

* **位置**：`tcad/verify/checks_solid.py:ExportabilityCheck`、`VerifyConfig`、`tcad/core/wiring.py`
* **问题**：该检查只看"目录里有什么"。目录空 → SKIP；只导出 STL 没导出 STEP → PASS（它检查的那个 STL 没问题）。
* **复现**：`ExportabilityCheck().run(ctx_with({"stl": path}))` → 旧代码 `pass`。
* **根因**：没有"本次构建**必须**交付哪些格式"这个概念。
* **修复**：`VerifyConfig.required_exports`；`ExportabilityCheck` 在设置该值时逐个核对（缺失或 0 字节 → 阻断 FAIL，消息里点名缺哪个、磁盘上有什么）；未设置时保持旧行为（兼容裸 `VerifyConfig()` 的单测与第三方调用方）。`wiring.required_export_formats(cfg.storage.artifact_exports)` 把配置转成合同，并**强制加上 `fcstd`**（它由 `compile_ir` 的 `saveAs` 产出，不在 `artifact_exports` 里）。
* **测试**：`tests/unit/test_export_discovery.py` 中"缺失/空文件/全齐/未设要求保持旧语义"4 项 + 2 项 wiring 契约（必含 `fcstd`、要求项必须落在 `EXPORT_FORMATS` 内，否则一个配置笔误就会永久阻断所有构建）。真实链路 `tests/contract/test_chat_end_to_end.py` 断言 `blocking_failures == []` 仍通过 —— 说明真实构建确实交付了 step+stl+FCStd。

### C-4 检查自己报告的列表值把 FAIL 变成 ERROR

* **位置**：`tcad/verify/checks_solid.py:_r` / `_as_dict`
* **问题**：`CheckResult.measurements` 是 `dict[str, float|str|bool]`，而 `exportability` 一直传 `{"empty_formats": [...]}`。构造即抛 `ValidationError`，被 `Gate._safe_run` 记成 **ERROR**，消息是十几行 pydantic 转储，检查原本要说的结论丢了。
* **复现**：`_r(SomeCheck(), "fail", "m", measurements={"k": ["a","b"]})` → 修复前抛异常。
* **根因**：结果模型只支持标量，检查层按"报告集合"的自然写法传值，中间没有归一。
* **修复**：`_r` 统一走 `_as_dict`；`_as_dict` 新增 `list/tuple → "a, b"` 分支（保留数值/布尔语义，不丢信息）。
* **测试**：`tests/unit/test_verify_checks.py::test_a_list_measurement_still_produces_a_verdict`；`C-3` 的多项测试现在断言 `measurements["missing_formats"] == "step, fcstd"`。

### C-5 "无法证明"这条原因会被别的失败挤掉

* **位置**：`tcad/verify/gate.py:79`
* **问题**：`blocking_failures` 里有失败项时，不再追加 `gate:cannot_attest_*`。于是"没有 digest 测量 + 交付缺失"的构建只报"exportability 失败"，掩盖了更严重的事实：Gate 根本没有证明过任何几何。
* **复现**：本轮改动后 `tests/contract/test_wired_pipeline.py::test_missing_digest_cannot_attest_and_does_not_explode` 立即失败（`['exportability']` 里没有 `cannot_attest`），这就是证据。
* **根因**：把"为什么没通过"当成了单选原因，实际是集合。
* **修复**：`cannot_attest` 成立时**始终**追加该原因；`passed` 判定逻辑一字未动（该标志本身已蕴含失败，只可能让报告更全，不会把失败说成成功）。
* **测试**：`tests/unit/test_verify_gate.py::test_cannot_attest_is_reported_beside_a_failing_check`；原契约测试恢复通过。

### A-1 枚举、两套映射与"真实验证能力"不同源

* **位置**：`tcad/ir/schema.py:161-167`（21 个 op）、`schema.py:137` 与 `worker/compiler.py:40`（两份手写 `FEATURE_TYPE_MAP`）、`tcad/tools/ir_tools.py`（手抄 op 清单的工具描述）
* **问题**：`api_selftest` 报 `feature:<op>` × 21，读起来像"21 个特征都支持"，实际只证明 `addObject()` 返回了对象。任务书 §5-B 明确禁止这种"创建了类型就算支持"。
* **复现**：`api_selftest` 输出（`tools/doctor.py` 现在会额外打印一条范围警告）；以及 `grep -n "_PROFILE_OPS" tcad/worker/compiler.py`（死代码）。
* **根因**：没有"能力 = 有证据"这一层，三处各自抄写。
* **修复**：新增 `tcad/ir/capability.py`（`VERIFIED` / `EXPERIMENTAL`，每项带 `proof` 与 `gap`；`proof` 引用具体契约测试文件名）；`validate_ir` 对实验性 op 逐特征 `warn`；`ir_patch` 的工具描述用 `capability.describe_for_model()` 生成（含" Nothing proves the shape —— 编译后必须 re-measure"）；`tests/unit/test_op_capability.py` 用 AST 读 `worker/compiler.py`（不导入 FreeCAD）比对两套映射与枚举，并要求 VERIFIED 的 `proof` 指向磁盘上真实存在的测试文件。
* **兼容**：没有拒绝任何 op（避免破坏既有 IR），只把"未证明"变成每次校验都看得见的告警。

### B-1 孔的验收证据来自 IR 而不是几何（本目标早前轮次）

* **位置**：`tcad/worker/introspect.py`（凹圆柱面测量）、`tcad/verify/specexpr.py:_hole_measure`、`tcad/verify/checks_spec.py`
* **要点**：孔径/孔位只与 `digest.holes` 比较；草图 attachment offset 不再当作孔位；`op=hole` 不是必要条件（圆草图 Pocket 出的孔同样能测到）；通/盲由面参数四角投影得到的深度判定；确认过的孔要求若无 BRep 证据 → 阻断 ERROR，不是 SKIP。
* **测试**：`tests/contract/test_measured_holes.py`（真内核）、`tests/unit/test_verify_specexpr.py`。

### E-1 诊断脚本自相矛盾

见 §1.1。位置 `tools/doctor.py`（`worker:api_selftest` 段）。根因：`WorkerHandle.request_sync` 返回已剥壳的结果载荷（`tcad/core/worker_client.py:396`；RPC 层 `rpc.py:28` 的 `ping` 恰好自带 `ok` 字段，掩盖了这个误用）。

### C-6 被 Gate 打分的文件，可能不是这次构建写的

* **位置**：`tcad/loop/commit.py`（步骤 2b）、`tcad/core/types.py`（新增 `BuildStamp`，`CheckContext.build_stamp`，`GateReport.attempt_id/ir_sha256`）、`tcad/store/artifacts.py`（`write_build_stamp`/`read_build_stamp`）、`tcad/verify/context.py:_load_stamp`、`tcad/verify/checks_solid.py:ProvenanceCheck`
* **问题**：产物目录按 `data/artifacts/<model_id>/v<N>` 复用，同版本重试写进同一个目录。Gate 此前只判断"文件在不在、是否为空"，于是上一次失败构建留下的 STEP 可以替这一次交差——文件是真的，但它不是本次构建的证据（任务书 §5-C"旧文件不得补足本次失败的导出"、"错误版本 digest"）。
* **复现**：`.venv/bin/python -m pytest tests/contract/test_wired_pipeline.py::test_leftover_export_cannot_satisfy_a_later_grading`；把 STEP 的 mtime 拨回一小时（模拟上一代的遗留件），修复前 Gate 依旧 `passed=True`。同版本重试（`test_retry_of_the_same_version_gets_its_own_attempt`）在修复前两次构建连"是哪一次"都无法区分。
* **根因**：目录名承担了"同一次构建"的语义，而它只能表达"同一个版本"。缺少构建身份，就没有任何一层能区分"重写过的文件"和"没被重写的旧文件"。
* **修复**：`run_commit` 在 pre_commit 之后、调用 worker **之前**写入 `build_stamp.json`（`attempt_id`、`model_id`、`ir_version`、`started_at`、交给编译器的 IR 的 sha256）；`build_check_context` 读回它，stamp 读不懂就抛 `CheckContextError`（宁可报错也不盲判）；阻断级 `provenance` 检查 digest/IR/stamp 三方身份一致，并要求每个导出文件的 mtime 不早于 `started_at`；`GateReport` 携带 `attempt_id`/`ir_sha256`，让"通过"能追溯到它实际测量的那批写入。
* **取舍**：没有 stamp 时判 **FAIL** 而不是 SKIP——按 §5-C，"必须验证却没有证据"不能降级成可忽略的跳过。生产路径每次 commit 都会 stamp，因此这条只会命中手工塞进目录的产物；`tests/contract/test_e2e_pipeline.py` 的 `built` fixture 也照生产顺序先 stamp 再 compile，而不是把检查放宽。
* **测试**：`tests/unit/test_verify_provenance.py`（9 项：clean PASS、digest 串版本、digest 串模型、stamp 冒用别人的 model/version、遗留 STEP、遗留+新文件混合、无 stamp、stamp 损坏）；`tests/contract/test_wired_pipeline.py` 真内核 4 项（活构建、同版本重试、遗留导出、删 stamp）。

### C-7 一次编译把整个进程的事件循环冻住

* **位置**：`tcad/loop/commit.py:_worker_call` 与 `run_commit` 里的 `Gate.evaluate`
* **问题**：`Worker.request` 按协议是同步的（`tcad/tools/base.py`），而 `run_commit` 是被 `await` 的。compile 的超时上限是 120 秒——这段时间 asyncio 循环一步都走不动：其他会话的 SSE 停流、SSE 心跳与健康检查失效、被取消的轮次拿不到取消点。
* **复现**：`.venv/bin/python -m pytest tests/unit/test_commit_event_loop.py`。修复前同一测试测到心跳 **0** 次（脚本级复核：`asyncio` 心跳 + 内联 `time.sleep(0.6)` → 0 ticks）；改走线程后同样时长得到 ~40 次。
* **根因**：同步协议与 async 调用方之间没人负责换线程。`WorkerHandle` 其实另有 async `request`（内部就是 `asyncio.to_thread`，`tcad/core/worker_client.py:476`），但工具层拿到的是 `SyncWorkerClient.request`，于是阻塞实现穿过了整条 async 链。
* **修复**：compile / export / introspect 与 `Gate.evaluate`（round_trip 会再走一次 worker 回读 STEP）统一经 `asyncio.to_thread`。
* **诚实边界**：线程内的调用仍不可中途取消，只能等自身超时（R-8，**该缺口已由 §26 C-26 补上**，此处保留当时的记录）；`to_thread` 用默认线程池（`min(32, cpu+4)`），因此同时进行的 commit 数量隐含该上限——这属于 §5-D 的"生产预算"议题，见 R-5。

## 6. 能力矩阵（§6）

事实源是 `tcad/ir/capability.py`，本节是它的展开视图；新增测试见 `tests/unit/test_op_capability.py`。
"真内核" = `tests/contract/*` 在 `FreeCADCmd 26.3.0/48708` 上跑通并断言了测量值。

| 能力 | 实现 | 单元测试 | 真内核测试 | 已知限制 |
| --- | --- | --- | --- | --- |
| 矩形/圆草图 + 尺寸约束 | ✅ | ✅ `test_ir_validate.py`、`test_geo_tools.py` | ✅ `test_sketch_planes.py`、`test_samples_acceptance.py` | 圆角矩形之类要靠约束组合，模型需显式给全约束 |
| `pad` | ✅ VERIFIED | ✅ | ✅ 样例 A/B/C | 方向遵循 WORLD 契约；非平面附件仍属实验 |
| `pocket` | ✅ VERIFIED | ✅ | ✅ 样例 A 贯穿槽、B 通孔、C 内孔 | 与体不相交会作为构建错误报出（`test_build_failures.py`） |
| 圆孔（通孔 / 盲孔） | ✅（圆草图 + pocket） | ✅ | ✅ `test_measured_holes.py` | `hole` 这个 op 仍标实验：应改用圆草图 + pocket，因为它没有独立测量证据 |
| 单实体参数修改 | ✅ | ✅ `test_loop_engine.py`、`test_ir_patch.py` | ✅ 样例 B 第二轮 Ø6→Ø8、样例 C 高度 40→50（跨 worker 重启） | 只有受影响尺寸应变；测试断言其余尺寸不变 |
| FCStd（可重开、保留 Body/Sketch/Feature 树） | ✅ | — | ✅ `test_samples_acceptance.py` 重开并再测量 | 目前由 `compile_ir` 整文档 `saveAs` 产出，非增量 |
| STEP 交付 + 回读 | ✅ | ✅ `test_verify_gate.py` | ✅ `round_trip`（回读体积相对误差 1e-6） | 回读与编译同进程：数据独立（走磁盘），进程不独立（`checks_solid.py` 顶部已写明） |
| STL（按需） | ✅ | ✅ | ✅ 产物见 `review/acceptance` | — |
| 多视图预览 | ✅ | ✅ `test_render.py` | ✅ `test_wired_pipeline.py` 走真实 worker 网格；`review/acceptance/**/preview_*.png` 36 张（9 个 turn 目录 × 4 视图，磁盘实数） | 交互式 3D 视图属后续增强；图片只做展示，未做模型侧视觉检查（无可用视觉模型，见 §3） |
| `revolution` | ✅ **VERIFIED** | ✅ `test_op_capability.py` | ✅ `test_revolution.py`（阶梯轴解析体积 `2720π`、包围盒、STEP 回读、FCStd 重开后改 `Angle` 体积减半、未知轴名结构化拒绝） | 用 `params.axis` 指定回转轴；轮廓用坐标定型，**不要**在已闭合的链上再加 `DistanceX/Y`（实测会把顶点挪走） |
| `groove` | ✅ **VERIFIED** | ✅ `test_op_capability.py` | ✅ `test_groove.py`（环槽解析体积、180° 恰好切掉一半、切空报错、STEP 回读、FCStd 重开后改 Angle） | 见 §22 |
| `fillet` / `chamfer` | ✅ **VERIFIED** | ✅ `test_op_capability.py` | ✅ `test_fillet_chamfer.py`（四竖边 r=5/3/4.5 与 d=2/3 的解析削减体积 `4(1−π/4)r²t` / `4(d²/2)t` 1e-6、只倒两条边时减半、FCStd 重开后改 `Radius` 5→8 体积按新半径重算、STEP 回读一致、`Edge99` 与 r=40 结构化拒绝） | 选边要先用 `ir_digest` 的 `kind`/`length`/`mid`/`direction` 认边，再写 `base_feature` + `sub_elements`；见 §24 |
| `mirrored` / `linear_pattern` / `polar_pattern` | ✅ 已实现 | ✅ 白名单与规则 5c/5d | ✅ `test_patterns_mirror.py` 16 项真内核（体积/孔位/重开改参/拒绝） | 只支持机体三轴与原点基准面/零件自身的平面；倾斜自定义轴、非平面镜像无字段 |
| `draft` | ✅ **VERIFIED** | ✅ `test_op_capability.py` | ✅ `test_draft_thickness.py`（5° 四侧面绕 XY 恰好落在解析棱台 `29282.0083`（`h/6·(A0+4Am+A1)`，1e-6）、`reversed` 外扩到 `34881.2827` 且包围盒 43.5、中性面用**底面 Face6** 得同一体积、FCStd 重开后 `Angle` 5→10 按新角度重算、`Face99` 与"无 plane"按名拒绝、与中性面**平行**的面按名拒绝） | 面表走 `base_feature` + `sub_elements`（面名来自 `ir_digest`），中性面走类型化 `plane` → `NeutralPlane`；见 §33 |
| `thickness` | ✅ **VERIFIED** | ✅ `test_op_capability.py` | ✅ `test_draft_thickness.py`（顶面 `value=2` 开壳得解析壁体积 `8672`、`value=4` 得 `15616`、开侧面得单侧开口的解析体积、FCStd 重开后 `Value` 2→4 重算、缺面表按名拒绝） | 被点名的面**保持原位**，其余面内缩 `value`；不读 plane（给了会被 `plane_unused` 拒绝）；见 §33 |
| `circular_pattern` | ✅ 代码在 | ⚠️ | ❌ | 本机是间距 API（`NumberCircles`/`RadialDistance`），表达不了"N 份均布在一个角度内"；要螺栓圆请用 `polar_pattern` → 提交会收到 `op_experimental` 告警 |
| `additive_*` / `subtractive_*` 六个原语 | ✅ 全部 **VERIFIED** | ✅ `test_ir_validate.py`（placement 的接受/拒绝）、`test_patch_type_safety.py`（payload 里的 placement 必须落地为类型化模型） | ✅ `test_primitive_placement.py` 9 项（销钉并成 1 实体 `32000+432π`、球半埋 `32000+144π`、绕自身位置转 90°、绕 Y 转 90° 的圆柱落在 X 向、通孔 `32000−200π`、盲槽 `−400`、埋球 `32000−36π`、无 placement 仍在原点、落空按名拒绝） | 放置载体是 `Placement`（世界坐标 mm，`axis`+`angle` 逆时针）；**不是** `AttachmentOffset`——实测它在 `MapMode=Deactivated` 下完全不生效（§31）。只有这六个 op 接受 placement，其他 op 给了会被 `placement_unused` 拒绝 |

## 7. 三层测试与运行命令

```bash
# 层一：确定性单元（不需要 FreeCAD）
.venv/bin/python -m pytest tests/unit -q                       # 758 passed
#   关键入口：test_verify_provenance.py（产物↔构建尝试绑定）、test_commit_event_loop.py（提交不冻住事件循环 + 取消会中止构建）、
#             test_worker_abort.py（进行中调用的中止/惰性重启/崩溃与中止不互相冒充）、
#             test_ir_validate.py（补丁/面引用/placement 校验）、test_patch_type_safety.py（payload 不得把未校验 dict 塞进类型化字段）、
#             test_op_capability.py（枚举↔编译器↔能力表漂移守卫）、test_acceptance_bundle_provenance.py（验收包的源码树摘要与 --check 陈旧判定）、
#             test_system_prompt_truth.py（运行时 system prompt 里点名的工具/只读声明/passed 字段与真实注册表对得上）、
#             test_context_wiring.py（历史/需求合同进 prompt）、test_http_*.py（并发写入、审批绑定、路径边界）

# 层二：真实 FreeCAD 内核（FCStd 重开 / STEP 回读 / 参数改后重算）
.venv/bin/python -m pytest tests/contract -q -rs               # 167 passed
#   关键入口：test_samples_acceptance.py（样例 A–E + 变体 + 中英表述）、
#             test_measured_holes.py（BRep 孔证据）、test_build_failures.py（失败可定位；
#             草图级与 pad 参数级的换向、居中、拼错参数被拒都按实测几何断言，
#             并把"坐标与原点绑定矛盾时求解器挪动轮廓而不是报错"钉成行为特征）、
#             test_sketch_planes.py（XY/XZ/YZ 世界坐标契约；挤出方向按实测与工具描述逐条比对）、test_wired_pipeline.py（整链 + 活构建产物溯源）、
#             test_revolution.py / test_groove.py（回转体与环槽的解析体积；
#             两个 op 的 reversed/midplane 扫掠方向按实测几何断言）、
#             test_fillet_chamfer.py（边选择的圆角/倒角解析削减体积）、
#             test_draft_thickness.py（draft 绕中性面的解析棱台体积与平行面按名拒绝、thickness 单侧开口的解析壁体积、
#             两者 FCStd 重开后改 Angle/Value 重算）、
#             test_patterns_mirror.py（镜像/阵列从 BRep 实测位置）、
#             test_primitive_placement.py（六个原语的放置/旋转，解析体积与包围盒）、
#             test_worker_interrupt.py（中止真的杀掉真进程，下一次构建仍能量）、
#             test_face_attachment.py（贴面草图的真实子元素与法向语义）、
#             test_e2e_pipeline.py（脚本化 LLM 编排，属合同层不是第 3 层）

# 层三：真实 LLM 端到端（未配置时明确跳过，不伪装）
TCAD_E2E_BASE_URL=... TCAD_E2E_MODEL=... .venv/bin/python -m pytest tests/e2e -q -rs

# 环境诊断 / 验收产物
.venv/bin/python tools/doctor.py
.venv/bin/python tools/build_acceptance_artifacts.py
```

脚本式 LLM 回复的用例（`tests/contract/test_chat_end_to_end.py`、`test_e2e_pipeline.py`）**属于合同/编排层**，不能替代第 3 层——它们的断言只覆盖管线行为，不覆盖模型理解能力。

尺寸公差写在测试里并注明理由：体积相对误差 `1e-6`、包围盒绝对误差 `1e-6`、STEP 回读相对体积误差 `1e-6`（见 `tests/contract/test_samples_acceptance.py` 模块文档与 `review/acceptance/manifest.json` 的 `tolerances` 段）。理由：这些样例是解析可算的精确形状，OCC 的偏差来自浮点累积而非建模近似，因此取机器精度量级而不是"工程公差"；一旦放松，样例 B 的 `32000−288π → 32000−512π` 这类"只允许孔变"的断言就失去分辨力。

## 8. 跨平台运行（不依赖任何人的私人目录）

FreeCAD 路径统一由 `TCAD_FREECAD_CMD` 决定，配置里是 `"${TCAD_FREECAD_CMD:-<repo>/free-cad/FreeCAD/build/debug/bin/FreeCADCmd}"`；`tools/doctor.py` 与 `tools/build_acceptance_artifacts.py` 读同一个变量。

**Linux / WSL**

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # 或 pip install -r requirements.txt
export TCAD_FREECAD_CMD=/usr/bin/freecadcmd          # apt 装法
# 或 conda: export TCAD_FREECAD_CMD=$CONDA_PREFIX/bin/freecadcmd
.venv/bin/python tools/doctor.py
.venv/bin/python -m pytest tests/contract -q
```

Linux 上会暴露 macOS 掩盖过的两类问题——路径大小写与 `FCBak` 残留；本轮的 `.FCStd` 修复正是为它准备的（第 5 节 C-2）。

**Windows（PowerShell）**

```powershell
py -3.11 -m venv .venv; .\.venv\Scripts\pip install -e ".[dev]"
$env:TCAD_FREECAD_CMD = "C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe"
.\.venv\Scripts\python.exe tools\doctor.py
.\.venv\Scripts\python.exe -m pytest tests\contract -q
```

服务默认只绑 `127.0.0.1`；未经身份验证与资源隔离不要扩到公网（README 与 `configs/default.yaml` 同此约束）。

## 9. 验收产物（§8 要求可下载、可重开）

`review/acceptance/`，由当前代码重建（当前 `attempt_id = acc-20260920T132149Z-8238f16a`，磁盘上 109 个文件 = 108 个产物 + `manifest.json`，36 张预览；历次 attempt 见 §11、§22、§23、§32）：

* **同源证明**：manifest 的 `source_tree` 段记下构建时的源码树摘要（`tcad/` + `tools/` + `tests/` 下 153 个 `.py`，按 `相对路径\0文件 sha256\n` 排序拼接后整体 sha256 = `df1be3a49d2e5402…`）。`.venv/bin/python tools/build_acceptance_artifacts.py --check` 重算当前树并比对，`OK:   bundle matches the working tree`（退出码 0）/ `FAIL: STALE: … rebuild with …`（退出码 1）。加这一层的原因：本机 git 不可用（Xcode 许可未接受），产物包没法靠 commit 号定位版本，而 §5-C 要求"必须交付的文件真实存在且属于同一次构建"——旧的 `acc-20260919…` 包就是在放置/校验/编译器六个轮次**之前**构建的，数字仍然对得上，但已经不是当前代码的产物。回归：`tests/unit/test_acceptance_bundle_provenance.py`（8 项，含"改一行即变摘要/陈旧包必须被 `STALE` 指名/缺字段与旧格式清单必须拒绝"）。
* `manifest.json` — `attempt_id`、`built_at_utc`、平台、`freecad_cmd` 与版本、`source_tree`、公差与理由、每个样例每轮的期望/实测体积、预览清单、**逐文件 sha256 + 字节数**。
* `sample_a/turn1/`、`sample_b/turn1|turn2/`、`sample_c/turn1|turn2/`、`sample_d/turn1|turn2/`（阶梯轴 + revolution）、`sample_e/turn1|turn2/`（圆柱环槽 + groove）— 每轮 12 个文件：`<model>.FCStd`、`<model>.step`、`<model>.stl`、`ir.json`（IR 快照）、`requirements.json`（需求合同）、`digest.json`（内核实测，含面清单）、`build_report.json`（验证记录，含本轮预览文件名）、`preview_{iso,front,top,right}.png`（多视图），turn 1 另有 `roundtrip.step`（编译诊断，**不是**交付物），turn 2 另有 `edit_report.json`（重开+改参的逐特征状态）。
* 预览的三角形来自 worker 对**同一份 IR** 的 `tessellate`，不是另一次构建；每张图写入后还会检查尺寸与非空白像素，全白即 `AcceptanceError` 中止——一张什么都不画的 PNG 不算交付物。人工抽查已确认：`sample_b/turn2/preview_iso.png` 是四孔板、`sample_a/turn1/preview_top.png` 的方孔透出背景（贯穿，不是盲槽）。
* 数值（内核实测，取 `manifest.json` 的 `samples` 段）：A `V=25600`、包围盒 `80×50×8`；B `32000−288π=31095.221…` → 改 Ø8 后 `32000−512π=30391.505…`，孔心与板尺寸不变、实体数仍为 1、跨 worker 重启；C `8000π=25132.741…` → 高 50 后 `10000π=31415.927…`，内孔保留；D `2720π=8545.132…` → `Revolution.Angle` 360°→180° 后 `1360π=4272.566…`；E `8500π=26703.538…` → `Groove.Angle` 360°→180° 后 `8750π=27488.936…`。STEP 回读体积相对误差 1e-6 内一致。
* 生成器在写 manifest 前逐项核对，任一不符就中止——所以磁盘上有 manifest 就等于这批数字全部来自同一次真实构建。

## 10. 变更文件清单（本轮）

| 文件 | 变更 |
| --- | --- |
| `tcad/verify/context.py` | 产物识别重写（诊断件排除、大小写无关、按 model_id 择一）；去掉已无用的 `import glob` |
| `tcad/verify/checks_solid.py` | `VerifyConfig.required_exports`；`ExportabilityCheck` 按合同判定；`_r`/`_as_dict` 归一列表测量值 |
| `tcad/verify/gate.py` | `cannot_attest` 原因与失败项并存 |
| `tcad/core/wiring.py` | 新增 `required_export_formats()` 并接入 Gate |
| `tests/unit/test_export_discovery.py` | 新增 17 项 |
| `tests/unit/test_verify_gate.py`、`tests/unit/test_verify_checks.py` | 各新增 1 项 |
| `tools/build_acceptance_artifacts.py` | 新增 `render_previews()`：产物包附四视图 PNG，空白图即中止；manifest 记录视图/尺寸/来源 |
| `review/acceptance/` | 产物重建（attempt `acc-20260919T105805Z-4487725c`，60 文件 + 清单；后续轮次重建为 `acc-20260919T114550Z-48950611`） |
| `README.md` | 测试计数（516 单测 / 72 契约 / 4 项按开关跳过）与目录树订正：补 `tcad/ir/capability.py`、`tcad/worker/reopen.py`、`tools/doctor.py`、`tools/build_acceptance_artifacts.py`、`review/` |

验证记录（**第一轮**末尾重跑，非引用旧数）：`pytest tests -q` → **604 passed, 4 skipped in 31.89s**；`pytest tests/unit -q --collect-only` → 528；`pytest tests/contract -q --collect-only` → 76。报告末尾的**当前**全仓数字见 §1.2（该节随轮次更新；当前 **925 passed, 4 skipped**）。

## 11. 后续轮次追加的修复

### C-8 会话历史存进了 SQLite，但没有传给模型（任务书 §5-D / R-4）

* **位置**：`tcad/loop/engine.py:_init_messages`（旧实现）、`tcad/context/assembler.py`（无人调用）、`tcad/server/app.py:run_turn_request`。
* **问题**：`/chat` 把用户消息与助手文本写进 `session_db`，但每个 Turn 只把 `[system, user]` 发给模型。于是"把刚才四个孔改成直径 8，其余不变"里的"刚才四个孔"没有任何可指代的东西；多轮修改只能靠 `ir_digest`/`ir_load` 重新拉状态。`tcad/context/assembler.py` 的预算装配与三档降级**从未被生产路径调用**。
* **复现**：`tests/unit/test_context_wiring.py::test_the_http_chat_endpoint_replays_the_previous_turn` —— 连发两次 `/chat`，修复前第二次请求里既没有第一次的文本、也没有需求合同。
* **根因**：历史只在写入侧接好了，读取侧（谁把它放进 prompt）根本没有实现；引擎也没有"当前 IR 摘要 / 需求合同 / 上一轮结论"这三块。
* **修复**：
  1. `tcad/context/requirements.py`：把 `ir.requirements` 投影成需求合同文本，逐条标出 `confirmed` 与逐字 `source_text`，并区分"用户明确给出"与"系统推断"；空合同时明说"Gate 无可判定项"，而不是静默。
  2. `tcad/context/verdict.py`：把上一轮 `GateReport`（活的或落盘后的 dict 形态）渲染成含 measured/expected 与 `feature_id` 的修复指引。
  3. `tcad/context/history.py`：从 `session_db` 读回历史并投影成可回放消息；排除本轮刚写入的那条用户消息（否则同一句话会出现两次），跳过未知角色与空内容，任何异常都降级为"没有历史"而不是让 Turn 失败。
  4. `tcad/store/artifacts.py` + `wiring.StoreAdapter` + `loop/commit.py`：Gate 结论落盘到 `<data_dir>/gate_reports/<model>/v<N>.json`（**故意不放在 artifact 目录**——那里的文件集本身是被 Gate 审计的交付面）。服务端每个请求新建引擎，只有落盘才能让"上一轮错误"跨进程可见。
  5. `tcad/loop/engine.py`：新增可选 `context_assembler` 与 `history_provider`；`_build_messages()` 组装 system / 需求合同 / IR 摘要 / 上一轮结论 / 历史，并把当前用户消息原样放在最后、只放一次。无装配器或装配抛异常时**整体回退**到原来的 `[system, user]`。
  6. `tcad/context/assembler.py`：新增 `requirements_text` 块，在 FULL/SUMMARIZED/MINIMAL 三档下**都不丢弃**（窗口最紧时最不能丢的正是"在按什么验收"）；新增 `to_openai_messages()`，空白系统块不上线，但需求合同永不为空。
  7. `tcad/context/digest.py`：特征链现在打印稳定 `id`（`name [id] (op)`），否则跨轮引用无从谈起。
  8. `wiring.build_context_assembler()` 从 `context.*` 配置构造装配器并挂到 `services.context_assembler`；`app.create_app` 把 `session_db` 挂到 services，`run_turn_request` 默认用它做历史来源；CLI REPL 同样落库并回灌。
* **诚实边界**：历史里仍没有工具调用与结果（这是有意的：只回放旁白比没有历史更误导；几何事实改由摘要块承担）；被压缩的旧历史用一条显式占位消息代替，**没有注入 LLM 摘要器**（那会在上下文构建里花掉一次模型调用，并给 Turn 增加一条失败路径）。两条都写进了 README 限制 #1。
* **测试**：`tests/unit/test_context_wiring.py`（21 项：需求合同、稳定 id、历史排除当前消息、dict/活体 verdict、引擎装配、无装配器回退、装配器/历史源抛异常的降级、结论落盘往返、以及**两次真实 HTTP `/chat`** 的端到端回灌）；`tests/unit/test_context_assembler.py` 新增 2 项（需求块在三档降级下存活、空白块不上线）并更新 3 项块计数。
* **验证**：`pytest tests -q` → **627 passed, 4 skipped**（551 单测 + 76 契约）；`pytest tests/contract -q` 真内核 76 项全绿，未因上下文接线回归。

### 验收产物重建（同轮次）

上下文接线之后重新跑了真实内核验收包，确认改动没有触碰几何链路：

```
.venv/bin/python tools/build_acceptance_artifacts.py
→ FreeCAD 26.3.0 / 48708
→ sample A turn1: OK (V=25600, bbox 80×50×8, STEP readback exact)
→ sample B turn1: OK (V=32000−288π, 4 holes r=3)
→ sample B turn2: OK (restart + Ø6→Ø8, V=32000−512π)
→ sample C turn1+turn2: OK (8000π → h=50 → 10000π)
→ wrote 61 files under review/acceptance
```

新 `attempt_id = acc-20260919T114550Z-48950611`，`manifest.json` 记录 60 个产物文件，逐文件 sha256/字节数**已在磁盘上重新核对通过**（缺失 0，哈希不符 0）；20 张 `preview_*.png` 全部非空白（程序化统计：暗像素 > 0 且灰阶种类 > 1，最少 3 级）。

### C-9 构建直接写版本目录，没有隔离暂存与"通过才发布"（任务书 §5-C / R-2）

* **位置**：`tcad/loop/commit.py:run_commit`、`tcad/store/artifacts.py`、`tcad/core/wiring.py:build_context_loader`、`tcad/verify/gate.py:Gate.evaluate`。
* **问题**：每次构建直接写 `data/artifacts/<model_id>/v<N>`。同一个版本重试时复用同一目录，上一次失败留下的 STEP 就躺在那里。C-6 的 `provenance` 用 mtime 把它挡成了 FAIL，但这是**启发式**：把遗留件的 mtime 拨到本次 stamp 之后就能骗过它（`tests/contract/test_wired_pipeline.py` 里那条测试正是这么构造的）。任务书要求的是隔离暂存目录 + 通过后发布。
* **复现**：`.venv/bin/python -m pytest tests/contract/test_wired_pipeline.py -k "staging or manifest or failed_build or failed_retry"`。把 `run_commit` 改回"直接评版本目录"，4 项全红。
* **根因**：目录名只能表达"哪个版本"，不能表达"哪一次尝试"；把构建产物和已发布产物放进同一个目录，就把"是否可信"变成了对文件时间戳的推断。
* **修复**：
  1. `ArtifactStore` 新增 `staging_dir()`（`v<N>.staging-<attempt>`，是版本目录的**兄弟**而不是子目录——任何对版本目录的 glob/rglob 都看不到半成品）、`discard_staging()`、`write_manifest()`（逐文件 sha256 + 字节数）、`publish()`、`recover_publish()`。
  2. `publish()` 用两次 `os.replace`：旧树先改名到 `.v<N>.previous`，新树再改名为 `v<N>`，最后删旧树。读者永远看不到新旧混合；崩在两次 rename 之间时 `recover_publish()` 会把旧树换回来。空暂存目录**拒绝发布**（"什么都没建"不能读成"已发布"）。
  3. `Gate.evaluate(model_id, ir_version, *, artifact_dir=None)` 支持指定评分目录；`build_context_loader` 的 `_load` 增加同名可选参数。对只接受两个参数的旧 loader 用 `inspect.signature` 判定后**不传**该参数，第三方/嵌入方的 loader 不受影响。
  4. `run_commit`：stamp、compile、export、introspect、digest **全部写进暂存目录**；Gate 对暂存目录评分；`report.passed` 为真才 `publish`，否则 `discard_staging`。发布失败会在返回文本里追加一条 upstream note——不能让绿色 Gate 暗示一个并不存在的交付。
  5. 修掉一个**只有在暂存路径下才会暴露的旧缺陷**：`ContextServiceAdapter` 的"测不到 digest"回退会去读**版本目录**里的 digest。若该版本此前成功发布过，重试失败时 Gate 会拿到**上一次的测量值**去评一个刚被重写的 IR——旧结论给新构建背书。现在回退只读**被评分的那个目录**，读不到就是 `measurements_available=False`（"无法验证"）。
* **诚实边界**：`render_view` 的 PNG 缓存仍写在版本目录里；发布时会随整目录替换而被丢弃（下次自动重渲染）。发布是两次 rename，不是单次原子操作，中间窗口由 `recover_publish()` 兜底。
* **测试**：`tests/unit/test_artifact_publish.py`（11 项：发布/替换/拒绝空目录/中断恢复/丢弃/manifest 逐文件哈希/Gate 目录覆写/两参数 loader 兼容/**不借用已发布 digest**）；`tests/contract/test_wired_pipeline.py` 第 6 节 4 项真内核（Gate 收到 `.staging-` 目录且随后发布、失败构建不发布且不留暂存、已发布带 manifest 且哈希自洽、失败重试不破坏上一次已验证构建）。全部做过**变异验证**：把 `artifact_dir` 改回版本目录后 4 项契约测试全红。
* **验证**：`pytest tests -q` → **642 passed, 4 skipped**（562 单测 + 80 契约）；`pytest tests/contract -q` 真内核 80 项全绿。

## 13. 第三轮：把 `revolution` 从"代码在"变成"量过"

### C-10 `revolution` 只证明能 `addObject`，没有任何几何证据（任务书 §6 / R-6）

* **位置**：`tcad/worker/compiler.py:_apply_feature`、`tcad/ir/capability.py`、`tcad/tools/ir_tools.py`。
* **问题**：`PartDesign::Revolution` 的轴是 `ReferenceAxis`，类型是 `App::PropertyLinkSub` —— 一个 JSON 标量无法表达的引用。编译器原来只 `addObject` 加标量 `setattr`，**从不设轴**。实测（把轴翻译删掉的变异）：`ft_shaft(Revolution) state=['Touched','Invalid']`，整条 body 没有任何实体。这个 op 因此既不能算支持、也不能算拒绝，只是"看起来在"。
* **复现**：`pytest tests/contract/test_revolution.py -q`；把 `_set_axis_reference` 的调用删掉 → **7 项全红**（已做变异验证）。
* **根因**：把"IR 无法携带 LinkSub"当成了"这个特征不可能支持"。轴其实可以用一个**标量枚举**表达，再由编译器翻译成 LinkSub —— 缺的是那一层翻译。
* **修复**：
  1. `params.axis` 新增（IR 校验的 revolution/groove 允许键里本来就有 `axis`，无需改 schema）：`V_Axis`/`H_Axis`/`N_Axis` = 轮廓草图自身的轴（默认 `V_Axis`，**跟随草图的附件与 offset**），`X`/`Y`/`Z` = body 原点轴。`_set_axis_reference()` 负责翻译，并把它从 `_assign_props` 的结构化键里排除。
  2. 轴名无法识别 → **结构化 `semantic` 错误**，点名 feature 和可用取值。理由写进了代码注释：绕错轴的旋转体是一个**尺寸不对但看起来合理**的实体，比一次拒绝危险得多。
  3. `capability.py`：`revolution` 从 EXPERIMENTAL 升为 VERIFIED，`proof` 指向新测试文件；`groove` 保留 EXPERIMENTAL，但把 gap 改成诚实版本（"轴能设了，只是没人量过旋转切除"）。
  4. 工具描述新增 `revolution params` 段：轴语义 + **实测配方**（轮廓用真实坐标画、一条边落在轴上、用 Coincident 闭链；**不要**在已闭合的链上再叠 `DistanceX/DistanceY` —— 实测会把顶点静默挪走，2720π 的轴变成 38453 mm³ 且无任何报错）。
* **诚实边界**：`groove`、`fillet`、`chamfer`、镜像、阵列仍是 EXPERIMENTAL。轴类引用解决了，但边/面引用（`Base`/`MirrorPlane` 等 LinkSub）IR 仍没有字段可携带。旋转切除一次都没量过。
* **测试**：
  * `tests/contract/test_revolution.py`（7 项真内核）：三个数值不同的阶梯轴，体积 = `π(r₁²h₁ + r₂²h₂)`（相对误差 1e-6）；`axis="Z"` 与 `axis="V_Axis"` 量到同一个实体（两条不同的 LinkSub 路径，结果一致才算证据）；未知轴名 → `semantic` 错误且 `feature_id` 正确；交付的 STEP 回读体积一致；交付的 FCStd 重开后 `ft_shaft` 仍是 `PartDesign::Revolution` 且 `Angle` 改 180° 后体积**恰好减半**。
  * `tests/unit/test_op_capability.py` 更新为"四个 op 被声明为 VERIFIED"，并被 `test_verified_claims_cite_real_test_files` 强制要求所引用的文件真实存在。
  * 验收包新增**样例 D**（阶梯轴旋转成型 + Angle 参数化修改），见下。
* **验证**：`pytest tests -q` → **649 passed, 4 skipped**（562 单测 + 87 契约）。

### 验收产物新增样例 D（`review/acceptance/`）

```
.venv/bin/python tools/build_acceptance_artifacts.py
→ sample A turn1: OK (V=25600, bbox 80×50×8)
→ sample B turn1/turn2: OK (32000−288π → 32000−512π, 跨 worker 重启)
→ sample C turn1/turn2: OK (8000π → h=50 → 10000π)
→ sample D turn1/turn2: OK (revolution 2720π → Angle 180° → 1360π, feature tree kept)
→ wrote 85 files under review/acceptance
```

新 `attempt_id = acc-20260919T115832Z-454c1c7a`（第六轮再次重建为 `acc-20260919T121751Z-f2b0e090`），`manifest.json` 84 个产物文件，逐文件 sha256/字节数**在磁盘上重新核对通过**（缺失 0、不符 0）；28 张 `preview_*.png` 全部非空白。样例 D turn2 的 `edit_report.json` 记录 `ft_shaft` 为 `PartDesign::Revolution` / `Up-to-date`，实测体积 `4272.566` = 1360π。

## 14. 第四轮：跨请求共享可变状态与并发写入

任务书 §5-D/§5-E 的两条要求，审阅报告的风险表里没有单列，但都是**真实存在且可复现**的缺陷。

### C-11 `/chat` 按请求替换 `services.hooks`，`geo_view` 检查点写在共享 bundle 上（任务书 §5-E）

* **位置**：`tcad/server/app.py`（`s.hooks = tap` / `s.hooks = original_hooks`）、`tcad/loop/engine.py`（`services_any._visual_ok = ...`）、`tcad/tools/geo_tools.py`、`tcad/tools/privileged.py`、`tcad/loop/commit.py`。
* **问题**：`services` 是进程内所有回合共用的一份 bundle，却被塞进了两种**每回合**状态：
  1. 前端为了观察生命周期，把 `services.hooks` 临时换成自己的 tap，`finally` 再换回来。两个并发 `/chat` 各自覆盖对方的 tap；**先结束的那个会把原始 dispatcher 换回去**，此时另一个回合还在跑——它此后丢失全部 hook 事件，更糟的是丢失 hook 的**决定**（`PRE_TOOL_USE` 的 DENY/ASK）。
  2. 引擎在每次工具调用前把 `services._visual_ok` 设成"本轮是否允许看图"，于是**一个会话提交通过，会给另一个会话的步骤打开 `geo_view` 检查点**。
* **复现**：`pytest tests/unit/test_request_isolation.py -q`。把 `LoopEngine._dispatch` 改回无条件读 `services.hooks` → `test_each_turn_dispatches_through_its_own_tap` 立刻红（已做变异验证）；把 `geo_view` 改回只读 bundle → 同一文件里的 `test_geo_view_reads_the_turn_context_not_the_shared_bundle` 第 2 个场景红。
* **根因**：把"每回合的状态"挂在了"每进程的对象"上。引擎虽然能拿到 tap，但它在**调用时**才知道 dispatcher，而前端只有通过改写共享对象才能把 tap 塞进去。
* **修复**：
  1. `ToolContext` 新增 `hooks` 与 `visual_ok` 两个字段（前者任意类型，后者 bool）——每回合一份，天然隔离。
  2. `LoopEngine` 新增可选的 `hooks=` 参数与 `_dispatch()`：所有 `PRE_TURN/PRE_STEP/PRE_TOOL_USE/POST_TOOL_USE/POST_TURN` 都走它；`_make_tool_context()` 把同一个 dispatcher 与 `visual_ok` 放进 `ToolContext`。
  3. `_step` 不再写 `services._visual_ok`，改为写**本次调用的** `ctx.visual_ok`。
  4. `geo_view` 先看 `ctx.visual_ok`，再看共享 bundle（保留旧拼写给直接驱动工具的嵌入方与 `tools/agent_driver.py`，后者也改成写 `ctx.visual_ok`）。
  5. `run_commit(..., hooks=)`：`PRE_COMMIT` / `ON_GATE_RESULT` 走调用方那一轮的 dispatcher，`ir_commit_handler` 从 `ctx.hooks` 传下去。
  6. `raw_python` 的三重门也从 `ctx.hooks` 取 dispatcher——**安全决定必须走发起这一轮的那个 dispatcher**，否则被污染的 dispatcher 可能放行。
  7. `app.py`/`cli.py` 把 tap **作为参数**传给 `run_turn_request(..., hooks=tap)`，删掉两处赋值与 `finally` 里的还原。
* **诚实边界**：同一会话并发两个 `/chat` 仍不受支持（没有 per-thread 回合锁），这一条留在 README 限制 #6 并写明了边界。
* **测试**：`tests/unit/test_request_isolation.py`（6 项）——两个回合各用各的 tap 且互不可见、共享 bundle 未被改写、`visual_ok` 只存在于 `ToolContext`、`geo_view` 的三种组合（都不允许 → 拒绝；**仅本步允许 → 放行**；仅 bundle 允许 → 放行）；最后一项是**源码结构守卫**（`app.py` 里不得再出现 `s.hooks = tap` / `s.hooks = original_hooks`），因为并发症状在单请求测试里根本不会出现。

### C-12 乐观并发的检查与写入不是同一个临界区（任务书 §5-D）

* **位置**：`tcad/store/ir_store.py:apply_patch`。
* **问题**：`apply_patch` 的顺序是"load 当前 IR → 校验 `base_version` → 校验语义 → 写事件 → 写快照"。`EventLog` 自己有一把 append 锁，但那**只保证写入有序**。两个并发 `apply_patch` 可以各自读到 v0、各自通过 `base_version == 0` 的比较（比的是各自读到的那份文档），然后都写 `v1.json`——一个补丁被静默覆盖，而两个调用方都收到"成功"。任务书明确写着"仅对事件日志 append 加锁不够"。
* **复现**：`pytest tests/unit/test_store_concurrency.py -q`。把 `apply_patch` 的临界区去掉后，两个并发写者都成功且只有一个 `patch_applied` 事件（已做变异验证，连跑 3 次都红）。
* **根因**：把乐观并发当成了"比较一下就够"，而比较和写入之间有一个窗口。
* **修复**：`IrStore` 增加**按模型**的 `threading.RLock`（`_lock_for()`，惰性创建 + 一把 guard 锁），`apply_patch` 与 `create` 的整个"读—改—写"序列都在锁内。锁**不**跨进程——这一点写进了代码注释与 README：跨进程写同一个 `data_dir` 需要文件锁，属于另一个议题。
* **测试**：`tests/unit/test_store_concurrency.py`（3 项）。用 `_OverlapSpy` 给 `IrStore.load` 加 50 ms 窗口并统计**同时在临界区内的写者数**：并发两个 `base_version=0` 的补丁，恰好一个成功、另一个收到 `stale base_version`，`v1.json` 只被写一次、`events.jsonl` 里恰好一条 `apply` 事件；`max_inside == 1` 是串行化本身的证据（与谁赢无关）；另有一项确认串行路径未被破坏。
* **验证**：`pytest tests -q` → **658 passed, 4 skipped**（571 单测 + 87 契约）。

## 15. 第五轮：审批不能只按工具名复用（任务书 §5-E）

### C-13 生产装配把 `args_hash` 丢了：一张 `raw_python` 审批 = 该工具的长期通行证

* **位置**：`tcad/core/wiring.py:build_hooks`、`tcad/hooks/policy.py:PrivilegedTripleGate`、`tcad/loop/engine.py:_request_approval`、`tcad/hooks/approval.py`。
* **问题**：`JsonFileApprovalStore.lookup_valid` 一直支持 `args_hash`，但**生产装配从来没用它**：

  ```python
  approval_lookup=lambda tool_name: approvals.lookup_valid(tool_name)
  ```

  而 `PrivilegedTripleGate` 也只把 `tool_name` 传下去（它的 docstring 还写着"调用方应当自己按 args_hash 绑定"——把安全属性交给了调用方，而调用方正是漏掉它的地方）。后果：用户为**一段自己看过的代码**批准一次 `raw_python`，在 TTL 内就等价于批准**任意代码**。任务书 §5-E 明确写着"不能只按工具名复用授权"。
* **复现**：`pytest tests/unit/test_approval_binding.py -q`。把装配改回 `lambda tool_name: ...` 并把 gate 的 args 计算删掉 → 3 项红（含生产装配那条与 engine↔gate 往返那条，已做变异验证）。
* **根因**：把"精确绑定"实现成了一个**可选参数**，并且把计算指纹的责任放在调用方。
* **修复**：
  1. `tcad/hooks/approval.py` 新增**唯一的**指纹函数 `args_fingerprint()`（`sha256:` 前缀 + 16 位十六进制，`sort_keys` + `default=str` 保证全序）。engine 建请求、gate 校验请求都调用它——两处各写一遍哈希正是"用户刚批准却被拒"的经典来源。
  2. `PrivilegedTripleGate` **自己**从 payload 计算指纹（不再信任调用方传参），并把它交给 lookup；同时传 `thread_id`。用 `inspect.signature` 判定 lookup 接受哪些参数，ALLOW 的 reason 会明说绑定了什么（`tool+args+session` / `tool only`），把弱化变成可见事实而不是隐含行为。
  3. 装配改为 `lambda tool_name, args_hash=None, thread_id=None: approvals.lookup_valid(...)`。
  4. `ApprovalRecord` 新增 `thread_id` / `turn_id` / `args_summary`：审批**绑定会话与回合**，并把**人能读的 payload**存下来。只给一个工具名和一个不透明哈希让人点"批准"，那不是审批，是在空白页上签名。未限定会话的旧记录仍然匹配（向后兼容），这一点写进了 docstring。
  5. `engine._request_approval(tool_name, args, thread_id=, turn_id=)` 写入上述字段，摘要上限 4000 字符（指纹仍覆盖**完整** payload，不是截断后的文本）。
  6. `tcad.server.cli approvals` 打印会话/回合与 payload 摘要；`GET /approvals` 本来就 `model_dump` 整条记录，新字段自动可见。
* **诚实边界**：审批仍是**多次可用**的（在 TTL 内同一 payload 可重复通过）——任务书要求的是"绑定"，没有要求一次性；改成一次性会让"批准后重新发起该回合"这个既有工作流失效，属于另一个决定。`raw_python` 依旧默认不注册、`allow_privileged` 默认 false、沙箱探针默认 `null`（fail-closed）。
* **测试**：`tests/unit/test_approval_binding.py`（11 项）：指纹的序无关/值敏感/非 JSON 值安全；gate 对**不同 payload** 拒绝、对同 payload 放行、且指纹由 gate 自己算；单参数 lookup 的向后兼容并把弱化写进 reason；**生产装配**下已批准 payload 放行、另一 payload 拒绝、未批准的拒绝；engine 写的请求与 gate 算的指纹一致（真 store 往返）；会话绑定（同会话放行、异会话拒绝）；旧的无会话记录仍匹配；`args_summary` 含代码且 `thread_id`/`turn_id` 落盘；摘要截断有界而指纹仍覆盖全量。
* **验证**：`pytest tests -q` → **669 passed, 4 skipped**（582 单测 + 87 契约）。

## 16. 第六轮：标识符与路径边界（任务书 §5-E）

### C-14 `model_id` / 导出名 / 导入路径未经校验就变成文件路径

* **位置**：`tcad/store/ir_store.py:_model_dir`、`tcad/store/artifacts.py:dir_for` / `gate_report_path`、`tcad/server/app.py`（`CreateModelRequest` / `CreateSessionRequest` / `ChatRequest`）、`tcad/tools/geo_tools.py`（`asset_export` / `asset_import`）、`tcad/worker/exporters.py`。
* **问题**：三处用户/模型可控的字符串被直接拼进路径，都没有校验：
  1. `model_id` 来自 HTTP body（`POST /models`、`POST /sessions`）、URL 路径（`GET /models/{model_id}/...`）与 `ChatRequest`，直接成为 `data/models/<model_id>/...` 与 `data/artifacts/<model_id>/...`。`model_id = "../../../../tmp/pwned"` 会写到数据目录之外；绝对路径则读/写它指向的地方。
  2. `asset_export` 的 `name` 来自**模型自己**（工具参数），在 worker 里变成 `os.path.join(out_dir, f"{name}.{fmt}")` —— 一个被诱导的模型可以写 `../../x.step`。
  3. `asset_import` 的 `path` 同样是模型给的，是一个**任意文件读取原语**。
  任务书 §5-E 明确要求"校验 model_id、thread_id、导入路径、导出名称及解析后的目录边界，覆盖路径穿越和符号链接"。
* **复现**：`pytest tests/unit/test_ids.py -q`。把四层校验全部去掉后 **12 项红**（已做变异验证：store 两条、API 五条参数化、chat 两条、导出名一条、导入一条）。
* **根因**：把"标识符"当成了普通字符串。它其实是**路径组件**，而路径组件有形状约束；缺了这一层，`..` 就是合法字符。
* **修复**：
  1. 新增 `tcad/core/ids.py`，两条互相独立的机制：
     * `ensure_safe_id()` —— **形状规则**（`[A-Za-z0-9]` 开头，后接字母/数字/`.`/`_`/`-`，≤64 字符）。这条规则在构造上就不可能含分隔符或 `..`，所以**不依赖**任何后续的路径比较。
     * `contained_path()` / `ensure_contained()` —— 对**解析后**路径做包含判断（`..` 与符号链接先折叠），用于真正收路径的地方和纵深防御。
  2. store 层落地：`IrStore._model_dir`、`ArtifactStore.dir_for`（版本号强转 `int()`，`"v0/../../etc"` 在变成路径前就被拒）、`gate_report_path`。放在 store 而不是只放 API，是因为脚本/测试/未来的前端都可能绕过 API 直接调 store。
  3. API 边界用 pydantic `field_validator` 校验 `model_id` / `thread_id` → 干净 **422**（而不是深处抛出的 500），并且同一个规则覆盖所有取 id 的端点。
  4. `asset_export` 校验导出名 → `SCHEMA` 错误；`asset_import` 要求路径解析后落在 `workdir` 或 `data_dir` 之内，否则 `DENIED` 且**永不触达 worker**。
  5. worker 侧 `exporters.py` 加一份**镜像规则**（worker 禁止 `import tcad.core`，见 `compiler.py` 的 import ban），`tests/unit/test_ids.py` 用 AST 读出 worker 的正则，与 supervisor 版本在 18 个样例上逐项比对，防止两边漂移。
* **诚实边界**：包含校验本身对"检查与使用之间新建符号链接"不是原子的。主规则（形状）没有这个弱点，这也是为什么名字类的东西走形状规则。`asset_import` 现在被限制在 `workdir` 与 `data_dir` 内——一个需要读取工作区之外文件的嵌入方必须显式放宽该根。
* **测试**：`tests/unit/test_ids.py`（50 项）：形状规则的接受/拒绝语料（含 `..`、`%2e%2e%2fx`、NUL、超长、Unicode、前导 `.`/`-`）；`contained_path` 对 `..` 与**符号链接**的拒绝；`IrStore`/`ArtifactStore`/`gate_report_path` 拒绝穿越与绝对 id 且**磁盘上没有越界文件**；API 的 422（5 组参数化）；`/chat` 的 `model_id` 与 `thread_id`；导出名拒绝 / 正常名放行；导入路径拒绝（`/etc/passwd` 与相对穿越）且不触达 worker、合法路径放行；worker 与 supervisor 规则一致性。
* **验证**：`pytest tests -q` → **719 passed, 4 skipped**（632 单测 + 87 契约）；验收包重建通过。

## 17. 第七轮：补丁 payload 的类型安全（任务书 §5-A）

### C-15 `setattr` 绕过校验 + `list(v)` 把字符串炸成引用

* **位置**：`tcad/ir/patch.py`（`_merge_sketch`、`_merge_feature`、`_op_add_sketch`、`_op_add_feature`、`apply_patch`）。
* **问题**：两个方向上的"类型被忽略"：
  1. **赋值不校验**。pydantic v2 默认 `validate_assignment=False`，而合并辅助函数是逐字段 `setattr`。于是 `update_sketch {"reversed": "false"}` 把**字符串** `"false"` 存进了 `bool` 字段——而 `"false"` 是**真值**，编译器会把草图朝反方向翻，全程没有一个错误。实测确认：`sk.reversed == 'false'`。
  2. **`list(v)` 会重解释而不是拒绝**。`update_feature {"refs": "ft_pad"}` 走 `list("ft_pad")` → `['f','t','_','p','a','d']`，补丁被"接受"了，只是意思完全不是模型写的那个。`add_feature` 的 `dict(p.get("params"))` / `list(p.get("refs"))` 对错误类型抛的是裸 `TypeError`/`ValueError`（"int object is not iterable"），模型无法据此修复。
* **复现**：`pytest tests/unit/test_patch_type_safety.py -q`。去掉重校验与列表守卫后 **9 项红**（已做变异验证）。
* **根因**：把"IR 是类型化模型"当成了"写进去的东西自然就是类型化的"。赋值路径和构造路径是两回事，前者默认不校验。
* **修复**：
  1. `apply_patch` 在所有 op 应用之后、语义校验之前，把文档**重新解析**一遍（`_revalidate`：`IrDocument.model_validate(doc.model_dump())`）。每个字段重新过一遍声明类型，存下来的就是 schema 说的那个东西。`"false"` 被正确解释为 `False`，`"perhaps"` 被拒绝。
  2. `_require_list()` / `_require_mapping()`：列表与对象字段必须是数组/对象，否则 `SCHEMA` 错误并**点名字段**（`refs must be a JSON array, got str`）。`geometry/constraints/refs/params/plane/offset` 及其 `*_append` 变体全部覆盖，`add_*` 与 `update_*` 两条路径都覆盖。
  3. op handler 抛出的 pydantic `ValidationError` 统一转成结构化 `PatchError(SCHEMA)`，并把 `_brief()` 渲染成 `字段路径: 原因`——而不是十几行 pydantic dump 加文档链接。
  4. 工具描述里明确写出"这些是数组 / 这些是布尔 / 不做隐式转换"。
* **不回退保证**：`apply_patch` 在深拷贝上工作，任何一步抛出都会丢弃整份候选文档；store 只在纯函数返回后才写事件与快照。测试同时断言**原文档逐字段未变**且**事件日志与版本号都未动**。
* **测试**：`tests/unit/test_patch_type_safety.py`（17 项）：`"false"` 必须存成 `False`、无意义布尔被拒且点名、补丁后整份文档重新解析等于自身、字符串 `refs`/`refs_append`/`geometry*`/`constraints*` 被拒且点名、`params`/`plane` 非对象被拒、`add_feature` 的坏 payload 是点名错误而非 pydantic dump、未知 op 仍是语义错误、**多 op 补丁部分失败时整份回滚**、store 对拒绝的补丁**不写事件也不写快照**。
* **验证**：`pytest tests -q` → **736 passed, 4 skipped**（649 单测 + 87 契约）。

## 18. 第八轮：给模型的 JSON schema 真正执行（任务书 §5-A）

### C-16 `params_schema` 只发给模型，服务端从不校验

* **位置**：`tcad/tools/base.py:execute_tool`、各工具的 `params_schema`。
* **问题**：`params_schema` 通过 `as_openai_tool()` 成为 function declaration 的一部分发给模型，然后**再没有任何一处读过它**。`execute_tool` 直接调 handler，参数由各 handler 用 `args.get(...)` 自行解释。后果是**声明与行为脱节**：
  * `geo_view {"views": "iso"}`（字符串而非数组）不会被拒——`"iso"` 是真值，原样传给 worker；
  * `geo_measure {"what": "volume"}` 同理；
  * 缺 `required` 参数只有在 handler 内碰巧检查时才会报错。
  任务书 §5-A 明确要求"给模型的 JSON schema 必须在服务端真正执行，而不只是作为说明"。
* **复现**：`pytest tests/unit/test_tool_schema_enforcement.py -q`。把 `execute_tool` 里的校验去掉 → 2 项红；再把 `base_version` 的声明改回 `integer` → 另外 2 项红（已做变异验证）。
* **根因**：把 schema 当成了"给模型看的文档"。文档与契约的区别就是后者会被执行。
* **修复**：
  1. 新增 `tcad/tools/schema_check.py`：纯 stdlib 的迷你 JSON-Schema 校验器，支持 `type`/`required`/`properties`/`items`/`enum`/`anyOf`/`oneOf`/`$ref`+`$defs`/`additionalProperties`；`title`/`description`/`default` 归为注解不校验。返回**问题列表 + 路径**（`arguments.views: expected array, got str ('iso')`）。
  2. `execute_tool` 在**权限检查之后、handler 之前**校验；失败返回 `ToolError(SCHEMA)` 并附 hint，**handler 不会被执行**（测试断言 handler 未被调用）。
  3. `type: integer` 显式排除 `bool`（Python 里 `isinstance(True, int)` 为真）。
  4. **声明必须与实际一致**：`ir_patch` 的 `base_version` 同时接受整数和文档写明的 `"current"`（handler 在建 `IrPatch` 之前先解析它），而 schema 原来只声明 `integer`。若照原样强制，会把一个受支持的写法拒掉——所以把声明改成 `anyOf [integer, string enum ["current"]]`。
  5. **防漂移守卫**：单测遍历所有已注册工具的 schema，断言关键字集合是校验器支持集合的子集。以后谁加了 `pattern`/`minimum` 却没教校验器，测试就会红，而不是让 schema 悄悄变回装饰。
* **诚实边界**：这不是完整 JSON Schema，只是本项目 schema 实际用到的那一子集；深层的 op 专属校验仍在 `IrPatch.model_validate`（类型化模型）里，本模块只做形状检查。`additionalProperties` 只在 schema 显式写 `false` 时拒绝多余键（本项目目前只有 `payload` 显式 `true`）。
* **测试**：`tests/unit/test_tool_schema_enforcement.py`（14 项）：类型不匹配带路径、`bool` 不是 `integer`、required/多余属性、`enum`/`anyOf`/`$ref`、坏 `$ref` 报错而非忽略、注解被忽略、**所有工具 schema 的关键字都在支持集合内**、畸形调用**不触达 handler**、合法调用照常执行、缺 required 被拒、`base_version: "current"` 被接受、真实 `geo_view` schema 拒绝字符串 `views`、真实工具的最小合法调用全部通过。
* **验证**：`pytest tests -q` → **750 passed, 4 skipped**（663 单测 + 87 契约）。真内核契约（含脚本化 LLM 走完整 `/chat` → `ir_patch`/`ir_commit` 的编排测试）全绿，说明强制校验没有破坏真实链路。

### §5-B `geo_measure` 与 `introspect_document` 结构一致性：复查结论

**已在早前轮次修复，本轮复查未重复打补丁。** `geo_measure_handler` 明确按 `introspect_document` 返回的
GeometryDigest 真实字段做映射（`topology.*`、`bbox.*`、`volume`、`holes`），没有 `measurements` 这个键；
未测到实体时返回错误而不是 `{}`；未知 `what` 项返回 `SEMANTIC` 并列出支持项；`holes` 缺失时给空数组而不是缺键。
`tests/unit/test_geo_tools.py` 的 7 项（含"按真实 digest 形状尊重 what"、"拒绝未知测量项"、"无实体时报错"、"holes 缺失给空数组"）覆盖。

## 19. 第九轮：持久化的"当前是否已验证"与前端审批 ID

### C-17 落盘的结论没有"是否仍然适用于当前版本"这一层（R-3）

* **位置**：`tcad/store/artifacts.py`、`tcad/core/wiring.py:StoreAdapter`、`tcad/server/app.py`、`tcad/loop/engine.py`。
* **问题**：`ir_commit` 之后又发生写操作时，引擎内存里的 `_last_commit_passed` 会被清掉（早前轮次已实现并有测试）。但**落盘**的那一层没有对应概念：`gate_reports/<model>/v<N>.json` 如实记录"vN 曾通过"，这本身没错，可一旦调用方拿它当"当前通过"来展示，就会把一个陈旧的绿灯显示成实时的绿灯。审计 R-3 记的就是这条："存储中的历史 PASS 事件可以被当成'当前通过'展示；需要落库的'结论失效'机制"。
* **复现**：`pytest tests/unit/test_verdict.py -q`。把 `verified` 的判定改成 `passed and published`（去掉版本时效性）→ 1 项红；把引擎的失效规则改回硬编码名单 → 1 项红（已做变异验证）。
* **根因**：把"版本 N 被评过且通过"（报告的语义）与"我正在看的这个东西已验证"（人的问题）当成了同一个问题。
* **修复**：
  1. 新增 `build_verdict(data_dir, model_id, current_version)`：`verified` 由**两个必须同时成立**的事实算出——落盘报告是**当前版本**的且 `passed`，**并且**该版本的产物确实已发布（第 2 轮的发布门只在通过后才写版本目录 + `manifest.json`）。任一单独成立都不算 verified，`reason` 明确说出是哪一条不成立（"从未评级" / "报告是 vN 的，已过期" / "通过了但没有产物" / "没有通过，阻断项是…"）。
  2. `StoreAdapter.verdict(model_id, version=None)` 作为唯一入口，默认取当前版本——API、前端、下一轮的上下文不必各自解释"上一次报告"。
  3. `GET /models/{id}/verdict`；`/models/{id}/artifacts` 的返回里带 `verdict`；`/sessions` 每行带 `verified`。**产物存在不等于验证通过**，客户端不必再去推断。
  4. 引擎的失效规则从硬编码的 `("ir_patch", "raw_python")` 改为**结构化**判定：任何 **write 或 privileged 档**的工具成功执行且不是 `ir_commit` 本身，就使通过失效。硬编码名单在它之后新增第一个写工具时就会悄悄失效，而"写操作之后通过仍然有效"正是这条机制要防的事。
* **诚实边界**：`verified` 要求"产物已发布"，所以一个在第 2 轮之前构建、目录里没有 `manifest.json` 的旧版本会被判为未验证（假阴性，取保守方向）；`artifact_manifest` 字段单独给出，便于区分"没发布"与"发布了但没有清单"。判定是读磁盘的两个文件，不是原子快照——并发构建期间它可能观察到中间态，此时它给出的答案是"未验证"，同样取保守方向。
* **测试**：`tests/unit/test_verdict.py`（13 项）：从未评级 / 通过且有产物 → verified / 通过但无产物 → 未验证且 reason 点明 / 失败报告带阻断项 / **写操作之后新版本未验证而旧版本仍如实为已验证** / 报告版本不符 → 明确"stale verdict" / `StoreAdapter.verdict` 默认当前版本且未知模型抛 `FileNotFoundError` / verdict 端点 200 与 404 / 产物列表带 verdict / 会话列表的 `verified` 对新播种模型为 False / **任何 write 档工具（新注册的、不在任何名单里的）都会使通过失效** / `ir_commit` 自身不会使自己失效。
* **验证**：`pytest tests -q` → **766 passed, 4 skipped**（679 单测 + 87 契约）。

### C-18 前端用 `p.approval_id` 取审批 id，而接口返回的是 `id`（任务书 §5-E）

* **位置**：`tcad/server/ui/app.js`（审批面板）。
* **问题**：`ApprovalRecord` 的字段是 `id`，`GET /approvals` 返回 `model_dump()`，所以键就是 `id`。前端三处读 `p.approval_id` → `undefined`，批准/拒绝按钮 POST 到 `/approvals/undefined`，得到 404，而 404 被面板外层的 `catch {}` 吞掉。**表现是：点"批准"什么都不会发生，也不报错。** 这正是任务书 §5-E 点名的"前端审批 ID 与接口字段不一致"。
* **复现**：`pytest tests/unit/test_server_ui.py -q -k approval`。把前端改回 `${p.approval_id}` → 结构断言立刻红（已做变异验证）。
* **根因**：字段名靠记忆在两端各写一遍，没有一处把"接口返回什么"和"前端读什么"绑在一起。
* **修复**：前端改用 `p.id`；同时把**将要执行的内容摘要**（`args_summary`）与会话（`thread_id`）显示出来——只给工具名让人点"批准"，那不是审批。测试同时断言 `ApprovalRecord` 的字段集合包含 `id` 且不含 `approval_id`，把接口契约钉住。
* **诚实边界**：这是**源码结构断言**加接口契约断言，不是浏览器端到端点击测试（本仓库不引入 JS 测试运行时，见 `test_server_ui.py` 的说明）。
* **测试**：`tests/unit/test_server_ui.py` 新增 3 项：前端不得出现 `approval_id` 拼法且必须用 `${p.id}`；前端必须显示 `args_summary`；`ApprovalRecord` 字段集合包含 `id`/`args_summary`/`thread_id`/`expires_at`。

## 20. 第十轮：前端异步会话切换与真实状态展示（任务书 §5-E）

### C-19 切换会话时，上一个会话的慢响应会覆盖新会话的面板

* **位置**：`tcad/server/ui/app.js`（`refreshInspector` / `loadArtifacts` / `loadView` / `switchSession`）。
* **问题**：三个加载器都是"读 `state` → `await` 网络 → 写 DOM"。用户在等待期间切换会话时：
  * `refreshInspector` 会把**旧模型**的特征链渲染进**新会话**的面板，并把 `state.version` 覆盖成旧模型的版本；
  * `loadArtifacts` 更明显：URL 是用旧 model id 取的，但渲染链接时又读**当前**的 `state.modelId`——于是列出来的是旧模型的文件、链接却指向新模型；
  * `loadView` 会把旧会话的渲染图贴到新会话的画布上。
  已有的 `state.turn` 身份只保护了 SSE 回合的帧，没有保护这三个加载器。
* **复现**：`pytest tests/unit/test_server_ui.py -q -k "session_epoch or async_loader or artifact_links"`。删掉 epoch 自增、删掉 `await` 之后的 `stale(token)` 检查、或把产物链接改回 `state.modelId`，对应断言立刻红（已做变异验证）。
* **根因**：把"当前会话"当成了全局可变的隐式上下文，而不是请求自己的身份。任何跨 `await` 读取它的代码都会在切换后读到一个**不同的**会话。
* **修复**：
  1. `state.sessionEpoch`；`switchSession` **先**自增再改状态，这样在飞的请求立刻作废。
  2. `sessionToken()` 在第一个 `await` 前取一份 `{epoch, modelId, version}`；每个加载器在**每次** `await` 之后 `if (stale(token)) return;`。
  3. 产物链接与标题一律用 token 里的身份拼装——响应属于哪个会话，就用哪个会话的身份展示它。
* **诚实边界**：前端仍无 JS 测试运行时（本仓库不引入），回归是**源码结构断言**。为补上这一层，新增一条 `node --check`（有 node 时执行，无则跳过）：它抓的是源码断言抓不到的那一类错误——文件根本不是合法 JS。已用"故意插入语法错误"验证它会红。

### C-20 前端不从"有文件"推断"已完成"（任务书 §5-E）

* **位置**：`tcad/server/ui/app.js`（检查器、会话列表）。
* **问题**：界面此前只显示 `v{N}`，读起来像进度；没有一处显示后端对**当前版本**是否验证过的判定。于是"v5 从未通过"与"v5 已通过并发货"在界面上长得一样。
* **修复**：检查器新增「验证状态」段，直接显示 `GET /models/{id}/verdict` 的 `verified` / `graded_version` / `attempt_id` / `blocking_failures` / `reason`，逐字展示后端给出的理由；侧边栏会话列表把 `v5` 改成 `v5 ✓`（已验证）/`v5 ✗`（未验证）/`v5 ?`（未知）并附 title 说明。
* **测试**：`tests/unit/test_server_ui.py` 新增 6 项：epoch 必须自增；三个加载器必须在第一个 `await` **之后**仍有 `stale(token)` 检查；产物链接不得在 await 之后使用 `state.modelId`；检查器必须调用 `/verdict` 并展示 `v.verified` 与 `v.reason`；会话列表必须区分 `s.verified === true/false`；`node --check` 通过。
* **验证**：`pytest tests -q` → **772 passed, 4 skipped**（685 单测 + 87 契约）。

## 21. 第十一轮：参数白名单必须等于"编译器兑现得了"（任务书 §5-A / R-7）

### C-21 白名单按属性**同名**收录，编译器却设不进去

* **位置**：`tcad/ir/validate.py:_VERIFIED_OP_PARAMS`、`tcad/worker/compiler.py`（`_assign_props`、死代码 `_PROFILE_OPS`）。
* **问题**：一个键只要存在同名 FreeCAD 属性就会被列进"已验证参数表"。但 `_assign_props` 是用 **JSON 标量**去 `setattr`，而这些属性里大量是 `App::PropertyLinkSub` / `LinkList` / `Part::Shape` / `Vector`，标量设不进去。实测（FreeCAD 26.3.0 / 48708，通过真 worker）：
  ```
  params={'length': 5.0, 'up_to_face': 'Face6'} -> raised
  TypeError: type must be 'DocumentObject', 'NoneType' or ('DocumentObject',['String',]) not str
  ```
  三种键都这样（`up_to_face` / `reference_axis` / `start_reference`）。失败发生在**补丁已经落盘、worker 往返已经花掉之后**，而且报的是 FreeCAD 的内部 TypeError。审计 R-7 记的正是这条："描述与实现不一致的残留；会误导模型"。同时 `_PROFILE_OPS` 是赋值一次、无人读取的死代码。
* **复现**：`pytest tests/unit/test_param_capability.py tests/contract/test_param_capability.py -q`。把 `up_to_face` 放回 pad 的白名单 → 3 项红；删掉 op 专属的文案修正 → 2 项红；把 `_PROFILE_OPS` 放回去 → 1 项红（已做变异验证）。
* **根因**：把"属性存在"当成了"参数可用"。可用还要求**值的形状**能对上——`setattr` 能吃的只有标量族。
* **修复**：
  1. 白名单**只保留 `_assign_props` 真能设的键**（Length/Distance/Angle/Bool/Enumeration/Float/Integer/String/Percent/*Constraint）。移除的键：`profile`、`base`、`up_to_face`、`up_to_shape`、`reference_axis`、`start_reference`、`mirror_plane`、`originals`、`axis`（图案类）、`direction`/`direction2`、`spacings*`/`spacing_pattern*`、`suppressed_positions`、`add_sub_shape`、`fuse_order`（groove）、`depth_type`（pad/pocket）。
  2. 新增 `_GUIDANCE_BASE` + `_GUIDANCE_BY_OP`：用被移除的键是**校验错误** `param_unsupported`，消息里点名替代写法，在落盘之前拒绝。文案按 **(op, key)** 解析，因为**同名键在不同对象上是不同的属性**——实测：`Base` 在 Fillet/Chamfer 上是 `LinkSub`、在 Revolution/Groove 上是 `Vector`（基点）；`DepthType` 在 Hole 上有、在 Pad/Pocket 上没有；`FuseOrder` 在 Revolution 上有、在 Groove 上没有。顺手把 `hole.depth_type`（真实 Enumeration，可设）**加进**白名单，而不是一刀切拒绝。
  3. 删除死代码 `_PROFILE_OPS`。
  4. `pad.direction`/`pocket.direction` 是 `Vector`，`_assign_props` 没有 Vector 分支 → 拒绝并在文案里说明"自定义拉伸方向尚不支持"（这是**待实现的能力**，不是已支持的参数）。
* **测试**：
  * `tests/contract/test_param_capability.py`（4 项，真内核）：对白名单里每个键读 `getTypeIdOfProperty`，要求落在可设置集合内（`axis` 在 revolution/groove 是编译器自己消费的**唯一**例外）；对每个被拒绝的键，要求它确实是 absent 或非标量；探针自证能力（能分辨 Length / Vector / LinkSub / LinkList / absent 五种答案）；样例用到的键逐个可设。
  * `tests/unit/test_param_capability.py`（24 项）：同一张静态分类表（值就是属性类型，防止有人"改测试迁就实现"）；被移除的键都在白名单外；每个被移除的键都有文案；拒绝是 `error` 且消息含文案与 target_id；**拒绝严格跟随 per-op 表**（`axis` 在 revolution 放行、在图案拒绝；`fuse_order` 在 revolution 放行、在 groove 拒绝）；未知键仍然是 warn 而不是 error；实验性 op 仍然放行；`_PROFILE_OPS` 已不存在。
  * 契约层再加一条不变量：**Vector 类型的拒绝文案必须包含 "Vector"**，不能写成"边引用"——否则会把模型引向错误的修法。
* **诚实边界**：`_assign_props` 仍然不做**值**类型校验（例如给 Float 属性传字符串会在 setattr 时失败）；本轮只保证**键**是可兑现的。要真正支持 `direction`/`up_to_face` 等，需要给 IR 增加引用/向量类型的参数（并配真内核证明），那是能力扩展而不是白名单修正。
* **验证**：`pytest tests -q` → **800 passed, 4 skipped**（708 单测 + 92 契约）。

## 22. 第十二轮：把 `groove` 从"代码在"变成"量过"

### C-22 `groove` 只证明能 `addObject`，没有任何旋转切除的证据（任务书 §6 / R-6）

* **位置**：`tcad/worker/compiler.py`（`_AXIS_OPS` 已含 groove）、`tcad/ir/capability.py`。
* **问题**：`groove` 与 `revolution` 共用同一套轴翻译（`_set_axis_reference`），但"机制相同"是论证不是证据：没有任何测试量过一次**旋转切除**。capability 表里它标着 EXPERIMENTAL / "no real-kernel test"——诚实，但也没法让模型放心用它。
* **复现**：`pytest tests/contract/test_groove.py -q`。把 `groove` 从 `_AXIS_OPS` 去掉 → **9 项全红**（已做变异验证）。
* **修复**：新增 `tests/contract/test_groove.py`（9 项真内核），并把 `groove` 升为 VERIFIED，`proof` 指向该文件。轮廓取圆柱壁上的一个环形截面（XZ 上 x∈[r_in, R]、z∈[z0, z0+w]），绕轴整圈旋转切除的体积有闭式解 `π(R²−r_in²)·w`。
* **测试内容**：
  * **三个数值不同的样例**都精确等于解析体积（相对 1e-6；只写一个例子的话硬编码也能过）；
  * 外围尺寸不变（30×30×40）——环槽不该改变外包络；
  * **180° 恰好切掉一半的环**（Angle 真的驱动几何，不是标签）；
  * `axis="Z"`（body 原点轴）与 `axis="V_Axis"`（草图轴）量到同一实体；
  * 未知轴名 → 结构化 `semantic` 错误且 `feature_id` 正确；
  * **切不到材料时报错而不是成功**：轮廓整个落在圆柱之外时，编译器给的是具体的
    `feature 'ft_groove' (op='groove') did not change the solid: the result is identical to the previous feature`
    （并指向 WORLD COORDINATES 契约），不是通用失败——测试把这个措辞钉住，因为它是模型唯一能据以修复的信息；
  * STEP 回读体积一致；
  * 交付的 FCStd 重开后 `ft_groove` 仍是 `PartDesign::Groove`、`ft_cyl` 仍是 `PartDesign::Pad`，把 `Groove.Angle` 改成 180° 后**去掉的体积恰好是半个环**。
* **验收产物**：新增**样例 E**（`review/acceptance/sample_e/`）——圆柱 + 周向环槽，turn 1 体积 8500π，turn 2 改 `Groove.Angle` 为 180° 后 8750π，并重新导出 `accept_e_v2.*`。重建后 `attempt_id = acc-20260919T124605Z-cde71e3f`，108 个产物文件逐文件 sha256/字节数**磁盘复核通过**，36 张预览全部非空白。
* **诚实边界**：`groove` 仍要求轮廓与材料相交（切空会被判为构建错误）；切割方向由轮廓所在平面的法向与 `axis` 共同决定，与 `pocket` 一样遵循 WORLD COORDINATES 契约。`fillet`/`chamfer`/阵列/镜像仍是 EXPERIMENTAL——它们需要的是**边/面引用**，IR 至今没有承载它的字段（这与 `axis` 那类"可以用标量枚举表达"的引用不同）。
* **验证**：`pytest tests -q` → **809 passed, 4 skipped**（708 单测 + 101 契约）。

## 23. 第十三轮：贴面草图——把"无法执行的指令"变成可执行的能力

### C-23 `ir_digest` 不列面，工具描述却叫模型去查面名；贴面草图无真内核证据

* **位置**：`tcad/core/types.py`（`GeometryDigest`）、`tcad/worker/introspect.py`、`tcad/context/digest.py`、`tcad/ir/validate.py`、`tcad/worker/compiler.py`、`tcad/tools/ir_tools.py`。
* **问题**：两件事叠在一起才致命。
  1. `plane: {"kind":"face","feature_id":..., "sub":"Face6"}` 在 IR 与编译器里一直是支持的，但**没有任何真内核测试量过它**（能力矩阵里挂"未验证"）。
  2. 工具描述明确写着"把面的编号查出来（ir_get / ir_digest），不要猜"，而 `ir_digest` 只给 `topology.faces`（一个**数量**）。模型**没有办法**知道 Face6 是顶面——这条指令本身无法执行。这正是任务书 §5-A 的"运行时提示词必须与实际工具 schema、能力一致"。
  3. 附带：面名写错时 FreeCAD **不报错**，只是让草图保持未附着，轮廓在自己的坐标系里塌陷，于是构建失败信息变成
     "does not form a closed wire … 所有轮廓点都在同一位置 … 检查世界坐标"——把模型引向完全错误的修法。
* **复现**：`pytest tests/contract/test_face_attachment.py tests/unit/test_ir_validate.py -q`。删掉校验器的面检查 / 删掉编译器的子元素检查 / 让 digest 不再测面 → 共 **7 项红**（已做变异验证）。
* **根因**：能力实现了，但**发现路径**没有实现；而"未验证"掩盖了"根本无法使用"。
* **修复**：
  1. `GeometryDigest.faces: list[FaceInfo]`（附加字段，默认空，老 digest.json 仍可解析）。worker 的 `_measure_faces()` 从真实 BRep 读出每条**平面**的 `name`（FreeCAD 1-based `Face<N>`）、`area`、外法向 `normal`、`center`；上限 32 条；曲面（圆柱壁等）不列出，因为 FlatFace 附件只对平面有意义。
  2. `tcad/context/digest.py:render_digest_text()` 逐条打印面清单——这是模型真正读到的那份文本（`ir_digest` 与上下文块都经过它），并写明附件写法。`ir_digest` 的工具描述同步更新。
  3. 校验器新增 `face_target`（`feature_id` 必须是本文档里的特征）与 `face_sub_missing`（必须给 `sub`），都是 `error`，在落盘前拒绝。
  4. 编译器在真内核上用 `Shape.getElement(sub)` 检查子元素是否存在，失败时报 `sub-element 'Face99' does not exist on feature 'ft_plate' (IndexError: Shape index 99 out of bound 6). Available faces: Face1, …, Face6. Call ir_digest …`。
* **测试**：
  * `tests/contract/test_face_attachment.py`（9 项真内核）：digest 的 6 个面名与真实 box 一致、顶面 +Z/面积 4000/中心 z=8、底面唯一、4 个侧面；面清单进入模型可见文本；**圆柱只列两个平面盖**（曲面不列）；贴顶面凸台体积精确 = `32000 + 360π` 且包围盒 z=18、x/y 不变；两个数值不同的尺寸组合同样精确；**贴侧面 Face1 时材料沿该面法向生长**（x 不变、y/z 变化）——证明"该面自己的坐标系"这一语义；未知面名报错并列出可用面名；digest 给出的面名在同一份构建上可用。
  * `tests/unit/test_ir_validate.py` 新增 3 项：面平面必须引用存在的特征、必须有子元素名、合法写法通过。
* **验收产物**：重建 `review/acceptance/`（`attempt acc-20260919T125156Z-3f77cb68`；**该轮快照**，当前生效的包见 §9/§32），digest.json 现在带面清单（样例 A 的顶面 area=3200 = 80×50−40×20，正是"开了槽的顶面"），108 个产物逐文件 sha256/字节数**磁盘复核通过**，36 张预览非空白。
* **诚实边界**：面编号由内核分配，**不保证**在任意编辑后稳定；本轮钉住的是更弱但诚实的一条——digest 给出的面名在同一份构建上可用，且用 `normal`/`center`/`area` 让模型可以**按意图**选择而不是按编号赌。非平面（圆柱面、锥面）与边引用仍不列出，`fillet`/`chamfer` 因此仍是 EXPERIMENTAL。
* **验证**：`pytest tests -q` → **821 passed, 4 skipped**（711 单测 + 110 契约）。

## 24. 第十四轮：边引用——把 `fillet`/`chamfer` 从"能建对象"变成"量过"

### C-24 `fillet`/`chamfer` 只证明过 `addObject`；而它们**根本无法编译**，且边中点报错坐标

* **位置**：`tcad/worker/compiler.py:_set_base_reference`、`tcad/worker/introspect.py:_measure_edges`、`tcad/ir/capability.py`、`tcad/ir/schema.py:187-192`。
* **问题**：三个事实叠在一起，前两个是隐藏的真缺陷。
  1. **它们曾经连一次都编译不出来。** `_set_base_reference` 在文档**从未 recompute**、被引用特征**还没有 Shape** 时就执行 `target.Shape`，于是任何 fillet/chamfer 都死在 `base_feature 'ft_plate' produced no shape to take edges from`。同一份代码里 `_add_sketch` 已经写明"必须先 recompute 再几何"这个道理，边引用这一步漏掉了。
  2. **`ir_digest` 的边中点不是中点。** `Edge.valueAt(t)` 取的是**参数**而非比例：对一条直线其范围是 `[0, length]`，所以一条 8 mm 竖边的"中点"被报成 `[0, 0, 0.5]`——一个和这条边在哪毫无关系的点（短于 0.5 的边甚至会落到边外，因为直线会外推）。模型据此认边必然认错。
     （对比：`Face.normalAt(u,v)` 是**归一化**的，所以面代码看起来相似但是对的——这正是这条 bug 能长期存活的原因。）
  3. 能力表里 `fillet`/`chamfer` 是 `EXPERIMENTAL`，工具描述让模型"用 `base_feature` + `sub_elements` 选边"，但没有一条真内核测试证明这条路径通。
* **复现**：`pytest tests/contract/test_fillet_chamfer.py -q`。首跑 **9 红 2 绿**（全部是缺陷 1）；修掉后第二跑 **10 绿 1 红**（缺陷 2：断言的 `mid` 是 `[0,0,0.5]`）。把编译器里的 recompute 删掉 / 把 `mid_param` 换回 `0.5`，对应测试立刻红（已做变异验证）。
* **根因**：边引用的**发现路径**（digest 里的边描述）与**使用路径**（`Base = (feature, subs)`）都没有被真内核走通过一次。
* **修复**：
  1. 编译器在解析 `Base` 前 recompute 目标所在文档（与 `_add_sketch` 同一惯例），recompute 自身抛错则返回结构化 compile 错误，不是带着半成品继续。
  2. `_measure_edges` 用 `(FirstParameter + LastParameter) / 2` 取真实参数中点；`direction` 的 `tangentAt` 同步修正。digest 的每条边带 `name`/`kind`/`length`/`mid`/`direction`（`EdgeInfo`），渲染文本里给出 `base_feature=<feature> + sub_elements=["EdgeN", …]` 的写法。
  3. `capability.py` 把两条 op 移到 `VERIFIED`，`proof` 指向 `tests/contract/test_fillet_chamfer.py` 并写明量的是什么（解析削减体积、重开改参），`gap` 清空。
* **测试**：`tests/contract/test_fillet_chamfer.py`（11 项真内核）。
  * 认边**不硬编码名字**：先从 `ir_digest` 按 `kind == "Line"` + `direction ≈ ±Z` 挑出四条竖边，再断言它们的中点 z=4.0、长度 8。
  * **解析恒等式**：80×50×8 板上四条竖边 r=5 圆角，削掉的体积必须恰好是 `4(1−π/4)r²t`；倒角 d 则是 `4(d²/2)t`——两组数值不同的参数（r=5/3/4.5、d=2/3）+ 只倒两条边时削减量减半。相对误差 1e-6，包围盒不变、实体数仍为 1。
  * **重开可编辑**：读回 FCStd 后把 `Radius` 从 5 改成 8 重新计算，体积按新半径重算；Pad/Sketch 仍在。
  * **失败可定位**：`Edge99` 报 `semantic` 错误并列出可用边名（含 `Edge1(...)`）；8 mm 板上 r=40 的圆角不能以"干净成功"返回。
* **漂移守卫**：`tests/unit/test_op_capability.py` 三处断言随能力表更新（`VERIFIED` 集合现在是 `additive_box/chamfer/fillet/groove/pad/pocket/revolution` 七项、`EXPERIMENTAL` 数量改为"总数 − 7"、实验性告警的样例 op 换成 `draft`），并新增一条**转正后不再告警**的测试——守卫是跟着能力表走的，不是被削弱的。
* **诚实边界**：边名由内核分配，不保证在任意编辑之后稳定；能钉住的是"digest 报出的边名在同一份构建上可用"，模型靠 `kind`/`length`/`mid`/`direction` **按几何意图**选边而不是赌编号。`mid` 是**参数中点**，不是质心（对直线两者重合，对圆弧不同）。
* **验证**：`pytest tests -q` → **833 passed, 4 skipped in 27.95s**（712 单测 + 121 契约）。


## 25. 第十五轮：镜像与阵列——两种"静默错误结果"的引用

### C-25 镜像/阵列缺引用时不报错，而是给出**一份**或**空形状**；交付的 FCStd 也无法改这两个参数（任务书 §6 / R-6 / §5-B）

* **位置**：`tcad/worker/compiler.py`（`_AXIS_LINK_OPS` / `_set_axis_reference` / `_set_mirror_plane`）、`tcad/ir/schema.py`（`FeatureSpec.plane`）、`tcad/ir/validate.py`（规则 5c/5d、白名单、文案）、`tcad/worker/reopen.py`（`_prop_value`）。
* **问题**：先用真内核探针把三个 op 的实际行为量出来，结果是两个**静默**失败和一个**无法交付**：
  1. `LinearPattern` **没有 `Direction` 时不报错**，返回一个**有效形状但只有一份**阵列——探针实测体积 1000 而不是 3000，`ok=True`、`is_valid=True`。这正是任务书反复要求消灭的"看起来成功"。
  2. `Mirrored` 没有 `MirrorPlane` 时返回**空形状**（NULL），同样是"编译成功"之后才炸。
  3. 交付的 FCStd 里 **`Occurrences` 和 `Suppressed` 根本改不动**：`reopen_edit_measure` 的 `_apply_edit` 把每个编辑值一律 `float(value)`，于是整数属性报 `type must be int, dict or tuple, not float`、布尔属性报 `type must be bool, not float`。也就是说"把阵列从 3 份改成 5 份""把镜像抑制掉"这两个最自然的参数化修改，在这条路径上**一次都不可能成功**。
  4. 探针还发现本机 `PartDesign::CircularPattern` 是**间距驱动**的（`NumberCircles`/`RadialDistance`/`TangentialDistance`，没有 `Angle`/`Occurrences`），表达不了"N 份均布在一个角度内"；经典 API 在 `PartDesign::PolarPattern` 上（`PP.Angle = 360 deg`、`PP.Occurrences = 3` 实测）。
* **复现**：`pytest tests/contract/test_patterns_mirror.py -q`。首跑 **12 绿 4 红**——失败 1 条是测试自身的 `_hole_centres` 把 digest 的 `center` 当字典（实际是 `[x,y,z]` 列表），另 3 条是上面第 3 条真缺陷。
* **根因**：这三个 op 需要的都是 **`PropertyLinkSub`**（镜像面、阵列轴），JSON 标量表达不了；IR 里既没有承载字段，工具描述也只字未提 `params.axis`。而"缺引用"在本机内核里既不报错也不失败，于是缺口一直以"编译通过"的形式存在。
* **修复**：
  1. **类型化载体**。镜像复用草图附着那套 `PlaneRef`（`FeatureSpec.plane`，`origin_plane`/`datum_plane`/`face` 三种 kind），阵列用 `params.axis` 这个**名字**——`_set_axis_reference` 把它翻译成 `Direction`/`Axis` 的 LinkSub（`LinearPattern.Direction`、`PolarPattern.Axis` 都是实测确认的属性名）。`H_Axis`/`V_Axis`/`N_Axis` 是**草图自身**的轴，对阵列无意义（阵列没有 profile），因此被明确拒绝并说明理由。
  2. **缺引用 = 具名拒绝**。编译器与校验器都不再"让它过去"：缺轴是 `semantic` 错误并说明"FreeCAD 会静默只生成一份"；缺镜像面同样是 `semantic`；不存在的面名报 `Available faces` 并列出真实面名（复用贴面草图那轮的做法）。
  3. **重开编辑按属性类型赋值**。`_prop_value` 先读 `getTypeIdOfProperty`：`PropertyBool` 要真 bool、`PropertyInteger*` 要整数值（不接受 3.5）、长度/角度走 `Quantity`、其余才 float。`Suppressed=True` 与 `Occurrences 3→5` 由此成为可交付、可验证的修改。
  4. **`circular_pattern` 留在 EXPERIMENTAL**，`gap` 写明本机是间距 API、请改用 `polar_pattern`——宁可标成实验性，也不假装它能表达"N 份均布"。
* **测试**：`tests/contract/test_patterns_mirror.py`（16 项真内核）。
  * **镜像恒等式**：跨 XZ 镜像体积恰好翻倍（2·80·50·8）、包围盒 y 变 100；跨 XY 则 z 变 16；跨零件**自身的某个面**（从 digest 按法向挑）后包围盒 x 变 160——三种面来源都量过。
  * **阵列位置从 BRep 实测**（不是读 IR 参数）：Extent 模式 3 份落在 x=20/35/50，Spacing 模式 offset=25 落在 20/45，沿 Y 落在 y=15/30，体积都精确等于 `w·h·t − n·πr²t`。
  * **圆周阵列**：以原点为中心的板、孔在 (15,0)，270°/4 份的实测孔心恰好是 (15,0)(0,15)(−15,0)(0,−15)——任何一个错轴都做不出这个集合。
  * **重开可编辑**：读回 FCStd 把 `Occurrences` 3→5，体积精确变成 `32000 − 5·72π`，被阵列的 Pocket 仍是 `PartDesign::Pocket 且非 Invalid`；`Suppressed=True` 后体积退回纯底板、包围盒回到 80。
  * **失败可定位**：缺轴（消息里说明"一份"）、`H_Axis`、缺镜像面、`Face99`（消息列出 `Available faces` 与 `Face1`）、原点面名不在枚举内（模式层直接拒绝）、空阵列——全部是具名拒绝，没有一条"干净成功"。
* **漂移守卫**：`tests/unit/test_op_capability.py` 的 `VERIFIED` 集合更新为 10 项（`experimental == 总数 − 10`），并新增 `mirrored`/两个阵列 op 的"转正后不再告警"；`tests/unit/test_param_capability.py` 的真内核探针把 `linear_pattern.axis → absent`、`polar_pattern.axis → App::PropertyLinkSub` 也**量出来断言**——`axis` 之所以能留在白名单，是因为编译器翻译它，而不是因为探针看走了眼。
* **诚实边界**：镜像面/阵列轴只支持机体三轴（`X`/`Y`/`Z`）与原点基准面（`XY`/`XZ`/`YZ`）或零件自身的一个**平面**；倾斜自定义轴、非平面镜像仍无字段可表达。阵列份数为 1 时内核仍然"成功"（语义上是单份），本轮没有把它当错误处理。
* **验证**：`pytest tests -q` → **857 passed, 4 skipped in 29.78s**（720 单测 + 137 契约）；`pytest tests/unit -q` → 720；`pytest tests/contract -q` → 137。


## 26. 第十六轮：调用进行中的 worker 可被中止（R-8，任务书 §5-D「中断必须覆盖模型请求、工具批次、正在运行的 worker 和子进程清理」）

### C-26 中断只停住了 supervisor：一次已发出的 FreeCAD 调用只能等它自己超时，并且把线程、暂存目录和子进程留在原地

* **位置**：`tcad/core/worker_client.py`（`WorkerHandle`）、`tcad/core/wiring.py`（`SyncWorkerClient`）、`tcad/loop/commit.py`（`_off_loop` / `_abort_worker` / `run_commit`）、`tools/serve.py`（关闭路径）、`tcad/core/types.py`（`ToolErrorKind`）。
* **问题**：任务书要求中断覆盖"正在运行的 worker 和子进程清理"。逐项复核后是三块真缺陷：
  1. **在飞的调用没有中止手段。** worker 协议是"一个进程、一次一请求"，没有 cancel 帧。上一轮（C-7）把 RPC 挪出了事件循环，于是"进程不冻了"，但被停止的回合仍会**等这次编译跑完**（上限 120s），期间还占着 worker——停止按钮只是不再看结果，活还在跑。
  2. **取消路径上藏着一个死锁。** 被取消的 `asyncio.to_thread` 只是"不再等待"：线程还在跑，结束时把响应投进一个没人再取的队列；队列一满（有界），**读线程**就阻塞在 `put` 上，之后**所有**响应都投递不出去。也就是说"取消一次调用"可能让 worker 连接从此装死。
  3. **取消后没有任何清理。** 被放弃的尝试留下 staging 目录（每停一次留一个）；服务进程 Ctrl-C 退出后 FreeCADCmd 子进程仍在（它会因 stdin 关闭而自行退出，但"迟早会自己退"不是"已清理"）。
* **复现**：
  * `tests/unit/test_worker_abort.py`：向一个真子进程发一次 `slow`（30s）调用并在中途 `abort_inflight()`。修复前只有两条路——干等 30s，或让 5s 超时把它**误报成** `WorkerCrashed`。
  * `tests/unit/test_commit_event_loop.py::test_cancelling_a_turn_aborts_the_running_build`：把 `_abort_worker` 改成空操作后，测试在 **10.03s** 后失败（构建一路跑完）——即"取消"确实停不住东西。
* **根因**：把"取消"实现成了"调用方不再等待"，而不是"让被调用的东西停下来"。worker 协议没有中途取消，能真正停掉一次 OCCT 计算的机制只有**结束那个进程**；而结束进程必须留下一个能被取消方看见的信号，否则只能退化成崩溃误报或超时。
* **修复**：
  1. `WorkerHandle.abort_inflight(reason)`：在 `_lock` 下递增 `_abort_epoch` 并记下原因（`_request_raw` 采样 epoch 用的是**同一把锁**，所以"这次调用是否被中止"是判定出来的）；然后 `proc.kill()` + `wait(timeout=2)` **就地回收**（返回时"已停止"就是真话，也不留僵尸），最后 `_fail_all_pending` 唤醒被阻塞的调用方——不等读线程去发现管道 EOF。
  2. `_request_raw` 收到合成帧（`id=-1`）时比较 epoch：变了 → `WorkerAborted`（`ToolErrorKind.CANCELLED`，消息带原因），没变 → 仍是原来的 `WorkerCrashed` + 惰性重启。真崩溃不会被洗成"取消"，反之亦然。
  3. `tcad/loop/commit.py` 的 `_off_loop(...)` 是唯一能察觉"这次构建已经没人要了"的地方：`await asyncio.to_thread(...)` 抛 `CancelledError` 时调 `_abort_worker(services, label)`（鸭子类型 `getattr(worker, "abort_inflight", None)`，因为最小测试 bundle 的 worker 是普通对象）。`_abort_worker` 自己绝不抛——停止不能失败在清理上。
  4. `run_commit` 捕获 `CancelledError` 时丢弃本次 attempt 的 staging 目录再 re-raise（staging 名带 `uuid4().hex[:8]`，只有整轮通过才发布，被放弃的尝试是纯残留）。
  5. `_dispatch` 由 `box.put(...)` 改为 `box.put_nowait(...)`（满则 debug 记录）——上面第 2 条的死锁在取消路径上被顺手关掉。
  6. `tools/serve.py` 把 `uvicorn.run` 包进 `try/finally`，退出（含 Ctrl-C）时 `handle.close()`。
* **测试**：
  * `tests/unit/test_worker_abort.py`（5 项）：中止让 30s 的调用在 5s 内结束并给出 `cancelled`/"abort" 语义；被中止的 handle **惰性**重启，下一个调用照常（`ping` → `pong`）；中止**之后**的真崩溃仍报 `WorkerCrashed`/`runtime`；"没有在跑的东西"时中止是 no-op（未启动 / 已关闭都返回 False）；工具层信封 `error["kind"] == "cancelled"`，且 `RpcError().kind` 默认值仍是 `"runtime"`（线上格式没变）。
  * `tests/unit/test_commit_event_loop.py`（5 项，含原有 3 项）：取消一个正在构建的回合 → worker 的 `abort_inflight` 被调用且理由是 `compile_ir`，整体在 2s 内返回（而不是 10s 的构建时长）；取消后暂存目录被删除、版本目录从未被创建。
  * `tests/contract/test_worker_interrupt.py`（4 项，**真 FreeCADCmd**）：真构建量出 40×20×5 板体积 4000；`abort_inflight` 后子进程真的死了（`is_alive() == False`、`os.kill(pid, 0)` → `ProcessLookupError`、`poll() is not None`）；下一次构建**重建** worker 后仍能量出 4000 且实体数为 1；重建后的 worker 仍把一次坏构建（孔跑到板外）报成带 `feature_id` 的结构化错误，而不是"成功"。
  * **变异验证**：`_abort_worker` 换成空操作 → 回合取消测试 10.03s 后失败；`_discard_staging` 换成空操作 → 暂存残留断言失败。
* **诚实边界**：
  * worker 协议**仍然**没有中途取消——"取消"的实现是杀进程，因此对 worker 内的任何中间状态都不做保留假设。
  * 一个服务进程只有**一个** worker 进程（`WorkerHandlePool` 存在但 `wiring` 未使用；`worker_pool_size` 目前只对 fork/join 有意义）。所以一次停止**也会**终止别的调用正在使用的那条 RPC——那些调用方各自收到 `cancelled`、各自按需重启。这是"能停止"与"共享一个几何后端"之间的真实取舍，已写进 README 限制 #7。
  * 真内核层**没有**覆盖"在飞调用被中止"：真实编译太快，抓不住中途那一刻；命中"在飞调用"的路径在单测层用真子进程 + 真 SIGKILL + 真回收验证，这一点在测试文件里写明，不假装契约层跑过。
  * 不做**急切**重启：下一次调用在入口自检里惰性重建，所以停止的代价就是被停止的那次构建，而不是每次停止都付一次冷启动。
* **验证**：`pytest tests -q` → **872 passed, 4 skipped in 31.86s**（731 单测 + 141 契约）；`pytest tests/unit -q` → 731；`pytest tests/contract -q` → 141。`pytest tests/unit/test_worker_abort.py tests/unit/test_commit_event_loop.py -q` → **10 项 2.57s**；`pytest tests/contract/test_worker_interrupt.py -q` → **4 项 1.11s**（墙钟随内核启动波动，两处都是实测；全仓耗时同理由 33.41s 变为 31.86s，均为实测值，不是估算）。

### C-27 运行时 system prompt 对工具集的描述没有守卫（任务书 §三）

* **位置**：`tcad/loop/engine.py:LoopConfig.system_prompt`（当前默认值）；新增 `tests/unit/test_system_prompt_truth.py`。
* **问题**：运行时的 system prompt 是一句关于工具集的**断言**——它点名 `ir_patch`/`ir_commit`/`ir_get`/`ir_digest`/`ir_list_features`/`geo_*`，并把后四个标为"只读、绝不改动设计"，还告诉模型"只有 `passed=true` 才算成功"。在这之前，没有任何测试把这些话与真实注册表对上：重命名一个工具、把 `ir_commit` 从 WRITE 降级或把只读工具提权，模型读到的提示词就会描述一个**已经不存在的系统**，而全部测试仍然全绿。工具描述那一侧早有守卫（`test_ir_tools_description.py` 的枚举/无通配/内容声明、`test_geo_tools.py` 的 digest 形状），唯独 system prompt 是空白。
* **复现**：把 `system_prompt` 里的 `ir_get` 改成 `ir_fetch`（或把 `ir_commit` 加进"Read tools"括号里），跑 `pytest tests/unit/test_system_prompt_truth.py -q` ——修复前无测试覆盖，修复后 1 项失败。
* **根因**：prompt 是常量字符串，工具集是 `build_default_registry` 的产物，两者之间没有任何代码或测试把"字符串里点的名"与"注册表里真实存在的名字/层级"连起来。断言与事实各写各的，漂移无人察觉。
* **修复**（`tests/unit/test_system_prompt_truth.py`，4 项）：
  1. 用 `build_default_registry` 建真实注册表（默认不开 `raw_python`），对 prompt 做 token 化（`[a-z][a-z0-9_]*` + `ir_/geo_/asset_/raw_` 前缀过滤，`ir_version` 显式列为"像工具名但不是工具"的豁免），断言**prompt 点名的每个工具都真的注册了**。
  2. 解析 prompt 里 "Read tools (...)" 那一句，把其中的 `geo_*` 通配展开成该前缀下的全部真实工具（另加显式名字），断言**每一个都真的是 `ToolTier.READ`**——"只读"这句话与注册表层级一致。
  3. 断言 prompt 里确实出现了这个通配家族（否则第 2 条会因为列表为空而空过），即"测试覆盖的通配就是 prompt 在用的通配"。
  4. 断言 prompt 里的成功判据字段（`passed`）在 `GateReport.model_fields` 里真的存在——模型被要求读的字段名不是编的。
* **测试**：`tests/unit/test_system_prompt_truth.py`（4 项）。**变异验证**（每次改真源码跑子进程、`finally` 还原，结果取退出码）：把点名的工具改成带数字的 `ir_get_v2` → 1 项失败；改成 `ir_fetch` → 1 项失败；把 WRITE 的 `ir_commit` 塞进只读列表 → 1 项失败；把 `geo_*` 换成 `asset_export` → 1 项失败；通配整个删掉 → 1 项失败；未变异的对照 → 4 项全过。
* **诚实边界**：这道守卫检查的是"名字存在、层级与'只读'声明一致、字段名存在"，**不检查** prompt 的措辞是否把工具语义讲清楚了（那需要人读）；它挡的是重命名/提权/删工具这类静默漂移，不是所有 prompt 质量问题。
* **修复过程中的一个真实缺陷（由变异验证本身抓到）**：第一版 token 正则写的是 `\b(?:ir|geo|asset|raw)_[a-z_]+\b`，**匹配不了含数字的名字**，于是把工具重命名成 `ir_get_v2` 时测试照样全绿——守卫对"带数字的新名字"是瞎的。改为 `re.findall(r"\b[a-z][a-z0-9_]*\b", text)` 再按前缀过滤后，五种变异才全部被抓住。这条记录在这里是因为它说明变异验证不是走过场：它抓出了守卫自己的盲点。
* **验证**：`pytest tests/unit/test_system_prompt_truth.py -q` → **4 passed**；该文件已并入全仓 **872 passed, 4 skipped in 31.86s**（731 单测 + 141 契约，见 §1.2）。


### C-28 工具描述里的坐标/方向声明原本只是"自己说自己"（任务书 §三、§5-B）

* **位置**：`tcad/tools/ir_tools.py::_IR_PATCH_DESCRIPTION`（EXTRUSION DIRECTION 段与 pad 参数段）；`tcad/worker/compiler.py::_assign_props` / `_prop_name`；`tests/contract/test_sketch_planes.py`、`tests/contract/test_build_failures.py`。
* **问题**：模型读到的坐标语义有三处**可检查的声明**，但守卫强度不一样：
  1. "XY -> +Z / XZ -> -Y / YZ -> +X"——原来的单元测试 `test_the_description_states_which_way_a_pad_extrudes` 只是把描述和三个字面量箭头比对，**这是同义反复**：描述改成什么都改不掉它，编译器真改了方向它也照样绿。
  2. "Set `reversed`: true, or `midplane`: true, **in the pad's params**"——这是描述**推荐**给模型的路径，而真内核测试 `test_sketch_reversed_flips_the_extrusion_side` 走的是**草图级** `SketchSpec.reversed`（模型从未被告知过的那个字段）。也就是说：描述指的路没人验过，验过的路描述里没提。
  3. 参数拼错会怎样，没有任何测试。
* **复现**：把描述里的 `XZ -> -Y` 改成 `XZ -> +Y`、或把编译器改成永远/从不 `MapReversed`，跑 `pytest tests/contract/test_sketch_planes.py -k extrusion`——修复前前者绿（描述测试只认字面量）、后者绿（没有把描述与实测连起来的断言）。
* **根因**：坐标语义在系统里以三份互相独立的形态存在——描述文本、编译器实现、真内核实测。三者之间有两两不一致的可能，而原来的测试只覆盖"文本没被删"这一层。
* **修复**：
  1. `test_extrusion_direction_follows_the_plane_normal` 改为**从描述里解析箭头**（只取 EXTRUSION DIRECTION 段，避免后续段落误匹配），再**从实测 bbox 推出**实体实际占据的有符号轴（`[0,5]` 记 `+`、`[-5,0]` 记 `-`），逐平面比对。任一方向漂移都会失败，且失败信息直接给出"承诺 vs 实测"。
  2. 新增 `test_the_pad_param_knobs_do_what_the_description_says`：在真内核上量出 pad 参数级 `reversed`（−Y→+Y）与 `midplane`（对称 −5..+5），并断言体积恒为 8000（换向不许改变体积）。
  3. 新增 `test_a_typo_in_a_param_is_refused_not_ignored`：`params.reveresd` 必须让构建失败，错误信息里带参数名与 `PartDesign::Pad`。
* **测试**：层一 `tests/unit/test_ir_tools_description.py`（保留，职责降为"这段文字还在"）；层二 `tests/contract/test_sketch_planes.py`（1 项，方向按实测比对）、`tests/contract/test_build_failures.py`（+2 项，pad 参数级换向/居中/拼错）。**变异验证**（改真源码跑真内核，`finally` 还原）：描述箭头改成 `+Y` → 失败；删掉 YZ 箭头 → 失败；编译器无条件 `MapReversed` → 失败；`App::PropertyBool` 分支改成空操作 → pad 参数测试失败；`_prop_name` 对未知键放过 → 拼错参数测试失败；未变异对照 → 全过。
* **探针先行的结论（这是本轮最该记住的一条）**：在写测试之前先用真内核探了四种情况，实测结果是**描述的三条声明全部为真**——默认 −Y、`reversed` +Y、`midplane` 对称、拼错参数被 `_assign_props` 以 `unsupported property 'reveresd' for PartDesign::Pad` 拒绝。也就是说这次没有修掉任何"产品缺陷"，修的是**守卫强度**：把"恰好今天是真话"变成"明天说错就会红"。
* **诚实边界**：本轮验的是 **pad** 这一族（`reversed`/`midplane`/未知键拒绝）；pocket、revolution、groove 的同名参数走同一条 `_assign_props` 通用路径，但没有逐个在真内核上量过——它们由 `test_param_capability.py` 的白名单覆盖到"参数名被允许"，不等于"方向已实测"，这一条留在剩余风险里。
* **验证**：`pytest tests/contract/test_sketch_planes.py tests/contract/test_build_failures.py -q` → **20 passed in 1.74s**；全仓 **874 passed, 4 skipped in 31.75s**（731 单测 + 143 契约，见 §1.2）。

## 29. 第十七轮：工具描述里的 pocket 与"原点绑定"两条声明与实测不符（任务书 §三、§5-B）

### C-29 描述说"落空是安静的 no-op"、"矛盾的原点绑定会报错"——两句都不是真的

* **位置**：`tcad/tools/ir_tools.py::_IR_PATCH_DESCRIPTION`（POCKET DIRECTION 段与约束引用段）；`tests/contract/test_build_failures.py`（+1 项契约测试）。
* **问题**：C-28 把"坐标/方向声明"改成对实测负责之后，同一轮继续逐段核对，发现两句话不是"守卫不够强"，而是**内容错误**：
  1. POCKET DIRECTION 段说落空的 pocket 是"安静的 no-op（quiet no-op）"——那是 §5-B 修好**之前**的旧行为。编译器现在会按名拒绝（`did not change the solid`），描述仍教模型"它会静默通过"，等于把模型往"不必检查"的方向推。
  2. 约束引用段暗示"把一个点绑到原点、又给它写了矛盾坐标"会产生**错误**（原文写的是 "is a solver conflict"；紧随其后的 "Invalid constraint index" 是另一件事）。实测不是错误：求解器把轮廓**拖到原点**，构建照样成功，切除落在模型没写过的地方。
* **复现**（真内核探针 `/tmp/probe_c29.py` 逐条打印，下面数字是它当次印出来的）：
  * 80×50×8 板上、(10,10)…(30,30) 的 20×20 方孔，默认方向 depth 6：**拒绝**——`feature 'ft_cut' (op='pocket') did not change the solid`（400 mm² 完全不在料上）。
  * 同一轮廓加 `"reversed": true`：成功，体积 **29600** = 32000 − 400×6。
  * 同一轮廓**再**加一条与坐标矛盾的原点绑定（`Coincident [0,1,-1,1]`）：**仍然成功**，体积 **28400** = 32000 − 600×6——底面积成了 600 mm²，不是 400。
  * 同一轮廓 `"midplane": true`：成功，体积 **30800** = 32000 − 1200（跨草图面 ±3，只有 3 mm 在料内）。
* **根因**：描述是手写文本；编译器行为改过一次（no-op 检测），Sketcher 语义从来没变过，但两句话都没有跟着核对。第 1 条是文字过期；第 2 条是把"冲突"这个词按日常语义写进了描述，而在 Sketcher 里坐标与约束**是同一类事实、约束优先**，结果不是报错而是**被解算到别处**。
* **修复**：
  1. POCKET DIRECTION 段改为陈述当前行为："编译器拒绝一次不改变实体的切除并按名报出来（`did not change the solid`），所以落空的 pocket 是错误而不是安静的 no-op——但任何切除之后仍然要量（`ir_digest`），因为'体积掉了'不等于'掉的正好是我要的量'"。
  2. 约束引用段改写为："坐标和约束对求解器是同一类事实——它会用移动几何来满足约束。与坐标矛盾的原点绑定不会失败：求解器把轮廓拖到原点，构建通过，切除落在你没写过的地方。只在轮廓真的从原点开始时才绑定原点；否则用 `offset` 放位并不要绑定。任何特征的实测结果与意图不符时，先查这一条。"
  3. 新增 `test_a_contradictory_origin_bind_moves_the_profile_instead_of_failing`，把第 2 条钉成**行为特征**：断言构建成功、体积 ≠ 32000−2400、体积 = 32000−3600。它测的是"陷阱存在且可被量出"，不是"陷阱已被修复"。
* **测试**：`tests/contract/test_build_failures.py` → **8 passed in 0.74s**；全仓 **875 passed, 4 skipped**（731 单测 + 144 契约；本轮两次实测 31.88s / 32.12s，见 §1.2）——这是**该轮结束时的快照**，本轮（§30）又加了两条契约测试，当前基线以 §1.2 为准（该节随轮次更新）。第 1 条没有新增测试：既有的 `test_a_pocket_whose_profile_misses_the_material_fails_loudly` 已经按真内核断言 `did not change the solid`，本条只是把描述文字追上它（"这段文字还在"这一层仍由 `tests/unit/test_ir_tools_description.py` 覆盖）。
* **诚实边界**：
  * 第 2 条**不是产品缺陷**——这是 Sketcher 的语义（约束优先），流水线里没有任何一环做错。所以没有"修代码"，只做了三件事：描述说真话、测试钉住行为、报告（以及测试文档串）说清为什么它与 no-op 检测不冲突：**这一次真的切到了料，只是切在了别处**，`did not change the solid` 正确地没有触发。
  * 缓解手段只有"描述 + 量"：模型按描述不滥用原点绑定，再用 `ir_digest` 读回体积。本轮**没有**（也不打算）让编译器去校验"坐标是否与约束自洽"——那要么得重写坐标、要么会拦掉合法建模，代价与收益不成比例。
  * 探针脚本放在 `/tmp`，不是仓库交付物；报告引用的四个数字全部来自它当次打印，可由任何人重跑（把脚本里的 `REPO` 指到本仓库、用 `.venv/bin/python` 执行即可）。

## 30. 第十八轮：`reversed`/`midplane` 在 revolution 与 groove 上的实测（R-9 收口）

### C-30 两个 op 的换向/居中参数"被白名单允许"，但没人量过它们到底做什么

* **位置**：`tcad/ir/validate.py::_VERIFIED_OP_PARAMS`（revolution/groove 的白名单）；`tcad/worker/compiler.py::_assign_props`（通用参数路径）；`tests/contract/test_revolution.py`、`tests/contract/test_groove.py`（各 +1 项）。
* **问题**：R-9 剩下的那一半。`reversed`/`midplane` 在 `revolution` 与 `groove` 上被校验器接受、由同一条 `_assign_props` 落到 FreeCAD 的 `Reversed`/`Midplane` 属性上，但**没有任何测试量过它们是否真的改变几何**。C-28/C-29 只把 pad 与 pocket 量到底了。一个"静默不生效的旋钮"比没有这个旋钮更糟：模型按描述设了它，构建照样成功，方向却是错的。
* **复现**（真内核探针 `/tmp/probe_c30.py`，`rev`/`grv` 两组各自打印）：
  * **revolution**（阶梯轴轮廓在 XZ，max r=10，angle=90，绕 V_Axis，全周 2720π → 四分之一 680π）：
    * 默认：体积 **2136.2830**（=680π），`y ∈ [0, 10]`（扫向 +Y）；
    * `reversed`：同体积，`y ∈ [−10, 0]`；
    * `midplane`：同体积，`y ∈ [−7.0711, +7.0711]`（±10/√2）；
    * 三者 `z ∈ [0, 40]`、`x ∈ [0, 10]` 均不变——**换向只挪位置，不动高度、不动体积**。
  * **groove**（80×50×8 板 + 嵌入式环槽轮廓 x∈[30,40], z∈[2,6]，angle=90；圆柱做不到这件事，它是旋转对称的）：
    * 默认：体积 **27601.7703** = 32000 − 1400π —— 注意这是探针用的**通厚**轮廓（z∈[0,8]）；定稿测试改用嵌入式环槽（z∈[2,6]），数字相应为 700π，见下；
    * `reversed`：**拒绝**——`feature 'ft_groove' (op='groove') did not change the solid`（扫向 −Y，板上没有料可咬）；
    * `midplane`：**29800.8851** = 32000 − 700π（±45°，只有一半环在料里）。
* **根因**：参数白名单按"编译器设得进去"收录，而"设进去"与"起作用"是两件事；`_assign_props` 是通用路径，任何一个 op 的属性名/语义与 pad 不同，都会表现成"构建成功但方向无关紧要"。这类缺陷不会自己暴露，只会被量出来。
* **修复**（两个 op 各一项契约断言，全部对真内核实测负责）：
  1. `test_the_direction_knobs_move_the_sweep_without_changing_it`（revolution）：三个变体各断言体积 = 680π、`y_min`/`y` 范围与预期一致、`z` 高度恒为 40——换向/居中不许改体积，也不许改高度。
  2. `test_the_sweep_direction_decides_whether_a_groove_bites`（groove）：默认切掉 700π 且实体数仍为 1、包围盒不变；`reversed` 必须按名报 `did not change the solid`；`midplane` 只切 350π。测试文件里新加 `grooved_plate_ir` 构造器，注释写明为什么必须用板而不是圆柱。
* **测试**：`tests/contract/test_revolution.py tests/contract/test_groove.py -q` → **18 passed in 1.97s**；全仓 **877 passed, 4 skipped in 36.17s**（731 单测 + 146 契约，见 §1.2），收尾时（文档改完后）复跑一次 **877 passed, 4 skipped in 34.04s**；README 的计数当时同步为 877 / 146（后续轮次继续递增，当前见 §1.2）。**变异验证**（改真源码跑真内核，`finally` 还原，判定取退出码）：把 `_assign_props` 的 `App::PropertyBool` 分支改成 `pass` → **3 项失败**（pad/pocket/revolution/groove 四族断言里被选中的三族）；改成恒 `False` → **2 项失败**；两次都确认源码已还原（`grep MUTANT` 无命中）。
* **诚实边界**：
  * 本轮修的是**守卫强度**，不是产品缺陷：探针先行的结论是"这些旋钮今天真的起作用"，两个 op 此前只是没人量过——所以产品代码一行未改（`tcad/` 下 0 行改动）。
  * 测的是**方向与体积**，不是"FreeCAD 的 `Reversed` 在一切情况下语义正确"：例如 `angle=360` 时换向在设计上就是等价的（整圈），本轮不覆盖那种无区分度的场景，也不假装覆盖了。
  * groove 的"咬料/落空"结论只在**板上**成立；圆柱上两个方向都会切到料（旋转对称），这正是构造器用板的原因，也写进了测试注释。
  * 参数白名单与"实测方向"仍是两份事实：`test_param_capability.py` 覆盖前者，本节的四项覆盖后者，两者之间没有自动同步机制——新增 op 参数时仍需照 §28/§29/§30 的做法先探针再断言（这条留在 R-6 的语境里，不再单列风险行）。

## 31. 第十九轮：原语的世界坐标放置载体（R-6 收口的那一半）

### C-31 六个原语只能建在原点：模型说"在板上加个销钉"，构建成功、零件却多了一个飘着的实体

* **位置**：`tcad/ir/schema.py`（新增 `PlacementSpec`、`FeatureSpec.placement`）、`tcad/ir/validate.py`（`_PLACEMENT_OPS` + 规则 5e + 有限性检查）、`tcad/worker/compiler.py`（`_apply_placement`）、`tcad/ir/patch.py`（`_require_placement` + `add_feature`/`update_feature` 两条路径）、`tcad/tools/ir_tools.py`（工具描述）、`tcad/ir/capability.py`（六个 op 转正）。
* **问题**：`additive_box`/`additive_cylinder`/`additive_sphere`/`subtractive_*` 自带尺寸、**不需要草图**，可 IR 里除了 `params` 没有任何"放在哪"的字段。于是一个 `x∈[0,80]` 的板加上 `additive_box` 引脚，只可能得到"原点处一个盒子 + 板"——不接触、不合并，`solid_count=2`，Gate 拒绝；而**编译本身是成功的**（`ok=True`、形状有效）。这不是"少了个功能"，是"模型能用它写出一份必然失败的 IR，却看不出为什么"。能力矩阵里这条写着"其余原语固定在原点，IR 没有放置载体"。
* **复现**（真内核探针，`/tmp/probe_c31*.py` 四个脚本；ASCII-only 的第 3 个是在 em dash 触发 `'ascii' codec` 之后重写的）：
  * 先量**载体**：给 `additive_box` 设 `AttachmentOffset = App.Placement(Vector(40, 25, 0), Rotation())` 后，实体仍在 `x ∈ [0, 20]`（**完全不动**）；改设 `obj.Placement` 才移到 `x ∈ [40, 60]`。根因是原语建出来时 `MapMode` 是 `Deactivated`，`AttachmentOffset` 在没有附着的坐标系里不参与定位——**这条要是猜错，就会做出一个"构建成功、重算通过、位置被静默忽略"的特性**（正是任务书 §5-B 反复要求消灭的那类）。
  * 再量**几何**：`additive_cylinder` r6 h20 放在 (10,10,0) → 与 80×50×8 板合并成 **1 个实体**，体积 `32000 + 432π`，z 范围 `[0,20]`；`additive_sphere` r6 放在上表面 (40,25,8) → `32000 + 144π`，z 最高 14；同一圆柱以 axis Y、angle 90 绕自身位置转 → 沿 +X 躺下，z 最高 16、体积 `32000 + 720π − 20·(36·acos(2/6) − 2√32)`（该切口的解析式）；`subtractive_cylinder` r5 h20 放在 (20,25,−6) → 通孔 `32000 − 200π`，包围盒不变；`subtractive_box` 10×10×4 放在 (40,20,4) → 盲槽切掉 **400**；埋入的 `subtractive_sphere` r3 → `32000 − 36π = 31886.90`；同一球放在原点则只挖走一个卦限 **14.14**；不带 placement 的原语仍在原点（对照组）。
  * 最后量**失败**：把 `subtractive_box` 放到板外 → 编译器按名拒绝 `feature 'ft_prim' (op='subtractive_box') did not change the solid`，`kind="compile"`、`feature_id="ft_prim"`——与 §29 的 pocket 同一种失败形态。
* **根因**：PartDesign 原语的定位载体在 FreeCAD 里是**两个不同属性**（`AttachmentOffset` 属于"附件坐标系"，`Placement` 属于对象本身），而 IR 此前一个都没有；差别的可见后果是"静默不动"与"真的移动"。缺字段 → 只能建在原点 → 与已有实体的并集为空 → Compound。
* **修复**：
  1. **类型化载体**。`PlacementSpec(position: Vec3, axis: Vec3 | None, angle: float)`，挂在 `FeatureSpec.placement` 上；`position` 是特征自身原点（盒子从它往 +X/+Y/+Z 长，圆柱/球以它为中心），`axis`+`angle`（度、逆时针、过 `position`）给出旋转。schema 文档串里写明 `AttachmentOffset` 为什么不生效（可复查的实测记录）。
  2. **白名单 + 具名拒绝**。`validate._PLACEMENT_OPS` 只收这六个 op；其余 op 携带 placement → `placement_unused`（**error**），消息里列出允许的 op 并说明"其他 op 的位置来自草图、refs 或 plane"。`angle ≠ 0` 却没有 `axis` → `placement_axis_missing`（FreeCAD 会把它当"没有旋转"）；零轴 → `placement_axis_zero`；`position`/`axis`/`angle` 一律过有限性检查（NaN 会在 Gate 层毒化所有下游比较）。
  3. **编译器落地**。`_apply_placement` 组 `App.Placement(Vector, Rotation(axis, angle))` 赋给 `obj.Placement`；属性不存在时返回**结构化语义错误**（"carries no Placement property"）而不是静默跳过。
  4. **补丁路径不留缺口**（本轮自查时发现的两处）：`add_feature` 是逐键读 payload 的，不点名的键会被**静默丢弃**——模型要求放好的引脚会落在原点而补丁报告成功；`update_feature` 走 `_merge_feature`，原始 dict 会被 `setattr` 进一个类型化字段。两者现在都经 `_require_placement`（非对象、半个 position、非数字 angle 一律 `SCHEMA` 错误并点名 `placement`；`None` 用于把特征送回原点）。
  5. **能力表与工具描述**。六个 op 转正为 `VERIFIED`，`proof` 逐条写清量的是什么；`ir_patch` 描述新增 placement 段（世界 mm、盒子/圆柱/球的 `position` 含义、`axis`+`angle` 的转向、以及"移动已有原语请用 `update_feature` 改 placement，不要重建零件"），并说明"只有原语接受 placement，其他地方给了会被拒"。
* **测试**：
  * `tests/contract/test_primitive_placement.py`（**9 项真内核**，`9 passed in 0.87s`）：上列九个探针场景全部转成断言，体积相对误差 `1e-6`、包围盒绝对误差 `1e-6`，每个用例都用 `plate_with(...)` 显式传 placement（调用点无法漏参）；落空那条断言 `WorkerCallFailed` 的 `kind`/`feature_id`/消息。
  * `tests/unit/test_ir_validate.py`（+5 项）：接受、非原语拒绝（`placement_unused`）、缺轴、零轴、非有限值。
  * `tests/unit/test_patch_type_safety.py`（+8 项）：`add_feature` 的 placement 必须落地为**类型化** `PlacementSpec`、轴+角、`update_feature` 移动（并断言同一特征的其他字段没被带着动）、`None` 清空、三种畸形 payload 按名拒绝且**不是 pydantic 原文**、pad 上给 placement 被语义闸门拒（`placement_unused`）。
  * `tests/unit/test_op_capability.py`：转正后的 15 项 `VERIFIED` 集合与"总数 − 15"计数；测试文档串里记录了这次转正是一次**决定**。
  * **变异验证**（改真源码跑真测试，`finally` 还原，判定取退出码）：M1 把 `obj.Placement = …` 赋值删掉 → **7 项契约失败**（`exit=1`，`7 failed, 2 passed in 0.93s`）；M2 把 `_PLACEMENT_OPS` 清空 → **3 项单测失败**（`exit=1`，`3 failed, 25 passed in 0.03s`）。两次都确认源码已还原（`grep MUTANT` 无命中）。M1 尤其说明这些测试量的是"真的动了"，不是"字段被读到了"。
* **验证**：全仓 `.venv/bin/python -m pytest tests -q` → **900 passed, 4 skipped in 35.40s**（745 单测 + 155 契约；**该轮快照**，当前基线见 §1.2 / §32）。另外单独跑了一次**整链拼接**（`/tmp/verify_c31_e2e.py`，非交付物）：两次 `apply_patch`（先草图+pad，再带 `placement` 的 `additive_cylinder`）→ 经 `IrStore` 落盘再读回（证明 JSON 往返不丢字段）→ 交给真 worker 编译 → `solids=1`、`volume=33357.1680`（= `32000+432π`，`rel_err=0.00e+00`）、z 范围 20。契约测试喂给 worker 的是手搭的 IR dict、单测止步于类型化 `PlacementSpec`，这一跑把两段接上了——即"模型真的按这个写法提交时会得到什么"。
* **诚实边界**：
  * 支持的只是一个**刚体放置**（平移 + 绕单轴一次旋转）。多段变换、倾斜/自定义轴系、按面法向自动摆正都没有字段；`multi_transform` 仍在 `EXPERIMENTAL`。
  * `position` 的语义随 op 而异（盒子的角 vs 圆柱/球的中心），这一点写进了描述与 schema 文档串，但没有做成两套字段——它由 FreeCAD 自己的原语语义决定，另造一套只会与内核不一致。
  * 放置**只影响位置**：任何尺寸变化都必须走 `params`，测试里同时断言"移动之后 height 仍是 4.0"。
  * 六个 op 之外一律拒绝——包含 `hole`（它本来也没有独立测量证据，见 §6）。若将来某个 op 也需要世界坐标位置，正确做法是把它加进 `_PLACEMENT_OPS` 并补真内核证据，而不是放宽校验。
  * 探针脚本在 `/tmp`，不是仓库交付物；本节所有数字都来自它当次打印，可由任何人重跑（`REPO` 指向本仓库、用 `.venv/bin/python` 执行）。

## 32. 第二十轮：验收产物包必须能证明它属于当前源码（任务书 §八"必须交付的文件真实存在且属于同一次构建"）

### C-32 `review/acceptance/` 是**六个轮次之前**的代码建的：数字全对，日期不对

* **位置**：`tools/build_acceptance_artifacts.py`（新增 `source_digest()`、`check_source_tree()`、`--check` 分支、header 打印）、`tests/unit/test_acceptance_bundle_provenance.py`（新增）、`review/acceptance/`（重建）、`README.md` 与本节所属报告（数字同步）。
* **问题**：`review/acceptance/` 上一次构建在 §25–§31 那六个轮次**之前**（放置载体、placement 校验、补丁两条路径、能力表转正、工具描述改写都还没发生）。包里的 108 个产物、`manifest.json` 的逐文件 sha256、样例 A–E 的体积断言**全部仍然自洽**——但没有任何一个字段能告诉读者"这是哪份代码的产物"。§六/§八 要求交付物"真实存在且属于同一次构建"，而当时代码状态只能用 git 定位；本机 `git status` 被 Xcode 许可拒绝（见报告头部），交付包缺少可核对的版本坐标。
* **风险的具体形态**：这不是"文件损坏"，是**证据错位**——一个读者拿着旧包核对 `tcad/ir/schema.py` 的当前内容，会得出"当前代码产出过这份几何"的结论，而实际上产出它的是已经不存在的代码。与 §5-C 判定"导出文件比本次 attempt 还早 → FAIL"是同一条原则，只是这里被判定对象是**整包**。
* **修复**：
  1. **包自带版本坐标**。`source_digest()` 对 `tcad/`+`tools/`+`tests/` 下每个 `*.py`（跳过 `__pycache__`）取 sha256，按 `'<相对路径>\0<sha256>\n'` 排序拼接后整体 sha256，写进 `manifest.json` 的 `source_tree` 段（`roots`/`files`/`sha256`/`method`）。选这三棵树的理由写在函数文档串里：产品、生产者，以及构建脚本会用到的 `tests/` 下的 IR 构造代码。
  2. **一条命令回答"它还新吗"**。`tools/build_acceptance_artifacts.py --check` 重算当前树摘要并比对：`OK:   bundle matches the working tree (153 py files, sha256 df1be3a49d2e5402…)`（退出码 0）／`FAIL: STALE: the bundle was built from sha256 … but the tree is now …; rebuild with tools/build_acceptance_artifacts.py`（退出码 1）。缺清单、清单读不动、没有 `source_tree` 字段（旧格式）都按 FAIL 处理并说明原因，**不会**因为"字段缺失"就默认为新。
  3. **建包流程先算摘要**。构建时在打印 `attempt`/`FreeCADCmd`/`version` 之后立即打印 `source: 153 py files, sha256 …`，让构建日志本身就带上坐标。
  4. **重建**：`attempt: acc-20260920T132149Z-8238f16a`、`source: 153 py files, sha256 df1be3a49d2e5402…`、`wrote 109 files under …/review/acceptance`、样例 A/B/C/D/E 五项 `OK`。
* **测试**：`tests/unit/test_acceptance_bundle_provenance.py`（8 项，不需要 FreeCAD）。用 `importlib.util.spec_from_file_location` 按路径加载构建脚本（它不在包里、也没有 `__init__`），把 `builder.REPO_ROOT` 指到临时假树：覆盖范围（4 个文件都进摘要）、`__pycache__` 不进、**改一行摘要就变**、同一树两次摘要相同、匹配的清单被接受、陈旧清单回 `STALE` 且消息里同时有旧摘要前 16 位、当前摘要前 16 位和 `rebuild`、缺清单/JSON 坏/旧格式（无 `source_tree`）全部拒绝、`source_tree` 是个字符串也拒绝。
* **验证**：
  * **变异验证**（真源码、真命令、`finally` 还原）：`--check` 基线 `(0, 'OK:   bundle matches the working tree (153 py files, sha256 df1be3a49d2e5402…)')` → 在 `tcad/ir/schema.py` 改一行 → `(1, 'FAIL: STALE: … rebuild with tools/build_acceptance_artifacts.py')` → 还原后 sha256 与改前**逐字节一致**（脚本比较确认 `True`）→ `(0, 'OK: …')`。探测器证明自己会响。
  * **独立复算**：另起一个脚本只读 `manifest.json` 与磁盘上的 `.py` 重算摘要 → `MATCH True`（不依赖构建脚本的实现）。
  * **包内容复核**：清单 108 个产物 = 9 `.fcstd` + 14 `.step` + 9 `.stl` + 40 `.json` + 36 `.png`；逐文件 sha256/字节数与磁盘一致；构建先写临时目录、全部样例通过后才 `rmtree` + `copytree`，中途失败不会留下半包（§11 起一直如此）。
  * 全仓 `.venv/bin/python -m pytest tests -q` → **908 passed, 4 skipped in 33.75s**（753 单测 + 155 契约）。
* **诚实边界**：
  * 摘要覆盖 `tcad/`、`tools/`、`tests/` 的 `.py`；`configs/`、`frontend/`（JS/HTML/CSS）、`review/*.md`、`README.md` 与任何非 `.py` 资源**不在其中**——改前端或改报告不会让包变 STALE。选 `tcad/tools/tests` 是因为它们决定几何与数字；把前端纳入只会制造与产物无关的 STALE。
  * `--check` 只回答"源码树是否同一份"，**不**重算产物 sha256（那是构建时的职责，见清单逐文件字段）。它是一条**新鲜度**证据，不是完整校验。
  * 摘要不是签名：它证明"同源/不同源"，不证明"谁构建的"。仓库里没有密钥体系，这一点不做过度承诺。
  * 旧包（`acc-20260919…`）已按 §5-C 的原则被替换，不留"以防万一"的备份在 `review/` 下——留一份不同源的包在旁边正是本轮要消灭的东西。历次 attempt 号保留在报告与清单历史里，可追溯但不可误用。
  * 报告里其它轮次引用的旧 attempt 号（§11、§22、§23）保留原文，它们是**当时**的快照；当前生效的包以 §9 与本节的 `acc-20260920T132149Z-8238f16a` 为准。

## 33. 第二十一轮：把 `draft` 与 `thickness` 从"代码在"变成"量过"

### C-33 能力表说"没量过"的两条 op，量下去发现两种"内核不喊"的失败形态

* **位置**：`tests/contract/test_draft_thickness.py`（新增，12 项真内核）、`tcad/worker/compiler.py`（新增 `_ORIGIN_PLANE_AXIS` / `_face_axis_in` / `_draft_degenerate_errors`，接线在 `_apply_feature`）、`tcad/ir/capability.py`（两条转 VERIFIED）、`tcad/ir/validate.py`（draft 的中性面与面表两条拒绝路径由新用例钉死）、`tcad/tools/ir_tools.py`（工具描述新增 draft/thickness 一段）、`tests/unit/test_ir_validate.py`（+5）、`tests/unit/test_op_capability.py`（VERIFIED 计数 15→17）。
* **问题**：`draft` 与 `thickness` 长期挂在能力表的 `EXPERIMENTAL`（`gap` 一行写的是缺 `NeutralPlane` / `FaceList` 的 LinkSub 载体），编译器里却已经在建 `PartDesign::Draft` / `PartDesign::Thickness` 对象。按任务书 §六"逐项验证并启用"和 §A"不要以创建了某个 FreeCAD 类型就宣称支持该特征"，这就是"枚举↔编译器↔能力表"漂移的另一种形态（主题同 §21 R-7）：**表说没量过，用户拿到的是一个从未被量过的特征**。量下去发现两处"内核不喊"的失败形态，都是真缺陷：
  1. **`draft` 少一个 `NeutralPlane`，内核不报错**。FreeCAD 只打一行 `Failed to add some face for drafting, skip`，返回 **NULL shape**，Body Tip 静默保持倒角前的形状——构建"成功"、交付一个没倒角的零件。与 §25 的"镜像缺 plane 返回 NULL"同一族，校验层因此按名拒绝（`plane_missing`，点名 `plane` 与 `NeutralPlane`）。
  2. **与中性面平行的面根本不可倒角**，内核把整个特征标 `Invalid`——响声够大，但只点名特征、不说哪张面、更不说为什么。实测这四种组合全部 Invalid：±Y 面绕 XZ、四个侧面绕 XZ、顶面绕 XY、底面绕 XY。`_draft_degenerate_errors` 在设完 `Base`/`NeutralPlane` 之后回读这两条引用，把与中性面严格平行（`|dot| > 1 - 1e-12`）的面**按名列出**，并给出"倒与中性面相交的那些面，或换一个它们穿过的平面"。
* **风险的具体形态**：形态 1 是**静默交差**（Gate 看体积/包围盒，零件没倒角但一切"正常"，没有一条断言会红）；形态 2 是**可定位性不足**（失败是真的，但名字指向特征而不是面，模型只能靠猜）。两类都不属于"文件损坏"，属于"失败看起来像成功 / 失败说不清"。选面本身也是风险点：面名（`Face1..FaceN`）是内核给的顺序，测试和模型都必须**按 `ir_digest` 的法向现取**而不是写死。
* **修复**：
  1. **真内核证据 12 项**（`test_draft_thickness.py`，1.80 s，FreeCADCmd 冷启 0.132 s 与之相符）：① 5° 四个侧面绕 XY = 解析棱台 `h/6·(A0+4Am+A1)` = **29282.0083**（1e-6），footprint 不变；② 中性面改用**底面 FaceN**（`kind: "face"`）得同一棱台；③ `reversed=true` 外扩到 **34881.2827**、包围盒 43.5；④ 只点 ±X 两面 = **30600.18**、Y 向包围盒保持 40（把"倒了我列的面"和"全倒了"分开的断言）；⑤⑥⑦ thickness 顶面开口 `value=2` → **8672**、`value=4` → **15616**、`value=2` 开单侧面 → **10112**（`40·40·20 − 38·36·16`）；⑧⑨ FCStd 重开后改 `Value` 2→4 / `Angle` 5→10 按新参数重算（`ft_box` 仍是 `PartDesign::Pad`，未被毁），STEP 回读体积 1e-6；⑩ 面名 `Face99` 按名拒绝并附可用面名；⑪ 无 plane 的 draft 是 `kind="schema"` 的按名拒绝，而不是 NULL；⑫ 与中性面平行的面是 `kind="semantic"` 的按名拒绝。
  2. **前置检查只拒绝严格平行**：`_face_axis_in` 用实测可用的 `getElement(sub).Surface.Axis`（平面面返回世界法向；圆柱面 / 坏名字返回 `None` 交回内核）。原点基准面不能用 `Placement` 推法向——实测 `App::Plane` 三个面的旋转都把 Z 映到 `(0,0,1)`、`App::GeoFeature` 又没有 `Shape`——所以用解析表 `_ORIGIN_PLANE_AXIS`（XY→+Z、XZ→+Y、YZ→+X，与 §19 的 WORLD COORDINATES 契约同一套）；`datum_plane` 中性面不做前置检查（推不出法向就交回内核，那里同样是响的）。
  3. **转正与对齐**：`capability.py` 两条移入 VERIFIED、`proof` 指向本文件并写明量的是什么、`gap` 清空；能力表漂移守卫（`test_op_capability.py`）同步到 17 条；`ir_tools.py` 的工具描述新增一段，把两个载体（面表走 `base_feature` + `sub_elements`，中性面走类型化 `plane`）、"没有 plane 返回 NULL shape"、"与中性面平行的面按名拒绝"、`thickness` 不读 `plane`（给了会被 `plane_unused` 拒），以及 29282.0083 / 34881.2827 / 8672 这几个量过的数字写进去。
  4. **修掉测试自身的一处选面错误**：第一版"只倒 ±X"的断言选错了面，实测体积是 **37599.27**（倒到了顶面+底面，包围盒 z 涨到 26.999）而不是该有的 30600.18。改成 `_side_faces()`——按 ±X / ±Y 四个法向逐个从 `ir_digest` 现取——之后归位。此后所有选面都走 digest，不在测试里写死面名。
* **测试**：`tests/contract/test_draft_thickness.py`（12 项真内核）；`tests/unit/test_ir_validate.py` 新增 5 项（draft 缺 plane、draft 缺面表、thickness 缺面表各自按名拒绝；合法 draft 通过；thickness 带 plane 被 `plane_unused` 拒）。
* **验证**：
  * `.venv/bin/python -m pytest tests/contract/test_draft_thickness.py -q` → **12 passed**（真内核）。
  * 单测 `.venv/bin/python -m pytest tests/unit -q` → **758 passed**；契约 `.venv/bin/python -m pytest tests/contract -q -rs` → **167 passed in 19.80s**；全仓 `.venv/bin/python -m pytest tests -q` → **925 passed, 4 skipped in 36.32s**（数字见 §1.2，本轮实测）。
  * **变异验证**：把 `_draft_degenerate_errors` 的接线从 `_apply_feature` 里拿掉，同一份 IR（±Y 绕 XZ）不再有 `kind="semantic"` 的按名拒绝、退回内核的 `kind="compile"` Invalid（探针实测）——说明这条前置检查是它自己在响，不是别的路径兜住的。
  * 探针另测两组**合法**倒角在检查上线后照常通过：±X 绕 XZ = 34799.6372（包围盒 x 46.999）、顶+底绕 XZ = 37599.2745（包围盒 z 26.999）——前置检查不会把"斜交/垂直中性面"的面误判成平行。
* **诚实边界**：
  * 前置检查只拦**严格平行**；"差一点点不平行"（例如 0.001°）仍交回内核，那里是 `Invalid`（响的），但没有被本轮按名化。
  * `datum_plane` 作中性面时推不出法向，同样只靠内核兜底；FCStd 重开测试只验了 `Angle` / `Value` 两个参数的编辑重算，没有验其它属性（如 `Reversed`）改名后的行为。
  * 12 项只在 40×40×20 长方体这一族形状上量过；`thickness` 的"开单侧面"解析式（`38·36·16`）是这一族几何的解析值，不是通用公式。
  * 能力的"实现"维度仍是 FreeCAD 的 `PartDesign::Draft` / `PartDesign::Thickness` 原样行为，本轮只是把它变得**可量、可拒、可重算**；不承诺任意复杂形状上的倒角/抽壳都能建。

---

## 34. 第二十二轮：把"守卫装了但没接线"的一批找出来（本会话）

日期：2026-09-23 · 分支 `main` · 基线提交 `00db836` · 工作目录 `/Users/slg/workspace/text_to_cad`
**本会话全部修改均未提交、未推送**（§4 的保留约定不变）。基线重跑：`925 passed, 4 skipped`；本轮结束时 `998 passed, 4 skipped`。

### 34.0 这一轮在找什么

前二十一轮修的是**已知缺陷**。这一轮换了个问法：**哪些"守卫"其实装了但没接线？**——即代码里有检查、有 hook、有字段、有文档，但那条路径上的返回值/测量值/配置从来没有人消费。这个问法命中率很高，因为"写了检查"和"检查会响"在代码里长得几乎一样。下面 13 条都是这么找出来的，每条都先复现、再修、再补回归，并做了**变异验证**（把修复临时还原，确认测试真的变红）。

### 34.1 C-34 `PRE_TURN` / `PRE_STEP` 的守卫返回值被丢掉（任务书 §5-D「DENY/ASK 必须生效」）

* **位置**：`tcad/loop/engine.py:279`（旧）、`:367`（旧）
* **事实**：两处都是 `self._dispatch(HookEvent.PRE_TURN, {...})` —— **调用表达式的返回值没有任何接收者**。`_dispatch` 返回 `HookResult`，于是 `DENY` 与 `ALLOW` 在效果上完全一样：策略刚刚拒绝了这个回合，回合照跑到底。
* **复现**：`/tmp` 下临时脚本对一个只返回 `DENY` 的 hook 跑 `run_turn`，回合正常完成 2 次 LLM 调用、工具照常写入。回归测试：`tests/unit/test_loop_engine.py::test_a_denied_pre_turn_never_reaches_the_model` / `::test_a_denied_pre_step_never_reaches_the_model`。
* **根因**：`PRE_COMMIT` 至少消费了 `DENY`，`PRE_TOOL_USE` 消费了 `DENY`/`ASK`，这两处漏了——**同一份契约在四个 hook 点上只落实了两个**。
* **修复**：新增 `LoopEngine._halt_on_hook(where, result, turn)`，把"停不停"和"为什么停"分开：`DENY → FAILED`（策略拒绝，无进一步工作），`ASK → AWAITING_APPROVAL`（要人决策，此后**没有任何写入发生**）。`_drive` 的循环条件是 `while turn.state == RUNNING`，所以终态是**结构性**停机：模型一次都不会被调用。`POST_TURN` 仍然照发（一次被拒绝的回合也是一次回合）。
* **测试**：4 项单测（含 `ASK` 不得执行任何写入、`POST_TURN` 必须仍然触发）。

### 34.2 C-35 `PRE_TOOL_USE` 的 `ASK` 不阻止同批后续调用（任务书 §5-D「ASK 不能继续执行后续写操作」）

* **位置**：`tcad/loop/engine.py` 的工具批循环（旧 `:500-524`）
* **事实**：`ASK` 分支只做两件事——`turn.state = AWAITING_APPROVAL`、给**本次**调用返回"待审批"——然后 `for tc in reply.tool_calls` 继续下一轮迭代。模型一步里提六个编辑、第三个撞上 `ASK`，**第四到第六个照常执行**。批准提示因此是最糟的一种：它看起来生效了，而它想拦的那些写已经落地了。
* **复现**：回归 `tests/unit/test_loop_engine.py::test_an_ask_stops_the_rest_of_the_same_tool_batch`（一条回复里两个 `ir_patch`，第一个 `ASK`）——修复前 `store.applied` 长度为 1。
* **修复**：`halt_after_this` 标志 + `break`。**被跳过的调用仍然各拿一条 `tool` 消息**（写明 "NOT executed"）：OpenAI 线格式要求每个 `tool_call_id` 都有应答，否则被挂起后恢复的对话会被 provider 判为协议错误，那会把"等审批"伪装成"协议故障"，同时把"这些调用根本没跑"这件事藏起来。观察者帧也照发一条 `ok=false`，前端才不会以为它们成功。
* **测试**：1 项（断言 `store.applied == []` 且轨迹里出现 "NOT executed"）。

### 34.3 C-36 `PRE_COMMIT` 的 `ASK` 直接穿透到构建（任务书 §5-D）

* **位置**：`tcad/loop/commit.py:279`（旧）
* **事实**：`if pre.decision == HookDecision.DENY: return ...` —— 只判 `DENY`。`ASK` 落到下一行，于是 compile → export → Gate → **发布**。而"提交"就是那个写操作：等有人来看的时候，产物、导出的 STEP 和验收结论都已经存在了。
* **复现**：回归 `tests/unit/test_commit_hook_gates.py::test_a_pre_commit_ask_does_not_build`——`_ExplodingWorker` 在任何 worker 调用上抛异常；修复前它会被调到。
* **修复**：`ASK` 与 `DENY` 同样早退，错误文案说清"没有编译、没有写盘、Gate 没有运行，批准后再提交"，并带 `hint="this is a suspension, not a build failure"`——这两种停法的排障路径完全不同。
* **测试**：3 项（`ASK` 不建、`DENY` 仍拒绝、`ALLOW` 不得被误当成拒绝）。

### 34.4 C-37 复用引擎时预算跨回合累加（任务书 §5-D）

* **位置**：`tcad/loop/engine.py:201`（`__init__`）与 `run_turn` 的重置块（`:_272-276`）
* **事实**：`self.budget = Budget(budget_limits)` 只在构造时建一次；`run_turn` 重置了 `_idle_steps` / `_compile_failures` / `_last_gate_report` / `_last_commit_passed` / `_candidate_reports`，**没有重置 `budget`**。注释明确写着引擎会被复用（CLI REPL 就是这么用的）。
* **后果**：默认档（五项全 `null`）下数字只是装饰；但加载 `configs/policies/strict.yaml` 后，**第 2 个回合从第 1 个回合的余量开始**——一个自己从未超限的普通回合会被判 `EXHAUSTED`，且 `turn.steps` 报的是会话累计值而不是本回合步数。
* **复现**：回归 `tests/unit/test_loop_engine.py::test_the_budget_is_per_turn_even_when_the_engine_is_reused`（第一轮烧完 3 步预算，第二轮必须拿到自己的预算）。修复前第二轮立刻 `EXHAUSTED`、`llm.calls == 0`；修复后第二轮 `steps <= 2` 且 `SUCCEEDED`。
* **修复**：`run_turn` 里每轮重建 `Budget`。**没有动** `turn_wall_clock_s` 的单次请求活性语义——工作量上限与单次调用超时是两件事（§R-5 已写明，此处不重新混淆）。
* **测试**：1 项。

### 34.5 C-38 四个 geo 工具在 async 处理器里同步调 worker（任务书 §5-D）

* **位置**：`tcad/tools/geo_tools.py` 的 `geo_view_handler` / `geo_measure_handler` / `asset_export_handler` / `asset_import_handler`
* **事实**：四个都是 `async def`，却直接 `services.worker.request(...)`，而按协议 `Worker.request` 是**同步**的（`tcad/tools/base.py`）。一次 tessellate 或 STEP 导入期间，整个 asyncio 循环停摆：其它会话的 SSE 断流、健康检查不响应、被取消的回合拿不到取消点。**与 §5 C-7 给 commit 修的完全同型，只是这四个被漏掉了**——`tests/unit/test_commit_event_loop.py` 只盯着 commit，所以那一轮"修好了"并不覆盖这里。
* **复现**：新增 `tests/unit/test_geo_tools_event_loop.py`（4 个参数化用例 + 1 个错误契约用例）。用一个刻意慢 0.4 s 的假 worker 跑心跳：修复前心跳 **0** 次，修复后 ≥15 次。
* **修复**：新增 `_ask_worker()`（`asyncio.to_thread` + `functools.partial`），四处调用点统一改走它。
* **诚实边界**：线程里的调用仍然不能中途取消（与 R-8 同一边界）；线程池是默认的 `min(32, cpu+4)`，因此同时进行的 geo 调用数隐含该上限。
* **测试**：5 项（含"移出事件循环不得改变错误契约"）。

### 34.6 C-39 `add_feature` 静默丢弃 `base_feature` / `sub_elements` / `plane`（任务书 §5-A）

* **位置**：`tcad/ir/patch.py:_op_add_feature`
* **事实**：`FeatureSpec(...)` 只传 `id/name/op/profile_sketch/params/refs/suppress/placement`，payload 里这三个**类型化字段从未被读取**（pydantic 默认忽略多余键，所以既不报错也不落地）。后果有两层：
  1. `fillet` / `chamfer` / `draft` / `thickness` / `mirrored` **无法用一条 `add_feature` 建成**——`validate_ir` 必然报 `sub_elements_missing` / `plane_missing`；
  2. 而那条报错说"请设置 `base_feature` 与 `sub_elements`"——**模型刚刚设置过**。于是模型照着做，第二次仍然被拒，第三次……这一条与 §24/§25/§33 三处"已解决"的结论直接冲突：载体字段确实存在、`update_feature` 确实能用，**只有 add 路径漏了**，而工具描述教的是 add。
* **复现**：`/tmp` 脚本 `apply_patch(add_feature fillet + base_feature + sub_elements)` → 修复前 `PatchError [sub_elements_missing]`；修复后 `base_feature='ft_pad' sub_elements=['Edge1','Edge2']`。回归 `tests/unit/test_patch_type_safety.py::test_add_feature_now_carries_the_reference_fields`。
* **修复**：`_op_add_feature` 改为构造必填字段后调用**同一个** `_merge_feature`（`update_feature` 用的那个）。一份映射两处使用，两者不可能再漂移——并加了一条断言两者键集合一致的测试。
* **顺带**：`_merge_feature` 补上 `plane`（显式 `PlaneRef` 构造，不再靠 `setattr` 裸 dict + 文末整体重解析兜底）、`sub_elements`/`refs` 的 `_require_str_list`（见下条）。

### 34.7 C-40 未知 payload 键被静默忽略（任务书 §5-A「给模型的 JSON schema 必须在服务端真正执行」）

* **位置**：`tcad/ir/patch.py`（新增 `_PAYLOAD_FIELDS` / `_reject_unknown_payload_keys`）
* **事实**：`ir_patch` 的 `payload` 被声明成一个**无类型对象**（`{"type":"object"}`，只有一句描述），而各 handler 只读自己认识的键、**其余全部静默丢弃**。实测四种形态、修复前**全部 ACCEPTED**：
  * `{"op":"pad","parms":{"length":10}}` → 一个 `params` 为空的 pad（拼错了 `params`）；
  * `{"op":"pad","profile":"sk1"}` → `profile_sketch` 为空的 pad；
  * `{"op":"pad","foo":"bar"}` → 静默吞掉；
  * `{"op":"pad","refs":"ft_pad"}` → 走 `list(v)` 变成 `['f','t','_','p','a','d']`。
  产出的 IR **类型合法、`validate_ir` 通过**，但**不是模型要的东西**；失败要等到编译期、且报的是"没有轮廓"，不是"你拼错了哪个键"。
* **复现**：回归 `tests/unit/test_patch_type_safety.py`（新增 8 项，含"每个 op 都适用"与"近名纠错"）。`refs` 那一条与 §17 C-15 是同一类缺陷，只是 C-15 只修了 `refs` 一个键，**没有把规则推广到其它键**。
* **修复**：
  1. `_PAYLOAD_FIELDS`：**从 `FeatureSpec.model_fields` / `SketchSpec.model_fields` 派生**（加显式的 `*_append`），而不是手写清单——手写清单会在有人给 spec 加字段的那天开始拒绝合法载荷，那正是白名单被删掉的经典原因；有一条测试钉住"新字段必须立即被接受"。
  2. `_reject_unknown_payload_keys`：未知键 → `kind=SCHEMA`，消息给出**允许集合**与 `difflib` 近名提示（`'parms' (did you mean 'params'?)`），并说明"忽略未知键会建出你没要的东西"。
  3. `_require_str_list`：`refs` / `sub_elements` 传字符串按名拒绝。
  4. 无参/少参也能被挡住：`{"op":"pad"}`（既无 `profile_sketch` 也无 `params`）现在仍是"类型合法但不可建"——**这一条没有修**，见 §34.15 的剩余风险。
* **顺带发现的真缺陷**：`payload` 里 `{"op":"pocket","reversed":true}`（**顶层**写 `reversed`）过去被静默忽略——而工具描述里"落空的 pocket 是静默 no-op"那段读起来正是这个意思。`reversed` 是 **`params` 键**（描述第 180 行写对了、第 308 行没写明落在哪一层）。现在顶层 `reversed` 被按名拒绝，描述那一段也改成明确写出 `params` 层并给出完整例子。

### 34.8 C-41 二阶检查把标量测量值交给 pydantic：已确认需求反而把构建判死（任务书 §5-C 第 7 点）

* **位置**：`tcad/verify/checks_spec.py:SpecCheck.run`（旧 `:94/102/111/118`）
* **事实**：`CheckResult.measurements` 是 `dict[str, float|str|bool]`、`expected` 是 `dict[str,float]|None`。一阶检查（`checks_solid`）的 `_r` 会经 `_as_dict` 归一（§5 C-4 修的），**二阶直接构造 `CheckResult`，完全没走归一**。而 `specexpr.evaluate` 返回的形态按 kind 而异：`count`/`feature_count`/`wall_thickness` 是**裸标量**，`hole_position` 是含三元列表的 dict，`symmetric` 是 `{"counterpart": <id>}`。实测修复前：
  * `count` / `feature_count` / `symmetric` / `wall_thickness` → **抛 `ValidationError`**（Gate 记成 ERROR + pydantic 转储，判决丢失）；
  * `hole_position` / `hole_diameter` → 值被字符串化进报告。
* **后果（这是本轮影响面最大的一条）**：`update_requirement` 的工具描述**明确要求**模型把用户给的每个数字记成 confirmed 约束，而样例 A/B/C 的第一句要求都是"期望单一有效实体"——**记下来 = Gate 报 ERROR = 回合不可能成功**。也就是说，照描述做反而害了模型。
* **复现**：回归 `tests/unit/test_verify_checks.py` 新增 6 项（4 个标量 kind 参数化 + `symmetric` + "Gate 永远看不到校验错误"）。
* **修复**：`SpecCheck` 新增 `_result()`，把 `measurements`/`expected` **统一走一阶同一个 `_as_dict`**（`expected` 用 `floats_only=True`，非数值直接丢弃而不是让 pydantic 抛）。一条检查，两处使用，形态差异不再泄漏到结果类型。

### 34.9 C-42 已确认的壁厚需求从来没有被验证过（任务书 §5-C 第 8/9 点）

* **位置**：`tcad/verify/checks_solid.py:WallThicknessCheck`、`tcad/verify/checks_spec.py:FIRST_TIER_OWNED`、`tcad/worker/introspect.py`
* **事实**：三件事叠在一起，把"壁厚"这一项从验收里整个抹掉了：
  1. `wall_thickness` 被 `FIRST_TIER_OWNED` 归属一阶，所以二阶**故意不建**这张检查（避免重复计一次失败）；
  2. 而一阶那张检查是 `ADVISORY + APPROXIMATE`，比的是**配置默认值** `wall_thickness_min_mm`（默认 1.0），**从不读** `ir.requirements`；
  3. 它要的 `digest.key_dimensions["min_wall_thickness"]` —— **`tcad/` 里没有任何代码写过它**（`grep` 只命中测试 fixture）。
  所以一个 confirmed 的"壁厚 3mm"进入需求合同后，**没有任何一条检查会看它**，且 `WallThicknessCheck` 在生产路径上永远只能 SKIP。这与任务书"已确认的受支持要求必须参与验收；壁厚需求不能被默认的 advisory 最小壁厚提示替代"逐字冲突。
* **修复（真的去量，而不是改口径）**：
  1. **worker 侧实现测量** `_measure_min_wall_thickness`：对每一对法向大致相对的面取最近距离，取其中点**在材料内**的最小值。方法选择有理由：OCC 的 `IntCurvesFace` 求交器**不在本构建的绑定里**（探针实测 `ModuleNotFoundError`），第一版射线法因此什么都量不到；`distToShape` + `isInside` 是公开 `Part` API。真内核实测：80×50×8 板 → **8.0**；Ø30/Ø10 圆筒 → **10.0**（= 15−5）；带 Ø8 通孔的板 → **8.0**（孔不被误当壁）。只在真正量到时才写进 `key_dimensions`（缺失 → 上层报 `required_but_unverified`，而不是伪造 0.0 被读成一个不合格的壁）。
  2. **检查改由需求驱动**：有 confirmed 的 `wall_thickness` 时，用**用户的值与容差**判、且 `BLOCKING`；量不到 → `required_but_unverified`（阻断 ERROR，不是 SKIP）；没有需求时保持原来的 advisory 语义（shop default，不改变"全绿=完成"）。
* **测试**：`tests/contract/test_wall_thickness.py`（6 项真内核，含"圆筒的壁不是它的外径"这种退化解守卫 + "要 9mm 而只有 8mm 必须 FAIL"）；`tests/unit/test_verify_checks.py` 4 项（需求驱动、不达标、量不到不许 SKIP、无需求保持 advisory）。
* **顺带修好的两处"证据落后于事实"**：`tests/contract/test_chat_end_to_end.py` 与 `tests/contract/test_e2e_pipeline.py` 各有一条断言写着"`wall_thickness` 预期会 SKIP，因为 worker 不算它"——那是旧限制。改成断言**它必须真的跑出判决**（且无需求时严重度仍是 advisory），而不是把断言删掉。
* **诚实边界**：这是**面配对法的下界**——只在两张面之间成立的厚度场（例如同一张曲面上的两点）量不到；面数超过 64 时返回"未测量"而不是猜或挂住。

### 34.10 C-43 草图 `offset` 不定位轮廓，还会把它**变形**（任务书 §5-B）

* **位置**：`tcad/tools/ir_tools.py`（描述）、`tcad/ir/validate.py`
* **事实**：描述里的**主打定位配方**是"把轮廓建在草图自己的原点附近（绑一条线到草图原点），再用 `offset` 摆放"。真内核实测该配方无效且有害——草图坐标是**世界坐标**（`_sketch_point` 把每个点过一遍草图 `Placement` 的逆），`offset` 是同一个 `Placement` 的一部分，于是两者**精确抵消**；而一旦轮廓被绑到草图原点，抵消之后求解器又把轮廓拖走，结果是**同一个包围盒、不同的实体**：

  | offset | 包围盒 | 体积 |
  | --- | --- | --- |
  | (0, 0, 0) | 40×20×5 | **4000** |
  | (10, 10, 0) | 40×20×5 | **2500** |
  | (5, −3, 0) | 40×23×5 | **4050** |

  同一份 IR、同样成功的构建、没有一条错误——只有用户恰好写了体积/包围盒需求时才有机会被发现。
* **复现**：`tests/contract/test_sketch_planes.py::test_the_offset_that_used_to_be_recommended_deforms_the_part`（真内核，把 4000 与 2500 都写进断言里）。
* **修复**：
  1. **IR 层按名拒绝非零 `offset`**（`sketch_offset_unsupported`，`error`），消息带实测数字与替代做法（写坐标；不要把轮廓绑到原点；要锚定就按边绝对标注）。全零的 `offset` 不视为违规。
  2. **描述改写**：删掉那条配方，改为"坐标就是位置"；并说明真正的过约束来源是"同一条线的两个端点既绝对标注、又把其中一个绑到原点"——两句话说了两件事，求解器用移动几何的方式同时满足它们。
  3. **三个 demo 脚本改写**（`tools/sessions/{bracket_step3,demo_bracket,long_run}.json`）：它们用的正是这条被拒配方。改成世界坐标定位、去掉原点绑定、显式声明该草图不做全约束要求（少一个平移锚点）。**用脚本里的真实载荷配一块 80×50×8 底板编译验证**：体积 **25600** = 32000 − 40×20×8，槽确实落在 (20,15)–(60,35) 并贯穿；旧配方给出的是一个变形且位置错误的槽。
  4. **新增守卫** `tests/unit/test_session_scripts_are_applicable.py`：shipped 脚本不得再出现 `offset` 键——不只是非零的，"写成范例"本身就是教学。
* **诚实边界**：`offset` 沿平面法向的分量在 XY 上实测是**有效**的（`offset z=5` 把平面移到 z=5、形状不变），但 XZ/YZ 上实测不对（体积 3500/3750）；本轮选择统一拒绝，因为"部分方向有效"正是最难被模型正确使用的形态。已记入 R-10。

### 34.11 C-44 `raw_python` 的沙箱由模型自己选，且服务端策略是死配置（任务书 §5-E）

* **位置**：`tcad/tools/privileged.py`
* **事实**：三处叠在一起：
  1. `use_sandbox = bool(args.get("sandbox", False))` —— **模型自己决定是否沙箱，默认否**。于是默认情形下模型拿到的是一个**完全无沙箱**的子进程，而"要沙箱"的唯一途径是它自己开口。这与任务书"模型不能传 `sandbox=false` 绕过"是同一句话的反面。
  2. `Config.sandbox`（`enabled` / `backend` / `no_network` / `read_only_roots` / `writable_root`）在 `privileged.py` 里**一次都没被读过**（全仓 `grep cfg.sandbox` 只命中 loader 的路径解析与一个 config 测试）。
  3. 沙箱 profile 是硬编码常量，**缺 `(version 1)`** —— `sandbox-exec` 会直接拒绝整份 profile（`no version specified`，退出码 65）。也就是说被标为"gate (3)"的沙箱路径**从来没有真正跑过**。
* **复现**：`printf '(deny default)...'` 交给 `sandbox-exec` → 退出码 65；加上 `(version 1)` 后语法被接受（本机"Operation not permitted"来自外层沙箱不允许嵌套 `sandbox_apply`，属环境条件）。回归 `tests/unit/test_tools_privileged.py`（新增 6 项）。
* **修复**：
  1. `_sandbox_decision(services)` 只读**服务端** `config.sandbox`；**未配置**一律按"要沙箱"处理（严格方向必须是操作者选择的结果，不能是"没人配"的默认）；`backend="bwrap"` **失败关闭**（未实现，绝不以"未沙箱"代替）。
  2. payload 里出现 `sandbox` 键 → `kind=SCHEMA` 按名拒绝（不是忽略：被忽略的参数仍然是模型以为自己控制的东西）；同时把它从声明的 schema 里删掉并加 `additionalProperties: false`，两层都挡。
  3. profile 由配置生成（`writable_root` / `no_network`），并补上 `(version 1)`。
  4. **纠正被夸大的 docstring**：原文写"read-only roots, no network, low-priv user"，实际 profile 是"读全放开、写限 writable_root、可选禁网、无降权"。改成逐条写实，并把 `read_only_roots` 未落实列为已知缺口（R-10）。
* **诚实边界**：`sandbox-exec` 在本机无法端到端验证（外层沙箱禁止嵌套）→ 只验证到"profile 语法被接受"与"策略决策正确"，**不声称沙箱已端到端跑通**。

### 34.12 C-45 `/render` 的 `style` 参数未校验，可越出版本目录读写（任务书 §5-E）

* **位置**：`tcad/server/app.py:render_view`
* **事实**：`view` 有白名单、`width/height` 有范围校验，`style` 什么都没有——而它被直接拼进缓存文件名 `view-{view}-{style}-{w}x{h}.png`。`style=../../../../escaped` 解析到**版本目录之外**；那个路径同时是"缓存命中时被 `FileResponse` 送出的文件"和"`produced.replace()` 把渲染结果移过去的**写入目标**"。整个端点是无鉴权 GET。
* **复现**：回归 `tests/unit/test_server_settings.py::test_render_rejects_a_traversal_shaped_style`——不只断言 400，还断言版本目录外**没有出现任何文件**（只断言状态码证明不了边界守住了）。
* **修复**：`_ALLOWED_STYLES = frozenset(get_args(RenderStyle))` —— **从 `RenderStyle` 字面量派生**，加一条"渲染器能画的每种风格都必须被接受"的测试，两边不可能漂移。
* **诚实边界**：默认只监听回环（`tools/serve.py:24` 已是 `127.0.0.1`），所以真实可达性受限于"谁能访问本机端口"；但 §5-E 明确要求校验解析后的目录边界，与暴露面无关。

### 34.13 C-46 诊断脚本里写死的能力计数（言过其实的反面）

* **位置**：`tools/doctor.py:178`
* **事实**：`api_selftest scope` 一行写死 `"instantiation only — 3 ops are kernel-verified (pad, pocket, additive_box)"`。能力表早已是 **17/21** 通过真内核验证（§2/§6/§13/§22/§24/§25/§31/§33 累计）。一个写死的数字随表漂移，而且方向是**低估**——排障的人会以为只有三个特征可用。
* **复现**：`tools/doctor.py` 输出 vs `tcad.ir.capability.ops_by_tier()`。
* **修复**：改为从 `ops_by_tier()` 现算，并点名剩下 4 个。现在打印：`17/21 ops are kernel-verified; the rest are circular_pattern, datum_plane, hole, multi_transform`。

### 34.14 C-47 第 3 层 e2e 的预检在撒谎（任务书 §七）

* **位置**：`tests/e2e/test_real_llm_sample_a.py`（旧 `_provider_reachable`）
* **事实**：预检只打 `GET /models`，**200 即认为可用**。而本机配置的 provider 恰好是那条反例：**`/models` 返回 200，每一次 `/chat/completions` 返回 402 Insufficient Balance**。于是四条第 3 层用例照跑，然后以四条关于**模型行为**的断言失败（`state=failed`、没有产物、没有工具调用）——**没有一条指出真正的原因**。
* **复现**：把磁盘上已配置的 provider 送进 `TCAD_E2E_*` 跑 `pytest tests/e2e`：修复前 4 failed；修复后 4 skipped，跳过原因是
  `the configured model service cannot serve a completion: HTTP 402 — the credential is valid but the account cannot pay for a request: Insufficient Balance. Environment BLOCKED, not a pass.`
* **修复**：新增 `tests/e2e/provider_probe.py`：**打一次最小 completion**（`max_tokens=1`）并按状态码分类（401 凭据被拒 / 402 凭据有效但账户无法付费 / 404 没有这个模型 / 429 限流 / 其它拒绝 / 不可达），每条都写明 "Environment BLOCKED, not a pass"。同时支持 `data/settings.json` 作为 env 之后的兜底，使 UI 里已配好的 provider 不必再手写一遍环境变量（密钥只透传、不打印）。回归 `tests/unit/test_e2e_provider_probe.py`（9 项，全确定性、不联网，含"必须打 completion 而不是 model list"的反漂移守卫）。

### 34.15 C-48 诊断脚本的三处失真（同一类：诊断读错了事实源）

* **位置**：`tools/doctor.py`（三处）、`tcad/llm/hotswap.py:probe_llm`（文档）
* **事实**：
  1. `api_selftest scope` 写死 `"3 ops are kernel-verified"`（实际 **17/21**，见 §34.13）——方向是**低估**，排障的人会以为只有三个特征可用。
  2. `privileged_ops` 读的是 `cfg.security.privileged` —— **`security` 段根本不存在**（开关在 `tools.privileged` + `policy.allow_privileged`）。`getattr(None, ...)` 恒为空，于是无论操作者怎么配，这一行永远打印"未注册"。
  3. provider 段只读 YAML（`cfg.llm`），**不看 `data/settings.json`** —— 那是 UI 保存的、优先级更高的快照。本机因此长期报 `http://127.0.0.1:8000/v1`（YAML 里的占位 vLLM）与 `HTTP 502`，而真实配置的 DeepSeek 端点根本没被读到。修好后它打印 `settings_from: settings.json (saved in the UI)` + 真实模型列表。
  4. 而**结论行**（`real-LLM e2e RUNNABLE`）是按 `/models` 是否 200 判的 —— 与 §34.14 修掉的 e2e 预检是同一个错误，只是长在另一个文件里：同一台机器上 doctor 说 RUNNABLE、e2e 说 BLOCKED。
  5. 顺带纠正 `probe_llm` 的 docstring：原文写"A provider that answers /models but cannot complete is still reported as failing the chat fallback"，而代码在 `models.list` 成功时**早退**，那条兜底根本不会跑。改成如实说明"这一步只证明端点会答、设置能解析；能不能服务是另一回事"。
* **修复**：三处事实源各自改对；结论行改为**打一次最小 completion 并分类**（`_can_complete`，与 e2e 预检同一套分类）。现在两者一致：
  `WARN provider:can_complete  cannot serve a completion: HTTP 402 — credential valid but the account cannot pay for a request: Insufficient Balance. Layer 3 will report BLOCKED, not a product failure`
* **注**：本条与前一条同属一个反模式——**诊断本身要有守卫**。一个会把 BLOCKED 报成 RUNNABLE 的 doctor，比没有 doctor 更费时间。

### 34.16 第 3 层：先 BLOCKED，随后解封并**全绿**（含一条由此暴露的测试缺陷）

**先说结论**：本会话第 3 层从「4 skipped」变成「**4 passed**」。1007 passed / **0 skipped**。

**过程**：
1. 会话开始时 provider 余额为 0 → `HTTP 402`，4 条稳定 skip（原因见下面的 §34.16b，写得可核验，不是"没配置"）。
2. 用户报告界面上的一条报错（见 §34.17），说明 provider 此刻已经能服务。复测确认：密钥有效、端点可达、**可以服务**。
3. 于是第 3 层第一次真正跑起来——**3 条失败**。而这 3 条失败与模型毫无关系，全是**测试自己对接口返回结构的假设过期**：

| 测试的假设 | 接口的实际契约 | 前端在消费哪个 |
| --- | --- | --- |
| `GET /approvals` 返回**裸列表**，空即 falsy | `{"pending": [...]}` | `const { pending } = await api("/approvals")` |
| `GET /models/{id}/artifacts` 返回**裸列表**，元素是 `{name: ...}` | `{model_id, version, dir, files, verdict}` | `(body.files \|\| []).filter(...)` |

两条断言因此得到 `['dir','files','model_id',...]` 和 `{'pending': []}`（**非空 dict，恒真**），报出"交付清单里没有 .step"。**接口是对的，测试是错的**——已经按真实契约修好。

**值得单独记下的教训**：一个长期 BLOCKED 的测试就是**一个从未被验证过的测试**。它的接口假设会随接口一起漂移，而漂移在"跳过"的绿色里完全看不出来；一旦环境恢复，它报出的是自己的过期，不是产品的问题——**这和"空检查报 PASS"是同一枚硬币的另一面**：跳过不是通过，也从不是"以后再验也一样"。

**第 3 层现在的四项（真机、真模型、真内核）**：
1. 回合 `SUCCEEDED`，且通过它的是 **Gate**（不是模型自称）；
2. `.step` / `.stl` / `.FCStd` 真实交付且可下载、非空；
3. **把交付的 STEP 重新导入真内核，体积恰为 25600 mm³**（rel 1e-6）；
4. 模型确实发起了工具调用（不是"碰巧一次说对"）。

命令：`TCAD_E2E_*`（或 `data/settings.json` 里已保存的 provider）→ `.venv/bin/python -m pytest tests/e2e -q`。

### 34.16b 第 3 层解封前的那份 BLOCKED 记录（保留，作为可核验的历史）


| 项 | 状态 | 可核验证据 |
| --- | --- | --- |
| 第 3 层「真实 LLM 端到端」 | **BLOCKED（环境）** | 密钥存在且有效、端点可达（`GET /models` 200，0.38 s）；**账户余额为 0** → 每次 completion 返回 `HTTP 402 {"message":"Insufficient Balance"}`。`pytest tests/e2e -q -rs` 打印的正是这一句 |
| 视觉检查类验收 | **未做** | 只有真正发送图像内容的调用才算视觉检查。第 3 层现在会跑，但它**不发送任何图像**（模型看不到预览图），因此仍然**不声称任何模型做过视觉检查** |

当时的解锁条件与命令（保留原文）：

```bash
TCAD_E2E_BASE_URL=https://api.deepseek.com/v1 TCAD_E2E_MODEL=<model> TCAD_E2E_API_KEY=<key> \
  .venv/bin/python -m pytest tests/e2e -q -rs
```

（`TCAD_E2E_*` 未设置时会回退到 `data/settings.json` 里已保存的 provider；当前正是它给出了上面那条 402。）

### 34.17 C-49 参数被截断时被静默变成空参数：模型被告知"你漏了 base_version"（用户实测报障）

* **位置**：`tcad/llm/client.py:OpenAIClient._parse`（旧 `:160-163`）、`tcad/loop/engine.py`（工具批循环）
* **报障原话**（用户在界面上看到的、本会话直接修掉的那一条）：
  ```
  ir_patch  失败 · schema
  [schema] ir_patch: arguments do not match the tool schema:
      missing required property 'base_version'; missing required property 'ops'
  ```
  伴随的模型旁白是"I'll design a phone stand … Let me start by creating the side-profile sketch and padding it."
* **根因**：`_parse` 里
  ```python
  try:
      args = json.loads(tc.function.arguments or "{}")
  except json.JSONDecodeError:
      args = {}          # ← 截断被吞掉
  ```
  模型正在写一个很大的 `ir_patch`（手机支架侧轮廓 + 约束 + 需求），**JSON 被每步 token 上限截断**，客户端的 `JSONDecodeError` 被换成 `{}`，于是**下一个环节**（给模型的 JSON schema 真正执行，§34.7）看到的是一个空对象，只能如实报告"缺这两个必填键"。
* **复现（真机，逐 token 预算逼近截断窗口）**：`max_tokens=1200` 时 provider 返回 `finish_reason="length"`，`arguments` 是 **1359 字符的非法 JSON**，结尾停在 `"direction": [0.0`。而 `LlmReply.finish_reason` **当场就有**，却没有任何人读它。
* **后果链**：模型被告知一件**假事**（它漏了键——它其实正写到一半），于是最合理的反应是**把同样大的 patch 再发一次**，撞同一堵墙。这正是用户看到的现象。
* **修复**：
  1. `ToolCall` 新增 `args_error`（解析失败的原因）与 `args_raw_len`（provider 实际发了多少字符）。`args == {}` 保持不变——**活下来是对的**，但"为什么"必须跟着一起活下来。
  2. 空字符串**不算错**（无必填参数的工具合法地取 `{}`）；合法 JSON 但类型是数组/标量要单独记（同样不是"缺键"）。
  3. `engine._step` 在派发**之前**短路：不把它当工具调用（不进 hook、不进 handler），直接回一条按**原因**给处方的错误——`finish_reason == "length"` 时说"你的输出撞上了每步 token 上限，JSON 被截断；**把 patch 拆小**，先草图、再特征、再需求"；否则说"重新发合法 JSON"。两种原因给两种处方，因为给错处方要多花一整个回合。
  4. 既有测试 `test_parse_survives_malformed_tool_arguments` 只断言 `args == {}`（"活下来"），**把它补成也断言原因被记下**——它当时把"静默"固定成了期望行为。
* **验证（真机、端到端、两条）**：
  * **自然跑通**：用用户的实际设置（`max_tokens_per_step: 4096`）请求"创建一个手机支架：楔形主体，前部有个托住手机的挡边" → `succeeded`，5 步，Gate `passed`，产出 `part-phone.{FCStd,step,stl}` + `manifest.json`。
  * **强制截断**（`max_tokens_per_step: 1200`，同一句话换成样例 A）→ 8 步 `succeeded`，轨迹为：
    `ir_patch`(FAIL，「…not usable, so NOTHING was applied — Unterminated string at character 2017 of 2021 characters」) → `ir_patch`(OK，模型照提示改小了) → `ir_patch`(FAIL，这次是它自己真的少写了 `base_version`) → `ir_patch`×3 OK → `ir_commit` OK。
    **修复前第一步会报"missing required property 'base_version'"，而模型手上没有任何线索**。
* **诚实边界**：仍在"每步 token 上限"下工作——修的是**可诊断性与自纠**，不是消除上限。要提高一次成型的概率应当调大 `max_tokens_per_step`，或（更好）让模型从描述里就知道"大零件要分几次 patch"；这条已写进 §34.19 的 R-19。
* **测试**：`tests/unit/test_llm_client.py`（+3 项：截断必须记录原因 / 空参数不算错 / 合法非对象 JSON）、`tests/unit/test_loop_engine.py`（+3 项：截断必须点名 token 上限且**不得**出现 "missing required property" / 非法 JSON 必须要求重发合法 JSON 且**不得**甩锅给预算 / 这种调用不得进 hook 与 handler）。

### 34.18 本轮测试与运行命令

```bash
.venv/bin/python -m pytest tests -q                # 1007 passed, 0 skipped in 163.79s  ← 最终实测
.venv/bin/python -m pytest tests/unit -q           # 缺 FreeCAD 也能跑（基线 758 → 787）
.venv/bin/python -m pytest tests/contract -q       # 真跑 FreeCADCmd
.venv/bin/python -m pytest tests/e2e -q            # 4 passed（真模型 + 真内核，23.86s）见 §34.16
.venv/bin/python tools/doctor.py                   # 退出码 0 = 可运行
.venv/bin/python tools/build_acceptance_artifacts.py --check   # OK，与本轮源码同源
```

数字的两次变化：`925 passed, 4 skipped`（会话开工）→ `998 passed, 4 skipped`（13 条修复完成）→ **`1007 passed, 0 skipped`**（第 3 层解封、4 条 e2e 真跑通过）。

`.venv/bin/python tools/build_acceptance_artifacts.py --check   # OK: bundle matches (160 py files, sha256 aaec828e2f17aad8)

本轮新增测试文件：`tests/unit/test_geo_tools_event_loop.py`、`tests/unit/test_commit_hook_gates.py`、`tests/unit/test_e2e_provider_probe.py`、`tests/unit/test_session_scripts_are_applicable.py`、`tests/e2e/provider_probe.py`、`tests/contract/test_wall_thickness.py`；扩充：`test_loop_engine.py`、`test_verify_checks.py`、`test_patch_type_safety.py`、`test_tools_privileged.py`、`test_server_settings.py`、`test_sketch_planes.py`。

**变异验证**（把每处修复临时还原，确认对应测试真的变红，源码随后还原）：8/8 全部 CAUGHT —— 预算重置、`_halt_on_hook` 恒定放行、`ASK` 不 break、二阶 `_as_dict` 归一、壁厚需求不读 requirements、`pre_commit` 的 `ASK` 分支、`_ask_worker` 退回同步调用、`/render` 的 style 守卫。

### 34.19 验收产物（§八"必须真实存在且属于同一次构建"）

上一轮交付的 `review/acceptance/` 本轮开头被 `--check` 判为 **STALE**（由 `df1be3a4…` 建、当时源码树是 `827cb0bd…`）——这正是该检查存在的意义。已用本轮源码重建：

```
attempt_id : acc-20260923T144323Z-2585065a
source_tree: 160 py files, sha256 aaec828e2f17aad8…
disk       : 109 files (108 产物 + manifest.json)
samples    : A 25600 / B 32000−288π → 32000−512π（跨 worker 重启）/ C 8000π → 10000π
             / D revolution 2720π → 1360π / E groove 8500π → 8750π
--check    : OK: bundle matches the working tree
```

### 34.20 尚未修复 / 剩余风险（本轮新增，按危害排序）

| 编号 | 位置 | 事实 | 后果 |
| --- | --- | --- | --- |
| R-10 | `tcad/tools/privileged.py`、`tcad/config/schema.py:200` | `sandbox.read_only_roots` 仍然只是配置项，profile 里读是全放开的；Linux `bwrap` 未实现（现在会**失败关闭**而不是无沙箱运行）；没有降权。`sandbox-exec` 在本机无法端到端验证（外层沙箱禁止嵌套 `sandbox_apply`） | "沙箱已生效"目前能证明的是**语法被接受 + 策略决策正确**，不是"恶意代码被关住"。生产启用 `allow_privileged` 前必须在一个能跑 `sandbox-exec` 的环境里实测一次 |
| R-11 | `tcad/ir/validate.py` | `FeatureSpec.params` 只查顶层有限性；嵌套 dict/list 里的 NaN/Inf 不查（同一文件已有递归版本 `_check_value_finite`，只用在 requirement 的 `value` 上） | 嵌套非有限数会在编译期以别的方式失败，报错位置离原因较远 |
| R-12 | `tcad/ir/patch.py:_op_add_feature` | `{"op":"pad"}`（缺 `profile_sketch` 与 `params`）仍是"类型合法但不可建"——`validate_ir` 不要求 profile/params 的存在性 | 本轮修掉了"拼错的键被吞掉"，没有修"什么都不写也被接受"。建议下一步给每个 op 加必填 params 的最小集断言 |
| R-13 | `tcad/ir/schema.py:247`（`ConstraintKind`）、`tcad/verify/specexpr.py` | 没有任何 requirement kind 能表达"孔的数量/深度/是否贯穿"：`_measure_holes` 实测了 `through`/`depth`，但只有 `diameter`/`position` 参与判定；`count` 指的是实体数 | 盲孔被建成通孔时 Gate 发现不了；§5-B 第 6 点要求的"孔数量/深度/贯穿校验"**仍不完整** |
| R-14 | `tcad/verify/specexpr.py:216` | `hole_diameter`/`hole_position` 的 `target` 支持写**名字**（`_matched_holes` 用 `id or name`），但随后定位 nominal 轴只用 `find_feature`（仅 id） | 用名字引用孔的正确零件会得到 `required_but_unverified` 而阻断。修法很小（`_nominal_axis` 也接受名字），本轮未做 |
| R-15 | `tcad/worker/compiler.py:527`（`_obj_name`） | IR id 经 `re.sub(r"\W","_",id)` 后不保证首字符合法、不去重；FreeCAD 会把非法 Name 改名（`1pad` → `_1pad`） | 以数字开头的 IR id 会让"重启后继续修改"（reopen 按名找对象）失效，而编译期完全看不出来 |
| R-16 | `tcad/ir/patch.py`、`tcad/ir/validate.py` | `add_sketch`/`add_feature` 不校验 **name** 唯一性，`validate_ir` 也只查 id；而 `rename` 专门拒绝重名 | 多轮编辑赖以为生的稳定句柄失去唯一性，"把 plate 加厚"变成有歧义的指令，两条路径规则不一致 |
| R-17 | `tcad/worker/reopen.py:229` | 约束编辑对**所有**约束类型都按 `mm` 施加 datum，角度约束也一样（实测 30° 被写成 1718.87，且 `ok=True, errors=[]`） | FCStd 重开后按参数改**角度**会静默写错值 |
| R-19 | `tcad/llm/client.py` 的上游（模型侧） | 每步 token 上限下，**大的 `ir_patch` 必然可能被截断**。§34.17 修的是"截断要能被诊断、模型能自纠"，不是消除上限：一次成型仍受 `max_tokens_per_step` 约束 | 复杂零件要多花几步；工具描述里应明确"大零件请分多次 patch"（尚未写） |
| R-18 | `tools/sessions/*.json` | 三个 demo 脚本本轮改成了世界坐标，但那个 sketch 显式设了 `require_fully_constrained: false`（去掉了原点绑定后少一个平移锚点） | demo 不再是"全约束"范例；一个正确的"把位于任意位置的轮廓完全约束"配方仍没有被固化成文档/示例（本轮验证失败的尝试：给同一条边的两端都加绝对标注会与 H/V 冲突） |

### 34.21 本轮变更文件

生产代码：`tcad/ir/patch.py`、`tcad/ir/validate.py`、`tcad/loop/engine.py`、`tcad/loop/commit.py`、`tcad/verify/checks_spec.py`、`tcad/verify/checks_solid.py`、`tcad/worker/introspect.py`、`tcad/tools/geo_tools.py`、`tcad/tools/privileged.py`、`tcad/tools/ir_tools.py`、`tcad/server/app.py`、`tcad/config/providers.py`（仅注释）；工具：`tools/doctor.py`（4 处）、`tools/sessions/{bracket_step3,demo_bracket,long_run}.json`；`tcad/llm/hotswap.py`（仅 docstring）；测试：见 §34.16；产物：`review/acceptance/**`（重建）。

