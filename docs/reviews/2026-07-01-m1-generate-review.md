# vcstudio M1 生成区 — AI 自审记录

> 日期 2026-07-01 · harness = `E:\V2.0.0\tools\ai_review`(DeepSeek/MiMo/Claude 三模型)
> 聚焦 diff:`generate/{incar_builder,job_builder,poscar[,potcar]}` 净新逻辑(~400–520 行)

## 各轮

| 轮 | tier | 结论 | 备注 |
|---|---|---|---|
| 1 | light | 5 findings(2×P0/1×P1/2×P2) | 仅 DeepSeek |
| 2 | full | P0 清零,新 1×P1(ENCUT=0) | MiMo API 400 unavailable |
| 3 | light | 2×P1 + 2×P2/P3(进入 edge churn) | 仅 DeepSeek |
| 4 | light | 外部 API 超时未完成 | MiMo 400 / DeepSeek hang;非代码问题 |

## 逐条处置(所有 P0/P1 均代码闭合 + 回归测试)

| ID | 级别 | 问题 | 处置 | commit |
|---|---|---|---|---|
| R1 P0-1 | P0 | job_builder 非数字 ENCUT → cryptic float 崩溃 | 修:清晰 ValueError | ca057ac |
| R1 P0-2 | P0 | lib_root 无默认 | **误报**:`potcar._resolve_root`(None→config)已实现,无 `--lib-root` 冒烟证实;首轮 diff 未含 potcar.py 所致 | — |
| R1 P1-1 | P1 | 浮点 ISPIN 漏判 | 修:`_as_int` 支持 `'1.0'→1` | ca057ac |
| R1 P2-1 | P2 | 计数解析失败保留 elements | **行为正确**(仍可拼 POTCAR/KPOINTS),澄清 docstring | ca057ac |
| R1 P2-2 | P2 | scale==0 不报错 | 修:ValueError | ca057ac |
| R2 P1-1 | P1 | `or 400` 吞用户 ENCUT=0 假值 | 修:成员判断(in)+ 无则 max_enmax 兜底 | 646fe34 |
| R3 P1-1 | P1 | 负 scale 静默生成错误晶格 | 修:NotImplementedError(不静默) | 655a27f |
| R3 P1-2 | P1 | 含磁+counts 缺失不补 ISPIN → VASP 默认 ISPIN=1 静默非磁 | 修:补 ISPIN=2 | 655a27f |
| R3 P2-1 | P2 | bool ENCUT(.TRUE.)静默转 1 | 修:清晰 ValueError | 655a27f |
| R3 P3-1 | P3 | SYSTEM 键 + system_name → 重复 SYSTEM 行 | 修:去重 | 655a27f |
| R3 P3-2 | P3 | 无效 ISPIN 不 warn | **接受不改**:VASP 自身校验 ISPIN | — |

## 结论

净新逻辑经 3 轮评审,8 处真实缺陷全部修复(每条附回归测试),2 处误报/正确行为已辨明,1 处 P3 接受不改。R3 起为 obscure edge churn(参见记忆 `ai-review-harness`:大 diff 会 churn,应用小聚焦 diff)。R4 仅为收敛复核,因外部 API 超时未完成——不阻断 M1:P0/P1 已全部闭合,全套 71 tests 绿 + 真实库端到端冒烟通过。
