"""失败分类器测试:用真实 VASP 日志/退出码签名做 fixture,离线验证分类学与状态映射。"""
from vcstudio.cluster import diagnose as dg
from vcstudio.cluster.diagnose import classify


# ── 收敛 + 物理合理性闸 ──
def test_converged_sane_energy_is_done():
    d = classify(converged=True, clean_exit=True, energy=-435.6,
                 outcar_size=120000, oszicar_size=4000)
    assert d.failure_class == dg.CONVERGED and d.state == 'DONE' and not d.restartable


def test_converged_but_energy_missing_is_bad_energy():
    d = classify(converged=True, clean_exit=True, energy=None, outcar_size=120000)
    assert d.failure_class == dg.BAD_ENERGY and d.state == 'NEEDS_HUMAN'


def test_converged_but_positive_energy_is_bad_energy():
    d = classify(converged=True, clean_exit=True, energy=3.2, outcar_size=120000)
    assert d.failure_class == dg.BAD_ENERGY and d.state == 'NEEDS_HUMAN'


def test_converged_but_absurd_magnitude_is_bad_energy():
    d = classify(converged=True, clean_exit=True, energy=-50000.0, outcar_size=120000)
    assert d.failure_class == dg.BAD_ENERGY


def test_energy_implausible_bounds():
    assert dg.energy_implausible(None)
    assert dg.energy_implausible(5.0)          # 正
    assert dg.energy_implausible(-20000.0)     # 爆掉
    assert dg.energy_implausible('nan text')   # 非数
    assert not dg.energy_implausible(-435.6)   # 正常


# ── 零输出:启动即死 / 静默退出 / 调度器杀于产出前 ──
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
    d = classify(converged=True, clean_exit=True, energy=0.0, outcar_size=90000)
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
    d = classify(converged=True, clean_exit=True, energy=-100.0,
                 log_tail='WARNING: Sub-Space-Matrix is not hermitian in DAV\n')
    assert d.failure_class == dg.CONVERGED and d.state == 'DONE'


# ── 有输出:裸退出码 137(无 OOM 日志/调度器原因)→ 疑墙钟,可续算 ──
def test_exit_137_bare_is_walltime_restartable():
    """修复:裸 137 + 有部分输出但无 OOM 证据 → 判疑墙钟(可续算),
    不再一律 OOM→FAILED 困死本可续算的墙钟作业(缺口分析 P0)。"""
    d = classify(outcar_size=90000, exit_code=137)
    assert d.failure_class == dg.WALLTIME and d.state == 'UNCONVERGED' and d.restartable


def test_exit_137_with_oom_log_is_still_oom():
    """有 OOM 日志证据的 137 仍判 OOM→FAILED(scan_log 先命中,不被降级)。"""
    d = classify(outcar_size=90000, exit_code=137,
                 log_tail='slurmstepd: error: Detected 1 oom-kill event(s)\n')
    assert d.failure_class == dg.OOM and d.state == 'FAILED'


def test_exit_137_with_scheduler_oom_reason_is_oom():
    """调度器明确报 OOM 原因的 137 仍判 OOM→FAILED(原因优先)。"""
    d = classify(outcar_size=90000, exit_code=137, scheduler_reason=dg.R_OOM)
    assert d.failure_class == dg.OOM and d.state == 'FAILED'


# ── 磁盘满 / IO 错误 → 不可续算,交人工 ──
def test_disk_full_is_needs_human_not_restartable():
    """磁盘满/配额满:盲目续算必再撞满 → DISK_FULL/NEEDS_HUMAN,不可续算。"""
    for msg in ('OUTCAR write: No space left on device\n',
                'Disk quota exceeded\n',
                'Input/output error while writing WAVECAR\n'):
        d = classify(outcar_size=90000, oszicar_size=3000, converged=False, log_tail=msg)
        assert d.failure_class == dg.DISK_FULL and d.state == 'NEEDS_HUMAN' \
            and not d.restartable, msg


def test_disk_full_not_flagged_when_converged():
    """已收敛+能量合理:计算其实已完成,末尾写盘噪声不改判(收敛短路在前)。"""
    d = classify(converged=True, clean_exit=True, energy=-123.4, outcar_size=90000,
                 log_tail='No space left on device\n')
    assert d.failure_class == dg.CONVERGED and d.state == 'DONE'


# ── ZBRENT 扩展签名(bracketing interval / can not reach accuracy)──
def test_zbrent_extended_signatures_restartable():
    for msg in ('ZBRENT: bracketing interval incorrect\n',
                'ZBRENT: can not reach accuracy\n'):
        d = classify(outcar_size=90000, log_tail=msg)
        assert d.failure_class == dg.ZBRENT and d.restartable, msg


# ── 有输出、无硬崩、无收敛串 → 未收敛(可续算) ──
def test_has_output_not_converged_is_nonconverged_restartable():
    d = classify(outcar_size=90000, oszicar_size=3000, converged=False, log_tail='EXIT: 0\n')
    assert d.failure_class == dg.NONCONVERGED and d.state == 'UNCONVERGED' and d.restartable


# ── SCF 震荡(原版 healer/lis_sac_status 生产口径移植) ──
def _oszicar_block(n_iters, last_de):
    # Real OSZICAR order: electronic DAV/RMM iterations first, then the ionic
    # ``F= ... E0= ...`` summary.  Keeping the fixture realistic protects the
    # VASP5 NELM false-positive guard from silently parsing an empty block.
    lines = []
    for i in range(1, n_iters + 1):
        de = last_de if i == n_iters else '-0.5E+00'
        lines.append(f'DAV:  {i}    -0.385031793E+03   {de}   -0.129E+02  4696   0.1E+00')
    lines.append('   1 F= -.38712683E+03 E0= -.38712683E+03  d E =-.387127E+03')
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


def test_sloshing_scans_all_blocks_worst_wins():
    """原版标定经验:扫尾部全部块取最坏块——历史块震荡、末块刚起步也要报
    (只看末块会在新离子步起步时漏判)。"""
    bad = _oszicar_block(85, '0.8E-01')
    new_step_started = bad \
        + 'DAV:   1    -0.388E+03   -0.5E+00   -0.1E+02  4696   0.1E+00\n' \
        + 'DAV:   2    -0.388E+03   -0.1E-03   -0.1E+02  4696   0.1E-02\n' \
        + '   2 F= -.38800000E+03 E0= -.38800000E+03  d E =-.87E+00\n'
    assert dg.scan_oszicar_sloshing(new_step_started) is not None
    # 原版 EDDAV 等算法前缀同样被识别
    eddav = '\n'.join(f'EDDAV:  {i}   -0.38E+03   0.5E-01   x  x  x' for i in range(1, 86))
    assert dg.scan_oszicar_sloshing(eddav) is not None


# ── 网研核对的取证升级:STOPCAR / VASP5 假阳性守卫 / 干净退出页脚 ──
def test_user_stopped_not_misjudged_nonconverged():
    """STOPCAR 叫停 ≠ 失败:标 USER_STOPPED 交人工,防盲目续算。"""
    d = classify(outcar_size=90000, oszicar_size=3000, stopped=True)
    assert d.failure_class == dg.USER_STOPPED and d.state == 'NEEDS_HUMAN' and not d.restartable


def test_vasp5_nelm_trap_converged_flag_is_false_positive():
    """VASP5 陷阱:NELM 耗尽同样打印 EDIFF-reached——收敛标志+末块打满 NELM → 不可信。"""
    tail = _oszicar_block(60, '0.5E-01')            # 60 步(默认 NELM)打满
    assert dg.nelm_saturated(tail, 60)
    d = classify(converged=True, clean_exit=True, energy=-100.0, oszicar_tail=tail, nelm=60,
                 outcar_size=90000)
    assert d.failure_class == dg.SCF_SLOSHING and d.state == 'NEEDS_HUMAN'
    # 真收敛(末块 12 步远未打满)不受影响
    ok = classify(converged=True, clean_exit=True, energy=-100.0,
                  oszicar_tail=_oszicar_block(12, '0.1E-04'),
                  nelm=60, outcar_size=90000)
    assert ok.failure_class == dg.CONVERGED


def test_convergence_marker_without_clean_footer_needs_human_not_restartable():
    """旧 OUTCAR 收敛串或截断输出不得 DONE，也不得被自动续算。"""
    d = classify(converged=True, clean_exit=False, energy=-100.0,
                 outcar_size=90000, oszicar_size=3000)
    assert d.failure_class == dg.UNKNOWN and d.state == 'NEEDS_HUMAN'
    assert d.restartable is False and 'timing 页脚' in d.evidence


def test_convergence_marker_with_nonzero_exit_is_never_done():
    d = classify(converged=True, clean_exit=True, exit_code=1, energy=-100.0,
                 outcar_size=90000, oszicar_size=3000)
    assert d.failure_class == dg.UNKNOWN and d.state == 'NEEDS_HUMAN'
    assert d.restartable is False and '非零' in d.evidence


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


# ── 运行中活体健康(原版 lis_sac_status 生产经验移植) ──
def test_next_poll_count():
    assert dg.next_poll_count(0, True) == 1
    assert dg.next_poll_count(2, True) == 3
    assert dg.next_poll_count(5, False) == 0          # 一旦干净立即清零


def test_live_health_sloshing_needs_two_polls():
    bad = _oszicar_block(85, '0.8E-01')
    r1 = dg.live_health(bad, ionic_steps=3, prev=None)
    assert r1['sloshing_polls'] == 1 and r1['warning'] == ''      # 第 1 轮:只计数不告警
    r2 = dg.live_health(bad, ionic_steps=3, prev=r1)
    assert r2['sloshing_polls'] == 2 and 'SCF 震荡' in r2['warning']
    assert '不自动 qdel' in r2['warning']                          # 告警不动手
    ok = dg.live_health(_oszicar_block(12, '0.1E-04'), ionic_steps=4, prev=r2)
    assert ok['sloshing_polls'] == 0 and ok['warning'] == ''       # 恢复健康即清零


def test_live_health_zero_step_stall_needs_three_polls():
    prev = None
    for i in range(1, 4):
        prev = dg.live_health('', ionic_steps=0, prev=prev)
        assert prev['zero_step_polls'] == i
    assert '首步假死' in prev['warning']                            # 第 3 轮才告警
    moved = dg.live_health('', ionic_steps=1, prev=prev)
    assert moved['zero_step_polls'] == 0 and moved['warning'] == ''
