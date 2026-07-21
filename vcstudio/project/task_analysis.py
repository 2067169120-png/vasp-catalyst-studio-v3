"""通用 DFT 任务分析能力表与可追溯报告。

本模块只把已经存在、能够由本地输出文件复核的解析能力接到统一入口。没有解析器的
任务会明确返回 ``evidence_only`` 或 ``dedicated``，绝不把“文件存在”冒充科学结论。
报告包含输入任务、解析结果和输出文件指纹，便于事后追溯本次结论来自哪一份结果。
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import shutil
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path


# 与 generate.task_catalog 的 23 项逐一对应。route 只描述分析入口，不代表计算生成能力。
TASK_CAPABILITIES = {
    'relax': ('generic', 'integrated', '查看末步能量、力和离子步；确认收敛后可派生静态计算。'),
    'cellopt': ('generic', 'integrated', '查看末步能量/力，并用 CONTCAR 作为后续平衡结构。'),
    'static': ('generic', 'integrated', '查看单点能量与电子收敛证据。'),
    'adsorption_project': ('dedicated', 'dedicated', '进入“吸附能项目”选择 clean slab、吸附态和参考态。'),
    'spin_scan': ('dedicated', 'dedicated', '进入“多自旋比较”汇总全部变体后判定基态。'),
    'dos_pdos': ('dos', 'integrated', '读取 vasprun.xml 的投影 DOS，并给出全体系 d 带中心。'),
    'bands': ('bands', 'integrated', '读取 EIGENVAL/vasprun.xml 并计算带隙。'),
    'bader': ('bader', 'evidence_only',
              '已下载 CHGCAR/AECCAR0/AECCAR2 时，请先用 Henkelman Bader '
              '生成 ACF.dat；导入 ACF.dat 后可在软件内解析与出报告。'),
    'chgdiff': ('chgdiff', 'integrated', '读取 CHGDIFF.vasp 并生成法向面平均 Δρ(z)。'),
    'elf': ('artifact', 'evidence_only', '已核对 ELFCAR；请用 VESTA 等工具检查等值面。'),
    'freq': ('freq', 'integrated', '检查虚频质量闸，再决定热校正能否进入自由能。'),
    'aimd': ('aimd', 'integrated', '检查能量/温度序列；热稳定结论仍需足够长轨迹。'),
    'neb': ('neb', 'integrated', '检查能垒、最高点位置和各 image 力收敛。'),
    'dimer': ('generic', 'integrated', '检查收敛后再用频率确认恰有一个大虚频。'),
    'eos': ('eos', 'integrated', '至少三个 E-V 点完成后执行 BM3 拟合。'),
    'surface_energy': ('dedicated', 'dedicated', '进入“表面能计算器”同时选择 slab 与体相参考。'),
    'workfunction': ('workfunction', 'integrated', '读取 LOCPOT 与 OUTCAR 的费米能并计算功函数。'),
    'formation_binding': ('dedicated', 'dedicated', '进入“形成能/结合能计算器”明确选择全部参考态。'),
    'vaspsol': ('dedicated', 'dedicated', '选择同几何的真空/溶剂两份结果后计算溶剂化能。'),
    'conv_encut': ('conv', 'integrated', '完成整组扫描后按每原子能差选择收敛点。'),
    'conv_kmesh': ('conv', 'integrated', '完成整组扫描后按每原子能差选择收敛点。'),
    'conv_vacuum': ('conv', 'integrated', '完成整组扫描后检查真空厚度收敛。'),
    'conv_thickness': ('conv', 'integrated', '完成整组扫描后检查 slab 层厚收敛。'),
}

ALIASES = {
    'band': 'bands', 'work_function': 'workfunction', 'wf': 'workfunction',
    'encut': 'conv_encut', 'kmesh': 'conv_kmesh', 'vacuum': 'conv_vacuum',
    'thickness': 'conv_thickness', 'conv': 'conv_encut', 'conv_scan': 'conv_encut',
    'dos': 'dos_pdos', 'pdos': 'dos_pdos', 'cell_opt': 'cellopt',
}

ARTIFACTS = {
    'dos_pdos': ('DOSCAR', 'vasprun.xml'),
    'bader': ('ACF.dat', 'CHGCAR', 'AECCAR0', 'AECCAR2'),
    'chgdiff': ('CHGDIFF.vasp',),
    'elf': ('ELFCAR',),
}

_TRACE_FILES = (
    'job.yaml', 'INCAR', 'KPOINTS', 'POSCAR', 'POTCAR', 'CONTCAR', 'OSZICAR', 'OUTCAR',
    'vasprun.xml', 'EIGENVAL', 'DOSCAR', 'ACF.dat', 'CHGCAR', 'AECCAR0',
    'AECCAR2', 'CHGDIFF.vasp', 'ELFCAR', 'LOCPOT', 'XDATCAR',
)
_MAX_HASH_BYTES = 256 * 1024 * 1024
_TEMP_RE = re.compile(r'\bT=\s*([-+0-9.Ee]+)')
_FRAME_RE = re.compile(r'^\d+$')
_ENGINE_ALIASES = {
    'g16': 'gaussian', 'g09': 'gaussian', 'gaussian16': 'gaussian',
    'materials_studio': 'castep', 'materials studio': 'castep',
    'ms': 'castep', 'castep/ms': 'castep',
}
_NON_VASP_ENGINES = frozenset(('gaussian', 'cp2k', 'castep'))


def normalize_task_key(key: str | None) -> str:
    """任务别名归一化；未知值原样返回，交调用方明确报错。"""
    token = str(key or '').strip().lower()
    return ALIASES.get(token, token)


def capability(key: str | None) -> dict:
    """返回单个任务的可机读分析能力；未知任务不伪装成通用 VASP。"""
    task_key = normalize_task_key(key)
    row = TASK_CAPABILITIES.get(task_key)
    if row is None:
        return {'task_key': task_key or None, 'known': False, 'route': None,
                'analysis_status': 'unsupported', 'report_supported': False,
                'next_action': '回到设置页选择已登记的计算类型。'}
    route, status, next_action = row
    return {'task_key': task_key, 'known': True, 'route': route,
            'analysis_status': status, 'report_supported': True,
            'next_action': next_action}


def capability_matrix() -> dict:
    """23 项能力矩阵的深拷贝式 JSON-safe 视图。"""
    return {key: capability(key) for key in TASK_CAPABILITIES}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def load_job_manifest(job_dir) -> dict:
    """读取作业清单；损坏/缺失时返回空字典，不根据输出文件猜引擎。"""
    try:
        from vcstudio.shared import manifest
        value = manifest.load_manifest(job_dir) or {}
    except Exception:
        value = {}
    return value if isinstance(value, dict) else {}


def normalize_engine(engine: str | None) -> str:
    """引擎别名归一化；空值仅为兼容旧 VASP 清单回落 vasp。"""
    token = str(engine or '').strip().lower()
    return _ENGINE_ALIASES.get(token, token) or 'vasp'


def job_engine(job_dir) -> str:
    """仅从 ``job.yaml inputs.engine`` 取引擎；旧清单默认 VASP。"""
    value = load_job_manifest(job_dir)
    inputs = value.get('inputs') or {}
    if not isinstance(inputs, dict):
        inputs = {}
    return normalize_engine(inputs.get('engine'))


def engine_task_key(job_dir) -> str:
    """为非 VASP ``quick`` 清单找到报告展示类型，未声明时安全回落单点摘要。"""
    value = load_job_manifest(job_dir)
    inputs = value.get('inputs') or {}
    if not isinstance(inputs, dict):
        inputs = {}
    candidates = (inputs.get('task'), value.get('task_type'))
    for raw in candidates:
        key = normalize_task_key(raw)
        if key in TASK_CAPABILITIES and key not in ('adsorption_project', 'spin_scan'):
            return key
    return 'static'


def _safe_relative_name(value) -> str | None:
    """用户可编辑 manifest 中的路径只能指向作业目录内的普通相对路径。"""
    raw = str(value or '').strip().replace('\\', '/')
    path = Path(raw)
    if (not raw or path.is_absolute() or any(part in ('', '.', '..') for part in path.parts)):
        return None
    return '/'.join(path.parts)


def expected_engine_output_names(engine: str, input_files=()) -> list[str]:
    """按提交器的命名契约从主输入派生默认输出，不扫描/伪造结果。"""
    eng = normalize_engine(engine)
    names = []
    for item in input_files or ():
        safe = _safe_relative_name(item)
        if safe and '/' not in safe and safe not in names:
            names.append(safe)
    if eng == 'gaussian':
        primary = next((name for name in names
                        if Path(name).suffix.lower() in ('.gjf', '.com')), None)
        stem = Path(primary).stem if primary else 'input'
        return [f'{stem}.log', f'{stem}.chk']
    if eng == 'cp2k':
        primary = next((name for name in names if Path(name).suffix.lower() == '.inp'), None)
        stem = Path(primary).stem if primary else 'cp2k'
        return [f'{stem}.out']
    if eng == 'castep':
        primary = next((name for name in names
                        if Path(name).suffix.lower() in ('.cell', '.param')), None)
        stem = Path(primary).stem if primary else 'case'
        return [f'{stem}.castep', f'{stem}.geom']
    return []


def _manifest_evidence_names(root: Path, value: dict) -> list[str]:
    """汇总清单声明的输入/输出/下载证据及 NEB image 关键文件。"""
    names = list(_TRACE_FILES)
    inputs = value.get('inputs') or {}
    results = value.get('results') or {}
    if not isinstance(inputs, dict):
        inputs = {}
    if not isinstance(results, dict):
        results = {}

    for key in ('files', 'output_files', 'imported_files'):
        raw = inputs.get(key) or []
        if isinstance(raw, (list, tuple)):
            names.extend(raw)
    for key in ('fetched', 'fetch_requested'):
        raw = results.get(key) or []
        if isinstance(raw, (list, tuple)):
            names.extend(raw)
    diagnosis = results.get('diagnosis') or {}
    if isinstance(diagnosis, dict) and diagnosis.get('output_file'):
        names.append(diagnosis['output_file'])

    engine = normalize_engine(inputs.get('engine'))
    declared = inputs.get('files') or []
    explicit_outputs = inputs.get('output_files') or []
    if not explicit_outputs and engine in _NON_VASP_ENGINES:
        names.extend(expected_engine_output_names(engine, declared))
    if engine == 'gaussian':
        patterns = ('*.log', '*.out', '*.chk')
    elif engine == 'cp2k':
        patterns = ('*.out',)
    elif engine == 'castep':
        patterns = ('*.castep', '*.geom')
    else:
        patterns = ()
    for pattern in patterns:
        names.extend(path.name for path in root.glob(pattern) if path.is_file())

    # NEB 证据位于 image 子目录；不对其他子目录做无界扫描。
    try:
        frames = sorted((p for p in root.iterdir() if p.is_dir() and _FRAME_RE.match(p.name)),
                        key=lambda p: int(p.name))
    except OSError:
        frames = []
    for frame in frames:
        for filename in ('POSCAR', 'CONTCAR', 'OSZICAR', 'OUTCAR', 'vasprun.xml'):
            if (frame / filename).is_file():
                names.append(f'{frame.name}/{filename}')

    safe_names = []
    for raw in names:
        safe = _safe_relative_name(raw)
        if safe and safe not in safe_names:
            safe_names.append(safe)
    return safe_names


def collect_evidence(job_dir) -> dict:
    """收集关键输出的大小、mtime 与 SHA256；超大文件明确标记未哈希。"""
    root = Path(job_dir).resolve()
    files, warnings = [], []
    value = load_job_manifest(root)
    for name in _manifest_evidence_names(root, value):
        path = root / name
        if not path.is_file():
            continue
        stat = path.stat()
        digest = None
        if stat.st_size <= _MAX_HASH_BYTES:
            try:
                digest = _sha256(path)
            except OSError as exc:
                warnings.append(f'{name} 计算 SHA256 失败：{exc}')
        else:
            warnings.append(f'{name} 超过 256 MiB，仅记录大小与修改时间。')
        files.append({'name': name, 'size': stat.st_size,
                      'mtime': datetime.fromtimestamp(
                          stat.st_mtime, timezone.utc).isoformat(),
                      'sha256': digest})
    return {'job_dir': str(root), 'files': files, 'warnings': warnings}


def inspect_artifacts(job_dir, task_key: str) -> dict:
    """只核对产物，不把存在性包装成已完成的定量分析。"""
    key = normalize_task_key(task_key)
    expected = list(ARTIFACTS.get(key, ()))
    root = Path(job_dir)
    present = [name for name in expected if (root / name).is_file()]
    missing = [name for name in expected if name not in present]
    if not present:
        return {'ok': False, 'kind': key, 'result': {'present': [], 'missing': missing},
                'figure': None, 'summary': '', 'files': [],
                'error': f'未找到 {" / ".join(expected)}；尚不能核对该任务产物。'}
    return {'ok': True, 'kind': key,
            'result': {'present': present, 'missing': missing}, 'figure': None,
            'summary': (f'已找到：{"、".join(present)}。'
                        + (f' 仍缺：{"、".join(missing)}。' if missing else '')
                        + ' 当前仅完成产物核对，未生成定量科学结论。'),
            'files': [str(root / name) for name in present], 'error': None}


def analyze_dos(job_dir) -> dict:
    """解析投影 DOS 并给出可复核的全体系 d 带中心，不把大数组塞进报告。"""
    from vcstudio.project import dosparse

    source = Path(job_dir) / 'vasprun.xml'
    if not source.is_file():
        return {'ok': False, 'kind': 'dos_pdos', 'result': None, 'figure': None,
                'summary': '', 'files': [],
                'error': '未找到 vasprun.xml；DOSCAR 单独存在时当前无法可靠恢复完整 PDOS 口径。'}
    try:
        pdos = dosparse.parse_vasprun_pdos(source)
        first = (pdos.get('ions') or [{}])[0]
        orbitals = sorted((first.get('orbitals') or {}).keys())
        centers = None
        if 'd' in orbitals:
            summed = dosparse.sum_pdos(pdos, orbitals=('d',), spin='both')
            centers = dosparse.spin_band_centers(
                summed['energies'], summed['dos_up'], summed['dos_down'],
                efermi=float(pdos['efermi']))
    except (OSError, ValueError, TypeError) as exc:
        return {'ok': False, 'kind': 'dos_pdos', 'result': None, 'figure': None,
                'summary': '', 'files': [], 'error': str(exc)}
    result = {'efermi_ev': pdos.get('efermi'),
              'n_energy_points': len(pdos.get('energies') or []),
              'n_ions': len(pdos.get('ions') or []),
              'spin_polarized': bool(pdos.get('spin_polarized')),
              'available_orbitals': orbitals, 'd_band_centers': centers,
              'integration_window_ev_relative_ef': [-10.0, 2.0]}
    if centers and centers.get('up', {}).get('center_eV') is not None:
        summary = f'全体系 d 带中心(up)={centers["up"]["center_eV"]:.4f} eV（相对 E_F）。'
    else:
        summary = f'已解析 {result["n_ions"]} 个原子、{result["n_energy_points"]} 个能量点；无可用 d 投影中心。'
    return {'ok': True, 'kind': 'dos_pdos', 'result': result, 'figure': None,
            'summary': summary, 'files': [str(source)], 'error': None}


def analyze_bader(job_dir) -> dict:
    """解析 ACF.dat；有 POTCAR/POSCAR 时按 ZVAL−CHARGE 给真实 ΔQ。"""
    from vcstudio.generate.poscar import parse_poscar_species
    from vcstudio.project import bader

    root = Path(job_dir)
    acf = root / 'ACF.dat'
    if not acf.is_file():
        return {'ok': False, 'kind': 'bader', 'result': None, 'figure': None,
                'summary': '', 'files': [], 'error': '未找到 ACF.dat；请先运行 Bader 分区。'}
    warnings = []
    try:
        charges = bader.parse_acf(acf)
        result = {'charges': charges, 'n_atoms': len(charges),
                  'delta_q': None, 'sum_delta_q': None, 'warnings': warnings}
        potcar = root / 'POTCAR'
        poscar = root / 'POSCAR'
        if not poscar.is_file():
            poscar = root / 'CONTCAR'
        if potcar.is_file() and poscar.is_file():
            _symbols, counts = parse_poscar_species(_read(poscar))
            if not counts:
                warnings.append('POSCAR 无可解析物种计数，未计算 ΔQ。')
            else:
                per_atom = bader.expand_zvals(bader.read_potcar_zvals(potcar), counts)
                detailed = bader.parse_acf(acf, per_atom)
                result.update(detailed)
                result['n_atoms'] = len(detailed['charges'])
        else:
            warnings.append('缺 POTCAR 或 POSCAR，仅报告 Bader 分区电子数；无法计算 ΔQ。')
    except (OSError, ValueError) as exc:
        return {'ok': False, 'kind': 'bader', 'result': None, 'figure': None,
                'summary': '', 'files': [], 'error': str(exc)}
    if result.get('sum_delta_q') is None:
        summary = f'解析 {result["n_atoms"]} 个原子的 Bader 电子数；缺 ZVAL，未计算 ΔQ。'
    else:
        summary = (f'解析 {result["n_atoms"]} 个原子，'
                   f'ΣΔQ={result["sum_delta_q"]:+.4f} e。')
    files = [str(path) for path in (acf, root / 'POTCAR', poscar) if path.is_file()]
    return {'ok': True, 'kind': 'bader', 'result': result, 'figure': None,
            'summary': summary, 'files': files, 'error': None}


def analyze_chgdiff(job_dir) -> dict:
    """从已生成的 CHGDIFF.vasp 读取网格并给出 z 向面平均电荷密度。"""
    from vcstudio.project import chgdiff

    source = Path(job_dir) / 'CHGDIFF.vasp'
    if not source.is_file():
        return {'ok': False, 'kind': 'chgdiff', 'result': None, 'figure': None,
                'summary': '', 'files': [],
                'error': '未找到 CHGDIFF.vasp；请先完成 AB/A/B 三静态并执行网格相减。'}
    try:
        profile = chgdiff.plane_averaged(source, axis='z')
    except (OSError, ValueError) as exc:
        return {'ok': False, 'kind': 'chgdiff', 'result': None, 'figure': None,
                'summary': '', 'files': [], 'error': str(exc)}
    rho = list(profile.get('rho') or [])
    result = {'axis': 'z', 'z_a': list(profile.get('z') or []), 'rho_e_a3': rho,
              'n_points': len(rho), 'rho_min_e_a3': min(rho) if rho else None,
              'rho_max_e_a3': max(rho) if rho else None}
    summary = (f'得到 {len(rho)} 个 z 向面平均点，Δρ 范围 '
               f'[{result["rho_min_e_a3"]:.6g}, {result["rho_max_e_a3"]:.6g}] e/Å³。'
               if rho else 'CHGDIFF.vasp 未产生可用面平均点。')
    return {'ok': bool(rho), 'kind': 'chgdiff', 'result': result, 'figure': None,
            'summary': summary, 'files': [str(source)],
            'error': None if rho else '差分电荷网格为空。'}


def _engine_files(job_dir, value: dict, engine: str) -> list[str]:
    """返回已存在的引擎输入/输出证据，路径只限作业根目录。"""
    root = Path(job_dir)
    inputs = value.get('inputs') or {}
    if not isinstance(inputs, dict):
        inputs = {}
    names = []
    for key in ('files', 'output_files'):
        raw = inputs.get(key) or []
        if isinstance(raw, (list, tuple)):
            names.extend(raw)
    if not inputs.get('output_files'):
        names.extend(expected_engine_output_names(engine, inputs.get('files') or ()))
    patterns = {
        'gaussian': ('*.gjf', '*.com', '*.log', '*.out', '*.chk'),
        'cp2k': ('*.inp', '*.out'),
        'castep': ('*.cell', '*.param', '*.castep', '*.geom'),
    }.get(engine, ())
    for pattern in patterns:
        names.extend(path.name for path in root.glob(pattern) if path.is_file())
    paths = []
    for raw in names:
        safe = _safe_relative_name(raw)
        path = root / safe if safe and '/' not in safe else None
        if path is not None and path.is_file() and str(path) not in paths:
            paths.append(str(path))
    return paths


def analyze_engine_outputs(job_dir, engine: str | None = None,
                           task_key: str | None = None) -> dict:
    """Gaussian/CP2K/CASTEP 主输出解析：能量统一 eV，完成证据独立呈现。

    ``ok`` 表示本地输出中解析到了能量，不等于已收敛；收敛/正常终止位由
    ``result.converged`` 与 ``completion_evidence`` 明确表达。
    """
    root = Path(job_dir)
    value = load_job_manifest(root)
    eng = normalize_engine(engine or job_engine(root))
    key = normalize_task_key(task_key or engine_task_key(root)) or 'static'
    if eng not in _NON_VASP_ENGINES:
        return {'ok': False, 'kind': key, 'result': None, 'figure': None,
                'summary': '', 'files': [],
                'error': f'引擎 {eng!r} 不属于已接入的非 VASP 解析器。'}
    try:
        from vcstudio import engines
        backend = engines.get_backend(eng)
        inputs = value.get('inputs') or {}
        explicit = ((inputs.get('output_files') or [])
                    if isinstance(inputs, dict) else [])
        declared = []
        if isinstance(explicit, (list, tuple)):
            for raw in explicit:
                safe = _safe_relative_name(raw)
                path = root / safe if safe and '/' not in safe else None
                if path is not None and path.is_file():
                    declared.append(path)
        if explicit:
            if not declared:
                parsed = {'energy_ev': None, 'converged': False,
                          'error': '清单声明的主输出尚未下载：'
                                   + ' / '.join(str(x) for x in explicit)}
            else:
                # Backend 的文件级解析器以目录为入口。用同卷硬链接构造仅含
                # 已声明输出的短命目录，防止误读同目录中另一轮旧 *.out/*.log。
                with tempfile.TemporaryDirectory(prefix='.vcstudio-analysis-',
                                                 dir=str(root)) as tmp:
                    for source in declared:
                        target = Path(tmp) / source.name
                        try:
                            os.link(source, target)
                        except OSError:
                            shutil.copy2(source, target)
                    # Task-specific completion (opt vs SP vs phonon) is encoded
                    # in each engine's input.  Include only manifest-declared
                    # local inputs in the sandbox so the backend can infer that
                    # task without seeing undeclared stale outputs.
                    raw_inputs = inputs.get('files') or [] if isinstance(inputs, dict) else []
                    for raw in raw_inputs if isinstance(raw_inputs, (list, tuple)) else []:
                        safe = _safe_relative_name(raw)
                        source = root / safe if safe and '/' not in safe else None
                        if source is None or not source.is_file():
                            continue
                        target = Path(tmp) / source.name
                        if target.exists():
                            continue
                        try:
                            os.link(source, target)
                        except OSError:
                            shutil.copy2(source, target)
                    parsed = dict(backend.parse_energy(tmp) or {})
        else:
            parsed = dict(backend.parse_energy(str(root)) or {})
    except (OSError, ValueError, TypeError) as exc:
        parsed = {'energy_ev': None, 'converged': False, 'error': str(exc)}

    energy = parsed.get('energy_ev')
    if isinstance(energy, bool) or not isinstance(energy, (int, float)) \
            or not math.isfinite(float(energy)):
        energy = None
    else:
        energy = float(energy)
    converged = bool(parsed.get('converged'))
    results = value.get('results') or {}
    if not isinstance(results, dict):
        results = {}
    diagnosis = results.get('diagnosis') or {}
    if not isinstance(diagnosis, dict):
        diagnosis = {}
    completion = {
        'manifest_state': value.get('state'),
        'failure_class': diagnosis.get('failure_class'),
        'evidence': diagnosis.get('evidence'),
        'output_file': diagnosis.get('output_file'),
        'output_bytes': diagnosis.get('output_bytes'),
        'fetched': list(results.get('fetched') or []),
        'fetched_missing': list(results.get('fetched_missing') or []),
        'parser_converged': converged,
    }
    result = {'engine': eng, 'energy_ev': energy, 'converged': converged,
              'completion_evidence': completion}
    for name, item in parsed.items():
        if name not in ('energy_ev', 'converged', 'error'):
            result[name] = item
    files = _engine_files(root, value, eng)
    if energy is None:
        error = str(parsed.get('error') or '未从本地主输出解析到最终能量。')
        return {'ok': False, 'kind': key, 'result': result, 'figure': None,
                'summary': '', 'files': files, 'error': error}
    if converged:
        summary = f'{eng.upper()} 最终能量={energy:.8f} eV，已见正常完成/收敛标志。'
    else:
        summary = (f'{eng.upper()} 最终能量={energy:.8f} eV；'
                   '尚未确认正常完成/收敛，请核对证据后再用于科学结论。')
    return {'ok': True, 'kind': key, 'result': result, 'figure': None,
            'summary': summary, 'files': files, 'error': None}


def analyze_vasp_outputs(job_dir, task_key: str) -> dict:
    """从 OSZICAR/OUTCAR 给出可复核的末步能量/力序列摘要。"""
    engine = job_engine(job_dir)
    if engine != 'vasp':
        return analyze_engine_outputs(job_dir, engine=engine, task_key=task_key)
    from vcstudio.cluster import convergence

    root = Path(job_dir)
    osz = _read(root / 'OSZICAR')
    out = _read(root / 'OUTCAR')
    series = convergence.convergence_series(osz, out or None)
    energies = list(series.get('E0') or [])
    if not energies:
        return {'ok': False, 'kind': normalize_task_key(task_key), 'result': None,
                'figure': None, 'summary': '', 'files': [],
                'error': '未从 OSZICAR 解析到能量；结果可能尚未下载完整。'}
    fmax = [value for value in (series.get('fmax') or []) if value is not None]
    clean_exit = 'General timing and accounting informations for this job:' in out
    ionic_mark = 'reached required accuracy' in out
    result = {'n_steps': len(energies), 'final_energy_e0_ev': energies[-1],
              'final_fmax_ev_a': fmax[-1] if fmax else None,
              'clean_exit': clean_exit, 'ionic_converged_marker': ionic_mark,
              'series': series}
    force_text = (f'，末步 |F|max={fmax[-1]:.4f} eV/Å' if fmax else '')
    summary = f'共 {len(energies)} 个离子/单点步，末步 E0={energies[-1]:.6f} eV{force_text}。'
    return {'ok': True, 'kind': normalize_task_key(task_key), 'result': result,
            'figure': None, 'summary': summary,
            'files': [str(root / name) for name in ('OSZICAR', 'OUTCAR')
                      if (root / name).is_file()], 'error': None}


def analyze_frequency(job_dir, *, temperature: float = 298.15) -> dict:
    """频率解析 + 虚频质量闸；没有频率行时明确失败。"""
    from vcstudio.project import thermo

    outcar = Path(job_dir) / 'OUTCAR'
    vib = thermo.analyze_outcar(outcar, temperature=temperature)
    if vib is None:
        return {'ok': False, 'kind': 'freq', 'result': None, 'figure': None,
                'summary': '', 'files': [], 'error': 'OUTCAR 缺失或未解析到频率行。'}
    gate = thermo.classify_imaginary(vib.imag_cm1, context='minimum')
    result = asdict(vib)
    result.update({'g_corr_ev': vib.g_corr_ev, 'imaginary_gate': gate})
    summary = (f'ZPE={vib.zpe_ev:.6f} eV，T·S={vib.ts_ev:.6f} eV，'
               f'G_corr={vib.g_corr_ev:.6f} eV；{gate["advice"]}')
    return {'ok': True, 'kind': 'freq', 'result': result, 'figure': None,
            'summary': summary, 'files': [str(outcar)], 'error': None}


def analyze_aimd(job_dir) -> dict:
    """AIMD 的真实能量/温度样本摘要；不凭短轨迹宣称“稳定”。"""
    from vcstudio.cluster import convergence

    root = Path(job_dir)
    text = _read(root / 'OSZICAR')
    steps = convergence.parse_oszicar(text)
    if not steps:
        return {'ok': False, 'kind': 'aimd', 'result': None, 'figure': None,
                'summary': '', 'files': [], 'error': 'OSZICAR 缺失或没有可解析的 AIMD 步。'}
    energies = [row['E0'] for row in steps]
    temperatures = []
    for match in _TEMP_RE.finditer(text):
        try:
            temperatures.append(float(match.group(1)))
        except ValueError:
            pass
    result = {'n_steps': len(steps), 'energy_first_ev': energies[0],
              'energy_last_ev': energies[-1],
              'energy_drift_total_ev': energies[-1] - energies[0],
              'temperature_samples_k': temperatures,
              'temperature_mean_k': (sum(temperatures) / len(temperatures)
                                     if temperatures else None)}
    temp_text = (f'，平均温度={result["temperature_mean_k"]:.1f} K'
                 if result['temperature_mean_k'] is not None else '')
    summary = (f'解析 {len(steps)} 步，首末能量漂移='
               f'{result["energy_drift_total_ev"]:+.6f} eV{temp_text}；'
               '该摘要不替代轨迹长度与结构完整性检查。')
    files = [str(root / name) for name in ('OSZICAR', 'XDATCAR')
             if (root / name).is_file()]
    return {'ok': True, 'kind': 'aimd', 'result': result, 'figure': None,
            'summary': summary, 'files': files, 'error': None}


def analyze_neb(job_dir) -> dict:
    """复用 NEB 解析器，返回能垒与质量闸；出图失败不吞掉数值。"""
    from vcstudio.project import neb

    try:
        data = neb.parse_neb_energies(job_dir)
    except (OSError, ValueError) as exc:
        return {'ok': False, 'kind': 'neb', 'result': None, 'figure': None,
                'summary': '', 'files': [], 'error': str(exc)}
    energies = list(data.get('energies') or [])
    if not energies or any(value is None for value in energies):
        return {'ok': False, 'kind': 'neb', 'result': data, 'figure': None,
                'summary': '', 'files': [], 'error': 'NEB image 能量尚未完整，无法计算能垒。'}
    gate = neb.neb_quality_gate(data)
    result = dict(data)
    result['quality_gate'] = gate
    fig = None
    try:
        paths = neb.neb_profile_plot(data, str(Path(job_dir) / 'neb_profile'))
        fig = paths[0] if paths else None
    except Exception:  # 可选 matplotlib 不应阻断数值解析
        fig = None
    forward = data.get('barrier_f')
    summary = (f'正向能垒={forward:.4f} eV；'
               if isinstance(forward, (int, float)) else '')
    summary += ('质量闸通过。' if gate.get('ok') else '；'.join(gate.get('issues') or []))
    return {'ok': True, 'kind': 'neb', 'result': result, 'figure': fig,
            'summary': summary, 'files': [fig] if fig else [], 'error': None}


def _manifest_summary(job_dir) -> dict:
    """报告只摘录任务身份与状态，不复制可能很大的/环境相关的完整 manifest。"""
    value = load_job_manifest(job_dir)
    keys = ('schema', 'job_id', 'system', 'task_type', 'calc_type', 'state',
            'created_at', 'updated_at', 'parent_job')
    summary = {key: value.get(key) for key in keys if value.get(key) is not None}
    inputs = value.get('inputs') or {}
    if isinstance(inputs, dict):
        selected = {key: inputs.get(key) for key in (
            'engine', 'task', 'gaussian_task', 'functional', 'periodic',
            'files', 'output_files', 'sha256', 'source_sha256', 'neb_endpoints')
                    if inputs.get(key) is not None}
        if selected:
            summary['inputs'] = selected
    results = value.get('results') or {}
    if isinstance(results, dict):
        selected = {key: results.get(key) for key in (
            'energy_e0_eV', 'energy_source', 'diagnosis', 'fetched',
            'fetched_missing', 'fetched_at') if results.get(key) is not None}
        if selected:
            summary['results'] = selected
    return summary


def write_trace_report(job_dir, analysis: dict, out_path, *, task_key: str | None = None) -> str:
    """原子写入 HTML/Markdown/JSON 通用报告；分析失败也会如实写入失败原因。"""
    root = Path(job_dir).resolve()
    if not root.is_dir():
        raise ValueError('作业目录不存在')
    target = Path(out_path).expanduser().resolve()
    if target.suffix.lower() not in ('.html', '.htm', '.md', '.json'):
        target = target.with_suffix('.html')
    key = normalize_task_key(task_key or analysis.get('kind'))
    payload = {
        'schema': 'vcstudio.task-report.v1',
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'job_dir': str(root), 'task_key': key or None,
        'capability': capability(key), 'manifest': _manifest_summary(root),
        'analysis': analysis, 'evidence': collect_evidence(root),
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    suffix = target.suffix.lower()
    if suffix == '.json':
        body = encoded + '\n'
    elif suffix == '.md':
        body = (f'# DFT 任务报告：{key or "未识别任务"}\n\n'
                f'- 生成时间：{payload["generated_at"]}\n'
                f'- 作业目录：`{root}`\n'
                f'- 分析状态：{payload["capability"]["analysis_status"]}\n\n'
                f'## 可复核数据\n\n```json\n{encoded}\n```\n')
    else:
        title = f'DFT 任务报告：{key or "未识别任务"}'
        body = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
                f'<title>{html.escape(title)}</title><style>'
                'body{font:15px/1.6 system-ui;max-width:1080px;margin:36px auto;padding:0 24px}'
                'pre{white-space:pre-wrap;background:#f5f6f8;padding:18px;border-radius:10px}'
                '</style></head><body>'
                f'<h1>{html.escape(title)}</h1><p>{html.escape(str(analysis.get("summary") or analysis.get("error") or "无摘要"))}</p>'
                '<h2>可复核数据与文件指纹</h2>'
                f'<pre>{html.escape(encoded)}</pre></body></html>')
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f'.{target.name}.', dir=str(target.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(body)
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return str(target)
