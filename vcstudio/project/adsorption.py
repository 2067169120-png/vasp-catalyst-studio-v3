"""吸附能项目:清洁表面 + N 个吸附构型 + 气相参考 → 批量生成、ΔE 汇总、CSV 导出。

一个吸附能数据点天然是一组作业:ΔE_ads = E(slab+ads) − E(slab) − E(ref)。
本模块把"组"落成 project.yaml(成员目录 + 元信息),能量一律从各成员 job.yaml 的
results.energy_e0_eV 现读(单一真相源);**任一成员未 DONE 时绝不给 ΔE**(防拿错数)。
导出用 CSV(utf-8-sig,Excel 直接打开中文不乱码;零新依赖,不增 EXE 体积)。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path

import yaml

from vcstudio.cluster import diagnose, ledger
from vcstudio.generate import methods_text, potcar
from vcstudio.generate.incar_builder import parse_incar
from vcstudio.generate.job_builder import build_job_dir
from vcstudio.generate.poscar import parse_poscar_species, read_poscar
from vcstudio.project import structure_identity
from vcstudio.shared import manifest as manifest_mod
from vcstudio.shared.config import user_config_dir

PROJECT_NAME = 'project.yaml'
_STEM_RE = re.compile(r'[^A-Za-z0-9_.-]')
# 化学式样 token:元素符号([A-Z][a-z]?)+ 可选计数,连缀成整式(Li2S4 / CO / OH / Fe2O3)。
# 与 freeenergy._species_in_name 同一 token 观:大写起头、贴着数字算计数、遇分隔即断。
_SPECIES_TOKEN_RE = re.compile(r'[A-Z][a-z]?\d*(?:[A-Z][a-z]?\d*)*')
_STRUCTURE_NAMES = {'poscar', 'contcar'}
_STRUCTURE_SUFFIXES = {'.vasp', '.poscar'}
_VASP_INPUT_NAMES = ('POSCAR', 'INCAR', 'KPOINTS', 'POTCAR')
# project.yaml 的 species_refs 只是便于展示/迁移的缓存，不是计算真相源。缓存与
# reference job.yaml 的差异超过 1 μeV 时 fail-closed，避免旧缓存静默污染 ΔE。
SPECIES_REF_CACHE_TOLERANCE_EV = 1e-6


def _config_species(member_name: str, proj_name: str) -> str:
    """构型成员名 → 吸附物种短名(多构型取最稳的分组键)。

    剥 '{项目名}_ads_' 前缀(缺则退剥到 '_ads_' 之后)得短名,再取短名里**首个化学式样
    token** 作物种(如 Li2S4_top→Li2S4、CO_fcc→CO);识别不出(如 h1/b2 无大写起头)用短名。
    """
    short = member_name
    prefix = f'{proj_name}_ads_'
    if proj_name and member_name.startswith(prefix):
        short = member_name[len(prefix):]
    else:
        i = member_name.find('_ads_')
        if i != -1:
            short = member_name[i + len('_ads_'):]
    m = _SPECIES_TOKEN_RE.search(short)
    return m.group(0) if m else short


def _path_keys(path, project_root=None) -> set[str]:
    """Return stable lookup keys for a project member path.

    Imported projects persist ``config_species`` by member path.  Paths may be
    absolute or relative and may contain harmless ``.``/``..`` segments, so a
    literal dictionary lookup is not sufficient after a project is moved or
    reloaded on a case-insensitive platform.
    """
    try:
        raw = os.fspath(path)
    except TypeError:
        return set()
    raw = os.path.expanduser(str(raw))
    keys = {raw, os.path.normcase(os.path.normpath(raw))}
    rooted = raw
    if project_root and not os.path.isabs(rooted):
        rooted = os.path.join(os.fspath(project_root), rooted)
    keys.add(os.path.normcase(os.path.abspath(os.path.normpath(rooted))))
    return keys


def _config_species_index(project: dict) -> dict[str, str]:
    """Normalise explicit imported-member → species mappings for lookup."""
    root = project.get('root')
    index: dict[str, str] = {}
    for member, species in dict(project.get('config_species') or {}).items():
        value = str(species or '').strip()
        if not value:
            continue
        for key in _path_keys(member, root):
            index[key] = value
    return index


def _stem(path: str) -> str:
    """文件名(去扩展名)→ 目录名安全形式。"""
    source = Path(path)
    s = source.stem
    # A folder import commonly contains many files all named POSCAR.  Use the
    # containing folder as the member label so they do not overwrite each
    # other inside one adsorption project.
    if s.casefold() in _STRUCTURE_NAMES and source.parent.name:
        s = source.parent.name
    s = _STEM_RE.sub('_', s) or 'config'
    return s


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def _structure_species_hint(path: Path) -> str:
    """Best-effort adsorbate/reference species hint from a structure path.

    A full slab composition cannot reveal which atoms are the adsorbate.  The
    folder/file naming convention (``Li2S8_top/POSCAR`` or
    ``Li2S8_top.vasp``) is therefore the only safe automatic hint; callers
    still show the value for confirmation before creating a project.
    """
    stem = path.stem
    candidates = []
    if stem.casefold() not in _STRUCTURE_NAMES:
        candidates.append(stem)
    candidates.append(path.parent.name)
    for value in candidates:
        for token in _SPECIES_TOKEN_RE.findall(value):
            if token.casefold() not in _STRUCTURE_NAMES:
                return token
    return ''


def scan_structure_files(root: str | os.PathLike) -> list[dict]:
    """Recursively discover ordinary VASP structure files without writes.

    Recognised files are exact ``POSCAR``/``CONTCAR`` names and files ending
    in ``.vasp``/``.poscar`` (case-insensitive).  Symlink files/directories are
    ignored and hard-linked duplicates are returned once.
    """
    base = Path(root).expanduser()
    if not base.is_dir():
        raise ValueError('结构根目录不存在')
    if base.is_symlink():
        raise ValueError('结构根目录不能是符号链接')
    base = base.resolve()
    found: list[dict] = []
    seen: set[tuple] = set()
    for current, dirnames, filenames in os.walk(base, followlinks=False):
        current_path = Path(current)
        dirnames[:] = sorted(
            (d for d in dirnames if not (current_path / d).is_symlink()),
            key=str.casefold,
        )
        exact_structures = {
            filename.casefold(): current_path / filename
            for filename in filenames if filename.casefold() in _STRUCTURE_NAMES
        }
        selected_exact = None
        selection_note = None
        if len(exact_structures) == 1:
            selected_exact = next(iter(exact_structures.values()))
        else:
            # This scanner supplies *new calculation inputs*.  A CONTCAR left
            # beside POSCAR may belong to an earlier relaxation, so POSCAR is
            # authoritative here.  Result import has a separate scanner that
            # deliberately keeps the opposite (CONTCAR-first) policy.
            for wanted in ('poscar', 'contcar'):
                candidate = exact_structures.get(wanted)
                if not candidate or candidate.is_symlink():
                    continue
                try:
                    parse_poscar_species(read_poscar(candidate))
                except (OSError, ValueError):
                    continue
                selected_exact = candidate
                if wanted == 'poscar' and 'contcar' in exact_structures:
                    selection_note = (
                        '同目录同时有 POSCAR/CONTCAR；这是本次计算输入，'
                        '已优先选 POSCAR，避免误用旧 CONTCAR')
                elif wanted == 'contcar' and 'poscar' in exact_structures:
                    selection_note = 'POSCAR 不可解析，已回退到 CONTCAR'
                break
        for filename in sorted(filenames, key=str.casefold):
            path = current_path / filename
            if path.is_symlink() or not path.is_file():
                continue
            low = filename.casefold()
            if low not in _STRUCTURE_NAMES and path.suffix.casefold() not in _STRUCTURE_SUFFIXES:
                continue
            if low in _STRUCTURE_NAMES and path != selected_exact:
                continue
            try:
                stat = path.stat()
                identity = ('inode', stat.st_dev, stat.st_ino)
                if not stat.st_ino:
                    identity = ('path', os.path.normcase(str(path.resolve())))
            except OSError:
                continue
            if identity in seen:
                continue
            seen.add(identity)
            item = {
                'path': str(path.resolve()),
                'name': filename,
                'species': _structure_species_hint(path),
            }
            item['structure'] = structure_identity.poscar_facts(path)
            quartet = _structure_input_bundle(path)
            item['quartet'] = quartet
            # Flat aliases keep the scanner convenient for existing GUI/API
            # consumers while ``quartet`` remains the authoritative record.
            item['mode'] = quartet['mode']
            item['status'] = quartet['status']
            item['issues'] = list(quartet['issues'])
            item['files'] = quartet['files']
            item['species_source'] = 'path_name' if item['species'] else 'unresolved'
            item['species_confidence'] = 'hint' if item['species'] else 'unknown'
            item['species_confirmed'] = False
            if selection_note:
                item['selection_note'] = selection_note
            found.append(item)
    found.sort(key=lambda item: os.path.relpath(item['path'], base).casefold())
    return found


def _inspect_incar_file(path, *, source: str) -> dict:
    """Read-only validation/provenance for one explicitly selected INCAR."""
    raw_path = str(path or '').strip()
    requested = Path(raw_path).expanduser() if raw_path else None
    resolved = str(requested.absolute()) if requested is not None else ''
    issues: list[str] = []
    digest = ''
    status = 'ready'
    selected = requested
    if selected is None:
        status = 'missing'
        issues.append(f'INCAR 不存在：{resolved}' if resolved else '同目录缺少 INCAR')
    elif selected.is_symlink():
        status = 'invalid'
        issues.append('INCAR 不能是符号链接')
    elif not selected.exists():
        status = 'missing'
        issues.append(f'INCAR 不存在：{resolved}')
    elif not selected.is_file():
        status = 'invalid'
        issues.append(f'INCAR 不是普通文件：{resolved}')
    else:
        selected = selected.resolve()
        resolved = str(selected)
        try:
            text = selected.read_text(encoding='utf-8', errors='replace')
            digest = manifest_mod.sha256_file(selected)
        except OSError as exc:
            status = 'invalid'
            issues.append(f'INCAR 无法读取：{exc}')
            text = ''
        if status == 'ready':
            if not text.strip():
                status = 'invalid'
                issues.append('INCAR 为空')
            parsed = parse_incar(text)
            if not parsed:
                status = 'invalid'
                issues.append('INCAR 未解析到任何 KEY=VALUE 参数')
            if parsed:
                def _number(value):
                    if isinstance(value, bool):
                        return None
                    try:
                        number = float(value)
                    except (TypeError, ValueError):
                        return None
                    return number if math.isfinite(number) else None

                def _integer(value):
                    number = _number(value)
                    return int(number) if number is not None and number == int(number) else None

                if 'ENCUT' in parsed:
                    encut = _number(parsed.get('ENCUT'))
                    if encut is None or encut <= 0:
                        status = 'invalid'
                        issues.append(f'ENCUT={parsed.get("ENCUT")!r} 不是正数')
                if 'ISPIN' in parsed and _integer(parsed.get('ISPIN')) not in {1, 2}:
                    status = 'invalid'
                    issues.append(f'ISPIN={parsed.get("ISPIN")!r} 无效（仅允许 1 或 2）')
                for key in ('NSW', 'IBRION', 'NELM', 'ISIF'):
                    if key in parsed and _integer(parsed.get(key)) is None:
                        status = 'invalid'
                        issues.append(f'{key}={parsed.get(key)!r} 必须是整数')
    return {
        'path': resolved, 'incar_path': resolved,
        'sha256': digest, 'incar_sha256': digest,
        'status': status, 'incar_status': status,
        'source': source,
        'issues': issues, 'incar_issues': list(issues),
    }


def resolve_structure_quartet(structure_path) -> dict:
    """Resolve one structure's same-directory VASP quartet without writes.

    A complete, unique, ordinary-file ``POSCAR/INCAR/KPOINTS/POTCAR`` quartet is
    validated without writes and marked ``copy``.  Partial folders remain
    ``generate`` so the established ``build_job_dir`` path can fill the missing
    inputs.  A complete-but-invalid or ambiguous quartet is ``blocked`` rather
    than silently discarded and regenerated.
    """
    raw_structure = str(structure_path or '').strip()
    if not raw_structure:
        return {
            'status': 'invalid', 'mode': 'blocked', 'files': {},
            'paths': {}, 'sha256': {}, 'missing': list(_VASP_INPUT_NAMES),
            'issues': ['结构文件路径为空'],
        }
    structure = Path(raw_structure).expanduser().absolute()
    if structure.is_symlink():
        return {
            'status': 'invalid', 'mode': 'blocked', 'files': {},
            'paths': {}, 'sha256': {}, 'missing': list(_VASP_INPUT_NAMES),
            'issues': [f'结构文件不能是符号链接：{structure}'],
        }
    if not structure.is_file():
        return {
            'status': 'invalid', 'mode': 'blocked', 'files': {},
            'paths': {}, 'sha256': {}, 'missing': list(_VASP_INPUT_NAMES),
            'issues': [f'结构文件不存在或不是普通文件：{structure}'],
        }
    structure = structure.resolve()
    folder = structure.parent
    candidates: dict[str, list[Path]] = {name: [] for name in _VASP_INPUT_NAMES}
    issues: list[str] = []
    try:
        entries = sorted(folder.iterdir(), key=lambda item: item.name.casefold())
    except OSError as exc:
        return {
            'status': 'invalid', 'mode': 'blocked', 'files': {},
            'missing': list(_VASP_INPUT_NAMES),
            'issues': [f'无法读取四件套目录 {folder}：{exc}'],
        }
    canonical = {name.casefold(): name for name in _VASP_INPUT_NAMES}
    for entry in entries:
        name = canonical.get(entry.name.casefold())
        if name is None:
            continue
        if entry.is_symlink():
            issues.append(f'{name} 不能是符号链接：{entry}')
            continue
        if not entry.is_file():
            issues.append(f'{name} 不是普通文件：{entry}')
            continue
        candidates[name].append(entry)

    files: dict[str, dict] = {}
    for name in _VASP_INPUT_NAMES:
        matches = candidates[name]
        if len(matches) > 1:
            issues.append(
                f'同目录存在多个大小写不同的 {name}：'
                + '、'.join(item.name for item in matches))
            continue
        if not matches:
            continue
        selected = matches[0].resolve()
        try:
            digest = manifest_mod.sha256_file(selected)
        except OSError as exc:
            issues.append(f'{name} 无法读取：{exc}')
            continue
        files[name] = {'path': str(selected), 'sha256': digest}

    missing = [name for name in _VASP_INPUT_NAMES if name not in files]
    selected_poscar = files.get('POSCAR', {}).get('path')
    structure_is_quartet_poscar = bool(
        selected_poscar and os.path.normcase(os.path.realpath(selected_poscar))
        == os.path.normcase(os.path.realpath(structure)))

    complete = not missing
    validation = []
    if complete:
        from vcstudio.project.result_import import validate_vasp_quartet
        validation = validate_vasp_quartet(folder)
        issues.extend(validation)

    if issues:
        mode, status = 'blocked', 'invalid'
    elif complete and structure_is_quartet_poscar:
        mode, status = 'copy', 'ready'
    else:
        # A partial folder (or a .vasp file beside another POSCAR) stays on the
        # established generation path.  INCAR availability is resolved later,
        # where an explicit per-member mapping or the legacy fallback can be
        # considered.  Treating a missing local INCAR as a quartet error here
        # would incorrectly block those supported sources before that step.
        mode, status = 'generate', 'generatable'
    paths = {name: record['path'] for name, record in files.items()}
    hashes = {name: record['sha256'] for name, record in files.items()}
    return {
        'status': status, 'mode': mode, 'files': files,
        'paths': paths, 'sha256': hashes,
        'missing': missing, 'issues': list(dict.fromkeys(issues)),
    }


def _structure_input_bundle(structure_path) -> dict:
    """Backward-compatible private alias for :func:`resolve_structure_quartet`."""
    return resolve_structure_quartet(structure_path)


def resolve_structure_incar(structure_path, fallback='') -> dict:
    """Resolve the INCAR belonging to one structure without guessing across folders.

    A unique, case-insensitive ``INCAR`` in the structure's own directory always
    wins.  An explicitly supplied fallback is considered only when that directory
    contains no INCAR at all; it never masks an empty/invalid file or a case-name
    conflict.  The returned path/hash pair lets callers detect scan-to-build races.
    """
    raw_structure = str(structure_path or '').strip()
    if not raw_structure:
        return {
            'path': '', 'incar_path': '', 'sha256': '', 'incar_sha256': '',
            'status': 'invalid', 'incar_status': 'invalid', 'source': '',
            'issues': ['结构文件路径为空'], 'incar_issues': ['结构文件路径为空'],
        }
    structure = Path(raw_structure).expanduser().resolve()
    if not structure.is_file():
        issue = f'结构文件不存在：{structure}'
        return {
            'path': '', 'incar_path': '', 'sha256': '', 'incar_sha256': '',
            'status': 'invalid', 'incar_status': 'invalid', 'source': '',
            'issues': [issue], 'incar_issues': [issue],
        }
    try:
        candidates = sorted(
            (item for item in structure.parent.iterdir()
             if item.name.casefold() == 'incar'),
            key=lambda item: item.name.casefold(),
        )
    except OSError as exc:
        issue = f'无法读取结构目录 {structure.parent}：{exc}'
        return {
            'path': '', 'incar_path': '', 'sha256': '', 'incar_sha256': '',
            'status': 'invalid', 'incar_status': 'invalid', 'source': 'same_directory',
            'issues': [issue], 'incar_issues': [issue],
        }
    if len(candidates) > 1:
        names = '、'.join(item.name for item in candidates)
        issue = f'同目录存在多个大小写不同的 INCAR（{names}），无法安全选择'
        return {
            'path': '', 'incar_path': '', 'sha256': '', 'incar_sha256': '',
            'status': 'ambiguous', 'incar_status': 'ambiguous',
            'source': 'same_directory', 'issues': [issue], 'incar_issues': [issue],
        }
    if candidates:
        return _inspect_incar_file(candidates[0], source='same_directory')
    if str(fallback or '').strip():
        return _inspect_incar_file(fallback, source='explicit_fallback')
    issue = f'{structure.parent} 同目录缺少 INCAR'
    return {
        'path': '', 'incar_path': '', 'sha256': '', 'incar_sha256': '',
        'status': 'missing', 'incar_status': 'missing', 'source': 'same_directory',
        'issues': [issue], 'incar_issues': [issue],
    }


def scan_lis_input_bundle(root: str | os.PathLike, reference_species=None) -> dict:
    """只读扫描一整套 Li-S 吸附输入，绑定逐目录 INCAR/slab/config。

    每个结构只绑定自己目录内唯一的 INCAR；不同目录各有 INCAR 是正常情况，
    不再被当作全局歧义。只有同一目录出现大小写冲突、INCAR 空/无键，或结构
    缺少 INCAR 时才标出问题。带 ``Li2Sx``/``S8`` 物种提示的目录不会因名字
    含 ``slab`` 被误判为清洁表面。
    """
    base = Path(root).expanduser()
    if not base.is_dir():
        raise ValueError('本次计算文件夹不存在')
    if base.is_symlink():
        raise ValueError('本次计算文件夹不能是符号链接')
    base = base.resolve()
    structures = scan_structure_files(base)
    for item in structures:
        resolution = resolve_structure_incar(item['path'])
        item.update({
            'incar_path': resolution['path'],
            'incar_sha256': resolution['sha256'],
            'incar_status': resolution['status'],
            'incar_issues': list(resolution['issues']),
            'incar_source': resolution['source'],
        })

    incar_candidates = []
    for current, dirnames, filenames in os.walk(base, followlinks=False):
        current_path = Path(current)
        dirnames[:] = sorted(
            (name for name in dirnames if not (current_path / name).is_symlink()),
            key=str.casefold,
        )
        for filename in sorted(filenames, key=str.casefold):
            path = current_path / filename
            if filename.casefold() == 'incar' and path.is_file() and not path.is_symlink():
                incar_candidates.append(str(path.resolve()))
    incar_candidates.sort(key=lambda path: os.path.relpath(path, base).casefold())

    def _is_lis_species(value):
        return bool(re.fullmatch(r'(?:Li\d*)?S\d*', str(value or ''), flags=re.I))

    def _clean_score(item):
        path = Path(item['path'])
        rel = path.relative_to(base).as_posix().casefold()
        if _is_lis_species(item.get('species')):
            return 0
        if re.search(r'(^|[/_. -])(?:clean[_. -]?slab|clean|bare|pristine)(?=$|[/_. -])', rel):
            return 100
        if re.search(r'(^|[/_. -])slab(?=$|[/_. -])', rel):
            return 60
        return 0

    scored = [(item, _clean_score(item)) for item in structures]
    clean_candidates = [item for item, score in scored if score > 0]
    clean_candidates.sort(
        key=lambda item: (-_clean_score(item), os.path.relpath(item['path'], base).casefold()))
    clean_slab = clean_candidates[0]['path'] if len(clean_candidates) == 1 else ''
    clean_inference = {'path': clean_slab, 'confidence': 'name' if clean_slab else 'unknown'}
    if not clean_slab:
        structural_clean = structure_identity.infer_clean_candidate(structures)
        named_paths = {item['path'] for item in clean_candidates}
        if (structural_clean.get('path') and (
                not named_paths or structural_clean['path'] in named_paths)):
            clean_slab = structural_clean['path']
            clean_inference = structural_clean
            clean_candidates = [
                item for item in structures if item['path'] == clean_slab
            ]
    excluded = {item['path'] for item in clean_candidates}
    if clean_slab:
        excluded = {clean_slab}
    configs = [dict(item) for item in structures if item['path'] not in excluded]
    if clean_slab:
        for item in configs:
            assignment = structure_identity.classify_against_clean(
                clean_slab, item['path'], name_hint=item.get('species') or '',
                reference_species=reference_species)
            item['assignment'] = assignment
            item['species'] = assignment['species']
            item['species_source'] = assignment['source']
            item['species_confidence'] = assignment['confidence']
            item['species_confirmed'] = bool(assignment['confirmed'])
            item['adsorbate_composition'] = assignment['composition']

    root_incars = [path for path in incar_candidates if Path(path).parent == base]
    root_incar = ''
    if len(root_incars) == 1:
        root_resolution = _inspect_incar_file(root_incars[0], source='explicit_fallback')
        if root_resolution['status'] == 'ready':
            root_incar = root_resolution['path']

    warnings = []
    if len(root_incars) > 1:
        warnings.append('输入根目录存在多个大小写不同的 INCAR；不能作为显式共享后备')
    if not structures:
        warnings.append('未找到 POSCAR / CONTCAR / .vasp 结构文件')
    elif not clean_candidates:
        warnings.append('未能可靠识别 clean slab；请使用“选择 POSCAR”指定清洁表面')
    elif len(clean_candidates) > 1:
        warnings.append(
            f'找到 {len(clean_candidates)} 个 clean/slab 候选；请手动选择正确的清洁表面')
    if clean_slab and clean_inference.get('confidence') == 'exact':
        warnings.append(
            f'已通过 POSCAR 晶格与组成关系识别 clean slab，并据此整理 '
            f'{sum(item.get("species_confidence") == "exact" for item in configs)} 个构型物种')
    for item in configs:
        warnings.extend(
            f'{Path(item["path"]).parent.name or Path(item["path"]).name}: {message}'
            for message in ((item.get('assignment') or {}).get('warnings') or []))
    for item in structures:
        if item.get('incar_status') != 'ready':
            label = Path(item['path']).parent.name or Path(item['path']).name
            warnings.extend(f'{label}: {message}' for message in item.get('incar_issues') or [])
        quartet = item.get('quartet') or {}
        if quartet.get('status') == 'invalid' or quartet.get('mode') == 'blocked':
            label = Path(item['path']).parent.name or Path(item['path']).name
            warnings.extend(
                f'{label}: 四件套不可用：{message}'
                for message in quartet.get('issues') or [])
    clean_item = next((item for item in structures if item['path'] == clean_slab), None)
    clean_incar = (clean_item or {}).get('incar_path', '')
    clean_incar_sha256 = (clean_item or {}).get('incar_sha256', '')
    clean_incar_status = (clean_item or {}).get('incar_status', 'missing')
    clean_incar_issues = list((clean_item or {}).get('incar_issues') or [])
    clean_incar_source = (clean_item or {}).get('incar_source', '')
    clean_quartet = dict((clean_item or {}).get('quartet') or {})
    return {
        'root': str(base),
        # Legacy shared-INCAR field now means only a unique root-level, explicit
        # fallback.  Per-member fields below are the authoritative binding.
        'incar': root_incar,
        'incar_candidates': incar_candidates,
        'clean_slab': clean_slab,
        'clean_incar': clean_incar,
        'clean_incar_sha256': clean_incar_sha256,
        'clean_incar_status': clean_incar_status,
        'clean_incar_issues': clean_incar_issues,
        'clean_incar_source': clean_incar_source,
        'clean_quartet': clean_quartet,
        'clean_mode': clean_quartet.get('mode', ''),
        'clean_status': clean_quartet.get('status', 'missing'),
        'clean_issues': list(clean_quartet.get('issues') or []),
        'clean_candidates': clean_candidates,
        'clean_inference': clean_inference,
        'configs': configs,
        'structures': structures,
        'species_groups': structure_identity.species_groups(configs),
        'unresolved_species': sum(not item.get('species') for item in configs),
        'warnings': warnings,
        'source_read_only': True,
    }


def identify_config_species(clean_slab, structures, reference_species=None) -> dict:
    """Enrich scanned structures using auditable config-clean composition differences."""
    clean = str(clean_slab or '').strip()
    items = [dict(item) if isinstance(item, dict) else {'path': str(item)}
             for item in (structures or [])]
    warnings = []
    if not clean:
        return {'items': items, 'species_groups': structure_identity.species_groups(items),
                'warnings': ['尚未指定 clean slab；物种仅按文件夹/文件名建议，需人工确认']}
    enriched = []
    for item in items:
        if os.path.normcase(os.path.abspath(str(item.get('path') or ''))) == \
                os.path.normcase(os.path.abspath(clean)):
            continue
        assignment = structure_identity.classify_against_clean(
            clean, item.get('path'), name_hint=item.get('species') or '',
            reference_species=reference_species)
        item.update({
            'assignment': assignment, 'species': assignment['species'],
            'species_source': assignment['source'],
            'species_confidence': assignment['confidence'],
            'species_confirmed': bool(assignment['confirmed']),
            'adsorbate_composition': assignment['composition'],
        })
        enriched.append(item)
        warnings.extend(
            f'{Path(str(item.get("path") or "")).parent.name}: {message}'
            for message in assignment.get('warnings') or [])
    return {'items': enriched, 'species_groups': structure_identity.species_groups(enriched),
            'warnings': warnings}


# ── 项目创建(批量生成) ───────────────────────────────────────────────────────
def _unified_encut(incar_paths, poscars: list, lib_root,
                   incar_overrides: dict | None = None) -> int | None:
    """Resolve a safe group ENCUT from the actual per-member INCAR files.

    All members lacking ENCUT get one value from the union of their elements.
    If some members explicitly use one common ENCUT, missing members inherit it.
    Different explicit values are preserved per member.  They make the later
    total-energy comparison non-final, but do not make either VASP job itself
    invalid and therefore must not block project generation.
    """
    paths = [incar_paths] if isinstance(incar_paths, (str, os.PathLike)) \
        else list(incar_paths or [])
    overrides = {
        os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path)))):
        {str(key).upper(): value for key, value in dict(values or {}).items()}
        for path, values in dict(incar_overrides or {}).items()
    }
    explicit: list[tuple[str, float]] = []
    missing = 0
    for path in paths:
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as handle:
                incar = parse_incar(handle.read())
        except OSError as exc:
            raise ValueError(f'无法读取成员 INCAR {path}：{exc}') from exc
        incar.update(overrides.get(
            os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path)))), {}))
        if 'ENCUT' not in incar:
            missing += 1
            continue
        raw = incar.get('ENCUT')
        try:
            if isinstance(raw, bool):
                raise ValueError
            value = float(raw)
            if not math.isfinite(value) or value <= 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ValueError(f'成员 INCAR {path} 的 ENCUT={raw!r} 不是正数') from exc
        explicit.append((str(Path(path).resolve()), value))
    if explicit:
        baseline = explicit[0][1]
        conflicts = [(path, value) for path, value in explicit
                     if abs(value - baseline) > 1e-9]
        if conflicts:
            return None
        if not missing:
            return None
        if baseline != int(baseline):
            return None
        return int(baseline)
    union: set = set()
    for p in poscars:
        try:
            els, _ = parse_poscar_species(read_poscar(p))
            union.update(els)
        except (OSError, ValueError):
            continue
    if not union:
        return None
    try:
        mx = potcar.max_enmax(sorted(union), lib_root)
    except Exception:            # noqa: BLE001  库缺失/元素未登记 → 回落逐成员
        return None
    return int(math.ceil(1.3 * mx / 50.0) * 50)


def create_project(root: str | os.PathLike, name: str, *,
                   clean_poscar: str, config_poscars: list,
                   incar_path: str = '', ref_poscar: str | None = None,
                   lib_root: str | None = None, validate: bool = True,
                   kpoints=None, config_species: dict | None = None,
                   config_species_evidence: dict | None = None,
                   species_refs: dict | None = None,
                   species_ref_jobs: dict | None = None,
                   molecules_dir: str | None = None,
                   reference_project: str | None = None,
                   preparation: dict | None = None,
                   fail_if_exists: bool = False,
                   member_incars: dict | None = None,
                   member_source_evidence: dict | None = None,
                   member_incar_patches: dict | None = None,
                   member_quartets: dict | None = None,
                   member_input_bundles: dict | None = None) -> dict:
    """批量生成 清洁表面 + 构型族 + (可选)气相参考,写 project.yaml 并登记台账。

    Returns:
        {'ok', 'project_path', 'project', 'generated': [(member, dir, warnings)],
         'errors': [(member, msg)]}。清洁表面生成失败 → 整体失败(其能量是公式必需项);
        个别构型失败只记入 errors,不拖垮全组。
    """
    # Reject composition aliases before creating any job directories.  VASP
    # composition can prove stoichiometry but cannot choose between two
    # same-composition references (isomer, charge/spin state, or mere alias).
    reference_labels = dict(species_ref_jobs or {})
    for label in dict(species_refs or {}):
        reference_labels.setdefault(label, None)
    structure_identity.build_dataset_groups([], {}, reference_labels)

    def _source_key(path) -> str:
        return os.path.normcase(os.path.realpath(os.path.abspath(
            os.path.expanduser(os.fspath(path)))))

    mapped_incars = {}
    for source, selected in dict(member_incars or {}).items():
        if isinstance(selected, dict):
            selected = selected.get('path') or selected.get('incar_path') or ''
        mapped_incars[_source_key(source)] = str(selected or '').strip()

    mapped_source_evidence = {}
    for source, evidence in dict(member_source_evidence or {}).items():
        if not isinstance(evidence, dict):
            raise ValueError(f'成员 {source} 的源文件哈希证据格式无效')
        mapped_source_evidence[_source_key(source)] = dict(evidence)

    mapped_incar_patches = {}
    for source, raw_patches in dict(member_incar_patches or {}).items():
        if not isinstance(raw_patches, dict):
            raise ValueError(f'成员 {source} 的 INCAR 智能修复格式无效')
        patches = {}
        for raw_key, value in raw_patches.items():
            key = str(raw_key or '').strip().upper()
            # The repair planner currently marks only an upward ENCUT alignment
            # as mechanically safe.  Magnetic initialisation and other physics
            # choices must remain explicit user decisions.
            if key != 'ENCUT':
                raise ValueError(
                    f'成员 {source} 的 {key or "INCAR"} 不是可自动应用的低风险修复')
            if isinstance(value, bool):
                raise ValueError(f'成员 {source} 的 ENCUT 修复值无效：{value!r}')
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f'成员 {source} 的 ENCUT 修复值无效：{value!r}') from exc
            if not math.isfinite(number) or number <= 0:
                raise ValueError(f'成员 {source} 的 ENCUT 修复值必须为正数')
            patches[key] = int(number) if number == int(number) else number
        if patches:
            mapped_incar_patches[_source_key(source)] = patches
    if mapped_incar_patches and not fail_if_exists:
        raise ValueError(
            'INCAR 智能修复只允许原子 staging 发布；请启用 fail_if_exists，'
            '避免修复失败后留下半成品项目')

    supplied_bundles = dict(member_input_bundles or {})
    for source, quartet in dict(member_quartets or {}).items():
        if source in supplied_bundles and supplied_bundles[source] != quartet:
            raise ValueError(f'成员 {source} 的四件套绑定重复且不一致')
        supplied_bundles[source] = quartet
    mapped_input_bundles = {}
    for source, raw_bundle in supplied_bundles.items():
        if not isinstance(raw_bundle, dict):
            raise ValueError(f'成员 {source} 的四件套绑定格式无效')
        bundle = raw_bundle.get('quartet') \
            if isinstance(raw_bundle.get('quartet'), dict) else raw_bundle
        mapped_input_bundles[_source_key(source)] = dict(bundle)

    def _source_evidence_part(evidence, kind):
        nested = evidence.get(kind)
        if not isinstance(nested, dict):
            nested = evidence.get(str(kind).upper())
        if isinstance(nested, dict):
            return (str(nested.get('path') or '').strip(),
                    str(nested.get('sha256') or '').strip().lower())
        return (str(evidence.get(f'{kind}_path') or '').strip(),
                str(evidence.get(f'{kind}_sha256') or '').strip().lower())

    def _normalise_member_bundle(label, poscar):
        """Re-scan a bundle and reject stale browser/scan evidence."""
        current = _structure_input_bundle(poscar)
        supplied = mapped_input_bundles.get(_source_key(poscar))
        if supplied is None:
            # ``create_project`` predates same-directory quartet binding and is
            # also used by the desktop/manual workflow, where ``incar_path`` is
            # an explicit user choice.  Only an explicit bundle opts a member
            # into byte-for-byte quartet copying; otherwise preserve the
            # established POSCAR + selected-INCAR generation contract instead
            # of silently letting a neighbouring INCAR override that choice.
            return {
                'mode': 'generate', 'input_mode': 'generate',
                'status': 'generatable', 'quartet_status': 'generatable',
                'files': {}, 'paths': {}, 'sha256': {},
                'missing': [], 'issues': [],
            }
        supplied_mode = str(supplied.get('mode') or '').strip().lower()
        if supplied_mode and supplied_mode != current['mode']:
            raise ValueError(
                f'{label} 的四件套模式在扫描后已变化'
                f'（{supplied_mode} → {current["mode"]}）；请重新扫描')
        supplied_files = supplied.get('files') or {}
        if not isinstance(supplied_files, dict):
            raise ValueError(f'{label} 的四件套文件证据格式无效')
        if current['mode'] == 'copy' and set(supplied_files) != set(_VASP_INPUT_NAMES):
            raise ValueError(f'{label} 的完整四件套缺少文件哈希证据；请重新扫描')
        for name, record in supplied_files.items():
            canonical = str(name).upper()
            if canonical not in _VASP_INPUT_NAMES or not isinstance(record, dict):
                raise ValueError(f'{label} 的四件套包含无效文件记录：{name}')
            actual = current['files'].get(canonical) or {}
            expected_path = str(record.get('path') or '').strip()
            expected_hash = str(record.get('sha256') or '').strip().lower()
            if expected_path and _source_key(expected_path) != _source_key(actual.get('path', '')):
                raise ValueError(f'{label} 的源 {canonical} 路径在扫描后已变化；请重新扫描')
            if not re.fullmatch(r'[0-9a-f]{64}', expected_hash):
                raise ValueError(f'{label} 的源 {canonical} 缺少有效 SHA256 证据；请重新扫描')
            if expected_hash != str(actual.get('sha256') or '').lower():
                raise ValueError(
                    f'{label} 的源 {canonical} 在扫描后内容已变化；请重新扫描')
        return current

    def _verify_member_sources(label, poscar, source_incar, phase, bundle=None):
        """Bind all relevant source bytes to the scan across generation/copying."""
        evidence = mapped_source_evidence.get(_source_key(poscar))
        copy_files = {}
        if (bundle or {}).get('mode') == 'copy':
            copy_files = (bundle or {}).get('files') or {}
        actual_files = {'poscar': poscar, 'incar': source_incar}
        for canonical in ('KPOINTS', 'POTCAR'):
            record = copy_files.get(canonical) or {}
            if record.get('path'):
                actual_files[canonical.lower()] = record['path']
        if evidence is None and not copy_files:
            return
        for kind, actual_path in actual_files.items():
            display = kind.upper()
            expected_path, expected_hash = ('', '')
            if evidence is not None:
                expected_path, expected_hash = _source_evidence_part(evidence, kind)
            bundle_record = copy_files.get(display) or {}
            bundle_path = str(bundle_record.get('path') or '').strip()
            bundle_hash = str(bundle_record.get('sha256') or '').strip().lower()
            if not expected_path:
                expected_path = bundle_path
            elif bundle_path and _source_key(expected_path) != _source_key(bundle_path):
                raise ValueError(
                    f'{label} 的源 {display} 路径证据互相冲突；请重新扫描')
            if not expected_hash:
                expected_hash = bundle_hash
            elif bundle_hash and expected_hash != bundle_hash:
                raise ValueError(
                    f'{label} 的源 {display} 哈希证据互相冲突；请重新扫描')
            if not re.fullmatch(r'[0-9a-f]{64}', expected_hash):
                raise ValueError(
                    f'{label} 的源 {display} 缺少有效 SHA256 证据；'
                    '请重新扫描并准备整组作业')
            if expected_path and _source_key(expected_path) != _source_key(actual_path):
                raise ValueError(
                    f'{label} 的源 {display} 路径在扫描后已变化；'
                    '请重新扫描并准备整组作业')
            try:
                actual_hash = manifest_mod.sha256_file(actual_path).lower()
            except OSError as exc:
                raise ValueError(
                    f'{label} 的源 {display} 在{phase}无法读取：{exc}；'
                    '请重新扫描并准备整组作业') from exc
            if actual_hash != expected_hash:
                raise ValueError(
                    f'{label} 的源 {display} 在扫描后内容已变化'
                    f'（{phase}哈希不一致）；请重新扫描并准备整组作业')

    planned = [('clean slab', clean_poscar)]
    planned.extend((f'构型 {Path(path).parent.name or Path(path).name}', path)
                   for path in config_poscars)
    if ref_poscar:
        planned.append(('气相参考', ref_poscar))
    planned_keys = {_source_key(poscar) for _label, poscar in planned}
    mapped_sources = (
        ('INCAR 映射', mapped_incars),
        ('源文件证据', mapped_source_evidence),
        ('四件套绑定', mapped_input_bundles),
        ('INCAR 智能修复', mapped_incar_patches),
    )
    for display, mapping in mapped_sources:
        if set(mapping) - planned_keys:
            raise ValueError(f'{display}包含不属于本项目的成员')
    member_bundles = {}
    for label, poscar in planned:
        bundle = _normalise_member_bundle(label, poscar)
        member_bundles[_source_key(poscar)] = bundle
        if bundle.get('status') == 'invalid' or bundle.get('mode') == 'blocked':
            details = '；'.join(bundle.get('issues') or ['完整四件套未通过输入校验'])
            raise ValueError(f'{label} 的同目录四件套不可用：{details}')
    member_resolutions = {}
    for label, poscar in planned:
        key = _source_key(poscar)
        selected = mapped_incars.get(key, '')
        bundle = member_bundles[key]
        if bundle.get('mode') == 'copy':
            quartet_incar = str(
                ((bundle.get('files') or {}).get('INCAR') or {}).get('path') or '')
            if selected and _source_key(selected) != _source_key(quartet_incar):
                raise ValueError(
                    f'{label} 已绑定同目录完整四件套，不能改用其他 INCAR')
            resolution = _inspect_incar_file(
                quartet_incar, source='same_directory_quartet')
        elif selected:
            resolution = _inspect_incar_file(selected, source='member_mapping')
        else:
            resolution = resolve_structure_incar(poscar, fallback=incar_path)
        member_resolutions[key] = resolution

    # Legacy/internal callers may not have a browser-side scan record.  Take a
    # local baseline before method completion or any output is created so the
    # generate path has the same before/after source-race protection as an
    # explicitly scanned complete quartet.  If the caller did supply evidence,
    # keep it fail-closed: missing or malformed fields must still be rejected by
    # _verify_member_sources instead of being silently filled here.
    for _label, poscar in planned:
        key = _source_key(poscar)
        resolution = member_resolutions[key]
        if key in mapped_source_evidence or resolution.get('status') != 'ready':
            continue
        try:
            mapped_source_evidence[key] = {
                'poscar': {
                    'path': str(Path(poscar).expanduser().resolve()),
                    'sha256': manifest_mod.sha256_file(poscar).lower(),
                },
                'incar': {
                    'path': str(Path(resolution['path']).expanduser().resolve()),
                    'sha256': manifest_mod.sha256_file(resolution['path']).lower(),
                },
            }
        except OSError as exc:
            raise ValueError(
                f'{_label} 的 POSCAR/INCAR 无法建立生成前哈希基线：{exc}') from exc
    clean_resolution = member_resolutions[_source_key(clean_poscar)]
    if clean_resolution.get('status') != 'ready':
        details = '；'.join(clean_resolution.get('issues') or ['没有可用的 INCAR'])
        raise ValueError(f'clean slab 的 INCAR 不可用：{details}')
    if fail_if_exists:
        invalid = [
            f'{label}: {"；".join(member_resolutions[_source_key(poscar)].get("issues") or [])}'
            for label, poscar in planned
            if member_resolutions[_source_key(poscar)].get('status') != 'ready'
        ]
        if invalid:
            raise ValueError('整组成员 INCAR 未完整，未发布任何项目：' + '；'.join(invalid))

    # Validate the scan evidence before any output/staging directory is made.
    # The same bytes are checked again immediately before and after each build
    # so a source editor/synchroniser cannot race the method gate and copying.
    for label, poscar in planned:
        resolution = member_resolutions[_source_key(poscar)]
        if resolution.get('status') == 'ready':
            _verify_member_sources(
                label, poscar, resolution['path'], '生成前',
                member_bundles[_source_key(poscar)])

    # Resolve the group ENCUT before creating either the final target or its
    # atomic staging directory.  A method conflict must leave no filesystem
    # artefact behind.
    valid_planned = [
        (poscar, member_resolutions[_source_key(poscar)]['path'])
        for _label, poscar in planned
        if member_resolutions[_source_key(poscar)].get('status') == 'ready'
        and member_bundles[_source_key(poscar)].get('mode') == 'generate'
    ]
    force_encut = _unified_encut(
        [incar for _poscar, incar in valid_planned],
        [poscar for poscar, _incar in valid_planned], lib_root,
        incar_overrides={
            member_resolutions[_source_key(poscar)]['path']:
            mapped_incar_patches.get(_source_key(poscar), {})
            for poscar, _incar in valid_planned
        },
    ) if validate else None

    final_root = Path(root).expanduser().resolve()
    stage_root = None
    if fail_if_exists:
        final_root.parent.mkdir(parents=True, exist_ok=True)
        if final_root.exists():
            raise FileExistsError(f'目标项目已存在，不会覆盖：{final_root}')
        stage_root = Path(tempfile.mkdtemp(
            prefix=f'.{final_root.name}.prepare-', dir=final_root.parent))
        root = stage_root
    else:
        root = final_root
        root.mkdir(parents=True, exist_ok=True)
    generated, errors = [], []

    def _apply_incar_patches(poscar, source_incar, managed_incar):
        """Apply allow-listed fixes only to the managed INCAR copy."""
        patches = mapped_incar_patches.get(_source_key(poscar)) or {}
        if not patches:
            return None
        source_path = Path(source_incar)
        managed_path = Path(managed_incar)
        source_text = source_path.read_text(encoding='utf-8', errors='replace')
        managed_text = managed_path.read_text(encoding='utf-8', errors='replace')
        source_hash_before = manifest_mod.sha256_file(source_path)
        managed_hash_before = manifest_mod.sha256_file(managed_path)
        backup_path = managed_path.with_name('INCAR.source.bak')
        source_values = parse_incar(source_text)
        managed_values = parse_incar(managed_text)
        changes = []
        for key, new_value in patches.items():
            old_value = source_values.get(key)
            try:
                old_number = None if old_value is None else float(old_value)
                managed_number = (None if managed_values.get(key) is None
                                  else float(managed_values[key]))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f'{Path(poscar).name} 的 {key} 现值无法安全自动修复') from exc
            new_number = float(new_value)
            if old_number is not None and new_number + 1e-10 < old_number:
                raise ValueError(f'{key} 低风险修复只能保持或提高原值，不能降低')
            if managed_number is not None and new_number + 1e-10 < managed_number:
                raise ValueError(f'{key} 低风险修复不能降低已生成的安全值')
            if old_number is not None and abs(old_number - new_number) <= 1e-10:
                continue
            rendered = str(new_value)
            pattern = re.compile(
                rf'(?im)(^|;)([ \t]*{re.escape(key)}[ \t]*=[ \t]*)'
                r'([^;#!\r\n]*?)(?=[ \t]*(?:;|[#!]|\r?$))')
            managed_text, replacements = pattern.subn(
                lambda match: match.group(1) + match.group(2) + rendered,
                managed_text)
            if not replacements:
                managed_text = managed_text.rstrip('\r\n') + f'\n{key} = {rendered}\n'
            changes.append({
                'key': key, 'old': old_value, 'new': new_value,
                'risk': 'low',
            })
        if not changes:
            return None
        # Back up the exact managed file that is about to be changed.  In the
        # generated-input path this may already contain deterministic
        # completions and is not necessarily byte-identical to the user source.
        shutil.copy2(managed_path, backup_path)
        if manifest_mod.sha256_file(backup_path) != managed_hash_before:
            raise ValueError('INCAR 智能修复备份哈希不一致，已停止发布项目')
        managed_path.write_text(managed_text, encoding='utf-8')
        final_values = parse_incar(managed_text)
        for change in changes:
            try:
                actual = float(final_values.get(change['key']))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f'{change["key"]} 智能修复写入后无法复核') from exc
            if abs(actual - float(change['new'])) > 1e-10:
                raise ValueError(f'{change["key"]} 智能修复写入后数值不一致')
        if manifest_mod.sha256_file(source_path) != source_hash_before:
            raise ValueError('源 INCAR 在智能修复期间发生变化；请重新扫描')
        return {
            'schema': 1, 'risk': 'low', 'source_unchanged': True,
            'source_incar': {
                'path': str(source_path.resolve()),
                'sha256': source_hash_before,
            },
            'backup': {
                # Relative paths survive the atomic staging-directory rename.
                'path': backup_path.name,
                'sha256': managed_hash_before,
            },
            'managed_before_sha256': managed_hash_before,
            'final_incar': {
                # Relative to the managed job directory: in atomic mode the
                # staging root is renamed after this evidence is recorded.
                'path': 'INCAR',
                'sha256': manifest_mod.sha256_file(managed_path),
            },
            'changes': changes,
        }

    # 项目级统一 ENCUT:各成员元素并集算一个 ENCUT,防 ΔE 大数相减被不同基组污染。
    # 仅当用户 INCAR 未显式给 ENCUT 时生效(用户值永远尊重);算不出则回落逐成员(旧行为)。
    def _copied_task_type(incar):
        def _integer(value, default):
            try:
                if isinstance(value, bool):
                    return default
                return int(float(value))
            except (TypeError, ValueError):
                return default
        if _integer(incar.get('IBRION'), -1) in (5, 6, 7, 8):
            return 'freq'
        return 'static' if _integer(incar.get('NSW'), 0) <= 0 else 'relax'

    def _copy_quartet(member, poscar, calc_type, source_incar, bundle, out):
        """Copy a validated quartet byte-for-byte and create an audited manifest."""
        files = bundle.get('files') or {}
        if set(files) != set(_VASP_INPUT_NAMES):
            raise ValueError(f'{member} 的 copy 四件套证据不完整')
        out.mkdir(parents=True, exist_ok=True)
        for filename in _VASP_INPUT_NAMES:
            source = files[filename]['path']
            destination = out / filename
            shutil.copy2(source, destination)
            copied_hash = manifest_mod.sha256_file(destination).lower()
            if copied_hash != str(files[filename]['sha256']).lower():
                raise ValueError(
                    f'{member} 的 {filename} 复制结果与扫描哈希不一致；'
                    '源文件可能在复制期间变化')

        repairs = _apply_incar_patches(poscar, source_incar, out / 'INCAR')
        from vcstudio.project.result_import import validate_vasp_quartet
        copied_issues = validate_vasp_quartet(out)
        if copied_issues:
            raise ValueError(
                f'{member} 的复制后四件套未通过校验：'
                + '；'.join(copied_issues))

        poscar_text = read_poscar(out / 'POSCAR')
        elements, _counts = parse_poscar_species(poscar_text)
        with open(out / 'INCAR', 'r', encoding='utf-8', errors='replace') as handle:
            incar = parse_incar(handle.read())
        with open(out / 'KPOINTS', 'r', encoding='utf-8', errors='replace') as handle:
            kpoints_info = methods_text.parse_kpoints_scheme(handle.read()) or {}
        with open(out / 'POTCAR', 'r', encoding='utf-8', errors='replace') as handle:
            potcar_rows = methods_text.parse_potcar_titels(handle.read())
        build_result = {
            'ok': True, 'out_dir': str(out), 'warnings': [],
            'kpoints': list(kpoints_info.get('grid') or []),
            'elements': list(elements), 'completions': {},
            'calc_type': calc_type, 'task_type': _copied_task_type(incar),
            'potcar': potcar_rows,
        }
        job_manifest = manifest_mod.create_from_build(
            str(out), build_result, poscar_path=poscar,
            incar_path=source_incar, validate=validate)
        job_manifest.setdefault('inputs', {})['input_mode'] = 'copy'
        job_manifest['inputs']['source_quartet'] = {
            filename: {
                'path': str(files[filename]['path']),
                'sha256': str(files[filename]['sha256']).lower(),
            }
            for filename in _VASP_INPUT_NAMES
        }
        if repairs:
            job_manifest['inputs']['incar_repairs'] = repairs
        # create_from_build already hashes the final managed inputs.  Assert the
        # invariant here before publishing the manifest/staging directory.
        final_hashes = job_manifest['inputs'].get('sha256') or {}
        if set(final_hashes) != set(_VASP_INPUT_NAMES):
            raise ValueError(f'{member} 的最终四件套哈希记录不完整')
        for filename in _VASP_INPUT_NAMES:
            if filename == 'INCAR' and repairs:
                if final_hashes[filename].lower() != \
                        repairs['final_incar']['sha256'].lower():
                    raise ValueError(f'{member} 的修复后 INCAR 哈希记录不一致')
                continue
            if final_hashes[filename].lower() != files[filename]['sha256'].lower():
                raise ValueError(f'{member} 的最终 {filename} 不是源文件的字节级副本')
        manifest_mod.save_manifest(out, job_manifest)
        return build_result

    def _gen(member: str, poscar: str, calc_type: str):
        resolution = member_resolutions[_source_key(poscar)]
        if resolution.get('status') != 'ready':
            details = '；'.join(resolution.get('issues') or ['没有可用的 INCAR'])
            raise ValueError(f'{poscar} 的 INCAR 不可用：{details}')
        source_incar = resolution['path']
        out = root / member
        bundle = member_bundles[_source_key(poscar)]
        _verify_member_sources(member, poscar, source_incar, '复制前', bundle)
        if bundle.get('mode') == 'copy':
            res = _copy_quartet(
                member, poscar, calc_type, source_incar, bundle, out)
        else:
            res = build_job_dir(poscar, source_incar, str(out), calc_type=calc_type,
                                kpoints=kpoints, validate=validate, lib_root=lib_root,
                                force_encut=force_encut)
            repairs = _apply_incar_patches(poscar, source_incar, out / 'INCAR')
            # This is a directory-local execution check only: POSCAR atom
            # counts/order must match this member's MAGMOM, Hubbard vectors and
            # generated POTCAR.  It deliberately does not demand cross-member
            # ISPIN/MAGMOM equality or treat a positive low ENCUT as invalid.
            from vcstudio.project.result_import import validate_vasp_quartet
            generated_issues = validate_vasp_quartet(out)
            if generated_issues:
                phase = '修复后' if repairs else '生成后'
                raise ValueError(
                    f'{member} 的{phase}四件套未通过校验：'
                    + '；'.join(generated_issues))
            job_manifest = manifest_mod.create_from_build(
                str(out), res, poscar_path=poscar,
                incar_path=source_incar, validate=validate)
            job_manifest.setdefault('inputs', {})['input_mode'] = 'generate'
            if repairs:
                job_manifest['inputs']['incar_repairs'] = repairs
            final_hashes = job_manifest['inputs'].get('sha256') or {}
            if set(final_hashes) != set(_VASP_INPUT_NAMES):
                raise ValueError(f'{member} 的最终四件套哈希记录不完整')
            for filename in _VASP_INPUT_NAMES:
                try:
                    actual_hash = manifest_mod.sha256_file(out / filename).lower()
                except OSError as exc:
                    raise ValueError(
                        f'{member} 的最终 {filename} 无法读取：{exc}') from exc
                if actual_hash != str(final_hashes[filename]).lower():
                    raise ValueError(
                        f'{member} 的最终 {filename} 与清单哈希不一致')
            manifest_mod.save_manifest(out, job_manifest)
        _verify_member_sources(member, poscar, source_incar, '复制后', bundle)
        if stage_root is None:
            ledger.register(str(out))
        generated.append((member, str(out), res['warnings']))
        return str(out)

    try:
        # 1) 清洁表面(必需;失败即整体失败)
        slab_member = f'{name}_slab_clean'
        slab_dir = _gen(slab_member, clean_poscar, 'slab')

        # 2) 构型族(个别失败不拖垮兼容旧流程；原子发布模式则整组失败)
        config_dirs = []
        config_species_out = {}
        config_evidence_out = {}
        source_species = {}
        source_evidence = {}
        for source, species in dict(config_species or {}).items():
            value = str(species or '').strip()
            if not value:
                continue
            source = os.path.expanduser(os.fspath(source))
            source_species[os.path.normcase(os.path.abspath(os.path.normpath(source)))] = value
        for source, evidence in dict(config_species_evidence or {}).items():
            source = os.path.expanduser(os.fspath(source))
            source_evidence[os.path.normcase(os.path.abspath(os.path.normpath(source)))] = \
                dict(evidence or {})
        for p in config_poscars:
            member = f'{name}_ads_{_stem(p)}'
            try:
                config_dir = _gen(member, p, 'slab')
                config_dirs.append(config_dir)
                source_key = os.path.normcase(os.path.abspath(os.path.normpath(p)))
                if source_key in source_species:
                    config_species_out[str(Path(config_dir).resolve())] = source_species[source_key]
                if source_key in source_evidence:
                    config_evidence_out[str(Path(config_dir).resolve())] = source_evidence[source_key]
            except Exception as e:   # PotcarError 亦在此兜住
                errors.append((member, str(e)))

        # 3) 气相参考(可选;molecule 类型 → Γ 点)
        ref_dir = None
        if ref_poscar:
            member = f'{name}_ref'
            try:
                ref_dir = _gen(member, ref_poscar, 'molecule')
            except Exception as e:
                errors.append((member, str(e)))

        if stage_root is not None and errors:
            details = '；'.join(f'{member}: {message}' for member, message in errors)
            raise ValueError(f'整组未完整生成，未发布任何项目：{details}')

        def _published(path):
            if not path or stage_root is None:
                return path
            return str(final_root / Path(path).relative_to(stage_root))

        slab_dir = _published(slab_dir)
        config_dirs = [_published(path) for path in config_dirs]
        ref_dir = _published(ref_dir)
        if stage_root is not None:
            config_species_out = {
                _published(path): species for path, species in config_species_out.items()
            }
            config_evidence_out = {
                _published(path): evidence for path, evidence in config_evidence_out.items()
            }

        project = {
            'schema': 1,
            'name': name,
            'created_at': _now(),
            'root': str(final_root),
            'members': {
                'clean_slab': slab_dir,
                'gas_ref': ref_dir,
                'configs': config_dirs,
            },
        }
        if config_species is not None:
            project['config_species'] = config_species_out
            project['config_species_evidence'] = config_evidence_out
            project['dataset_groups'] = structure_identity.build_dataset_groups(
                config_dirs, config_species_out, species_ref_jobs)
        if species_refs is not None:
            project['species_refs'] = dict(species_refs)
        if species_ref_jobs is not None:
            project['species_ref_jobs'] = dict(species_ref_jobs)
        if molecules_dir is not None:
            project['molecules_dir'] = str(Path(molecules_dir).expanduser().resolve())
        if reference_project is not None:
            project['reference_project'] = str(Path(reference_project).expanduser().resolve())
        if preparation is not None:
            prepared = dict(preparation)
            # request_sha256 proves local idempotency but is deterministic.  A
            # deleted/recreated project (or the same inputs on another machine)
            # must not reuse a remote directory that may still contain a live job.
            project_uuid = str(prepared.get('project_uuid') or uuid.uuid4().hex)
            if not re.fullmatch(r'[A-Fa-f0-9]{32}', project_uuid):
                raise ValueError('preparation.project_uuid 必须是 32 位十六进制标识')
            prepared['project_uuid'] = project_uuid.lower()
            project['preparation'] = prepared
            project['project_uuid'] = prepared['project_uuid']
            safe_name = _STEM_RE.sub('_', str(name)).strip('._-')[:80] or 'project'
            namespace = f'{safe_name}-{prepared["project_uuid"]}'
            if namespace:
                project['remote_namespace'] = namespace
                for _member, job_dir, _warnings in generated:
                    job_manifest = manifest_mod.load_manifest(job_dir)
                    if job_manifest is None:
                        raise ValueError(f'作业 {job_dir} 缺可读 job.yaml，不能写入远程命名空间')
                    job_manifest.setdefault('inputs', {})['remote_namespace'] = namespace
                    manifest_mod.save_manifest(job_dir, job_manifest)
        save_project(root, project)
        advisories = _project_advisories(clean_resolution['path'],
                                         [str(root / Path(d).name) for d in config_dirs],
                                         ref_poscar, generated, lib_root)
        ppath = final_root / PROJECT_NAME
        registered_jobs = []
        project_registered = False
        published_atomically = False
        try:
            if stage_root is not None:
                os.replace(stage_root, final_root)
                published_atomically = True
            for _member, job_dir, _warnings in generated:
                published_job = _published(job_dir)
                if ledger.register(published_job):
                    registered_jobs.append(published_job)
            project_registered = register_project(ppath)
        except Exception:
            if project_registered:
                unregister_project(ppath)
            for job_dir in reversed(registered_jobs):
                ledger.unregister(job_dir)
            if published_atomically and final_root.exists():
                shutil.rmtree(final_root, ignore_errors=True)
            raise
        generated = [(member, _published(path), warnings)
                     for member, path, warnings in generated]
        return {'ok': True, 'project_path': str(ppath), 'project': project,
                'generated': generated, 'errors': errors, 'advisories': advisories}
    except Exception:
        if stage_root is not None and stage_root.exists():
            shutil.rmtree(stage_root, ignore_errors=True)
        raise


def _project_advisories(incar_path, config_dirs, ref_poscar, generated, lib_root):
    """方法学顾问(warn-only,失败静默降级为空——顾问绝不能挡生成)。"""
    try:
        from vcstudio.generate.incar_builder import parse_incar
        from vcstudio.generate.poscar import (read_poscar, parse_poscar_species,
                                              read_cell_vectors)
        from vcstudio.generate import potcar as potcar_mod
        from vcstudio.project import advisor
        with open(incar_path, 'r', encoding='utf-8', errors='replace') as f:
            incar = parse_incar(f.read())
        gas = None
        if ref_poscar:
            text = read_poscar(ref_poscar)
            els, cnts = parse_poscar_species(text)
            gas = {'elements': els, 'counts': cnts, 'cell': read_cell_vectors(text)}
        unified = None
        if 'ENCUT' not in {str(k).upper() for k in incar}:
            union = set()
            for d in ([g[1] for g in generated]):
                m = manifest_mod.load_manifest(d)
                union.update((m or {}).get('inputs', {}).get('elements') or [])
            if union:
                import math
                mx = potcar_mod.max_enmax(sorted(union), lib_root)
                unified = int(math.ceil(1.3 * mx / 50.0) * 50)
        return advisor.advise(incar, has_configs=bool(config_dirs),
                              gas=gas, unified_encut=unified)
    except Exception:                                    # noqa: BLE001 顾问失败绝不挡生成
        return []


def save_project(root: str | os.PathLike, project: dict) -> Path:
    target = Path(root) / PROJECT_NAME
    tmp = target.with_suffix('.yaml.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        yaml.safe_dump(project, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp, target)
    return target


def load_project(path: str | os.PathLike) -> dict | None:
    """path 可以是 project.yaml 或项目根目录。不存在/畸形 → None。"""
    p = Path(path)
    if p.is_dir():
        p = p / PROJECT_NAME
    if not p.is_file():
        return None
    with open(p, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) and data.get('members') else None


# ── 项目注册表(与 ledger 同风格:只记路径) ───────────────────────────────────
def default_registry_path() -> Path:
    return user_config_dir() / 'projects.json'


def register_project(project_yaml: str | os.PathLike,
                     path: str | os.PathLike | None = None) -> bool:
    target = Path(path) if path is not None else default_registry_path()
    entry = str(Path(project_yaml).resolve())
    items = list_projects(path=target)
    if entry in items:
        return False
    items.append(entry)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix('.json.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'projects': items}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, target)
    return True


def unregister_project(project_yaml: str | os.PathLike,
                       path: str | os.PathLike | None = None) -> bool:
    """从项目注册表移除一个 project.yaml，不删任何项目文件。

    与 ``register_project`` 使用同一原子写口径，供结果导入在后续登记
    失败时做事务回滚。
    """
    target = Path(path) if path is not None else default_registry_path()
    entry = str(Path(project_yaml).resolve())
    items = list_projects(path=target)
    if entry not in items:
        return False
    items = [item for item in items if item != entry]
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix('.json.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'projects': items}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, target)
    return True


def list_projects(path: str | os.PathLike | None = None) -> list:
    target = Path(path) if path is not None else default_registry_path()
    if not target.is_file():
        return []
    try:
        with open(target, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    items = data.get('projects') if isinstance(data, dict) else None
    return [str(x) for x in items] if isinstance(items, list) else []


# ── ΔE 汇总 ─────────────────────────────────────────────────────────────────
def _member_info(job_dir: str | None, label='成员'):
    """成员目录 → ``(state, energy|None, validation_error)``。

    DONE 只是生命周期状态，不足以授权能量相减。最终吸附能的每个操作数还
    必须有完整结束证据，并由当前目录 OSZICAR 的末 ``E0`` 复核。
    """
    if not job_dir:
        return '未设置', None, ''
    m = manifest_mod.load_manifest(job_dir)
    if m is None:
        return '缺 job.yaml', None, f'{label}缺少可读 job.yaml'
    e = m.get('results', {}).get('energy_e0_eV')
    if (not isinstance(e, (int, float)) or isinstance(e, bool)
            or not math.isfinite(float(e)) or diagnose.energy_implausible(e)):
        return m.get('state', '?'), None, f'{label}能量缺失或不合理'
    if m.get('state') != 'DONE':
        return m.get('state', '?'), float(e), ''
    from vcstudio.project import energy_gate
    try:
        checked, _manifest, _evidence = energy_gate.validate_done_energy(
            job_dir, label, manifest_mod, require_oszicar=True)
    except ValueError as exc:
        return m.get('state', '?'), None, str(exc)
    return m.get('state', '?'), float(checked), ''


def _reasonable_energy(value) -> float | None:
    """Return a finite, physically plausible VASP total energy, else None."""
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(float(value)) or diagnose.energy_implausible(value)):
        return None
    return float(value)


def _species_reference_info(species: str, cached_energy, job_dir) -> dict:
    """Resolve one molecular reference from its job manifest, fail-closed.

    ``project['species_refs']`` is intentionally treated as a consistency cache.
    The value used in a formula always comes from the referenced ``job.yaml``.
    """
    cached = _reasonable_energy(cached_energy)
    info = {
        'species': species,
        'cache_energy': cached,
        'energy': None,
        'state': '未登记',
        'source': '',
        'job': str(job_dir or ''),
        'imported_from': '',
        'hashes': {},
        'confirmation': {},
        'valid': False,
        'note': '',
    }
    if not job_dir:
        info['note'] = (
            '物种参考作业未登记（项目缺少 species_ref_jobs）；'
            '请重新导入分子结果或重新选择参考项目')
        return info

    ref_manifest = manifest_mod.load_manifest(job_dir)
    if ref_manifest is None:
        info['state'] = '缺 job.yaml'
        info['note'] = (
            f'物种 {species} 的参考作业缺少可读 job.yaml；'
            '请重新导入该分子结果或修复参考项目路径')
        return info

    results = ref_manifest.get('results') or {}
    inputs = ref_manifest.get('inputs') or {}
    state = str(ref_manifest.get('state') or '?')
    manifest_energy = _reasonable_energy(results.get('energy_e0_eV'))
    info.update({
        'energy': manifest_energy,
        'state': state,
        'source': str(results.get('energy_source') or ''),
        'imported_from': inputs.get('imported_from') or '',
        'hashes': inputs.get('source_sha256') or {},
        'confirmation': results.get('import_confirmation') or {},
    })

    blockers = []
    if state != 'DONE':
        blockers.append(
            f'物种 {species} 的参考作业状态为 {state}，请先完成或人工确认该结果')
    if manifest_energy is None:
        blockers.append(
            f'物种 {species} 的参考 job.yaml 能量缺失或不合理，请重新解析/导入结果')
    if state == 'DONE' and manifest_energy is not None:
        from vcstudio.project import energy_gate
        try:
            checked, _checked_manifest, evidence = energy_gate.validate_done_energy(
                job_dir, f'物种 {species} 参考', manifest_mod, require_oszicar=True)
            manifest_energy = float(checked)
            info['energy'] = manifest_energy
            info['energy_validation'] = evidence
        except ValueError as exc:
            info['energy'] = None
            info['energy_validation'] = []
            blockers.append(str(exc))
    # A molecule may be imported as CREATED and finish later.  In that valid
    # lifecycle the project cache is intentionally empty; the DONE job.yaml is
    # the truth source and must become usable without re-importing the project.
    if cached is None and state == 'DONE' and manifest_energy is not None:
        info['cache_refresh_needed'] = True
        info['note'] = 'project.yaml 参考能缓存尚未刷新；本次采用 DONE job.yaml 真值'
    else:
        info['cache_refresh_needed'] = False
    if manifest_energy is not None and cached is not None:
        drift = abs(manifest_energy - cached)
        if drift > SPECIES_REF_CACHE_TOLERANCE_EV:
            blockers.append(
                f'物种 {species} 的参考能缓存与 job.yaml 不一致'
                f'（差 {drift:.9g} eV，容差 {SPECIES_REF_CACHE_TOLERANCE_EV:g} eV）；'
                '请刷新参考项目后重新准备')
    info['valid'] = not blockers
    if blockers:
        info['note'] = '；'.join(blockers)
    return info


def _species_reference_index(project: dict) -> dict[str, dict]:
    """Return per-species manifest-backed reference evidence."""
    cached = dict(project.get('species_refs') or {})
    jobs = dict(project.get('species_ref_jobs') or {})
    return {
        species: _species_reference_info(species, cached.get(species), jobs.get(species))
        for species in sorted(set(cached) | set(jobs))
    }


def _energy_pair_method_check(left_dir, left_label, right_dir, right_label,
                              *, require_same_kpoints):
    """Compare one subtraction pair using actual-output method signatures."""
    from vcstudio.project import energy_gate

    if not left_dir or not right_dir:
        return {'status': 'unverified', 'issues': [],
                'warnings': ['方法核验缺少作业路径'], 'labels': []}
    left_manifest = manifest_mod.load_manifest(left_dir)
    right_manifest = manifest_mod.load_manifest(right_dir)
    records = [
        energy_gate.method_record(left_dir, left_manifest, left_label),
        energy_gate.method_record(right_dir, right_manifest, right_label),
    ]
    check = energy_gate.compare_methods(
        records, require_same_kpoints=require_same_kpoints)
    if not require_same_kpoints:
        # A molecular reference necessarily uses a different cell and normally
        # Γ-only sampling.  K-point equality is therefore neither required nor
        # meaningful for this cross-cell subtraction; retain it as audit context
        # without downgrading otherwise complete method evidence.
        kpoint_notes = [
            warning for warning in (check.get('warnings') or [])
            if warning.startswith('K 点')]
        if kpoint_notes:
            check['warnings'] = [
                warning for warning in check['warnings']
                if warning not in kpoint_notes]
            check['advisories'] = list(dict.fromkeys([
                *(check.get('advisories') or []),
                *(warning + '；分子参考与周期体系晶胞不同，此差异不阻断'
                  for warning in kpoint_notes),
            ]))
    if check.get('issues'):
        # ISPIN/MAGMOM describe each system's own magnetic ground-state search.
        # clean=1 and adsorption=2 can be a legitimate adsorption-induced
        # magnetic solution, so spin is never a cross-directory hard blocker.
        # Gas-vs-periodic DFT+U remains soft here for backward compatibility;
        # common-element U identities are audited separately when provenance is
        # available instead of comparing raw vectors with different species order.
        legal_spin_difference = all(
            row.get('known', {}).get('spin')
            and (row.get('fingerprint') or {}).get('spin') in {1, 2}
            for row in records)
        spin_soft = ([issue for issue in check['issues']
                      if issue.startswith('ISPIN 不一致')]
                     if legal_spin_difference else [])
        u_soft = ([issue for issue in check['issues']
                   if issue.startswith('DFT+U 不一致')]
                  if not require_same_kpoints else [])
        soft = spin_soft + u_soft
        if soft:
            check['issues'] = [issue for issue in check['issues'] if issue not in soft]
            check['advisories'] = list(dict.fromkeys([
                *(check.get('advisories') or []),
                *(issue + '；不同体系可采用各自基态自旋设置' for issue in spin_soft),
            ]))
            check['warnings'] = list(dict.fromkeys([
                *(check.get('warnings') or []),
                *(issue + '；气相/周期体系差异需人工核对' for issue in u_soft),
            ]))
    check['status'] = ('incompatible' if check.get('issues')
                       else ('verified' if not check.get('warnings') else 'unverified'))
    return check


def _row_method_check(slab_dir, config_dir, reference_dir=None) -> dict:
    """Audit every direct energy subtraction used by one adsorption row."""
    checks = [_energy_pair_method_check(
        slab_dir, '清洁表面', config_dir, '吸附构型', require_same_kpoints=True)]
    if reference_dir:
        # Gas/molecular references live in a different cell; their k-point
        # schemes may differ, but functional/ENCUT/POTCAR conflicts still block.
        checks.append(_energy_pair_method_check(
            config_dir, '吸附构型', reference_dir, '参考态',
            require_same_kpoints=False))
    issues = list(dict.fromkeys(
        issue for check in checks for issue in (check.get('issues') or [])))
    warnings = list(dict.fromkeys(
        warning for check in checks for warning in (check.get('warnings') or [])))
    advisories = list(dict.fromkeys(
        item for check in checks for item in (check.get('advisories') or [])))
    status = 'incompatible' if issues else ('verified' if not warnings else 'unverified')
    return {'status': status, 'issues': issues, 'warnings': warnings,
            'advisories': advisories, 'checks': checks}


def delta_e_rows(project: dict) -> dict:
    """按当前各成员 job.yaml 计算 ΔE 表。

    Returns:
        {'slab': (state, E), 'ref': (state, E|None), 'has_ref': bool,
         'rows': [{'name','state','e_config','delta_e','note',
                   'species','is_most_stable','dd_e'}]}
    规则:ΔE 只在 构型 DONE 且 slab DONE 且(有参考时)ref DONE 时给出;否则 note 说明缺谁。
    多构型取最稳:同一 species(构型名剥前缀识别)内 delta_e 最低者 is_most_stable=True,
    dd_e 为组内相对最稳的 ΔΔE(最稳 0.0);delta_e 为 None 的行 is_most_stable=False、dd_e=None。
    """
    members = project.get('members') or {}
    proj_name = str(project.get('name') or '')
    slab_state, e_slab, slab_error = _member_info(
        members.get('clean_slab'), '清洁表面')
    has_gas_ref = bool(members.get('gas_ref'))
    if has_gas_ref:
        ref_state, e_ref, ref_error = _member_info(members.get('gas_ref'), '气相参考')
    else:
        ref_state, e_ref, ref_error = '无', None, ''
    # 逐物种气相参考(原版 lis_sac_analysis 口径):project['species_refs']=
    # {物种: E_mol};构型名以 '_<物种>' 结尾即匹配。与单一 gas_ref 互斥,优先。
    species_refs = dict(project.get('species_refs') or {})
    species_ref_jobs = dict(project.get('species_ref_jobs') or {})
    reference_index = _species_reference_index(project)
    reference_mode = ('species' if (species_refs or species_ref_jobs)
                      else ('single' if has_gas_ref else 'none'))
    has_ref = reference_mode != 'none'
    explicit_species = _config_species_index(project)

    rows = []
    for cdir in (members.get('configs') or []):
        name = os.path.basename(os.path.normpath(cdir))
        st, e_cfg, config_error = _member_info(cdir, f'吸附构型 {name}')
        config_manifest = manifest_mod.load_manifest(cdir) or {}
        configuration_id = str(
            config_manifest.get('job_id') or config_manifest.get('job_uuid') or '').strip()
        delta, note = None, ''
        sp_ref = None
        sp = None
        reference_job = None
        reference_source = None
        reference_state = None
        reference_note = ''
        reference_valid = False
        mapped_species = next(
            (explicit_species[key] for key in _path_keys(cdir, project.get('root'))
             if key in explicit_species), None)
        row_species = mapped_species or _config_species(name, proj_name)
        if reference_mode == 'species':
            # Imported projects carry an explicit mapping because their managed
            # directory may simply be ``configs/Li2S8``.  Prefer that audited
            # metadata; retain the historical suffix convention for old projects.
            reference_species = set(reference_index)
            sp = mapped_species if mapped_species in reference_species else None
            if mapped_species is None:
                sp = next((s for s in sorted(reference_species, key=len, reverse=True)
                           if name.endswith('_' + s)), None)
            ref_info = reference_index.get(sp)
            if ref_info:
                # Keep the actual manifest energy visible even while another gate
                # (state/cache consistency) blocks the formula.  Only valid refs
                # are assigned to sp_ref and can enter arithmetic below.
                actual_ref_energy = ref_info['energy']
                if ref_info['valid']:
                    sp_ref = actual_ref_energy
                reference_job = ref_info['job'] or None
                reference_source = ref_info['source'] or None
                reference_state = ref_info['state']
                reference_note = ref_info['note']
                reference_valid = ref_info['valid']
            else:
                actual_ref_energy = None
                reference_state = '未匹配'
        else:
            actual_ref_energy = e_ref
            if has_gas_ref:
                reference_job = members.get('gas_ref')
                reference_state = ref_state
                gas_manifest = manifest_mod.load_manifest(reference_job)
                reference_source = str(
                    (((gas_manifest or {}).get('results') or {}).get('energy_source') or '')
                ) or None
                reference_valid = ref_state == 'DONE' and e_ref is not None
        method_check = _row_method_check(
            members.get('clean_slab'), cdir,
            reference_job if reference_mode != 'none' else None)
        blockers = []
        if st != 'DONE':
            blockers.append('构型未完成')
        elif e_cfg is None:
            blockers.append(config_error or '构型能量缺失或不合理')
        if slab_state != 'DONE':
            blockers.append('清洁表面未完成')
        elif e_slab is None:
            blockers.append(slab_error or '清洁表面能量缺失或不合理')
        if reference_mode == 'species':
            if sp is None:
                blockers.append(
                    '无匹配物种参考作业；请检查构型物种映射并重新选择参考项目')
            elif not reference_valid:
                blockers.append(reference_note or '物种参考未通过完整性校验')
        elif has_gas_ref:
            if ref_state != 'DONE':
                blockers.append('气相参考未完成')
            elif e_ref is None:
                blockers.append(ref_error or '气相参考能量缺失或不合理')
        if method_check['status'] == 'incompatible':
            blockers.append('能量项方法不一致：' + '；'.join(method_check['issues']))
        elif method_check['status'] == 'unverified':
            blockers.append(
                '能量项方法证据不完整：' + '；'.join(method_check['warnings'])
                + '；自动 ΔE 与最终报告已暂停')
        if not blockers:
            if sp_ref is not None:
                delta = e_cfg - e_slab - sp_ref
            else:
                delta = e_cfg - e_slab - (e_ref if has_gas_ref else 0.0)
                if not has_gas_ref:
                    note = '未设气相参考:此值为 E(slab+ads)−E(slab)'
        else:
            note = '；'.join(blockers)
        rows.append({'name': name, 'job': cdir,
                     'configuration_id': configuration_id or None,
                     'state': st, 'e_config': e_cfg,
                     'delta_e': delta, 'note': note, 'species': row_species,
                     'reference_species': sp if reference_mode == 'species' else None,
                     'e_ref': actual_ref_energy,
                     'reference_state': reference_state,
                     'reference_job': reference_job,
                     'reference_source': reference_source,
                     'reference_valid': reference_valid,
                     'reference_note': reference_note,
                     'method_check': method_check,
                     'method_warnings': method_check['warnings'],
                     'method_advisories': method_check.get('advisories') or []})

    # 多构型取最稳:按 species 分组求组内最低 ΔE,标注 is_most_stable 与相对 ΔΔE
    group_min: dict = {}
    for r in rows:
        d = r['delta_e']
        if d is not None and (r['species'] not in group_min or d < group_min[r['species']]):
            group_min[r['species']] = d
    for r in rows:
        d, gm = r['delta_e'], group_min.get(r['species'])
        if d is None or gm is None:
            r['is_most_stable'], r['dd_e'] = False, None
        else:
            r['dd_e'] = round(d - gm, 6)
            r['is_most_stable'] = (r['dd_e'] == 0.0)

    resolved_refs = {
        species: (info['energy'] if info['valid'] else None)
        for species, info in reference_index.items()
    }
    method_checks = [row['method_check'] for row in rows]
    method_issues = list(dict.fromkeys(
        issue for check in method_checks for issue in (check.get('issues') or [])))
    method_warnings = list(dict.fromkeys(
        warning for check in method_checks for warning in (check.get('warnings') or [])))
    method_advisories = list(dict.fromkeys(
        item for check in method_checks for item in (check.get('advisories') or [])))
    method_status = ('incompatible' if method_issues
                     else ('verified' if not method_warnings else 'unverified'))
    dataset_groups = list(project.get('dataset_groups') or [])
    if not dataset_groups:
        explicit_mapping = {
            path: next((explicit_species[key] for key in _path_keys(path, project.get('root'))
                        if key in explicit_species), '')
            for path in (members.get('configs') or [])
        }
        dataset_groups = structure_identity.build_dataset_groups(
            members.get('configs') or [], explicit_mapping, species_ref_jobs)
    return {'slab': (slab_state, e_slab), 'ref': (ref_state, e_ref),
            'has_ref': has_ref, 'reference_mode': reference_mode,
            'species_refs': resolved_refs, 'species_ref_cache': species_refs,
            'species_reference_evidence': list(reference_index.values()),
            'dataset_groups': dataset_groups,
        'method_consistency': {'status': method_status, 'issues': method_issues,
                               'warnings': method_warnings,
                               'advisories': method_advisories},
            'rows': rows}


def export_csv(project: dict, summary: dict, out_path: str | os.PathLike) -> Path:
    """ΔE 表导出 CSV(utf-8-sig:Excel 双击打开中文/负号不乱码)。"""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    slab_state, e_slab = summary['slab']
    ref_state, e_ref = summary['ref']
    with open(out, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow([f"吸附能项目:{project.get('name','')}", f'导出时间:{_now()}'])
        if summary.get('reference_mode') == 'species':
            evidence = summary.get('species_reference_evidence') or []
            ref_label = '逐物种参考（job.yaml 真值）：' + '；'.join(
                f'{item.get("species")}={_fmt(item.get("energy"))} eV'
                f'({item.get("state") or "未知"})'
                for item in evidence)
        elif summary['has_ref']:
            ref_label = f'E(ref)={_fmt(e_ref)} eV({ref_state})'
        else:
            ref_label = 'E(ref)=未设置'
        w.writerow([f'E(slab)={_fmt(e_slab)} eV({slab_state})', ref_label])
        w.writerow([])
        w.writerow(['构型', '物种', '状态', 'E(slab+ads) / eV', 'E(ref) / eV',
                    'ΔE_ads / eV (= E(slab+ads)-E(slab)-E(ref))',
                    'ΔΔE(eV)', '是否最稳', '参考状态', '参考来源', '参考作业', '备注'])
        for r in summary['rows']:
            w.writerow([r['name'], r.get('species'), r['state'], _fmt(r['e_config']),
                        _fmt(r.get('e_ref')), _fmt(r['delta_e']), _fmt(r.get('dd_e')),
                        '是' if r.get('is_most_stable') else '',
                        r.get('reference_state') or '', r.get('reference_source') or '',
                        r.get('reference_job') or '', r['note']])
    return out


def _fmt(v) -> str:
    return f'{v:.6f}' if isinstance(v, float) else ''
