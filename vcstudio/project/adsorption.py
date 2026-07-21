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
from vcstudio.generate import potcar
from vcstudio.generate.incar_builder import parse_incar
from vcstudio.generate.job_builder import build_job_dir
from vcstudio.generate.poscar import parse_poscar_species, read_poscar
from vcstudio.shared import manifest as manifest_mod
from vcstudio.shared.config import user_config_dir

PROJECT_NAME = 'project.yaml'
_STEM_RE = re.compile(r'[^A-Za-z0-9_.-]')
# 化学式样 token:元素符号([A-Z][a-z]?)+ 可选计数,连缀成整式(Li2S4 / CO / OH / Fe2O3)。
# 与 freeenergy._species_in_name 同一 token 观:大写起头、贴着数字算计数、遇分隔即断。
_SPECIES_TOKEN_RE = re.compile(r'[A-Z][a-z]?\d*(?:[A-Z][a-z]?\d*)*')
_STRUCTURE_NAMES = {'poscar', 'contcar'}
_STRUCTURE_SUFFIXES = {'.vasp', '.poscar'}
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
            for wanted in ('contcar', 'poscar'):
                candidate = exact_structures.get(wanted)
                if not candidate or candidate.is_symlink():
                    continue
                try:
                    parse_poscar_species(read_poscar(candidate))
                except (OSError, ValueError):
                    continue
                selected_exact = candidate
                if wanted == 'contcar' and 'poscar' in exact_structures:
                    selection_note = '同目录同时有 POSCAR/CONTCAR，已优先选可解析的最终 CONTCAR'
                elif wanted == 'poscar' and 'contcar' in exact_structures:
                    selection_note = 'CONTCAR 不可解析，已回退到 POSCAR'
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
            if selection_note:
                item['selection_note'] = selection_note
            found.append(item)
    found.sort(key=lambda item: os.path.relpath(item['path'], base).casefold())
    return found


def scan_lis_input_bundle(root: str | os.PathLike) -> dict:
    """只读扫描一整套 Li-S 吸附输入，自动分出共享 INCAR/slab/config。

    只有唯一且命名明确的候选才自动填入；多个 INCAR 或多个 clean/slab 候选
    必须由用户确认。带 ``Li2Sx``/``S8`` 物种提示的目录不会因名字含 ``slab``
    被误判为清洁表面。
    """
    base = Path(root).expanduser()
    if not base.is_dir():
        raise ValueError('本次计算文件夹不存在')
    if base.is_symlink():
        raise ValueError('本次计算文件夹不能是符号链接')
    base = base.resolve()
    structures = scan_structure_files(base)

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
    excluded = {item['path'] for item in clean_candidates}
    if clean_slab:
        excluded = {clean_slab}
    configs = [item for item in structures if item['path'] not in excluded]

    warnings = []
    if not incar_candidates:
        warnings.append('未找到固定 INCAR；请使用“选择 INCAR”补充')
    elif len(incar_candidates) > 1:
        warnings.append(f'找到 {len(incar_candidates)} 个 INCAR；为避免用错，请手动选择整组固定 INCAR')
    if not structures:
        warnings.append('未找到 POSCAR / CONTCAR / .vasp 结构文件')
    elif not clean_candidates:
        warnings.append('未能可靠识别 clean slab；请使用“选择 POSCAR”指定清洁表面')
    elif len(clean_candidates) > 1:
        warnings.append(
            f'找到 {len(clean_candidates)} 个 clean/slab 候选；请手动选择正确的清洁表面')
    return {
        'root': str(base),
        'incar': incar_candidates[0] if len(incar_candidates) == 1 else '',
        'incar_candidates': incar_candidates,
        'clean_slab': clean_slab,
        'clean_candidates': clean_candidates,
        'configs': configs,
        'structures': structures,
        'warnings': warnings,
        'source_read_only': True,
    }


# ── 项目创建(批量生成) ───────────────────────────────────────────────────────
def _unified_encut(incar_path, poscars: list, lib_root) -> int | None:
    """项目内各成员元素并集 → 统一 ENCUT(eV);算不出或用户已给 ENCUT → None。

    保吸附能 ΔE=E(slab+ads)−E(slab)−E(ref) 三个作业基组一致(缺口分析:此前各成员
    按自身元素补 ENCUT 会得不同截断能,静默污染 ΔE)。best-effort:任一步失败即回落
    逐成员(旧行为),绝不因统一逻辑拖垮生成。
    """
    # 用户显式给了 ENCUT → 尊重,不统一(共享 INCAR 本就一致)
    try:
        with open(incar_path, 'r', encoding='utf-8', errors='replace') as f:
            incar = parse_incar(f.read())
        if 'ENCUT' in {str(k).upper() for k in incar}:
            return None
    except (OSError, ValueError):
        return None
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
                   incar_path: str, ref_poscar: str | None = None,
                   lib_root: str | None = None, validate: bool = True,
                   kpoints=None, config_species: dict | None = None,
                   species_refs: dict | None = None,
                   species_ref_jobs: dict | None = None,
                   molecules_dir: str | None = None,
                   reference_project: str | None = None,
                   preparation: dict | None = None,
                   fail_if_exists: bool = False) -> dict:
    """批量生成 清洁表面 + 构型族 + (可选)气相参考,写 project.yaml 并登记台账。

    Returns:
        {'ok', 'project_path', 'project', 'generated': [(member, dir, warnings)],
         'errors': [(member, msg)]}。清洁表面生成失败 → 整体失败(其能量是公式必需项);
        个别构型失败只记入 errors,不拖垮全组。
    """
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

    # 项目级统一 ENCUT:各成员元素并集算一个 ENCUT,防 ΔE 大数相减被不同基组污染。
    # 仅当用户 INCAR 未显式给 ENCUT 时生效(用户值永远尊重);算不出则回落逐成员(旧行为)。
    all_poscars = [clean_poscar, *config_poscars] + ([ref_poscar] if ref_poscar else [])
    force_encut = _unified_encut(incar_path, all_poscars, lib_root) if validate else None

    def _gen(member: str, poscar: str, calc_type: str):
        out = root / member
        res = build_job_dir(poscar, incar_path, str(out), calc_type=calc_type,
                            kpoints=kpoints, validate=validate, lib_root=lib_root,
                            force_encut=force_encut)
        manifest_mod.create_from_build(str(out), res, poscar_path=poscar,
                                       validate=validate)
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
        source_species = {}
        for source, species in dict(config_species or {}).items():
            value = str(species or '').strip()
            if not value:
                continue
            source = os.path.expanduser(os.fspath(source))
            source_species[os.path.normcase(os.path.abspath(os.path.normpath(source)))] = value
        for p in config_poscars:
            member = f'{name}_ads_{_stem(p)}'
            try:
                config_dir = _gen(member, p, 'slab')
                config_dirs.append(config_dir)
                source_key = os.path.normcase(os.path.abspath(os.path.normpath(p)))
                if source_key in source_species:
                    config_species_out[str(Path(config_dir).resolve())] = source_species[source_key]
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
        advisories = _project_advisories(incar_path, [str(root / Path(d).name) for d in config_dirs],
                                         ref_poscar, generated, lib_root)
        if stage_root is not None:
            os.replace(stage_root, final_root)
        ppath = final_root / PROJECT_NAME
        for _member, job_dir, _warnings in generated:
            ledger.register(_published(job_dir))
        register_project(ppath)
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
def _member_info(job_dir: str | None):
    """成员目录 → (state, energy|None)。无目录/无 manifest → ('缺失', None)。"""
    if not job_dir:
        return '未设置', None
    m = manifest_mod.load_manifest(job_dir)
    if m is None:
        return '缺 job.yaml', None
    e = m.get('results', {}).get('energy_e0_eV')
    if (not isinstance(e, (int, float)) or isinstance(e, bool)
            or not math.isfinite(float(e)) or diagnose.energy_implausible(e)):
        return m.get('state', '?'), None
    return m.get('state', '?'), float(e)


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
    if not require_same_kpoints and check.get('issues'):
        # A gas molecule may legitimately use a different spin setting, and a
        # catalyst-only Hubbard U vector naturally differs from the molecule.
        # Keep both visible for audit, but only shared method invariants/POTCAR
        # identities are hard cross-cell blockers.
        soft_prefixes = ('ISPIN 不一致', 'DFT+U 不一致')
        soft = [issue for issue in check['issues'] if issue.startswith(soft_prefixes)]
        if soft:
            check['issues'] = [issue for issue in check['issues'] if issue not in soft]
            check['warnings'] = list(dict.fromkeys([
                *(check.get('warnings') or []),
                *(issue + '；气相/周期体系差异需人工核对' for issue in soft),
            ]))
            check['status'] = ('incompatible' if check['issues']
                               else ('verified' if not check['warnings'] else 'unverified'))
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
    status = 'incompatible' if issues else ('verified' if not warnings else 'unverified')
    return {'status': status, 'issues': issues, 'warnings': warnings,
            'checks': checks}


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
    slab_state, e_slab = _member_info(members.get('clean_slab'))
    has_gas_ref = bool(members.get('gas_ref'))
    ref_state, e_ref = _member_info(members.get('gas_ref')) if has_gas_ref else ('无', None)
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
        st, e_cfg = _member_info(cdir)
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
            blockers.append('构型能量缺失或不合理')
        if slab_state != 'DONE':
            blockers.append('清洁表面未完成')
        elif e_slab is None:
            blockers.append('清洁表面能量缺失或不合理')
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
                blockers.append('气相参考能量缺失或不合理')
        if method_check['status'] == 'incompatible':
            blockers.append('能量项方法不一致：' + '；'.join(method_check['issues']))
        if not blockers:
            if sp_ref is not None:
                delta = e_cfg - e_slab - sp_ref
            else:
                delta = e_cfg - e_slab - (e_ref if has_gas_ref else 0.0)
                if not has_gas_ref:
                    note = '未设气相参考:此值为 E(slab+ads)−E(slab)'
        else:
            note = '；'.join(blockers)
        rows.append({'name': name, 'state': st, 'e_config': e_cfg,
                     'delta_e': delta, 'note': note, 'species': row_species,
                     'reference_species': sp if reference_mode == 'species' else None,
                     'e_ref': actual_ref_energy,
                     'reference_state': reference_state,
                     'reference_job': reference_job,
                     'reference_source': reference_source,
                     'reference_valid': reference_valid,
                     'reference_note': reference_note,
                     'method_check': method_check,
                     'method_warnings': method_check['warnings']})

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
    method_status = ('incompatible' if method_issues
                     else ('verified' if not method_warnings else 'unverified'))
    return {'slab': (slab_state, e_slab), 'ref': (ref_state, e_ref),
            'has_ref': has_ref, 'reference_mode': reference_mode,
            'species_refs': resolved_refs, 'species_ref_cache': species_refs,
            'species_reference_evidence': list(reference_index.values()),
            'method_consistency': {'status': method_status, 'issues': method_issues,
                                   'warnings': method_warnings},
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
