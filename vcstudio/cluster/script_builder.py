"""提交脚本双轨引擎(纯函数,不碰网络/不写文件)。

轨道 A(auto):资源参数 → 方言 directives + 环境行 + 运行块。
轨道 B(template):**尊重用户提交脚本**——用户模板一字不改,只替换已知占位符;
  未知的花括号内容(如 shell 的 ${PBS_NODEFILE})原样保留,绝不误伤。

已知占位符(含旧版 submit_template.sh 的历史别名):
  {job_name} {queue} {nodes} {ppn} {walltime} {remote_dir}
  {short}=job_name  {cores}=ppn  {dir}=作业目录名(相对 remote_root 的最后一段)
中文注释允许,英文标识符。
"""
from __future__ import annotations

import posixpath
import re

from vcstudio.cluster.schedulers import JobScriptSpec, SchedulerDialect

# 占位符 → JobScriptSpec 取值函数(dir 取 remote_dir 最后一段,与旧模板 cd …/{dir} 语义一致)
_PLACEHOLDERS = {
    'job_name': lambda s: s.job_name,
    'queue': lambda s: s.queue,
    'nodes': lambda s: s.nodes,
    'ppn': lambda s: s.ppn,
    'walltime': lambda s: s.walltime,
    'remote_dir': lambda s: s.remote_dir,
    'short': lambda s: s.job_name,
    'cores': lambda s: s.ppn,
    'dir': lambda s: posixpath.basename(s.remote_dir.rstrip('/')),
}

_SANITIZE_RE = re.compile(r'[^A-Za-z0-9_.-]')


def sanitize_job_name(name: str, max_len: int = 15) -> str:
    """作业名清洗:非法字符→'_';Torque 限 15 字符,超长截断;空→'vcs_job'。"""
    clean = _SANITIZE_RE.sub('_', (name or '').strip()) or 'vcs_job'
    if clean[0].isdigit():          # PBS 作业名不能以数字开头
        clean = 'j' + clean
    return clean[:max_len]


def render_auto(dialect: SchedulerDialect, spec: JobScriptSpec) -> str:
    """轨道 A:方言指令 + 环境准备行 + 运行块。"""
    lines = list(dialect.directives(spec))
    lines.append('')
    lines.extend(str(x) for x in (spec.env_lines or []) if str(x).strip())
    lines.append('')
    lines.extend(dialect.run_block(spec))
    return '\n'.join(lines).replace('\n\n\n', '\n\n') + '\n'


def find_placeholders(template_text: str) -> list:
    """模板中出现的**已知**占位符列表(未知花括号不算,留给 shell)。"""
    found = []
    for key in _PLACEHOLDERS:
        if '{' + key + '}' in template_text:
            found.append(key)
    return found


def render_template(template_text: str, spec: JobScriptSpec) -> str:
    """轨道 B:只替换已知占位符;所需值为空 → ValueError(中文,列明缺什么)。

    绝不使用 str.format(shell 的 ${VAR} 会炸);逐 token 替换,未知花括号原样保留。
    """
    needed = find_placeholders(template_text)
    missing = []
    values = {}
    for key in needed:
        val = _PLACEHOLDERS[key](spec)
        if val is None or str(val).strip() == '' or (key in ('ppn', 'cores') and int(val or 0) <= 0):
            missing.append(key)
        else:
            values[key] = str(val)
    if missing:
        raise ValueError(
            '模板占位符缺少取值: ' + ', '.join('{' + k + '}' for k in missing) +
            ';请在集群页补全对应资源参数。')
    out = template_text
    for key, val in values.items():
        out = out.replace('{' + key + '}', val)
    if not out.endswith('\n'):
        out += '\n'
    return out


def build_script(mode: str, dialect: SchedulerDialect, spec: JobScriptSpec,
                 template_text: str | None = None) -> str:
    """双轨分发。mode ∈ {'auto','template'}。"""
    if mode == 'template':
        if not template_text:
            raise ValueError('模板模式但未提供模板内容;请在集群页选择你的提交脚本模板。')
        return render_template(template_text, spec)
    if mode == 'auto':
        missing = [n for n, v in (('队列', spec.queue), ('VASP 命令', spec.vasp_cmd)) if not v]
        if spec.ppn <= 0:
            missing.append('每节点核数')
        if missing:
            raise ValueError('自动生成脚本缺少资源参数: ' + '、'.join(missing) + ';请在集群页填写。')
        return render_auto(dialect, spec)
    raise ValueError(f'未知脚本模式: {mode!r}(应为 auto / template)')
