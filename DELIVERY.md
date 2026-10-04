# 交付说明：当前 CAD 工作台

更新日期：2026-10-04。原交互预览代码基线：`9bf00e0`；当前工作区已增加需求复核，见下文。

本文以当前仓库代码和可核查测试为准。此前版本包含超出当前仓库实现的交付描述：
可恢复运行、完整会话管理、参数表单、对象选择和像素级浏览器验收，
其对应接口、脚本或依赖不在当前仓库中，现撤回这些已交付、已验证的结论。
原始文字可从 Git 历史查看；当前交互预览的历史验收记录见
[交互预览验收](docs/interactive-preview-acceptance.md)。

## 当前实现

- 对话通过工具修改 IR，经 FreeCAD 编译、导出，再由 Gate 判定；
  最新版本没有有效的通过报告时不能宣布成功。生产默认还要求 `design_review`
  关联用户原话与当前测量证据；缺少证据则返回待验收草稿。构建通过不会自动结束建模。
  已记录约束通过也不代表实际机械功能已经验证，见 [功能验收说明](docs/functional-acceptance.md)。
- 中央视口使用 `tcad/server/ui/viewport.js` 的本地 WebGL 渲染器绘制真实网格。
  支持旋转、缩放、平移、七个预设视角、适配模型、坐标轴和触摸操作。
  同模型版本更新保留相机，会话切换重置相机；过期响应不能覆盖新视口。
- WebGL 不可用或网格加载错误时可降级到静态 PNG；预览繁忙返回 429，
  前端不会用 PNG 请求绕过该限流。预览可用本身不代表 Gate 通过。
- 会话与模型绑定，支持新建、列表、切换和读取已保存的文本消息。
  工具轨迹和 Gate 卡片不会通过消息历史完整回放。
- 同一会话或模型只能有一个在飞回合；冲突返回 409，注册竞态通过 SSE 报错。
  客户端断开会取消回合，取消清理结束前保留占用；切换会话也会断开当前流。
- 模型请求计入工具 schema 并预留输出空间；上下文压缩只移除完整旧工具轮次，
  保留已选历史、当前请求和刷新后的 CAD 状态。临时错误重试有等待上限，
  确定性错误不重试；同一 IR 上相同工具失败达到阈值后停止。
- FreeCAD 支持 PATH 命令解析和显式 Linux 原生 Python 适配器。
  阵列枚举按内核实际属性转换；实验性 CircularPattern 缺失会明确报告。

## 接口

| 方法 | 路径 | 当前行为 |
| --- | --- | --- |
| POST | `/models` | 创建初始 IR，已有模型拒绝覆盖 |
| GET | `/models/{id}/ir?version=N` | 读取当前或指定版本 IR |
| GET | `/models/{id}/artifacts?version=N` | 读取版本产物列表 |
| GET | `/models/{id}/artifacts/{path}?version=N` | 下载版本产物 |
| GET | `/models/{id}/verdict?version=N` | 读取版本验证状态 |
| GET | `/models/{id}/mesh?version=N&tolerance=0.5&force=false` | 读取有界、按快照缓存的真实网格 |
| GET | `/models/{id}/render` | 静态 PNG 渲染与缓存 |
| POST | `/chat` | 请求字段为 `model_id / text / thread_id? / kind / privileged_requested / request_id?`，返回 SSE |
| POST | `/chat/interrupt` | 按 `request_id` 停止；回答 `interrupted / stage`，未注册请求会短暂记为 pending |
| GET / POST | `/sessions` | 列出会话（`limit`）或创建会话及绑定模型 |
| GET | `/threads`、`/threads/{id}/messages` | 读取会话索引和文本消息 |

另有 `/health`、`/settings/*`、`/approvals*`。SSE 使用 `start / progress / agent /
result / error` 事件，没有可重放的 `seq` 游标协议。完整字段约束以
`tcad/server/app.py` 的请求模型和路由为准。最终 `result` 新增 `completion_review`，
包含 `verified / scope / summary / checklist / remaining_work / ir_version / note`。
`draft` 表示构建通过但需求待验收；`/verdict` 的 `verified` 仍只表示构建验证。

网格预览限制：容差 0.1–5mm、100k 顶点、200k 面片、16MiB 响应、
16 项 / 32MiB 缓存、最多四个活跃预览作业。取消 HTTP 请求后，实际工作完成前
仍占用作业名额；临时构建目录与已发布产物分离。

## 依赖与运行

Python ≥ 3.11；依赖和版本约束见 `pyproject.toml`。
前端为原生 HTML/CSS/ES module，无构建步骤、CDN 或第三方前端库。
当前没有 Three.js、OrbitControls、markdown-it 或 DOMPurify。

```bash
.venv/bin/pip install -e '.[server,dev]'
# 如使用已有内核：export TCAD_FREECAD_CMD=/path/to/FreeCADCmd
.venv/bin/uvicorn tcad.server.app:create_app --factory --port 8000
```

## 快捷键

| 按键 | 当前行为 |
| --- | --- |
| Ctrl/Cmd+K | 新建会话 |
| Enter / Shift+Enter | 发送 / 换行；尚无输入法组字状态保护 |
| Esc | 运行中且设置对话框未打开时停止当前回合 |
| 画布聚焦：F / Home / 双击 | 适配模型 |
| 画布聚焦：0–6 | 等轴测、前、后、右、左、上、下视角 |
| 画布聚焦：方向键 / Shift+方向键 | 旋转 / 平移 |
| 画布聚焦：+ / - | 放大 / 缩小 |

鼠标左键旋转，滚轮缩放，右键或 Shift+拖动平移；单指旋转，双指缩放和平移。

## 验证结果与边界

2026-10-04 对上述代码基线实际运行：

```text
.venv/bin/python -m pytest tests -q       1258 passed，76.58s
node --test tests/frontend/*.test.mjs    17 passed
.venv/bin/python -m compileall -q tcad tools    通过
git diff --check                              通过
```

覆盖包括真实 FreeCAD 网格与版本编辑、空模型和失败构建拒绝、预览并发与取消、
回合互斥、上下文保护、错误重试和当前版本 Gate 判定。Node 测试验证相机数学、
事件处理和异步竞态。供应商端到端测试依赖实际配置，不能将本次总数推广为
所有供应商或所有环境均可通过。

历史预览验收记录明确指出浏览器访问 localhost 被阻止，因此实际 WebGL 着色器、
桌面拖拽、截图和像素变化仍需浏览器验收。本次未重新执行浏览器或视觉模型验收。
此前的 139 项浏览器检查、12 项像素检查及像素计数没有当前仓库可复现证据，
不再作为交付结论。`tools/browser_acceptance.py`、`tools/browser_fixture.py` 和
`tests/manual/live_param_*.py` 不在当前仓库中。

## 尚未实现

- `/runs*` 运行查询、事件游标重放、刷新重连、进程重启后的运行恢复。
- 会话重命名、置顶、归档、回收站、删除、搜索，以及相关迁移与清理接口。
- `/attachments*` 上传和图片建模入口；当前 `/chat` 请求不接收附件。
- `/models/{id}/params` 参数表单提交、`object_ref` 校验、模型与树双向选择。
- Markdown 渲染器、按会话持久化草稿、输入法保护、命令面板、面板拖拽。
- 剖切、精确 BRep 测量、装配爆炸图、多人协作和桌面安装包。

`request_id` 只在回合在飞期间防重复，不是跨完成回合的永久幂等键。
全量测试本次正常结束；旧文档所述小模型卡住和沙箱错误不作为当前环境结论。
