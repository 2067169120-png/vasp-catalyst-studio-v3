# 失败验证 + 有界恢复 + 报告生成 — 设计 spec

日期：2026-07-05　分支：`feature/gui-exe-config`　作者：Claude(自主通宵,对齐论文)

## 背景与依据

用户要求:对齐参考论文(OpenClaw, *J. Chem. Theory Comput.* 2026)把简化版做成可信产物,重点补 **任务失败验证** 与 **报告生成**;并从原版本 `E:\V2.0.0\` 学习。一次并行研究(workflow `wupm9w1yb`)对原版本机制做了逐行审计并产出 9 条缺口分析;本 spec 是其落地设计。领域知识(VASP 失败签名)直接取自原版本——它是真实生产系统,已把 SIGSEGV/ZBRENT/OOM/沉默退出/SCF 震荡等签名验证过。

**论文对齐点**:显式的每阶段**验证条件** + **有界恢复**(分类→有界修正→重试上限→停机/交人工,绝不无界) + **最终研究摘要报告**。论文明说科学有效性(收敛/采样)仍需人工把关——正是要硬化的失败验证。

**三条不变式(交接文档 §1,不可违反)**:①方法学主权(绝不改用户 INCAR 已写的键)②确定性核心零 token(修复/报告全纯 Python)③显式状态绝不静默(规则不覆盖即停机标 `NEEDS_HUMAN`)。

## 现状(要修的洞)

`submitter.refresh_job` 的全部失败验证 = 一次 grep:作业从调度器消失(GONE,由 `parse_status` 的**缺席**推断)→ 远端 `grep -c "reached required accuracy" OUTCAR`(`2>/dev/null || echo 0`)+ `tail -2 OSZICAR` 取 E0。count>0 → `DONE`;其余一律 `UNCONVERGED`(单一 catch-all)。`VALID_STATES` 声明 9 态但 **`FAILED`/`NEEDS_HUMAN` 无任何代码置入**(死态)。所有更丰富的信号被上游收集又丢弃:Slurm 的 TO/OOM/CA/NF 终态被 `_MAP` 压平成 GONE;脚本自己写的 `echo EXIT: $?` 标记从不读取;stdout/日志从不拉取解析;E0 无论收敛与否都存、无物理合理性检查。

## 设计:三层,全部纯函数可离线测,零新依赖

### 1. `vcstudio/cluster/diagnose.py` — 纯函数失败分类器(P0,基石)

失败分类学 `FailureClass`(取自原版本 `job.py::check_error` + `heal_agent` 签名):

| Class | 触发签名(证据) | 来源文件 | → 状态 | 可续算 |
|---|---|---|---|---|
| `CONVERGED` | OUTCAR 含 "reached required accuracy" 且能量物理合理 | OUTCAR/OSZICAR | `DONE` | — |
| `NO_OUTPUT` | OUTCAR 缺失/0 字节 且 OSZICAR 缺失/0 字节(启动即死/缺 POTCAR) | stat 大小 | `NEEDS_HUMAN` | 否 |
| `SILENT_EXIT` | exit_code==0 但 OUTCAR 0 字节 | EXIT 标记+stat | `NEEDS_HUMAN` | 否 |
| `OOM` | exit_code==137,或调度器 OOM | 日志/调度器 | `FAILED` | 否 |
| `WALLTIME` | 调度器 TIMEOUT(Slurm TO / PBS 墙钟确认) | 调度器 | `UNCONVERGED` | 是 |
| `CANCELLED` | 调度器 CANCELLED | 调度器 | `FAILED` | 否 |
| `NODE_FAIL` | 调度器 NODE_FAIL | 调度器 | `FAILED` | 否 |
| `SIGSEGV` | 日志末≥3 非空行全为 '1'(Intel-MPI 段错误列),或含 "SIGSEGV"/"segmentation fault" | 日志 | `FAILED` | 否 |
| `ZBRENT` | 日志/OUTCAR 含 "ZBRENT: fatal error" | 日志/OUTCAR | `UNCONVERGED` | 是(CONTCAR 续) |
| `NONCONVERGED` | 有输出但无 accuracy 串(SCF/几何未收敛,NELM/NSW 耗尽) | OUTCAR/OSZICAR | `UNCONVERGED` | 是 |
| `BAD_ENERGY` | 有 accuracy 串但能量不合理(None / E>0 / \|E\|>10000) | OSZICAR | `NEEDS_HUMAN` | 否 |
| `UNKNOWN` | 规则不覆盖 | — | `NEEDS_HUMAN` | 否 |

核心纯函数:
- `classify(*, scheduler_reason, exit_code, outcar_size, oszicar_size, log_tail, converged, energy) -> Diagnosis(failure_class, state, restartable, evidence)`——单一决策点,优先级:先判 CONVERGED(+能量合理性)→ 再调度器终态 → 再日志硬崩签名 → 再输出完整性 → 再收敛/未收敛。
- `scan_log(log_tail) -> class|None`(SIGSEGV/ZBRENT/OOM 文本签名)
- `energy_implausible(e) -> bool`(通用界:None / >0 / |E|>10000;**丢弃** Li-S 专用窗口)
- `valid_poscar(text) -> bool`(CONTCAR 续算前校验:VASP5 元素行 + 原子数 + 坐标行齐全)

### 2. 接入 `submitter` + `schedulers`(P0,复活状态机)

- `schedulers.py`:新增纯函数 `parse_terminal(raw) -> {jid: reason}`,把 Slurm TO/OOM/CA/NF/F 与 PBS F/X 映成终态原因 token(不动 `parse_status` 的现有契约,保持向后兼容与既有测试)。
- `submitter.refresh_job` 的 GONE 分支重写为:一次组合 SSH 取证(`stat -c%s OUTCAR OSZICAR`、combined 日志尾、EXIT 标记、accuracy grep、E0)→ 调 `diagnose.classify` → 置 `DONE`/`UNCONVERGED`/`FAILED`/`NEEDS_HUMAN` + 把 `results.diagnosis={failure_class,evidence,scheduler_reason,classified_at}` 写回 job.yaml,并在失败时追加 `attempts[]` 记录(现仅提交时追加)。
- **DONE 前物理合理性闸**:即便有 accuracy 串,若能量 None/>0/|E|>10000 → 降级 `NEEDS_HUMAN`(防"收敛却是垃圾数"流入 ΔE 表)。

### 3. `vcstudio/project/report.py` — 零依赖批量报告(P1,报告那一半)

扫描项目下/台账里所有 job.yaml,聚合 按状态计数 + 按 failure_class 分类 + 失败/待人工作业清单(带 diagnosis 证据),产出**自包含单文件 HTML**(内联 CSS,无外链,浏览器直接开;不用 matplotlib/docx/openpyxl,守 EXE 零增重)+ 可选 markdown。数据只从 job.yaml(单一真相源)聚合,**不引入 SQLite/并行状态存**。

### 4. `submitter` + GUI:CONTCAR 手动续算(P1,有界恢复)

对 `WALLTIME`/`ZBRENT`/`NONCONVERGED` 等**可续算**分类:校验已拉回的 CONTCAR(`valid_poscar`)→ 提升为新作业目录/attempt 的 POSCAR → **INCAR 逐字冻结**(无可比性护栏前的安全默认,遵方法学主权)重投 → `continue_rounds` 计数硬上限(3)防死循环。**人工触发**(GUI 按钮),对齐 human-in-the-loop;自动轮询续算留待后续。

## 明确不做(缺口分析 do_not_port,守不变式/EXE 约束)

- **自动改写 INCAR 的 HealAgent/orchestrator 渐进松弛**——原团队自己弃用的旧regime,逐作业改方法学键会毁 E_ads 可比性;简化版无可比性护栏,自动改 INCAR 会静默产不可信能量。任何 INCAR 补救必须人工确认、显式标记、一次性,绝不成环。
- Li-S/SAC 科学层(放电路径/PDS/极限电位/物种序/Cui 窗口)、逐分子能量窗口——项目专用。
- Origin/POV-Ray 绘图、python-docx WordReport 的固定方法横幅与参考文献、SMTP 邮件、Windows toast 通知、SQLite/三 JSON 状态合并——依赖重或项目/团队专用,与自包含 EXE + job.yaml 单真相源冲突。

## 实现顺序(TDD,每块跑全量 pytest 再提交)

1. `diagnose.py` + `test_diagnose.py`(fixtures 编码真实签名)
2. `schedulers.parse_terminal` + 接入 `refresh_job` + 完整性/合理性闸 + diagnosis 回写 + 测试
3. `report.py`(HTML/md)+ `test_report.py`
4. CONTCAR 手动续算 + `valid_poscar` + GUI 按钮 + 测试
5. GUI:任务页显示 diagnosis、报告导出按钮
6. 晨间复核报告 artifact

前 3 块即用户要的核心(失败验证 + 报告);第 4-5 块视限流情况推进,保持手动/安全。
