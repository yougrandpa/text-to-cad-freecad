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
- `TopoShape` **无 `BoundBox` 属性** → 用 `optimalBoundingBox()`。
- `shape.check()` 实现是成功返回 `None`/失败抛 `ValueError`（与 `.pyi` 的 `-> bool` 不符）→ 必须 try/except。
- `SketchObject.solve()` 返回 `SolveStatus` 整数，**不是 DOF** → DOF 读 `sketch.DoF`。
- **无头出不了 PNG**（渲染全在 `src/Gui`）→ worker 回传网格，supervisor 软件光栅化。
- 不存在：`Part.checkGeometry`、`Part.checkSolid`、`shape.isSolid`、`shape.Vertices`（是 `Vertexes`）、`BoundBox`。
- 详细清单与源码行号见 `docs/02-架构设计.md` 附录 A；`docs/` 里的未核实项不得照抄。

## 构建 / 运行约定
- FreeCAD 构建：`cd free-cad/FreeCAD && pixi run configure && pixi run build` → `build/debug/bin/FreeCADCmd`。**已构建可用。**
- worker 启动：`FreeCADCmd -c --console -P <repo_root> tcad/worker/bootstrap.py --pass --worker-id=wN`
  （**`--pass` 必须放在脚本自身参数之前**，否则 FreeCADCmd 先解析并拒绝未知选项，脚本一行都不执行）
- 渲染只在 supervisor 侧（numpy + Pillow，PNG 有纯 stdlib zlib 兜底后端），不往 FreeCAD 进程塞第三方依赖。

## 唯一入口与常用命令
- **唯一生产装配入口**：`tcad.core.wiring.build_services(config)`。它把各子系统「库形状」适配成 `tcad/tools/base.py` 里的窄 Protocol。
  不要在其他地方重新接线；`LoopEngine.build_default_services(cfg)` 只是转发（传 LoopConfig 会报错）。
- 装一个完整栈不启 worker（测试用）：`build_services(cfg, start_worker=False)`
- 跑全部测试：`.venv/bin/python -m pytest tests/ -q`（当前 **362 例全绿**）
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

## 建模时最容易踩的两条（已写进 ir_patch 工具描述）
1. **Pocket 方向**：草图在 XY（法向 +Z）时 `PartDesign::Pocket` **默认朝 -Z 切**，而底板在 +Z 侧 → 切进空气，特征什么都不做**却报告成功**（`ok=true`、`errors=[]`、体积不变）。要用 `"reversed": true`。
2. **草图定位**：把一条线的**起点与终点**都用绝对坐标标注 + H/V 约束 = 求解器冲突。正确做法：轮廓建在**草图自己的原点**上（绑一条线到草图原点 + 标注对侧端点），再用 sketch 的 `offset` 整体定位。
3. 通用：**静默无效比报错危险**。凡"特征没生效"，先怀疑方向/基准面，再看体积有没有变。

## 已知环境限制
- **无法 push 到 GitHub**：认证与体积都正常，pack 能完整上传，但代理不转发 `receive-pack` 的响应流（HTTP/2 → 408，HTTP/1.1 → curl 52 empty reply）。`ls-remote` 正常（下载类 OK）。`~/.ssh` 无密钥。**需要换代理或加 SSH key**；`127.0.0.1:7890` 与默认 50683 都试过，后者连不上 GitHub。
- 执行沙箱禁止写 OS 临时目录 → pytest 必须用 `--basetemp=.pytest_tmp`（已在 pyproject.toml）。**该目录每次运行会被清空，别把产物放那里。**
- macOS BSD `grep` 不支持 `\|` 交替 → 用 Grep 工具或 `grep -E`。
- 无 `timeout` 命令。
