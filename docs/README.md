# 文档索引

这里保留当前实现的设计、配置和使用说明。测试数量及验收日期只代表对应记录的运行结果。

- [构建运行时与 Viewer](build-runtime-refactor.md)：缓存、调度、worker 池和截图配置。
- [产物查询边界](artifact-boundary-refactor.md)：不可变产物、测量、Scene 和兼容性。
- [原生装配](native-assembly.md)：关节、驱动、运动检查与示例。
- [原生草图曲线](native-sketch-curves.md)：椭圆和 B 样条的输入及约束。
- [紧凑曲面与旋翼](compact-shape-recipes.md)：用截面及尺寸生成原生 CAD，减少模型坐标计算与修复负担。
- [特征引用](feature-references.md)：特征树、面边引用和选择身份。
- [功能验收](functional-acceptance.md)：需求复核与完成条件。
- [提示冻结与独立评测](generalization-evaluation.md)：按需流程、固定基线和真实模型验收边界。
- [小模型固定回归评测](small-model-regression.md)：固定任务集、重复运行的流水线指标与独立验收边界。
- [Agent 运行时加固](agent-runtime-hardening.md)：预算、重试和停滞检测。
- [权限模式](access-modes.md)：访问与工具权限配置。

早期需求问卷、架构/UI 设计、预览验收、削笔器实测及截图/STEP 样例已移至
[历史资料](../review/history/README.md)。历史记录中的接口、配置和完成状态不能作为当前实现依据。
