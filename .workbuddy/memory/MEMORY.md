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
- FreeCAD 构建：`cd free-cad/FreeCAD && pixi run configure && pixi run build` → `build/debug/bin/FreeCADCmd`。**当前尚未构建。**
- worker 启动：`FreeCADCmd -c --console -P <repo_root> tcad/worker/bootstrap.py`。
- 渲染只在 supervisor 侧（numpy + Pillow，PNG 有纯 stdlib zlib 兜底后端），不往 FreeCAD 进程塞第三方依赖。
