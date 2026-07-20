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

import os
import shutil
import time

from vcstudio.generate.incar_builder import parse_incar
from vcstudio.generate.job_builder import _infer_task_type
from vcstudio.generate.poscar import parse_poscar_species
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

# 每引擎的集群运行命令模板提示(仅文案:真正 vasp_cmd 由用户模板/集群 profile 决定)。
_SUBMIT_HINTS = {
    'gaussian': 'g16 < input.gjf > output.log   # 或 g09;Gaussian 本体与许可用户自备',
    'cp2k': 'cp2k.psmp -i input.inp -o output.out   # 或 cp2k.popt',
    'castep': 'mpirun -np <N> castep.mpi <seedname>   # 需 <seedname>.cell 与 .param',
    'vasp': 'mpirun -np <N> vasp_std > vasp.log 2>&1   # VASP 本体与 POTCAR 用户自备',
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


def _unique_dir_name(base: str, out_root: str, used: set) -> str:
    """base 撞名(本轮 used 或磁盘已存在)→ 追加 _2/_3… 直至唯一。"""
    cand = base
    n = 1
    while cand in used or os.path.exists(os.path.join(out_root, cand)):
        n += 1
        cand = f'{base}_{n}'
    return cand


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


def _missing_vasp_inputs(path: str) -> list[str]:
    """VASP 作业目录缺失的输入四件套。非目录不在此处限制。"""
    if not os.path.isdir(path):
        return []
    return [fn for fn in _VASP_INPUTS if not os.path.isfile(os.path.join(path, fn))]


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


def build_quick_jobs(files: list, out_root: str, *, job_prefix: str = '') -> dict:
    """任意输入文件列表 → 逐个建轻量作业目录 + 落 job.yaml(state=CREATED)。

    每个可识别文件:建 out_root/<sanitize(作业名)> 目录(同名自动 _2/_3 后缀避让),
    拷入输入,写 manifest(VASP 从 INCAR 推断 task_type,其它引擎为 'quick'、
    inputs.engine、各文件 sha256 溯源、submit_hint 入 warnings)。认不出引擎/
    文件不存在 → 记 skipped(不打断整批)。VASP 目录必须同时包含
    INCAR/POSCAR/KPOINTS/POTCAR,否则跳过且列出缺失项。

    返回 {'ok', 'jobs':[{'dir','name','engine','files'}], 'skipped':[{'file','reason'}], 'error'}。
    """
    result = {'ok': False, 'jobs': [], 'skipped': [], 'error': None}
    try:
        os.makedirs(out_root, exist_ok=True)
    except OSError as e:
        result['error'] = f'无法创建输出根目录 {out_root!r}:{e}'
        return result

    used: set = set()
    for f in files:
        engine = detect_engine(f)
        if engine is None:
            result['skipped'].append({'file': str(f), 'reason': '无法识别引擎(未知输入类型/文件名)'})
            continue
        if not (os.path.isfile(f) or os.path.isdir(f)):
            result['skipped'].append({'file': str(f), 'reason': '输入不存在'})
            continue
        if engine == 'vasp' and os.path.isdir(f):
            missing = _missing_vasp_inputs(f)
            if missing:
                result['skipped'].append({
                    'file': str(f),
                    'reason': f'VASP 输入四件套不完整，缺少: {", ".join(missing)}',
                })
                continue
            # Existence alone cannot catch an empty POTCAR, a malformed mesh,
            # or POSCAR/POTCAR element-order mismatches.  Share the same
            # side-effect-free gate as the adsorption folder importer.
            from vcstudio.project.result_import import validate_vasp_quartet
            issues = validate_vasp_quartet(f)
            if issues:
                result['skipped'].append({
                    'file': str(f),
                    'reason': 'VASP 输入门控未通过: ' + '；'.join(issues),
                })
                continue

        name = _unique_dir_name(
            _sanitize(f'{job_prefix}{_job_name_for(f, engine)}'), out_root, used)
        used.add(name)
        job_dir = os.path.join(out_root, name)
        try:
            os.makedirs(job_dir, exist_ok=True)
            copied = _copy_inputs(f, job_dir)
        except OSError as e:
            result['skipped'].append({'file': str(f), 'reason': f'拷贝失败:{e}'})
            continue

        sha = {fn: manifest_mod.sha256_file(os.path.join(job_dir, fn)) for fn in copied}
        task_type = _manifest_task_type(engine, job_dir)
        inputs = {'engine': engine, 'files': copied,
                  'source': os.path.abspath(f), 'sha256': sha}
        if engine == 'vasp':
            try:
                with open(os.path.join(job_dir, 'POSCAR'), 'r', encoding='utf-8',
                          errors='replace') as handle:
                    elements, counts = parse_poscar_species(handle.read())
            except (OSError, ValueError, IndexError, TypeError):
                elements, counts = [], []
            inputs.update({'elements': elements, 'counts': counts,
                           'import_input_issues': []})
        m = manifest_mod.new_manifest(
            job_id=f'{name}-{time.strftime("%Y%m%d-%H%M%S")}',
            system=_job_name_for(f, engine),
            task_type=task_type,
            calc_type='',
            inputs=inputs,
            warnings=[f'快速提交作业(引擎 {engine});集群运行命令模板:{submit_hint(engine)}'],
        )
        manifest_mod.save_manifest(job_dir, m)
        result['jobs'].append({'dir': job_dir, 'name': name,
                               'engine': engine, 'files': copied})

    result['ok'] = True
    return result
