# 落地风扇真实端到端独立黑盒检查 — 2026-10-08

## 结论

本次单轮测试失败，用户请求“创建一个落地风扇，带动画和摇头”未完成。CLI 在第 35 步因模型服务 `InternalServerError: Error code: 521` 终止，耗时 **429.67 秒**，累计 **9 次工具失败**。没有原生关节、驱动、运动帧、GIF 或动画导出。最后成功冻结的几何 v6 虽然 5 个 Body 都各自有效且单实体，但机头悬空、叶轮嵌入机头、后网罩没有连接。可识别为风扇示意造型，不能视为结构可信且可运动的落地风扇。

此结论独立基于本轮日志、冻结 IR、public scene 和导出的 FCStd。已实际通过 `view_image` 看过模型返回的 v4 正视/等轴测图，以及从本轮冻结 v6 public scene 渲染的正视、侧视、等轴测与机头细节。没有读取 `tcad/`、验收器、修复差异或之前运行输出，没有修改模型提示或项目实现，没有重跑。

## 执行范围与复现

工作目录：`/Users/slg/.codex/worktrees/standing-fan-e2e/text_to_cad`。

```sh
TCAD_FREECAD_CMD=/Users/slg/workspace/text_to_cad/free-cad/FreeCAD/build/debug/bin/FreeCADCmd /Users/slg/workspace/text_to_cad/.venv/bin/python tools/run_model_e2e.py --case standing-fan --settings-dir /Users/slg/workspace/text_to_cad/data --output-dir output/local/tcad_e2e/fan-blackbox --require-animation-mode motion --max-steps 80 --timeout 1500
```

模型：`deepseek-v4.1-flash`。从空模型生成，runner 只读加载 settings，没有读取或打印保存密钥。实际只运行一次，进程退出码 1。输入 token 1,052,870，输出 token 8,759。

- 原始证据：`output/local/tcad_e2e/fan-blackbox/events.jsonl`、`summary.json`、`result.json`。
- 最后 IR：`models/e2e-model/v14.json`；最后成功构建/冻结：v6；`current_build=false`。
- 冻结产物：`artifact_sets/1ec611ac73043fb11d5b7d507d45a5d7f0b7c3a47aa7bd403fc6d662752e0030/`。
- 观察图：`blackbox-review/last-built-v6-contact-sheet.png`。
- 独立 FCStd 测量：`blackbox-review/last-built-v6-pairwise-geometry.json`。
- 复现辅助脚本：`blackbox-review/render_public_scene.py`、`blackbox-review/measure_exported_fcstd.py`。前者只渲染公开 scene 的网格；后者只读打开导出 FCStd，用 `distToShape` 和 `Shape.common().Volume` 测量，不修改源文件。

所有辅助证据与二进制都留在该次 `output/local` 目录。

## 实际造型、连接与运动检查

冻结模型总包围盒约 **464 × 320 × 1162 mm**；圆底座直径 320 mm，底座/渐变立柱属于同一 Body，立柱顶部 z=790 mm。落地轮廓和比例基本可识别，但侧视图揭示关键连接缺失：

| 检查 | 实际证据 | 判断 |
| --- | --- | --- |
| 底座/立柱落地结构 | `column` 为 1 个有效实体，z=0..790；底座与立柱连续 | 下部静态几何可信，未验证承载 |
| 立柱连接机头 | head z=866..994，bbox 垂直间隙 76 mm；BRep 最近距离 **86 mm**；无支架/轴承连接 | **不可信，机头悬空** |
| 扇叶相对机头自转 | `scene.motion=[]`、`scene.animation=null`；冻结 IR 和最新 v14 的 assembly 均 null，body.motion 均 null | **未实现**，无中间姿态可看 |
| 叶轮与机头配合 | 冻结叶片 Y=66.5..73.5；机头实心前锥 Y=60..95；BRep 交集 **70,945.8566 mm³**，占叶轮约 16.8% | **存在实质干涉**，不是可旋转配合 |
| 机头往复摇头 | 0 native joints、0 drivers、0 frames，没有轨迹/反转证据 | **未实现** |
| 底座/立柱动画中固定 | 无动画、未声明 grounded/Fixed | 只能确认静态文件中的位置，**不能证明动画固定** |
| 网罩随头运动 | 两个网罩均独立 Body，无 Fixed 或其他装配连接；后罩与机头最小距离 **5.5 mm** | **未实现/未连接** |
| 网罩不穿扇叶 | 静态 blades↔front guard 距离 **18 mm**，↔rear guard **152 mm**，交集均 0 | 静态这一姿态不穿叶片，**运动全程未验证** |
| 网罩造型 | 实际图是前后两个平面辐条轮，外缘没有罩壳/连接筋，网罩之间最近距离 **177 mm** | 符号化外形，笼罩结构不完整 |
| 动画播放/下载 | 0 animation exports、0 GIF，`http_animation_status={}` | **无动画可播放/下载** |

冻结 head↔front guard 交集 27,943.7521 mm³；这可解释为前罩中心固定安装区域的重叠，单凭重叠不能证明装配连接正确。主叶片仍是三片平直矩形板，没有扭角/翼型；本轮没有验证气流性能，不能据运动或 CAD 有效性推断送风功能。

冻结 FCStd 通过 FreeCAD 26.3.0 打开，各 Body `isValid=true`、`Solids=1`。这证明单个 Body 可编译，不能证明 Body 之间的安装关系。

## 首轮失败与模型自行处理

| 步骤 | 工具/问题 | 结果 |
| --- | --- | --- |
| 13 | `ir_help(shape='cyl')` 不在枚举中 | 首次工具失败，schema 拒绝 |
| 14 | 两个 `cad_wheel` 将 rim_width、spoke_width 数值传成字符串 | 2 次 schema 拒绝；15 步重发数值后成功 |
| 18 | `ir_get(ids=['blade_rotor'])` 查询 recipe 名而非实际 IR feature ID | not_found；19 步查询真实 features |
| 22、24 | `head.motor_shaft` 添加结果与之前实体完全一致 | 两次 compile 失败；23 步把轴起点往后移仍失败 |
| 29 | `remove_feature(head_front_pocket, cascade=true)` 后又把 sketch ID 当 feature 删除 | not_found，整个调用拒绝；31 步只删 feature 成功 |
| 32 | 同一模型回复两个写调用都用 base_version=12；第一个更新叶轮升到 v13 | 第二个支承环调用 stale base_version；33 步以 v13 重试 |
| 34 | 更新 head_taper 的 cad_build_parts 缺 `shape` | schema 拒绝；随后 35 步遇到服务 HTTP 521，无后续修复 |

模型确实在测量/位置理解后反复重建和删除部件：22/24 步轴 no-op 后，25 步读取 placement，26 步终于识别轴完全在已有实心机头内部并删除轴及鼻锥；27 步承认叶轮 hub 被实心前锥包住，28 步尝试空腔，29/31 步删除错误位置空腔。31/32 步多次重新规划 Y 坐标，32 步又将叶轮移至 Y=52。未看到它把 schema 拒绝后的合法批次项当成已经部分执行；32 步发生的是两个独立写调用共享旧版本，第一调用确实成功、第二调用被版本检查拒绝。

最新未编译 v14 中，补加的 head_yoke 声明范围 z=800..834，原 column 顶部 z=790，原 head 下缘 z=866。因此按声明它仍与原立柱留 10 mm、与原机头留至少 32 mm 的垂直缺口，不能解释为已修好的连接。此处只是公开 IR 的声明检查，**没有重新编译 v14，也不把未构建几何当作成功交付**。最后公开冻结 FCStd 仍是 v6。

## 交付/API与边界

runner 的本轮公开 HTTP 记录：scene 200、`e2e-model.FCStd` 下载 200；animation 没有请求结果。导出 FCStd 在磁盘存在并可实际打开，文件内几何与冻结 v6 scene/digest 一致。没有 STEP/STL 或 GIF 动画交付。没有启动 UI 做人工播放检查，因为没有生成动画资源；动画播放/下载应记为未完成，不可写成通过。

`build_passed=false`、`delivery_passed=false`、`case_evidence.passed=false` 与独立证据一致，本报告没有仅凭这些布尔值判断。失败状态同时提供了旧版本静态几何，因此消费端需要明确区分“最后成功预览”与“当前修改已构建”；本轮 `artifact_ir_version=6` 与 `final_ir_version=14` 已公开这个差异。

## 最值得优化的问题

1. **先验证部件连接和轴向配合，再进入运动设置。** 单体有效/单实体检查未捕获悬空机头、浮动后罩和叶轮嵌壳；本轮量测又只看 bbox，没有完成关键部件距离/交集核查。应让模型把侧视图和两两距离/交集作为装配前证据。
2. **改进 additive no-op 的诊断。** 实际轴完全被现有实体包住，工具提示却泛举“空空间、不相交”等原因；模型先沿错因继续移轴，浪费一次编译和多步返工。区分“加料完全被包含”与“相离导致多实体”等原因会直接帮助定位。
3. **提高 schema 与版本调用可靠性。** 字符串数值、recipe/feature ID 混淆、级联删除草图、同回复重复旧 base_version、漏 shape 共造成 7 个非几何编译错误。原始契约/错误已可见，仍需提高模型按契约构造后续工具调用的可靠性。
4. **检查模型服务失败的可恢复性。** 在 35 步被 HTTP 521 截断，此前几何返工占据所有步骤，用户的两个核心动态功能均未开始。服务故障是本轮终止原因；没有证据表明在服务恢复后余下 45 步一定能完成。
