# text_to_cad 项目长期笔记

## 仓库信息
- 远端：https://github.com/yougrandpa/text-to-cad-freecad.git，主分支 `main`。
- `free-cad/` 为 FreeCAD 本地源码依赖，已在 `.gitignore` 中忽略，**不要提交**（近 8000 文件）。

## 网络 / 推送约定
- 当前环境默认代理（`127.0.0.1:50683`）无法访问 GitHub（502 CONNECT tunnel failed），直连超时。
- 推送 GitHub 必须走本机代理 `127.0.0.1:7890`：
  ```bash
  HTTPS_PROXY=http://127.0.0.1:7890 HTTP_PROXY=http://127.0.0.1:7890 \
  git -c http.proxy=http://127.0.0.1:7890 -c http.version=HTTP/2 push origin main
  ```
- git 凭据使用 `osxkeychain`。
- **`api.deepseek.com` 在本机直连可用**（实测 0.2s 返回 401，即端点通、只差 key），走代理也通 → 配 DeepSeek 时 `use_env_proxy` 勾不勾都行。
- **npm 走代理会 ECONNRESET**（TLS 握手被掐断，与 git push 同源）→ 用 `--registry=https://registry.npmmirror.com` 直连。
- **curl 访问 localhost 会被环境代理劫持**（`upstream connect failed`）→ 加 `--noproxy '*'`。

## 项目定位
- 目标：**聊天式生成 CAD 模型**的 Agent Harness（包名 `tcad`）。FreeCAD 全量源码（26.3.0dev）作几何内核与 Python API 引擎，**不改内核**。
- 设计文档：`docs/02-架构设计.md`（可直接编码）；需求澄清问卷：`docs/01-需求澄清问卷.md`。
- 设计方法：H=(E,T,C,S,L,V)+P 描述性框架；用户已授权由 AI 自行决策（全部决策标注「默认假设，可推翻」）。

## 核心架构不变量（改代码前先读）
1. **几何内核与语义真相分离**：模型永不直接读写 FreeCAD document，只改 IR（特征 DAG + 参数 + 稳定命名）；FreeCAD 降级为「编译器后端」。这是全项目最有区分度的决策，T/S/V 三层形态由它决定。
2. **成功定义唯一**：Gate 全绿 = 完成。模型声称完成不构成终止条件。
3. **校验走独立读路径**（CQRS）：Gate 重新从磁盘加载 IR 快照 + 重新读导出产物，不碰生成路径的内存对象。
4. **单写入口**：IR 只能经 `ir_patch`/`ir_commit` 变更；事件流 append-only 是单一真相源，先写事件再写快照。
5. **Hook fail-closed**：Hook 异常/超时一律 DENY，永不 fail-open。
6. **无头优先**：一切能力必须在 `FreeCADCmd` 下可用；依赖 GUI 的能力（真视口渲染）只能是可选增强。

## FreeCAD API 陷阱（写 worker 代码前必看）
- `shape.BoundBox` **存在**（`XMin/XMax/.../XLength/YLength/ZLength`）→ 轴对齐校验用它。`optimalBoundingBox()` 是**可旋转 OBB**，拿它比轴对齐规格会偏小。
  （早期文档曾断言「无 BoundBox 属性」，是**读源码得出的错误结论**，已被运行时实测推翻。以本节为准。）
- `shape.check()` 实现是成功返回 `None`/失败抛 `ValueError`（与 `.pyi` 的 `-> bool` 不符）→ 必须 try/except，**不能写 `if shape.check():`**。
- `TopoShape.tessellate(tolerance)` **必须传参**；无参调 TypeError（`.pyi` 的零参签名是错的）。
- `SketchObject.solve()` 返回 `SolveStatus` 整数，**不是 DOF** → DOF 读 `sketch.DoF`。
- **无头出不了 PNG**（渲染全在 `src/Gui`）→ worker 回传网格，supervisor 软件光栅化。
- 不存在：`Part.checkGeometry`、`Part.checkSolid`、`shape.isSolid`、`shape.Vertices`（是 `Vertexes`）。
- `setDatum()` 在**约束冲突**时抛的是 `ValueError: Invalid constraint index: N` —— 错误信息与真实原因无关。
- 详细清单与源码行号见 `docs/02-架构设计.md` 附录 A/B；附录 B（运行时实测）**效力高于附录 A（源树阅读）**。

## 构建 / 运行约定
- FreeCAD 构建：`cd free-cad/FreeCAD && pixi run configure && pixi run build` → `build/debug/bin/FreeCADCmd`。**已构建可用。**
- worker 启动：`FreeCADCmd -c --console -P <repo_root> tcad/worker/bootstrap.py --pass --worker-id=wN`
  （**`--pass` 必须放在脚本自身参数之前**，否则 FreeCADCmd 先解析并拒绝未知选项，脚本一行都不执行）
- 渲染只在 supervisor 侧（numpy + Pillow，PNG 有纯 stdlib zlib 兜底后端），不往 FreeCAD 进程塞第三方依赖。

## 唯一入口与常用命令
- **唯一生产装配入口**：`tcad.core.wiring.build_services(config)`。它把各子系统「库形状」适配成 `tcad/tools/base.py` 里的窄 Protocol。
  不要在其他地方重新接线；`LoopEngine.build_default_services(cfg)` 只是转发（传 LoopConfig 会报错）。
- 装一个完整栈不启 worker（测试用）：`build_services(cfg, start_worker=False)`
- 跑全部测试：`.venv/bin/python -m pytest tests/ -q`（当前 **438 例全绿**）
- 真端到端（打真 FreeCADCmd）：`pytest tests/contract/ -q`
- CLI：`.venv/bin/python -m tcad.server.cli --data-dir <dir> new|chat|repl|approvals|approve ...`
- **Web 界面**：`.venv/bin/python tools/serve.py --data-dir <dir> --port 8765` → http://127.0.0.1:8765/ui/
  - 命令行 override（`--provider/--model/--base-url`）只作用于内存（`persist=False`），不会覆盖 UI 里保存的设置。
- **离线模型替身**（无 Key 也能跑通整条 /chat 路径）：
  `.venv/bin/python tools/stub_llm.py --script tools/sessions/demo_bracket.json --port 8123`
  然后 `tools/serve.py --base-url http://127.0.0.1:8123/v1 --model stub-scripted`
- **「操作者即 LLM」驱动器**（不接模型供应商，自己当模型走真实工具面）：
  `python -m tools.agent_driver --list-tools --full --only ir_patch`（看模型可见的工具面/schema）
  `python -m tools.agent_driver --model-id X --calls tools/sessions/stepN.json`（执行一串工具调用）
  `python -m tools.agent_driver --model-id X --worker-probe`（直接问 worker 要原始结果，排查静默失败用这个）
- 重新生成渲染样图：`.venv/bin/python tools/render_sample.py` → `docs/renders/`
- FreeCAD API 探测：`FreeCADCmd tools/probes/smoke_freecad.py` / `probe2.py`

## 模型配置（M1/M2 新增，改之前先读）
- **`HotSwapLlm`**：`services.llm` 永远返回同一个对象（engine 在 `engine.py` 的 `_step` 里读它），换模型只替换内部 `_client`，**不重建 services、不重启 FreeCAD**。`configure()` 先建新 client 再原子替换，建失败则保留旧的。
- **设置优先级**：`settings.json`（UI 写的完整快照）> YAML/环境变量。删掉该文件即回到 YAML。落盘 0600，**回传一律掩码**（`api_key_masked`），前端永远拿不到明文。
- **`api_key` 的三角语义**（patch 里）：字段缺失=保留；`null`=清除；字符串=设置。靠 `model_fields_set` 区分。
- **切换 provider 会重置继承字段**（`model`/`base_url`/`api_key_env`/`context_window`），否则 deepseek→ollama 会继续用 DeepSeek 的端点。
- **`base_url` 为空必须拒绝**：OpenAI SDK 会静默回退到 `api.openai.com`，等于把提示词和 Key 发给了用户没选的厂商。
- **`base_version: "current"`** 是 `ir_patch` 契约的一部分（服务端解析为当前版本）；显式整数仍是多轮编辑的安全形式。
- 供应商预设只给**候选**模型名；真实列表由 `GET /settings/models` 代调 provider 的 `GET /models`。DeepSeek 官方中英文档仍并存两代模型名（`deepseek-v4-*` 与 `deepseek-chat`/`deepseek-reasoner`），**不要硬编码**。
- provider 预设 `use_env_proxy` 默认全 False：本机 `HTTPS_PROXY` 曾把直连失败伪装成"厂商挂了"。

## 模型协议（真机验证过，别改坏）
- **DeepSeek 思考模式要求 `reasoning_content` 原样回传**：assistant 消息带 tool_calls 被回放时必须带上它，否则 400（`The reasoning_content in the thinking mode must be passed back to the API`）。链路：`LlmReply.reasoning_content` ← `_parse` 读 `reasoning_content`（回退 `reasoning`）→ engine 构造 assistant 消息时**有则回显、无则不加键**。
- 一轮 = 多步循环 ⇒ 每步都在回放上一步的 assistant 消息，所以这个缺陷**必然在第 2 步爆**，且**任何思考模型都会**。
- **这类协议要求只有真机能发现**：`tools/stub_llm.py` 不产生 reasoning。离线全绿 ≠ 真机可用。
- 真机实测（`deepseek-v4-flash`，2026-09-19）：一句"60x40 底板厚 10mm"→ 5 步、11 秒、Gate 全绿，中间有一次模型自修复。

## Gate 的诚实性（核心，别退回）
- **`bbox_spec`/`mass_spec` 在无对应需求时返回 SKIP 而不是 PASS**。曾经返回 PASS（"no confirmed bbox requirement"）——**没做校验却报通过**，导致"模型只建了两根探针也全绿"。空检查报 PASS 比报 FAIL 危险得多。
- **`requirement_coverage`**（advisory）在无 confirmed 约束时明确说"只验证了几何自洽，没验证是否符合要求"。
- **`ir_commit` 的 PASS 文案会说明它是否判过需求**：没判过就明说，不再只写 "Build succeeded"。
- 全 blocking SKIP 仍 = 不通过（fail-closed 未动）。
- **`update_requirement`（confirmed=true）是 Gate 唯一的判据来源**；用户给的尺寸/数量/位置必须记下来，否则 Gate 无卷可判。

## ir_patch 工具描述（模型可见的契约，改它要跑测试）
- **op 必须逐个列全，不能用 `additive_*` 通配** —— 通配等于没告诉模型有这些能力（曾导致模型用 pad 建出三个不接触的实体）。
- 必须写明：**同一 body 内特征只在几何相交时合并**；`refs` 只声明构建顺序、**不产生几何关系**；贴到已有形体要用 `plane: {kind:"face", feature_id, sub:"FaceN"}`。
- `tests/unit/test_ir_tools_description.py` 会拿描述与 `FeatureOp` 对账，别再让描述落后于 schema。

## 熔断上限：可选，且默认不设（用户指令，别再改回硬上限）
- **`None` = 该维度不设上限**，五项（`max_steps_per_turn` / `max_tokens_per_turn` / `step_timeout_s` / `turn_wall_clock_s` / `max_compile_retries`）在 `configs/default.yaml` 里**全部为 `null`**。
  用 `None` 而非 `0`/大数：`0` 与 `999999` 的语义都会在不同方向上静默出错，`None` 不会（`0` 在 `is_unlimited` 里是**真上限**，有测试守着）。
- 无上限时一个 Turn 只会因三件事结束：**Gate 全绿 / `FAILED` / `AWAITING_APPROVAL`**。
- **有界档 = `configs/policies/strict.yaml`**，它必须把五项**全部**限住（漏一项就留一处无上限）。反证实测：同一 27 步脚本，默认档 26 步 `succeeded`，strict 档第 12 步 `exhausted`。
- **别把「工作量上限」和「单次请求活性」混为一谈**：取消前者**不影响**后者。挂死的 LLM 调用仍撞 `llm.request_timeout_s`、挂死的工具仍撞 `ToolSpec.timeout_s`、死 worker 仍撞 worker 传输超时。改预算时**不要顺手把这些也去掉**。
- 无上限状态必须**可见**：`GET /health` 的 `budget.{limits,unbounded,unbounded_all}` + 界面顶栏「无预算上限」徽标。
- **客户端断开必须终止 Turn**：`/chat` 的 SSE 生成器在 `finally` 里 `task.cancel()`。无上限之前靠 `max_steps_per_turn` 兜底，之后不会自己停。
  ⚠️ **`yield start` 必须在 `try` 之内**：生成器在进入 `try` 之前被关闭时 `finally` **不执行**，任务就泄漏（这是实测踩到的）。
- 同时顺手修了：每次 `/chat` 都把 `services.hooks` 包一层 `HookEventTap` 且不还原 → 逐请求累积。现在 `finally` 里还原。

## 前后端契约（改界面之前先读，这三条都踩过）
- **SSE 帧里的枚举是「序列化值」不是「枚举名」**：pydantic 对 `(str, Enum)` 发出的是小写值 —— `"succeeded"` / `"exhausted"` / `"pass"` / `"blocking"`。前端任何以状态为键的表都必须用小写值，用 `SUCCEEDED` 这类枚举名**永远匹配不到**（曾导致 turn 结束屏幕上没有任何结论）。
- **engine 的 observer 帧里 `images[].path` 是磁盘绝对路径**，浏览器用不了；server 侧必须经 `artifact_url_for()` 转成 `/models/{id}/artifacts/{file}?version={n}` 再发。
- **不要用 `StoreAdapter.current_version()` 判断模型是否存在**：它对"模型不存在"和"模型处于 v0"**都返回 0**（而新建的模型就是 v0）。需要区分时用 `load()`；端点对未知模型必须 404 而不是 500。

## 建模时最容易踩的两条（已写进 ir_patch 工具描述）
1. **Pocket 方向**：草图在 XY（法向 +Z）时 `PartDesign::Pocket` **默认朝 -Z 切**，而底板在 +Z 侧 → 切进空气，特征什么都不做**却报告成功**（`ok=true`、`errors=[]`、体积不变）。要用 `"reversed": true`。
2. **草图定位**：把一条线的**起点与终点**都用绝对坐标标注 + H/V 约束 = 求解器冲突。正确做法：轮廓建在**草图自己的原点**上（绑一条线到草图原点 + 标注对侧端点），再用 sketch 的 `offset` 整体定位。
3. 通用：**静默无效比报错危险**。凡"特征没生效"，先怀疑方向/基准面，再看体积有没有变。

## 操作纪律（踩过的坑）
- **不要在用户正在使用的端口上起演示/验证服务。** 曾用同一个 8765 端口配 `--data-dir .tcad_live2` 指向离线替身跑验证，用户那段时间打开界面看到的是 stub 的配置和无 key 状态，误以为「配置没保存、回到默认模型」。验证请换端口（8766+），或明确告诉用户当前端口被占用。
- 多实例并存时用 `GET /health` 的 `data_dir` 分辨（UI 的设置对话框与顶栏 tooltip 也会显示）。
- 用户报「功能 X 没用」时，先确认他连的是哪个实例/数据目录，再怀疑代码。

## 已知环境限制
- **无法 push 到 GitHub**：认证与体积都正常，pack 能完整上传，但代理不转发 `receive-pack` 的响应流（HTTP/2 → 408，HTTP/1.1 → curl 52 empty reply）。`ls-remote` 正常（下载类 OK）。`~/.ssh` 无密钥。**需要换代理或加 SSH key**；`127.0.0.1:7890` 与默认 50683 都试过，后者连不上 GitHub。
- 执行沙箱禁止写 OS 临时目录 → pytest 必须用 `--basetemp=.pytest_tmp`（已在 pyproject.toml）。**该目录每次运行会被清空，别把产物放那里。**
- macOS BSD `grep` 不支持 `\|` 交替 → 用 Grep 工具或 `grep -E`。
- 无 `timeout` 命令。
