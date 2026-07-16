<!-- 本文件由 tools/gen_failure_table.py 从 diagnose.py 自动生成,请勿手改。 -->
<!-- Auto-generated from diagnose.py by tools/gen_failure_table.py — do not edit by hand. -->

# 失败分类表 / Failure taxonomy

VASP 作业失败分类器(`vcstudio/cluster/diagnose.py`)把"作业没成功"变成**可行动的
原因**:综合 调度器终态 + 退出码 + OUTCAR/OSZICAR 完整性 + 日志硬崩签名 + 收敛串 +
能量物理合理性 → **单一分类 + 目标状态 + 是否可续算 + 证据**。规则不覆盖一律
`UNKNOWN` → 交人工(显式状态,绝不静默)。

本表由代码程序化生成:分类学与目标状态直接取自 `diagnose.FAILURE_TO_STATE` /
`diagnose.RESTARTABLE` / `diagnose._VASP_ERROR_TABLE`,与实现零漂移(`tests/`
有同步守卫,代码改了不重新生成即测试失败)。

- **作业分类**:15 类分类结果(含 `CONVERGED` 成功态),映射到
  **4 个目标状态**(`DONE / FAILED / NEEDS_HUMAN / UNCONVERGED`);其中 **3 类可 CONTCAR 续算**。
- **VASP 内部错误签名**:15 条(字面串核对自 pymatgen Custodian
  VaspErrorHandler);命中即具体命名 + 标准补救提示,状态 `NEEDS_HUMAN`——本平台守
  方法学主权,**绝不自动改用户 INCAR**,只精确诊断并交人工。


## 作业失败分类 / Job classification

| 分类 | 触发证据 | 目标状态 | 可否续算 |
|---|---|---|---|
| `CONVERGED` | OUTCAR 达到要求精度且能量物理合理(末离子步电子真收敛) | `DONE` | — |
| `NONCONVERGED` | SCF/几何未收敛(NELM/NSW 耗尽);有输出、无硬崩信号、无收敛串 | `UNCONVERGED` | ✅ 可 CONTCAR 续算 |
| `WALLTIME` | 墙钟耗尽(调度器 TIMEOUT;或裸退出码 137 且有部分输出,疑超墙钟被杀) | `UNCONVERGED` | ✅ 可 CONTCAR 续算 |
| `ZBRENT` | 离子步线搜索崩(ZBRENT: fatal / bracketing interval incorrect / can not reach accuracy) | `UNCONVERGED` | ✅ 可 CONTCAR 续算 |
| `OOM` | 内存耗尽(调度器 OOM;或日志 oom-kill / out of memory) | `FAILED` | — |
| `CANCELLED` | 作业被取消(调度器 CANCELLED) | `FAILED` | — |
| `NODE_FAIL` | 计算节点故障(调度器 NODE_FAIL) | `FAILED` | — |
| `SIGSEGV` | 段错误(SIGSEGV / segmentation fault;或 Intel-MPI 段错误末尾全 1 退出列) | `FAILED` | — |
| `SILENT_EXIT` | 退出码 0 但 OUTCAR 为空(沉默退出,疑输入/环境问题) | `NEEDS_HUMAN` | — |
| `NO_OUTPUT` | 零输出(OUTCAR/OSZICAR 均缺失或空,启动即死;疑缺 POTCAR/输入错) | `NEEDS_HUMAN` | — |
| `BAD_ENERGY` | 有收敛串但能量不合理(E≥0 或 \|E\|>1e4;疑结构重叠/SCF 发散) | `NEEDS_HUMAN` | — |
| `DISK_FULL` | 磁盘满 / IO 错误(No space left / quota exceeded / I-O error / read-only fs) | `NEEDS_HUMAN` | — |
| `SCF_SLOSHING` | 电子步震荡(某 SCF 块打满 NELM 且末步 \|dE\|>1e-2 eV);或收敛串为 NELM 耗尽假阳性 | `NEEDS_HUMAN` | — |
| `USER_STOPPED` | STOPCAR 人工叫停(OUTCAR 见 soft stop);非失败,由人决定续算/放弃 | `NEEDS_HUMAN` | — |
| `UNKNOWN` | 规则不覆盖(调度器泛化失败且无具体原因/日志签名) | `NEEDS_HUMAN` | — |

## VASP 内部错误签名 / VASP internal-error signatures

命中以下任一日志签名 → 具体命名 + 补救提示,目标状态一律 `NEEDS_HUMAN`(不可自动续算,靠改 INCAR 修复,交人工)。

| 分类 | 触发证据(日志签名) | 补救提示 | 目标状态 |
|---|---|---|---|
| `TOO_FEW_BANDS` | `TOO FEW BANDS` | 能带不足:增大 NBANDS | `NEEDS_HUMAN` |
| `TETRAHEDRON` | `Tetrahedron method fails / Routine TETIRR needs special values` | 四面体积分失败(金属/slab 常见):ISMEAR 改 0 或 1、SIGMA≈0.05,或加密 k 点 | `NEEDS_HUMAN` |
| `ZPOTRF` | `LAPACK: Routine ZPOTRF failed / Routine ZPOTRF ZTRTRI` | ZPOTRF 分解失败(常因原子过近/晶胞塌缩):检查结构是否重叠 | `NEEDS_HUMAN` |
| `BRMIX` | `BRMIX: very serious problems` | 电荷混合严重发散:检查初始结构/磁矩,或调 AMIX/BMIX/IMIX | `NEEDS_HUMAN` |
| `EDDDAV` | `Error EDDDAV: Call to ZHEGV failed` | Davidson 本征求解失败:试 ALGO=Normal 或 All | `NEEDS_HUMAN` |
| `EDDRMM` | `WARNING in EDDRMM: call to ZHEGV failed` | RMM-DIIS 本征求解失败:试 ALGO=Normal 或减小 POTIM,并删 WAVECAR/CHGCAR | `NEEDS_HUMAN` |
| `EDWAV` | `EDWAV: internal error, the gradient is not orthogonal` | 梯度不正交(ALGO=All/Damped 常见):试 ALGO=Fast 或 Normal | `NEEDS_HUMAN` |
| `ZHEEV` | `Call to routine ZHEEV failed / EDDIAG: Call to (?:routine )?ZHEEV` | 对角化 ZHEEV 失败:试 ALGO=Normal | `NEEDS_HUMAN` |
| `WAVECAR_CORRUPT` | `while reading plane / while reading WAVECAR` | WAVECAR 损坏/不匹配:删除 WAVECAR 后重跑 | `NEEDS_HUMAN` |
| `INCAR_READ` | `Error reading item / Error code was IERR= 5` | INCAR 读取错误(键名/格式笔误):检查 INCAR 拼写 | `NEEDS_HUMAN` |
| `LREAL` | `ERROR RSPHER / REAL_OPTLAY: internal error / REAL_OPT: internal ERROR` | 实空间投影错误:试 LREAL=.FALSE. | `NEEDS_HUMAN` |
| `SYMMETRY` | `internal error in subroutine PRICEL / POSMAP / group operation missing / Inconsistent Br…` | 对称性判定错误:试 ISYM=0 或调 SYMPREC | `NEEDS_HUMAN` |
| `FEXCF` | `supplied exchange-correlation table` | XC 表错误:检查 POTCAR/GGA 设置是否匹配 | `NEEDS_HUMAN` |
| `AMIN` | `One of the lattice vectors is very long .*AMIN` | 长晶胞混合问题:设 AMIN=0.01 | `NEEDS_HUMAN` |
| `VASP_INTERNAL` | `VERY BAD NEWS / internal error in subroutine` | VASP 内部错误:查 OUTCAR/stdout 详情人工处理 | `NEEDS_HUMAN` |
