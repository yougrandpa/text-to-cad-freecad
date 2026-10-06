# text-to-cad-freecad

基于 FreeCAD 的对话式参数化 CAD。用自然语言描述零件或机构，生成可继续编辑的
FCStd 模型、STEP/STL，以及可播放和导出的装配动画。

模型通过校验工具修改声明式 IR，FreeCAD 在独立 worker 中编译几何。
构建经 Gate 验证后发布，再通过 `design_review` 复核用户需求。

**技术栈：** Python 3.11+ · Pydantic v2 · FastAPI/SSE · SQLite · 原生 JavaScript/WebGL，无前端构建步骤。

## 当前能力

截至 **2026-10-05**：

- **参数化建模**：版本化 IR、稳定特征命名、可编辑 PartDesign 历史和多 Body 建模。
- **快捷创作**：批量盒体、圆柱、管、梁与极坐标复制；轮体、支架和直立吊舱生成工具。
- **装配与动画**：FreeCAD 原生关节/驱动求解、规定旋转、平面重力摆动画、采样干涉检查；GIF/视频导出。
- **构建运行时**：不可变产物、几何缓存、持久任务调度、worker 池、组件并行构建与固定零件引用。
- **交互预览**：真实网格、标准视角、旋转/缩放/平移、动画播放；读取已发布预览无需在线 worker。
- **模型接入**：OpenAI 兼容接口、供应商设置与热切换、上下文预算、重复失败检测和回合中断。

几何通过只证明已检查的约束。采样干涉检查与重力摆模型不证明连续间隙、接触力、
结构安全或实际切削；缺少需求证据时交付状态为 `draft`。真实供应商 E2E runner 已加入，尚未执行。

## 快速开始

安装依赖并指定 FreeCADCmd：

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[server,dev]"
export TCAD_FREECAD_CMD=/path/to/FreeCADCmd
.venv/bin/python tools/serve.py
```

打开 [Web 界面](http://127.0.0.1:8000/ui/)，在右上角设置中填写供应商、模型、
Base URL 和 API Key，测试连通性后保存。新建会话，输入例如：

> 做一个 80×50 mm 的底板，厚 8 mm，中间开一个 40×20 mm 的通槽。

界面显示工具执行、构建与需求复核结果，并提供模型下载。运行中可点击「停止」，
已写入的 IR 改动保留，后续可继续对话。仅查看界面和已发布产物可用 `--no-worker` 启动。

Linux 嵌入式 FreeCADCmd 无法启动、但系统 Python 能加载匹配 ABI 的 FreeCAD 模块时，
可将 `TCAD_FREECAD_CMD` 设为 `$PWD/tools/freecad_python.py`，必要时设置 `TCAD_FREECAD_LIB_DIR`。

### 无 API Key 演示

分别在两个终端运行：

```bash
# 终端 A：脚本化模型替身
.venv/bin/python tools/stub_llm.py --script tools/sessions/demo_bracket.json --port 8123

# 终端 B：仍使用真实 FreeCAD 编译
.venv/bin/python tools/serve.py --data-dir .tcad_demo --port 8765 \
  --base-url http://127.0.0.1:8123/v1 --model stub-scripted
```

打开 [演示界面](http://127.0.0.1:8765/ui/)。脚本包含建模失败与修复，用于观察完整工具链。

## 配置

默认配置为 [`configs/default.yaml`](configs/default.yaml)。常用覆盖方式：

```bash
export TCAD_LLM_API_KEY=<api-key>
.venv/bin/python tools/serve.py --provider deepseek --model deepseek-chat
```

- 界面保存的 `<data_dir>/settings.json` 优先于 YAML/环境配置；密钥回传时掩码。
- 命令行 `--provider`、`--model`、`--base-url` 只临时覆盖，不改保存的设置。
- 默认不设回合总步骤/token/时间上限；单次请求超时和停滞检测仍生效。
- 服务默认绑定本机，无 HTTP 身份认证；权限模式见[说明](docs/access-modes.md)。

可选功能：

```bash
# 视频导出，需要对应编码器；GIF 已包含在基础依赖中
.venv/bin/pip install -e ".[animation]"

# 共用 WebGL 渲染器截图；默认使用软件渲染
.venv/bin/pip install -e ".[webgl]"
.venv/bin/playwright install chromium
```

WebGL 截图需设置 `context.render.backend: webgl`。
worker 池、缓存、调度及截图后端配置见[构建运行时说明](docs/build-runtime-refactor.md)。

## 开发与测试

```bash
.venv/bin/python tools/doctor.py                  # 环境诊断
.venv/bin/python -m pytest tests/unit -q          # 单元测试
.venv/bin/python -m pytest tests/contract -q      # 真实 FreeCAD 契约测试
node --test tests/frontend/*.test.mjs            # 前端运行时测试
.venv/bin/python -m pytest tests -q               # Python 全套，真实供应商测试需配置
```

真实供应商检查可在隔离目录运行以下命令，会调用配置的模型并可能产生费用：

```bash
.venv/bin/python tools/run_model_e2e.py --request '生成一个 80×50×8 mm 的底板' --max-steps 35
```

runner 保存脱敏事件、结果与 HTTP 交付检查；`delivery_passed` 可包含待验收草稿。
各阶段测试范围见[构建重构记录](docs/build-runtime-refactor.md)和[交互预览验收](review/history/interactive-preview-acceptance.md)。

目录分工：

| 目录 | 职责 |
|---|---|
| `tcad/ir/`、`tools/`、`loop/` | 声明式模型、校验工具与 Agent 回合 |
| `tcad/build/`、`worker/` | 调度、缓存、组件构建与 FreeCAD 编译 |
| `tcad/artifacts/`、`inspect/`、`verify/` | 产物身份、测量与 Gate |
| `tcad/viewer/`、`render/`、`server/` | 交互视图、截图、API 与 Web UI |
| `configs/`、`tools/`、`tests/` | 配置、运行脚本与测试 |
| `docs/`、`review/` | 设计说明与验收记录 |
| `data/`、`free-cad/` | 本地运行数据与 FreeCAD 源码/构建，均不入库 |

修改代码时保留 IR/worker 边界：写入需经过校验 patch，Gate 读取本次冻结产物，
只有通过的构建才能替换正式版本。HTTP 接口详见运行服务的 [API 文档](http://127.0.0.1:8000/docs)。

## 详细文档

- [文档索引](docs/README.md)
- [构建运行时与 Viewer](docs/build-runtime-refactor.md)、[产物查询边界](docs/artifact-boundary-refactor.md)
- [原生装配：关节、驱动与示例](docs/native-assembly.md)
- [功能验收与完成条件](docs/functional-acceptance.md)、[Agent 运行时加固](docs/agent-runtime-hardening.md)
- [权限模式](docs/access-modes.md)、[特征引用](docs/feature-references.md)与[原生草图曲线](docs/native-sketch-curves.md)
- [历史设计与验收记录](review/history/README.md)

## 许可

本仓库自有代码遵循上游仓库许可。FreeCAD 以 LGPL-2.1 使用，作为本地构建依赖，
不随本仓库分发；当前通过 Python API 使用内核，不修改内核。
