# 交付说明：对话式 3D CAD 工作台

本文件是这一轮改造的交付记录：接口、依赖与构建、快捷键、验收逐条的结论，
以及**如实区分**的 已实现并验证 / 已实现但受环境阻塞 / 尚未实现。
凡是没有真跑过的，这里不会写成已验证。

上一版把「像素级浏览器验收」和「FreeCAD 内核」都标成了 BLOCKED。这一版把
**这两条都解开了** —— 前者是因为拿到了真实视口的 Chromium，后者是因为发现
之前的判断本身是错的（见 §五.0）。解开之后立刻暴露出五个此前一直藏在
「看起来对」下面的真缺陷（§六）。

---

## 〇、本轮最重要的变化：验收从「断言」变成了「测量」

上一轮唯一能用的浏览器上报 0×0 视口，所以「模型能转、能看到背面」只能写
成文字。这一轮用 Playwright 驱动真实 Chromium（1366×768 / 1440×900 /
1920×1080，真实 WebGL 2.0），把它量了出来：

```
139/139 browser checks passed
像素级：12/12 通过        ← 用 Pillow 独立分析截图，与浏览器脚本互不依赖
```

三条最能说明问题的测量 **（不是读代码得出的，是数出来的）**：

| 测量 | 方法 | 结果 |
| --- | --- | --- |
| 屏幕上画的三角形 == worker 网格的面片 | 在页面脚本之前 patch 浏览器的 `drawElements` / `drawArrays`，按 mode 记数 | 只有实体时每帧 `elements/TRIANGLES/96`，而网格载荷是 32 面片 ×3 = **96** |
| 拓扑边线不是三角剖分边 | 切到「着色+边线」再数 | 多出一次 `arrays/LINES/48`，而 BRep 边 24 段 ×2 = **48**；「着色」模式下这个数确实是 0 |
| 沿孔轴能看穿，横着看被挡住 | 对截图做背景色洪泛，数**深入孔内**（2px 边距内没有背景可达像素）的背景像素 | 顶视 20255 px / 前视 **37 px**；换到偏心孔的第二块零件：顶视 12134 px / 前视 35 px |

这些都是「一张图片怎么变换都做不出来」的量：一张图不可能同时是「顶视有洞」
和「前视没洞」。

---

## 一、接口一览（本轮新增或改动）

### 运行（阶段 C，§8）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/chat` | 创建一次运行并返回它的 SSE。请求体新增 `object_ref`；同一会话已有运行 → 409；重复 `request_id` → 409 |
| GET | `/runs/{run_id}` | 权威快照：`state / last_seq / started_at / saved / note / resumable` |
| GET | `/runs/{run_id}/events?after_seq=N` | 从游标读事件（只读，不触发执行） |
| GET | `/runs/{run_id}/stream?after_seq=N` | 重连：先重放再跟随；**不重发 `/chat`** |
| GET | `/runs` | 本进程仍在进行的运行 |
| GET | `/threads/{id}/events?after_seq=N` | 一个会话的完整事件史 |
| POST | `/chat/interrupt` | 停止：返回 `stage`、`reaches_engine`、`saved` |

事件帧的形状：`event: <type>` + `data` 为事件负载本身，另附 `run_id / thread_id / seq`。
`seq` 是重连去重的依据；SSE 结束**不等于**成功——只有 `result` 帧才是结论。

### 会话管理（阶段 C/D，§7）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/sessions?view=active\|archived\|trash&query=&limit=&offset=` | 视图 + 标题/正文搜索 + 分页；每行带 `pinned / unread / run_state / live_run / ir_version / verified` |
| PATCH | `/threads/{id}` | `{title?, pinned?}`；空标题恢复自动标题 |
| POST | `/threads/{id}/archive` | **本轮修复**：现在同时接受 JSON body `{"archived": false}` 和查询参数 `?archived=false`（见 §六.2） |
| POST | `/threads/{id}/trash` | `{trashed: bool, stop?: bool}` |
| POST | `/threads/batch` | `{action, thread_ids[]}`，逐个报告 `done/skipped` |
| POST | `/threads/{id}/read` | 标记已读 |
| POST | `/threads/{id}/purge` | `{confirm: <thread_id>, delete_model?}`；二次确认 + 共享模型检查 + 清理报告 |
| GET/POST | `/maintenance/cleanup` | 列出/重试永久删除时没删掉的文件 |

被移入回收站的会话，`GET /threads/{id}/messages` 返回 **410**。

### 对象引用与参数（阶段 D，§9）

`POST /chat` 的 `object_ref` = `{model_id, ir_version, object_id}`；版本已推进
→ 409「旧选择已过期」。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/models/{id}/params` | `{ir_version, edits:[{feature_id, param, value}], reason?}`。按**提交时那一版**改参数，走与回合相同的 `run_commit`：校验 IR → 补丁进 IR（产生 v+1）→ 编译 → 导出 → Gate。回答带 `ok / based_on / ir_version / applied / gate / compile / note` |

未通过时不发布：`ok:false`，`note` 明确写「没有发布」并指出界面应保持在哪个
版本；通过的那一版才会有 step/stl/FCStd 落到磁盘。

### 其余（既有）

`/health`、`/models*`（IR / artifacts / versions / verdict / mesh / render）、
`/settings/*`、`/attachments*`、`/approvals*`。

---

## 二、依赖与构建

- **后端**：Python ≥ 3.11；`pydantic>=2.7`、`pyyaml`、`numpy`、`pillow`、`openai`；
  可选 `fastapi`、`uvicorn`（`pip install -e ".[server]"`），开发用 `pytest`、`pytest-asyncio`、`httpx`。
- **前端**：无构建步骤。`tcad/server/ui/` 下的 HTML/CSS/JS 由 FastAPI 直接以 `Cache-Control: no-store` 提供。
  第三方库全部**本地落盘**（Three.js 0.186.0 + OrbitControls、markdown-it 14.1.0、DOMPurify 3.2.6），
  经 import map 引用；仓库里有一条守卫测试断言前端不出现任何外部 URL，所以不可能悄悄引入 CDN。
- **浏览器验收（开发时依赖，锁版本）**：`playwright@1.63.0` + Chromium 153.0.8010.12。
  它只用于验收，运行时不需要。安装到隔离目录，不污染项目 venv：

  ```bash
  cd /Users/slg/.workbuddy/binaries/node/workspace \
    && npm install playwright@1.63.0 --registry=https://registry.npmmirror.com
  PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright \
    npx playwright install chromium chromium-headless-shell
  ```
  `tools/browser_acceptance.py` 会自己去 `TCAD_PLAYWRIGHT_DIR`（默认上面那个目录）
  找它；找不到就明确报错，不会假装通过。

### 怎么跑起来

```bash
.venv/bin/python -m pytest tests/unit -q        # 958 passed, 2 skipped（42s）
.venv/bin/python -m pytest tests/contract -q    # 181 passed —— 真跑 FreeCADCmd
.venv/bin/python tests/manual/live_param_edit.py    # 14/14：参数改动的契约
.venv/bin/python tests/manual/live_param_kernel.py  # 21/21：参数改动 → 真内核重建 → 发布
.venv/bin/python tools/browser_acceptance.py    # 139/139 浏览器 + 12/12 像素
.venv/bin/uvicorn tcad.server.app:create_app --factory --port 8000
```

`browser_acceptance.py` 一条命令做完三件事：用真内核建出夹具零件
（`tools/browser_fixture.py`）、起真实 uvicorn、驱动 Chromium，最后用 Pillow
分析截图。产物落在 `review/browser/`（截图 + `browser-results.json`）。

---

## 三、快捷键

| 按键 | 作用 |
| --- | --- |
| `Ctrl/Cmd+K` | 命令面板：新建/切换/搜索会话、适应模型、重置视角、六个预设视角、正交↔透视、着色/边线/三角线框、网格、坐标轴、主题、侧栏、检查器、专注模式、附件、导出、版本、截图 |
| `Ctrl/Cmd+I` | **本轮新增**：显示/隐藏检查器（等价于顶栏 ▤ 按钮） |
| `Enter` | 发送（组合输入法组字期间不发送；`Shift+Enter` 换行） |
| `Ctrl/Cmd+Enter` | 发送（可配置） |
| `Esc` | 优先关闭当前弹层（菜单 → 对话框 → 命令面板），不误停别的会话 |
| `Ctrl/Cmd+B` | 折叠/展开会话列表 |
| 方向键 + `Enter` | 命令面板内选择与执行 |
| `F` `R` `1`–`6` `0` | 适应 / 重置 / 六个预设视角（输入框有焦点时不触发） |

---

## 四、验收逐条的结论

| # | 验收项 | 状态 |
| --- | --- | --- |
| 1 | 真实 3D 旋转/缩放/平移、遮挡变化，相机操作不触发 `/render`、LLM、重建 | **已实现并实测（像素级）**：旋转 15.4%、缩放 9.2%、平移 24.5% 的像素变化；顶视 20255 px 能看穿通孔、前视 37 px 被挡住；相机操作期间 `/models/*` 与 `/chat` 各 **0** 次请求；且 GPU 实际绘制数 == 网格面片数 |
| 2 | 正交/透视、预设视角、全屏、面板拖拽、单位方向、更新不重置相机 | **已实现并实测**：三种断点（1366/1440/1920）都量过，无横向溢出，画布中心无面板遮挡；拖拽分隔条 240→330 且刷新后保持；HUD 显示「单位 mm · Z 轴向上 · 正在查看 v4」 |
| 3 | A/B 会话切换不串模型/参数/Gate/文件 | **已实现并实测**：把 A 的 `/mesh` 故意延迟 2.6s 再切到 B，视口停在 `acc-plate v1`，迟到结果到达后**没有**覆盖回来 |
| 4 | Markdown 表格/代码/附件正确；恶意 HTML、危险链接、远程跟踪图不外发 | **已实现并实测**：敌意文本写进数据库后渲染——表格/代码块/任务框都在，`<script>` 0 个、`onerror/onclick` 0 个、`javascript:` 链接被剥、`window.__pwned` 为 null、对外部主机 **0** 次请求 |
| 5 | 中文输入法不误发；草稿/附件/发送失败可恢复；重复请求不重复修改 | **已实现并实测**：`compositionstart` + `isComposing=true` 的 Enter 之后文字保留且 `/chat` 0 次；草稿按会话隔离；重复 `request_id` 由服务端拒绝 |
| 6 | 上传 .md，模型用文件里的真实尺寸 | **已实现并验证**（阶段 B）：请求体里是带行号的文件内容，不是文件名 |
| 7 | 单图/多图真实图片建模，证据是请求真的带图 | **已实现但受环境阻塞（BLOCKED）**：能力探测/真实像素入请求/拒绝时的显式提示都验过；**本机三个模型都不是视觉模型**（Ollama 的 `capabilities` 里没有 `vision`），所以「图片→CAD」这一步没跑过，不声称跑过 |
| 8 | 纯文本模型收到图片有明确提示，不丢图、不擅自换云服务 | **已实现并验证**：本机 Ollama 纯文本模型跑通——每次请求都不带图片，请求里写明「这些图片没有发送给模型」，附件原样取回，`tests/manual/live_vision_text_only.py` 16/16 |
| 9 | 归档/删除/恢复/重命名/置顶/搜索持久化，重启后一致 | **已实现并实测**：真实浏览器里逐项做过，并且**每一项都再问一次服务端**（不只是看 DOM）。本轮因此抓到两个真 bug，见 §六.1 / §六.2 |
| 10 | A 运行时切 B，A 继续；刷新重连同一运行不重复写；停止/预算/重启中断正确 | **已实现**：运行与连接解耦、游标重连、停止到达引擎、重启后标记「已中断，可重试」。单元测试覆盖 |
| 11 | 历史版本一致；新版本失败不被旧版本掩盖 | **已实现并实测（本轮补上内核段）**：`live_param_kernel.py` 21/21。违背已确认需求的重建 → `ok:false`、Gate `passed:false`、**没有 step/stl/FCStd 产出**、`note` 写明「请继续查看上一次成功构建 v1」；没有确认需求时同样的改动 → `ok:true`、Gate 通过、v2 产物落盘、`/mesh?version=2` 给的是新版本真几何 |
| 12 | 有映射时模型↔树双向选择；过期引用不误改 | **已实现并实测**：模型→树、树→模型两个方向都量过；「隐藏」之后 GPU 绘制数 **360 → 0**，「显示」后回到 360；对象引用带 `body:body_1 · acc-bracket v4` |
| 13 | 旧库迁移不丢会话/消息/IR/产物；纯文本 API 仍可用 | **已实现并验证**：手工造旧 schema → 新代码打开后列补齐、数据完好、可重复打开；`/chat` 纯文本入口保持兼容 |
| 14 | 断网可用；反复切换不泄漏资源 | **已实现并实测**：拦掉所有非本机请求后，界面/三维/Markdown 全部照常（被拦截的外部请求 **0** 次，说明没有任何东西依赖 CDN）；6 次切换后画布仍是 1 张，堆增长 0.6 MB |

---

## 五、明确尚未实现 / 受环境阻塞

0. **「本机没有 FreeCAD」这个结论是错的，本轮撤回。** 之前写这一条时没有去跑
   它：`free-cad/FreeCAD/build/debug/bin/FreeCADCmd` 一直都在（FreeCAD 26.3.0），
   `tools/doctor.py` 现在也报 RUNNABLE。真正的拒答原因是那个种子 IR 没有草图、
   编译不出实体 —— 而把这个当成环境问题，让「参数重建的发布」整段时间都没被
   验证过。现在 `tests/unit` 958 + `tests/contract` 181（真内核）都跑过，
   `live_param_kernel.py` 也补上了发布那一半。**教训写在 §六.0。**

1. **真实图片→CAD（§6 验收第 7 条）**：本机没有视觉模型，也没有可用的视觉服务额度。
   能力探测、真实像素入请求、拒绝时的显式提示三段都用真实 HTTP 证据验过，
   唯独「看图建模」这一步没跑过 —— 不声称跑过。

2. **`tests/e2e` 用本地小模型跑不动**：全量 `pytest tests` 会卡在
   `test_real_llm_sample_a.py`（2.5B 的 `minicpm5-2b-32k`，一轮回合约 15 分钟没有进展）。
   这与本轮改动无关，但意味着**全量一条命令跑不完**；分层跑（unit → contract → e2e）
   是可以的。想跑 e2e 需要一个能真正驱动工具循环的模型。

3. **沙箱导致的两个环境假象**（都不是代码问题，但会浪费时间）：
   - pytest 的 `--basetemp` 落在工作区时，删除守卫会拦住清理（>50 项），
     表现为几百个 ERROR。改用 OS 临时目录（`--basetemp=/tmp/...`）即可。
   - 沙箱的 `mkdir` broker 不尊重 `exist_ok=True`，重复建同一目录会抛
     `PermissionError(EEXIST)`。已让 `test_ids.py` 的夹具先判断存在性。

4. `/render` 的光栅图接口仍保留，视口已不依赖它。
5. 剖切、精确 BRep 测量、装配爆炸图、多人协作、桌面安装包：按任务书属于后续扩展，未做。

---

## 六、这一轮修掉的「看起来对但不对」

按发现顺序。前两条是**按钮接不上后端**，后三条是**界面对自己说了谎**。

0. **把环境问题当成原因，是一种更贵的错。**「本机没有 FreeCAD」被写进了交付
   文档、也写进了测试的 docstring，于是没人再去跑它 —— 一个可以验证的能力被
   当成不可验证的，整整一个阶段。真正的原因是种子 IR 没有草图。**先跑，再下结论。**

1. **前端的 `api()` 把对象 body 原样交给 `fetch`。** `fetch` 只接受 BodyInit，
   其他东西被强制成字符串，于是 `body: {title: "x"}` 以 `[object Object]` 加
   JSON 头发出去 → 422。六个会话操作（重命名、置顶、归档、回收站、恢复、
   永久删除）**全部是死按钮**，而端点本身是好的。文件里另外十处调用都手写了
   `JSON.stringify`，所以唯一那个入口恰恰是最不像错的地方。现在在 `api()` 里
   序列化一次，并有守卫测试 `test_the_only_http_entry_point_serialises_an_object_body`。

2. **`POST /threads/{id}/archive` 把 `archived` 声明成了查询参数。**
   前端发的是 body，于是 `{"archived": false}` 被忽略、默认值 `True` 生效 ——
   「取消归档」等于再归档一次。而 live 测试用的是 `?archived=false`，所以它是
   绿的。**两端各自读各自的文档都能自圆其说，只有真实点击能发现。** 现在两种
   都接受，并有两条测试分别钉住 body 形态和 query 形态。

3. **检查器抽屉默认展开，盖住了模型的中间。** 在 1440×900 —— 最常见的笔记本
   尺寸 —— 抽屉宽 420px 而画布只有 623px 宽，OrbitControls 监听在 canvas 上，
   所以**在零件正中间拖动完全没有反应**。这是浏览器验收量出来的：抽屉打开时
   一次拖拽产生 **0 次 WebGL 绘制**。现在窄屏默认收起（除非用户显式打开过），
   顶栏 ▤ 按钮或 `Ctrl/Cmd+I` 打开；打开时它从视口工具栏下方开始，不再盖住
   工具栏。验收里新增了一条持续守卫：三个断点下都要断言「画布中心最上层元素
   就是 canvas」。

4. **`hidden` 属性被 CSS 盖掉。** `.object-tag` 设了 `display: flex` 却没有
   `[hidden]` 规则，于是空的「本条消息针对：」框一直挂在输入框上；更糟的是它
   让 `#objectTag.isHidden()` 返回 false，一次「单击选中了对象」因此**假通过**。
   现在加了一条全局 `[hidden] { display: none !important; }`，让这类问题不可能
   再靠「每个元素各写一条」来防。

5. **视口选中对象不生成聊天上下文标签。** 对象树的点击会 `setObjectTag()`，
   视口的点击只更新了 HUD 和检查器 —— 于是「点一下这个孔，然后说『把它改大』」
   什么都不带，而从树里点同样的东西却好用。现在两条路一致；点空白处清除引用。

6. **`window.prompt` 换成应用内对话框。** 重命名和永久删除之前用浏览器原生
   prompt：不能同时展示影响范围和输入框、不跟随主题、焦点交还给文档而不是
   触发按钮、并且**无法被测试驱动**（它正是唯一没有覆盖的交互功能）。现在走
   `promptDialog()`，Enter 提交 / Esc 取消，关闭后焦点回到触发它的按钮。

7. **列表重建会偷走焦点。** 每次会话操作后 `renderSessions()` 重建整列，如果
   焦点在某一行（或对话框刚还回来的 ⋯ 按钮上），节点被替换后焦点掉到 `<body>`。
   现在按 `data-thread` 记住是谁，重建后还回去；焦点不在列表里时不抢。

---

## 七、怎么复现浏览器验收（以及它到底证明了什么）

```bash
.venv/bin/python tools/browser_acceptance.py            # 一条命令
.venv/bin/python tools/browser_acceptance.py --keep-server   # 留着服务手动看
```

它做四件事，每一步的产物都留在 `review/browser/`：

1. **建夹具**（`tools/browser_fixture.py`）：两块零件，都由真内核经真工具面
   建出来并通过 Gate —— `acc-bracket`（80×50×8 板 + 40×20 通槽，经四个回合、
   其中两个故意失败，所以会话里带着真实失败史）和 `acc-plate`（60×60×12 板 +
   偏心 20×20 通孔）。模型不是替身：Gate 不过就整个脚本退出。
2. **起真实服务**（`tools/serve.py`，worker 真的活着）。
3. **驱动 Chromium**：14 组检查（G1–G14），覆盖启动、状态灯独立性、三维操作、
   点击阈值、检查器六个页签、Markdown 安全、输入法与草稿、会话管理、断点、
   主题、A/B 切换、断网、资源释放。
4. **独立分析截图**（Pillow）：像素变化率、深入孔内的背景像素、孔的偏心量、
   主题亮度差。

故意保留的两条诚实设计：

- **WebGL 计数是从浏览器外面做的**：在页面脚本之前 patch `drawElements` /
  `drawArrays`，记下 kind/mode/count。问「这是实时渲染还是一张图」只能问
  被问对象之外的东西 —— 一张静态图会让这个计数为 0。
- **「深入孔内」要求 2px 边距**：没有这个条件，材质阴影边缘那一两行深色像素
  会被算成「被包围的背景」，acc-bracket 的前视因此误报 1099 px（一块完全看不
  到洞的板）。加上边距后是 37 px。

---

## 八、这一轮没有做的事

- 参数表单目前只对**数值参数**做编辑（`ft_hole` 的 `reversed/type` 这类非数值
  参数不在表单里），因为改布尔/枚举语义上不是「改尺寸」。
- 对象选择仍停在实际 Body 粒度；面/边选择需要当前构建的可靠拓扑映射，没有
  映射时按任务书要求如实降级，没有假装支持。
- 会话搜索没有做虚拟化/分页上限之外的性能优化（列表规模在本轮没有到达需要
  它的量级）。
