"""调度器方言层测试:指令/作业号解析/状态解析/命令字符串(纯函数,无网络)。"""
import pytest

from vcstudio.cluster.schedulers import (
    JobScriptSpec, PBSDialect, SlurmDialect, get_dialect, QUEUED, RUNNING,
)


def _spec(**kw):
    base = dict(job_name='zn_li2s', remote_dir='/work/u/zn_li2s', queue='batch',
                nodes=1, ppn=12, walltime='24:00:00',
                env_lines=['source /opt/intel/mkl.sh'], vasp_cmd='mpirun -np 12 vasp_std > log 2>&1')
    base.update(kw)
    return JobScriptSpec(**base)


# ── PBS(1w/Torque 实测方言) ──
def test_pbs_directives_and_run_block():
    d = PBSDialect()
    lines = d.directives(_spec())
    assert '#PBS -N zn_li2s' in lines
    assert '#PBS -l nodes=1:ppn=12' in lines
    assert '#PBS -q batch' in lines
    assert '#PBS -l walltime=24:00:00' in lines
    rb = d.run_block(_spec())
    assert rb[0] == 'cd /work/u/zn_li2s' and 'EXIT' in rb[-1]


def test_pbs_parse_job_id_variants():
    d = PBSDialect()
    assert d.parse_job_id('8812345.cluster.hpc\n') == '8812345'   # 1w 实测格式
    assert d.parse_job_id('123\n') == '123'
    assert d.parse_job_id('qsub: submit error\n') == ''


def test_pbs_parse_status_10col_and_basic():
    d = PBSDialect()
    ten_col = (
        "cluster.hpc:\n"
        "Job ID                  Username Queue  Jobname     SessID NDS TSK Memory Time     S Time\n"
        "----------------------- -------- ------ ----------- ------ --- --- ------ -------- - ----\n"
        "8812345.cluster.hpc     sk2067   batch  zn_li2s       1234   1  12     -- 24:00:00 R 01:02\n"
        "8812346.cluster.hpc     sk2067   fat    ads_h1          --   1  28     -- 24:00:00 Q    --\n"
        "8812347.cluster.hpc     sk2067   fat    ads_h2          --   1  28     -- 24:00:00 C 09:00\n")
    st = d.parse_status(ten_col)
    assert st == {'8812345': RUNNING, '8812346': QUEUED}          # C(完成)不在字典 → GONE
    basic = ("Job id            Name   User    Time Use S Queue\n"
             "----------------- ------ ------- -------- - -----\n"
             "123.cluster       job1   sk2067  00:01:02 E batch\n")
    assert d.parse_status(basic) == {'123': RUNNING}              # E=Exiting 视为运行收尾


def test_pbs_cmds_use_scheduler_bin():
    d = PBSDialect()
    assert d.submit_cmd('/w/j/vcs_job.sh', '/opt/torque-6.1.2/bin') == \
        'cd /w/j && /opt/torque-6.1.2/bin/qsub /w/j/vcs_job.sh 2>&1'
    assert d.status_cmd('sk2067') == 'qstat -u sk2067 2>/dev/null'
    assert d.cancel_cmd('8812345', '/opt/t/bin') == '/opt/t/bin/qdel 8812345 2>&1'


# ── Slurm(八期实测方言) ──
def test_slurm_directives_run_block_and_parse():
    d = SlurmDialect()
    lines = d.directives(_spec(queue='xiaohe2', ppn=28))
    assert '#SBATCH -p xiaohe2' in lines and '#SBATCH --ntasks-per-node=28' in lines
    rb = d.run_block(_spec())
    assert any('scontrol show hostnames' in x for x in rb)        # 老 Intel MPI hostfile
    assert d.parse_job_id('Submitted batch job 456789\n') == '456789'
    assert d.parse_job_id('sbatch: error: invalid partition\n') == ''
    st = d.parse_status('456789|R\n456790|PD\n456791|CD\n')
    assert st == {'456789': RUNNING, '456790': QUEUED}
    assert 'squeue -u u -o "%i|%t" --noheader' in d.status_cmd('u')
    assert d.cancel_cmd('456789') == 'scancel 456789 2>&1'


def test_cmds_quote_spaced_paths():
    """化学家目录带空格是常态:submit_cmd/run_block 里的路径必须加引号,否则一跑就崩。"""
    pbs = PBSDialect()
    assert pbs.submit_cmd('/w/my jobs/vcs_job.sh', '/opt/t/bin') == \
        "cd '/w/my jobs' && /opt/t/bin/qsub '/w/my jobs/vcs_job.sh' 2>&1"
    slurm = SlurmDialect()
    assert slurm.submit_cmd('/w/my jobs/vcs_job.sh') == \
        "cd '/w/my jobs' && sbatch '/w/my jobs/vcs_job.sh' 2>&1"
    assert pbs.run_block(_spec(remote_dir='/w/my jobs/zn'))[0] == "cd '/w/my jobs/zn'"
    assert slurm.run_block(_spec(remote_dir='/w/my jobs/zn'))[0] == "cd '/w/my jobs/zn'"
    # scheduler_bin 带空格同病:qsub/qstat/qdel 的全路径也要引号
    assert pbs.submit_cmd('/w/j/vcs_job.sh', '/opt/my torque/bin') == \
        "cd /w/j && '/opt/my torque/bin/qsub' /w/j/vcs_job.sh 2>&1"
    assert pbs.status_cmd('u', '/opt/my torque/bin') == \
        "'/opt/my torque/bin/qstat' -u u 2>/dev/null"
    assert pbs.cancel_cmd('8812345', '/opt/my torque/bin') == \
        "'/opt/my torque/bin/qdel' 8812345 2>&1"


def test_get_dialect_dispatch_and_unsupported():
    assert get_dialect('PBS').name == 'PBS'
    assert get_dialect('Slurm').name == 'Slurm'
    with pytest.raises(ValueError, match='暂不支持'):
        get_dialect('LSF')
