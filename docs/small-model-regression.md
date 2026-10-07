# 小模型固定回归评测

`review/regression/cases.json` 保存四个固定任务：椅子（带弧度靠背与腰部支撑）、L 形
支架、带曲面外壳、简单机构（底座 + 转盘）。与通用冻结评测相同，同一代码版本、系统
提示与配置下重复运行，题目与验收条件互不混入：请求只含用户需求，`key_design` 与
`manual` 是独立验收条件，不进入模型输入。

```sh
.venv/bin/python tools/evaluate_regression.py --freeze
.venv/bin/python tools/evaluate_regression.py --check
.venv/bin/python tools/evaluate_regression.py --run          # 需要显式授权与可用凭据
```

冻结文件记录当前模型可见契约、题目哈希、运行器哈希与重复次数/步数/超时限制；
基线只增不改，契约变化时创建新版本。`--run` 每题重复 `repeats`（默认 3）次，
每次新开隔离数据目录，只保存模型/参数与端点哈希，不保存凭据。
仓库保留任务集和运行器；基线在评测环境中通过 `--freeze` 生成，契约更新时使用
`--baseline <新文件>`，并在 `--check` 与 `--run` 中使用同一文件。

## 记录指标（`tcad/agent/regression.py`）

| 指标 | 定义 |
| --- | --- |
| 首次构建成功率 | 第一次 `ir_commit` 即通过 Gate 的运行占比 |
| 构建恢复 | `build_passed` 表示是否曾通过构建，`first_pass_step` 记录首次通过的步数；与首次构建成功分开统计 |
| 参数错误数 | schema 级拒绝次数（小型模型的主要错误类），其余错误类型分列 |
| 恢复步数 | 首次失败到首次通过构建之间的模型步数；一直未恢复记 `None` |
| 完整交付率 | Gate 通过 + 需求复核通过 + 产物可服务的运行占比（`draft` 分列） |
| 关键设计牺牲 | 取自记录在案的部件计划（`ir_plan`）与设计退化记录：`yes`/`no`/`unknown` |

未记录计划时记 `unknown` 并要求人工按 `key_design` 复核，绝不把“没检测到”当作
“没有牺牲”。报告同时输出错误类型分布与 `draft` 交付数。

## 验收边界

自动指标只覆盖流水线行为。人体舒适性、坐姿适配、承载与疲劳、机械可靠性仍逐题
保留独立人工验收；报告与 `report.json` 中的 `acceptance_boundary` 固定声明这一点。
体积相同或 Gate 通过都不能替代外形与功能的实际复核。
