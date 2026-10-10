# 落地风扇最终产物独立黑盒复核 — 2026-10-08

## 结论

**原始简短请求的概念交付通过：**实际静态图可识别为落地 duct 风扇；叶轮相对机头连续自转，机头真正往复摇头，下部固定，motion GIF 完整可读取。**机械连接完整性未通过：**立柱与轴承套没有实体夹持/承托，叶轮没有实际轴/轴承支承，前口没有网罩格栅；这些不能由 Fixed/Revolute 声明补足。

本轮最终冻结 **v24**，与当前 IR 一致。summary 实际记录 **74 步、1271.37 秒（约 21 分 11 秒）、14 次工具错误**，无终止服务错误；模型 `deepseek-v4.1-flash`，输入 3,685,771 tokens、输出 28,965 tokens，状态 draft。公开 `delivery_passed/case_passed=true` 与独立概念运动/媒体检查一致，但不等同于机械完整性通过。

复核仅使用 `fan-final` 本次公开输出、冻结 IR、scene、静态与装配 FCStd、GIF 和必要只读 FreeCAD 测量。未读取项目实现、修复差异、验收器或之前运行产物；未另起模型生成、修改提示、修改原始产物或项目实现。

## 本次证据位置

输出根目录：`/Users/slg/.codex/worktrees/standing-fan-e2e/text_to_cad/output/local/tcad_e2e/fan-final`。

冻结目录：`artifact_sets/f5e17c1e37a3a2ca18480e8df380a803a02855070c7fea379bcafd181b7efa5f/`。

- 原始记录：`summary.json`、`result.json`、`events.jsonl`。
- 动画 GIF：`derived/animations/f5e17c1e37a3a2ca18480e8df380a803a02855070c7fea379bcafd181b7efa5f/a7bdac69962e2fc78cee8b18373cee14fa8594a5d4ab175667ee3482b8fc9fb8/animation.gif`。
- GIF 真实解码帧图：`blackbox-review/gif-decoded-contact-sheet.png`。
- 世界坐标中间姿态：`blackbox-review/scene-world-intermediate-poses.png`。
- 消去机头整体姿态后的叶轮自转图：`blackbox-review/rotor-relative-to-head-poses.png`。
- 独立 161 帧矩阵与媒体数据：`blackbox-review/independent-motion-media.json`。
- 独立全 161 帧、全部 10 个 Body 对几何数据：`blackbox-review/independent-all-161-frames-geometry.json`。
- 复现脚本：`blackbox-review/analyze_public_motion_media.py`、`blackbox-review/measure_all_saved_poses.py`。脚本只读原始产物，辅助图/JSON 留在本轮 output/local。

已实际用 `view_image` 查看最终 v24 官方 iso/front/top 三张静态图，以及上述 GIF 解码图、世界中间姿态图、载体坐标下的自转图。判断不只依赖摘要数值。

## 原请求的造型与动作

造型为圆底盘、细长立柱、套筒、逐渐扩大的封闭后段机头和开放式前腔、三叶轮。public scene 尺寸约 **338.75 × 376 × 1329.38 mm**，底座 Ø320，立柱 Ø36。整体落地结构与风扇轮廓清楚，适合作为概念模型。

造型明显简化：机头是 duct 外壳，前面没有网格/辐条防护罩；三片叶片是 **24 mm 厚的平直矩形板**，公开 IR 每片为 `additive_box`，没有翼型、扭角或曲面叶片。模型最终 design_review 声称“非矩形替代”与实际图像/冻结 IR 不符。不能把这个封闭后段、开放前口的模型解释为完成了传统落地风扇网罩或真实气动设计。原话没有指定网罩外形、叶片翼型、尺寸或风量，因此这些简化不单独推翻原请求的概念造型/动画完成结论；应明确告知。

独立对 scene 中全部 **161 个保存姿态**重算：将 16 个数按行组成 4×4 矩阵，用 `inverse(M_head) * M_rotor` 提取叶轮相对机头的 Y 轴角度并展开，直接检查机头 Z 轴 yaw，逐帧比较下部矩阵。结果：

| 动作 | 独立结果 | 判断 |
| --- | --- | --- |
| 叶轮相对机头自转 | 0..4320°，4 秒内 12 圈，连续同向；每保存帧 27°，0 次换向 | **通过**，不是只继承摇头 |
| 机头往复摇头 | -40°..+40°，累计 160°，2 次换向；帧 40（1s）为 +40°、帧 120（3s）为 -40°，帧 160 回起点 | **通过**，实际看过两端与中间图 |
| 底座/立柱/套筒固定 | base/post/neck 在全部帧的矩阵差均精确 0；GIF 下部 y=300..599 像素在全部帧也相同 | **通过** |
| duct 外壳随头移动 | 外壳同属 head；前口点 [0,216,1160] 横向扫过 277.684 mm | **通过**，没有独立网罩 Body |
| 相对转轴保持位置 | 叶轮相对机头枢轴 [0,160,1160] 最大偏差约 1.16e-12 mm | 数值一致 |

载体固定图实际展示了同一机头内叶片从 0° 到 27°、54°、135° 的不同姿态。世界坐标图和原 GIF 实际展示了 +40°/-40° 两侧朝向，下部位置不变。旋转矩阵正交误差最大约 1.33e-15。

## 静态连接与完整保存帧的干涉

只读打开导出 `e2e-model.FCStd`，取每个 Body 的实际 BRep。对 public scene 的 **全部 161 帧**应用相应刚体矩阵，再对**全部 10 个 Body 对**计算 `distToShape` 和 `common().Volume`；静态不变的配对复用相同测量。过程约 29 秒。没有调用项目验收器。

5 个 Body 各为有效单实体。所有含 head 或 rotor 的运动配对，在全部 161 帧中交集体积均为 0。这比本轮工具日志的 81 帧抽样更密，但仍只是离散保存姿态，未证明采样间连续路径或受力性能。

| 配对 | 独立最小间隙/交集 | 结构判断 |
| --- | --- | --- |
| head ↔ rotor | 全帧最小 **2.5381405244 mm**，全帧交集 0 | 叶片不穿壳；**无实体轴/轴承连接** |
| neck ↔ head | 全帧最小 **3 mm**，交集 0；head 的 Ø46 柱在 neck 的 Ø52 孔内，轴向重合 25 mm | 有概念轴孔关系，但没有肩台、轴向止挡/轴承等承载细节 |
| post ↔ neck | 全帧最小 **8 mm**，交集 0；post Ø36，neck 内孔 Ø52，仅 z=895..900 重合 5 mm | **Fixed 连接缺乏几何实现**，不存在夹持、缩径或实体支承 |
| post ↔ head | 全帧最小 **50 mm**，交集 0 | 立柱与机头轴柱端部也没有接触，不能用轴柱补足套筒连接缺口 |
| base ↔ post | 所有帧交集 **27,482.6525 mm³**，最近距离 0 | 静态立柱嵌入底盘实心凸台 27 mm；可作为固定概念连接，但未建安装孔/独立件真实装配细节 |
| neck ↔ rotor | 全帧最小 **120.9928854 mm**，交集 0 | 不碰撞 |
| post ↔ rotor | 全帧最小 **171.9681773 mm**，交集 0 | 不碰撞 |

因此不能笼统写“所有部件零干涉”：base/post 的固定概念连接始终有体积重叠，原工具可能过滤了静态 Fixed 对。准确表述是：**运动部件的所有配对，在全部保存姿态中没有穿透；静态 base/post 固定连接有上述重叠。**

rotor 只有轮毂与三块叶片。机头前腔从 Y=118 开始，rotor hub 后面在 Y=148，中心轴方向留 **30 mm** 空隙；没有轴体从机头后壁延伸到轮毂，冻结 IR 没有 shaft/bearing 支承特征。Revolute 关节提供的是运动学约束，不能视为实体电机轴。post/neck 的 8 mm 间隙则是实测两个实体没有接触，不能仅凭它们 grounded/Fixed 认为连接已经完成。

## 媒体和导出读取

GIF 格式有效，尺寸 **800×600**，**81 帧全部解码成功**，每帧 50 ms，总时长 **4050 ms**，`loop=0`（循环）。71 个不同解码画面，其余为周期性重复姿态，结合完整矩阵与中间图确认不是静态/伪动画。GIF 首尾回到同一周期位置，实际中间图包含显著朝向与叶轮角度变化。

本轮公开 HTTP 记录：scene 200、静态 `e2e-model.FCStd` 200、`assembly.FCStd` 200、GIF 200。独立在磁盘上读取 GIF 和两份 FCStd，静态 CAD 的全部 Body 有效，装配文件加载出 101 个对象。未另开服务或浏览器点击 UI 播放/下载；媒体文件完整可读与公开 HTTP 状态已验证，UI 控件交互没有额外断言。

FreeCADCmd headless 打开装配文件时，输出了三次上游 `CommandCreateSimulation.py` 的 `NameError: QtCore is not defined` GUI 模块诊断；打开操作未抛给测量脚本，文件对象加载完成。这是可复现的 headless 装配读取诊断，不能据此断言桌面 FreeCAD 无法打开，也不能把它隐藏为完全无诊断。

## 过程问题与未解决项

本轮 14 次工具错误：首个为步骤 5 的 cylinder 字段 schema 错误；7/13/43 步加料完全包含导致 no-op；10 步 pocket 使用无效 placement；11/28 步缺 extend_existing；35 步 cut no-op；39 步查询不存在的 h_cage 局部量测；54 步使用旧 length 字段；60 步非法 plane.kind；62/67 步槽轮廓未闭合；65 步 Coincident refs 数量错误。最终都没有阻止 v24 编译与概念动画交付，但返工成本较高。

还未解决：post/neck 物理固定、rotor 实体轴与轴承支承、机头枢轴轴向承托、前防护网罩，以及气动叶片形状。下一步优先应在几何修改后重新检查**全部实际连接，包括 grounded/Fixed 对**，然后继续保留相同动作与媒体。请求没有给定总高 1150 mm；这只是模型自拟假设，最终高 1329 mm 不作为违反用户硬性尺寸要求。

**最终判断：可识别落地风扇 + 真实自转/往复摇头概念动画完成；机械连接完整性未通过，工程设计尚未完成。**
