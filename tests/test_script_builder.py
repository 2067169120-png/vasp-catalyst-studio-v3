"""提交脚本双轨测试:自动生成 + 用户模板透传(占位符只替换已知键)。"""
import pytest

from vcstudio.cluster.schedulers import JobScriptSpec, PBSDialect
from vcstudio.cluster.script_builder import (
    sanitize_job_name, render_auto, render_template, build_script, find_placeholders,
)

# 旧版 1w 真实模板的骨架(含 shell 变量 ${PBS_NODEFILE},绝不能被误替换)
REAL_TEMPLATE = """#!/bin/bash
#PBS -N {short}
#PBS -l nodes=1:ppn={cores}
#PBS -j oe
#PBS -q {queue}
#PBS -l walltime=24:00:00
source /home/guq/soft/intel/mkl/bin/mklvars.sh intel64
cd /home/Maple123/calculations/{dir}
mpirun -np {cores} -machinefile $PBS_NODEFILE /home/guq/soft/vasp.5.4.4/bin/vasp_std > log 2>&1
echo "EXIT: $?"
"""


def _spec(**kw):
    base = dict(job_name='ads_site_h1', remote_dir='/work/u/ads_site_h1', queue='batch',
                nodes=1, ppn=12, walltime='24:00:00', env_lines=[], vasp_cmd='mpirun vasp_std')
    base.update(kw)
    return JobScriptSpec(**base)


def test_sanitize_job_name():
    assert sanitize_job_name('ads site@h1!') == 'ads_site_h1_'[:15]
    assert sanitize_job_name('123abc').startswith('j')      # PBS 名不能数字开头
    assert sanitize_job_name('') == 'vcs_job'
    assert len(sanitize_job_name('a' * 40)) == 15           # Torque 15 字符上限


def test_render_auto_pbs_full_shape():
    text = render_auto(PBSDialect(), _spec(env_lines=['module load intel']))
    assert text.startswith('#!/bin/bash')
    assert '#PBS -N ads_site_h1' in text
    assert 'module load intel' in text
    assert 'cd /work/u/ads_site_h1' in text
    assert text.rstrip().endswith('echo "EXIT: $?"')


def test_template_replaces_known_keeps_shell_vars():
    out = render_template(REAL_TEMPLATE, _spec())
    assert '#PBS -N ads_site_h1' in out                    # {short} → job_name
    assert 'ppn=12' in out and '-np 12' in out             # {cores} → ppn
    assert '#PBS -q batch' in out                          # {queue}
    assert 'calculations/ads_site_h1' in out               # {dir} → 目录名
    assert '$PBS_NODEFILE' in out                          # shell 变量原样保留
    assert '{' not in out.replace('${', '')                # 已知占位符无残留


def test_template_missing_value_raises_chinese():
    with pytest.raises(ValueError, match='占位符缺少取值'):
        render_template(REAL_TEMPLATE, _spec(queue=''))
    with pytest.raises(ValueError, match='cores'):
        render_template('x {cores} y', _spec(ppn=0))


def test_template_unknown_braces_untouched():
    out = render_template('a {job_name} b {UNKNOWN_THING} c', _spec())
    assert '{UNKNOWN_THING}' in out and 'ads_site_h1' in out
    assert find_placeholders('{job_name} {UNKNOWN}') == ['job_name']


def test_build_script_dispatch_and_auto_missing():
    with pytest.raises(ValueError, match='资源参数'):
        build_script('auto', PBSDialect(), _spec(queue='', vasp_cmd=''))
    with pytest.raises(ValueError, match='模板'):
        build_script('template', PBSDialect(), _spec(), template_text=None)
    with pytest.raises(ValueError, match='未知脚本模式'):
        build_script('magic', PBSDialect(), _spec())
    assert '#PBS' in build_script('auto', PBSDialect(), _spec())
