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

import math
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
DISK_FULL = 'DISK_FULL'           # 磁盘满/IO 错误——交人工(盲目续算必再撞满,浪费机时)
SCF_SLOSHING = 'SCF_SLOSHING'     # 电子步震荡(NELM 打满且 dE 不降)——盲目续算必复现,交人工
USER_STOPPED = 'USER_STOPPED'     # STOPCAR 人工叫停——不是失败,人决定下一步
# ── NEB 专属分类(多 image 作业;结构性输入错误,盲目续算无益 → 交人工) ──
NEB_IMAGES_MISMATCH = 'NEB_IMAGES_MISMATCH'  # INCAR 的 IMAGES 与实际 image 子目录数不符
NEB_IMAGE_MISSING = 'NEB_IMAGE_MISSING'      # 某 image 子目录无有效输出(缺失/启动即死)
NEB_IMAGE_SCF = 'NEB_IMAGE_SCF'              # 某 image SCF 崩/震荡(点名 image 编号)
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
    DISK_FULL: 'NEEDS_HUMAN',
    SCF_SLOSHING: 'NEEDS_HUMAN',
    USER_STOPPED: 'NEEDS_HUMAN',
    NEB_IMAGES_MISMATCH: 'NEEDS_HUMAN',
    NEB_IMAGE_MISSING: 'NEEDS_HUMAN',
    NEB_IMAGE_SCF: 'NEEDS_HUMAN',
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
# ZBRENT:除 "fatal error" 外,"bracketing interval incorrect" 与 "can not reach accuracy"
# 同样常见(离子步线搜索失败),均可从 CONTCAR 续算(custodian 亦一并处理)
_ZBRENT_RE = re.compile(
    r'ZBRENT:\s*(?:fatal|bracketing interval incorrect|can\s*not reach accuracy)',
    re.IGNORECASE)
_SEGV_RE = re.compile(r'sigsegv|segmentation fault', re.IGNORECASE)
# OOM 的日志直接证据:Slurm slurmstepd 的 oom-kill 行 / 内核 oom_kill / MPI 被 SIGKILL 收尸
_OOM_LOG_RE = re.compile(r'oom-kill|oom_kill|out of memory'
                         r'|APPLICATION TERMINATED WITH THE EXIT STRING: Killed', re.IGNORECASE)
# 磁盘满 / IO 错误(共享集群极常见):配额满、写盘失败、只读文件系统。
# 盲目续算必再次撞满 → 不可续算,交人工清理后再说(区别于纯墙钟的可续算)
_IO_ERROR_RE = re.compile(
    r'No space left on device|Disk quota exceeded|quota exceeded'
    r'|Input/output error|Read-only file system|Error writing file|write error',
    re.IGNORECASE)

# 已知 VASP 内部错误签名(字面串核对自 pymatgen Custodian VaspErrorHandler)。
# 命中 → 具体命名 + 标准补救提示,状态 NEEDS_HUMAN:这些几乎都靠改 INCAR
# (ISMEAR/LREAL/ALGO/SYMPREC/NBANDS…)修复,而本平台守方法学主权**绝不自动改用户 INCAR**,
# 故只精确诊断并给建议交人工,不自动出手(区别于 Custodian 的 in-situ 保姆)。
# 顺序 = 优先级(具体在前,泛化兜底在后);只收清晰致命项,避开可自恢复的告警(如单次
# Sub-Space-Matrix not hermitian),防误报。
_VASP_ERROR_TABLE = [
    # NEB 并行划分:总核数须能被 IMAGES 整除,否则 VASP 报 M_divide/子群划分失败(NEB 专属签名)
    (re.compile(r'M_divide.*can not|can not subdivide|number of images.*not'), 'NEB_NPAR_DIVIDE',
     'NEB 并行划分失败:总 MPI 进程数须能被 IMAGES 整除(检查 IMAGES 与 -np/NCORE/KPAR)'),
    (re.compile(r'TOO FEW BANDS'), 'TOO_FEW_BANDS', '能带不足:增大 NBANDS'),
    (re.compile(r'Tetrahedron method fails|Routine TETIRR needs special values'), 'TETRAHEDRON',
     '四面体积分失败(金属/slab 常见):ISMEAR 改 0 或 1、SIGMA≈0.05,或加密 k 点'),
    (re.compile(r'LAPACK: Routine ZPOTRF failed|Routine ZPOTRF ZTRTRI'), 'ZPOTRF',
     'ZPOTRF 分解失败(常因原子过近/晶胞塌缩):检查结构是否重叠'),
    (re.compile(r'BRMIX: very serious problems'), 'BRMIX',
     '电荷混合严重发散:检查初始结构/磁矩,或调 AMIX/BMIX/IMIX'),
    (re.compile(r'Error EDDDAV: Call to ZHEGV failed'), 'EDDDAV',
     'Davidson 本征求解失败:试 ALGO=Normal 或 All'),
    (re.compile(r'WARNING in EDDRMM: call to ZHEGV failed'), 'EDDRMM',
     'RMM-DIIS 本征求解失败:试 ALGO=Normal 或减小 POTIM,并删 WAVECAR/CHGCAR'),
    (re.compile(r'EDWAV: internal error, the gradient is not orthogonal'), 'EDWAV',
     '梯度不正交(ALGO=All/Damped 常见):试 ALGO=Fast 或 Normal'),
    (re.compile(r'Call to routine ZHEEV failed|EDDIAG: Call to (?:routine )?ZHEEV'), 'ZHEEV',
     '对角化 ZHEEV 失败:试 ALGO=Normal'),
    (re.compile(r'while reading plane|while reading WAVECAR'), 'WAVECAR_CORRUPT',
     'WAVECAR 损坏/不匹配:删除 WAVECAR 后重跑'),
    (re.compile(r'Error reading item|Error code was IERR=\s*5'), 'INCAR_READ',
     'INCAR 读取错误(键名/格式笔误):检查 INCAR 拼写'),
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
    """通用物理合理性:None(拿不到)/ E≥0(正=结构重叠;恰为 0 只能是解析垃圾)/
    |E|>1e4(爆掉)→ 不可信。真实束缚体系的 DFT 总能恒为负。"""
    if e is None:
        return True
    try:
        v = float(e)
    except (TypeError, ValueError):
        return True
    return not math.isfinite(v) or v >= 0 or abs(v) > _ENERGY_ABSURD


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
    if _OOM_LOG_RE.search(log_tail):
        return OOM
    if _SEGV_RE.search(log_tail):
        return SIGSEGV
    nonempty = [ln.strip() for ln in log_tail.splitlines() if ln.strip()]
    if len(nonempty) >= 3 and all(ln == '1' for ln in nonempty[-3:]):
        return SIGSEGV
    return None


# SCF 震荡阈值(移植原版生产口径 lis_sac_status:scf_iters>=80 且 |dE|>1e-2 判真震荡;
# heal_agent 的 CONVERGENCE_STALL 用 DAV>60——取保守的 80+能量证据双条件防误报)
_SLOSH_MIN_ITERS = 80
_SLOSH_MIN_DE = 1e-2
# 电子步算法行前缀(原版 _SCF_TAGS 全集:DAV/RMM/EDDAV/CG/DMP/QDMP + DIA/NONE)
_SCF_LINE_RE = re.compile(
    r'^(?:DAV|RMM|EDDAV|CG|DMP|QDMP|DIA|NONE):\s*(\d+)\s+\S+\s+([+-]?[\d.E+-]+)',
    re.MULTILINE)


def last_block_scf_iters(oszicar_tail: str) -> int:
    """OSZICAR 尾部最后一个离子步块的电子步数(解析不出 → 0)。"""
    if not oszicar_tail:
        return 0
    last_block = oszicar_tail.rsplit('F=', 1)[-1] if 'F=' in oszicar_tail else oszicar_tail
    matches = _SCF_LINE_RE.findall(last_block)
    return int(matches[-1][0]) if matches else 0


def nelm_saturated(oszicar_tail: str, nelm: int = 60) -> bool:
    """末离子步电子步数 ≥ NELM → 电子未收敛(即便日志出现 EDIFF-reached 串)。

    网研核对(custodian NonConvergingErrorHandler + VASP5 行为):VASP5 在 NELM 耗尽时
    **同样**打印 'aborting loop because EDIFF is reached',静态作业只看该串会假阳性;
    必须叠加本守卫。NELM 从用户 INCAR 读,缺省 60(VASP 默认)。
    """
    n = last_block_scf_iters(oszicar_tail)
    return n > 0 and n >= max(int(nelm or 60), 1)


def scan_oszicar_sloshing(oszicar_tail: str) -> str | None:
    """OSZICAR 尾部 → 最后一个离子步的 SCF 块是否呈电子震荡。

    判据(两条同时满足才报,防误报;原版真实 Co 组标定):任一 SCF 块电子步数 ≥ 80
    且该块末行 |dE| > 1e-2 eV(远未收敛而非慢收敛)。**扫尾部全部块取最坏块**而非只看
    最后一块——原版经验:刚好在新离子步起步时只看末块会漏判。
    命中返回证据串,否则 None。纯文本解析,离线可测。
    """
    if not oszicar_tail:
        return None
    blocks = [b for b in
              (_SCF_LINE_RE.findall(seg) for seg in oszicar_tail.split('F='))
              if b]
    if not blocks:
        return None
    worst = max(blocks, key=lambda b: int(b[-1][0]))   # 电子步数最多的块最像震荡
    n_iter = int(worst[-1][0])
    try:
        de = abs(float(worst[-1][1].replace('E', 'e')))
    except ValueError:
        return None
    if n_iter >= _SLOSH_MIN_ITERS and de > _SLOSH_MIN_DE:
        flips = sum(1 for a, b in zip(worst, worst[1:])
                    if (float(a[1]) > 0) != (float(b[1]) > 0))
        return (f'SCF 块电子步数 {n_iter}(NELM 打满)且末步 |dE|={de:.3g} eV 仍远未收敛'
                f'({flips} 次变号)——SCF 震荡,同 INCAR 续算必复现;'
                f'建议人工调 ALGO/AMIX/SIGMA 或查结构')
    return None


# ── 运行中作业活体健康(移植原版 lis_sac_status 生产经验) ─────────────────────
SLOSH_CONFIRM_POLLS = 2      # 连续确认 2 轮才告警震荡(过滤单轮瞬态,原版标定)
ZERO_STEP_CONFIRM_POLLS = 3  # 连续 3 轮 0 离子步才告警首步假死(给大体系首步 SCF 时间)


def next_poll_count(prev: int, hit: bool) -> int:
    """跨轮确认计数器:本轮命中 +1,否则清零(原版 next_sloshing_state 同款)。"""
    return (int(prev or 0) + 1) if hit else 0


def live_health(oszicar_tail: str, ionic_steps: int, prev: dict | None) -> dict:
    """运行中作业健康评估 → {'sloshing_polls','zero_step_polls','warning'}。

    在作业烧掉整个墙钟前抓出病态(原版经验:活体监控省机时):
    - SCF 震荡(scan_oszicar_sloshing 同判据)连续 ≥2 轮 → 告警;
    - 首步假死(离子步恒 0)连续 ≥3 轮 → 告警。
    只告警绝不自动取消(方法学主权 + human-in-the-loop;原版 AUTO_CANCEL 的人道版)。
    """
    prev = prev or {}
    slosh_ev = scan_oszicar_sloshing(oszicar_tail)
    sp = next_poll_count(prev.get('sloshing_polls', 0), slosh_ev is not None)
    zp = next_poll_count(prev.get('zero_step_polls', 0), ionic_steps == 0)
    warning = ''
    if sp >= SLOSH_CONFIRM_POLLS:
        warning = (f'SCF 震荡已连续 {sp} 轮确认:{slosh_ev};'
                   f'建议人工取消并调 ALGO/AMIX(本工具不自动 qdel)')
    elif zp >= ZERO_STEP_CONFIRM_POLLS:
        warning = (f'首步假死已连续 {zp} 轮确认(0 离子步):首个 SCF 卡死,'
                   f'建议人工检查结构/并行设置(本工具不自动 qdel)')
    return {'sloshing_polls': sp, 'zero_step_polls': zp, 'warning': warning}


def scan_vasp_error(log_tail: str):
    """扫已知 VASP 内部错误签名 → (label, 补救提示) 或 None(Custodian 字面串)。"""
    if not log_tail:
        return None
    for rx, label, hint in _VASP_ERROR_TABLE:
        if rx.search(log_tail):
            return label, hint
    return None


def scan_io_error(log_tail: str) -> bool:
    """日志尾部是否含磁盘满/IO 错误签名(共享集群常见)。命中 → True。"""
    return bool(log_tail) and bool(_IO_ERROR_RE.search(log_tail))


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
             converged: bool = False, energy=None, oszicar_tail: str = '',
             nelm: int = 60, clean_exit: bool | None = None,
             stopped: bool = False) -> Diagnosis:
    """单一决策点:所有取证证据 → Diagnosis。

    优先级:①收敛串 + 干净页脚 + 无非零退出证据 → 判能量合理性
    (CONVERGED / BAD_ENERGY);②零输出 →
    区分调度器杀 / 静默退出 / 启动即死;③有输出未收敛 → 调度器具体原因 > 日志硬崩
    签名 > 退出码 137(OOM) > 未收敛(可续算);④都不覆盖 → UNKNOWN 交人工。
    """
    rcls = _reason_to_class(scheduler_reason)

    # ⓪ STOPCAR 人工叫停:不是失败,人决定下一步(防被误判 NONCONVERGED 而盲目续算)
    if stopped and not converged:
        return Diagnosis(USER_STOPPED, FAILURE_TO_STATE[USER_STOPPED], False,
                         'OUTCAR 见 soft stop(STOPCAR 人工叫停);非失败,由人决定续算/放弃')

    # ⓪′ 磁盘满 / IO 错误:先于一切续算判定拦下——盲目续算必再次撞满,浪费机时。
    # 未收敛时才拦(已收敛+能量合理说明计算其实已完成,末尾写盘报错另说)。
    if scan_io_error(log_tail) and not converged:
        return Diagnosis(DISK_FULL, FAILURE_TO_STATE[DISK_FULL], False,
                         '日志命中磁盘满/IO 错误(No space / quota / I-O error)——'
                         '不可续算,请先清理磁盘或换配额目录再重跑')

    # ① 收敛串在场仍不等于完成：OUTCAR 可能是旧轮残留或在收尾时截断。
    # DONE 必须同时有 timing 页脚，且作业包装无非零退出证据。None 表示未取到
    # 退出码，不当作失败；显式非零则一定不能 DONE。
    if converged:
        if clean_exit is not True:
            return Diagnosis(
                UNKNOWN, FAILURE_TO_STATE[UNKNOWN], False,
                '收敛串在场但无 timing 页脚——可能是旧 OUTCAR 残留或中途截断；'
                '不能判 DONE，也不能自动续算，需人工核对当轮作业日志')
        if nelm_saturated(oszicar_tail, nelm):
            # VASP5 陷阱:NELM 耗尽同样打印 EDIFF-reached 串——收敛标志是假阳性
            return Diagnosis(SCF_SLOSHING, FAILURE_TO_STATE[SCF_SLOSHING], False,
                             f'收敛标志在场但末离子步电子步打满 NELM={nelm}——电子实未收敛'
                             f'(VASP5 对 NELM 耗尽同样打印 EDIFF-reached),能量不可信,需人工')
        if energy_implausible(energy):
            return Diagnosis(BAD_ENERGY, FAILURE_TO_STATE[BAD_ENERGY], False,
                             f'OUTCAR 报收敛但能量不合理(E={energy});疑结构重叠/SCF 发散,需人工核对')
        if clean_exit is True and exit_code in (None, 0):
            return Diagnosis(CONVERGED, 'DONE', False,
                             f'OUTCAR 达到要求精度且见干净页脚,E0={energy} eV')

    # ② 零输出:OUTCAR 与 OSZICAR 都空/缺
    if _empty(outcar_size) and _empty(oszicar_size):
        if rcls is not None:
            # 零输出连 CONTCAR 都没有 → 即便 WALLTIME 也无从续算,restartable=False
            return Diagnosis(rcls, FAILURE_TO_STATE[rcls], False,
                             f'调度器报 {scheduler_reason} 且无任何输出(杀于产出前,无 CONTCAR 可续)')
        if exit_code == 0:
            return Diagnosis(SILENT_EXIT, FAILURE_TO_STATE[SILENT_EXIT], False,
                             '退出码 0 但 OUTCAR 为空——静默退出,疑输入/环境问题,需人工')
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
    if exit_code == _OOM_EXIT:
        # 137 = 128+9(SIGKILL)。此处已排除 OOM 日志证据(scan_log 会先命中真 OOM)与
        # 调度器 OOM/TIMEOUT 原因。裸 137 既可能超墙钟被杀(可续算)也可能 OOM(硬失败),
        # 但**有部分输出**时更常是墙钟。旧版一律判 OOM→FAILED 会困死可续算的墙钟作业
        # (缺口分析 P0);改判为疑墙钟 → WALLTIME(可 CONTCAR 续算),把主动权交回恢复流程。
        return Diagnosis(WALLTIME, FAILURE_TO_STATE[WALLTIME], True,
                         f'退出码 {exit_code}(SIGKILL)但无 OOM 日志/调度器原因,且有部分输出——'
                         f'疑超墙钟被杀,按可续算处理;若续算仍 137 再按 OOM 人工核查')
    ve = scan_vasp_error(log_tail)                  # 已知 VASP 内部错误(命名+建议,交人工)
    if ve is not None:
        label, hint = ve
        return Diagnosis(label, 'NEEDS_HUMAN', False, f'命中 VASP 已知错误 {label}:{hint}')
    if scheduler_reason == R_FAILED:               # 泛化失败无更具体信号 → 交人工
        return Diagnosis(UNKNOWN, FAILURE_TO_STATE[UNKNOWN], False,
                         '调度器报失败但无具体原因/日志签名,需人工')
    if exit_code not in (None, 0):
        return Diagnosis(
            UNKNOWN, FAILURE_TO_STATE[UNKNOWN], False,
            f'退出码 {exit_code} 为非零；即使 OUTCAR 留有收敛串也不能证明作业完整完成，'
            '需核对作业日志和输出完整性')
    slosh = scan_oszicar_sloshing(oszicar_tail)     # 电子震荡:盲目续算必复现 → 交人工
    if slosh is not None:
        return Diagnosis(SCF_SLOSHING, FAILURE_TO_STATE[SCF_SLOSHING], False, slosh)
    # 有输出、无硬崩信号、未见收敛串 → SCF/几何未收敛(可 CONTCAR 续算)。
    # 干净退出页脚(General timing)细化证据:有页脚=跑满自然结束(NSW/NELM 耗尽),
    # 无页脚=中途被杀(墙钟/OOM 未留其他痕迹)——两者都可续算,但人看得懂差别
    if clean_exit is True:
        detail = 'VASP 正常收尾但未达收敛判据(NSW/NELM 耗尽)'
    elif clean_exit is False:
        detail = '无 timing 页脚——中途被杀(疑墙钟/资源)'
    else:
        detail = 'SCF/几何未收敛'
    return Diagnosis(NONCONVERGED, 'UNCONVERGED', True,
                     f'有输出但未见收敛标志——{detail},可从 CONTCAR 续算')


def classify_neb(*, images_expected=None, images_found=None, image_status=None,
                 converged: bool = False, scheduler_reason: str | None = None,
                 exit_code: int | None = None, log_tail: str = '') -> Diagnosis:
    """NEB 作业专用分类:结构性检查优先,再沿用单作业签名裁定收敛/续算。

    NEB 主输出在各 image 子目录(根无 OUTCAR),收敛标志在 stdout 的 'reached required
    accuracy'(IBRION=1 路径:各 image 力收敛)。优先级:
      ① IMAGES 与实际 image 子目录数不符 → 输入错误(NEB_IMAGES_MISMATCH,交人工);
      ② 某 image 无有效输出 → NEB_IMAGE_MISSING(点名 image,交人工);
      ③ 某 image SCF 崩/震荡 → NEB_IMAGE_SCF(点名 image,交人工);
      ④ 收敛串在场 → CONVERGED;
      ⑤ 调度器具体原因(WALLTIME 等)> stdout 硬崩签名 > VASP 已知错误 > 未收敛(可续算)。

    Args:
        images_expected: INCAR 的 IMAGES(中间 image 数);None 表示未知(跳过①)。
        images_found:    实际中间 image 子目录数;None 表示未探测到(跳过①)。
        image_status:    [{'index':int,'empty':bool,'scf_fail':bool,'energy':float|None}, ...]。
        converged:       stdout 是否出现收敛标志(各 image 力收敛)。
    """
    status = list(image_status or [])

    # ① IMAGES 配置错误(结构性,盲目续算无益)
    if (images_expected is not None and images_found is not None
            and int(images_expected) != int(images_found)):
        return Diagnosis(
            NEB_IMAGES_MISMATCH, FAILURE_TO_STATE[NEB_IMAGES_MISMATCH], False,
            f'INCAR 的 IMAGES={images_expected} 与实际 image 子目录数 {images_found} 不符,'
            f'输入配置错误——请核对 IMAGES 与 01..N 目录数量后重建')

    # ② 某 image 无有效输出(缺失/启动即死)
    missing = [s['index'] for s in status if s.get('empty')]
    if missing and not converged:
        names = ', '.join(f'{int(i):02d}' for i in missing)
        return Diagnosis(
            NEB_IMAGE_MISSING, FAILURE_TO_STATE[NEB_IMAGE_MISSING], False,
            f'image {names} 无有效输出(OSZICAR/OUTCAR 缺失或空)——疑启动即死/缺输入,需人工')

    # ③ 某 image SCF 崩/震荡(点名 image)
    crashed = [s['index'] for s in status if s.get('scf_fail')]
    if crashed and not converged:
        names = ', '.join(f'{int(i):02d}' for i in crashed)
        return Diagnosis(
            NEB_IMAGE_SCF, FAILURE_TO_STATE[NEB_IMAGE_SCF], False,
            f'image {names} SCF 崩/震荡——同 INCAR 续算必复现,建议人工调 ALGO/AMIX 或查该 image 结构')

    # ④ 收敛(各 image 力收敛)
    if converged:
        return Diagnosis(CONVERGED, 'DONE', False,
                         'NEB 各 image 力收敛(stdout 出现 reached required accuracy)')

    # ⑤ 未收敛:调度器原因 > stdout 硬崩签名 > VASP 已知错误 > 未收敛(可续算)
    rcls = _reason_to_class(scheduler_reason)
    if rcls is not None:
        return Diagnosis(rcls, FAILURE_TO_STATE[rcls], rcls in RESTARTABLE,
                         f'调度器报 {scheduler_reason};NEB 有部分输出但未收敛')
    sig = scan_log(log_tail)
    if sig is not None:
        return Diagnosis(sig, FAILURE_TO_STATE[sig], sig in RESTARTABLE,
                         f'NEB stdout 命中 {sig} 签名')
    if exit_code == _OOM_EXIT:
        return Diagnosis(WALLTIME, FAILURE_TO_STATE[WALLTIME], True,
                         f'退出码 {exit_code}(SIGKILL)但无 OOM 证据,且有部分输出——疑超墙钟,按可续算处理')
    ve = scan_vasp_error(log_tail)
    if ve is not None:
        label, hint = ve
        return Diagnosis(label, 'NEEDS_HUMAN', False, f'NEB 命中 VASP 已知错误 {label}:{hint}')
    return Diagnosis(NONCONVERGED, 'UNCONVERGED', True,
                     'NEB 未见收敛标志(reached required accuracy)——力未收敛/墙钟,可从各 image CONTCAR 续算')


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
