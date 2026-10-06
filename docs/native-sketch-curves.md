# 原生椭圆与插值 B 样条

草图 IR 的 `geometry` 支持 `ellipse` 和 `bspline`。worker 直接创建
`Part.Ellipse`、`Part.BSplineCurve` 并加入 Sketcher，不转换为折线。
新增字段为可选字段，既有直线、圆、圆弧和点的文档仍可读取。

## 椭圆

```json
{
  "id": "g0",
  "kind": "ellipse",
  "points": [{"x": 30, "y": 40, "z": 0}],
  "major_radius": 20,
  "minor_radius": 10,
  "rotation": 30
}
```

`points` 必须只有一个世界坐标中心，位于草图平面上。半轴单位为 mm，
满足 `major_radius >= minor_radius > 0`。`rotation` 默认为 0，单位为度，
从草图局部 +u 向 +v 旋转；附着平面旋转后，它不等于世界 XY 平面上的角度。
椭圆本身闭合，不需要指定 `periodic`。

## 插值 B 样条

```json
{
  "id": "g0",
  "kind": "bspline",
  "points": [
    {"x": 10, "y": 40, "z": 0},
    {"x": 30, "y": 50, "z": 0},
    {"x": 50, "y": 40, "z": 0}
  ],
  "periodic": false
}
```

输入点是曲线经过的插值点，均使用世界坐标并位于草图平面。
`periodic=false` 默认为开放曲线，需要另外添加边构成有效闭合轮廓才能拉伸或挖槽。
`periodic=true` 构成周期闭合曲线，末尾不要重复首点。
开放曲线允许 2–128 个点，周期曲线允许 3–128 个点；相邻点必须相异。
次数、节点、重数和权重由 FreeCAD 插值算法生成；目前不接受控制点、
`degree`、`knots`、`multiplicities` 或 `weights` 输入。
周期样条经过椭圆上的点，并不意味着生成精确椭圆。

## 约束、编辑与验证

第一阶段使用 `{"type":"Block","refs":[0]}` 固定整条曲线，保持默认的
完全约束检查。通过 `update_sketch` 替换 `geometry` 中的参数或插值点，
再提交重新生成。不要自动暴露内部几何或猜测轴、控制点的约束索引。
当前未接入椭圆轴尺寸和样条控制点的独立尺寸驱动约束。

闭合轮廓仍须满足既有成形要求；自交等问题由内核和现有几何检查报告。
此扩展没有新增放样或扫掠操作。

验证见 `tests/unit/test_sketch_curves.py` 和
`tests/contract/test_sketch_curves.py`：参数拒绝、工具 schema 与补丁往返、
三个基准平面、旋转椭圆体积与外形、开放/周期样条成形、STEP 往返、
原生曲线保存、固定约束自由度及 FCStd 重开后拉伸长度编辑。
