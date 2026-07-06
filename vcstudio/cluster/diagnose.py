"""VASP 作业失败分类器(纯函数,不碰网络)。

把"作业没成功"变成**可行动的原因**:综合 调度器终态原因 + 退出码 + OUTCAR/OSZICAR
完整性 + 日志硬崩签名 + 收敛串 + 能量物理合理性 → 单一分类 + 目标状态 + 是否可续算 + 证据。

签名取自原版本(E:\\V2.0.0)真实生产系统 job.py::check_error 与 heal_agent(已在真集群
验证):SIGSEGV(Intel-MPI 段错误末尾全 '1' 列)、ZBRENT、OOM(退出码 137)、SILENT_EXIT
(退出 0 但 OUTCAR 空)、NO_OUTPUT(启动即死/缺 POTCAR)、NELM/NSW 未收敛。

不变式:纯 Python 零 token(确定性核心);规则不覆盖 → UNKNOWN → 交人工,绝不猜
(显式状态绝不静默)。执行取证的是 submitter(可注入假件),本模块只吃文本吐结论。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ── 失败分类学 ────────────────────────────────────────────────────────────────
CONVERGED = 'CONVERGED'
NONCONVERGED = 'NONCONVERGED'      # SCF/几何未收敛(NELM/NSW 耗尽)——可 CONTCAR 续算
WALLTIME = 'WALLTIME'              # 墙钟耗尽——可续算
ZBRENT = 'ZBRENT'                  # 离子步 bracketing 崩——可 CONTCAR 续算(复发需人工调 POTIM/IBRION)
OOM = 'OOM'                        # 内存耗尽——硬失败
CANCELLED = 'CANCELLED'            # 被取消——硬失败
NODE_FAIL = 'NODE_FAIL'           # 节点故障——硬失败
SIGSEGV = 'SIGSEGV'               # 段错误——硬失败
SILENT_EXIT = 'SILENT_EXIT'       # 退出 0 但无 OUTCAR——诡异,交人工
NO_OUTPUT = 'NO_OUTPUT'           # 启动即死/缺 POTCAR,零输出——交人工
BAD_ENERGY = 'BAD_ENERGY'         # 有收敛串但能量不合理——交人工(防收敛却是垃圾数)
UNKNOWN = 'UNKNOWN'               # 规则不覆盖——交人工

# 分类 → job.yaml 状态(复活死态 FAILED/NEEDS_HUMAN)
FAILURE_TO_STATE = {
    CONVERGED: 'DONE',
    NONCONVERGED: 'UNCONVERGED',
    WALLTIME: 'UNCONVERGED',
    ZBRENT: 'UNCONVERGED',
    OOM: 'FAILED',
    CANCELLED: 'FAILED',
    NODE_FAIL: 'FAILED',
    SIGSEGV: 'FAILED',
    SILENT_EXIT: 'NEEDS_HUMAN',
    NO_OUTPUT: 'NEEDS_HUMAN',
    BAD_ENERGY: 'NEEDS_HUMAN',
    UNKNOWN: 'NEEDS_HUMAN',
}

# 可 CONTCAR 续算的分类(冻结 INCAR 重投,见 submitter 续算路径)
RESTARTABLE = frozenset({NONCONVERGED, WALLTIME, ZBRENT})

# 调度器终态原因 token(schedulers.parse_terminal 产出;与本模块 classify 对齐)
R_TIMEOUT = 'TIMEOUT'
R_OOM = 'OOM'
R_CANCELLED = 'CANCELLED'
R_NODE_FAIL = 'NODE_FAIL'
R_FAILED = 'FAILED'          # 泛化失败(Slurm F / PBS X):弱信号,交由取证细化

# 物理合理性通用界(丢弃原版本 Li-S 逐分子窗口):正能量=结构重叠;|E|>1e4=爆掉
_ENERGY_ABSURD = 10000.0

_OOM_EXIT = 137             # 128+9(SIGKILL),常见 OOM/超墙钟被杀
_ZBRENT_RE = re.compile(r'ZBRENT:\s*fatal', re.IGNORECASE)
_SEGV_RE = re.compile(r'sigsegv|segmentation fault', re.IGNORECASE)

# 已知 VASP 内部错误签名(字面串核对自 pymatgen Custodian VaspErrorHandler)。
# 命中 → 具体命名 + 标准补救提示,状态 NEEDS_HUMAN:这些几乎都靠改 INCAR
# (ISMEAR/LREAL/ALGO/SYMPREC/NBANDS…)修复,而本平台守方法学主权**绝不自动改用户 INCAR**,
# 故只精确诊断并给建议交人工,不自动出手(区别于 Custodian 的 in-situ 保姆)。
# 顺序 = 优先级(具体在前,泛化兜底在后);只收清晰致命项,避开可自恢复的告警(如单次
# Sub-Space-Matrix not hermitian),防误报。
_VASP_ERROR_TABLE = [
    (re.compile(r'TOO FEW BANDS'), 'TOO_FEW_BANDS', '能带不足:增大 NBANDS'),
    (re.compile(r'Tetrahedron method fails|Routine TETIRR needs special values'), 'TETRAHEDRON',
     '四面体积分失败(金属/slab 常见):ISMEAR 改 0 或 1、SIGMA≈0.05,或加密 k 点'),
    (re.compile(r'LAPACK: Routine ZPOTRF failed|Routine ZPOTRF ZTRTRI'), 'ZPOTRF',
     'ZPOTRF 分解失败(常因原子过近/晶胞塌缩):检查结构是否重叠'),
    (re.compile(r'BRMIX: very serious problems'), 'BRMIX',
     '电荷混合严重发散:检查初始结构/磁矩,或调 AMIX/BMIX/IMIX'),
    (re.compile(r'Error EDDDAV: Call to ZHEGV failed'), 'EDDDAV',
     'Davidson 本征求解失败:试 ALGO=Normal 或 All'),
    (re.compile(r'Call to routine ZHEEV failed|EDDIAG: Call to (?:routine )?ZHEEV'), 'ZHEEV',
     '对角化 ZHEEV 失败:试 ALGO=Normal'),
    (re.compile(r'ERROR RSPHER|REAL_OPTLAY: internal error|REAL_OPT: internal ERROR'), 'LREAL',
     '实空间投影错误:试 LREAL=.FALSE.'),
    (re.compile(r'internal error in subroutine PRICEL|POSMAP|group operation missing'
                r'|Inconsistent Bravais lattice|non-integer element in rotation matrix'), 'SYMMETRY',
     '对称性判定错误:试 ISYM=0 或调 SYMPREC'),
    (re.compile(r'supplied exchange-correlation table'), 'FEXCF',
     'XC 表错误:检查 POTCAR/GGA 设置是否匹配'),
    (re.compile(r'One of the lattice vectors is very long .*AMIN'), 'AMIN',
     '长晶胞混合问题:设 AMIN=0.01'),
    (re.compile(r'VERY BAD NEWS|internal error in subroutine'), 'VASP_INTERNAL',
     'VASP 内部错误:查 OUTCAR/stdout 详情人工处理'),
]


@dataclass
class Diagnosis:
    """分类结论:类别 + 目标状态 + 可否续算 + 人类可读证据。"""
    failure_class: str
    state: str
    restartable: bool
    evidence: str


def energy_implausible(e) -> bool:
    """通用物理合理性:None(拿不到)/ 正总能(结构重叠)/ |E|>1e4(爆掉)→ 不可信。"""
    if e is None:
        return True
    try:
        v = float(e)
    except (TypeError, ValueError):
        return True
    return v > 0 or abs(v) > _ENERGY_ABSURD


def scan_log(log_tail: str) -> str | None:
    """扫描 VASP stdout/日志尾部的硬崩文本签名 → FailureClass 或 None。

    - SIGSEGV:显式 "SIGSEGV"/"segmentation fault",或 Intel-MPI 段错误特征
      (末尾≥3 个非空行每行仅一个 '1' ——各 rank 退出码列全 1,原版本实测口径)。
    - ZBRENT:"ZBRENT: fatal error"。
    """
    if not log_tail:
        return None
    if _ZBRENT_RE.search(log_tail):
        return ZBRENT
    if _SEGV_RE.search(log_tail):
        return SIGSEGV
    nonempty = [ln.strip() for ln in log_tail.splitlines() if ln.strip()]
    if len(nonempty) >= 3 and all(ln == '1' for ln in nonempty[-3:]):
        return SIGSEGV
    return None


def scan_vasp_error(log_tail: str):
    """扫已知 VASP 内部错误签名 → (label, 补救提示) 或 None(Custodian 字面串)。"""
    if not log_tail:
        return None
    for rx, label, hint in _VASP_ERROR_TABLE:
        if rx.search(log_tail):
            return label, hint
    return None


def _empty(size) -> bool:
    """stat 大小视角:None(文件不存在/取不到)或 0 字节 → 空。"""
    return size is None or size == 0


def _reason_to_class(reason: str | None) -> str | None:
    """调度器终态原因 → 具体 FailureClass(泛化 FAILED 返回 None,交取证细化)。"""
    return {
        R_TIMEOUT: WALLTIME,
        R_OOM: OOM,
        R_CANCELLED: CANCELLED,
        R_NODE_FAIL: NODE_FAIL,
    }.get(reason)


def classify(*, scheduler_reason: str | None = None, exit_code: int | None = None,
             outcar_size=None, oszicar_size=None, log_tail: str = '',
             converged: bool = False, energy=None) -> Diagnosis:
    """单一决策点:所有取证证据 → Diagnosis。

    优先级:①收敛串在场 → 判能量合理性(CONVERGED / BAD_ENERGY);②零输出 →
    区分调度器杀 / 沉默退出 / 启动即死;③有输出未收敛 → 调度器具体原因 > 日志硬崩
    签名 > 退出码 137(OOM) > 未收敛(可续算);④都不覆盖 → UNKNOWN 交人工。
    """
    rcls = _reason_to_class(scheduler_reason)

    # ① 收敛串在场:成功当且仅当能量物理合理
    if converged:
        if energy_implausible(energy):
            return Diagnosis(BAD_ENERGY, FAILURE_TO_STATE[BAD_ENERGY], False,
                             f'OUTCAR 报收敛但能量不合理(E={energy});疑结构重叠/SCF 发散,需人工核对')
        return Diagnosis(CONVERGED, 'DONE', False,
                         f'OUTCAR 达到要求精度,E0={energy} eV')

    # ② 零输出:OUTCAR 与 OSZICAR 都空/缺
    if _empty(outcar_size) and _empty(oszicar_size):
        if rcls is not None:
            # 零输出连 CONTCAR 都没有 → 即便 WALLTIME 也无从续算,restartable=False
            return Diagnosis(rcls, FAILURE_TO_STATE[rcls], False,
                             f'调度器报 {scheduler_reason} 且无任何输出(杀于产出前,无 CONTCAR 可续)')
        if exit_code == 0:
            return Diagnosis(SILENT_EXIT, FAILURE_TO_STATE[SILENT_EXIT], False,
                             '退出码 0 但 OUTCAR 为空——沉默退出,疑输入/环境问题,需人工')
        return Diagnosis(NO_OUTPUT, FAILURE_TO_STATE[NO_OUTPUT], False,
                         '零输出(OUTCAR/OSZICAR 均缺失或空)——启动即死,疑缺 POTCAR/输入错,需人工')

    # ③ 有输出但未收敛
    if rcls is not None:                          # 调度器具体原因优先
        return Diagnosis(rcls, FAILURE_TO_STATE[rcls], rcls in RESTARTABLE,
                         f'调度器报 {scheduler_reason};作业有部分输出但未收敛')
    sig = scan_log(log_tail)                       # 日志硬崩签名
    if sig is not None:
        return Diagnosis(sig, FAILURE_TO_STATE[sig], sig in RESTARTABLE,
                         f'日志命中 {sig} 签名')
    if exit_code == _OOM_EXIT:                      # 137 = OOM/被杀
        return Diagnosis(OOM, FAILURE_TO_STATE[OOM], False,
                         f'退出码 {exit_code}(128+9 SIGKILL)——疑 OOM/超墙钟被杀')
    ve = scan_vasp_error(log_tail)                  # 已知 VASP 内部错误(命名+建议,交人工)
    if ve is not None:
        label, hint = ve
        return Diagnosis(label, 'NEEDS_HUMAN', False, f'命中 VASP 已知错误 {label}:{hint}')
    if scheduler_reason == R_FAILED:               # 泛化失败无更具体信号 → 交人工
        return Diagnosis(UNKNOWN, FAILURE_TO_STATE[UNKNOWN], False,
                         '调度器报失败但无具体原因/日志签名,需人工')
    # 有输出、无硬崩信号、未见收敛串 → SCF/几何未收敛(可 CONTCAR 续算)
    return Diagnosis(NONCONVERGED, 'UNCONVERGED', True,
                     '有输出但未见"reached required accuracy"——SCF/几何未收敛,可从 CONTCAR 续算')


# ── CONTCAR 续算前校验(valid_poscar 移植) ─────────────────────────────────────
def valid_poscar(text: str) -> bool:
    """校验一段文本是否是可用的 VASP5 POSCAR/CONTCAR(续算前防拿到半个文件)。

    要求:≥8 行;第6行是元素符号(非纯数字);第7行是正整数计数;坐标行数 ≥ 总原子数
    (容忍 Selective dynamics 行:若第8行以 s/S 开头则坐标从第9行起)。
    """
    if not text:
        return False
    lines = text.splitlines()
    if len(lines) < 8:
        return False
    species = lines[5].split()
    if not species or all(_looks_int(tok) for tok in species):
        return False                       # 第6行全是数字 → VASP4,无元素符号行
    counts_toks = lines[6].split()
    if not counts_toks or not all(_looks_int(tok) for tok in counts_toks):
        return False
    try:
        total = sum(int(t) for t in counts_toks)
    except ValueError:
        return False
    if total <= 0:
        return False
    coord_start = 8
    if len(lines) > 7 and lines[7].strip()[:1] in ('s', 'S'):
        coord_start = 9                    # Selective dynamics 行占一行
    coord_lines = [ln for ln in lines[coord_start:] if ln.strip()]
    return len(coord_lines) >= total


def _looks_int(tok: str) -> bool:
    try:
        int(tok)
        return True
    except ValueError:
        return False
