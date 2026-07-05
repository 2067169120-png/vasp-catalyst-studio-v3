"""失败分类器测试:用真实 VASP 日志/退出码签名做 fixture,离线验证分类学与状态映射。"""
from vcstudio.cluster import diagnose as dg
from vcstudio.cluster.diagnose import classify, Diagnosis


# ── 收敛 + 物理合理性闸 ──
def test_converged_sane_energy_is_done():
    d = classify(converged=True, energy=-435.6, outcar_size=120000, oszicar_size=4000)
    assert d.failure_class == dg.CONVERGED and d.state == 'DONE' and not d.restartable


def test_converged_but_energy_missing_is_bad_energy():
    d = classify(converged=True, energy=None, outcar_size=120000)
    assert d.failure_class == dg.BAD_ENERGY and d.state == 'NEEDS_HUMAN'


def test_converged_but_positive_energy_is_bad_energy():
    d = classify(converged=True, energy=3.2, outcar_size=120000)
    assert d.failure_class == dg.BAD_ENERGY and d.state == 'NEEDS_HUMAN'


def test_converged_but_absurd_magnitude_is_bad_energy():
    d = classify(converged=True, energy=-50000.0, outcar_size=120000)
    assert d.failure_class == dg.BAD_ENERGY


def test_energy_implausible_bounds():
    assert dg.energy_implausible(None)
    assert dg.energy_implausible(5.0)          # 正
    assert dg.energy_implausible(-20000.0)     # 爆掉
    assert dg.energy_implausible('nan text')   # 非数
    assert not dg.energy_implausible(-435.6)   # 正常


# ── 零输出:启动即死 / 沉默退出 / 调度器杀于产出前 ──
def test_no_output_startup_death():
    d = classify(outcar_size=0, oszicar_size=0, exit_code=None)
    assert d.failure_class == dg.NO_OUTPUT and d.state == 'NEEDS_HUMAN'


def test_no_output_missing_files_none_sizes():
    d = classify(outcar_size=None, oszicar_size=None)
    assert d.failure_class == dg.NO_OUTPUT


def test_silent_exit_zero_code_empty_outcar():
    d = classify(outcar_size=0, oszicar_size=0, exit_code=0)
    assert d.failure_class == dg.SILENT_EXIT and d.state == 'NEEDS_HUMAN'


def test_no_output_but_scheduler_oom_wins():
    d = classify(outcar_size=0, oszicar_size=0, scheduler_reason=dg.R_OOM)
    assert d.failure_class == dg.OOM and d.state == 'FAILED'


# ── 有输出:调度器具体终态 ──
def test_scheduler_timeout_is_walltime_restartable():
    d = classify(outcar_size=90000, oszicar_size=3000, scheduler_reason=dg.R_TIMEOUT)
    assert d.failure_class == dg.WALLTIME and d.state == 'UNCONVERGED' and d.restartable


def test_scheduler_cancelled_is_failed():
    d = classify(outcar_size=90000, scheduler_reason=dg.R_CANCELLED)
    assert d.failure_class == dg.CANCELLED and d.state == 'FAILED' and not d.restartable


def test_scheduler_node_fail_is_failed():
    d = classify(outcar_size=90000, scheduler_reason=dg.R_NODE_FAIL)
    assert d.failure_class == dg.NODE_FAIL and d.state == 'FAILED'


def test_scheduler_generic_failed_no_signal_needs_human():
    d = classify(outcar_size=90000, oszicar_size=3000, scheduler_reason=dg.R_FAILED)
    assert d.failure_class == dg.UNKNOWN and d.state == 'NEEDS_HUMAN'


# ── 有输出:日志硬崩签名 ──
def test_log_zbrent_is_restartable():
    log = 'VERY BAD NEWS! internal error in subroutine ...\n ZBRENT: fatal error in bracketing\n'
    d = classify(outcar_size=90000, log_tail=log)
    assert d.failure_class == dg.ZBRENT and d.state == 'UNCONVERGED' and d.restartable


def test_log_segfault_text_is_sigsegv_failed():
    log = 'forrtl: severe (174): SIGSEGV, segmentation fault occurred\n'
    d = classify(outcar_size=90000, log_tail=log)
    assert d.failure_class == dg.SIGSEGV and d.state == 'FAILED'


def test_log_intel_mpi_all_ones_is_sigsegv():
    log = 'some earlier line\n1\n1\n1\n'
    assert dg.scan_log(log) == dg.SIGSEGV
    d = classify(outcar_size=90000, log_tail=log)
    assert d.failure_class == dg.SIGSEGV


def test_scan_log_none_when_clean():
    assert dg.scan_log('running fine\nEXIT: 0\n') is None
    assert dg.scan_log('') is None


# ── 有输出:退出码 137 = OOM ──
def test_exit_137_is_oom():
    d = classify(outcar_size=90000, exit_code=137)
    assert d.failure_class == dg.OOM and d.state == 'FAILED'


# ── 有输出、无硬崩、无收敛串 → 未收敛(可续算) ──
def test_has_output_not_converged_is_nonconverged_restartable():
    d = classify(outcar_size=90000, oszicar_size=3000, converged=False, log_tail='EXIT: 0\n')
    assert d.failure_class == dg.NONCONVERGED and d.state == 'UNCONVERGED' and d.restartable


# ── 状态映射与可续算集自洽 ──
def test_state_map_and_restartable_consistency():
    for cls, state in dg.FAILURE_TO_STATE.items():
        assert state in ('DONE', 'UNCONVERGED', 'FAILED', 'NEEDS_HUMAN')
    # 可续算的一定映到 UNCONVERGED(续算 = 从 CONTCAR 接着跑)
    for cls in dg.RESTARTABLE:
        assert dg.FAILURE_TO_STATE[cls] == 'UNCONVERGED'


# ── valid_poscar(CONTCAR 续算前校验) ──
_GOOD = ('C slab\n1.0\n10 0 0\n0 10 0\n0 0 12\nC O\n2 1\nCartesian\n'
         '0 0 0\n1 0 0\n0 1 0\n')
_GOOD_SELDYN = ('C slab\n1.0\n10 0 0\n0 10 0\n0 0 12\nC O\n2 1\nSelective dynamics\n'
                'Cartesian\n0 0 0 T T T\n1 0 0 T T T\n0 1 0 F F F\n')


def test_valid_poscar_good():
    assert dg.valid_poscar(_GOOD)
    assert dg.valid_poscar(_GOOD_SELDYN)          # 容忍 Selective dynamics 行


def test_valid_poscar_rejects_vasp4_no_species():
    bad = 'title\n1.0\n10 0 0\n0 10 0\n0 0 12\n2 1\nCartesian\n0 0 0\n1 0 0\n0 1 0\n'
    assert not dg.valid_poscar(bad)               # 第6行是数字 → 无元素符号行


def test_valid_poscar_rejects_truncated_coords():
    truncated = 'C slab\n1.0\n10 0 0\n0 10 0\n0 0 12\nC O\n2 1\nCartesian\n0 0 0\n'
    assert not dg.valid_poscar(truncated)         # 声称 3 原子只给 1 行坐标


def test_valid_poscar_rejects_empty_and_short():
    assert not dg.valid_poscar('')
    assert not dg.valid_poscar('a\nb\nc\n')
