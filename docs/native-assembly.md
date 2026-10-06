# FreeCAD 内置 Assembly 接入

本项目通过 FreeCAD 自带的 Assembly/OndselSolver 求解装配；网页只播放求解后的
刚体姿态，不以预设 ratio 替代原生关节计算。范围是内置 Assembly，不包含第三方
装配工作台、FEM 变形或切削材料去除。

## 关节与驱动

全部 13 种内置关节均可声明：Fixed、Revolute、Cylindrical、Slider、Ball、Distance、
Parallel、Perpendicular、Angle、RackPinion、Screw、Gears、Belt。
真实内核回归覆盖前六类被动/方向约束的静态求解，以及旋转、滑动、圆柱双驱动、
齿轮、皮带、螺杆和齿条齿轮运动。

按原生工作台规则，Angular 驱动 Revolute/Cylindrical，Linear 驱动
Slider/Cylindrical。球铰等作为被动关节通过机构约束联动，不虚构原生不存在的直接驱动。
多关节、多驱动与圆柱双驱动可以同时配置；约束冲突或自由度不匹配由原生求解器报告。

## 工具流程

模型根据需求及其复杂度自行判断使用单 Body 还是多 Body，结合功能、制造边界、
可拆卸性和相对运动选择能满足需求的最简单结构。特征较多的一体零件仍可用一个
Body；需要独立几何或运动的多个组件再拆成有明确名称的 Body。特征数量本身
不是拆分依据，用户明确提出的单零件、一体成型或装配要求优先。

决定拆分时先规划零件、摆放位置和连接关系；零件相互接触或同步运动不必合并。
修改已有模型时保留原有零件边界，建模计划中按需简述选择单 Body 或多 Body 的理由。

新增草图和特征时用 `payload.body_id` 指定所属零件。多个 Body 时省略此字段会
拒绝整批补丁，反馈可选 Body ID；空模型仍支持自动创建 `body_1`，只有一个 Body
时仍支持省略字段。显式传入的 `body_id` 必须是非空字符串并指向已有 Body。
更新已有草图/特征使用 `target_id`，保持原有归属；`ir_list_features` 返回各特征
的 `body_id`，供修改前确认零件边界。
历史事件重建保留旧版的首 Body 默认归属；新的写入执行上述显式路由规则。

1. 先列出零件、摆放位置及连接关系，用 `cad_build_parts` 或 `ir_patch` 建立独立零件。
   先清除原有 `body.motion`，避免混用两套运动定义。
2. `assembly_configure` 原子保存完整装配声明，也可传 `assembly=null` 清除。
   按实际连接选择 Fixed 或可动关节；静态装配不需要虚构运动驱动。配置后 `ir_commit`。
3. `assembly_solve` 查看已保存的静态求解结果；`assembly_simulate` 读取已保存的
   运动帧并按需检查零件干涉。修改后需重新 `ir_commit` 生成当前版本的结果。
4. 网页自动加载原生姿态，支持播放/暂停、拖动定位、归零、倍速与循环。
5. `assembly_export` 导出 GIF、MP4、AVI 或 WebM；重复导出复用当前 IR 的帧缓存。
   视频依赖 `pip install -e ".[animation]"` 和对应编码器；GIF 使用已有 Pillow。

```json
{
  "assembly": {
    "grounded": ["housing"],
    "joints": [{
      "id": "shaft_axis", "type": "Revolute",
      "side1": {"body_id": "housing", "position": [28,0,38], "axis": [0,1,0]},
      "side2": {"body_id": "shaft", "position": [28,0,38], "axis": [0,1,0]}
    }],
    "drivers": [{"joint_id": "shaft_axis", "type": "Angular", "formula": "2*pi*time"}],
    "start": 0, "end": 1, "step": 0.05
  },
  "reason": "Build native crank mechanism"
}
```

位置为世界坐标 mm，轴为世界方向；`angle_deg` 为连接坐标系的绕轴滚转。
可指定最终 Body 的 `element`/`vertex`（如 Face3、Vertex1）引用原生拓扑。
Angular **公式使用弧度**，Linear 使用 mm，time 使用秒；连接角和角度限位使用度。
支持原生数学公式与 initialValue，不执行模型提供的 Python。
Gears/Belt 的 distance、distance2 是节圆半径；RackPinion 的 distance 是节圆半径；
Screw 的 distance 按原生 pitch 语义传入。

原生长度/角度上下限可声明，按上游求解语义处理。驱动公式不会由网页自动裁剪；
不能把声明限位当作每帧已满足限位的独立证明。
单次最多 600 个求解帧，原生序列前置的未求解输入状态被排除。

## 证据与导出

`assembly_simulate(check_pairs=[["housing","shaft"]], check_stride=2)` 对原生姿态
采样计算 BRep 交集；摘要最多返回十个干涉项，完整结果留在文件中。
采样范围外的碰撞、接触力、摩擦与切削不在验收范围内。

产物位于 `data/artifacts/<model>/vN/assembly/`：animation.json、assembly.FCStd、
动画媒体。FCStd 保存原生装配、关节和运动数据代理，帧文件保存参考网格及每帧矩阵。
原始 PartDesign 零件仍保留供编辑；普通 `ir_commit` 验证零姿态几何，与运动求解分别报告。
多 Body 的 Compound 表示组合几何；装配约束是否成立需要查看保存的原生求解结果。

复现削笔机构：

```sh
.venv/bin/python tools/agent_driver.py --new --model-id sharpener_native_trial \
  --data-dir .tcad_native --calls tools/sessions/pencil_sharpener_native.json
```

实现参考本地上游 `Mod/Assembly/JointObject.py`、`CommandCreateSimulation.py` 和
`App/AssemblyObject.cpp`。无 GUI 时仅加载上游 Simulation/Motion 数据代理，
避免 GUI 播放器类的 QtCore 导入问题；关节与仿真计算始终由原生 C++ 求解器执行。

本轮验证：1383 项 Python 单元/契约测试、30 项前端测试通过。
真实削笔机构为 7 个 Body，原生求解生成 21 个时间帧；11 个采样帧、4 对零件
无超过 1e−6 mm³ 的交叠。GIF/MP4 实物产物导出成功，AVI/WebM 编码回归通过。
网页实测播放、暂停与定位到 0.25 s。
