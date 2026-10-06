# 紧凑曲面与旋翼建模

`cad_build_parts` 可将少量尺寸参数展开为原生可编辑 CAD，减少模型手写截面平面、
草图、约束和旋转坐标的负担。每项仍经过普通声明式 IR 补丁及 FreeCAD 编译，
不执行模型生成的 Python，不绕过几何 Gate。

## 曲面截面

`shape=loft` 接受 `section_axis` 和 2–12 个有序截面。`center` 为世界坐标，
`radii` 为两个半轴，单位 mm。截面沿选定轴严格递增或递减；允许中心线横向变化。

| section_axis | radii[0] | radii[1] |
| --- | --- | --- |
| X | 世界 Y 半轴 | 世界 Z 半轴 |
| Y | 世界 X 半轴 | 世界 Z 半轴 |
| Z | 世界 X 半轴 | 世界 Y 半轴 |

例如光滑外壳：

```json
{
  "parts": [{
    "id": "shell", "body_id": "housing", "shape": "loft", "section_axis": "X",
    "sections": [
      {"center": [0, 0, 0], "radii": [12, 8]},
      {"center": [30, 0, 0], "radii": [24, 16]},
      {"center": [70, 0, 0], "radii": [15, 10]}
    ]
  }],
  "reason": "建立原生光滑外壳"
}
```

工具生成独立 datum plane、全约束椭圆草图和原生 AdditiveLoft。默认 `ruled=false`
使用光滑放样，`ruled=true` 使用直线生成段。`operation=cut` 在已有 Body 中生成
SubtractiveLoft。截面半轴大小互换时，工具自动选择椭圆主/次轴及方向。
OCC 的光滑曲面包围盒可能保守，仍需测量或渲染实际产物。

## 无轮缘桨叶

`shape=rotor` 生成一个轮毂和 2–8 片径向桨叶，融合成一个 Body。`axis` 是桨盘法线，
`[0,0,1]` 对应 XY 桨盘，`[0,1,0]` 对应 XZ 桨盘，`[1,0,0]` 对应 YZ 桨盘。
`radius` 为中心到桨尖的距离，`thickness` 沿轴向；不会生成圆轮缘。

```json
{
  "parts": [{
    "id": "fan", "body_id": "fan_body", "shape": "rotor",
    "center": [0, 0, 50], "axis": [0, 0, 1],
    "radius": 80, "blade_count": 5, "blade_width": 14,
    "thickness": 6, "hub_radius": 12
  }],
  "reason": "建立可编辑风扇轮毂与五片桨叶"
}
```

这是平直桨叶的几何草案，不是翼型、螺距或空气动力学验证。带轮缘的结构轮仍用
`cad_wheel`；普通旋翼的原生关节/驱动使用 `ir_help(topic=assembly)` 与
`assembly_configure`，重力吊舱才使用 `assembly_motion`。

## 编辑与反馈

重发原来的 `id`、`body_id` 及新尺寸，工具原子更新已有特征和截面；不要用新 ID
修正旧部件，否则会新增材料。降低桨叶数量会删除多余的生成桨叶；依赖关系及自定义
名称/类型会保护相关特征。返回结果列出原生特征 ID，`ir_get` 读取这些 ID 或 Body ID。
新增生成特征保留在原特征组内，后续加工顺序不变。复用曲面或旋翼 recipe ID 时，
不允许改变形状类型或 Body 归属。

`ir_commit` 仍决定是否得到有效实体。运动检查仍使用保存的原生求解帧，不能把几何
有效性当作定性设计完成。客户没有给出的尺寸仅为草案假设。

低层修复可用 `update_feature` 的 `params_remove` 删除错误参数：`params` 本身仍按
字段合并。不能在同一操作中设置和删除同一个键。装配可空字段的错误会指出实际对象
字段，不再优先返回无关的 null 分支；截图样式/视图及版本号也由工具契约明确列出。
`add_feature.after_feature` 可指定同一 Body 内的插入位置，省略时追加到历史末尾。
