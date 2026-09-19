# text-to-cad-freecad

**Chat-driven parametric CAD on top of FreeCAD.** You describe a part in words; the
agent edits an intermediate representation (IR), FreeCAD compiles it, and a
deterministic Gate decides whether it is actually done. FreeCAD is used as a
*geometry compiler backend* — the model never touches a FreeCAD document.

> 用一句话描述零件，拿到一个**可继续编辑**的参数化模型 + 可导出的 STEP/STL。
> 包名 `tcad`。成功的定义只有一个：**Gate 全绿**。模型说"我做完了"不算数。

```
Python 3.11+ · pydantic v2 · FastAPI + SSE · SQLite(WAL) · 前端零依赖零构建（原生 ES module）
491 测试全绿（451 单测 + 40 契约测试，整包约 12 秒）
```

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [四条不变量（改代码前先读）](#四条不变量改代码前先读)
- [快速开始](#快速开始)
- [使用方式](#使用方式)
- [模型看到的工具面](#模型看到的工具面)
- [一个 Turn 的生命周期](#一个-turn-的生命周期)
- [Gate 到底判什么](#gate-到底判什么)
- [目录结构](#目录结构)
- [配置](#配置)
- [HTTP API](#http-api)
- [测试](#测试)
- [已知限制与未验证项](#已知限制与未验证项)
- [设计文档](#设计文档)
- [许可](#许可)

---

## 它解决什么问题

让**不会 CAD 的人**用对话拿到可继续编辑的参数化模型。工程上真正的难点不是"生成
一个形状"，而是三件事：

1. **指代消解** —— "把那个孔往右挪 5mm" 里的"那个孔"是什么？
2. **上下文爆炸** —— 把整个 BRep/特征树塞进 context，几轮就撑爆了。
3. **反馈质量** —— 失败信息如果不能让模型自己改对，自修复就是空话。

本项目的答案是一条主决策：**几何内核与语义真相分离**。

```
              唯一语义真相                        投影产物
   用户 ⇄ LLM ⇄  IR（特征 DAG + 参数 + 稳定命名）  ──编译器──▶  FreeCAD document ──▶ BRep/STEP/STL
                     ▲                                            │
                     └──────────── Gate（独立读路径校验）───────────┘
```

模型**永远不直接读写 FreeCAD document**，只改 IR。FreeCAD 被降级为一个编译器后端。
这一条决定了其余所有设计：稳定命名让"那个孔"可指代；IR 摘要让上下文可控；
Gate 只读磁盘快照与导出产物，让"校验"与"生成"不共享内存。

> 为什么不直接用 `raw_python` 自由脚本？黑盒脚本跑完只剩一个 BRep，
> 无法做特征级校验，V 层形同虚设。脚本只作为**默认禁用、三重门放行**的逃生舱存在。

---

## 四条不变量（改代码前先读）

| # | 不变量 | 含义 |
|---|---|---|
| 1 | **成功定义唯一** | 只有 `GateReport.passed == true` 才是完成。模型自称完成不是终止条件。全 blocking 检查都 SKIP 也**不算**通过（fail-closed）。 |
| 2 | **单写入口** | IR 只能经 `ir_patch` / `ir_commit` 变更。事件流 append-only 是单一真相源，**先写事件再写快照**。 |
| 3 | **Hook fail-closed** | Hook 抛异常/超时一律 `DENY`，永不 fail-open。 |
| 4 | **无头优先** | 一切能力必须在 `FreeCADCmd`（无 GUI）下可用。FreeCAD 的渲染在 `src/Gui` 层，无头用不了 —— 所以渲染走"worker 回传网格 + supervisor 侧软件光栅化"。 |

还有一条纪律贯穿全项目：**失败不许长得像正常状态**。
`bbox_spec` 在无对应需求时返回 `SKIP` 而不是 `PASS`（"没做校验却报通过"比报失败危险得多）；
会话列表读不到时显示"读取失败"而不是"还没有会话"；`latest_version()` 用 `None` 而不是 `0`
表示"模型不存在"（因为新建的模型**就是** v0）。

---

## 快速开始

### 1. 前置：构建 FreeCAD（硬前置，一次性）

```bash
cd free-cad/FreeCAD          # 上游源码 26.3.0dev，已在 .gitignore 中（近 8000 文件，不入库）
pixi run configure           # → cmake --preset conda-macos-debug
pixi run build               # → cmake --build build/debug
# 产物：free-cad/FreeCAD/build/debug/bin/FreeCADCmd
```

### 2. 安装 harness

```bash
python -m venv .venv
.venv/bin/pip install -e ".[server,dev]"
```

> supervisor（本包）跑在 venv 的 Python 上；worker 跑在 FreeCADCmd **自带的解释器**里。
> 两者只经 stdin/stdout 上的 JSON Lines RPC 通信，不跨进程传 BRep，
> 也不往 FreeCAD 进程塞第三方依赖。

### 3. 先离线跑通（不需要任何 API Key）

```bash
# 终端 A：一个脚本化的"模型替身"，复刻一次真实的建模轨迹（含两次故意失败与自我修复）
.venv/bin/python tools/stub_llm.py --script tools/sessions/demo_bracket.json --port 8123

# 终端 B：起服务，把模型指向替身
.venv/bin/python tools/serve.py --data-dir data --port 8765 \
    --base-url http://127.0.0.1:8123/v1 --model stub-scripted
```

打开 **http://127.0.0.1:8765/ui/** ，点「＋ 新建」，输入：

> 一个 80x50 的安装底板，厚度 8mm，中间开一个 40x20 的通槽

你会看到工具调用卡片、Gate 报告、四个标准视图的实时渲染，以及最终结论。
`demo_bracket.json` 里有两次真实失败（草图过约束、pocket 切反方向）——
重点看模型**有没有读懂错误并自己改对**，这是本项目最核心的假设。

> 命令行的 `--base-url/--model/--provider` 只作用于内存（`persist=False`），
> 不会覆盖你在界面里保存的设置。

### 4. 接真实模型

界面右上角 **⚙** 打开设置：选供应商（deepseek / openai / ollama / vllm / custom）、
模型名（可点「列模型」向供应商实时索取）、base_url、API Key、温度，然后「测试连通性」。
保存即热切换 `HotSwapLlm` 内部的 client —— **不需要重建服务栈，更不重启 FreeCAD**。

配置落盘在 `<data_dir>/settings.json`（0600），优先级高于 YAML 与环境变量；
删掉它即回到 YAML/环境变量。API Key 回传一律掩码，前端永远拿不到明文。

命令行的等价写法（不落盘）：

```bash
.venv/bin/python tools/serve.py --data-dir data --port 8765 \
    --provider deepseek --model deepseek-chat
```

---

## 使用方式

### Web 界面

四栏布局：**会话列表 │ 对话流 │ 视图 │ 检查器**。

```
┌───────────────────────────────────────────────────────────────────────────┐
│ 顶栏 ☰  模型: DeepSeek / deepseek-chat   ● 就绪                        ⚙   │
├──────────────┬────────────────┬─────────────────────────┬─────────────────┤
│ 会话 [＋新建] │ 会话流          │ 视图区                   │ 检查器          │
│  底板 80x50   │  用户消息       │  [等轴测][正视][俯视]     │  IR 特征链      │
│  刚刚 · 4条   │  模型文本       │                         │  当前版本       │
│  v3           │  工具调用卡片   │      渲染图              │  Gate 报告      │
│  法兰 · 无几何│  Gate 报告卡片  │                         │  需求(已确认)   │
│  ▸ 进行中     │  hook 事件流    │  下载 STEP / STL         │  待批审批       │
│              ├─────────────────┴─────────────────────────┴─────────────────┤
│              │ 输入框                                    [停止] [发送]      │
└──────────────┴───────────────────────────────────────────────────────────┘
```

- **新建会话**：点「＋ 新建」或 `⌘/Ctrl + K`。一个会话 = 一次对话 **+ 它唯一在造的零件**，
  两者同时创建、绑定不再变更（要继续旧会话就带它的 `thread_id` 发消息，不要复用 `model_id`——
  那会把零件重置为 v0，服务端会直接 409 拒绝）。
- **切换会话**：对话流、视图、产物、检查器**四件一起换**；URL 带 `?thread=<id>`，刷新或分享链接回到同一会话。
- **运行中**：侧栏标出正在执行的会话，对话流底部有当前步骤 + 已用时的 live 行；
  此时切走会中止该回合（并通知服务端 cancel，不留后台烧 token）。
- **打断回合**：回合运行时输入框右侧出现「停止」（或按 `Esc`）。它点名停掉**这一个**回合：
  服务端取消在飞的模型调用，回合以 `aborted` 结束 —— 界面上是一个明确的结论
  「⏹ 回合已被打断 …… 此后没有任何东西经过 Gate 验证」，不是文字突然安静下来。
  打断前已写进 IR 的改动保留，会话可以立刻继续。**打断 ≠ 完成**，也 ≠ 失败。
- 「清空视图」只清显示，不影响磁盘上的会话；切走再切回即重新回放。

### CLI

```bash
.venv/bin/python -m tcad.server.cli new bracket --requirement "一个 60x40 的安装底板，厚 10mm"
.venv/bin/python -m tcad.server.cli chat bracket "做一个 60x40 的矩形底板，厚度 10mm"
.venv/bin/python -m tcad.server.cli repl bracket          # 多轮
.venv/bin/python -m tcad.server.cli approvals             # 待批
.venv/bin/python -m tcad.server.cli approve <id> --grant
```

进度来自 Hook dispatcher 的**真实生命周期**，不是事后重建。

### 不用模型，自己当模型：`agent_driver`

验证"工具面是否可发现、失败是否可行动、Gate 是否是唯一判据"，不需要任何 LLM：

```bash
# 模型实际看到的工具面与 schema
.venv/bin/python -m tools.agent_driver --list-tools --kind create

# 执行一串脚本化的工具调用（真实 registry / 真实 hook dispatch）
.venv/bin/python -m tools.agent_driver --model-id bracket --calls tools/sessions/bracket_step1.json

# 直接问 worker 要原始结果（排查静默失败用这个）
.venv/bin/python -m tools.agent_driver --model-id bracket --worker-probe
```

---

## 模型看到的工具面

| 工具 | 作用 |
|---|---|
| `ir_list_features` | 列特征链与稳定命名 —— "那个孔"靠它指代 |
| `ir_get` | 读当前 IR（可按版本） |
| `ir_digest` | 几何摘要：包围盒、体积、面/边计数、关键尺寸（**不是**整个 BRep） |
| `ir_patch` | 唯一的写入口：一组带 `reason` 的 op，带 `base_version` 乐观并发 |
| `ir_commit` | 提交：编译 → Gate → 把 GateReport 原样回灌给模型 |
| `geo_view` | 看渲染图（**占用 context**，因此由声明的视觉检查点把关） |
| `geo_measure` | 量距离/角度/体积等 |
| `asset_export` / `asset_import` | 导出 STEP/STL，导入已有 STEP/STL |
| `raw_python` | **默认不注册**的逃生舱（privileged 档，三重门） |

IR 支持的 op（模型可见的契约，`tests/unit/test_ir_tools_description.py` 会拿它和 schema 对账）：

```
pad · pocket · revolution · groove · fillet · chamfer · draft · thickness · hole
mirrored · linear_pattern · circular_pattern · polar_pattern · multi_transform · datum_plane
additive_box · additive_cylinder · additive_sphere
subtractive_box · subtractive_cylinder · subtractive_sphere
```

两条最容易让模型建错的语义，已写进工具描述：

- **同一 body 内的特征只有在几何上真的相交时才合并**；`refs` **只声明构建顺序，不产生任何几何关系**。
- 要贴在已有形体上，用 `plane: {kind:"face", feature_id:..., sub:"Face6"}`。

---

## 一个 Turn 的生命周期

```
用户一句话
   │
   ▼  LoopEngine.run_turn
   ├─ pre_turn hook ─────────────── 配额/内容安全预检（fail-closed，异常即 DENY）
   └─ 循环（直到终态）
        ├─ pre_step hook ────────── 上下文装载 / 成本上限
        ├─ LLM.chat(messages, tools)        只给「该 kind 允许的工具档」
        ├─ pre_tool_use hook → 执行工具     失败→ ToolError(带 feature_id/hint) 文本化回灌
        └─ ir_commit
             ├─ IR 语义预检
             ├─ worker 编译（IR → FreeCAD document）
             ├─ 导出 STEP/STL
             └─ Gate（独立读路径）──▶ GateReport 回灌给模型
   │
   ▼
TurnResult{state, steps, tokens_in/out, gate_report}
    终态只有四种：SUCCEEDED（Gate 全绿）/ FAILED / AWAITING_APPROVAL / EXHAUSTED
    外加一种来自人的终态：ABORTED —— 被「停止」打断。它不是成功，也不是失败：
    它是「你叫停了它，此后没有任何东西经过 Gate 验证」。
```

**默认不设工作量上限**（`max_steps_per_turn` 等五项在 `configs/default.yaml` 里全是 `null`）：
一个 Turn 只会因 Gate 全绿、FAILED、AWAITING_APPROVAL 结束。
需要上界时用有界档（见[配置](#配置)）。

> **打断是这四种终态之外的那一种，它必须同时做到两件事。** 步骤边界上的
> `stop_requested` 谓词是「为什么停」（停在两步之间、甚至第一步之前也不会漏），
> `task.cancel()` 是「怎么停」（一轮的绝大部分墙钟时间是一个 LLM 请求，不取消它
> 就只能在下一个步骤边界生效，最坏要等一整个模型超时）。两者都在引擎里，
> 且**只有谓词说「是有人要求的」时，`CancelledError` 才会被解释成 ABORTED**——
> 别的取消（服务端关闭、客户端断开）保持 asyncio 原本的语义。

> 单次请求的**活性**与工作量上限是两件事，别一起删：挂死的 LLM 仍撞 `llm.request_timeout_s`、
> 挂死的工具仍撞 `ToolSpec.timeout_s`、死 worker 仍撞传输超时。
> 另外，无上限意味着必须有**停滞检测**：连续 3 步只输出文本、不调任何工具就判 FAILED——
> 空转不能改变世界，重复它只是烧 token。

---

## Gate 到底判什么

Gate 从磁盘重新加载 IR 快照与导出产物（CQRS：**不碰生成路径的内存对象**），
按 fail-closed 规则聚合：

| check | 严重度 | 判什么 |
|---|---|---|
| `solid_validity` | blocking | 每个 solid 过 OCC BRepCheck |
| `solid_count` | blocking | 实体数量符合预期 |
| `sketch_fully_constrained` | blocking | 必需草图完全约束 |
| `round_trip` | blocking | STEP 往返前后体积一致 |
| `exportability` | blocking | 声明的产物都存在且非空 |
| `bbox_spec` / `mass_spec` | blocking | 包围盒 / 质量是否符合**已确认**的需求表达式；**无需求时 SKIP** |
| 规格检查（每个 `ConstraintExpr` 一条） | blocking（仅 confirmed） | 把你说的尺寸/数量/位置变成可执行判定 |
| `wall_thickness` | advisory · approximate | 最小壁厚（worker 没测就 SKIP，**绝不编造通过**） |
| `requirement_coverage` | advisory | 无 confirmed 约束时明说"只验证了几何自洽，没验证是否符合要求" |

`passed` = 无 blocking FAIL/ERROR，**且**不是所有 blocking 检查都 SKIP。

> 这条不变量是被真实缺陷逼出来的：`bbox_spec` 曾在"没有对应需求"时返回 PASS，
> 于是模型只建了两根探针也"全绿"。**空检查报 PASS 比报 FAIL 危险得多**——
> 后者会被看见，前者会被信任。
>
> 唯一能喂给 Gate 的判据是 `update_requirement(confirmed=true)`。
> 相关测试：`tests/unit/test_commit_report_text.py`、`test_verify_checks.py`。

---

## 目录结构

```
tcad/
├── core/          types.py（全部 pydantic 模型）· wiring.py（唯一装配入口 build_services）
│                  worker_client.py（supervisor 侧的 worker 句柄）
├── loop/          engine.py（Turn/Step 状态机）· strategies.py（M1 循环/M2 分叉/M3 对抗）
│                  budget.py（熔断上限，None = 不设）· commit.py（ir_commit→编译→Gate→回灌）
├── tools/         base.py（ToolSpec/ToolResult/权限档 + build_default_registry）
│                  ir_tools.py · geo_tools.py · privileged.py
├── context/       assembler.py（预算装配 + 三档降级）· digest.py（几何摘要投影）· compactor.py
├── store/         ir_store.py（版本化快照 + 乐观并发）· event_log.py（append-only jsonl）
│                  session_db.py（SQLite/WAL：threads/turns/steps/messages/approvals）· artifacts.py
├── hooks/         dispatcher.py（确定性合并，fail-closed）· policy.py · approval.py
├── verify/        gate.py · checks_solid.py · checks_spec.py · specexpr.py · context.py
├── ir/            schema.py · patch.py · naming.py（稳定命名）· validate.py
├── worker/        跑在 FreeCADCmd 进程内：bootstrap.py · rpc.py · compiler.py
│                  introspect.py · mesh.py · exporters.py · protocol.py · selftest.py
├── render/        仅 supervisor 侧：camera.py · raster.py（numpy z-buffer）· png.py（Pillow 或纯 stdlib）
├── llm/           client.py（OpenAI 兼容 + 重试 + token 计数）· hotswap.py（原地换模型）
├── server/        app.py（FastAPI + SSE）· cli.py · ui/（index.html · app.js · styles.css）
└── config/        schema.py · loader.py · providers.py（供应商预设）· settings.py（运行时设置）

tools/             serve.py · stub_llm.py（离线模型替身）· agent_driver.py（操作者即 LLM）
                   render_sample.py · probes/ · sessions/（脚本化轨迹）
configs/           default.yaml · policies/strict.yaml（有界档）
data/              运行期数据（.gitignore）：models/<id>/{v*.json,events.jsonl} · artifacts/ · tcad.sqlite3
docs/              01-需求澄清问卷 · 02-架构设计 · 03-交互界面与模型配置 · renders/
tests/             unit/（437）· contract/（40，真跑 FreeCADCmd）· fixtures/
```

---

## 配置

`configs/default.yaml` 是唯一默认配置；`configs/policies/strict.yaml` 是**只能收紧**的叠加档。

```python
from tcad.config.loader import load_config
cfg = load_config("configs/default.yaml", overlays=["configs/policies/strict.yaml"])
```

有界档把五项上限**全部**限住（`max_steps_per_turn: 12` / 60k tokens / 120s 步超时 /
240s 回合墙钟 / 1 次编译重试），并把温度降到 0。`tools/serve.py --config` 只接**单个**文件，
所以严格档要么用上面的 Python API 叠加，要么自己合成一个完整 YAML。

配置优先级：`<data_dir>/settings.json`（界面写的完整快照）> YAML / 环境变量。
`${VAR}` 与 `${VAR:-default}` 在 YAML 里会被环境变量插值。

接入点只有两个：

- **生产装配**：`tcad.core.wiring.build_services(config)` —— 把各子系统适配成 `tcad/tools/base.py` 的窄 Protocol。
  **不要在其他地方重新接线。**
- **模型配置热切换**：`apply_llm_settings(services, settings)` —— 换 client，不重建栈。

常用环境变量：`TCAD_LLM_BASE_URL` · `TCAD_LLM_MODEL` · `TCAD_LLM_API_KEY`。
离线/私有化：把 `llm.base_url` 指向 vLLM 或 Ollama 即可，协议是 OpenAI 兼容的。

---

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 活性 + `data_dir`（分辨实例）+ `budget`（哪些上限没设） |
| `POST` | `/chat` | 跑一个 Turn，**SSE 流**：`start` / `progress` / `agent` / `error` / `result` |
| `POST` | `/chat/interrupt` | 打断 `{request_id}` 指名的那个回合；返回 `stage: running\|pending` |
| `GET` | `/sessions` | 会话列表：标题、`model_id`、`ir_version`、最近活动时间 |
| `POST` | `/sessions` | 新建会话：同时建它的模型（`model_id` 已存在 → 409） |
| `GET` | `/threads/{id}/messages` | 会话历史（刷新不丢） |
| `POST` | `/models` | 播种一个空 IR 模型（已存在 → 409） |
| `GET` | `/models/{id}/ir` | IR 快照（可 `?version=`） |
| `GET` | `/models/{id}/render` | **人的**视图：`?view=iso\|front\|top\|right`，按 (版本, 视图, 风格, 尺寸) 磁盘缓存 |
| `GET` | `/models/{id}/artifacts[/path]` | 产物列表 / 下载 PNG·STEP·STL |
| `GET`/`POST` | `/approvals[/{id}]` | 待批审批 / 批准或拒绝 |
| `GET`/`PUT` | `/settings/llm` | 读（掩码）/ 改配置（热切换） |
| `GET` | `/settings/providers` · `/settings/models` | 供应商预设 / 代调供应商列模型 |
| `POST` | `/settings/llm/probe` | 连通性探测（可先测后存） |
| `GET` | `/ui/` | 前端 |

三个值得记住的设计点：

- **`/render` 不是模型的眼睛。** 模型的视觉能力是 `geo_view` 工具，受视觉检查点约束（每张图都占 context）。
  人看自己的零件是另一回事，所以走另一条路：无 hook、无预算、磁盘缓存。
- **`/settings/*` 不依赖 worker。** FreeCADCmd 起不来时，"连不上模型供应商**又**改不了供应商"是个死局。
- **打断按回合点名，不按会话。** `request_id` 由**客户端**在发请求之前铸造并随 `/chat` 送出，
  所以停止按钮在任何帧回来之前就能说出「停哪一个」；服务端不认识这个 id 时会把它记下来
  （有界、30s 过期），等这个回合注册时立刻执行——一个真的发起过 `/chat` 的客户端不会输掉这场比赛。
  没有在跑的回合时返回 200 + `stage`，因为「没有这个回合」是答案，不是服务端故障。

---

## 测试

```bash
.venv/bin/python -m pytest tests -q              # 491 全绿（整包约 12 秒）
.venv/bin/python -m pytest tests/unit -q         # 451，不需要 FreeCAD
.venv/bin/python -m pytest tests/contract -q     # 40，真跑 FreeCADCmd
```

打断的**运行时**行为另有一条真实路径可复跑（需要一个慢模型，否则没有「回合中间」可打断）：

```bash
# 终端 A：慢速替身（每次回复停 1.5s）
.venv/bin/python tools/stub_llm.py --script tools/sessions/demo_bracket.json --port 8766 --delay 1.5
# 终端 B：服务指向它（换端口，别占用你正在用的那个）
.venv/bin/python tools/serve.py --data-dir .tcad_interrupt --port 8767 \
    --base-url http://127.0.0.1:8766/v1 --model stub-scripted
# 终端 C：在模型调用飞的途中打断，检查结论、耗时与会话可继续性
.venv/bin/python tools/probes/interrupt_live.py --port 8767
```

写法上的两条纪律：

1. **断言要能失败。** 新增的每条关键断言都要做**非空转验证**：把修复删掉，确认测试会红。
   做不到这一点的断言只是在描述代码，不是证据。
2. **只有真机能证明真机。** 离线替身不产生 `reasoning_content`，所以"思考模型必须回传 reasoning"
   这类协议要求**离线全绿也发现不了**——它是真机第 2 步炸出来的，现在由 `tests/unit/test_llm_client.py` 守着。

`tests/unit/test_server_ui.py` 里前端部分是**源码结构断言**（本仓库不引入 JS 测试运行时，
以保持"前端零依赖、零构建"）。运行时行为由真实浏览器验收覆盖，不靠断言假装覆盖。

---

## 已知限制与未验证项

不遮掩，这些是当前的真实边界：

| # | 限制 | 说明 |
|---|---|---|
| 1 | **跨回合只延续几何，不延续对话** | 每个 Turn 只喂 `[system, user]`。`session_db` 只存文本、不存工具调用与结果，直接回灌会让模型只看到自己的旁白而看不到工具产出——比没有历史更误导。正解是接 `tcad/context/assembler.py`（已存在、未接线）+ 持久化工具轨迹。 |
| 2 | **真机只在 DeepSeek 上验证过** | function calling 与 thinking 并存的真实行为、`probe_llm` 对真实供应商，只在 `deepseek-v4-flash` 上跑过。 |
| 3 | **`context.window_tokens` 是估值** | 128000 未按真实 token 标定，三档降级（0.70 / 0.85）的阈值因此不准。 |
| 4 | **界面无认证** | 默认只绑 `127.0.0.1`；绑非回环地址时启动会打印警告。不要暴露到公网。 |
| 5 | **无会话重命名 / 删除 / 搜索**，不做会话内换模型 | 会话↔模型绑定单向是有意的。 |
| 6 | **多标签页未协调** | 服务端没有 per-thread 回合锁，`/chat` 按请求替换 `services.hooks`；并发两个 `/chat` 不是受支持的用法。同一个 `request_id` 起第二个回合会被 409 拒绝（否则被覆盖的那个回合就再也停不下来了）。 |
| 7 | **打断停的是 supervisor，不是 worker 里的活** | 在飞的 LLM 请求会被真的取消；但已经发给 FreeCAD worker 的一次调用（编译 / 网格化）会在 worker 进程里跑完，结果被丢弃——worker 协议没有中途取消。 |
| 8 | **不做的范围** | GUI 交互建模 / 自由曲面造型 / 装配约束求解 / 2D 工程图 / 仿真 / CAM / 多用户协作。 |
| 9 | **本机无法 `push` 到 GitHub** | 代理不转发 `receive-pack` 的响应流（`ls-remote` 正常）。需要换代理或加 SSH key。 |

---

## 设计文档

| 文档 | 内容 |
|---|---|
| [`docs/01-需求澄清问卷.md`](docs/01-需求澄清问卷.md) | 需求规格的每个十字路口 |
| [`docs/02-架构设计.md`](docs/02-架构设计.md) | 可直接编码的架构设计：六层（E/T/C/S/L/V）+ 配置格式 + FreeCAD API 实测附录 |
| [`docs/03-交互界面与模型配置.md`](docs/03-交互界面与模型配置.md) | 前端、会话列表、模型配置与热切换 |

`docs/02` 的附录 B（**运行时实测**）效力高于附录 A（源码树阅读）——
早期曾有"读源码得出的错误结论"被运行时推翻（例如 `shape.BoundBox` 其实存在，
而 `shape.check()` 的失败是抛 `ValueError` 而不是返回 `False`）。写 worker 代码前先看附录。

---

## 许可

本仓库自有代码遵循上游仓库的许可；**FreeCAD 以 LGPL-2.1 使用**，`free-cad/` 为本地构建依赖，
**不随本仓库分发**（已在 `.gitignore` 中）。不做静态链接闭源分发；对内核的改动会回馈上游
（当前策略是**不改内核**，只用 `Part` / `PartDesign` / `Sketcher` 的 Python API）。
