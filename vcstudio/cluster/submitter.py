"""提交编排:preflight → 上传四件套+脚本 → 提交 → 回写 job.yaml;查状态 → 收敛判定。

client/sftp 由调用方注入(GUI 经 connection.open_client;测试注入假件),
本模块不 import paramiko——全部逻辑可离线测试。
安全阀:preflight 不过绝不出手;提交动作逐条写入 manifest.attempts(可审计)。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import hashlib
import math
import os
import posixpath
import re
import shlex
import shutil
import tempfile
import time

from vcstudio.cluster import script_builder
from vcstudio.cluster import diagnose
from vcstudio.cluster.schedulers import (
    JobScriptSpec, get_dialect, QUEUED, RUNNING, GONE,
)
from vcstudio.engines.calcspec import ENGINE_RUN_CONTRACTS, get_run_contract
from vcstudio.shared import manifest as manifest_mod

SCRIPT_NAME = 'vcs_job.sh'
_INPUT_FILES = ('INCAR', 'POTCAR', 'KPOINTS', 'POSCAR')
# 有些 VASP 派生任务除了传统“四件套”还会在启动阶段硬读额外文件。过去提交器
# 只上传四件套，导致能带(ICHARG=11)和 Dimer 目录在本地看似完整、到集群却立刻
# 因 CHGCAR/MODECAR 缺失失败。这里把额外输入视为任务契约：缺失时在联网前拦截。
_VASP_TASK_INPUTS = {
    'bands': ('CHGCAR',),
    'band': ('CHGCAR',),                 # 兼容早期清单别名
    'dimer': ('MODECAR',),
}
_SUPPORTED_ENGINES = tuple(ENGINE_RUN_CONTRACTS)
_ENGINE_LABELS = {
    'vasp': 'VASP',
    'gaussian': 'Gaussian',
    'cp2k': 'CP2K',
    'castep': 'CASTEP',
}
_ENGINE_COMMAND_EXAMPLES = {
    key: contract.command_example for key, contract in ENGINE_RUN_CONTRACTS.items()
}
# NEB 根目录共享文件(POSCAR 在各 image 子目录,不在根)
_NEB_ROOT_FILES = ('INCAR', 'POTCAR', 'KPOINTS')
_E0_RE = re.compile(r'E0=\s*([-+.\dEe]+)')
_NEB_FRAME_RE = re.compile(r'^\d+$')
# NEB 收敛标志(各 image 力收敛后 VASP 向 stdout 打印,与离子弛豫同串)
_NEB_CONVERGED_MARK = 'reached required accuracy'
_REMOTE_NAMESPACE_RE = re.compile(r'^[A-Za-z0-9_.-]{1,120}$')


def _job_engine(m: dict | None) -> str:
    """Return the manifest engine; old manifests remain VASP-compatible.

    ``inputs.engine`` was introduced by quick submit.  Older generated VASP
    jobs do not carry it, so an absent/blank value intentionally means VASP.
    A present but unsupported value is *not* coerced to VASP.
    """
    inputs = (m or {}).get('inputs') or {}
    if not isinstance(inputs, dict):
        return 'vasp'
    return str(inputs.get('engine') or 'vasp').strip().lower() or 'vasp'


def assert_profile_binding(profile, job_dir: str, action: str,
                           manifest: dict | None = None) -> dict:
    """Fail closed when a remote action targets a job owned by another server.

    Scheduler job ids and remote paths are only meaningful inside the cluster
    recorded in ``job.yaml``.  Letting the UI-selected profile override that
    identity could download unrelated files, mutate a foreign work directory,
    or even cancel an unrelated same-numbered job on another scheduler.
    """
    m = manifest if manifest is not None else manifest_mod.load_manifest(job_dir)
    if m is None:
        raise ValueError(f'{action}失败：作业目录缺可读 job.yaml')
    expected = str(getattr(profile, 'name', '') or '').strip()
    actual = str(m.get('cluster') or '').strip()
    if not actual:
        raise ValueError(f'{action}失败：作业尚未绑定服务器，不能使用「{expected}」执行远程操作')
    if not expected or actual != expected:
        raise ValueError(
            f'{action}失败：作业属于服务器「{actual}」，当前选择的是「{expected}」；'
            '为防止操作错误服务器，已阻止本次操作')
    return m


def _engine_command_template(profile, engine: str) -> str:
    """Select one engine's configured command without unsafe cross-fallback.

    ``vasp_cmd`` remains the fallback for VASP only.  Non-VASP engines must
    have their own entry in ``engine_commands`` and can therefore never run a
    stale ``vasp_std`` line by accident.
    """
    raw = getattr(profile, 'engine_commands', {}) or {}
    commands = {}
    if isinstance(raw, dict):
        commands = {str(key).strip().lower(): str(value or '').strip()
                    for key, value in raw.items()}
    command = commands.get(engine, '')
    if engine == 'vasp' and not command:
        command = str(getattr(profile, 'vasp_cmd', '') or '').strip()
    return command


def _missing_command_message(profile, engine: str) -> str:
    label = _ENGINE_LABELS.get(engine, engine or '未知引擎')
    if engine == 'vasp':
        return (
            f'集群「{getattr(profile, "name", "")}」未配置 VASP 执行命令；'
            '请填写 vasp_cmd（旧配置继续兼容）或 engine_commands.vasp。')
    example = _ENGINE_COMMAND_EXAMPLES.get(engine, '<完整执行命令>')
    return (
        f'集群「{getattr(profile, "name", "")}」未配置 {label} 执行命令；'
        f'请在 engine_commands.{engine} 中填写，例如「{example}」。'
        '为防止误算，系统不会用 VASP 命令代替。')


def _declared_input_files(job_dir: str, m: dict | None) -> tuple[list[str], list[str]]:
    """Resolve and validate the files uploaded for one manifest.

    VASP uses the four common files plus task-specific hard inputs (currently
    CHGCAR for bands and MODECAR for Dimer).  Other engines use the explicit
    ``inputs.files`` list.  The manifest is user-editable, so only files
    directly inside ``job_dir`` are accepted; this prevents ``../`` or an
    absolute path from uploading an unrelated local file under the user's SSH
    credentials.
    """
    engine = _job_engine(m)
    vasp_requirements: dict[str, str] = {}
    if engine == 'vasp':
        task = str((m or {}).get('task_type') or '').strip().lower()
        for name in _VASP_TASK_INPUTS.get(task, ()):
            vasp_requirements[name] = (
                '能带 ICHARG=11' if name == 'CHGCAR'
                else 'Dimer 初始模式' if name == 'MODECAR' else '该任务')
        icharg = _local_vasp_icharg(job_dir)
        if icharg in (1, 11):
            vasp_requirements['CHGCAR'] = f'INCAR ICHARG={icharg}'
        raw = list(_INPUT_FILES) + list(vasp_requirements)
    else:
        inputs = (m or {}).get('inputs') or {}
        raw = inputs.get('files') if isinstance(inputs, dict) else None
        if not isinstance(raw, list) or not raw:
            return [], [
                f'{_ENGINE_LABELS.get(engine, engine)} 作业清单缺 inputs.files；'
                '请从「快速提交」重新导入输入文件。']
    names: list[str] = []
    errs: list[str] = []
    for item in raw:
        name = str(item or '').strip()
        if (not name or os.path.isabs(name) or os.path.basename(name) != name
                or '/' in name or '\\' in name
                or name in (manifest_mod.MANIFEST_NAME, SCRIPT_NAME)):
            errs.append(f'作业清单输入文件名非法:{name!r}（只允许作业目录内的单个文件名）')
            continue
        if name in names:
            continue
        local_path = os.path.join(job_dir, name)
        if not os.path.isfile(local_path):
            if engine == 'vasp' and name in vasp_requirements:
                errs.append(
                    f'作业目录缺 {name}（{vasp_requirements[name]} 的必需输入，提交前请补齐）')
            elif engine == 'vasp':
                errs.append(f'作业目录缺 {name}(先在生成页产出四件套)')
            else:
                errs.append(f'作业目录缺 {name}（清单 inputs.files 已声明）')
            continue
        root = os.path.realpath(job_dir)
        resolved = os.path.realpath(local_path)
        try:
            inside = os.path.commonpath([root, resolved]) == root
        except ValueError:
            inside = False
        if not inside:
            errs.append(f'输入文件 {name} 指向作业目录之外，拒绝上传')
            continue
        names.append(name)
    if not names and not errs:
        errs.append('作业清单没有可上传的输入文件')
    return names, errs


def _local_vasp_icharg(job_dir: str) -> int | None:
    """读取本地 INCAR 的 ICHARG；提交/续算文件契约只以实际输入为准。"""
    try:
        from vcstudio.generate.incar_builder import parse_incar
        with open(os.path.join(job_dir, 'INCAR'), 'r', encoding='utf-8',
                  errors='replace') as handle:
            value = parse_incar(handle.read()).get('ICHARG')
        return int(value) if not isinstance(value, bool) else None
    except (OSError, TypeError, ValueError):
        return None


def _primary_input(engine: str, names: list[str]) -> str:
    """Choose the main file used by ``{input}``/``{stem}`` placeholders."""
    try:
        return get_run_contract(engine).primary_input(names)
    except ValueError:
        return names[0] if names else ''


def _engine_task(m: dict | None, job_dir: str | None = None) -> str | None:
    """Resolve a non-VASP task from manifest evidence, then its local input."""
    m = m or {}
    inputs = m.get('inputs') or {}
    if not isinstance(inputs, dict):
        inputs = {}
    raw = (inputs.get('gaussian_task') or inputs.get('task')
           or (m.get('task_type') if m.get('task_type') != 'quick' else None))
    if raw:
        return str(raw).strip().lower()
    if job_dir:
        try:
            from vcstudio.engines import get_backend
            return get_backend(_job_engine(m)).infer_task(job_dir)
        except (OSError, TypeError, ValueError):
            return None
    return None


def _non_vasp_input_contract_issues(engine: str, job_dir: str, names: list[str]) -> list[str]:
    """Run the engine checker before any SSH action."""
    try:
        from vcstudio.engines import get_backend
        contract = get_run_contract(engine)
        primary = contract.primary_input(names)
        issues = []
        if not primary or not primary.lower().endswith(contract.input_suffixes):
            issues.append(
                f'{_ENGINE_LABELS.get(engine, engine)} 清单没有受支持的主输入'
                f'({"/".join(contract.input_suffixes)})。')
        issues.extend(str(item) for item in get_backend(engine).check_inputs(job_dir))
        return issues
    except (OSError, TypeError, ValueError) as exc:
        return [f'{_ENGINE_LABELS.get(engine, engine)} 输入检查失败：{exc}']


def _render_engine_command(profile, engine: str, names: list[str],
                           job_name: str) -> str:
    """Render the small, documented placeholder set in an engine command.

    Unknown braces (including shell ``${VAR}``) are deliberately untouched.
    Filenames are shell-quoted before insertion.
    """
    command = _engine_command_template(profile, engine)
    if not command:
        return ''
    primary = _primary_input(engine, names)
    if ('{input}' in command or '{stem}' in command) and not primary:
        raise ValueError(
            f'{_ENGINE_LABELS.get(engine, engine)} 执行命令使用了 '
            '{input}/{stem}，但 job.yaml 没有可用的 inputs.files。')
    try:
        ppn = int(getattr(profile, 'ppn', 0) or 0)
    except (TypeError, ValueError):
        ppn = 0
    try:
        nodes = int(getattr(profile, 'nodes', 1) or 1)
    except (TypeError, ValueError):
        nodes = 1
    values = {
        'engine': engine,
        'input': shlex.quote(primary) if primary else '',
        'stem': shlex.quote(os.path.splitext(primary)[0]) if primary else '',
        'job_name': shlex.quote(job_name),
        'nodes': str(nodes),
        # 未配置核数时先保留 token；preflight/build_script_text 只在
        # 命令真正被脚本使用时报错。这样不会误伤自带运行行的旧 VASP 模板。
        'ppn': str(ppn) if ppn > 0 else '{ppn}',
        'cores': str(nodes * ppn) if ppn > 0 else '{cores}',
    }
    out = command
    for key, value in values.items():
        out = out.replace('{' + key + '}', value)
    return out


# ── NEB 多 image 作业辅助(一目录多 image 子目录,根共享 INCAR/POTCAR/KPOINTS) ──────
def _is_neb(m: dict | None) -> bool:
    """manifest task_type=='neb' → 走 NEB 专用上传/刷新/取证路径。"""
    return bool(m) and str(m.get('task_type') or '') == 'neb'


def _neb_n_images(m: dict | None) -> int | None:
    """中间 image 数:优先 manifest inputs.n_images。"""
    if not m:
        return None
    n = (m.get('inputs') or {}).get('n_images')
    try:
        return int(n)
    except (TypeError, ValueError):
        return None


def _neb_local_frames(job_dir: str) -> list:
    """本地 image 子目录名(纯数字升序):00,01,...,N+1。"""
    try:
        names = [d for d in os.listdir(job_dir)
                 if os.path.isdir(os.path.join(job_dir, d)) and _NEB_FRAME_RE.match(d)]
    except OSError:
        return []
    return sorted(names, key=lambda s: int(s))


def _read_incar_images(job_dir: str) -> int | None:
    """本地根 INCAR 的 IMAGES 值(缺/读不到 → None)。"""
    try:
        from vcstudio.generate.incar_builder import parse_incar
        with open(os.path.join(job_dir, 'INCAR'), 'r', encoding='utf-8', errors='replace') as f:
            v = parse_incar(f.read()).get('IMAGES')
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    except (OSError, ValueError, TypeError):
        return None


def _neb_input_check(job_dir: str, m: dict) -> list:
    """NEB 提交前输入检查:根共享文件 + 各 image 子目录 POSCAR + IMAGES 与目录数一致性。"""
    errs = []
    for f in _NEB_ROOT_FILES:
        if not os.path.isfile(os.path.join(job_dir, f)):
            errs.append(f'NEB 作业根目录缺 {f}(先在生成页产出 NEB 目录树)')
    frames = _neb_local_frames(job_dir)
    if len(frames) < 3:
        errs.append(f'NEB 作业 image 子目录不足(找到 {len(frames)} 个,至少需 00/01/02)')
    for fr in frames:
        if not os.path.isfile(os.path.join(job_dir, fr, 'POSCAR')):
            errs.append(f'NEB image 子目录 {fr} 缺 POSCAR')
    n = _neb_n_images(m)
    if frames and n is not None and (len(frames) - 2) != n:
        errs.append(
            f'NEB image 子目录数 {len(frames)}(含端点 → {len(frames) - 2} 中间 image)'
            f'与 manifest n_images={n} 不符')
    return errs


def _upload_neb_tree(client, sftp, job_dir: str, remote_dir: str) -> int:
    """整棵 NEB 目录树上传(根共享文件 + 各 image 子目录 POSCAR)。返回上传文件数。

    递归 os.walk:逐子目录远端 mkdir -p,逐文件 sftp.put;跳过 job.yaml/脚本/*.bak/figs。
    """
    skip_names = {manifest_mod.MANIFEST_NAME, SCRIPT_NAME}
    count = 0
    for root, dirs, files in os.walk(job_dir):
        dirs[:] = [d for d in dirs if d != 'figs']       # 结构图缓存不上传
        rel = os.path.relpath(root, job_dir)
        rdir = remote_dir if rel == '.' else posixpath.join(remote_dir, rel.replace(os.sep, '/'))
        if rel != '.':
            run_cmd(client, f'mkdir -p {shlex.quote(rdir)}', check=True)
        for fn in sorted(files):
            if fn in skip_names or '.bak' in fn:
                continue
            sftp.put(os.path.join(root, fn), posixpath.join(rdir, fn))
            count += 1
    return count


def run_cmd(client, cmd: str, timeout: int = 30, check: bool = False):
    """exec_command 薄封装 → (stdout_text, stderr_text)。

    check=True 时退出码非零抛 RuntimeError(先读尽输出再取退出码,防 paramiko 死锁)。
    """
    _in, out, err = client.exec_command(cmd, timeout=timeout)
    o = out.read().decode('utf-8', errors='replace')
    e = err.read().decode('utf-8', errors='replace')
    if check:
        status = out.channel.recv_exit_status()
        if status != 0:
            raise RuntimeError(f'远程命令失败(退出码 {status}):{cmd};{e.strip()[:200]}')
    return o, e


# ── preflight(全部本地检查,不联网) ─────────────────────────────────────────
def preflight(profile, job_dir: str) -> list:
    """提交前检查,返回错误文案列表(空=可以出手)。"""
    errs = []
    m0 = manifest_mod.load_manifest(job_dir)
    engine = _job_engine(m0)
    # 通用提交只能消费全新的 CREATED 清单。已有远端身份或任何后续状态都必须走
    # 查询/续算/显式重提流程；否则会覆盖正在运行的远端目录并产生孤儿双作业。
    if m0 is not None:
        if str(m0.get('state') or '') != 'CREATED':
            errs.append(
                f'作业状态为 {m0.get("state") or "?"}，仅全新的 CREATED 作业可提交；'
                '已有作业请查询状态或使用明确的续算流程')
        occupied = [key for key in ('cluster', 'remote_dir', 'scheduler_job_id')
                    if m0.get(key) not in (None, '')]
        if occupied:
            errs.append(
                '作业已带远端身份(' + '、'.join(occupied) + ')，拒绝重复提交；'
                '如需重算请新建作业目录或使用续算流程')
    if engine not in _SUPPORTED_ENGINES:
        errs.append(
            f'不支持的计算引擎:{engine!r}；可选 '
            + ' / '.join(_ENGINE_LABELS[key] for key in _SUPPORTED_ENGINES))
    if _is_neb(m0):
        # NEB 多 image:POSCAR 在各 image 子目录,根只放共享 INCAR/POTCAR/KPOINTS
        if engine != 'vasp':
            errs.append('NEB 目录只支持 VASP；请修正 job.yaml 的 inputs.engine')
        errs += _neb_input_check(job_dir, m0)
    elif engine in _SUPPORTED_ENGINES:
        _names, input_errs = _declared_input_files(job_dir, m0)
        errs += input_errs
        if engine != 'vasp' and not input_errs:
            errs += _non_vasp_input_contract_issues(engine, job_dir, _names)
    if m0 is None:
        errs.append('作业目录缺 job.yaml(旧目录可重新生成一次以补台账)')
    else:
        inputs0 = m0.get('inputs') or {}
        if not isinstance(inputs0, dict):
            inputs0 = {}
        namespace = str(inputs0.get('remote_namespace') or '')
        if namespace and not _REMOTE_NAMESPACE_RE.fullmatch(namespace):
            errs.append('远程命名空间非法：只允许单段字母/数字/._-')
        # POTCAR TITEL 闸(原版提交前防线):截断/拼错的 POTCAR 会给出"收敛但静默错"
        # 的能量——TITEL 段数必须等于 POSCAR 物种数,不等拒绝提交
        if engine == 'vasp':
            errs += _potcar_gate(job_dir, m0)
    if not profile.remote_root:
        errs.append('集群配置未填远程工作目录 remote_root')
    elif not str(profile.remote_root).startswith('/'):
        errs.append('remote_root 需为绝对路径(以 / 开头)')
    try:
        get_dialect(profile.scheduler)
    except ValueError as e:
        errs.append(str(e))
    try:
        profile_ppn = int(getattr(profile, 'ppn', 0) or 0)
    except (TypeError, ValueError):
        profile_ppn = 0
    mode = getattr(profile, 'script_mode', 'auto')
    if mode == 'template':
        tp = getattr(profile, 'template_path', '')
        if not tp or not os.path.isfile(tp):
            errs.append('模板模式但模板文件不存在;请在集群页重新选择')
        elif engine in _SUPPORTED_ENGINES:
            try:
                with open(tp, 'r', encoding='utf-8', errors='replace') as handle:
                    template_text = handle.read()
            except OSError as exc:
                errs.append(f'提交模板无法读取:{exc}')
                template_text = ''
            # 旧 VASP 模板常自带 vasp_std，继续允许。非 VASP 必须
            # 通过 {command} 明确接入对应引擎命令，避免误跑模板里的 VASP。
            if engine != 'vasp' and '{command}' not in template_text:
                errs.append(
                    f'{_ENGINE_LABELS[engine]} 模板模式必须在提交模板中放置 '
                    f'{{command}}；它会替换为 engine_commands.{engine}，'
                    '防止误跑 VASP。')
            if ('{command}' in template_text or engine != 'vasp') and not _engine_command_template(
                    profile, engine):
                errs.append(_missing_command_message(profile, engine))
            command_template = _engine_command_template(profile, engine)
            if (('{command}' in template_text or engine != 'vasp')
                    and command_template
                    and any(token in command_template for token in ('{cores}', '{ppn}'))
                    and profile_ppn <= 0):
                errs.append(
                    f'{_ENGINE_LABELS[engine]} 执行命令使用了核数占位符，'
                    '但集群配置的 ppn 未填写或无效')
    elif mode == 'auto':
        if not profile.queue:
            errs.append('自动脚本模式缺队列名')
        if profile_ppn <= 0:
            errs.append('自动脚本模式缺每节点核数 ppn')
        if engine in _SUPPORTED_ENGINES and not _engine_command_template(profile, engine):
            errs.append(_missing_command_message(profile, engine))
    else:
        errs.append(f'未知脚本模式: {mode!r}')
    return errs


def _potcar_gate(job_dir: str, m: dict) -> list:
    """本地 POTCAR 的 TITEL 段数 == manifest 物种数,否则拒提交(读不到不硬拦)。"""
    inputs = m.get('inputs') or {}
    elements = (inputs.get('elements') or []) if isinstance(inputs, dict) else []
    if not elements:
        return []
    try:
        with open(os.path.join(job_dir, 'POTCAR'), 'r', encoding='utf-8',
                  errors='replace') as f:
            n_titel = f.read().count('TITEL')
    except OSError:
        return []                                       # 缺文件已有独立检查项
    if n_titel != len(elements):
        return [f'POTCAR 完整性检查失败:TITEL 段数 {n_titel} ≠ 物种数 {len(elements)}'
                f'({" ".join(elements)});文件疑被截断/手改,请重新生成后再提交']
    return []


def _remote_dir_for(profile, job_dir: str, manifest: dict | None = None) -> str:
    """Build a stable, collision-resistant remote directory for one profile.

    Reusing only the local basename lets two projects called ``clean_slab``
    overwrite each other across separate submissions.  The readable prefix is
    therefore followed by a deterministic digest.  An already persisted path
    for the same profile is a compatibility boundary and is never migrated.
    """
    item = manifest if manifest is not None else manifest_mod.load_manifest(job_dir)
    existing = str((item or {}).get('remote_dir') or '').strip()
    existing_cluster = str((item or {}).get('cluster') or '').strip()
    if existing and existing_cluster == str(profile.name):
        return existing

    dir_name = os.path.basename(os.path.normpath(job_dir))
    inputs = (item or {}).get('inputs') or {}
    if not isinstance(inputs, dict):
        inputs = {}
    namespace = str(inputs.get('remote_namespace') or '')
    if namespace and not _REMOTE_NAMESPACE_RE.fullmatch(namespace):
        raise ValueError('远程命名空间非法，拒绝生成远程路径')
    canonical = os.path.normcase(os.path.realpath(os.path.abspath(job_dir)))
    identity = '\0'.join((
        'vcstudio-remote-v1', str(profile.name),
        str((item or {}).get('job_id') or ''),
        str((item or {}).get('created_at') or ''), canonical,
    ))
    suffix = hashlib.sha256(
        identity.encode('utf-8', errors='surrogatepass')).hexdigest()[:16]
    readable = script_builder.sanitize_job_name(dir_name, max_len=48)
    root = (posixpath.join(profile.remote_root, namespace)
            if namespace else profile.remote_root)
    return posixpath.join(root, f'{readable}--{suffix}')


def _spec_for(profile, job_dir: str, manifest: dict | None = None) -> JobScriptSpec:
    dir_name = os.path.basename(os.path.normpath(job_dir))
    manifest = manifest if manifest is not None else (manifest_mod.load_manifest(job_dir) or {})
    engine = _job_engine(manifest)
    names, _input_errs = _declared_input_files(job_dir, manifest)
    return JobScriptSpec(
        job_name=script_builder.sanitize_job_name(dir_name),
        remote_dir=_remote_dir_for(profile, job_dir, manifest),
        queue=getattr(profile, 'queue', ''),
        nodes=int(getattr(profile, 'nodes', 1) or 1),
        ppn=int(getattr(profile, 'ppn', 0) or 0),
        walltime=getattr(profile, 'walltime', '24:00:00') or '24:00:00',
        env_lines=list(getattr(profile, 'env_lines', []) or []),
        # JobScriptSpec 保留历史字段名，但此处装入的是 manifest
        # 指定引擎的命令，绝不是无条件 vasp_cmd。
        vasp_cmd=_render_engine_command(
            profile, engine, names, script_builder.sanitize_job_name(dir_name)),
    )


def planned_remote_dir(profile, job_dir: str) -> str:
    """Return the exact remote directory submit_job would use, without I/O."""
    return _spec_for(profile, job_dir).remote_dir


def build_script_text(profile, job_dir: str) -> str:
    """按 profile 双轨生成最终 job 脚本文本(预览按钮与真提交共用,所见即所交)。"""
    manifest = manifest_mod.load_manifest(job_dir) or {}
    engine = _job_engine(manifest)
    if engine not in _SUPPORTED_ENGINES:
        raise ValueError(f'不支持的计算引擎:{engine!r}')
    spec = _spec_for(profile, job_dir, manifest)
    dialect = get_dialect(profile.scheduler)
    template_text = None
    mode = getattr(profile, 'script_mode', 'auto')
    if mode == 'template':
        with open(profile.template_path, 'r', encoding='utf-8') as f:
            template_text = f.read()
        if engine != 'vasp' and '{command}' not in template_text:
            raise ValueError(
                f'{_ENGINE_LABELS[engine]} 模板必须包含 {{command}} 占位符；'
                f'它会使用 engine_commands.{engine}。')
    if (mode == 'auto' or engine != 'vasp'
            or (template_text is not None and '{command}' in template_text)) and not spec.vasp_cmd:
        raise ValueError(_missing_command_message(profile, engine))
    if ((mode == 'auto' or engine != 'vasp'
         or (template_text is not None and '{command}' in template_text))
            and any(token in spec.vasp_cmd for token in ('{cores}', '{ppn}'))):
        raise ValueError(
            f'{_ENGINE_LABELS[engine]} 执行命令使用了核数占位符，'
            '但集群配置的 ppn 未填写或无效。')
    return script_builder.build_script(
        mode, dialect, spec, template_text)


# ── 提交 ───────────────────────────────────────────────────────────────────
def submit_job(client, sftp, profile, job_dir: str) -> dict:
    """上传 + 提交一个作业;成功回写 manifest(UPLOADED→SUBMITTED)并返回之。

    失败抛 RuntimeError/ValueError(中文),manifest 不落 SUBMITTED。
    """
    errs = preflight(profile, job_dir)
    if errs:
        raise ValueError('；'.join(errs))
    m = manifest_mod.load_manifest(job_dir)
    spec = _spec_for(profile, job_dir, m)
    dialect = get_dialect(profile.scheduler)
    script_text = build_script_text(profile, job_dir)

    # 远程目录 + 上传(脚本统一 LF,防 Windows CRLF 毒害 shell)
    run_cmd(client, f'mkdir -p {shlex.quote(spec.remote_dir)}', check=True)
    if _is_neb(m):
        # NEB:整棵目录树(根共享 INCAR/POTCAR/KPOINTS + 各 image 子目录 POSCAR)
        n_up = _upload_neb_tree(client, sftp, job_dir, spec.remote_dir)
        upload_note = f'NEB 目录树 {n_up} 文件 + {SCRIPT_NAME}'
    else:
        engine = _job_engine(m)
        input_files = _declared_input_files(job_dir, m)[0]
        for fname in input_files:
            sftp.put(os.path.join(job_dir, fname), posixpath.join(spec.remote_dir, fname))
        upload_note = (
            f'{_ENGINE_LABELS.get(engine, engine)} {len(input_files)} 输入 + {SCRIPT_NAME}')
    with sftp.file(posixpath.join(spec.remote_dir, SCRIPT_NAME), 'w') as f:
        f.write(script_text.replace('\r\n', '\n'))
    m['cluster'] = profile.name
    m['remote_dir'] = spec.remote_dir
    manifest_mod.set_state(m, 'UPLOADED', note=upload_note)

    out, err = run_cmd(client, dialect.submit_cmd(
        posixpath.join(spec.remote_dir, SCRIPT_NAME),
        getattr(profile, 'scheduler_bin', '')))
    job_id = dialect.parse_job_id(out)
    if not job_id:
        manifest_mod.save_manifest(job_dir, m)     # 保留 UPLOADED 痕迹
        raise RuntimeError(f'提交失败,{dialect.name} 返回:{(out or err).strip()[:300]}')

    m['scheduler_job_id'] = job_id
    m['attempts'] = list(m.get('attempts') or [])
    m['attempts'].append({
        'n': len(m['attempts']) + 1,
        'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'job_id': job_id,
        'cluster': profile.name,
        'queue': spec.queue or None,
        'script_mode': getattr(profile, 'script_mode', 'auto'),
        'engine': _job_engine(m),
        # v3.3.0 实际核时统计:提交时点核数(nodes×ppn;ppn 未配 → None,usage 端不编数)
        'cores': (spec.nodes * spec.ppn) if spec.ppn else None,
    })
    manifest_mod.set_state(m, 'SUBMITTED', note=f'{dialect.name} {job_id}')
    manifest_mod.save_manifest(job_dir, m)
    return m


# ── 状态刷新(含最小收敛判定,S3 雏形) ────────────────────────────────────────
_QOK = '___VCSQOK___'


def query_scheduler(client, profile) -> tuple:
    """调度器一次查询 → ({job_id: QUEUED|RUNNING}, {job_id: 终态原因})。

    在命令尾追加 `&& echo 哨兵`:qstat/squeue 退出码非零(调度器抖动/不可达)时哨兵
    不出现 → 抛错本轮跳过,**绝不**把空输出误判成"所有作业都结束了"再逐个标终态
    (瞬时一次抖动会永久错标 RUNNING 作业为 FAILED/UNCONVERGED,原版用跨轮确认防此)。
    终态原因(Slurm TO/CA/NF/OOM 短暂可见)喂 diagnose 消歧——超墙钟 137 若无原因
    会被误判 OOM→FAILED 困死可续算作业(审查确认的接线断点)。
    """
    dialect = get_dialect(profile.scheduler)
    cmd = dialect.status_cmd(profile.username, getattr(profile, 'scheduler_bin', ''))
    out, _ = run_cmd(client, f'{cmd} && echo {_QOK}')
    if _QOK not in out:
        raise RuntimeError('调度器状态查询失败(qstat/squeue 无响应或报错);本轮跳过,不误判作业已结束')
    raw = out.replace(_QOK, '')
    return dialect.parse_status(raw), dialect.parse_terminal(raw)


def query_states(client, profile) -> dict:
    """兼容入口:只要统一态。新代码请用 query_scheduler(带终态原因)。"""
    return query_scheduler(client, profile)[0]


def query_queue_detail(client, profile) -> list:
    """调度器全量明细(该用户所有在队/在跑作业,含外部提交的)。

    → [{'job_id','state','name','workdir'}]。用途:「集群队列」视图 + 认领外部任务。
    同 query_scheduler 的哨兵防抖:查询失败抛错,绝不静默返回空当"队列空"。
    """
    dialect = get_dialect(profile.scheduler)
    cmd = dialect.detail_cmd(profile.username, getattr(profile, 'scheduler_bin', ''))
    out, _ = run_cmd(client, f'{cmd} && echo {_QOK}')
    if _QOK not in out:
        raise RuntimeError('调度器队列查询失败(qstat/squeue 无响应或报错)')
    return dialect.parse_detail(out.replace(_QOK, ''))


def query_workdir(client, profile, job_id: str) -> str:
    """认领辅助:按作业号查远程工作目录(PBS 走 qstat -f;Slurm 的 detail 已带 %Z
    → 方言返回空命令,这里不发远程调用直接 '')。查不到 → ''(交前端让用户手填)。"""
    dialect = get_dialect(profile.scheduler)
    cmd = dialect.workdir_cmd(job_id, getattr(profile, 'scheduler_bin', ''))
    if not cmd:
        return ''
    out, _ = run_cmd(client, cmd)
    return dialect.parse_workdir(out)


def adopt_external_job(local_dir: str, profile, job_id: str, remote_dir: str,
                       name: str = '', task_type: str | None = None) -> dict:
    """认领一个非本软件提交的集群作业:落 job.yaml + 入台账,之后查状态/拉回/续算全走原生路径。

    local_dir 是用户指定的本地目录(存在则直接用,不存在则创建;作为结果落点)。
    不上传/不动远端——认领只是登记事实:该作业号在该集群、结果在 remote_dir。
    状态置 SUBMITTED,下一次「查询状态」会按调度器现状推进(QUEUED/RUNNING/终态取证)。
    """
    if not str(remote_dir).startswith('/'):
        raise ValueError('远程目录需为绝对路径(以 / 开头)')
    existing = manifest_mod.load_manifest(local_dir)
    requested_task = (str(task_type).strip() if task_type is not None else '')
    requested_task = (manifest_mod.normalize_task_type(requested_task)
                      if requested_task else None)
    if existing:
        raw_existing_task = str(existing.get('task_type') or '').strip()
        if not raw_existing_task:
            if requested_task is None:
                raise ValueError('现有 job.yaml 缺 task_type；请显式选择计算类型后再认领')
            effective_task = requested_task
        else:
            effective_task = manifest_mod.normalize_task_type(raw_existing_task)
            if requested_task is not None and requested_task != effective_task:
                raise ValueError(
                    f'显式任务类型 {requested_task!r} 与现有 job.yaml '
                    f'的 {effective_task!r} 冲突；为避免改错收敛口径，拒绝覆盖')
    else:
        # 旧 API 调用没有 task_type 时仅对「全新目录」兼容为 relax。
        effective_task = requested_task or 'relax'
    if existing and existing.get('scheduler_job_id'):
        existing_job_id = str(existing.get('scheduler_job_id') or '')
        exact_binding = (
            existing_job_id == str(job_id)
            and str(existing.get('cluster') or '') == str(profile.name)
            and posixpath.normpath(str(existing.get('remote_dir') or ''))
            == posixpath.normpath(str(remote_dir)))
        if exact_binding:
            # 认领响应可能在前端收到前中断；同一 job/profile/remote 的重试只修复
            # 台账登记，不重复追加 attempts，也不改状态。
            from vcstudio.cluster import ledger
            ledger.register(local_dir)
            return existing
        raise ValueError(
            f'该本地目录已关联服务器「{existing.get("cluster") or "?"}」的作业号 '
            f'{existing_job_id}，与本次「{profile.name}」/{job_id} 不同；'
            '请换一个目录或先移出台账')
    # 所有参数/任务类型验证通过后才创建目录，失败调用不留空文件夹。
    os.makedirs(local_dir, exist_ok=True)
    dir_name = os.path.basename(os.path.normpath(local_dir))
    m = existing or manifest_mod.new_manifest(
        job_id=f'external-{dir_name}-{time.strftime("%Y%m%d-%H%M%S")}',
        system=name or dir_name, task_type=effective_task, calc_type='slab',
        inputs={'adopted': True, 'adopted_from': f'{profile.name}:{job_id}'})
    m['cluster'] = profile.name
    m['remote_dir'] = remote_dir
    m['scheduler_job_id'] = str(job_id)
    m['task_type'] = effective_task
    m.setdefault('attempts', []).append({
        'n': len(m.get('attempts') or []) + 1,
        'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'result': 'adopted',
        'job_id': str(job_id),
        'cluster': profile.name,
    })
    manifest_mod.set_state(m, 'SUBMITTED', note=f'认领外部作业 {job_id}(remote: {remote_dir})')
    manifest_mod.save_manifest(local_dir, m)
    from vcstudio.cluster import ledger
    ledger.register(local_dir)
    return m


# 续算沉降护栏:续算后连续 SETTLE_MAX_CHECKS 次仍"调度器无此作业 + OUTCAR 未刷新",
# 才放行终态取证(几乎必然是重投即被拒);正常情形新作业一两轮内就会现身/写出新 OUTCAR。
SETTLE_MAX_CHECKS = 3


def _mark_observed_alive(m: dict) -> None:
    """标记当前(重投)轮已在调度器现身(QUEUED/RUNNING)。续算沉降护栏据此放行。"""
    r = m.setdefault('results', {})
    r['observed_alive'] = True
    r.pop('settling', None)  # 现身即清沉降态


def _set_continue_baseline(m: dict, old_outcar_mtime) -> None:
    """续算重投时落基线:上一轮 OUTCAR 的 mtime + 复位本轮存活标记与沉降态。

    refresh_job 的沉降护栏据此判断"新一轮是否真的重写过 OUTCAR",
    避免新作业还没启动时误读旧 OUTCAR 而判终态(见 _in_continue_settling)。
    """
    r = m.setdefault('results', {})
    r['continue_baseline'] = {'outcar_mtime': old_outcar_mtime, 'settle_checks': 0,
                              'at': time.strftime('%Y-%m-%dT%H:%M:%S')}
    r['observed_alive'] = False
    r.pop('settling', None)


def _in_continue_settling(m: dict, reason, outcar_mtime) -> bool:
    """续算后作业仍未真正启动本轮 → 返回 True(保持 SUBMITTED,不判终态)。

    仅对续算过(continue_rounds≥1)、当前 SUBMITTED、且无调度器终态原因的作业生效。
    "已启动本轮"的证据:本轮在调度器现过身(observed_alive),或 OUTCAR 相对续算基线
    被重写过(mtime 变化)。两者皆无 → 说明新作业还没被登记/还没写新 OUTCAR,手里那份是
    上一轮旧 OUTCAR,绝不能拿去判终态。连续 SETTLE_MAX_CHECKS 次仍如此才兜底放行。
    调用方已确保 u==GONE(非 QUEUED/RUNNING)。返回 True 时已就地更新 results,调用方需落盘。
    """
    r = m.get('results') or {}
    rounds = int(r.get('continue_rounds', 0) or 0)
    if rounds < 1 or m.get('state') != 'SUBMITTED' or reason:
        return False
    if r.get('observed_alive'):
        return False
    baseline = r.get('continue_baseline') or {}
    base_mtime = baseline.get('outcar_mtime')
    rewritten = outcar_mtime is not None and outcar_mtime != base_mtime
    if rewritten:
        return False  # 本轮确已重写 OUTCAR → 真终态,放行取证
    # 仍是旧 OUTCAR(或暂无 OUTCAR):记一次沉降观测
    checks = int(baseline.get('settle_checks', 0) or 0) + 1
    baseline['settle_checks'] = checks
    r['continue_baseline'] = baseline
    r['settling'] = {
        'checks': checks,
        'reason': '续算后新作业尚未被调度器登记或尚未写出新 OUTCAR,暂不判终态(疑仍在排队)',
        'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    m['results'] = r
    return checks < SETTLE_MAX_CHECKS


def _engine_output_evidence(client, remote: str, m: dict, job_dir: str | None = None):
    """Read the main non-VASP output's size and tail without downloading it."""
    files = fetch_files_for_manifest(m, job_dir=job_dir)
    name = files[0] if files else ''
    if not remote or not name:
        return name, None, ''
    path = shlex.quote(posixpath.join(remote, name))
    out, _ = run_cmd(
        client,
        f'stat -c "%s" {path} 2>/dev/null || true; '
        f'echo "___VCSENGINE___"; tail -n 2000 {path} 2>/dev/null')
    size_text, _sep, tail = out.partition('___VCSENGINE___')
    size = None
    for token in size_text.split():
        if token.isdigit():
            size = int(token)
            break
    return name, size, tail


def _refresh_non_vasp_terminal(client, job_dir: str, m: dict, reason):
    """Terminal-state evidence for Gaussian/CP2K/CASTEP quick jobs.

    These engines do not create VASP's OUTCAR/OSZICAR, so applying the VASP
    classifier always produced NO_OUTPUT.  A job is DONE only when its own
    normal-termination footer is present and the wrapper did not report a
    non-zero exit.  Ambiguous output is sent to NEEDS_HUMAN, never guessed.
    """
    engine = _job_engine(m)
    remote = str(m.get('remote_dir') or '')
    jid = str(m.get('scheduler_job_id') or '')
    output_name, output_size, output_tail = _engine_output_evidence(
        client, remote, m, job_dir=job_dir)
    exit_code, log_tail = _read_log(client, remote, jid)
    combined = (output_tail or '') + '\n' + (log_tail or '')
    task = _engine_task(m, job_dir)
    try:
        from vcstudio.engines import get_backend
        parsed = dict(get_backend(engine).parse_output_text(combined, task=task) or {})
    except (AttributeError, TypeError, ValueError) as exc:
        parsed = {'energy_ev': None, 'converged': False,
                  'normal_termination': False, 'task_converged': False,
                  'failed': True, 'error': f'解析器异常：{exc}'}
    normal = bool(parsed.get('normal_termination'))
    task_converged = bool(parsed.get('task_converged'))
    parser_failed = bool(parsed.get('failed'))

    reason_map = {
        diagnose.R_TIMEOUT: ('WALLTIME', 'UNCONVERGED'),
        diagnose.R_OOM: ('OOM', 'FAILED'),
        diagnose.R_CANCELLED: ('CANCELLED', 'FAILED'),
        diagnose.R_NODE_FAIL: ('NODE_FAIL', 'FAILED'),
        diagnose.R_FAILED: ('SCHEDULER_FAILED', 'FAILED'),
    }
    contract = get_run_contract(engine)
    restartable = bool(contract.restart_supported)
    if reason in reason_map:
        failure_class, state = reason_map[reason]
        evidence = f'调度器报 {reason}；{contract.restart_note}'
    elif exit_code not in (None, 0):
        failure_class, state = 'ENGINE_EXIT_NONZERO', 'FAILED'
        evidence = f'{_ENGINE_LABELS.get(engine, engine)} 退出码 {exit_code}'
    elif parser_failed:
        failure_class, state = 'ENGINE_OUTPUT_ERROR', 'FAILED'
        evidence = str(parsed.get('error') or
                       f'{_ENGINE_LABELS.get(engine, engine)} 输出含失败标志')
    elif (parsed.get('converged') and parsed.get('energy_ev') is not None
          and (output_size is None or output_size > 0)):
        failure_class, state = diagnose.CONVERGED, 'DONE'
        evidence = (f'{_ENGINE_LABELS.get(engine, engine)} 正常结束、任务级完成与最终能量'
                    f'三项证据齐全（{output_name or "主输出"}）')
    elif normal and not task_converged:
        failure_class, state = 'TASK_NOT_CONVERGED', 'UNCONVERGED'
        evidence = str(parsed.get('error') or
                       f'{_ENGINE_LABELS.get(engine, engine)} 正常退出但任务未收敛；'
                       f'{contract.restart_note}')
    elif normal and parsed.get('energy_ev') is None:
        failure_class, state = 'FINAL_ENERGY_MISSING', 'NEEDS_HUMAN'
        evidence = str(parsed.get('error') or '正常退出但缺最终能量，拒绝冒充完成')
    elif not output_size:
        failure_class, state = diagnose.NO_OUTPUT, 'NEEDS_HUMAN'
        evidence = f'{_ENGINE_LABELS.get(engine, engine)} 主输出 {output_name or "未声明"} 缺失或为空'
    else:
        failure_class, state = 'NORMAL_TERMINATION_NOT_FOUND', 'NEEDS_HUMAN'
        evidence = (f'{output_name} 有内容但未见 {_ENGINE_LABELS.get(engine, engine)} '
                    '正常结束标志，拒绝冒充完成')

    energy = parsed.get('energy_ev')
    if (isinstance(energy, bool) or not isinstance(energy, (int, float))
            or not math.isfinite(float(energy))):
        energy = None
    results = m.setdefault('results', {})
    if energy is not None:
        if state == 'DONE':
            results['energy_e0_eV'] = float(energy)
            results.pop('raw_energy_e0_eV', None)
        else:
            results['raw_energy_e0_eV'] = float(energy)
            results.pop('energy_e0_eV', None)
    elif state != 'DONE':
        results.pop('energy_e0_eV', None)
    results['diagnosis'] = {
        'failure_class': failure_class,
        'restartable': restartable,
        'evidence': evidence,
        'scheduler_reason': reason,
        'exit_code': exit_code,
        'engine': engine,
        'task': parsed.get('task') or task,
        'parser_error': parsed.get('error'),
        'normal_termination': normal,
        'task_converged': task_converged,
        'restart_note': contract.restart_note,
        'output_file': output_name,
        'output_bytes': output_size,
        'classified_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    if state != 'DONE':
        m.setdefault('attempts', []).append({
            'n': len(m.get('attempts') or []) + 1,
            'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'result': 'failed', 'failure_class': failure_class, 'to_state': state,
        })
    manifest_mod.set_state(m, state, note=f'{failure_class}: {evidence}')
    manifest_mod.save_manifest(job_dir, m)
    return m


def refresh_job(client, profile, job_dir: str, live_states: dict | None = None,
                terminal_reasons: dict | None = None) -> dict:
    """按调度器现状更新一个作业的 manifest;终态时做取证 + 失败分类。

    live_states/terminal_reasons 可传入 query_scheduler 结果避免逐作业重复查询。
    终态分支不再只有 DONE/UNCONVERGED:调 diagnose.classify 综合 调度器原因 + 退出码 +
    OUTCAR/OSZICAR 完整性 + 日志签名 + 收敛串 + 能量合理性 → DONE/UNCONVERGED/FAILED/
    NEEDS_HUMAN,并把结构化诊断写回 results.diagnosis + 失败时追加 attempts(可审计)。
    """
    m = manifest_mod.load_manifest(job_dir)
    if m is None or not m.get('scheduler_job_id'):
        return m
    states = live_states if live_states is not None else query_states(client, profile)
    jid = str(m['scheduler_job_id'])
    u = states.get(jid, GONE)
    remote = m.get('remote_dir') or ''

    # NEB 多 image 走专用路径:主输出在各 image 子目录,根无 OUTCAR,收敛判定在 stdout
    if _is_neb(m):
        return _refresh_neb_job(client, profile, job_dir, m, u,
                                (terminal_reasons or {}).get(jid))

    if u == QUEUED:
        # 本轮已在调度器现身:记下"活过",续算沉降护栏据此放行(不再疑为旧 OUTCAR)
        _mark_observed_alive(m)
        if m['state'] != 'QUEUED':
            manifest_mod.set_state(m, 'QUEUED')
        manifest_mod.save_manifest(job_dir, m)
        return m
    if u == RUNNING:
        _mark_observed_alive(m)
        if m['state'] != 'RUNNING':
            manifest_mod.set_state(m, 'RUNNING')
        engine = _job_engine(m)
        task = _canonical_vasp_task(m)
        # 静态/频率没有“离子步”，若套用 relax 的 0 步告警，连续刷新三次便会假报
        # 首步卡死。只对真正沿结构/时间推进的 VASP 任务启用该活体判据；其他任务
        # 保留无告警的类型提示，终态再按各自完成证据裁决。
        if engine == 'vasp' and (task in _IONIC_TASKS or task == 'aimd'):
            live = _live_check(client, remote, (m.get('results') or {}).get('live'))
            if task == 'aimd':
                live['md_steps'] = live.get('ionic_steps')
                live['progress_kind'] = 'md'
            else:
                live['progress_kind'] = 'ionic'
            m.setdefault('results', {})['live'] = live
        else:
            m.setdefault('results', {})['live'] = {
                'warning': '', 'progress_kind': ('engine' if engine != 'vasp' else task),
            }
        manifest_mod.save_manifest(job_dir, m)
        return m

    # 终态候选(GONE / 调度器终态原因):先取 OUTCAR 现状(含 mtime)
    reason = (terminal_reasons or {}).get(jid)
    if _job_engine(m) != 'vasp':
        return _refresh_non_vasp_terminal(client, job_dir, m, reason)
    outcar_size, oszicar_size, outcar_mtime = _stat_outcar_full(client, remote)

    # ── 续算沉降护栏(修复:续算后仍在排队却被误判终态)─────────────────────────
    # 症状:续算重投 → 新作业号还没进 qstat(登记有延迟)→ 这里查为 GONE → 直接进终态分支
    # → 抓到的却是**上一轮**留下的完整 OUTCAR(收敛/SCF 震荡串还在)→ 明明在排队,
    # 却被标成 DONE / 需续算 / sloshing。只对**续算过**的作业设防(唯有它们才可能有旧 OUTCAR),
    # 且要求"当前这一轮确有产出"才认终态:本轮在调度器现过身,或 OUTCAR 相对续算基线被重写过。
    if _in_continue_settling(m, reason, outcar_mtime):
        manifest_mod.save_manifest(job_dir, m)
        return m
    m.setdefault('results', {}).pop('settling', None)  # 放行终态 → 清沉降态
    # ────────────────────────────────────────────────────────────────────────

    task = _canonical_vasp_task(m)
    inputs = m.get('inputs') or {}
    expected_steps = inputs.get('steps') if isinstance(inputs, dict) and task == 'aimd' else None
    converged, clean_exit, stopped = _grep_marks(
        client, remote, task, expected_steps=expected_steps)
    exit_code, log_tail = _read_log(client, remote, jid)
    energy, oszicar_tail = _read_oszicar(client, remote)

    d = diagnose.classify(
        scheduler_reason=reason, exit_code=exit_code,
        outcar_size=outcar_size, oszicar_size=oszicar_size,
        log_tail=log_tail, converged=converged, energy=energy,
        oszicar_tail=oszicar_tail, nelm=_read_nelm(job_dir),
        clean_exit=clean_exit, stopped=stopped)

    if energy is not None:
        # 物理合理性闸(审查#4):BAD_ENERGY 的垃圾数不进 energy_e0_eV(防经自由能路径
        # 漏进 ΔG/U_L 图),原始值留 raw_energy_e0_eV 供人工核查
        key = 'raw_energy_e0_eV' if d.failure_class == diagnose.BAD_ENERGY else 'energy_e0_eV'
        m.setdefault('results', {})[key] = energy
    m.setdefault('results', {})['diagnosis'] = {
        'failure_class': d.failure_class,
        'restartable': d.restartable,
        'evidence': d.evidence,
        'scheduler_reason': reason,
        'exit_code': exit_code,
        'clean_exit': clean_exit,
        'outcar_bytes': outcar_size,
        'classified_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    if d.failure_class != diagnose.CONVERGED:
        m.setdefault('attempts', []).append({
            'n': len(m.get('attempts') or []) + 1,
            'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'result': 'failed',
            'failure_class': d.failure_class,
            'to_state': d.state,
        })
    manifest_mod.set_state(m, d.state, note=f'{d.failure_class}: {d.evidence}')
    manifest_mod.save_manifest(job_dir, m)
    return m


def _neb_remote_intermediate_count(client, remote: str):
    """远端 image 子目录探测 → 中间 image 数(总帧数−2);列不到 → None(不据此误判)。"""
    if not remote:
        return None
    out, _ = run_cmd(client, f'cd {shlex.quote(remote)} && ls -1d [0-9][0-9] 2>/dev/null')
    dirs = [ln.strip() for ln in out.splitlines() if _NEB_FRAME_RE.match(ln.strip())]
    if not dirs:
        return None
    return max(len(dirs) - 2, 0)


def _neb_image_energies(client, remote: str, n_images):
    """各中间 image(01..N)末 E0 列表(缺 → None);运行中进度 + 终态取证共用。"""
    energies = []
    n = int(n_images or 0)
    for i in range(1, n + 1):
        e0, _tail = _read_oszicar(client, posixpath.join(remote, f'{i:02d}'))
        energies.append(e0)
    return energies


def _neb_forensics(client, remote: str, n_images):
    """终态取证:逐 image(01..N)读 OSZICAR → (energies, image_status, images_found)。

    image_status 每项 {'index','energy','empty','scf_fail'}:empty=OSZICAR 无内容(启动即死),
    scf_fail=末离子步 SCF 震荡(diagnose 判据)。images_found 为远端探测的中间 image 数。
    """
    energies, status = [], []
    n = int(n_images or 0)
    for i in range(1, n + 1):
        e0, tail = _read_oszicar(client, posixpath.join(remote, f'{i:02d}'))
        empty = not (tail or '').strip()
        scf_fail = diagnose.scan_oszicar_sloshing(tail) is not None
        energies.append(e0)
        status.append({'index': i, 'energy': e0, 'empty': empty, 'scf_fail': scf_fail})
    images_found = _neb_remote_intermediate_count(client, remote)
    return energies, status, images_found


def _grep_neb_converged(client, remote: str, job_id: str = '') -> bool:
    """NEB 收敛判定:stdout 出现 'reached required accuracy'(各 image 力收敛)→ True。

    NEB 根目录无 OUTCAR,收敛标志打到作业 stdout(*.o<num>/slurm-<num>.out/vasp.out/log)。
    按本作业号精确定位(同 _read_log,避免续算读到旧轮)。
    """
    if not remote:
        return False
    num = str(job_id).split('.')[0].strip()
    if num:
        targets = f'*.o{num} slurm-{num}.out vasp.out log stdout'
    else:
        targets = '*.o* slurm-*.out vasp.out log stdout'
    out, _ = run_cmd(
        client,
        f'cd {shlex.quote(remote)} && grep -h "{_NEB_CONVERGED_MARK}" {targets} 2>/dev/null | head -1')
    return _NEB_CONVERGED_MARK in out


def _refresh_neb_job(client, profile, job_dir: str, m: dict, u: str, reason):
    """NEB 作业状态刷新:QUEUED/RUNNING 沿用状态机(活体取各 image 进度),终态走 NEB 取证。

    终态取证:stdout 收敛标志 + 逐 image OSZICAR(能量/崩溃)+ 远端 image 数,
    交 diagnose.classify_neb 裁定(IMAGES 不符/image 缺输出/image SCF 崩/收敛/未收敛)。
    """
    remote = m.get('remote_dir') or ''
    n = _neb_n_images(m)

    if u == QUEUED:
        _mark_observed_alive(m)
        if m['state'] != 'QUEUED':
            manifest_mod.set_state(m, 'QUEUED')
        manifest_mod.save_manifest(job_dir, m)
        return m
    if u == RUNNING:
        _mark_observed_alive(m)
        if m['state'] != 'RUNNING':
            manifest_mod.set_state(m, 'RUNNING')
        # 活体进度:各 image 当前 E0(NEB 根无 OUTCAR,不查根收敛/SCF)
        m.setdefault('results', {})['neb_energies'] = _neb_image_energies(client, remote, n)
        manifest_mod.save_manifest(job_dir, m)
        return m

    # 终态取证
    jid = str(m.get('scheduler_job_id') or '')
    converged = _grep_neb_converged(client, remote, jid)
    exit_code, log_tail = _read_log(client, remote, jid)
    energies, status, images_found = _neb_forensics(client, remote, n)
    images_expected = _read_incar_images(job_dir)
    if images_expected is None:
        images_expected = n

    d = diagnose.classify_neb(
        images_expected=images_expected, images_found=images_found,
        image_status=status, converged=converged, scheduler_reason=reason,
        exit_code=exit_code, log_tail=log_tail)

    r = m.setdefault('results', {})
    r['neb_energies'] = energies
    r['diagnosis'] = {
        'failure_class': d.failure_class,
        'restartable': d.restartable,
        'evidence': d.evidence,
        'scheduler_reason': reason,
        'exit_code': exit_code,
        'images_found': images_found,
        'classified_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    if d.failure_class != diagnose.CONVERGED:
        m.setdefault('attempts', []).append({
            'n': len(m.get('attempts') or []) + 1,
            'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'result': 'failed',
            'failure_class': d.failure_class,
            'to_state': d.state,
        })
    manifest_mod.set_state(m, d.state, note=f'{d.failure_class}: {d.evidence}')
    manifest_mod.save_manifest(job_dir, m)
    return m


def _live_check(client, remote: str, prev: dict | None) -> dict:
    """运行中作业一次 SSH 活体取数 → live dict(健康计数器+进度)。

    取 离子步数(grep -c 'F=' 全文精确)/ 当前 |F|max(OUTCAR 'FORCES: max atom'
    末行,原版 running_progress 口径)/ OSZICAR 尾部(震荡扫描),交 diagnose.live_health
    做跨轮确认。远端取数失败 → 保留上轮计数器不误清零。
    """
    if not remote:
        return dict(prev or {})
    q = shlex.quote
    out, _ = run_cmd(
        client,
        f'grep -c "F=" {q(remote + "/OSZICAR")} 2>/dev/null || echo 0; '
        f"echo '___VCSLIVE___'; "
        f'grep "FORCES: max atom" {q(remote + "/OUTCAR")} 2>/dev/null | tail -1; '
        f"echo '___VCSLIVE___'; "
        f'tail -n 200 {q(remote + "/OSZICAR")} 2>/dev/null')
    parts = out.split('___VCSLIVE___')
    if len(parts) != 3:
        return dict(prev or {})                     # 取数异常:保留上轮计数,不误清零
    steps = _first_int(parts[0])
    fmax = ''
    toks = parts[1].split()
    if 'RMS' in toks:                                # 'FORCES: max atom, RMS  0.031  0.012'
        i = toks.index('RMS')
        if len(toks) > i + 1:
            fmax = toks[i + 1]
    live = diagnose.live_health(parts[2], steps, prev)
    live['ionic_steps'] = steps
    live['fmax'] = fmax
    return live


def _stat_sizes(client, remote: str):
    """一次 stat 取 OUTCAR/OSZICAR 字节数 → (outcar, oszicar);缺失文件对应 None。

    与收敛 grep 分开:区分"缺输出/启动即死"(size None/0)与"跑了但没收敛",
    堵掉旧版 '|| echo 0' 把缺 OUTCAR 误当未收敛的假阴性(缺口分析 P0)。
    """
    outcar, oszicar, _mtime = _stat_outcar_full(client, remote)
    return outcar, oszicar


def _stat_outcar_full(client, remote: str):
    """一次 stat 取 OUTCAR/OSZICAR 的字节数与 OUTCAR mtime(远端 epoch 秒)。

    → (outcar_size, oszicar_size, outcar_mtime);缺失/无 remote 对应 None。
    mtime 用于续算沉降护栏:判断"当前这一轮是否真的重写过 OUTCAR",
    避免续算后新作业尚未启动时误读上一轮旧 OUTCAR(见 refresh_job)。
    mtime 与 baseline 同取自集群 stat,同一时钟,无 client/server 时钟偏差问题。
    """
    if not remote:
        return None, None, None
    out, _ = run_cmd(client, f"cd {shlex.quote(remote)} && "
                             f"stat -c '%n %s %Y' OUTCAR OSZICAR 2>/dev/null")
    sizes: dict[str, int] = {}
    mtimes: dict[str, int] = {}
    for line in out.splitlines():
        parts = line.split()
        # 兼容旧格式(仅 name size,2 列)与新格式(name size mtime,3 列)
        if len(parts) >= 2 and parts[1].lstrip('-').isdigit():
            sizes[parts[0]] = int(parts[1])
        if len(parts) >= 3 and parts[2].lstrip('-').isdigit():
            mtimes[parts[0]] = int(parts[2])
    return sizes.get('OUTCAR'), sizes.get('OSZICAR'), mtimes.get('OUTCAR')


# 收敛证据必须按任务语义分流：离子优化、电子单点、有限差分频率和 AIMD 的“完成”
# 不是同一件事。旧版只把 static/dos/band 当电子任务，其余新目录都会误用离子标志。
_IONIC_MARK = 'reached required accuracy'
_ELEC_MARK = 'aborting loop because EDIFF is reached'
_FREQ_MARK = 'THz'
_AIMD_MARK = 'F='

_IONIC_TASKS = frozenset({'relax', 'cellopt', 'dimer'})
_ELECTRONIC_TASKS = frozenset({
    'static', 'dos', 'dos_pdos', 'pdos', 'band', 'bands', 'bader',
    'chgdiff', 'elf', 'eos', 'workfunction', 'esp', 'vaspsol',
    'conv_scan', 'conv_encut', 'conv_kmesh', 'conv_vacuum',
    'conv_thickness', 'surface_energy', 'formation_binding',
})


def _canonical_vasp_task(m_or_task) -> str:
    """Normalise old aliases and estatic ``inputs.purpose`` into one task key."""
    if isinstance(m_or_task, dict):
        task = str(m_or_task.get('task_type') or '').strip().lower()
        inputs = m_or_task.get('inputs') or {}
        if not isinstance(inputs, dict):
            inputs = {}
        purpose = str(inputs.get('purpose') or '').strip().lower()
        declared_task = str(inputs.get('task') or '').strip().lower()
        # 老 estatic 清单曾统一写 static + purpose；规范清单可直接写具体任务。
        if task in ('', 'static'):
            task = {
                'pdos': 'dos_pdos', 'bader': 'bader', 'chgdiff': 'chgdiff',
                'esp': 'workfunction', 'elf': 'elf',
            }.get(purpose, task or 'static')
        if declared_task == 'workfunction':
            task = 'workfunction'
    else:
        task = str(m_or_task or '').strip().lower()
    return {'band': 'bands', 'dos': 'dos_pdos', 'pdos': 'dos_pdos'}.get(task, task)


# 干净退出页脚 + STOPCAR 叫停(网研核对:Kitchin/sisl 口径 completed≠converged;
# pymatgen Outcar.is_stopped 的 soft stop 双空格字面串)
_CLEAN_EXIT_MARK = 'General timing and accounting informations for this job'
_STOP_MARK = 'soft stop encountered'


def _grep_marks(client, remote: str, task_type: str = 'relax', *, expected_steps=None):
    """一次 SSH 取 OUTCAR 三个标志计数 → (converged, clean_exit, stopped)。

    - 离子优化/Dimer:OUTCAR 的力收敛标志；
    - 电子静态/性质/收敛扫描:OUTCAR 的 EDIFF 标志；
    - 频率:至少产出频率(THz)且有干净页脚；
    - AIMD:OSZICAR 的 MD 步数达到期望且有干净页脚。

    缺文件时三者 (False, False, False)。静态/离子任务的第一位表示「收敛串在场」，
    必须与第二位 clean_exit 一并交给 diagnose 才可判 DONE；保留原始收敛位是为了
    把「旧收敛串 + 截断」精确标成 NEEDS_HUMAN，而不是自动续算。
    """
    if not remote:
        return False, False, False
    task = _canonical_vasp_task(task_type)
    if task == 'freq':
        mark, marker_file = _FREQ_MARK, 'OUTCAR'
    elif task == 'aimd':
        mark, marker_file = _AIMD_MARK, 'OSZICAR'
    elif task in _ELECTRONIC_TASKS:
        mark, marker_file = _ELEC_MARK, 'OUTCAR'
    else:
        # 未知旧清单沿用离子口径，避免无证据地把历史 relax 改成静态。
        mark, marker_file = _IONIC_MARK, 'OUTCAR'
    o = shlex.quote(remote + '/OUTCAR')
    marker_path = shlex.quote(remote + '/' + marker_file)
    out, _ = run_cmd(
        client,
        f'c=$(grep -c "{mark}" {marker_path} 2>/dev/null || true); '
        f'printf "%s\\n" "${{c:-0}}"; '
        f'c=$(grep -c "{_CLEAN_EXIT_MARK}" {o} 2>/dev/null || true); '
        f'printf "%s\\n" "${{c:-0}}"; '
        f'c=$(grep -c "{_STOP_MARK}" {o} 2>/dev/null || true); '
        f'printf "%s\\n" "${{c:-0}}"')
    nums = [_first_int(ln) for ln in out.splitlines() if ln.strip()]
    nums += [0] * (3 - len(nums))
    marker_count, clean_count, stop_count = nums[:3]
    clean_exit = clean_count > 0
    if task == 'freq':
        converged = marker_count > 0 and clean_exit
    elif task == 'aimd':
        try:
            need = max(int(expected_steps or 0), 0)
        except (TypeError, ValueError):
            need = 0
        converged = marker_count > 0 and clean_exit and (need == 0 or marker_count >= need)
    else:
        converged = marker_count > 0
    return converged, clean_exit, stop_count > 0


def _grep_converged(client, remote: str, task_type: str = 'relax') -> bool:
    """兼容入口:只要收敛位。"""
    return _grep_marks(client, remote, task_type)[0]


def _read_nelm(job_dir: str) -> int:
    """本地作业目录 INCAR 的 NELM(缺失/读不到 → VASP 默认 60)。"""
    try:
        from vcstudio.generate.incar_builder import parse_incar
        with open(os.path.join(job_dir, 'INCAR'), 'r', encoding='utf-8', errors='replace') as f:
            v = parse_incar(f.read()).get('NELM')
        return int(v) if isinstance(v, (int, float)) and int(v) > 0 else 60
    except (OSError, ValueError, TypeError):
        return 60


def _read_log(client, remote: str, job_id: str = ''):
    """取本作业的 stdout 日志:EXIT 标记(退出码)+ 尾部文本(供 diagnose.scan_log 扫签名)。

    脚本 run_block 尾部 echo "EXIT: $?";PBS -j oe 合并到 <name>.o<num>,Slurm 到
    slurm-<jid>.out。**按本作业号定位**:续算在同一目录重投,旧 attempt 的 .o<旧号>
    仍在,通配 *.o* + tail -1 会按字母序读到旧作业(o100 < o99),把新一轮误分类;
    故用 *.o<本号> / slurm-<本号>.out 精确锁定,再以每次覆盖的 vasp.out/log 兜底。
    → (exit_code|None, tail)。
    """
    if not remote:
        return None, ''
    num = str(job_id).split('.')[0].strip()
    if num:
        targets = f'*.o{num} slurm-{num}.out vasp.out log'
    else:                                        # 无作业号 → 退回宽通配(单作业目录仍准)
        targets = '*.o* slurm-*.out vasp.out log'
    out, _ = run_cmd(
        client,
        f"cd {shlex.quote(remote)} && "
        f"grep -h 'EXIT:' {targets} 2>/dev/null | tail -1; "
        f"echo '___VCSLOG___'; "
        f"tail -n 20 {targets} 2>/dev/null")
    marker, _sep, tail = out.partition('___VCSLOG___')
    mm = re.search(r'EXIT:\s*(-?\d+)', marker)
    exit_code = int(mm.group(1)) if mm else None
    return exit_code, tail


def _first_int(text: str) -> int:
    for tok in text.split():
        try:
            return int(tok)
        except ValueError:
            continue
    return 0


# ── 结果回收(S3):把关键输出拉回本地作业目录 ─────────────────────────────────
# FETCH_FILES 保留为兼容常量（手动指定/旧调用仍取传统三件套）；默认下载现在由
# fetch_files_for_manifest 按任务动态决定，电子结构产物不再静默留在集群。
FETCH_FILES = ('CONTCAR', 'OSZICAR', 'OUTCAR')
_VASP_FETCH_BY_TASK = {
    'static': ('vasprun.xml', 'CHGCAR'),
    'dos_pdos': ('DOSCAR', 'vasprun.xml'),
    'bands': ('EIGENVAL', 'PROCAR', 'vasprun.xml'),
    'bader': ('CHGCAR', 'AECCAR0', 'AECCAR2', 'ACF.dat'),
    'chgdiff': ('CHGCAR',),
    'elf': ('ELFCAR',),
    'workfunction': ('LOCPOT',),
    'aimd': ('XDATCAR',),
    'freq': ('vasprun.xml',),
}
_FETCH_EVIDENCE_KEYS = (
    'fetched', 'fetched_missing', 'missing', 'fetched_at', 'fetched_job_id',
    'fetched_remote_dir', 'fetch_requested',
)


def _safe_manifest_names(raw) -> list[str]:
    """Return de-duplicated single-segment names from a user-editable manifest."""
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for item in raw:
        name = str(item or '').strip()
        if (not name or os.path.isabs(name) or os.path.basename(name) != name
                or '/' in name or '\\' in name
                or name in (manifest_mod.MANIFEST_NAME, SCRIPT_NAME)):
            continue
        if name not in out:
            out.append(name)
    return out


def _gaussian_checkpoint_from_input(job_dir: str | None, input_files) -> str | None:
    """Return a safe local ``%chk`` basename, or ``None`` when disabled/unknown."""
    if not job_dir:
        return None
    for name in input_files or ():
        if not str(name).lower().endswith(('.gjf', '.com')):
            continue
        try:
            with open(os.path.join(job_dir, str(name)), 'r', encoding='utf-8',
                      errors='replace') as handle:
                match = re.search(r'^\s*%chk\s*=\s*(\S+)\s*$', handle.read(),
                                  flags=re.MULTILINE | re.IGNORECASE)
        except OSError:
            return None
        if not match:
            return None
        value = match.group(1).strip()
        if os.path.basename(value) != value or '/' in value or '\\' in value:
            return None
        return value
    return None


def fetch_files_for_manifest(m: dict | None, *, job_dir: str | None = None) -> tuple[str, ...]:
    """Resolve the default result bundle for one manifest.

    VASP tasks receive their scientifically relevant parser inputs.  For
    Gaussian/CP2K/CASTEP quick jobs, users may declare ``inputs.output_files``;
    otherwise the main output filename is derived from the primary input and
    the documented command convention.  Every name is a safe single path
    segment because it is later joined to the SSH work directory.
    """
    m = m or {}
    inputs = m.get('inputs') or {}
    if not isinstance(inputs, dict):
        inputs = {}
    engine = _job_engine(m)
    explicit = _safe_manifest_names(inputs.get('output_files'))
    if engine == 'vasp':
        if explicit:
            return tuple(explicit)
        task = _canonical_vasp_task(m)
        names = list(FETCH_FILES) + list(_VASP_FETCH_BY_TASK.get(task, ()))
    else:
        declared = _safe_manifest_names(inputs.get('files'))
        contract = get_run_contract(engine)
        derived = list(contract.result_files(declared, task=_engine_task(m, job_dir)))
        # Explicit output_files remain authoritative for imported/custom jobs.
        # engine_generate manifests use the shared contract so task-specific
        # CASTEP .phonon/.geom and restart evidence are not silently omitted.
        names = list(explicit)
        if not explicit or inputs.get('generator') == 'engine_generate':
            names.extend(derived)
        if engine == 'gaussian' and (not explicit or inputs.get('generator') == 'engine_generate'):
            checkpoint = _gaussian_checkpoint_from_input(job_dir, declared)
            names = [name for name in names if not name.lower().endswith('.chk')]
            if checkpoint:
                names.append(checkpoint)
    return tuple(dict.fromkeys(names))


def fetch_files_for_job(job_dir: str) -> tuple[str, ...]:
    """Load ``job.yaml`` and return that job's default download bundle."""
    m = manifest_mod.load_manifest(job_dir)
    if m is None:
        raise ValueError('作业目录缺 job.yaml,无法确定结果文件')
    return fetch_files_for_manifest(m, job_dir=job_dir)


def _clear_fetch_evidence(m: dict) -> None:
    """新一轮重投成功后清掉上一轮的下载完成证据（本地输出文件原样保留）。

    同一作业目录续算会复用 ``remote_dir``，本地也会保留旧 OUTCAR/OSZICAR 供审计；
    因此不能以文件仍存在或旧 ``fetched_at`` 判断新一轮结果已经回收。调用方只在取得
    新调度器作业号后调用本函数，重投失败时不会误清上一轮证据。
    """
    results = m.setdefault('results', {})
    for key in _FETCH_EVIDENCE_KEYS:
        results.pop(key, None)


def _atomic_sftp_get(sftp, remote_path: str, local_path: str) -> None:
    """Download beside the destination and atomically replace it on success.

    Paramiko writes directly to its target.  A dropped connection would
    otherwise truncate an older valid OUTCAR/vasprun.xml and leave no recovery
    path.  The temporary file lives in the same directory so ``os.replace`` is
    atomic on the destination filesystem.
    """
    parent = os.path.dirname(os.path.abspath(local_path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f'.{os.path.basename(local_path)}.vcstudio-', suffix='.part', dir=parent)
    os.close(fd)
    try:
        sftp.get(remote_path, tmp_path)
        os.replace(tmp_path, local_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def fetch_results(client, sftp, job_dir: str, files=None, *, profile=None):
    """下载远程输出文件到本地作业目录。返回 (fetched, missing) 两个文件名列表。

    - 不覆盖输入语义:CONTCAR/OSZICAR/OUTCAR 与四件套不重名,直接落在 job_dir。
    - 单个文件缺失(如未跑出 CONTCAR)记入 missing,不中断其余下载。
    - 下载记录绑定当前 scheduler_job_id；同目录续算时旧本地文件不能冒充本轮结果。
    """
    m = manifest_mod.load_manifest(job_dir)
    if m is None:
        raise ValueError('作业目录缺 job.yaml,无法定位远程目录')
    if profile is not None:
        assert_profile_binding(profile, job_dir, '拉回结果', manifest=m)
    remote = m.get('remote_dir')
    if not remote:
        raise ValueError('该作业尚未提交过(manifest 无 remote_dir)')
    if files is None:
        requested = list(fetch_files_for_manifest(m, job_dir=job_dir))
    else:
        raw_requested = list(files)
        requested = _safe_manifest_names(raw_requested)
        raw_clean = list(dict.fromkeys(str(name or '').strip() for name in raw_requested
                                       if str(name or '').strip()))
        if requested != raw_clean:
            raise ValueError('结果文件名非法：只允许远程作业目录内的单个文件名')
    if _is_neb(m):
        fetched, missing = _fetch_neb_results(client, sftp, job_dir, remote, requested)
    else:
        fetched, missing = [], []
        for fname in requested:
            try:
                _atomic_sftp_get(
                    sftp, posixpath.join(remote, fname), os.path.join(job_dir, fname))
                fetched.append(fname)
            except (IOError, OSError):
                missing.append(fname)
    res = m.setdefault('results', {})
    # ``missing`` 是旧版本曾用过的别名；本轮统一写 ``fetched_missing``，并清掉旧值，
    # 避免界面把上一轮缺失列表与当前下载结果混在一起。
    res.pop('missing', None)
    res['fetched'] = fetched
    res['fetched_missing'] = missing
    res['fetched_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    res['fetched_job_id'] = str(m.get('scheduler_job_id') or '')
    res['fetched_remote_dir'] = str(remote)
    res['fetch_requested'] = requested
    manifest_mod.save_manifest(job_dir, m)
    return fetched, missing


def _fetch_neb_results(client, sftp, job_dir: str, remote: str, files):
    """NEB 结果回收:逐 image 子目录(00..N+1)下载 files 到本地同名子目录。

    → (fetched, missing),名字带 image 前缀(如 '01/OSZICAR')。端点(00/N+1)常只有
    OUTCAR(读取初/末态能量),缺项记 missing 不中断其余。
    """
    fetched, missing = [], []
    for fr in _neb_local_frames(job_dir):
        local_sub = os.path.join(job_dir, fr)
        os.makedirs(local_sub, exist_ok=True)
        for fname in files:
            tag = f'{fr}/{fname}'
            try:
                _atomic_sftp_get(
                    sftp, posixpath.join(remote, fr, fname), os.path.join(local_sub, fname))
                fetched.append(tag)
            except (IOError, OSError):
                missing.append(tag)
    return fetched, missing


# ── 有界恢复:CONTCAR 续算(对齐论文 bounded recovery;人工触发,冻结 INCAR) ──────
CONTINUE_MAX_ROUNDS = 3
# 续算前几何健全阈值(Å):周期最小原子间距低于此值判原子重叠(< 最短化学键 H-H 0.74)。
MIN_INTERATOMIC_OK = 0.7


def _read_remote_text(client, path: str) -> str:
    out, _ = run_cmd(client, f'cat {shlex.quote(path)} 2>/dev/null')
    return out


def _contcar_min_distance(text: str):
    """CONTCAR 文本 → 周期最小原子间距(Å);解析失败 → None(不因此拦续算,valid_poscar 已把关)。"""
    try:
        from vcstudio.generate.slab_builder import min_interatomic_distance
        return min_interatomic_distance(text)
    except Exception:                                    # noqa: BLE001
        return None


def _restart_cleanup_command(m: dict, job_dir: str | None = None) -> str:
    """Return safe wavefunction-history cleanup for one VASP restart.

    Non-self-consistent bands (ICHARG=11) hard-require the parent CHGCAR, so
    deleting it turns every otherwise valid restart into an immediate crash.
    Other VASP tasks retain the established clean-charge restart policy.
    """
    needs_chgcar = (_canonical_vasp_task(m) == 'bands'
                    or (job_dir is not None and _local_vasp_icharg(job_dir) in (1, 11)))
    return ('rm -f WAVECAR' if needs_chgcar
            else 'rm -f WAVECAR CHGCAR')


def continue_from_contcar(client, profile, job_dir: str,
                          max_rounds: int = CONTINUE_MAX_ROUNDS) -> dict:
    """把一个可续算作业从 CONTCAR 接着跑(cp CONTCAR POSCAR + 冻结 INCAR 重投同一脚本)。

    有界恢复(论文核心 + 交接三不变式):
    - 只对 diagnose 标 restartable 的分类(未收敛/墙钟/ZBRENT)出手,否则拒绝;
    - CONTCAR 必须通过 valid_poscar 校验(防拿半个结构续出垃圾);
    - **INCAR 逐字冻结**(方法学主权,无可比性护栏前的安全默认);
    - continue_rounds 硬上限(默认 3),到顶停机交人工(防死循环);
    - 清远端 WAVECAR/CHGCAR 去混合历史；bands 因 ICHARG=11 必须保留 CHGCAR；
      记 prev_job_id 溯源。
    失败抛 ValueError/RuntimeError(中文)。成功返回更新后的 manifest(state=SUBMITTED)。
    """
    m = manifest_mod.load_manifest(job_dir)
    if m is None:
        raise ValueError('作业目录缺 job.yaml,无法续算')
    if _job_engine(m) != 'vasp':
        contract = get_run_contract(_job_engine(m))
        raise ValueError(
            f'{_ENGINE_LABELS.get(_job_engine(m), _job_engine(m))} 不能走 VASP CONTCAR 续算；'
            f'{contract.restart_note}')
    assert_profile_binding(profile, job_dir, '续算', manifest=m)
    if _is_neb(m):
        raise ValueError(
            'NEB 不能使用通用 CONTCAR 续算：NEB 根目录没有单一 CONTCAR，'
            '必须保留各 image 并使用 NEB 专用重提流程；本次未修改远端文件')
    # 状态门(严重 bug 防护):仍在队列/运行中的作业绝不续算——否则会往活作业目录里
    # cp CONTCAR POSCAR + 重投第二个实例,两个 VASP 同写 OUTCAR 冲垮结果,且旧作业号被
    # 覆盖成孤儿。restartable 诊断是上一轮终态留下的,重投后必须消费掉(见函数尾)。
    if m.get('state') in ('UPLOADED', 'SUBMITTED', 'QUEUED', 'RUNNING'):
        raise ValueError(f"该作业仍在队列/运行中(状态 {m['state']}),不能续算;请先查询状态确认已结束")
    diag = (m.get('results') or {}).get('diagnosis') or {}
    if not diag.get('restartable'):
        raise ValueError(
            f"该作业不可自动续算(分类 {diag.get('failure_class', '?')});"
            f'仅 未收敛/墙钟/ZBRENT 等可从 CONTCAR 续算,硬崩/缺输出需人工')
    rounds = int((m.get('results') or {}).get('continue_rounds', 0))
    if rounds >= max_rounds:
        raise RuntimeError(f'已续算 {rounds} 次达上限 {max_rounds},停机交人工(防死循环)')
    remote = m.get('remote_dir')
    if not remote:
        raise ValueError('该作业无 remote_dir(未提交过),无法续算')

    contcar = _read_remote_text(client, posixpath.join(remote, 'CONTCAR'))
    if not diagnose.valid_poscar(contcar):
        raise RuntimeError('远端 CONTCAR 缺失或不完整,不能续算(防半个结构续出垃圾),请人工检查')

    # 几何健全:CONTCAR 原子重叠(周期最小间距 < 阈值)→ 不续算,转人工。病态几何续算只会
    # 反复崩(ZPOTRF/发散),盲目重投浪费机时;显式转 NEEDS_HUMAN 让人先修结构。
    min_d = _contcar_min_distance(contcar)
    if min_d is not None and min_d < MIN_INTERATOMIC_OK:
        msg = f'CONTCAR 存在原子重叠(最小间距 {min_d:.2f} Å),疑似几何病态,请人工检查'
        manifest_mod.set_state(m, 'NEEDS_HUMAN', note=msg)
        manifest_mod.save_manifest(job_dir, m)
        raise RuntimeError(msg)

    # 续算沉降基线:重投前记下上一轮 OUTCAR 的 mtime(此刻新作业尚未启动,仍是旧文件)
    _o0, _z0, _base_outcar_mtime = _stat_outcar_full(client, remote)

    # 本地也留证:备份旧 POSCAR,用 CONTCAR 覆盖(保持本地目录与远端一致)
    local_poscar = os.path.join(job_dir, 'POSCAR')
    if os.path.isfile(local_poscar):
        shutil.copyfile(local_poscar, f'{local_poscar}.bak{rounds + 1}')
    with open(local_poscar, 'w', encoding='utf-8', newline='') as f:
        f.write(contcar)

    # 远端:CONTCAR→POSCAR + 清混合历史,再重投同一脚本(INCAR 不动)
    cleanup = _restart_cleanup_command(m, job_dir)
    run_cmd(client, f'cd {shlex.quote(remote)} && cp CONTCAR POSCAR && {cleanup}',
            check=True)
    dialect = get_dialect(profile.scheduler)
    out, err = run_cmd(client, dialect.submit_cmd(
        posixpath.join(remote, SCRIPT_NAME), getattr(profile, 'scheduler_bin', '')))
    job_id = dialect.parse_job_id(out)
    if not job_id:
        raise RuntimeError(f'续算重投失败,{dialect.name} 返回:{(out or err).strip()[:300]}')

    prev = m.get('scheduler_job_id')
    m['scheduler_job_id'] = job_id
    _clear_fetch_evidence(m)
    # 消费掉上一轮的终态诊断:新作业尚未诊断,restartable=True 不能被下一次误用
    m.setdefault('results', {}).pop('diagnosis', None)
    m.setdefault('results', {})['continue_rounds'] = rounds + 1
    _set_continue_baseline(m, _base_outcar_mtime)  # 沉降护栏基线:防新作业未启动前误读旧 OUTCAR
    m.setdefault('attempts', []).append({
        'n': len(m.get('attempts') or []) + 1,
        'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'result': 'continued',
        'action': 'contcar_restart',
        'prev_job_id': prev,
        'job_id': job_id,
        'round': rounds + 1,
    })
    manifest_mod.set_state(m, 'SUBMITTED',
                           note=f'CONTCAR 续算 第{rounds + 1}轮(prev {prev} → {job_id},INCAR 冻结)')
    manifest_mod.save_manifest(job_dir, m)
    return m


# ── 改参续算(S6 修复引擎):白名单键受控修改 INCAR 后重投 ─────────────────────
# 只放非方法学"数值旋钮":收敛算法/展宽/混合/步长/迭代上限/能带数。ENCUT/泛函/
# IVDW/ISPIN/赝势这类**决定可比性**的键绝不进白名单(方法学主权不破)。
INCAR_TUNE_WHITELIST = frozenset({
    'ALGO', 'ISMEAR', 'SIGMA', 'AMIX', 'BMIX', 'AMIX_MAG', 'BMIX_MAG', 'IMIX',
    'POTIM', 'IBRION', 'NELM', 'NELMIN', 'NSW', 'NBANDS', 'LREAL', 'ISYM',
    'SYMPREC', 'AMIN', 'MAXMIX', 'NCORE', 'KPAR', 'EDIFF',
})
_TUNE_BANNER = '# --- vcstudio 改参续算 第{round}轮 {at} ---'


def continue_with_incar_changes(client, sftp, profile, job_dir: str,
                                changes: dict,
                                max_rounds: int = CONTINUE_MAX_ROUNDS,
                                restart_from_contcar: bool = True) -> dict:
    """诊断建议 → 受控改参重投:白名单键追加覆盖到 INCAR 文末(原文一字不删),
    可选 CONTCAR→POSCAR,清 WAVECAR/CHGCAR（bands 保留 CHGCAR）,重投同一脚本。

    与冻结续算共用状态门/轮次上限;差异:
    - changes 仅允许 INCAR_TUNE_WHITELIST 键(违例 ValueError 点名,绝不静默丢弃);
    - 放宽 restartable 限制:SCF_SLOSHING/EDDDAV 等 NEEDS_HUMAN 类正是改参对象,
      故只要求终态(不在队/不在跑),不要求 diagnose.restartable;
    - INCAR 修改以"追加覆盖块"落地(VASP 取同键末次出现值;原文保留可审计),
      同步上传远端;attempts 记录完整 changes。
    """
    if not changes:
        raise ValueError('未提供任何 INCAR 修改项')
    bad = [k for k in changes if str(k).upper() not in INCAR_TUNE_WHITELIST]
    if bad:
        raise ValueError(
            f'以下键不在改参白名单,拒绝修改:{", ".join(bad)}。'
            f'白名单(非方法学旋钮):{", ".join(sorted(INCAR_TUNE_WHITELIST))}')
    m = manifest_mod.load_manifest(job_dir)
    if m is None:
        raise ValueError('作业目录缺 job.yaml,无法改参续算')
    if _job_engine(m) != 'vasp':
        contract = get_run_contract(_job_engine(m))
        raise ValueError(
            f'{_ENGINE_LABELS.get(_job_engine(m), _job_engine(m))} 不能使用 INCAR 改参续算；'
            f'{contract.restart_note}')
    assert_profile_binding(profile, job_dir, '改参续算', manifest=m)
    if _is_neb(m):
        raise ValueError(
            'NEB 不能使用通用改参/CONTCAR 续算；请使用 NEB 专用 image 级重提流程')
    if m.get('state') in ('UPLOADED', 'SUBMITTED', 'QUEUED', 'RUNNING'):
        raise ValueError(f"该作业仍在队列/运行中(状态 {m['state']}),不能改参重投")
    rounds = int((m.get('results') or {}).get('continue_rounds', 0))
    if rounds >= max_rounds:
        raise RuntimeError(f'已续算 {rounds} 次达上限 {max_rounds},停机交人工(防死循环)')
    remote = m.get('remote_dir')
    if not remote:
        raise ValueError('该作业无 remote_dir(未提交过),无法改参续算')

    # 1) 本地 INCAR:备份 + 追加覆盖块(原文保留)
    local_incar = os.path.join(job_dir, 'INCAR')
    if not os.path.isfile(local_incar):
        raise ValueError('本地作业目录缺 INCAR')
    with open(local_incar, 'r', encoding='utf-8', errors='replace') as f:
        incar_text = f.read()
    shutil.copyfile(local_incar, f'{local_incar}.bak{rounds + 1}')
    at = time.strftime('%Y-%m-%dT%H:%M:%S')
    block = '\n' + _TUNE_BANNER.format(round=rounds + 1, at=at) + '\n'
    block += ''.join(f'{str(k).upper()} = {v}\n' for k, v in changes.items())
    new_text = (incar_text if incar_text.endswith('\n') else incar_text + '\n') + block
    with open(local_incar, 'w', encoding='utf-8', newline='') as f:
        f.write(new_text)

    # 2) 可选 CONTCAR 续结构(结构没跑几步/硬崩时也允许保持原 POSCAR 重跑)
    if restart_from_contcar:
        contcar = _read_remote_text(client, posixpath.join(remote, 'CONTCAR'))
        if diagnose.valid_poscar(contcar):
            local_poscar = os.path.join(job_dir, 'POSCAR')
            if os.path.isfile(local_poscar):
                shutil.copyfile(local_poscar, f'{local_poscar}.bak{rounds + 1}')
            with open(local_poscar, 'w', encoding='utf-8', newline='') as f:
                f.write(contcar)
            run_cmd(client, f'cd {shlex.quote(remote)} && cp CONTCAR POSCAR', check=True)

    # 续算沉降基线:重投前记下上一轮 OUTCAR 的 mtime(此刻新作业尚未启动,仍是旧文件)
    _o0, _z0, _base_outcar_mtime = _stat_outcar_full(client, remote)

    # 3) 上传新 INCAR + 清混合历史,重投同一脚本
    sftp.put(local_incar, posixpath.join(remote, 'INCAR'))
    run_cmd(client, f'cd {shlex.quote(remote)} && {_restart_cleanup_command(m, job_dir)}',
            check=True)
    dialect = get_dialect(profile.scheduler)
    out, err = run_cmd(client, dialect.submit_cmd(
        posixpath.join(remote, SCRIPT_NAME), getattr(profile, 'scheduler_bin', '')))
    job_id = dialect.parse_job_id(out)
    if not job_id:
        raise RuntimeError(f'改参重投失败,{dialect.name} 返回:{(out or err).strip()[:300]}')

    prev = m.get('scheduler_job_id')
    m['scheduler_job_id'] = job_id
    _clear_fetch_evidence(m)
    m.setdefault('results', {}).pop('diagnosis', None)
    m.setdefault('results', {})['continue_rounds'] = rounds + 1
    _set_continue_baseline(m, _base_outcar_mtime)  # 沉降护栏基线:防新作业未启动前误读旧 OUTCAR
    m.setdefault('attempts', []).append({
        'n': len(m.get('attempts') or []) + 1,
        'at': at,
        'result': 'continued',
        'action': 'incar_tuned_restart',
        'incar_changes': {str(k).upper(): str(v) for k, v in changes.items()},
        'from_contcar': bool(restart_from_contcar),
        'prev_job_id': prev,
        'job_id': job_id,
        'round': rounds + 1,
    })
    manifest_mod.set_state(
        m, 'SUBMITTED',
        note=f'改参续算 第{rounds + 1}轮({", ".join(f"{str(k).upper()}={v}" for k, v in changes.items())};'
             f'prev {prev} → {job_id})')
    manifest_mod.save_manifest(job_dir, m)
    return m


def _read_oszicar(client, remote_dir: str):
    """OSZICAR 尾部一次取数 → (E0|None, tail_text)。

    tail -150 覆盖最后一个离子步的完整 SCF 块(供 diagnose 震荡扫描)且必含末行
    E0(取最后一个匹配)。拿不到 E0 → None(绝不编数)。"""
    if not remote_dir:
        return None, ''
    out, _ = run_cmd(client, f'tail -n 150 {shlex.quote(remote_dir + "/OSZICAR")} 2>/dev/null')
    m = None
    for m in _E0_RE.finditer(out):
        pass                                   # 取最后一个匹配
    if m is None:
        return None, out
    try:
        return float(m.group(1)), out
    except ValueError:
        return None, out


def _read_e0(client, remote_dir: str):
    """兼容入口:只要 E0。"""
    return _read_oszicar(client, remote_dir)[0]
