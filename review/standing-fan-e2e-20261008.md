# 落地风扇真实端到端测试与项目优化 — 2026-10-08

## 当前结果与范围

从当时本地 `main` 的 `5f6b33d` 建立 managed worktree `/Users/slg/.codex/worktrees/standing-fan-e2e/text_to_cad`，开发分支 `codex/e2e-standing-fan-motion`。原工作目录未改动。工作期间本地 main 又有其他提交；本分支未改基线或混入这些变更。

同一从零输入始终是 **“创建一个落地风扇，带动画和摇头。”**。真实服务使用保存配置中的 `deepseek-v4.1-flash`，FreeCAD 26.3.0 / OndselSolver；credentials 仅在内存读取，没有复制 settings、打印密钥或提交运行数据。所有生成通过声明式 IR 与独立 FreeCAD worker 执行。

最终从零运行 `fan-final` 完成可识别的简化落地 duct 风扇与实际自转/往返摇头；5 个有效单实体 Body、4 个原生关节、2 个独立角驱动、161 个保存姿态。机头 ±40°，一周期有2次方向反转；扣除机头带动后的转子相对角路径4320°，最大采样步27°。底座、立柱和颈部固定。81帧 motion GIF 可全部解码，HTTP播放资源与原生CAD下载通过。

独立黑盒进一步指出实体支承仍不完整，见其最终报告：立柱与套筒之间8mm间隙，叶轮缺少实体轴支承。项目不将 `case_passed` 当作机械连接或送风性能验收。反馈续修 `fan-connected` 已完成（38步、598.25秒、7次工具错误），冻结v32与当前版本一致，动作与媒体仍通过。新增实体Ø16轴/Ø42轴颈、转子Ø43.4通孔，以及颈部Ø37缩径盲孔。该结果属于真实模型续修，不计为新的从零成功样本。立柱夹紧、枢轴承托、轴向止挡、防护网罩和叶片翼型仍未完成，保留概念/机械完整性的区别。

## 实际运行记录

| 运行 | 步数 / 秒 | 工具错误 | 实际结果 |
| --- | --- | --- | --- |
| fan-before / 原main | 32 / 343.13 | 10 | Gate与GIF交付通过，81帧；独立相对姿态检查发现转子相对机头路径为0，只有继承摇头。原交付布尔值误判核心功能 |
| fan-after / 第一轮修复 | 80 / 971.73 | 13 | 已有321帧、真实相对旋转，但未导出最终GIF；第80步provider HTTP522中止。圆放样误测导致约20步不必要返工 |
| fan-blackbox / 子代理独立从零 | 35 / 429.67 | 9 | provider HTTP521中止；最后成功v6、当前v14，0关节/运动帧/GIF。机头离立柱86mm，叶轮嵌入机头70945.86mm³ |
| fan-final / 第二轮修复 | 74 / 1271.37 | 14 | 当前v24与冻结v24一致，Gate、实际嵌套运动、GIF及公开HTTP交付通过；独立检查保留实体连接缺陷 |
| fan-connected / 黑盒反馈续修 | 38 / 598.25 | 7 | 真实模型在复制的v24上补轴与缩径套筒，冻结v32；核心动作及GIF/API继续通过，机械固定细节仍不完整 |

基线的0相对转角与新输出的4320°是实际姿态证据，不能仅凭几次有序运行推算统计成功率。修复后仍需要长轮次，schema错误、连接完整性和provider稳定性尚有改进空间。

## 已落地的项目修复

1. **扣除父部件运动**：`motion_summary.joints` 从所有保存矩阵测量关节相对角范围、累计角路径、反转次数和最大角步；时间驱动相对路径为0时明确警告。无需运行模型公式，避免以机头带动误认扇叶自转；参考向量按子关节自身轴取垂直方向，覆盖初始轴方向不同的矩阵情况。真实内核另验证非零初始相位的相对角路径。
2. **拒绝不完整原生仿真**：Ondsel曾报成功却仅保存首帧，现在核对请求时间区间应有的完整帧数；建议降低速度/步长重试。
3. **实际几何尺寸**：采用 `optimalBoundingBox(False, False)`，不依赖显示网格。原生圆放样真实280×280mm，保守BoundBox给280×313.69mm，导致模型拆除正确底座；增加真实内核回归。
4. **冻结几何安装测量**：`geo_measure(pairs=[[body_a,body_b],...])` 读取固定FCStd测距及BRep交集，不重新编译或求解。区分实体间距与表面间距，完全包含时不能把正表面间距误叫作实体间隙。
5. **可行动的建模诊断**：加料完全被包含时说明no-op与轴/轴承空腔；批次schema失败明确整批未执行、需完整重发；同shape的endpoint/align分支只诊断相关变体；已有Body新增材料的错误标出recipe并说明每项需要extend_existing。
6. **建模帮助**：说明 rimmed wheel 与自由叶尖 rotor 的区别、polar copies默认独立Body、放样曲面低面数不是多边形；给出圆柱/圆锥方向旋转、静态连接、父子相对运动与采样指导。按最终黑盒反馈补充：每次改动后重查ALL连接，grounded不产生夹持、支承或实体轴。
7. **可复用真实E2E**：`--case standing-fan` 采用仅看公开输出的运动验收，条件不注入模型提示；自动要求motion GIF及HTTP交付。`--settings-dir`只读配置；`--resume-from`只复制CAD模型与冻结产物到独立目录，不复制settings或会话数据库，支持失败后续修。

## 独立黑盒与浏览器证据

第一次黑盒由子代理从空模型运行同一句请求，未读tcad源码、修复差异、验收器或先前结果。其反馈促成静态pair测量与包含no-op诊断。最终黑盒读取冻结v24公开scene、IR、FCStd和GIF，实际查看静态及中间姿态，并独立检查所有161姿态中的全部10个Body配对。

所有含head/rotor的运动对交集为0；head↔rotor最小2.53814mm，neck↔head为3mm。唯一重叠是静态固定base↔post的27482.65mm³，各帧相同。此处没有把固定装配嵌合当作运动碰撞，也没有以“无碰撞”证明支承存在。

另通过Playwright技能在真实浏览器打开本次输出的UI，加载v24、5零件/21特征、10230三角面/161帧，实际点击播放与暂停，滑块从0变化并停在124，重复读取保持124，归零恢复起点，浏览器console无error，并实际通过FreeCAD按钮下载模型；截图见 `output/playwright/standing-fan-start.png` 与 `standing-fan-paused.png`。CLI输出本来没有UI会话，因此仅在隔离测试数据库添加一个指向既有模型的预览会话，未重新生成或改动IR。

最终从零输出另执行生产 `POST /models/e2e-model/exports` 与返回URL的GET：FCStd137057字节、STEP64047字节、STL50556500字节，均200且下载字节数匹配。证据 `fan-final/download-verification.json`。续修v32同样验证全部3种格式POST/GET均200：FCStd236531字节、STEP74100字节、STL51708156字节，见 `fan-connected/download-verification.json`。STL较大，这一轮未优化网格下载体积。

## 验证与复现

- 完整Python unit：**1740通过，1跳过**；跳过的是 `test_worker_client` 硬编码worktree内FreeCAD路径，该目录没有构建。另有既有Starlette/anyio弃用警告。
- 指定真实FreeCAD的受影响contract：**48通过，0跳过**（原生装配、圆放样、固定几何pair测量、primitive placement、no-op/build failures）。
- 最后一次合并运行unit与上述contract：**1788通过，1跳过，1项既有警告**，用时35.77秒。
- Node frontend：**110通过，0跳过**。
- `git diff --check` 通过；运行证据、二进制和截图在ignored output里。

```sh
TCAD_FREECAD_CMD=/Users/slg/workspace/text_to_cad/free-cad/FreeCAD/build/debug/bin/FreeCADCmd \
  /Users/slg/workspace/text_to_cad/.venv/bin/python tools/run_model_e2e.py \
  --case standing-fan --settings-dir /Users/slg/workspace/text_to_cad/data \
  --output-dir output/local/tcad_e2e/fan-new-run --max-steps 120 --timeout 1800
```

真实服务会产生provider用量，复现结果受provider和模型采样影响。`fan-final`身份是 `sha256:f5e17c1e37a3a2ca18480e8df380a803a02855070c7fea379bcafd181b7efa5f`。完整原始事件、summary、result和所有冻结失败/成功尝试都保留于各运行目录，未隐藏不成功轮次。

验收器范围是保存姿态的概念动作与落地比例；实体连接、外形细节、全程连续碰撞、载荷、电机传动和气流不能从该布尔值推断。当前叶片是平直矩形、开放机头，没有气流仿真。

## 反馈续修的实际配合与交付

子代理独立读取v32 BRep剖面、GIF、中间矩阵与全部161帧：Ø42轴颈在Ø43.4转子通孔内覆盖完整24mm轴长，径向间隙0.7mm，全帧不穿透；Ø36立柱在Ø37颈部盲孔内重合24mm，径向间隙0.5mm。原来8mm的大间隙及转子缺轴问题得到实质改进，但立柱顶部距盲底仍2mm，枢轴与颈部仍3mm径向间隙、15mm轴向余量。没有把这些余量称为已完成夹紧或承载。概念动作保持±40°往复和12圈相对自转，81帧720×720的motion GIF全部可读。

最终使用 `fan-connected` 的v32交付；身份 `sha256:bde722879cd01560f72596eb0d488a877be35aec4aef6ba9c044b304050bd6dd`。其summary保存 `resumed_from`，续修要求文本保留在 `output/local/tcad_e2e/fan-connection-followup.txt`，原始请求raw_text仍为原句。子代理完整剖面与161姿态复核支持上述概念配合结论；最终交付仍为draft，未把夹紧/轴向保持提升为已完成。另保留一份仅启动失败、没有调用模型的目录 `fan-connected-startup-failed`：新增续修入口初次用了错误的StoreAdapter方法名，随后改为load并完成真实续修。

对应独立报告：`review/standing-fan-blackbox-20261008.md`（独立从零失败）、`review/standing-fan-final-blackbox-20261008.md`（从零最终概念通过）、`review/standing-fan-connected-blackbox-20261008.md`（续修几何与动作复核）。
