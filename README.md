# text-to-cad-freecad

**Chat-driven parametric CAD on top of FreeCAD.** You describe a part in words; the
agent edits an intermediate representation (IR), FreeCAD compiles it, and a
deterministic Gate decides whether it is actually done. FreeCAD is used as a
*geometry compiler backend* — the model never touches a FreeCAD document.

> 用一句话描述零件，拿到一个**可继续编辑**的参数化模型 + 可导出的 STEP/STL。
> 包名 `tcad`。成功的定义只有一个：**Gate 全绿**。模型说"我做完了"不算数。

```
Python 3.11+ · pydantic v2 · FastAPI + SSE · SQLite(WAL) · 前端零依赖零构建（原生 ES module）
Python 单测 + 真 FreeCAD 契约测试 + Node 前端运行时测试；真实供应商测试按配置开关（见「测试」）
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

### 1. 前置：安装或构建 FreeCAD（一次性）

可以使用已有的 FreeCADCmd：

```bash
export TCAD_FREECAD_CMD=/path/to/FreeCADCmd
# PATH 中的裸命令名也支持：export TCAD_FREECAD_CMD=freecadcmd
```

Linux 发行版的 FreeCADCmd 嵌入解释器若无法启动，但匹配版本的系统 Python
可以加载 FreeCAD.so，可显式使用原生模块适配器（仍然运行真实 FreeCAD）：

```bash
export TCAD_FREECAD_CMD="$PWD/tools/freecad_python.py"
# 必要时：export TCAD_FREECAD_LIB_DIR=/path/to/freecad/lib
# 适配器 shebang 默认 /usr/bin/python3，必须与 FreeCAD 的 Python ABI 匹配
```

也可以使用项目原来的源码构建：

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

你会看到工具调用卡片、Gate 报告、可自由旋转的真实网格，以及最终结论。
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

中央 3D 视图使用真实 FreeCAD 三角网格，无 CDN、无需前端构建：

- 左键拖动旋转，滚轮缩放，右键 / Shift+拖动平移
- 等轴测、前、后、左、右、上、下七个视角；F / Home / 双击适配模型
- 单指旋转、双指缩放和平移；聚焦画布后可使用方向键和 0–6 键
- 同会话几何更新保留相机；切换会话重置，过期响应不能覆盖新会话
- WebGL 不可用时明确降级为静态 PNG；网格加载不等于 Gate 通过
- 预览繁忙时显示可重试提示，不会通过 PNG 重试绕过后台限流

实现与验收边界见 [交互预览验收记录](docs/interactive-preview-acceptance.md)。

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
| `raw_python` | **默认不注册**的逃生舱（privileged 档，三重门）。审批不是"按工具名发一张长期通行证"：每张审批绑定**工具 + 参数指纹 + 会话/回合 + 有效期**，并把人能读的 payload 摘要一并存下来（`tcad/hooks/approval.py`） |

IR 支持的 op（模型可见的契约，`tests/unit/test_ir_tools_description.py` 会拿它和 schema 对账）：

```
pad · pocket · revolution · groove · fillet · chamfer · draft · thickness · hole
mirrored · linear_pattern · circular_pattern · polar_pattern · multi_transform · datum_plane
additive_box · additive_cylinder · additive_sphere
subtractive_box · subtractive_cylinder · subtractive_sphere
```

**已验证能力 vs 实验性能力**由 `tcad/ir/capability.py` 单独记录，并直接生成给模型的 op 清单——
`VERIFIED` = 有真内核测试量过几何（`pad` / `pocket` / `revolution` / `groove` / `fillet` / `chamfer` /
`draft` / `thickness` / `mirrored` / `linear_pattern` / `polar_pattern`，以及六个原语 `additive_*` /
`subtractive_*`，共 17 个），`EXPERIMENTAL` = 只证明"能 `addObject`"（剩 `hole` / `circular_pattern` /
`multi_transform` / `datum_plane`），编译后必须自己 `ir_digest` 复测。给模型的描述与校验器读同一张表。

六个原语的**世界坐标放置**用 `placement` 字段（`tests/contract/test_primitive_placement.py`，9 项真内核）：
`{"position": {"x": 10, "y": 10, "z": 0}}` 是特征自身原点（盒子从它长向 +X/+Y/+Z，圆柱/球以它为中心），
`{"axis": {"x": 0, "y": 1, "z": 0}, "angle": 90}` 给出绕该轴的旋转（度、逆时针、过 `position`）。
实测：r6 h20 的圆柱放在 (10,10,0) 与 80×50×8 的板并成**一个**实体、体积 `32000+432π`；同一圆柱绕 Y 转 90° 就
沿 X 躺下；`subtractive_cylinder` r5 放在 (20,25,−6) 打出 `32000−200π` 的通孔；放到板外的切除按名拒绝
（`did not change the solid`）。载体是 FreeCAD 的 **`Placement`**——`AttachmentOffset` 在 `MapMode=Deactivated`
下**不生效**（实测设了它实体一动不动），这条差别搞错就会做出"构建成功但放置被静默忽略"的特性。只有这六个 op
接受 placement，其他 op 给了会被 `placement_unused` 拒绝；移动已有原语用 `update_feature` 改 `placement`，不要重建零件。

`mirrored` / `linear_pattern` / `polar_pattern` 转正的经过（`tests/contract/test_patterns_mirror.py`）：这三个 op 的
镜像面/重复轴都是 **LinkSub 引用**，JSON 标量表达不了，而内核缺引用时的失败方式是**静默**的——`LinearPattern`
没有 `Direction` 会返回**一份**阵列（体积 1000 而不是 3000，`ok=True`），`Mirrored` 没有 `MirrorPlane` 直接返回
**空形状**。修法是给它们类型化载体：镜像复用草图附着那套 `plane` 字段（`origin_plane`/`datum_plane`/`face`），
阵列用 `params.axis` 这个**名字**（`X`/`Y`/`Z` 机体轴；`H_Axis` 等草图轴被明确拒绝）。16 项真内核测试断言：
镜像跨 XZ/XY/自身某个面后体积恰好翻倍、包围盒精确反射，线性阵列 Extent/Spacing/沿 Y 三种模式的孔位**从 BRep
实测**（20/35/50、20/45、y=15/30），圆周阵列 270°/4 份的孔落在 (15,0)(0,15)(−15,0)(0,−15)，交付的 FCStd 重开后
`Occurrences` 3→5 多切两个孔、`Suppressed=True` 退回纯底板；缺轴/缺面/错轴名/不存在的面名/空阵列全部是**具名拒绝**。
`circular_pattern` 仍留在 EXPERIMENTAL：这台机器的 `PartDesign::CircularPattern` 是**间距驱动**的
（`NumberCircles`/`RadialDistance`），表达不了"N 份均布在一个角度内"——那正是 `polar_pattern` 的用途。

`fillet` / `chamfer` 转正的经过（`tests/contract/test_fillet_chamfer.py`）：它们选的是**边**，所以测试先用 `ir_digest`
按 `kind`/`length`/`mid`/`direction` 挑出四条竖边（不是硬编码名字），再断言圆角/倒角削掉的体积恰好等于闭式解
`4(1−π/4)r²t` / `4(d²/2)t`（1e-6 相对误差）、包围盒不变、实体数仍为 1；交付的 FCStd 重开后把 `Radius` 从 5 改成 8，
体积按新半径重算。**这两条 op 曾经连一次都编译不出来**：`Base` 是在文档还没 recompute、被引用特征还没有 Shape 时
设置的，于是任何 fillet 都报 "base_feature produced no shape"——同一轮修掉了这个（以及 `ir_digest` 的 `mid` 把
"半个毫米处"当成中点的问题），补上 11 项真内核回归后才改的状态。

`groove` 转正的经过（`tests/contract/test_groove.py`）：轮廓是圆柱壁上的一个环形截面，绕轴整圈旋转切除的体积
有闭式解 `π(R²−r²)·w`；测试断言两个数值不同的样例都精确等于该值、180° 恰好切掉一半、**切不到材料时报错而不是成功**
（编译器给的是 "did not change the solid" 而不是通用失败）、STEP 回读一致、交付的 FCStd 重开后改 `Groove.Angle`
体积变化恰好等于半个环。验收包里的**样例 E** 就是它（8500π → 180° → 8750π）。

`revolution` 从实验性转正的经过（`tests/contract/test_revolution.py`）：PartDesign::Revolution 需要一个
`ReferenceAxis`，而它是 `App::PropertyLinkSub`——JSON 标量表达不了；编译器原来建了对象却从不设轴，
特征要么报错要么什么都不产生。现在 `params.axis`（`V_Axis`/`H_Axis`/`N_Axis` 用草图自身轴，
`X`/`Y`/`Z` 用 body 原点轴）由编译器翻译成那个 LinkSub，并用阶梯轴的解析体积、包围盒、STEP 回读、
FCStd 重开后改 `Angle` 的参数量作为证明；无法识别的轴名是**结构化拒绝**，不是拿一个猜的轴去建。

三条最容易让模型建错、也最容易**静默**建错的语义，已写进工具描述：

- **同一 body 内的特征只有在几何上真的相交时才合并**；`refs` **只声明构建顺序，不产生任何几何关系**。
- 要贴在已有形体上，用 `plane: {kind:"face", feature_id:..., sub:"Face6"}`；构建顺序按依赖解析，
  画在某个特征面上的草图会在那个特征之后才建。
- **草图坐标是世界坐标，且必须落在草图自己的平面内**：XY 用 x/y、XZ 用 x/z、YZ 用 y/z，
  法向那一维被忽略。侧面轮廓（楔形、支架立板、手机支架的斜面）属于 YZ 或 XZ。
  挤出方向随平面法向：`pad` 在 XY 走 +Z、XZ 走 **−Y**、YZ 走 +X（`reversed` / `midplane` 可改）。
  这条以前**从未定义**，编译器把坐标当局部 (u,v) 用，于是**所有非 XY 平面上的草图都静默变成一条线**：
  手机支架的侧面轮廓既建不出实体，也没有任何错误信息（`docs/03` 附录 H）。

约束只接受**实测验证过**的类型与参数形状（`Sketcher.Constraint` 对无法识别的形状不是抛异常，而是直接
segfault，见「已知限制」）。

---

## OpenCode 参考下的运行时加固

- 每次模型请求都计算含工具 schema 的上下文估值，并预留输出空间
- 长回合只移除完整的旧工具轮次；保留已选历史、当前需求、最新 IR/Gate 和最近工具结果
- 同一 IR 上相同失败重复三次后明确停止；修改成功后重新计数，不自动放行权限
- 400/401/确定性配额错误不反复请求；临时失败遵循有上限的 Retry-After
- Gate 通过后再修改，或进入待审批状态，都不能沿用旧结果宣布成功

参考源码、适配取舍和边界见 [Agent 运行时加固](docs/agent-runtime-hardening.md)。

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
> 若此刻正卡在一次 FreeCAD 调用里（编译 / 网格化），这次取消还会**杀掉那个 worker 进程**
> 并丢弃本次未发布的暂存目录——代价与边界见[已知限制](#已知限制与未验证项) #7。

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
| 规格检查（每个 `ConstraintExpr` 一条） | blocking（仅 confirmed） | 把你说的尺寸/数量/位置变成可执行判定。`hole_diameter`/`hole_position` 只认**从 BRep 实测**的孔（`digest.holes`），不读 IR 里声明的 `diameter`——圆草图 Pocket 出来的孔一样被数出来，附离偏差不算孔位 |
| 已确认但测不了 | blocking **ERROR** | 确认过的孔要求却没有 BRep 孔证据时是 `required_but_unverified`（ERROR，不是可忽略的 SKIP，也不是假 PASS） |

| `wall_thickness` | advisory · approximate | 最小壁厚（worker 没测就 SKIP，**绝不编造通过**） |
| `requirement_coverage` | advisory | 无 confirmed 约束时明说"只验证了几何自洽，没验证是否符合要求" |

`passed` = 无 blocking FAIL/ERROR，**且**不是所有 blocking 检查都 SKIP。

### 「这一版验证过没有」是一个持久化事实，不是一个可以推断的结论

`/models/{id}/verdict`（以及产物列表与会话列表里的 `verified` 字段）回答的是**当前版本现在是否已验证**，
由 `tcad/store/artifacts.build_verdict` 从两个必须同时成立的事实算出：

1. 落盘的 `GateReport` 是**当前版本**的，且 `passed`；
2. 该版本的产物**确实已经发布**（第 2 轮的发布门只在通过后才写版本目录）。

任一单独成立都不算 verified，返回的 `reason` 会说明是哪一条不成立。这样做的原因是：`ir_commit` 通过之后
若又发生了写操作，模型已经前进到新版本，而旧版本的报告**仍然真的**是 passed——把旧报告读成"当前通过"
就是陈旧的绿灯被显示成实时的绿灯。`verified` 只有在两者都成立时为真，陈旧的通过永远回不来。

---

### 交付是"通过才发布"，不是"写进同一个目录再检查"

每次构建先写进一个**私有暂存目录** `artifacts/<id>/v<N>.staging-<attempt>/`，Gate 只对**这个目录**评分；只有整次构建通过，才用两次 `os.replace` 把它整体换进 `v<N>/`，并写一份 `manifest.json`（逐文件 sha256 + 字节数 + attempt_id）。

三件由此变成结构性事实、而不是靠时间戳猜测：

1. Gate 看到的分区里**只有本次尝试写的文件**——上一次失败留下的 STEP 不可能替本次缺失的导出顶包，因为它根本不在被评分的目录里。
2. 失败的尝试**从不发布**：暂存目录被丢弃，`v<N>/` 保持上一次已验证的构建原样，"恢复最后一个好版本"就是"什么都不做"。
3. 崩溃在两次 rename 之间也不会丢版本：旧树先被改名到 `.v<N>.previous`，下一次尝试的 `recover_publish` 会把它换回来。

> 相关测试：`tests/unit/test_artifact_publish.py`（11 项）、`tests/contract/test_wired_pipeline.py` 第 6 节（4 项真内核）。
> 这 4 项在"把 artifact_dir 换回版本目录"的变异下全部变红——它们真的在测这条性质。

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
│                  ids.py（model_id/导出名/导入路径的合法性 + 目录包含校验）
│                  worker_client.py（supervisor 侧的 worker 句柄）
├── loop/          engine.py（Turn/Step 状态机）· strategies.py（M1 循环/M2 分叉/M3 对抗）
│                  budget.py（熔断上限，None = 不设）· commit.py（ir_commit→编译→Gate→回灌）
├── tools/         base.py（ToolSpec/ToolResult/权限档 + build_default_registry + 参数 schema 强制）
│                  schema_check.py（stdlib 迷你 JSON-Schema 校验器）
│                  ir_tools.py · geo_tools.py · privileged.py
├── context/       assembler.py（预算装配 + 三档降级）· digest.py（几何摘要投影，含稳定特征 id）· compactor.py
│                  requirements.py（需求合同投影）· verdict.py（上一轮 Gate 结论）· history.py（会话历史回灌）
├── store/         ir_store.py（版本化快照 + 乐观并发 + **按模型的写入串行化**）· event_log.py（append-only jsonl）
│                  session_db.py（SQLite/WAL：threads/turns/steps/messages/approvals）
│                  artifacts.py（digest/时间戳/**暂存→通过后原子发布 + manifest**/当前verdict）
├── hooks/         dispatcher.py（确定性合并，fail-closed）· policy.py · approval.py
├── verify/        gate.py · checks_solid.py · checks_spec.py · specexpr.py · context.py
├── ir/            schema.py · patch.py · naming.py（稳定命名）· validate.py
│                  capability.py（FeatureOp 能力真相表：VERIFIED / EXPERIMENTAL + 缺口）
├── worker/        跑在 FreeCADCmd 进程内：bootstrap.py · rpc.py · compiler.py
│                  introspect.py · mesh.py · exporters.py · reopen.py（重开 FCStd 改参再测）
│                  protocol.py · selftest.py
├── render/        仅 supervisor 侧：camera.py · raster.py（numpy z-buffer）· png.py（Pillow 或纯 stdlib）
├── llm/           client.py（OpenAI 兼容 + 重试 + token 计数）· hotswap.py（原地换模型）
├── server/        app.py（FastAPI + SSE）· cli.py · ui/（index.html · app.js · styles.css）
└── config/        schema.py · loader.py · providers.py（供应商预设）· settings.py（运行时设置）

tools/             serve.py · stub_llm.py（离线模型替身）· agent_driver.py（操作者即 LLM）
                   render_sample.py · doctor.py（环境诊断）· build_acceptance_artifacts.py（验收产物）
                   probes/ · sessions/（脚本化轨迹）
configs/           default.yaml · policies/strict.yaml（有界档）
data/              运行期数据（.gitignore）：models/<id>/{v*.json,events.jsonl} · artifacts/<id>/v<N>/
                   （含 manifest.json 产物清单；失败的尝试只写 v<N>.staging-<attempt>/ 且不发布）
                   gate_reports/<id>/v<N>.json（上一轮 Gate 结论）· tcad.sqlite3
docs/              01-需求澄清问卷 · 02-架构设计 · 03-交互界面与模型配置 · renders/
review/            AUDIT_AND_FIXES_ZH.md（审阅报告）· acceptance/（可下载的验收产物 + manifest）
tests/             unit/（753）· contract/（155，真跑 FreeCADCmd）· e2e/（4，需真实模型服务）· fixtures/
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
| `GET` | `/sessions` | 会话列表：标题、`model_id`、`ir_version`、`verified`、最近活动时间 |
| `POST` | `/sessions` | 新建会话：同时建它的模型（`model_id` 已存在 → 409） |
| `GET` | `/threads/{id}/messages` | 会话历史（刷新不丢） |
| `POST` | `/models` | 播种一个空 IR 模型（已存在 → 409） |
| `GET` | `/models/{id}/ir` | IR 快照（可 `?version=`） |
| `GET` | `/models/{id}/render` | **人的**视图：`?view=iso\|front\|top\|right`，按 (版本, 视图, 风格, 尺寸) 磁盘缓存 |
| `GET` | `/models/{id}/artifacts[/path]` | 产物列表 / 下载 PNG·STEP·STL（列表带 `verdict`） |
| `GET` | `/models/{id}/verdict` | **这一版到底验证过没有**：`verified`/`passed`/`graded_version`/`blocking_failures`/`reason`（可 `?version=`） |
| `GET`/`POST` | `/approvals[/{id}]` | 待批审批（含工具、会话、以及**将要执行的内容摘要**）/ 批准或拒绝 |
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
.venv/bin/python tools/doctor.py                     # 三层前置体检：supervisor / 几何内核 / 模型服务
.venv/bin/python -m pytest tests -q                  # 包含真实内核契约测试（需 TCAD_FREECAD_CMD）
.venv/bin/python -m pytest tests/unit -q             # 不需要 FreeCAD
.venv/bin/python -m pytest tests/contract -q         # 真跑 FreeCADCmd
node --test tests/frontend/*.test.mjs                # 相机 / 控件 / 异步加载运行时回归
```

第 3 层（真实 LLM 端到端）需要明确配置的 provider，未配置时**跳过而不是假装通过**：

```bash
export TCAD_E2E_BASE_URL=http://127.0.0.1:8000/v1 TCAD_E2E_MODEL=<一个会用工具的模型>
.venv/bin/python -m pytest tests/e2e -q              # 未预写 IR：自然语言 → 真实工具调用 → 真实 FreeCAD
```

验收包（样例 A/B/C/D/E 的真实 FCStd/STEP/STL + 四视图预览 + IR + 需求合同 + 逐个文件 sha256 清单）由内核现做现测；
manifest 里还有一份 **源码树摘要**（`tcad/`+`tools/`+`tests/` 下所有 `.py` 的排序 sha256），用来回答"这个包是当前代码建的吗"——本机 `git` 不可用时它就是版本坐标：

```bash
.venv/bin/python tools/build_acceptance_artifacts.py # 写入 review/acceptance/，任一数字不合就中止且不写清单
.venv/bin/python tools/build_acceptance_artifacts.py --check  # 包与当前源码树同源 → OK(0)；改过源码 → STALE(1) 并提示重建
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

`tests/unit/test_server_ui.py` 保留源码结构断言；`tests/frontend/` 使用 Node 内置测试器
验证相机数学、真实事件处理函数和异步加载竞态，不增加运行时依赖或构建步骤。
WebGL 着色器和浏览器实际拖拽仍需真实浏览器验收，不等同于 Node 测试。

---

## 前端的异步会话切换

切换会话时 `switchSession` 会先 `state.sessionEpoch += 1`。三个异步加载器（`refreshInspector` / `loadArtifacts` / `loadView`）
在第一个 `await` **之前**取一份 `sessionToken()`，之后每次 `await` 回来都检查 `stale(token)`，过期就直接返回：

- 否则，你刚离开的那个会话的慢响应会落进你刚打开的会话的面板里，而且还会被贴上**新** model id 的标签
  （产物链接尤其明显：文件是旧模型的，链接指向新模型）。
- 产物链接一律由**响应所描述的那个身份**（token）拼出，不用 `state.modelId`。
- 面板里的「验证状态」直接显示后端的 `GET /models/{id}/verdict`（已验证 / 未验证 + 原因 + 阻断项），
  不从"有文件"推断"已完成"；侧边栏会话列表也区分 `v5 ✓` 与 `v5 ✗`。

前端使用 Node 内置运行时测试 + 源码结构断言 + `ApprovalRecord` 接口契约断言，
另外加一条 `node --check`（有 node 时执行，没有则跳过）——它抓的是源码断言抓不到的那一类错误：文件根本不是合法 JS。

---

## 贴面草图：能力要能兑现，名字要能查到

`plane: {"kind":"face","feature_id":"ft_plate","sub":"Face6"}` 一直是 IR 的一部分，编译器也一直支持——
但**没有任何真内核测试量过它**，而且工具描述写着「用 ir_get / ir_digest 查面的编号，不要猜」，`ir_digest`
却只给了一个**面的数量**。也就是说：指令本身无法执行。

现在两件事都补上了：

- **面清单**：`GeometryDigest.faces`（`FaceInfo`）由 worker 从真实 BRep 读出，每条含 `name`（FreeCAD 的
  1-based `Face<N>`）、`area`、外法向 `normal`、`center`。只列**平面**（曲面无法承载 FlatFace 附件），
  上限 32 条，并在 `ir_digest` 渲染的文本里逐条打印——所以模型能按意图挑（"面积 3200 的 +Z 面"），
  而不是赌一个会随模型变化而移动的编号。
- **真内核证明**（`tests/contract/test_face_attachment.py`）：贴到顶面 Face6 的凸台体积精确等于
  `底板 + πr²h`、包围盒 z 精确等于 `t + h` 且不外扩；换成侧面 Face1 用同一份坐标，材料**沿该面法向**
  长出（x 不变、y/z 变化）——这正是描述里"贴面草图在该面自己的坐标系里解释"的含义；两个数值不同的
  尺寸组合同样精确。

**顺带修掉一个会把人带偏的诊断**：面名写错时 FreeCAD 不会报错，而是让草图保持未附着，于是轮廓在自己
的坐标系里塌陷，构建失败信息变成"轮廓不闭合……检查世界坐标"——把模型引向完全错误的方向。现在
校验器在落盘前就检查 `kind="face"` 的 `feature_id` 存在且 `sub` 非空（`face_target` / `face_sub_missing`），
编译器在真内核上检查 `sub` 是否真的存在，报错点名该面并列出**可用的面名**。

---

## 参数白名单的意思是「编译器兑现得了」

`_VERIFIED_OP_PARAMS` 里的一个键，过去只要**同名属性存在**就会被列进去。但 `_assign_props` 是用
JSON 标量去 `setattr`，而这些属性里有 `App::PropertyLinkSub` / `LinkList` / `Part::Shape` / `Vector` ——
标量根本设不进去。实测（FreeCAD 26.3.0 / 48708）：

```
params={'length': 5.0, 'up_to_face': 'Face6'} -> raised
TypeError: type must be 'DocumentObject', 'NoneType' or ('DocumentObject',['String',]) not str
```

而且这发生在**补丁已经落盘、worker 往返已经花掉之后**。现在白名单只保留 `_assign_props` 真的能设的键
（Length/Distance/Angle/Bool/Enumeration/Float/Integer/String/Percent/*Constraint），
碰到被移除的键是**校验错误**并点名替代写法（`param_unsupported`），在落盘之前就拒绝。

分类是**量出来的**，不是猜的：`tests/contract/test_param_capability.py` 在真内核上创建每种 PartDesign 对象，
对白名单里每个键读 `getTypeIdOfProperty`，并要求它落在可设置的集合里；`tests/unit/test_param_capability.py`
带一份同样的静态表，让这条守卫在快速单测层也生效。同名键在不同对象上含义不同的事实也被测到：
`Base` 在 Fillet 上是 `LinkSub`、在 Revolution 上是 `Vector`（基点），`DepthType` 在 Hole 上有、在 Pad 上没有
——所以拒绝文案是按 **(op, key)** 给的，并且测了一条不变量：**Vector 类型的拒绝必须说 "Vector"，不能写成"边引用"**。

---

## 工具参数 schema 是契约，不是说明

每个 `ToolSpec.params_schema` 都会作为 function declaration 发给模型，并且**在服务端真正执行**
（`tcad/tools/schema_check.py`，纯 stdlib）。一个不符合声明的调用在**进入 handler 之前**就被拒成
结构化 `SCHEMA` 错误并点名路径（`arguments.views: expected array, got str ('iso')`）。

- 支持的关键字是一个**封闭集合**（`type`/`required`/`properties`/`items`/`enum`/`anyOf`/`oneOf`/`$ref`/`$defs`/`additionalProperties`），
  单测会遍历**所有已注册工具**的 schema，断言没有出现集合外的关键字——否则 schema 又会变成部分装饰。
- `type: integer` 不会接受 `True`（Python 的 bool 是 int 的子类，这里显式排除）。
- 声明必须与实际行为一致：`ir_patch` 的 `base_version` 同时接受整数与文档里的 `"current"`，
  schema 也如实声明 `anyOf`——否则强制之后会把一个受支持的写法拒掉。

---

## 标识符与路径边界

`model_id` 来自 HTTP body / URL 路径，`asset_export` 的 `name` 来自**模型自己**，两者过去都被直接拼进文件路径。
`tcad/core/ids.py` 是唯一判定处：

- **形状规则**（主规则）：合法标识符只能是 `[A-Za-z0-9]` 开头、后接字母/数字/`.`/`_`/`-`、长度 ≤64。它**在构造上**就不可能含有分隔符或 `..`，所以不可能指到父目录之外。
- **包含校验**（纵深防御 + 真正收路径的地方）：`contained_path()` / `ensure_contained()` 对**解析后**的路径做包含判断，`..` 与符号链接先被折叠——字符串比较才是看起来对、实际不对的那种写法。
- 落点：`IrStore._model_dir`、`ArtifactStore.dir_for`、`gate_report_path`（store 层，最后一道）、`POST /models` / `POST /sessions` / `/chat` 的请求模型（pydantic → 干净 422，而不是深处 500）、`asset_export` 的导出名、`asset_import` 的导入路径（限定在 `workdir` 与 `data_dir` 之内）。
- worker 侧 `exporters.py` 有一份**镜像规则**（worker 禁止 `import tcad.core`），`tests/unit/test_ids.py` 用 AST 读它的正则与 supervisor 版本逐项比对，防止两边漂移。

---

## 已知限制与未验证项

不遮掩，这些是当前的真实边界：

| # | 限制 | 说明 |
|---|---|---|
| 1 | **跨回合延续对话，但不回灌工具轨迹** | 每个 Turn 现在会装配受预算约束的上下文：相关历史（`session_db`）、需求合同、当前 IR 摘要（含稳定特征 id）、当前版本、以及上一轮 Gate 结论（`tcad/context/*`）。仍**不**回灌历史工具调用与结果——那会让模型只看到自己的旁白而看不到工具产出；几何事实由摘要块承担，无需从旁白里猜。历史超窗时走三档降级，但**未注入 LLM 摘要器**：被压缩的旧历史落成一条显式占位消息，而不是被静默丢弃。 |
| 2 | **真机只在 DeepSeek 上验证过** | function calling 与 thinking 并存的真实行为、`probe_llm` 对真实供应商，只在 `deepseek-v4-flash` 上跑过。 |
| 3 | **`context.window_tokens` 是估值** | 128000 未按真实 token 标定，三档降级（0.70 / 0.85）的阈值因此不准。 |
| 4 | **界面无认证** | 默认只绑 `127.0.0.1`；绑非回环地址时启动会打印警告。不要暴露到公网。 |
| 5 | **无会话重命名 / 删除 / 搜索**，不做会话内换模型 | 会话↔模型绑定单向是有意的。 |
| 6 | **多标签页仍未协调（但有界）** | 每个请求的 hooks/视觉状态**已经隔离**：观测用的 tap 与 `geo_view` 检查点走 `ToolContext`，不再改写共享的 `services.hooks` / `services._visual_ok`（§5-E）。同一模型的并发写入也已按模型串行化（`IrStore` 的 load→apply→append→snapshot 是一个临界区，§5-D）。**当前增加了同会话 / 同模型的回合互斥**：在飞回合结束前，新 `/chat` 返回 409；注册竞态返回明确的 SSE 冲突。断连清理完成前保留占用。同一个 `request_id` 起第二个回合会被 409 拒绝。跨进程写同一个 `data_dir` 也不支持（进程内锁，不是文件锁）。 |
| 7 | **打断靠杀进程，不是协议级取消** | 在飞的 LLM 请求会被真的取消；已经发给 FreeCAD worker 的一次调用（编译 / 网格化）现在也**真的会停下来**：`WorkerHandle.abort_inflight` 杀掉并回收 worker 进程，被阻塞的调用方立刻拿到 `kind="cancelled"`（`WorkerAborted`），而不是等超时、也不再被误报成崩溃；被放弃的暂存目录同时丢弃，下一次调用按需重启 worker，所以"停止"的代价就是被停的那次构建。**代价与边界**：worker 协议本身没有中途取消，停的是**进程**；一个服务进程只有**一个** worker 进程，所以它也会终止同时使用它的其他调用的 RPC——正常情况下同一模型的并发写已被 `IrStore` 串行化，但跨模型的并发构建会互相牵连，各自收到 `cancelled` 并各自重启。OCCT 卡死的进程同样只能这样丢弃，不能指望它恢复。 |
| 8 | **`Sketcher.Constraint` 对无法识别的形状会 segfault** | 不是抛异常，是原生崩溃（SIGSEGV，26.3.0dev 实测）。所以编译器只构造**实测验证过**的类型/参数组合，其余一律结构化拒绝（`tests/contract/test_sketch_planes.py`）。**代价**：`Radius`/`Diameter` 只能用「值写在构造函数里」的形式（它们的 refs-only 形式正是会崩的那种），而那种形式下 FreeCAD 不做冗余校验——「同时标半径和直径」这类过约束不会被判为 solver 错误。其余维度约束（`DistanceX/Y` 等）仍走校验路径。 |
| 9 | **worker 崩溃/卡死靠替换，不是修复** | `worker_restart_on_crash`（默认开）现在真的会生效：崩溃或超时后杀掉旧进程、拉起新的，所以坏调用只毁掉**一次**调用。用户中断走同一条路（见限制 #7），服务器退出时（含 Ctrl-C）后端也会被关闭，不留孤儿进程。 |
| 10 | **不做的范围** | GUI 交互建模 / 自由曲面造型 / 装配约束求解 / 2D 工程图 / 仿真 / CAM / 多用户协作。 |
| 11 | **本机无法 `push` 到 GitHub** | 代理不转发 `receive-pack` 的响应流（`ls-remote` 正常）。需要换代理或加 SSH key。 |

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
