"""调度器方言层(纯函数,不碰网络):指令生成 / 作业号解析 / 状态解析 / 取消命令。

移植自旧版 layer1_hpc/scheduler_pbs.py + scheduler_slurm.py(1w=Torque、八期=Slurm 实测),
去掉网络执行部分——本模块只生成命令字符串与解析输出文本,执行交给 submitter(可注入假件)。

统一状态(调度器层,与 job.yaml 状态机衔接):
  QUEUED  排队/挂起      RUNNING 运行/收尾中
  GONE    调度器里已不存在(完成或被清理;收敛与否由 submitter 查 OUTCAR 判定)
中文注释允许,英文标识符。
"""
from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass, field

QUEUED, RUNNING, GONE = 'QUEUED', 'RUNNING', 'GONE'


@dataclass
class JobScriptSpec:
    """调度器无关的作业描述(自动轨脚本生成用)。"""
    job_name: str
    remote_dir: str
    queue: str = ''
    nodes: int = 1
    ppn: int = 0
    walltime: str = '24:00:00'
    env_lines: list = field(default_factory=list)   # module load / source …
    vasp_cmd: str = ''                              # 完整执行行(mpirun/srun …)


class SchedulerDialect:
    """方言基类。子类只提供字符串进出,无任何 IO。"""
    name = '?'

    def directives(self, spec: JobScriptSpec) -> list:
        raise NotImplementedError

    def run_block(self, spec: JobScriptSpec) -> list:
        return [f'cd {shlex.quote(spec.remote_dir)}', spec.vasp_cmd, 'echo "EXIT: $?"']

    def submit_cmd(self, script_path: str, bin_path: str = '') -> str:
        raise NotImplementedError

    def parse_job_id(self, raw: str) -> str:
        raise NotImplementedError

    def status_cmd(self, user: str, bin_path: str = '') -> str:
        raise NotImplementedError

    def parse_status(self, raw: str) -> dict:
        """原始查询输出 → {job_id: QUEUED|RUNNING};不在字典里的作业视为 GONE。"""
        raise NotImplementedError

    def parse_terminal(self, raw: str) -> dict:
        """原始查询输出 → {job_id: 终态原因 token}(供 diagnose.classify 消歧)。

        Slurm 的 squeue 会短暂显示 TO/CA/NF/OOM/F 等终态——能看到就别丢
        (缺口分析 P0:超墙钟 SIGKILL 137 若无调度器原因会被误判 OOM→FAILED,
        困死本可续算的作业)。PBS 的 C 态天然歧义(成功/失败同码)→ 保守返回空,
        由 OUTCAR/日志取证裁决(取证才是权威)。看不到终态窗口时同样回落取证。
        """
        return {}

    def cancel_cmd(self, job_id: str, bin_path: str = '') -> str:
        raise NotImplementedError


def _bin(bin_path: str, exe: str) -> str:
    """scheduler_bin 非空时用全路径(1w 的 torque 不在默认 PATH,实测必须),带引号防空格。"""
    return shlex.quote(posixpath.join(bin_path, exe)) if bin_path else exe


class PBSDialect(SchedulerDialect):
    """Torque/PBS(1w 集群实测方言)。"""
    name = 'PBS'
    # PBS 单字母态 → 统一态;C/F 完成态按 GONE 处理(交收敛判定)
    _MAP = {'Q': QUEUED, 'H': QUEUED, 'W': QUEUED, 'T': QUEUED, 'S': QUEUED,
            'R': RUNNING, 'E': RUNNING,
            'C': GONE, 'F': GONE, 'X': GONE}
    _STATES = set(_MAP)

    def directives(self, spec):
        return [
            '#!/bin/bash',
            f'#PBS -N {spec.job_name}',
            f'#PBS -l nodes={spec.nodes}:ppn={spec.ppn}',
            '#PBS -j oe',
            f'#PBS -q {spec.queue}',
            f'#PBS -l walltime={spec.walltime}',
        ]

    def submit_cmd(self, script_path, bin_path=''):
        d = posixpath.dirname(script_path)
        return (f'cd {shlex.quote(d)} && '
                f'{_bin(bin_path, "qsub")} {shlex.quote(script_path)} 2>&1')

    def parse_job_id(self, raw):
        # qsub 输出如 '8812345.cluster.hpc'(1w 实测)或裸数字;取短号供 qstat 匹配
        for line in raw.splitlines():
            m = re.match(r'\s*(\d+)(?:\.\S+)?\s*$', line)
            if m:
                return m.group(1)
        return ''

    def status_cmd(self, user, bin_path=''):
        return f'{_bin(bin_path, "qstat")} -u {shlex.quote(str(user))} 2>/dev/null'

    def parse_status(self, raw):
        """兼容两种 qstat 输出:`qstat -u user`(≥10 列,S 在第 10 列)与裸 `qstat`(6 列,S 在第 5 列)。"""
        result = {}
        for line in raw.splitlines():
            parts = line.split()
            if not parts or not re.match(r'\d', parts[0]):
                continue
            jid = parts[0].split('.')[0]
            state = None
            if len(parts) >= 10 and parts[9] in self._STATES:
                state = parts[9]
            elif len(parts) >= 5 and parts[4] in self._STATES:
                state = parts[4]
            if state is not None:
                u = self._MAP[state]
                if u != GONE:
                    result[jid] = u
        return result

    def cancel_cmd(self, job_id, bin_path=''):
        return f'{_bin(bin_path, "qdel")} {shlex.quote(str(job_id))} 2>&1'


class SlurmDialect(SchedulerDialect):
    """Slurm(超算八期/七期实测方言)。"""
    name = 'Slurm'
    _MAP = {'PD': QUEUED, 'PR': QUEUED, 'S': QUEUED, 'RQ': QUEUED,
            'R': RUNNING, 'CG': RUNNING,
            'CD': GONE, 'F': GONE, 'TO': GONE, 'CA': GONE, 'NF': GONE, 'OOM': GONE}
    # 终态原因(squeue 短暂可见窗口)→ diagnose 的原因 token;CD=正常完成不发原因
    _TERMINAL = {'TO': 'TIMEOUT', 'OOM': 'OOM', 'CA': 'CANCELLED',
                 'NF': 'NODE_FAIL', 'F': 'FAILED'}

    def directives(self, spec):
        return [
            '#!/bin/bash',
            f'#SBATCH -J {spec.job_name}',
            f'#SBATCH -N {spec.nodes}',
            f'#SBATCH --ntasks-per-node={spec.ppn}',
            '#SBATCH --cpus-per-task=1',
            '#SBATCH -o slurm-%j.out',
            '#SBATCH -e slurm-%j.err',
            f'#SBATCH -p {spec.queue}',
            f'#SBATCH --time={spec.walltime}',
        ]

    def run_block(self, spec):
        # 八期实测:老 Intel MPI 需要 hostfile;vasp_cmd 未引用时该文件无害
        return [f'cd {shlex.quote(spec.remote_dir)}',
                'scontrol show hostnames $SLURM_JOB_NODELIST > hostfile.txt',
                spec.vasp_cmd, 'echo "EXIT: $?"']

    def submit_cmd(self, script_path, bin_path=''):
        d = posixpath.dirname(script_path)
        return (f'cd {shlex.quote(d)} && '
                f'{_bin(bin_path, "sbatch")} {shlex.quote(script_path)} 2>&1')

    def parse_job_id(self, raw):
        m = re.search(r'Submitted batch job (\d+)', raw)
        return m.group(1) if m else ''

    def status_cmd(self, user, bin_path=''):
        return f'{_bin(bin_path, "squeue")} -u {shlex.quote(str(user))} -o "%i|%t" --noheader 2>/dev/null'

    def parse_status(self, raw):
        result = {}
        for line in raw.strip().splitlines():
            parts = line.strip().split('|')
            if len(parts) >= 2 and parts[0].strip():
                u = self._MAP.get(parts[1].strip(), None)
                if u in (QUEUED, RUNNING):
                    result[parts[0].strip()] = u
        return result

    def parse_terminal(self, raw):
        result = {}
        for line in raw.strip().splitlines():
            parts = line.strip().split('|')
            if len(parts) >= 2 and parts[0].strip():
                reason = self._TERMINAL.get(parts[1].strip())
                if reason:
                    result[parts[0].strip()] = reason
        return result

    def cancel_cmd(self, job_id, bin_path=''):
        return f'{_bin(bin_path, "scancel")} {shlex.quote(str(job_id))} 2>&1'


_DIALECTS = {'PBS': PBSDialect, 'Slurm': SlurmDialect}


def get_dialect(scheduler: str) -> SchedulerDialect:
    """按 ClusterProfile.scheduler 取方言;暂不支持的显式报错(绝不静默)。"""
    cls = _DIALECTS.get(scheduler)
    if cls is None:
        raise ValueError(
            f'调度器 {scheduler!r} 暂不支持自动提交(当前支持: {", ".join(_DIALECTS)});'
            f'LSF/Shell 请改用"我的模板"模式并自定提交命令(后续版本支持)。')
    return cls()
