"""任意输入文件批量提交(对标 starpivot-DFT ③生成输入 + ④提交):把用户手里已有的
Gaussian/CP2K/CASTEP/VASP 输入文件,逐个建成轻量作业目录(拷入 + 落 job.yaml),
之后即可走现有 submitter 提交链投递。

设计原则(与全局不变式一致):
- **显式识别不猜**:detect_engine 只按已知扩展名/文件名判定,认不出返回 None(交
  build_quick_jobs 记 skipped,绝不静默当某引擎处理)。
- **溯源**:每个作业 job.yaml 记 inputs.engine + 拷入文件 + 各文件 sha256(来源可答)。
- **纯本地**:本模块只在本地建目录/拷文件/写 manifest,不做任何远程动作(那是 submitter)。

本模块不 import 任何 GUI 框架;可离线单测。中文注释允许,英文标识符。
"""
from __future__ import annotations

import math
import os
import shutil
import tempfile
import time

from vcstudio.generate.incar_builder import parse_incar
from vcstudio.generate.job_builder import _infer_task_type, build_job_dir
from vcstudio.generate.poscar import parse_poscar_species, read_poscar
from vcstudio.generate.potcar import max_enmax
from vcstudio.shared import manifest as manifest_mod

# 引擎识别:扩展名(小写)→ 引擎名。VASP 无独有扩展名,靠特征文件名/目录判定。
_EXT_ENGINE = {
    '.gjf': 'gaussian', '.com': 'gaussian',
    '.inp': 'cp2k',
    '.cell': 'castep', '.param': 'castep',
}
# VASP 特征文件名(单文件命中即判 vasp;目录内含其一亦然)。
_VASP_MARKERS = ('INCAR', 'POSCAR', 'CONTCAR')
# VASP 目录拷贝的标准输入四件套(缺一不可提交)。
_VASP_INPUTS = ('INCAR', 'POSCAR', 'KPOINTS', 'POTCAR')

# 每引擎的集群运行命令模板提示(仅文案；真正命令从
# ClusterProfile.engine_commands 选择，VASP 额外兼容旧 vasp_cmd)。
_SUBMIT_HINTS = {
    'gaussian': 'g16 < {input} > {stem}.log   # 或 g09;Gaussian 本体与许可用户自备',
    'cp2k': 'cp2k.psmp -i {input} -o {stem}.out   # 或 cp2k.popt',
    'castep': 'mpirun -np {cores} castep.mpi {stem}   # 需同名 .cell 与 .param',
    'vasp': 'mpirun -np {cores} vasp_std > vasp.log 2>&1   # VASP 本体与 POTCAR 用户自备',
}


def detect_engine(path: str) -> str | None:
    """输入文件(或 VASP 作业目录)→ 引擎名;认不出 → None(绝不臆断)。

    .gjf/.com→gaussian、.inp→cp2k、.cell/.param→castep、INCAR/POSCAR(文件名或含之目录)→vasp。
    """
    if os.path.isdir(path):
        if any(os.path.isfile(os.path.join(path, f)) for f in _VASP_MARKERS):
            return 'vasp'
        return None
    base = os.path.basename(str(path).rstrip('/\\'))
    if base in _VASP_MARKERS:
        return 'vasp'
    return _EXT_ENGINE.get(os.path.splitext(base)[1].lower())


def submit_hint(engine: str) -> str:
    """引擎 → 集群运行命令模板提示文案(未登记引擎 → 空串)。"""
    return _SUBMIT_HINTS.get(str(engine).lower(), '')


def _sanitize(name: str) -> str:
    """作业名 → 安全目录段(保留 [A-Za-z0-9_.-],其余转下划线;空 → 'job')。"""
    out = ''.join(c if (c.isalnum() or c in '_.-') else '_' for c in str(name or ''))
    return out.strip('_') or 'job'


def _gjf_job_name(path: str) -> str:
    """.gjf/.com → 作业名:优先 %chk 名,退而取标题行(首个非 Link0(%)/非路线(#)非空行)。"""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            raw = [ln.strip() for ln in fh]
    except OSError:
        return ''
    for ln in raw:
        if ln.lower().startswith('%chk='):
            val = os.path.splitext(os.path.basename(ln.split('=', 1)[1].strip()))[0]
            if val:
                return val
    for ln in raw:
        if ln and not ln.startswith('%') and not ln.startswith('#'):
            return ln
    return ''


def _job_name_for(path: str, engine: str) -> str:
    """作业名:.gjf 解析 %chk/标题;VASP 目录取目录名;其余取文件名主干。"""
    if engine == 'gaussian':
        name = _gjf_job_name(path)
        if name:
            return name
    if os.path.isdir(path):
        return os.path.basename(os.path.normpath(path))
    return os.path.splitext(os.path.basename(path))[0]


def _shared_incar_value(shared_incar: str) -> str | None:
    """共享 INCAR 参数 -> 可交给 job_builder 的文件路径/原文;无效路径 -> None。

    GUI 传入的是文件路径;保留原文入口方便离线/脚本调用。一个不存在且看起来像
    路径的字符串不能被误当 INCAR 内容,否则用户会得到难以理解的解析结果。
    """
    value = str(shared_incar or '').strip()
    if not value:
        return None
    if os.path.isfile(value):
        return os.path.abspath(value)
    if '\n' in value or '=' in value:
        return value
    return None


def _vasp_structure(path: str) -> str | None:
    """VASP 候选目录 -> 结构文件(POSCAR 优先,否则 CONTCAR)。"""
    for name in ('POSCAR', 'CONTCAR'):
        candidate = os.path.join(path, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def _is_vasp_candidate(path: str) -> bool:
    """是否为计算目录，而不是仅存放共享 INCAR 的批次根目录。

    有 POSCAR/CONTCAR 即为结构候选；没有结构时，仅 INCAR/KPOINTS/POTCAR 三件
    同时存在才作为“缺 POSCAR”的作业提示。这样常见的 ``root/INCAR + root/*/POSCAR``
    布局不会把 root 本身多报成一个失败作业。
    """
    if _vasp_structure(path):
        return True
    return all(os.path.isfile(os.path.join(path, name))
               for name in ('INCAR', 'KPOINTS', 'POTCAR'))


def _scan_vasp_item(path: str, shared_incar: str | None) -> dict:
    """单个 VASP 候选目录 -> UI 可直接呈现的预检项。"""
    structure = _vasp_structure(path)
    present = [name for name in _VASP_INPUTS
               if os.path.isfile(os.path.join(path, name))]
    missing = [name for name in _VASP_INPUTS if name not in present]
    if not missing:
        from vcstudio.project.result_import import validate_vasp_quartet
        issues = validate_vasp_quartet(path)
        if issues:
            status, mode = 'invalid', 'blocked'
            message = '四件套未通过输入检查：' + '；'.join(issues)
        else:
            status, mode, message = 'ready', 'copy', '四件套完整且已通过检查，可直接建作业'
    elif structure and shared_incar:
        status, mode = 'generatable', 'generate'
        src = os.path.basename(structure)
        message = f'将用 {src} + 共享 INCAR 自动生成标准四件套'
    else:
        status, mode = 'incomplete', 'blocked'
        if structure:
            message = '已有结构；请选择一份共享 INCAR 后即可自动补齐'
        else:
            message = '缺少 POSCAR/CONTCAR，无法生成作业'
    return {
        'path': os.path.abspath(path),
        'name': os.path.basename(os.path.normpath(path)) or 'vasp_job',
        'engine': 'vasp',
        'kind': 'vasp_dir',
        'status': status,
        'mode': mode,
        'can_build': status in ('ready', 'generatable'),
        'can_generate': status == 'generatable',
        'structure': os.path.abspath(structure) if structure else '',
        'present': present,
        'missing': missing,
        'message': message,
    }


def scan_inputs(paths: list, *, shared_incar: str = '') -> dict:
    """预检输入路径，并递归发现父目录下的 VASP 作业。

    - 目录会递归扫描；不跟随目录符号链接，真实路径/目录 inode 双重去重。
    - 完整 VASP 四件套标为 ``ready``。
    - 只有 POSCAR/CONTCAR 时，提供共享 INCAR 后标为 ``generatable``。
    - 其它引擎继续保留原有单文件入口。

    返回 ``{'ok','items','summary','error'}``;每个 item 都含 ``status``、
    ``missing``、``can_build`` 与面向用户的 ``message``，可直接做 UI 预检。
    """
    result = {'ok': True, 'items': [], 'summary': {}, 'error': None}
    shared = _shared_incar_value(shared_incar)
    seen_sources: set[str] = set()
    seen_dirs: set[tuple[int, int]] = set()
    duplicate_count = 0

    def add_item(item: dict, key: str) -> None:
        nonlocal duplicate_count
        canonical = os.path.normcase(os.path.realpath(key))
        if canonical in seen_sources:
            duplicate_count += 1
            return
        seen_sources.add(canonical)
        result['items'].append(item)

    def add_vasp_dir(directory: str) -> None:
        nonlocal duplicate_count
        try:
            stat = os.stat(directory, follow_symlinks=False)
            inode = (int(stat.st_dev), int(stat.st_ino))
        except OSError:
            inode = (-1, hash(os.path.realpath(directory)))
        if inode in seen_dirs:
            duplicate_count += 1
            return
        seen_dirs.add(inode)
        add_item(_scan_vasp_item(directory, shared), directory)

    for raw in paths or []:
        path = os.path.abspath(str(raw).strip())
        if not str(raw).strip():
            continue
        if not os.path.exists(path):
            add_item({
                'path': path, 'name': os.path.basename(path), 'engine': '',
                'kind': 'missing', 'status': 'missing', 'mode': 'blocked',
                'can_build': False, 'can_generate': False, 'structure': '',
                'present': [], 'missing': [], 'message': '输入不存在',
            }, path)
            continue
        if os.path.isdir(path):
            found = False
            walk_errors: list[str] = []

            def onerror(exc):
                walk_errors.append(str(exc))

            for current, dirnames, _filenames in os.walk(
                    path, topdown=True, followlinks=False, onerror=onerror):
                # 不进入目录符号链接，避免环与同一计算的别名重复。
                dirnames[:] = sorted(
                    d for d in dirnames
                    if not os.path.islink(os.path.join(current, d)))
                if _is_vasp_candidate(current):
                    found = True
                    add_vasp_dir(current)
            if not found:
                msg = '目录内未发现 VASP 输入或结构文件'
                if walk_errors:
                    msg += f'；另有 {len(walk_errors)} 个子目录无法读取'
                add_item({
                    'path': path, 'name': os.path.basename(os.path.normpath(path)),
                    'engine': '', 'kind': 'directory', 'status': 'unknown',
                    'mode': 'blocked', 'can_build': False, 'can_generate': False,
                    'structure': '', 'present': [], 'missing': [], 'message': msg,
                }, path)
            continue

        engine = detect_engine(path)
        base = os.path.basename(path)
        if engine == 'vasp':
            # 单独选择 POSCAR/CONTCAR/INCAR 时也检查其同级目录，避免四件套被拆散。
            add_vasp_dir(os.path.dirname(path))
        elif engine:
            add_item({
                'path': path, 'name': _job_name_for(path, engine), 'engine': engine,
                'kind': 'engine_file', 'status': 'ready', 'mode': 'copy',
                'can_build': True, 'can_generate': False, 'structure': '',
                'present': [base], 'missing': [], 'message': '输入文件可直接建作业',
            }, path)
        else:
            add_item({
                'path': path, 'name': os.path.splitext(base)[0], 'engine': '',
                'kind': 'unknown_file', 'status': 'unknown', 'mode': 'blocked',
                'can_build': False, 'can_generate': False, 'structure': '',
                'present': [base], 'missing': [], 'message': '无法识别引擎或输入类型',
            }, path)

    items = result['items']
    result['summary'] = {
        'total': len(items),
        'ready': sum(i['status'] == 'ready' for i in items),
        'generatable': sum(i['status'] == 'generatable' for i in items),
        'blocked': sum(not i['can_build'] for i in items),
        'duplicates': duplicate_count,
        'shared_incar_valid': bool(shared),
    }
    return result


def _unique_dir_name(base: str, out_root: str, used: set) -> str:
    """base 撞名(本轮 used 或磁盘已存在)→ 追加 _2/_3… 直至唯一。"""
    cand = base
    n = 1
    while cand in used or os.path.exists(os.path.join(out_root, cand)):
        n += 1
        cand = f'{base}_{n}'
    return cand


def _inside_directory(path: str, parent: str) -> bool:
    """Return whether *path* is the same as or below *parent*.

    Both sides use their real paths so a symlink alias cannot bypass the
    source-tree write guard.  Windows paths on different drives are never in
    an ancestor relationship (``commonpath`` raises ``ValueError`` there).
    """
    try:
        child = os.path.normcase(os.path.realpath(path))
        root = os.path.normcase(os.path.realpath(parent))
        return os.path.commonpath([child, root]) == root
    except ValueError:
        return False


def _copy_inputs(path: str, job_dir: str) -> list:
    """把输入拷入作业目录:VASP 目录拷标准输入集,单文件拷该文件。返回拷入的文件名列表。"""
    copied = []
    if os.path.isdir(path):
        for fn in _VASP_INPUTS:
            src = os.path.join(path, fn)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(job_dir, fn))
                copied.append(fn)
    else:
        fn = os.path.basename(path)
        shutil.copy2(path, os.path.join(job_dir, fn))
        copied.append(fn)
    return copied


def _manifest_task_type(engine: str, job_dir: str) -> str:
    """输入引擎/已拷贝作业目录 → manifest task_type。

    VASP 的完成判定依赖此字段:static 查电子收敛标志,relax/freq 查离子
    收敛标志。其它引擎继续使用原有 quick 类型,保持兼容。
    """
    if engine != 'vasp':
        return 'quick'
    incar_path = os.path.join(job_dir, 'INCAR')
    try:
        with open(incar_path, 'r', encoding='utf-8', errors='replace') as fh:
            incar = parse_incar(fh.read())
    except OSError:
        # 单文件旧入口可能只选 POSCAR/CONTCAR;无 INCAR 时按 VASP 默认
        # NSW=0 处理。完整目录在到达此处前已强制校验四件套。
        incar = {}
    return _infer_task_type(incar)


def _read_incar_input(incar: str) -> tuple[str, dict]:
    """共享 INCAR 路径/原文 -> (原文,解析字典)。"""
    if os.path.isfile(incar):
        with open(incar, 'r', encoding='utf-8', errors='replace') as fh:
            text = fh.read()
    else:
        text = incar
    return text, parse_incar(text)


def _uniform_generated_encut(items: list[dict], shared_incar: str,
                              lib_root: str | None) -> tuple[int | None, dict[str, str]]:
    """共享 INCAR 未写 ENCUT 时，按所有可用结构统一计算组级 ENCUT。

    单项 POSCAR/POTCAR 失败写入 errors 并从并集剔除，不能拖垮同批其它结构。
    用户显式 ENCUT 时返回 None，由 job_builder 原样尊重。
    """
    _text, incar_dict = _read_incar_input(shared_incar)
    if 'ENCUT' in {str(k).upper() for k in incar_dict}:
        return None, {}
    errors: dict[str, str] = {}
    maxima: list[float] = []
    for item in items:
        structure = item.get('structure') or ''
        try:
            elements, _counts = parse_poscar_species(read_poscar(structure))
            if not elements:
                raise ValueError('POSCAR 缺元素符号行(VASP5 第 6 行)')
            # 逐项查赝势，坏元素/缺库只隔离本项；余下项目仍可生成。
            maxima.append(max_enmax(elements, lib_root))
        except Exception as exc:                         # noqa: BLE001
            errors[item['path']] = str(exc)
    if not maxima:
        return None, errors
    force = int(math.ceil(1.3 * max(maxima) / 50.0) * 50)
    return force, errors


def _write_quick_manifest(job_dir: str, *, name: str, source: str,
                          engine: str, copied: list[str], mode: str,
                          build_result: dict | None = None,
                          structure: str = '', shared_incar: str = '') -> None:
    """为复制/自动生成两条路径写统一、可追溯的 job.yaml。"""
    build_result = build_result or {}
    sha = {fn: manifest_mod.sha256_file(os.path.join(job_dir, fn)) for fn in copied}
    inputs = {
        'engine': engine,
        'files': copied,
        'source': os.path.abspath(source),
        'sha256': sha,
        'quick_submit_mode': mode,
    }
    if mode == 'generate':
        inputs.update({
            'source_structure': os.path.abspath(structure),
            'shared_incar': (os.path.abspath(shared_incar)
                             if os.path.isfile(shared_incar) else 'inline'),
            'completions': dict(build_result.get('completions') or {}),
            'elements': list(build_result.get('elements') or []),
            'kpoints': list(build_result.get('kpoints') or []),
            'potcar': list(build_result.get('potcar') or []),
        })
    task_type = (str(build_result.get('task_type') or '')
                 if build_result else _manifest_task_type(engine, job_dir))
    if not task_type:
        task_type = 'quick'
    warnings = [
        f'快速提交作业(引擎 {engine});集群运行命令模板:{submit_hint(engine)}',
        (f'提交前请在集群配置 engine_commands.{engine} 中确认此命令'
         if engine != 'vasp' else '提交前请在集群配置中确认 VASP 执行命令'),
    ]
    warnings.extend(list(build_result.get('warnings') or []))
    if mode == 'generate':
        warnings.insert(0, '由源结构 + 共享 INCAR 自动生成；源文件未被修改')
    manifest = manifest_mod.new_manifest(
        job_id=f'{name}-{time.strftime("%Y%m%d-%H%M%S")}',
        system=_job_name_for(source, engine),
        task_type=task_type,
        calc_type=str(build_result.get('calc_type') or ''),
        inputs=inputs,
        warnings=warnings,
    )
    manifest_mod.save_manifest(job_dir, manifest)


def build_quick_jobs(files: list, out_root: str, *, job_prefix: str = '',
                     shared_incar: str = '', lib_root: str = '',
                     calc_type: str = 'slab') -> dict:
    """任意输入文件列表 → 逐个建轻量作业目录 + 落 job.yaml(state=CREATED)。

    ``files`` 可含一个父根目录：先递归预检并去重。完整四件套原样复制；只有
    POSCAR/CONTCAR 的结构在 ``shared_incar`` 有效时由 job_builder 自动生成
    INCAR/POSCAR/KPOINTS/POTCAR。共享 INCAR 未显式写 ENCUT 时，按本批可用
    结构的元素统一补同一个 ENCUT。所有输出写入临时目录后原子落位，不覆盖源文件；
    任一结构失败只记 skipped，不影响同批其它项。

    返回 {'ok', 'jobs':[{'dir','name','engine','files'}], 'skipped':[{'file','reason'}], 'error'}。
    """
    result = {'ok': False, 'jobs': [], 'skipped': [], 'preflight': None, 'error': None}
    shared = _shared_incar_value(shared_incar)
    preflight = scan_inputs(files, shared_incar=shared_incar)
    result['preflight'] = preflight

    # The selected parent directory is part of the user's read-only source,
    # even when each discovered calculation lives one level below it.  Guard
    # the original directory entries before creating *any* output so a layout
    # such as ``batch -> batch/generated`` cannot pollute the next recursive
    # scan or contradict the UI's "source remains unchanged" promise.
    for raw in files or []:
        source_entry = os.path.abspath(str(raw).strip())
        if not str(raw).strip() or not os.path.isdir(source_entry):
            continue
        if _inside_directory(out_root, source_entry):
            reason = (
                '输出根目录位于所选源目录内部；为保证源目录不被修改，'
                '请选择该目录的同级位置或其它目录'
            )
            result['skipped'].append({
                'file': source_entry, 'reason': reason, 'missing': [],
                'status': 'unsafe_output_root',
            })
            result['error'] = f'未生成任何作业：{reason}'
            return result

    generatable = [i for i in preflight['items'] if i['status'] == 'generatable']
    force_encut, generate_errors = (None, {})
    if shared and generatable:
        try:
            force_encut, generate_errors = _uniform_generated_encut(
                generatable, shared, (lib_root or None))
        except Exception as exc:                         # noqa: BLE001
            # 共享 INCAR 本身坏时，所有“自动生成”项失败；完整四件套仍照常导入。
            generate_errors = {i['path']: f'共享 INCAR 无法读取/解析:{exc}'
                               for i in generatable}

    used: set[str] = set()
    root_ready = False
    for item in preflight['items']:
        source = item['path']
        engine = item.get('engine') or ''
        if not item.get('can_build'):
            missing = item.get('missing') or []
            reason = item.get('message') or '输入不可用'
            if missing:
                reason += f'；缺少: {", ".join(missing)}'
            result['skipped'].append({'file': source, 'reason': reason,
                                      'missing': missing, 'status': item.get('status')})
            continue
        if engine == 'vasp' and os.path.isdir(source):
            if _inside_directory(out_root, source):
                result['skipped'].append({
                    'file': source,
                    'reason': '输出根目录位于源计算目录内部；为保证源目录不被修改，请选择同级或其它目录',
                    'missing': item.get('missing') or [],
                    'status': 'unsafe_output_root',
                })
                continue
        if source in generate_errors:
            result['skipped'].append({
                'file': source,
                'reason': f'自动生成失败:{generate_errors[source]}',
                'missing': item.get('missing') or [],
                'status': 'generation_failed',
            })
            continue

        if not root_ready:
            try:
                os.makedirs(out_root, exist_ok=True)
            except OSError as exc:
                result['error'] = f'无法创建输出根目录 {out_root!r}:{exc}'
                return result
            root_ready = True

        name = _unique_dir_name(
            _sanitize(f'{job_prefix}{item.get("name") or _job_name_for(source, engine)}'),
            out_root, used)
        used.add(name)
        job_dir = os.path.join(out_root, name)
        stage = ''
        try:
            stage = tempfile.mkdtemp(prefix=f'.{name}.building-', dir=out_root)
            mode = item.get('mode') or 'copy'
            build_result = None
            if mode == 'generate':
                if not shared:
                    raise ValueError('未选择有效的共享 INCAR')
                structure = item.get('structure') or ''
                build_result = build_job_dir(
                    structure, shared, stage,
                    calc_type=(calc_type or 'slab'), validate=True,
                    lib_root=(lib_root or None), system_name=item.get('name') or name,
                    force_encut=force_encut,
                )
                copied = [fn for fn in _VASP_INPUTS
                          if os.path.isfile(os.path.join(stage, fn))]
                if set(copied) != set(_VASP_INPUTS):
                    absent = [fn for fn in _VASP_INPUTS if fn not in copied]
                    raise ValueError(f'生成后四件套仍不完整，缺少: {", ".join(absent)}')
            else:
                structure = ''
                copied = _copy_inputs(source, stage)
                if engine == 'vasp':
                    from vcstudio.project.result_import import validate_vasp_quartet
                    issues = validate_vasp_quartet(stage)
                    if issues:
                        raise ValueError('四件套复核失败：' + '；'.join(issues))
            _write_quick_manifest(
                stage, name=name, source=source, engine=engine, copied=copied,
                mode=mode, build_result=build_result, structure=structure,
                shared_incar=(shared or ''),
            )
            # unique_dir_name 已保证目标不存在；原子改名使台账看不到半成品。
            if os.path.exists(job_dir):
                raise FileExistsError(f'目标目录刚刚被其它进程创建，未覆盖: {job_dir}')
            os.replace(stage, job_dir)
            stage = ''
        except Exception as exc:                         # noqa: BLE001
            if stage and os.path.isdir(stage):
                shutil.rmtree(stage, ignore_errors=True)
            result['skipped'].append({
                'file': source,
                'reason': f'{"自动生成" if item.get("mode") == "generate" else "拷贝"}失败:{exc}',
                'missing': item.get('missing') or [],
                'status': 'generation_failed' if item.get('mode') == 'generate' else 'copy_failed',
            })
            continue
        result['jobs'].append({'dir': job_dir, 'name': name,
                               'engine': engine, 'files': copied,
                               'mode': item.get('mode') or 'copy',
                               'warnings': list((build_result or {}).get('warnings') or [])})

    if not result['jobs']:
        if result['skipped']:
            first = result['skipped'][0].get('reason') or '所有输入均被跳过'
            extra = len(result['skipped']) - 1
            result['error'] = '未生成任何作业：' + first
            if extra:
                result['error'] += f'（另有 {extra} 项失败或被跳过）'
        else:
            result['error'] = '未生成任何作业：没有可处理的输入'
        return result
    result['ok'] = True
    return result
