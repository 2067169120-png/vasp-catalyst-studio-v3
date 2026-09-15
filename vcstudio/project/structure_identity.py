"""VASP structure identity and adsorbate-species grouping helpers.

Folder names are useful hints, but the auditable identity of an adsorption
configuration comes from ``composition(config) - composition(clean slab)``.
This module keeps that rule in one dependency-light place so result import and
new-job input discovery cannot silently disagree.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

from vcstudio.generate.poscar import parse_poscar_species, read_cell_vectors


ELEMENTS = frozenset(
    'H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn '
    'Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La '
    'Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po '
    'At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg '
    'Cn Nh Fl Mc Lv Ts Og'.split())


def formula_composition(formula: str) -> dict[str, int] | None:
    """Parse a plain chemical formula without accepting partial matches."""
    value = str(formula or '').strip()
    parts = re.findall(r'([A-Z][a-z]?)(\d*)', value)
    if not parts or ''.join(element + count for element, count in parts) != value:
        return None
    composition: dict[str, int] = {}
    for element, raw_count in parts:
        if element not in ELEMENTS:
            return None
        count = int(raw_count or 1)
        if count <= 0:
            return None
        composition[element] = composition.get(element, 0) + count
    return composition


def composition_formula(composition: dict[str, int], order=None) -> str:
    """Build a stable formula, preserving POSCAR order when it is available."""
    values = {str(element): int(count) for element, count in dict(composition or {}).items()
              if int(count) > 0}
    ordered = []
    for element in list(order or []):
        if element in values and element not in ordered:
            ordered.append(element)
    ordered.extend(sorted(element for element in values if element not in ordered))
    return ''.join(element + (str(values[element]) if values[element] != 1 else '')
                   for element in ordered)


def composition_key(composition: dict[str, int]) -> str:
    """Return an order-independent identity for one exact atom composition."""
    values = {str(element): int(count) for element, count in dict(composition or {}).items()
              if int(count) > 0}
    return '|'.join(f'{element}:{values[element]}' for element in sorted(values))


def poscar_facts(path) -> dict:
    """Read composition and scaled cell from POSCAR/CONTCAR without changing it."""
    source = Path(path).expanduser()
    # Callers naturally hold either one POSCAR path (new inputs) or a result
    # directory.  Prefer the final CONTCAR for results, then fall back to POSCAR
    # when no readable final structure exists.
    candidates = ([source / 'CONTCAR', source / 'POSCAR']
                  if source.is_dir() else [source])
    errors = []
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding='utf-8', errors='replace')
            elements, counts = parse_poscar_species(text)
            if (not elements or len(elements) != len(counts)
                    or any(element not in ELEMENTS for element in elements)
                    or any(not isinstance(count, int) or count <= 0 for count in counts)):
                raise ValueError('缺少合法的 VASP5 元素/计数行')
            composition: dict[str, int] = {}
            for element, count in zip(elements, counts):
                composition[element] = composition.get(element, 0) + count
            cell = read_cell_vectors(text)
            if not all(math.isfinite(float(value)) for vector in cell for value in vector):
                raise ValueError('晶格包含非有限数值')
            return {
                'ok': True, 'path': str(candidate.resolve()), 'elements': elements,
                'counts': counts, 'composition': composition,
                'formula': composition_formula(composition, elements),
                'natoms': sum(composition.values()), 'cell': cell, 'error': None,
            }
        except (OSError, ValueError) as exc:
            errors.append(f'{candidate.name}: {exc}')
    detail = '；'.join(errors) if errors else 'POSCAR/CONTCAR 不存在'
    return {
        'ok': False, 'path': str(source), 'elements': [], 'counts': [],
        'composition': {}, 'formula': '', 'natoms': 0, 'cell': [],
        'error': detail,
    }


def cells_equal(left, right, tolerance=1e-6) -> bool:
    """Return whether two fully scaled 3x3 cells are equal within tolerance."""
    try:
        return all(abs(float(left[i][j]) - float(right[i][j])) <= float(tolerance)
                   for i in range(3) for j in range(3))
    except (IndexError, TypeError, ValueError):
        return False


def composition_delta(clean: dict[str, int], config: dict[str, int]) -> dict:
    """Return config-clean, failing closed when config removes slab atoms."""
    clean = dict(clean or {})
    config = dict(config or {})
    removed = {
        element: clean.get(element, 0) - config.get(element, 0)
        for element in clean if config.get(element, 0) < clean.get(element, 0)
    }
    if removed:
        detail = '、'.join(f'{element}{count}' for element, count in sorted(removed.items()))
        return {'ok': False, 'composition': {}, 'formula': '',
                'error': f'构型比 clean slab 少原子：{detail}'}
    delta = {
        element: count - clean.get(element, 0)
        for element, count in config.items() if count - clean.get(element, 0) > 0
    }
    return {'ok': bool(delta), 'composition': delta,
            'formula': composition_formula(delta, config),
            'error': None if delta else '构型与 clean slab 原子组成相同，未识别到吸附物'}


def classify_against_clean(clean_path, config_path, *, name_hint='', reference_species=None) -> dict:
    """Infer one adsorbate formula from structure difference, with named fallback."""
    clean = poscar_facts(clean_path)
    config = poscar_facts(config_path)
    hint = str(name_hint or '').strip()
    fallback = {
        'species': hint, 'source': 'path_name' if hint else 'unresolved',
        'confidence': 'hint' if hint else 'unknown', 'confirmed': False,
        'name_hint': hint, 'composition': {}, 'formula': hint,
        'clean_structure': clean, 'config_structure': config,
        'cell_matches': False, 'warnings': [],
    }
    if not clean['ok'] or not config['ok']:
        errors = []
        if not clean['ok']:
            errors.append('clean slab POSCAR 无法解析：' + str(clean['error']))
        if not config['ok']:
            errors.append('构型 POSCAR 无法解析：' + str(config['error']))
        fallback['warnings'] = errors
        return fallback
    if not cells_equal(clean['cell'], config['cell']):
        fallback['warnings'] = ['构型与 clean slab 晶格不同，不能用组成差自动绑定物种']
        return fallback
    fallback['cell_matches'] = True
    delta = composition_delta(clean['composition'], config['composition'])
    if not delta['ok']:
        fallback['warnings'] = [str(delta['error'])]
        return fallback
    species = delta['formula']
    hint_composition = formula_composition(hint)
    warnings = []
    source = 'poscar_minus_clean_slab'
    reference_matches = [
        str(label).strip() for label in (reference_species or [])
        if str(label).strip()
        and formula_composition(str(label).strip()) == delta['composition']
    ]
    reference_matches = list(dict.fromkeys(reference_matches))
    confidence = 'exact'
    confirmed = True
    if len(reference_matches) == 1:
        species = reference_matches[0]
        source += '+reference_library'
    elif len(reference_matches) > 1:
        # Stoichiometry alone cannot choose between two user-facing reference
        # labels that describe the same atoms (for example Li2S8/S8Li2, or two
        # isomers/multiplicities given different names).  Keep the structural
        # formula visible, but require an explicit cleanup/choice instead of
        # silently taking the first dictionary entry.
        species = delta['formula']
        source += '+ambiguous_reference_library'
        confidence = 'ambiguous'
        confirmed = False
        warnings.append(
            '结构组成同时匹配多个参考标签：' + '、'.join(reference_matches)
            + '；不能仅凭化学计量自动选择，请合并重复参考或人工确认')
    elif hint_composition == delta['composition']:
        species = hint
        source += '+path_name'
    elif hint:
        warnings.append(
            f'文件夹/文件名提示为 {hint}，但 POSCAR−clean slab 实际为 {species}；'
            '已采用结构组成，提交前仍会复核')
    return {
        'species': species, 'source': source, 'confidence': confidence,
        'confirmed': confirmed, 'name_hint': hint,
        'composition': delta['composition'], 'formula': delta['formula'],
        'composition_key': composition_key(delta['composition']),
        'reference_matches': reference_matches,
        'clean_structure': clean, 'config_structure': config,
        'cell_matches': True, 'warnings': warnings,
    }


def infer_clean_candidate(items) -> dict:
    """Find a unique structure that is the same-cell subset of the most peers."""
    rows = [dict(item or {}) for item in items or []]
    scored = []
    for candidate in rows:
        facts = candidate.get('structure') or poscar_facts(candidate.get('path'))
        if not facts.get('ok'):
            continue
        explained = []
        for other in rows:
            if other.get('path') == candidate.get('path'):
                continue
            other_facts = other.get('structure') or poscar_facts(other.get('path'))
            if (not other_facts.get('ok')
                    or not cells_equal(facts.get('cell'), other_facts.get('cell'))):
                continue
            delta = composition_delta(
                facts.get('composition') or {}, other_facts.get('composition') or {})
            if delta.get('ok'):
                explained.append(str(other.get('path') or ''))
        if explained:
            scored.append({
                'path': str(candidate.get('path') or ''), 'score': len(explained),
                'explained_paths': explained, 'natoms': facts.get('natoms'),
            })
    if not scored:
        return {'path': '', 'confidence': 'unknown', 'candidates': [],
                'reason': '没有结构能作为同晶胞构型的严格组成子集'}
    scored.sort(key=lambda row: (-row['score'], row.get('natoms') or 0, row['path'].casefold()))
    best = scored[0]
    tied = [row for row in scored if row['score'] == best['score']]
    if len(tied) != 1:
        return {'path': '', 'confidence': 'ambiguous', 'candidates': tied,
                'reason': f'有 {len(tied)} 个结构都能解释 {best["score"]} 个同晶胞构型'}
    # One subset/superset pair is not enough to decide which file is the clean
    # surface in a generic two-structure folder: it may instead be a defect,
    # reconstruction, different coverage, or an accidentally mixed structure.
    # A clean-name hint is handled by the caller; structural auto-selection is
    # reserved for a repeated family with at least two independently explained
    # configurations.
    if best['score'] < 2:
        return {
            'path': '', 'confidence': 'insufficient', 'candidates': scored,
            'reason': '只找到 1 个可解释的同晶胞结构；至少需要 2 个构型'
                      '共同支持，否则请人工指定 clean slab',
        }
    return {**best, 'confidence': 'exact', 'candidates': scored,
            'reason': f'POSCAR 组成与晶格可解释 {best["score"]} 个吸附构型'}


def species_groups(items) -> list[dict]:
    """Create a JSON-friendly species→configuration grouping summary."""
    grouped: dict[str, list[dict]] = {}
    for raw in items or []:
        item = dict(raw or {})
        species = str(item.get('species') or '').strip()
        assignment = item.get('assignment') or {}
        composition = (item.get('adsorbate_composition')
                       or assignment.get('composition')
                       or formula_composition(species) or {})
        key = composition_key(composition) or (f'label:{species.casefold()}' if species else '未识别')
        grouped.setdefault(key, []).append(item)
    rows = []
    for key in sorted(grouped, key=lambda value: (value == '未识别', value.casefold())):
        members = grouped[key]
        first = members[0]
        assignment = first.get('assignment') or {}
        composition = (first.get('adsorbate_composition')
                       or assignment.get('composition')
                       or formula_composition(str(first.get('species') or '')) or {})
        labels = [str(item.get('species') or '').strip() for item in members
                  if str(item.get('species') or '').strip()]
        species = (labels[0] if labels and len({label.casefold() for label in labels}) == 1
                   else composition_formula(composition))
        species = species or None
        rows.append({
            'key': key, 'species': species,
            'label': species or '未识别', 'composition': composition,
            'count': len(members),
            'paths': [str(item.get('path') or '') for item in members],
            'names': [str(item.get('name') or Path(str(item.get('path') or '')).name)
                      for item in members],
            'all_exact': all(item.get('species_confidence') == 'exact' for item in members),
        })
    return rows


def build_dataset_groups(config_paths, config_species, reference_jobs=None) -> list[dict]:
    """Persist stable groups used by ΔE tables/reports without name re-guessing."""
    mapping = dict(config_species or {})
    references = dict(reference_jobs or {})
    reference_by_key: dict[str, tuple[str, str | None]] = {}
    for raw_label, job in references.items():
        label = str(raw_label or '').strip()
        composition = formula_composition(label)
        key = (f'composition:{composition_key(composition)}' if composition
               else f'label:{label.casefold()}')
        previous = reference_by_key.get(key)
        if previous and previous[0] != label:
            raise ValueError(
                f'参考标签 {previous[0]} 与 {label} 具有相同原子组成；'
                '后端无法只凭 POSCAR 判断应使用哪个参考，请先合并或重命名')
        reference_by_key[key] = (label, job)

    grouped: dict[str, dict] = {}
    for path in config_paths or []:
        label = str(mapping.get(str(path)) or '').strip()
        composition = formula_composition(label)
        key = (f'composition:{composition_key(composition)}' if composition
               else f'label:{label.casefold()}' if label else 'unresolved')
        group = grouped.setdefault(key, {
            'composition': composition or {}, 'labels': [], 'configs': [],
        })
        group['configs'].append(str(path))
        if label and label not in group['labels']:
            group['labels'].append(label)

    rows = []
    for key, group in sorted(
            grouped.items(), key=lambda item: (item[0] == 'unresolved', item[0])):
        reference = reference_by_key.get(key)
        if reference:
            species, reference_job = reference
        elif group['composition']:
            species = composition_formula(group['composition'])
            reference_job = None
        else:
            species = group['labels'][0] if group['labels'] else None
            reference_job = references.get(species) if species else None
        rows.append({
            'group_id': key,
            'species': species,
            'configs': group['configs'], 'n_configs': len(group['configs']),
            'reference_job': reference_job,
        })
    return rows


__all__ = [
    'ELEMENTS', 'build_dataset_groups', 'cells_equal', 'classify_against_clean',
    'composition_delta', 'composition_formula', 'composition_key', 'formula_composition',
    'infer_clean_candidate', 'poscar_facts', 'species_groups',
]
