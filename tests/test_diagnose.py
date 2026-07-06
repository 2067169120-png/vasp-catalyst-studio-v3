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


# ── 已知 VASP 内部错误签名(Custodian 交叉核对) ──
def test_scan_vasp_error_signatures():
    assert dg.scan_vasp_error(' TOO FEW BANDS\n')[0] == 'TOO_FEW_BANDS'
    assert dg.scan_vasp_error('Tetrahedron method fails (number of k-points < 4)')[0] == 'TETRAHEDRON'
    assert dg.scan_vasp_error('LAPACK: Routine ZPOTRF failed')[0] == 'ZPOTRF'
    assert dg.scan_vasp_error('BRMIX: very serious problems')[0] == 'BRMIX'
    assert dg.scan_vasp_error('Error EDDDAV: Call to ZHEGV failed')[0] == 'EDDDAV'
    assert dg.scan_vasp_error('ERROR RSPHER')[0] == 'LREAL'
    assert dg.scan_vasp_error('internal error in subroutine PRICEL')[0] == 'SYMMETRY'
    assert dg.scan_vasp_error('VERY BAD NEWS! internal error in subroutine XXX')[0] == 'VASP_INTERNAL'


def test_scan_vasp_error_round2_signatures():
    """review-round2 收尾:补齐撞额度时漏掉的 Custodian 签名。"""
    assert dg.scan_vasp_error('WARNING in EDDRMM: call to ZHEGV failed')[0] == 'EDDRMM'
    assert dg.scan_vasp_error('EDWAV: internal error, the gradient is not orthogonal')[0] == 'EDWAV'
    assert dg.scan_vasp_error('while reading WAVECAR, plane wave coefficients changed')[0] == 'WAVECAR_CORRUPT'
    assert dg.scan_vasp_error('Error reading item ''IMAGES'' from file INCAR.')[0] == 'INCAR_READ'


def test_scan_log_oom_text_evidence():
    """Slurm oom-kill / MPI SIGKILL 收尸行 = OOM 直接证据 → FAILED。"""
    assert dg.scan_log('slurmstepd: error: Detected 1 oom-kill event(s)\n') == dg.OOM
    assert dg.scan_log('APPLICATION TERMINATED WITH THE EXIT STRING: Killed (signal 9)\n') == dg.OOM
    d = classify(outcar_size=90000, log_tail='slurmstepd: error: Detected 1 oom-kill event(s)\n')
    assert d.failure_class == dg.OOM and d.state == 'FAILED'


def test_energy_zero_is_implausible():
    """E0 恰为 0.0 只能是解析垃圾(真实束缚体系总能恒负)→ 不可信。"""
    assert dg.energy_implausible(0.0)
    d = classify(converged=True, energy=0.0, outcar_size=90000)
    assert d.failure_class == dg.BAD_ENERGY


def test_scan_vasp_error_no_false_positive():
    assert dg.scan_vasp_error('running fine, ionic step 5 converged\n') is None
    assert dg.scan_vasp_error('BRMIX') is None                 # 裸 BRMIX(正常混合类型回显)不误报
    assert dg.scan_vasp_error('') is None


def test_classify_known_vasp_error_is_needs_human():
    d = classify(outcar_size=90000, oszicar_size=3000, converged=False,
                 log_tail='some output\n Tetrahedron method fails\n')
    assert d.failure_class == 'TETRAHEDRON' and d.state == 'NEEDS_HUMAN' and not d.restartable


def test_classify_zbrent_still_restartable_over_error_table():
    """ZBRENT 仍走 scan_log 可续算路径,不被通用错误表吞成 NEEDS_HUMAN。"""
    d = classify(outcar_size=90000, log_tail='ZBRENT: fatal error in bracketing\n')
    assert d.failure_class == dg.ZBRENT and d.restartable


def test_converged_ignores_benign_error_text_in_log():
    """收敛成功的作业即便日志里有可自恢复告警,也不被误判(收敛短路在前)。"""
    d = classify(converged=True, energy=-100.0,
                 log_tail='WARNING: Sub-Space-Matrix is not hermitian in DAV\n')
    assert d.failure_class == dg.CONVERGED and d.state == 'DONE'


# ── 有输出:退出码 137 = OOM ──
def test_exit_137_is_oom():
    d = classify(outcar_size=90000, exit_code=137)
    assert d.failure_class == dg.OOM and d.state == 'FAILED'


# ── 有输出、无硬崩、无收敛串 → 未收敛(可续算) ──
def test_has_output_not_converged_is_nonconverged_restartable():
    d = classify(outcar_size=90000, oszicar_size=3000, converged=False, log_tail='EXIT: 0\n')
    assert d.failure_class == dg.NONCONVERGED and d.state == 'UNCONVERGED' and d.restartable


# ── SCF 震荡(原版 healer/lis_sac_status 生产口径移植) ──
def _oszicar_block(n_iters, last_de):
    lines = ['   1 F= -.38712683E+03 E0= -.38712683E+03  d E =-.387127E+03']
    for i in range(1, n_iters + 1):
        de = last_de if i == n_iters else '-0.5E+00'
        lines.append(f'DAV:  {i}    -0.385031793E+03   {de}   -0.129E+02  4696   0.1E+00')
    return '\n'.join(lines) + '\n'


def test_scan_oszicar_sloshing_hit():
    tail = _oszicar_block(85, '0.8E-01')          # 85 步 + |dE|=0.08 > 1e-2 → 震荡
    ev = dg.scan_oszicar_sloshing(tail)
    assert ev is not None and '85' in ev
    d = classify(outcar_size=90000, oszicar_size=9000, oszicar_tail=tail)
    assert d.failure_class == dg.SCF_SLOSHING and d.state == 'NEEDS_HUMAN' and not d.restartable


def test_scan_oszicar_slow_but_converging_not_sloshing():
    assert dg.scan_oszicar_sloshing(_oszicar_block(85, '0.3E-02')) is None   # dE 小=慢收敛
    assert dg.scan_oszicar_sloshing(_oszicar_block(40, '0.8E-01')) is None   # 步数少
    assert dg.scan_oszicar_sloshing('') is None
    # 无震荡 → 仍走可续算的 NONCONVERGED
    d = classify(outcar_size=90000, oszicar_tail=_oszicar_block(40, '0.8E-01'))
    assert d.failure_class == dg.NONCONVERGED and d.restartable


def test_sloshing_only_checks_last_ionic_block():
    """前面离子步曾震荡但最后一步正常 → 不报(只看最后一块)。"""
    bad = _oszicar_block(85, '0.8E-01')
    good_last = bad + '   2 F= -.38800000E+03 E0= -.38800000E+03  d E =-.87E+00\n' \
        + 'DAV:   1    -0.388E+03   -0.5E+00   -0.1E+02  4696   0.1E+00\n' \
        + 'DAV:   2    -0.388E+03   -0.1E-03   -0.1E+02  4696   0.1E-02\n'
    assert dg.scan_oszicar_sloshing(good_last) is None


# ── 网研核对的取证升级:STOPCAR / VASP5 假阳性守卫 / 干净退出页脚 ──
def test_user_stopped_not_misjudged_nonconverged():
    """STOPCAR 叫停 ≠ 失败:标 USER_STOPPED 交人工,防盲目续算。"""
    d = classify(outcar_size=90000, oszicar_size=3000, stopped=True)
    assert d.failure_class == dg.USER_STOPPED and d.state == 'NEEDS_HUMAN' and not d.restartable


def test_vasp5_nelm_trap_converged_flag_is_false_positive():
    """VASP5 陷阱:NELM 耗尽同样打印 EDIFF-reached——收敛标志+末块打满 NELM → 不可信。"""
    tail = _oszicar_block(60, '0.5E-01')            # 60 步(默认 NELM)打满
    assert dg.nelm_saturated(tail, 60)
    d = classify(converged=True, energy=-100.0, oszicar_tail=tail, nelm=60,
                 outcar_size=90000)
    assert d.failure_class == dg.SCF_SLOSHING and d.state == 'NEEDS_HUMAN'
    # 真收敛(末块 12 步远未打满)不受影响
    ok = classify(converged=True, energy=-100.0, oszicar_tail=_oszicar_block(12, '0.1E-04'),
                  nelm=60, outcar_size=90000)
    assert ok.failure_class == dg.CONVERGED


def test_clean_exit_refines_nonconverged_evidence():
    d1 = classify(outcar_size=90000, clean_exit=True)
    assert d1.failure_class == dg.NONCONVERGED and '正常收尾' in d1.evidence
    d2 = classify(outcar_size=90000, clean_exit=False)
    assert d2.failure_class == dg.NONCONVERGED and '中途被杀' in d2.evidence
    assert d1.restartable and d2.restartable          # 两者都可续算


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
