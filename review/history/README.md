# 历史设计与验收资料

这些资料于 2026-10-06 从 `docs/` 归档，保留早期决策与验收证据。
部分接口、渲染路径、环境、测试数量和完成状态已被后续实现替代。
现行说明见 [文档索引](../../docs/README.md)，配置字段以 `tcad/config/schema.py` 为准。

- [需求澄清问卷](01-需求澄清问卷.md)
- [早期架构设计](02-架构设计.md)
- [早期界面与模型配置](03-交互界面与模型配置.md)
- [交互预览历史验收](interactive-preview-acceptance.md)
- [削笔器 Agent 实测](sharpener-agent-trial.md)
- `renders/`：早期截图、几何预览及 `bracket.step`，保留原始字节。

`tools/render_sample.py` 的新输出写入忽略的 `output/renders/`，不覆盖归档证据。
