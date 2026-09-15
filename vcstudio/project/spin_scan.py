"""多自旋初猜并跑 + 磁矩/能量口径守卫(F3/F10)——防 0.1–0.5 eV 的静默错数。

自旋态选错是催化 DFT 最隐蔽的系统误差之一:M–N–C 单原子催化剂初猜高自旋却应低自旋
(或反之),自洽能量可偏 0.1–0.5 eV,直接污染 ΔE/ΔG/U_L,且**不报错**。本模块:

- ``spin_candidates`` / ``nupdown_ladder``:按体系磁性元素生成多组自旋初猜(非磁/低/高自旋,
  或 NUPDOWN 定值梯度),供"并跑取基态"。
- ``build_spin_variants``:对一个已生成作业目录克隆 N 份变体(仅改 INCAR 的 MAGMOM/自旋键)。
- ``pick_ground_state``:全部 DONE 后按能量判自旋基态,近简并给人工确认提示。
- ``audit_magmom``:末态磁矩 vs 初猜,塌零/翻转 → 中文告警(磁矩守卫)。
- ``electronic_entropy_check``:电子熵 T*S 超标 → 提示减小 SIGMA / 用 E(sigma→0)(F10 能量口径守卫)。

复用 incar_builder.build_magmom / manifest / freeenergy.read_e0,不另造 parser。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from vcstudio.generate.poscar import parse_poscar_species
from vcstudio.generate.incar_builder import build_magmom
from vcstudio.shared import manifest as manifest_mod
from vcstudio.project.freeenergy import read_e0

# 高自旋经验磁矩(μB,初猜非终值):3d 按未配对 d 电子量级,4d/5d(Mo/W)偏小。
HIGH_SPIN_MOMENTS = {
    'V': 3, 'Cr': 5, 'Mn': 5, 'Fe': 4, 'Co': 3, 'Ni': 2, 'Cu': 1, 'Mo': 3, 'W': 2,
}
# 3d 过渡金属:含之则额外加"低自旋"候选(4d/5d 低/高自旋差异小,不单列 ls)。
THREE_D_METALS = frozenset({'V', 'Cr', 'Mn', 'Fe', 'Co', 'Ni', 'Cu'})

NEAR_DEGENERATE_MEV = 10.0        # 自旋基态能量差 <此 → 近简并,提示人工确认
_COLLAPSE_INIT = 2.0              # 初猜磁矩 ≥此且末态塌到 <_COLLAPSE_FINAL → 判塌零
_COLLAPSE_FINAL = 0.3
_FLIP_MIN = 0.3                   # 末态 |μ| ≥此且符号相反 → 判翻转(而非塌零)


# ── 自旋初猜族 ──────────────────────────────────────────────────────────────────
def spin_candidates(elements, *, magnetic_elements=None) -> list:
    """按体系磁性元素生成自旋初猜族 → ``[{'name','magmom_overrides','nupdown'}]``。

    - 非磁 ``nm``(磁性元素初猜 0)/ 高自旋 ``hs``(HIGH_SPIN_MOMENTS)/ 低自旋 ``ls``
      (高自旋减半向下取整;仅体系含 3d 金属时加)。
    - 含磁性元素时至少 [nm, hs] 两个,3d 体系再加 ls,按磁矩升序返回 [nm,(ls,)hs]。
    - 无磁性元素 → 单个 ``nm``(自旋扫描无意义,不造重复变体)。
    - magnetic_elements 可覆盖高自旋磁矩表({元素:μB});3d 判定仍按 THREE_D_METALS。
    每候选 magmom_overrides 为 {磁性元素:μB},喂给 incar_builder.build_magmom;nupdown=None。
    """
    hs_table = dict(HIGH_SPIN_MOMENTS)
    if magnetic_elements:
        hs_table.update(magnetic_elements)
    # 体系出现的磁性元素(去重保序)
    seen, present = set(), []
    for el in elements:
        if el in hs_table and el not in seen:
            seen.add(el)
            present.append(el)

    if not present:
        return [{'name': 'nm', 'magmom_overrides': {}, 'nupdown': None}]

    nm = {'name': 'nm', 'magmom_overrides': {el: 0 for el in present}, 'nupdown': None}
    hs = {'name': 'hs', 'magmom_overrides': {el: hs_table[el] for el in present}, 'nupdown': None}
    cands = [nm]
    if any(el in THREE_D_METALS for el in present):
        ls = {'name': 'ls',
              'magmom_overrides': {el: hs_table[el] // 2 for el in present}, 'nupdown': None}
        cands.append(ls)
    cands.append(hs)
    return cands


def nupdown_ladder(max_unpaired: int) -> list:
    """NUPDOWN 定值扫描族:固定净自旋(未配对电子数)0..max_unpaired 各一候选。

    NUPDOWN 强制 n↑−n↓ 定值,比 MAGMOM 初猜更硬,适合逐个净自旋态穷举。
    max_unpaired 须为非负整数,否则 ValueError。magmom_overrides=None(不改 MAGMOM,只设 NUPDOWN)。
    """
    if not isinstance(max_unpaired, int) or max_unpaired < 0:
        raise ValueError(f'max_unpaired 须为非负整数,收到 {max_unpaired!r}')
    return [{'name': f'nupdown{n}', 'magmom_overrides': None, 'nupdown': n}
            for n in range(max_unpaired + 1)]


# ── INCAR 键 upsert(子句级,保留注释与其余键) ─────────────────────────────────
def _split_comment(line: str):
    idxs = [i for i in (line.find('#'), line.find('!')) if i != -1]
    if idxs:
        i = min(idxs)
        return line[:i], line[i:]
    return line, ''


def _upsert_incar_key(text: str, key: str, value) -> tuple:
    """设置/替换 INCAR 单键 → (新文本, old_value|None)。缺则追加于文末,只动该键。"""
    key = key.upper()
    out, old, matched = [], None, False
    for line in text.splitlines():
        code, comment = _split_comment(line)
        if '=' not in code:
            out.append(line)
            continue
        new_clauses, hit = [], False
        for clause in code.split(';'):
            if '=' in clause and clause.split('=', 1)[0].strip().upper() == key:
                old = clause.split('=', 1)[1].strip()
                new_clauses.append(f'{key} = {value}')
                hit = matched = True
            elif clause.strip():
                new_clauses.append(clause.strip())
        if hit:
            merged = ' ; '.join(new_clauses)
            out.append(merged + (('  ' + comment) if comment else ''))
        else:
            out.append(line)
    if not matched:
        out.append(f'{key} = {value}')
    return '\n'.join(out) + '\n', old


# ── 变体克隆 ────────────────────────────────────────────────────────────────────
def build_spin_variants(job_dir, out_root, *, candidates=None,
                        magnetic_elements=None) -> list:
    """对一个已生成作业目录克隆 N 份自旋变体,仅改 INCAR 的 MAGMOM/ISPIN/NUPDOWN。

    - candidates 缺省 = spin_candidates(POSCAR 元素);目录名 ``<job>_spin_<name>``。
    - 每变体复制四件套,改写 INCAR:按候选 magmom_overrides 写 MAGMOM(build_magmom)、
      确保 ISPIN=2、NUPDOWN 候选设 NUPDOWN;job.yaml 记 spin_variant 名与 parent。
    Returns ``[{'name','out_dir','magmom','changes','warnings'}]``。
    """
    job_dir = Path(job_dir)
    poscar_text = _read(job_dir / 'POSCAR')
    if poscar_text is None:
        raise ValueError(f'作业目录缺 POSCAR,无法生成自旋变体:{job_dir}')
    base_incar = _read(job_dir / 'INCAR')
    if base_incar is None:
        raise ValueError(f'作业目录缺 INCAR,无法生成自旋变体:{job_dir}')
    syms, counts = parse_poscar_species(poscar_text)

    cands = candidates if candidates is not None else \
        spin_candidates(syms, magnetic_elements=magnetic_elements)
    parent_manifest = manifest_mod.load_manifest(job_dir)
    parent_id = (parent_manifest or {}).get('job_id') or job_dir.name
    out_root = Path(out_root)

    results = []
    for cand in cands:
        name = cand['name']
        dest = out_root / f'{job_dir.name}_spin_{name}'
        dest.mkdir(parents=True, exist_ok=True)
        # A spin variant is a new job generation, not a clone of the parent's
        # scheduler/results identity.  Copy only scientific VASP inputs; the
        # new authoritative manifest is written once after INCAR is final.
        for filename in ('POSCAR', 'INCAR', 'KPOINTS', 'POTCAR'):
            source = job_dir / filename
            if source.is_file():
                shutil.copy2(source, dest / filename)

        incar = base_incar
        changes, warnings = [], []
        magmom = None
        if cand.get('magmom_overrides') is not None:
            magmom = build_magmom(syms, counts, cand['magmom_overrides'])
            if magmom is None:
                warnings.append('POSCAR 无可用 counts(VASP4/畸形),无法写 MAGMOM;仅设 ISPIN=2。')
            else:
                incar, old = _upsert_incar_key(incar, 'MAGMOM', magmom)
                changes.append({'key': 'MAGMOM', 'old': old, 'new': magmom})
        # 自旋扫描前提:ISPIN=2(缺/非2 都纠正,否则 MAGMOM/NUPDOWN 无效,静默算成非磁)
        incar, old_ispin = _upsert_incar_key(incar, 'ISPIN', 2)
        if old_ispin != '2':
            changes.append({'key': 'ISPIN', 'old': old_ispin, 'new': 2})
        if cand.get('nupdown') is not None:
            incar, old_nu = _upsert_incar_key(incar, 'NUPDOWN', cand['nupdown'])
            changes.append({'key': 'NUPDOWN', 'old': old_nu, 'new': cand['nupdown']})

        with open(dest / 'INCAR', 'w', encoding='utf-8') as f:
            f.write(incar)

        _annotate_variant_manifest(dest, parent_manifest, parent_id, str(job_dir.resolve()),
                                   name, magmom, changes, warnings)
        results.append({'name': name, 'out_dir': str(dest), 'magmom': magmom,
                        'changes': changes, 'warnings': warnings})
    return results


def _annotate_variant_manifest(dest, parent_manifest, parent_id, parent_dir,
                               name, magmom, changes, warnings):
    """Write a fresh spin-variant manifest after final INCAR bytes exist."""
    parent = parent_manifest if isinstance(parent_manifest, dict) else {}
    parent_inputs = parent.get('inputs') if isinstance(parent.get('inputs'), dict) else {}
    inputs = {
        key: value for key, value in parent_inputs.items()
        if key not in {
            'execution_environment', 'input_closure', 'sha256', 'potcar_sha256',
            'method_recipe', 'method_recipe_status',
        }
    }
    inputs['engine'] = 'vasp'
    inputs['spin_magmom'] = magmom
    inputs['spin_changes'] = changes
    from vcstudio.generate.method_recipe import builder_recipe
    inputs['method_recipe'] = builder_recipe(
        builder='vcstudio.project.spin_scan/v1', task_type='spin_scan',
        calc_type=str(parent.get('calc_type') or 'slab'), validate=True,
        completions={'incar_changes': changes}, kpoints_source='parent-copy',
        extra={'variant': name, 'magmom': magmom})
    system = str(parent.get('system') or Path(dest).name)
    m = manifest_mod.new_manifest(
        job_id=Path(dest).name, system=system, task_type='spin_scan',
        calc_type=str(parent.get('calc_type') or 'slab'), inputs=inputs,
        warnings=list(parent.get('warnings') or []) + list(warnings or []))
    m['spin_variant'] = name
    m['parent_job'] = parent_id
    m['parent_job_dir'] = parent_dir
    # Resolve side inputs from the final spin-variant INCAR/task, then copy
    # only those scientific dependencies from the parent.  This preserves
    # restart/kernel/constraint inputs without cloning outputs or scheduler
    # journals.  KPOINTS_OPT is presence-triggered, so carry that known input
    # when it exists in the parent even for a legacy parent manifest.
    from vcstudio.shared.scientific_inputs import (
        record_input_closure, required_input_names,
    )
    required, _reasons, _invalid = required_input_names(dest, m)
    if (Path(parent_dir) / 'KPOINTS_OPT').is_file():
        required.append('KPOINTS_OPT')
    parent_root = Path(parent_dir).resolve()
    for filename in dict.fromkeys(required):
        target = Path(dest).joinpath(*filename.split('/'))
        if target.is_file():
            continue
        source = parent_root.joinpath(*filename.split('/'))
        try:
            resolved = source.resolve(strict=True)
            inside = os.path.normcase(os.path.commonpath(
                (str(parent_root), str(resolved)))) == os.path.normcase(str(parent_root))
        except (OSError, ValueError):
            continue
        traversed = parent_root
        has_link = False
        for part in Path(filename).parts:
            traversed /= part
            if traversed.is_symlink():
                has_link = True
                break
        if inside and not has_link and source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    record_input_closure(dest, m)
    manifest_mod.save_manifest(dest, m)
    return m


# ── 基态判定 ────────────────────────────────────────────────────────────────────
def _variant_name(d) -> str:
    base = os.path.basename(str(d).rstrip('/\\'))
    i = base.rfind('_spin_')
    return base[i + len('_spin_'):] if i != -1 else base


def pick_ground_state(variant_dirs) -> dict:
    """读各变体 manifest/OSZICAR 能量,全部 DONE 才判自旋基态。

    每变体能量:优先 manifest results.energy_e0_eV,退 freeenergy.read_e0(OSZICAR)。
    任一未完成(state≠DONE)或缺能量 → ``{'pending':[名字...]}``(不臆断)。
    全就绪 → ``{'winner','energies':{name:E},'de_meV':{name:相对最低 meV},'warning'}``;
    最低两态差 <10 meV 时 warning 提示近简并、建议人工确认。
    """
    infos = []
    for d in variant_dirs:
        m = manifest_mod.load_manifest(d)
        name = (m or {}).get('spin_variant') or _variant_name(d)
        state = (m or {}).get('state')
        energy = ((m or {}).get('results') or {}).get('energy_e0_eV')
        if energy is None:
            energy = read_e0(d)                       # OSZICAR 兜底
        infos.append({'name': name, 'state': state, 'energy': energy})

    pending = [i['name'] for i in infos if i['state'] != 'DONE' or i['energy'] is None]
    if pending:
        return {'pending': pending}

    energies = {i['name']: i['energy'] for i in infos}
    winner = min(energies, key=lambda k: energies[k])
    emin = energies[winner]
    de_meV = {k: round((v - emin) * 1000.0, 3) for k, v in energies.items()}
    warning = None
    ordered = sorted(energies.values())
    if len(ordered) >= 2 and (ordered[1] - ordered[0]) * 1000.0 < NEAR_DEGENERATE_MEV:
        warning = (f'最低两自旋态能量差 <{NEAR_DEGENERATE_MEV:g} meV(近简并),'
                   f'基态判定不确定,建议人工确认磁矩与占据。')
    return {'winner': winner, 'energies': energies, 'de_meV': de_meV, 'warning': warning}


# ── 磁矩守卫(F3) ──────────────────────────────────────────────────────────────
_MAG_RE = re.compile(r'magnetization\s+([-+]?[\d.]+(?:[eE][-+]?\d+)?)')


def _final_magnetization(outcar_text: str):
    """OUTCAR 末态总磁化(取最后一个 'number of electron ... magnetization X');无 → None。"""
    hits = _MAG_RE.findall(outcar_text or '')
    if not hits:
        return None
    try:
        return float(hits[-1])
    except ValueError:
        return None


def audit_magmom(outcar_text: str, initial_magmom) -> dict:
    """末态磁矩 vs 初猜守卫 → ``{'final_magnetization','initial_magmom','collapsed','flipped',
    'audited','warning'}``。

    initial_magmom:初猜(总)磁矩(SAC 单磁性原子体系即该原子矩)。
    - 塌零:|初猜| ≥2 且 |末态| <0.3 → 自旋极化丢失(疑非磁亚稳态)。
    - 翻转:|末态| ≥0.3 且与初猜符号相反 → 自旋方向反转。
    OUTCAR 无磁化信息(ISPIN=1/非自旋) → audited=False,warning 提示无法审计。
    """
    final = _final_magnetization(outcar_text)
    try:
        init = float(initial_magmom)
    except (TypeError, ValueError):
        init = 0.0
    if final is None:
        return {'final_magnetization': None, 'initial_magmom': init, 'collapsed': False,
                'flipped': False, 'audited': False,
                'warning': 'OUTCAR 未含磁化信息(可能 ISPIN=1 非自旋计算),无法审计磁矩。'}

    collapsed = abs(init) >= _COLLAPSE_INIT and abs(final) < _COLLAPSE_FINAL
    flipped = (abs(final) >= _FLIP_MIN and abs(init) >= _FLIP_MIN
               and (init > 0) != (final > 0))
    warning = None
    if collapsed:
        warning = (f'磁矩塌缩:初猜 {init:g} μB → 末态 {final:.2f} μB(自旋极化丢失);'
                   f'疑收敛到非磁亚稳态,建议换初猜或核对 ISPIN/MAGMOM。')
    elif flipped:
        warning = (f'磁矩翻转:初猜 {init:g} μB → 末态 {final:.2f} μB(符号相反);'
                   f'自洽后自旋方向反转,请核对是否为目标磁序。')
    return {'final_magnetization': final, 'initial_magmom': init, 'collapsed': collapsed,
            'flipped': flipped, 'audited': True, 'warning': warning}


# ── 电子熵口径守卫(F10) ───────────────────────────────────────────────────────
_EENTRO_RE = re.compile(r'EENTRO\s*=\s*([-+]?[\d.]+(?:[eE][-+]?\d+)?)')
_NIONS_RE = re.compile(r'NIONS\s*=\s*(\d+)')
_IONS_PER_TYPE_RE = re.compile(r'ions per type\s*=\s*([\d\s]+)')


def _natoms_from_outcar(outcar_text: str):
    """OUTCAR 原子数:优先 NIONS,退 'ions per type = ...' 求和;都无 → None。"""
    m = _NIONS_RE.search(outcar_text or '')
    if m:
        return int(m.group(1))
    m = _IONS_PER_TYPE_RE.search(outcar_text or '')
    if m:
        nums = [int(x) for x in m.group(1).split()]
        if nums:
            return sum(nums)
    return None


def electronic_entropy_check(outcar_text: str, *,
                             threshold_meV_per_atom: float = 1.0) -> dict:
    """电子熵(展宽熵)口径守卫 → 每原子 T*S 超阈告警(F10)。

    解析 OUTCAR 的 EENTRO(entropy T*S,eV)与原子数 → |T*S|/atom(meV/atom)。
    超阈(默认 1.0 meV/atom)时 warning 建议减小 SIGMA 或改用 E(sigma→0),防 ΔE 被展宽熵污染。
    Returns ``{'eentro_ev','natoms','ts_meV_per_atom','over_threshold','threshold_meV_per_atom',
    'warning'}``;缺 EENTRO/原子数 → 对应值 None 且 warning 说明无法核算。
    """
    result = {'eentro_ev': None, 'natoms': None, 'ts_meV_per_atom': None,
              'over_threshold': False, 'threshold_meV_per_atom': float(threshold_meV_per_atom),
              'warning': None}
    hits = _EENTRO_RE.findall(outcar_text or '')
    if not hits:
        result['warning'] = 'OUTCAR 未找到 EENTRO(可能非 VASP OUTCAR 或未完成),无法核算电子熵。'
        return result
    try:
        eentro = float(hits[-1])                       # 取最后(收敛)值
    except ValueError:
        result['warning'] = 'EENTRO 解析失败,无法核算电子熵。'
        return result
    natoms = _natoms_from_outcar(outcar_text)
    result['eentro_ev'] = eentro
    result['natoms'] = natoms
    if not natoms:
        result['warning'] = 'OUTCAR 未能确定原子数(NIONS/ions per type),无法给出每原子电子熵。'
        return result
    ts_per_atom = abs(eentro) / natoms * 1000.0
    result['ts_meV_per_atom'] = round(ts_per_atom, 4)
    if ts_per_atom > threshold_meV_per_atom:
        result['over_threshold'] = True
        result['warning'] = (
            f'电子熵 T*S={ts_per_atom:.2f} meV/atom 超标(阈 {threshold_meV_per_atom:g});'
            f'ΔE 口径建议减小 SIGMA 或改用 E(sigma→0)(能量外推值)。')
    return result


def _read(path):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    except OSError:
        return None
