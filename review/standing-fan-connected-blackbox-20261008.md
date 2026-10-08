# 落地风扇连接续修独立黑盒复核 — 2026-10-08

## 结论

**原简短请求的可识别落地风扇、自转与往复摇头动画通过。新增实体轴/轴颈、转子孔及缩径套筒配合，也通过概念层面的几何复核。** 这些是实际存在的 BRep，不再只是虚拟关节连接。机械固定与轴向承托仍不完整：post/neck 留有径向 0.5 mm、轴向 2 mm 间隙，没有夹紧件；head 枢轴没有轴向接触止挡；rotor 没有轴向挡圈/螺母等保持件。不能据“带间隙配合”宣称这些未建的功能已经存在。

本次 `fan-connected` 是公开摘要记录的真实模型反馈续修，最终 **v32**，`current_build=true`，5 个有效单实体。**38 步、598.25 秒（约 9 分 58 秒）、7 次工具错误**，没有终止服务错误；模型 deepseek-v4.1-flash，状态 draft，输入 1,676,246 tokens、输出 17,114 tokens。case/delivery 布尔值为 true；本报告另外查看图像、实际剖面，并独立对全部保存姿态量测，不只信布尔值。

范围只限本次公开日志/summary、冻结 IR、scene、GIF 和导出 FCStd；未读取项目实现、修复差异或验收器，未另起生成，未改动原始产物或模型输入。

## 实际看图与轴孔配合

已用 `view_image` 查看本次 v32 public scene 渲染的静态/中间姿态、GIF 解码中间帧、消去机头姿态后的叶轮自转图，以及**从导出 FCStd 实际 `Shape.slice(X=0)` 提取的剖面**。剖面不是按声明尺寸画的示意图；线条由真实 BRep 截面离散得到。

实际剖面：`blackbox-review/actual-brep-x0-sections.png`。原始截面与圆柱面：`static-x0-sections.json`、`static-cylinder-surfaces.json`。

| 配合 | 实际 BRep / 冻结 IR | 独立判断 |
| --- | --- | --- |
| head 新实体轴 | 声明 Ø16，Y=60..210；最终 head 圆柱面实际露出 Y=118..143 和 177..210，根部嵌入壳体，轴与中间轴颈一体 | **实体轴已存在且与机头连成一个有效实体** |
| head 轴颈/简化轴承体 | 实际圆柱外径 Ø42，Y=143..177，与上述轴一体；不是独立滚动体轴承 | 可作为概念固定轴颈/支承外圆 |
| rotor 最终孔 | BRep 只有 **Ø43.4 ×24 通孔**，Y=148..172 | 对 Ø42 轴颈形成 **0.7 mm 径向间隙** |
| 轴颈/rotor 轴向重合 | [143,177] 与 [148,172] 交集 **24 mm**，覆盖整个 rotor 轮毂厚度，两端轴颈各超出 5 mm | **实际同轴插入配合成立**，rotor 不再悬在无轴空腔内 |
| post/neck 下部孔 | post Ø36；实际 pilot 孔 Ø37，孔实体范围 z=876..902，post 顶部 z=900 | 径向间隙 **0.5 mm**，轴向重合 **24 mm**；套筒导向配合成立 |
| post/neck 轴向承托 | post 端面 z=900，盲孔底 z=902 | 尚有 **2 mm 空隙**，未发生接触承托；没有夹紧螺钉/收紧机构/过盈 |
| head 枢轴/neck 上部孔 | 枢轴 Ø46，自 z=930；neck 上孔 Ø52，z=915..975 | 径向间隙 **3 mm**，轴向重合 **45 mm**；轴孔关系可见 |
| head 枢轴轴向定位 | 枢轴端 z=930，neck 上孔盲底 z=915 | 仍留 **15 mm 空隙**，未建接触肩台/止挡 |

需要修正模型自述的两个细节：

1. 冻结 IR 虽有 Ø17.2 的 `rotor_hub_bore`，随后 Ø43.4 的 `rotor_hub_counterbore` 从 Y=140..180 贯穿整个 24 mm 轮毂，完全覆盖了小孔。最终 BRep **没有保留下来的 Ø17.2 孔段**；正确配合描述是 Ø42 轴颈对 Ø43.4 轮毂通孔，不是 Ø16 轴对 Ø17.2 孔。
2. design_review 称“0.5 mm 径向间隙真实抱持并承托立柱”，超出了实际几何证据。当前已建的是缩径导向套筒/盲孔雏形；剖面显示立柱顶与盲底间仍有 2 mm 空隙，且没有实体夹紧件。Fixed/grounded 约束可以固定姿态，不能替代实际夹持/承托。

这些未完成项不要求按工程制造级细节补齐才承认概念配合成立，但应如实限定“机械连接完整性”结论。

## 完整 161 帧运动与干涉独立测量

从本次 v32 导出 `e2e-model.FCStd` 取各 Body 实际 Shape；使用同一冻结 scene 中的每个 4×4 刚体矩阵，对**全部 161 帧、全部 10 个 Body 对**独立计算 `distToShape` 和 `Shape.common().Volume`。计算约 35.2 秒。结果保存在 `blackbox-review/independent-all-161-frames-geometry.json`，包含逐帧原始数值，没有调用项目验收器。

| Body 对 | 全部保存帧最小距离 | 最大交集体积 | 判断 |
| --- | --- | --- | --- |
| head / rotor | **0.7 mm** | 0 | 轴颈/孔与叶片/机壳无穿透 |
| neck / head | **3 mm** | 0 | 摇头轴孔配合全帧不穿透 |
| post / neck | **0.5 mm** | 0 | 静态缩径配合无穿透 |
| post / head | **30 mm** | 0 | 立柱与 head 仍非直接接触，但有 neck 导向关系 |
| neck / rotor | **120.9928854 mm** | 0 | 无穿透 |
| post / rotor | **171.9681773 mm** | 0 | 无穿透 |
| base / post | 0 | **27,482.6525 mm³** | 所有帧相同，是静态固定概念连接的实体嵌入 |

其余配对全部为零交集。准确结论是**所有运动部件配对在全部保存姿态无穿透**，不是所有零件对都无交集；base/post 的固定连接始终重叠。离散帧不证明帧间连续路径、接触力、承载、锁紧或轴向保持。

对 scene 矩阵另行重算 `inverse(M_head) * M_rotor`，消去机头摇头后展开 Y 轴自转角；并检查机头 Z yaw 和下部矩阵：

- 叶轮相对机头 **0..4320°**，4 秒 12 圈，0 次换向，每保存帧 27°。
- 机头 **-40°..+40°**，累计 160°，2 次换向；frame40（1s）+40°、frame120（3s）-40°、frame160 回起点。
- base/post/neck 全部 161 帧矩阵与首帧差精确 0。
- 叶轮相对旋转轴点最大位置误差约 1.16e-12 mm；旋转矩阵正交误差约 1.33e-15。

实际 GIF 和世界中间姿态图显示两端朝向；载体固定视图实际显示 0°/27°/54°/135° 叶轮变化，确认不是只随 head 共同旋转。

## 媒体、导出和造型限制

本轮 motion GIF **720×720，81 帧全数解码成功，每帧 50 ms，总 4050 ms，loop=0**。有 71 个不同画面，其余为周期性重复。公开 HTTP 记录中 scene、两份 FCStd 和 GIF 均 200；磁盘媒体可实际读取。未额外启动服务或浏览器测试 UI 控件点击。

静态 FCStd 的 5 个 Body 均为一个有效实体。`assembly.FCStd` 加载了 109 个对象。FreeCADCmd headless 打开装配仍出现三次上游 `CommandCreateSimulation.py` 的 `QtCore is not defined` GUI 模块诊断，文件加载没有因此终止；不能把这写成完全无诊断，也不能据此断言桌面 FreeCAD 无法打开。

造型保持可识别的落地 duct 风扇：圆底盘、细立柱、圆套筒、封闭后段/开放前腔、三片厚矩形叶片。前口仍没有防护网罩；叶片仍无翼型、扭角，未验证实际送风。轴颈是 head 的固定实心轴对称体；独立运动驱动是规定的两条运动学公式，没有验证电机扭矩传递或实体摇头传动。模型不应称其为滚动体轴承、夹紧件或轴向止挡。

本轮 7 个工具失败：step2/13 未构建版本量测，step9 不支持 `reversed`，step12 rotor 孔 cut no-op，step16 shaft 加料 no-op，step17 非 checkpoint 渲染受限，step19 cylinder 不支持 `start` 参数。最终 v32 是当前成功构建，与导出一致。

## 产物路径与复现

输出根目录：`/Users/slg/.codex/worktrees/standing-fan-e2e/text_to_cad/output/local/tcad_e2e/fan-connected`。

冻结目录：`artifact_sets/bde722879cd01560f72596eb0d488a877be35aec4aef6ba9c044b304050bd6dd/`。

GIF：`derived/animations/bde722879cd01560f72596eb0d488a877be35aec4aef6ba9c044b304050bd6dd/e820b3003935caa89cd53b0f8621a0eedc7a509b9f67fb589e381912d5a03109/animation.gif`。

所有独立图像、JSON 与复现脚本放在 `blackbox-review/`：`actual-brep-x0-sections.png`、`gif-decoded-contact-sheet.png`、`scene-world-intermediate-poses.png`、`rotor-relative-to-head-poses.png`、`independent-motion-media.json`、`independent-all-161-frames-geometry.json`、`static-cylinder-surfaces.json`、`static-x0-sections.json`、`analyze_public_motion_media.py`、`measure_all_saved_poses_and_sections.py`、`render_actual_sections.py`。脚本只读原始产物，不保存或重编译原 FCStd。

**最终验收：原请求的概念造型/动作/媒体通过；新增轴颈与套筒导向几何配合通过；夹紧、接触承托及轴向保持尚未完整实现。**
