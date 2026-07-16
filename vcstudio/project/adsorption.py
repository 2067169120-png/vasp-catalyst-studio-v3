"""吸附能项目:清洁表面 + N 个吸附组态 + 气相参考 → 批量生成、ΔE 汇总、CSV 导出。

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
import time
from pathlib import Path

import yaml

from vcstudio.cluster import ledger
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


def _config_species(member_name: str, proj_name: str) -> str:
    """组态成员名 → 吸附物种短名(多构型取最稳的分组键)。

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


def _stem(path: str) -> str:
    """文件名(去扩展名)→ 目录名安全形式。"""
    s = Path(path).stem
    s = _STEM_RE.sub('_', s) or 'config'
    return s


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


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
                   kpoints=None) -> dict:
    """批量生成 清洁表面 + 组态族 + (可选)气相参考,写 project.yaml 并登记台账。

    Returns:
        {'ok', 'project_path', 'project', 'generated': [(member, dir, warnings)],
         'errors': [(member, msg)]}。清洁表面生成失败 → 整体失败(其能量是公式必需项);
        个别组态失败只记入 errors,不拖垮全组。
    """
    root = Path(root)
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
        ledger.register(str(out))
        generated.append((member, str(out), res['warnings']))
        return str(out)

    # 1) 清洁表面(必需;失败即整体失败)
    slab_member = f'{name}_slab_clean'
    slab_dir = _gen(slab_member, clean_poscar, 'slab')

    # 2) 组态族(个别失败不拖垮全组)
    config_dirs = []
    for p in config_poscars:
        member = f'{name}_ads_{_stem(p)}'
        try:
            config_dirs.append(_gen(member, p, 'slab'))
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

    project = {
        'schema': 1,
        'name': name,
        'created_at': _now(),
        'root': str(root.resolve()),
        'members': {
            'clean_slab': slab_dir,
            'gas_ref': ref_dir,
            'configs': config_dirs,
        },
    }
    ppath = save_project(root, project)
    register_project(ppath)
    advisories = _project_advisories(incar_path, config_dirs, ref_poscar,
                                     generated, lib_root)
    return {'ok': True, 'project_path': str(ppath), 'project': project,
            'generated': generated, 'errors': errors, 'advisories': advisories}


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
    return m.get('state', '?'), (float(e) if isinstance(e, (int, float)) else None)


def delta_e_rows(project: dict) -> dict:
    """按当前各成员 job.yaml 计算 ΔE 表。

    Returns:
        {'slab': (state, E), 'ref': (state, E|None), 'has_ref': bool,
         'rows': [{'name','state','e_config','delta_e','note',
                   'species','is_most_stable','dd_e'}]}
    规则:ΔE 只在 组态 DONE 且 slab DONE 且(有参考时)ref DONE 时给出;否则 note 说明缺谁。
    多构型取最稳:同一 species(组态名剥前缀识别)内 delta_e 最低者 is_most_stable=True,
    dd_e 为组内相对最稳的 ΔΔE(最稳 0.0);delta_e 为 None 的行 is_most_stable=False、dd_e=None。
    """
    members = project.get('members') or {}
    proj_name = str(project.get('name') or '')
    slab_state, e_slab = _member_info(members.get('clean_slab'))
    has_ref = bool(members.get('gas_ref'))
    ref_state, e_ref = _member_info(members.get('gas_ref')) if has_ref else ('无', None)
    # 逐物种气相参考(原版 lis_sac_analysis 口径):project['species_refs']=
    # {物种: E_mol};组态名以 '_<物种>' 结尾即匹配。与单一 gas_ref 互斥,优先。
    species_refs = dict(project.get('species_refs') or {})

    rows = []
    for cdir in (members.get('configs') or []):
        name = os.path.basename(os.path.normpath(cdir))
        st, e_cfg = _member_info(cdir)
        delta, note = None, ''
        sp_ref = None
        if species_refs:
            sp = next((s for s in sorted(species_refs, key=len, reverse=True)
                       if name.endswith('_' + s)), None)
            sp_ref = species_refs.get(sp)
        blockers = []
        if st != 'DONE' or e_cfg is None:
            blockers.append('组态未完成')
        if slab_state != 'DONE' or e_slab is None:
            blockers.append('清洁表面未完成')
        if species_refs and sp_ref is None:
            blockers.append('无匹配物种参考能量')
        elif has_ref and not species_refs and (ref_state != 'DONE' or e_ref is None):
            blockers.append('气相参考未完成')
        if not blockers:
            if sp_ref is not None:
                delta = e_cfg - e_slab - sp_ref
            else:
                delta = e_cfg - e_slab - (e_ref if has_ref else 0.0)
                if not has_ref:
                    note = '未设气相参考:此值为 E(slab+ads)−E(slab)'
        else:
            note = '；'.join(blockers)
        rows.append({'name': name, 'state': st, 'e_config': e_cfg,
                     'delta_e': delta, 'note': note,
                     'species': _config_species(name, proj_name)})

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

    return {'slab': (slab_state, e_slab), 'ref': (ref_state, e_ref),
            'has_ref': has_ref, 'rows': rows}


def export_csv(project: dict, summary: dict, out_path: str | os.PathLike) -> Path:
    """ΔE 表导出 CSV(utf-8-sig:Excel 双击打开中文/负号不乱码)。"""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    slab_state, e_slab = summary['slab']
    ref_state, e_ref = summary['ref']
    with open(out, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow([f"吸附能项目:{project.get('name','')}", f'导出时间:{_now()}'])
        w.writerow([f'E(slab)={_fmt(e_slab)} eV({slab_state})',
                    f'E(ref)={_fmt(e_ref)} eV({ref_state})' if summary['has_ref']
                    else 'E(ref)=未设置'])
        w.writerow([])
        w.writerow(['组态', '状态', 'E(slab+ads) / eV', 'ΔE_ads / eV',
                    'ΔΔE(eV)', '最稳位?', '备注'])
        for r in summary['rows']:
            w.writerow([r['name'], r['state'], _fmt(r['e_config']),
                        _fmt(r['delta_e']), _fmt(r.get('dd_e')),
                        '是' if r.get('is_most_stable') else '', r['note']])
    return out


def _fmt(v) -> str:
    return f'{v:.6f}' if isinstance(v, float) else ''
