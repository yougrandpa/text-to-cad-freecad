# text_to_cad 项目长期笔记

> 详细缺陷史见 `review/AUDIT_AND_FIXES_ZH.md`（已到 §34）。本文件只留**改代码前必须先知道的**。
> 最近一次整理：2026-09-23（压缩去重，并从 §34 补入新教训）。

## 仓库 / 网络 / 环境
- 远端 `https://github.com/yougrandpa/text-to-cad-freecad.git`，主分支 `main`；当前 HEAD `00db836`。
- `free-cad/` 是 FreeCAD 源码依赖，已在 `.gitignore`，**不要提交**。
- **无法 push GitHub**（代理不转发 `receive-pack` 的响应流；HTTP/2→408，HTTP/1.1→curl 52）。需要换代理或加 SSH key。
- 执行沙箱禁止写 OS 临时目录 → pytest 必须 `--basetemp=.pytest_tmp`（已在 pyproject）。**该目录每次运行被清空。**
- macOS BSD `grep` 不支持 `\|` 交替 → 用 Grep 工具或 `grep -E`。无 `timeout` 命令。
- `curl` 访问 localhost 会被环境代理劫持 → 加 `--noproxy '*'`。
- npm 走代理会 ECONNRESET → `--registry=https://registry.npmmirror.com` 直连。

## 项目定位
- 目标：**聊天式生成 CAD 模型**的 Agent Harness（包名 `tcad`）。FreeCAD（26.3.0dev，已构建）作几何内核，**不改内核**。
- 设计文档 `docs/02-架构设计.md`；需求问卷 `docs/01-需求澄清问卷.md`。

## 核心架构不变量（改代码前先读）
1. **几何内核与语义真相分离**：模型只改 IR（特征 DAG + 参数 + 稳定命名），不碰 FreeCAD document；FreeCAD 是「编译器后端」。
2. **成功定义唯一**：Gate 全绿 = 完成。模型声称完成不是终止条件。
3. **校验走独立读路径**（CQRS）：Gate 重新从磁盘加载 IR 快照 + 重读产物。
4. **单写入口**：IR 只经 `ir_patch`/`ir_commit` 变更；事件流 append-only 是真相源，先写事件再写快照。
5. **Hook fail-closed**：Hook 异常/超时一律 DENY。
6. **无头优先**：一切能力必须在 `FreeCADCmd` 下可用。
7. **守卫必须接线**（§34 的教训，最重要的一条）：写了检查 ≠ 检查会响。`_dispatch(...)` 的返回值有没有被消费、测量值有没有被写进 digest、配置项有没有被读、错误信息是不是在叫模型做它已经做过的事——这四问能找出大部分残留缺陷。**静默无效比报错危险。**
8. **诊断必须与它报告的事实同源**：写死的计数、读错的配置段、只验连通的探针、"跳过"的测试——
   这四样都会让绿色变成假象。**跳过不是通过。**

## FreeCAD API 陷阱
- `shape.BoundBox` **存在** → 轴对齐校验用它。`optimalBoundingBox()` 是**可旋转 OBB**，比轴对齐规格会偏小。
- `shape.check()` 成功返回 `None`、失败抛 `ValueError` → 必须 try/except，**不能写 `if shape.check():`**。
- `TopoShape.tessellate(tolerance)` **必须传参**。`SketchObject.solve()` 返回 SolveStatus，**不是 DOF** → DOF 读 `sketch.DoF`。
- **无头出不了 PNG** → worker 回传网格，supervisor 软件光栅化。
- 不存在：`Part.checkGeometry`、`shape.isSolid`、`shape.Vertices`（是 `Vertexes`）。
- `setDatum()` 在约束冲突时抛 `ValueError: Invalid constraint index: N`（信息与真实原因无关）。
- **`sandbox-exec` 的 profile 必须以 `(version 1)` 开头**，否则退出码 65「no version specified」，子进程一行代码都不跑。
- **OCC `IntCurvesFace` 求交器不在本构建的绑定里**（`ModuleNotFoundError`）→ 做射线/厚度分析请用 `distToShape` + `Solid.isInside` 等公开 `Part` API。
- 源码行号清单见 `docs/02` 附录 A/B；**附录 B（运行时实测）效力高于附录 A（源树阅读）**。

## 构建 / 运行
- FreeCAD 构建：`cd free-cad/FreeCAD && pixi run configure && pixi run build` → `build/debug/bin/FreeCADCmd`。
- worker：`FreeCADCmd -c --console -P <repo_root> tcad/worker/bootstrap.py --pass --worker-id=wN`
  （**`--pass` 必须在脚本自身参数之前**）。跑独立脚本：`FreeCADCmd /path/to/script.py`。
- 渲染只在 supervisor 侧（numpy + Pillow），不往 FreeCAD 进程塞第三方依赖。

## 唯一入口与常用命令
- **唯一生产装配入口**：`tcad.core.wiring.build_services(config)`；测试用 `start_worker=False`。
- 全量测试：`.venv/bin/python -m pytest tests -q`（**当前 1007 passed, 0 skipped**，约 164 s —— 4 条真机 e2e 在跑真模型）
  `tests/unit`（787，不需 FreeCAD）/ `tests/contract`（真跑 FreeCADCmd）/ `tests/e2e`（需真模型，见下）
- 环境诊断：`.venv/bin/python tools/doctor.py`（退出码 0 = 可运行；会打印 17/21 ops kernel-verified）
- 验收产物：`tools/build_acceptance_artifacts.py [--check]` → `review/acceptance/`
- CLI：`.venv/bin/python -m tcad.server.cli --data-dir <dir> new|chat|repl|approvals|approve`
- **Web 界面**：`tools/serve.py --data-dir <dir> --port 8765` → `http://127.0.0.1:8765/ui/`
  （命令行 `--provider/--model/--base-url` 只作用于内存，不覆盖 UI 保存的设置）
- **离线模型替身**：`tools/stub_llm.py --script tools/sessions/demo_bracket.json --port 8123`
- **「操作者即 LLM」驱动器**：`python -m tools.agent_driver --list-tools --full` / `--calls <stepN.json>` / `--worker-probe`

## 第 3 层（真实 LLM e2e）：已解封，4 passed
- provider 在 `data/settings.json`（DeepSeek）。**曾经**余额为 0 → 每次 completion `HTTP 402 Insufficient Balance`；
  现已恢复，第 3 层真跑通过（真模型 + 真内核，~24 s，把交付的 STEP 回读体积 25600 mm³）。
- `TCAD_E2E_*` 未设置时会回退到 `data/settings.json`；探针是 `tests/e2e/provider_probe.probe_provider`（打一次最小 completion，不是 `GET /models`）。
- **不要因为没有 key 就说做不到**——先跑探针看真实原因。
- ⚠️ **长期 BLOCKED 的测试 = 从未被验证过的测试**：解封时 4 条里有 3 条失败，且全是**测试自己**对接口形状的过期假设
  （`GET /approvals` 是 `{"pending":[…]}` 不是裸列表；`/artifacts` 是 `{…, "files":[…]}` 不是裸列表）。
  接口是对的、测试是错的。跳过不是通过，也从不是"以后再验也一样"。

## LLM 客户端：参数截断必须能被诊断（C-49，用户实测报障）
- **`ToolCall.args_error` / `args_raw_len` 是必须的**：`args == {}` 同时表示两种相反的情况——
  「模型发了个空调用」（它自己的错）与「JSON 被每步 token 上限截断」（chunk 太小）。二者处方相反。
- `_parse` 曾经 `except JSONDecodeError: args = {}` **把原因扔掉**，于是截断被下游报成
  "missing required property 'base_version'"——**假诊断**，模型唯一的合理反应是把同样大的 patch 再发一次。
- 真机证据：`max_tokens=1200` → `finish_reason="length"`，`arguments` 是 1359 字符非法 JSON（结尾 `"direction": [0.0`）。
  **`finish_reason` 一直在响应里，只是没人读。**
- `engine._step` 在派发**之前**短路（不进 hook、不进 handler），按原因给处方：`length` → 把 patch 拆小；否则 → 重发合法 JSON。
- 空字符串**不算错**（无必填参数的工具合法取 `{}`）；合法 JSON 但非对象单独记。
- **改 `_parse` 时别退回静默**：`tests/unit/test_llm_client.py` 与 `test_loop_engine.py` 各有守卫，
  其中一条明确断言错误文案里**不得**出现 "missing required property"。

## 模型配置（改之前先读）
- **`HotSwapLlm`**：`services.llm` 永远返回同一对象，换模型只替换内部 `_client`，不重建 services、不重启 FreeCAD。
- 设置优先级 `settings.json`（UI 写的完整快照）> YAML/环境变量。落盘 0600，**回传一律掩码**。
- `api_key` 三角语义：字段缺失=保留；`null`=清除；字符串=设置（靠 `model_fields_set` 区分）。
- 切换 provider 会**重置继承字段**（`model`/`base_url`/`api_key_env`/`context_window`）。
- `base_url` 为空必须拒绝（OpenAI SDK 会静默回退到 `api.openai.com`）。
- provider 预设只给**候选**模型名；真实列表由 `GET /settings/models` 代调 provider 的 `GET /models`。**不要硬编码**。
- 预设里 `default_model` 可能与端点实际不符（本机 `deepseek-v4-flash` 已不在列表，现为 `deepseek-flash`/`deepseek-v4-pro`）→ 以运行时列表为准。
- provider 预设 `use_env_proxy` 默认全 False（本机 `HTTPS_PROXY` 曾把直连失败伪装成「厂商挂了」）。

## 模型协议（真机验证过，别改坏）
- **DeepSeek 思考模式要求 `reasoning_content` 原样回传**：assistant 消息带 tool_calls 被回放时必须带上它，否则 400。
  链路：`LlmReply.reasoning_content` ← `_parse` → engine 构造 assistant 消息时**有则回显、无则不加键**。
- 一轮 = 多步循环 ⇒ 每步都在回放上一步 ⇒ 该缺陷**必然在第 2 步爆**，且任何思考模型都会。
- **这类协议要求只有真机能发现**（stub 不产生 reasoning）：离线全绿 ≠ 真机可用。

## 校验层（Gate）的诚实性——别退回
- `bbox_spec`/`mass_spec` 在无对应需求时返回 **SKIP 而不是 PASS**。空检查报 PASS 比报 FAIL 危险得多。
- 全 blocking SKIP 仍 = 不通过（fail-closed 未动）。
- **`update_requirement`（confirmed=true）是 Gate 唯一的判据来源**；用户给的尺寸/数量/位置必须记下来。
- **`CheckResult` 只接受标量**（`measurements: dict[str,float|str|bool]`，`expected: dict[str,float]|None`）。
  `specexpr.evaluate` 返回的形态按 kind 而异（裸标量 / 含列表的 dict / `{"counterpart":…}`）
  → **任何构造 `CheckResult` 的地方都必须过 `checks_solid._as_dict`**（一阶 `_r` 与二阶 `SpecCheck._result` 都已这么做；新加检查别绕过去，否则 confirmed 需求会把构建判成 pydantic ERROR）。
- **`wall_thickness` 是需求驱动的**：有 confirmed 需求时用**用户的值与容差**判且 BLOCKING，量不到报 `required_but_unverified`；没有需求时才退回配置默认值 + ADVISORY。
  worker 的 `_measure_min_wall_thickness` 是面配对下界（`distToShape` + `isInside`），只在真量到时写 `key_dimensions`。

## IR / ir_patch（模型可见的契约，改它要跑测试）
- **加字段的正确姿势**：patch 层的 payload 白名单 `_PAYLOAD_FIELDS` **从 `model_fields` 派生**——手写清单会在加字段那天开始拒绝合法载荷。
- **`add_*` 与 `update_*` 必须共用一份字段映射**（`_merge_feature`/`_merge_sketch`）。曾经 `add_feature` 静默丢弃 `base_feature`/`sub_elements`/`plane`，于是 fillet/chamfer/draft/thickness/mirrored 一条 add 建不出来，报错还叫模型去设置它刚设置过的字段。
- **未知 payload 键一律按名拒绝**，带允许集合 + difflib 近名提示（`'parms' (did you mean 'params'?)`）。静默忽略 = 产出「类型合法但不是要的东西」。
- `refs`/`sub_elements` 传字符串必须拒绝（`list("Edge1")` → 5 个不存在的名字）。
- **`reversed` / `midplane` 是 `params` 键**（不是 payload 顶层）；顶层写会被按名拒绝。
- op 必须逐个列全，**不能用通配**；`tests/unit/test_ir_tools_description.py` 拿描述与 `FeatureOp` 对账。
- 同一 body 内特征只在**几何相交**时合并；`refs` 只声明构建顺序、不产生几何关系。

## 草图坐标（真机报障后补，改编译器前必读）
- **草图坐标是世界坐标**：XY→(x,y)、XZ→(x,z)、YZ→(y,z)。编译器用 `sk.Placement.inverse()` 把世界点映射进局部，且在加几何**之前**要 `doc.recompute()`。
- **`offset` 不定位轮廓，还会变形**（真内核实测：40×20 矩形 pad 5，offset (0,0,0)→4000、(10,10,0)→**2500**（同包围盒）、(5,−3,0)→4050/包围盒 40×23）。原因：坐标已是世界坐标，offset 是同一个 Placement 的一部分 → **精确抵消**；若轮廓又绑到草图原点，求解器再把点拖走。
  → IR 层现在**按名拒绝非零 offset**（`sketch_offset_unsupported`）。**要把轮廓放在别处，就写那个坐标。**
- **原点绑定与坐标是同一件事的两句话**：`{"Coincident","refs":[i,pt,-1,1]}` 把点绑到草图原点；若坐标写的不是原点，求解器用**移动几何**来同时满足两者——构建成功、切除落在没写过的地方。
- `pad` 挤出方向随平面法向：XY→+Z、**XZ→−Y**、YZ→+X。
- **`Sketcher.Constraint` 对无法识别的形状是 segfault 不是异常** → 编译器只构造实测过形状，其余抛 `_BadConstraint`。
- **值写进构造函数会跳过 FreeCAD 的冗余校验**：能用 refs-only 的（`DistanceX/Y[g,p]`、`Distance[g,p]`、`Angle[g1,g2]`）一律 refs-only + `setDatum`；只有 refs-only 会崩的 `Radius`/`Diameter` 才把值放进构造函数。
- 构建顺序按依赖解析（不是「先所有草图再所有特征」）；依赖环 → `kind=semantic` 报错。
- **编译失败绝不能没有原因**：`_build` 在 recompute 后专门诊断「为什么没有实体」。
- worker 崩溃/卡死必须替换进程（`WorkerHandle.recover()`，受 `runtime.worker_restart_on_crash` 控制）。
- 回归：`tests/contract/test_sketch_planes.py`（含 offset 实测）、`tests/unit/test_worker_error_detail.py`。

## 熔断上限：可选，默认不设（用户指令，别改回硬上限）
- **`None` = 不设上限**，五项（`max_steps_per_turn`/`max_tokens_per_turn`/`step_timeout_s`/`turn_wall_clock_s`/`max_compile_retries`）在 `configs/default.yaml` 里**全部 null**。用 `None` 而非 `0`/大数。
- **预算按回合**：`run_turn` 每轮重建 `Budget`（曾经不重建 → 复用引擎时第 2 轮从第 1 轮余量开始，strict 档下被误判 EXHAUSTED）。
- 有界档 = `configs/policies/strict.yaml`，必须把五项**全部**限住。
- **别把「工作量上限」和「单次请求活性」混为一谈**：取消前者不影响后者（`llm.request_timeout_s`、`ToolSpec.timeout_s`、worker 传输超时都还在）。
- 无上限状态必须可见：`GET /health` 的 `budget.{limits,unbounded,unbounded_all}` + 顶栏徽标。
- **客户端断开必须终止 Turn**：`/chat` 的 SSE 生成器在 `finally` 里 `task.cancel()`；**`yield start` 必须在 `try` 之内**，否则 `finally` 不执行、任务泄漏。

## Hooks：四个 PRE_* 点都必须**消费**返回值（§34 的教训）
- `PRE_TURN`/`PRE_STEP` 走 `Engine._halt_on_hook`：`DENY → FAILED`、`ASK → AWAITING_APPROVAL`，**都是结构性停机**（`_drive` 循环条件是 `while state == RUNNING`，模型一次都不会被调用）。
- `PRE_TOOL_USE` 的 `ASK` 会**中止同批剩余调用**；被跳过的调用仍各拿一条 `tool` 消息（写 "NOT executed"），否则恢复后的线格式不合法、且「它们没跑」这件事被藏起来。
- `PRE_COMMIT` 的 `ASK` 与 `DENY` 同样早退（提交就是那个写操作，等有人来看时产物已经存在）。
- `POST_TURN` 永远触发（包括被拒绝的回合）。

## 前后端契约（改界面前先读）
- **SSE 帧里的枚举是「序列化值」不是「枚举名」**：`"succeeded"`/`"exhausted"`/`"pass"`/`"blocking"`。用枚举名**永远匹配不到**。
- engine 的 observer 帧里 `images[].path` 是**磁盘绝对路径**；server 侧必须经 `artifact_url_for()` 转 URL。
- **不要用 `StoreAdapter.current_version()` 判断模型是否存在**（对它而言「不存在」与「v0」都返回 0）→ 用 `load()`；未知模型必须 404 不是 500。
- **`/render` 的 `view` 和 `style` 都必须过白名单**（`style` 曾未校验 → 拼进缓存文件名可穿越读/写）。`_ALLOWED_STYLES` 从 `RenderStyle` 派生。

## 会话 / 打断（改前端前先读）
- **一个会话 = 一次对话 + 它唯一在造的零件**；切换会话必须同时换四件东西（对话流/视图/产物/检查器 + URL `?thread=`）。
- **`abort()` 只停网络，不停 JavaScript**：被切走的回合其回调/catch/finally 都会继续跑 → 用**身份对象** `state.turn`，所有回调以 `state.turn === turn` 为前置条件。
- **`POST /sessions` 复用已存在的 `model_id` 必须 409**（`IrStore.create()` 会重写 `v0.json`）。
- **列表读取失败 ≠ 列表为空**：必须渲染「读取失败 + 重试」。失败不许长得像正常状态（与 Gate「空检查不许报 PASS」同一条纪律）。
- `ABORTED` 是**第五种终态、来自人**：文案必须说清「是谁停的 + 此后没有任何东西经过 Gate 验证」。
- 中断三件套：`stop_requested` 谓词（唯一判定依据）+ `task.cancel()`（真的打断在飞的 LLM 请求）+ **只有谓词为真时 `CancelledError` 才解释成 ABORTED**。
- `request_id` 由**客户端在发请求之前铸造**；不认识的 id 记为「待停止」（有界 64、30s 过期）；同一 id 起第二个回合 → 409。
- **限制（未做）**：跨回合只延续 IR 几何，**不延续对话历史**。

## 特权工具（raw_python）
- **默认不注册**（`build_default_registry(enable_privileged=False)`），三重门：`policy.allow_privileged` + 未过期审批（绑定工具+参数摘要+会话+TTL）+ `policy.sandbox_probe` 健康。
- **沙箱由服务端 `config.sandbox` 决定，模型不能选**：payload 里的 `sandbox` 键会被按名拒绝（schema 也已 `additionalProperties:false`）。
- 未配置 sandbox → **按要沙箱处理**；`backend="bwrap"`（未实现）→ **失败关闭**。
- profile 由配置生成并**必须以 `(version 1)` 开头**；实际只保证「写限 writable_root / 可选禁网」，**读全放开、无降权**（`read_only_roots` 未落实，R-10）。
- `sandbox-exec` 在本机**无法端到端验证**（外层沙箱禁止嵌套）→ 只验证到语法与策略决策。

## 操作纪律（踩过的坑）
- **不要在用户正在使用的端口上起演示/验证服务**（曾用 8765 跑验证，用户看到的界面是 stub 的配置，误以为「配置没保存」）。验证换 8766+。
- 多实例并存时用 `GET /health` 的 `data_dir` 分辨。用户报「功能 X 没用」时先确认他连的是哪个实例/数据目录。
- **验收产物会 STALE**：改了源码就要重建 `review/acceptance/`，`--check` 会告诉你。
- 改修复前先备份、改完做**变异验证**（还原修复 → 确认测试变红 → 还原源码）。本轮 8/8 全中；这能区分「真守卫」和「恰好也绿」。
