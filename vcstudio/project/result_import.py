"""Import user-owned VASP inputs/results into a managed adsorption project.

The importer has two deliberately separate phases:

``scan_root`` / ``preview_project``
    Read-only discovery and scientific classification.  A preview never creates
    the output directory, ledger, registry, or a temporary staging directory.

``import_project``
    Copy an explicit allowlist into a sibling staging directory, write manifests
    and ``project.yaml`` there, then atomically rename the complete tree into its
    final location.  Source directories are never modified.

Roles are explicit at commit time: ``clean``, ``config``, ``ref`` (a single gas
reference), and ``molecule`` (a species-specific reference).  Role suggestions
are intentionally conservative: a directory named only ``Li2S4`` is ambiguous;
it is suggested as a molecular reference only when its path also contains an
unambiguous reference marker such as ``refs``, ``molecules`` or ``mol_``.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from pathlib import Path

from vcstudio.cluster import diagnose, ledger
from vcstudio.engines.calcspec import parse_structure
from vcstudio.generate import methods_text
from vcstudio.generate.incar_builder import parse_incar
from vcstudio.generate.job_builder import _infer_task_type
from vcstudio.generate.poscar import parse_poscar_species
from vcstudio.project import adsorption, freeenergy
from vcstudio.shared import manifest as manifest_mod


INPUT_FILES = ('INCAR', 'POTCAR', 'KPOINTS', 'POSCAR')
OUTPUT_MARKERS = ('OUTCAR', 'OSZICAR', 'vasprun.xml', 'vasprun', 'CONTCAR')
# CONTCAR is often kept beside an edited quartet as a convenient starting
# geometry.  By itself it is not evidence that this directory contains a
# completed calculation and must not turn a submittable quartet into
# NEEDS_HUMAN.
RESULT_EVIDENCE_FILES = ('OUTCAR', 'OSZICAR', 'vasprun.xml', 'vasprun')

# The normal import is intentionally small enough to be safe for report generation
# and resubmission.  Large wavefunction/density products are opt-in.
BASIC_COPY_FILES = (
    *INPUT_FILES,
    'OUTCAR', 'OSZICAR', 'CONTCAR',
    'vasp.out', 'vasp.log', 'stdout', 'stderr',
)
LARGE_OUTPUT_FILES = (
    'vasprun.xml', 'vasprun', 'CHGCAR', 'CHG', 'WAVECAR', 'WAVEDER',
    'LOCPOT', 'ELFCAR', 'PARCHG', 'DOSCAR', 'EIGENVAL', 'PROCAR', 'XDATCAR',
)
SCAN_MARKERS = frozenset((*INPUT_FILES, *OUTPUT_MARKERS))
COPY_ALLOWLIST = frozenset((*BASIC_COPY_FILES, *LARGE_OUTPUT_FILES))

LIS_SPECIES = ('Li2S8', 'Li2S6', 'Li2S4', 'Li2S2', 'Li2S', 'S8')
_SPECIES_PATTERNS = {
    species: re.compile(rf'(?<![A-Za-z0-9]){re.escape(species)}(?!\d)', re.I)
    for species in LIS_SPECIES
}
_FORMULA_RE = re.compile(r'(?:[A-Z][a-z]?\d*)+$')
_TOTEN_RE = re.compile(
    r'free\s+energy\s+TOTEN\s*=\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)',
    re.I,
)
_ENMAX_RE = re.compile(r'ENMAX\s*=\s*([0-9.]+)', re.I)

_IONIC_MARK = 'reached required accuracy'
_ELECTRONIC_MARK = 'aborting loop because EDIFF is reached'
_CLEAN_EXIT_MARKS = (
    'General timing and accounting informations for this job',
    'General timing and accounting',
    'Total CPU time used',
)
_STATIC_TASKS = frozenset(('static', 'dos', 'band'))
_IMPORT_TASK_TYPES = frozenset(('relax', 'static', 'dos', 'band', 'freq'))

_ROLE_ALIASES = {
    'clean': 'clean', 'clean_slab': 'clean', 'slab': 'clean',
    'config': 'config', 'configuration': 'config', 'ads': 'config',
    'ref': 'ref', 'reference': 'ref', 'gas_ref': 'ref',
    'molecule': 'molecule', 'molecular': 'molecule', 'species_ref': 'molecule',
}
_REFERENCE_SEGMENTS = frozenset(
    ('ref', 'refs', 'reference', 'references', 'mol', 'molecule', 'molecules')
)
_CLEAN_TOKENS = frozenset(('clean', 'bare', 'pristine'))
_CONFIG_TOKENS = frozenset(
    ('ads', 'adsorbate', 'adsorbed', 'config', 'configuration', 'site',
     'top', 'bridge', 'hollow', 'fcc', 'hcp')
)


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def _safe_stem(value: str, fallback: str = 'item') -> str:
    # ``str.isalnum`` includes Unicode letters/numbers, so a Chinese project
    # name remains recognisable instead of every such name collapsing to the
    # same ``project`` directory.  Separators, controls and punctuation are
    # replaced, never interpreted as path components.
    stem = ''.join(
        char if char.isalnum() or char in '._-' else '_'
        for char in str(value).strip()
    )
    stem = re.sub(r'_+', '_', stem).strip('._')
    return stem or fallback


def _normalise_species(value) -> str | None:
    raw = str(value or '').strip()
    for species in LIS_SPECIES:
        if raw.casefold() == species.casefold():
            return species
    if raw and len(raw) <= 64 and _FORMULA_RE.fullmatch(raw):
        return raw
    return None


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _read_text(path: Path) -> str | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        return path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None


def _tail_text(path: Path, limit: int = 131072) -> str:
    """Read only the tail needed by the NELM saturation gate."""
    if not path.is_file() or path.is_symlink():
        return ''
    try:
        with path.open('rb') as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit), os.SEEK_SET)
            return fh.read().decode('utf-8', errors='replace')
    except OSError:
        return ''


def _file_size(path: Path) -> int:
    if not path.is_file() or path.is_symlink():
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _copyable_file(path: Path) -> bool:
    """Only regular, non-symlink files may cross the import boundary."""
    return path.is_file() and not path.is_symlink()


def _species_in_path(path: Path) -> str | None:
    text = '/'.join(path.parts)
    for species in LIS_SPECIES:  # longest Li-polysulfides precede Li2S
        if _SPECIES_PATTERNS[species].search(text):
            return species
    return None


def _tokens(path: Path) -> set[str]:
    return {
        token for part in path.parts
        for token in re.split(r'[^a-z0-9]+', part.lower()) if token
    }


def _has_reference_context(path: Path) -> bool:
    for part in path.parts:
        low = part.lower()
        if low in _REFERENCE_SEGMENTS:
            return True
        if low.startswith(('mol_', 'molecule_', 'ref_')):
            return True
    return False


def _suggest_role(path: Path, species: str | None) -> tuple[str | None, str]:
    """Return a conservative role suggestion and a human-readable reason."""
    if _has_reference_context(path):
        if species:
            return 'molecule', f'路径含明确参考线索且识别到 Li-S 物种 {species}'
        return 'ref', '路径含 refs/molecules/mol_ 等明确参考线索'

    tokens = _tokens(path)
    name_low = path.name.lower()
    if tokens & _CLEAN_TOKENS or 'slab_clean' in name_low or 'clean_slab' in name_low:
        return 'clean', '目录名含 clean/bare/pristine 等清洁表面线索'
    if tokens & _CONFIG_TOKENS or name_low.startswith(('ads_', 'config_')):
        return 'config', '目录名含 ads/config/site 等吸附构型线索'
    if species:
        return None, f'识别到 {species}，但路径无 refs/molecules/mol_ 或 ads/config 明确线索'
    return None, '目录名不足以可靠判断 clean/config/ref/molecule'


def _infer_task(source: Path) -> tuple[str, dict, list[str]]:
    incar_path = source / 'INCAR'
    warnings: list[str] = []
    incar = {}
    text = _read_text(incar_path)
    if text is None:
        warnings.append('缺 INCAR，task_type 无法确定；禁止自动判 DONE，需显式映射或人工确认')
        return 'unknown', incar, warnings
    try:
        incar = parse_incar(text)
        if not incar:
            warnings.append('INCAR 未解析到参数，task_type 未知；禁止自动判 DONE')
            return 'unknown', {}, warnings
        return _infer_task_type(incar), incar, warnings
    except (TypeError, ValueError) as exc:
        warnings.append(f'INCAR 解析失败({exc})，task_type 未知；禁止自动判 DONE')
        return 'unknown', {}, warnings


def _nelm(incar: Mapping) -> int:
    try:
        value = incar.get('NELM', 60)
        parsed = int(float(value))
        return parsed if parsed > 0 else 60
    except (TypeError, ValueError):
        return 60


def _expected_marker(task_type: str) -> str | None:
    if task_type in _STATIC_TASKS:
        return _ELECTRONIC_MARK
    if task_type in ('relax', 'freq'):
        return _IONIC_MARK
    return None


def _outcar_facts(source: Path, task_type: str) -> dict:
    expected = _expected_marker(task_type)
    marker_seen = False
    clean_exit = False
    toten = None
    path = source / 'OUTCAR'
    # The final marker and final TOTEN live near the end of OUTCAR.  Reading a
    # bounded tail avoids repeatedly streaming multi-GB files during
    # scan/preview/commit while preserving the evidence relevant to the final
    # state.  16 MiB comfortably covers unusually verbose final ionic steps.
    for line in _tail_text(path, limit=16 * 1024 * 1024).splitlines():
        marker_seen = marker_seen or bool(expected and expected in line)
        clean_exit = clean_exit or any(mark in line for mark in _CLEAN_EXIT_MARKS)
        match = _TOTEN_RE.search(line)
        if match:
            try:
                toten = float(match.group(1))
            except ValueError:
                pass
    return {
        'expected_marker': expected,
        'marker_seen': marker_seen,
        'clean_exit': clean_exit,
        'toten_eV': toten,
    }


def _vasprun_energies(source: Path) -> dict:
    """Best-effort streaming parse preserving the exact vasprun energy key."""
    path = source / 'vasprun.xml'
    if not path.is_file():
        path = source / 'vasprun'
    if not path.is_file() or path.is_symlink():
        return {'e_0_energy': None, 'e_fr_energy': None}
    energies = {'e_0_energy': None, 'e_fr_energy': None}
    try:
        for _event, elem in ET.iterparse(path, events=('end',)):
            tag = elem.tag.rsplit('}', 1)[-1]
            if tag == 'i' and elem.attrib.get('name') in ('e_0_energy', 'e_fr_energy'):
                try:
                    energies[elem.attrib['name']] = float((elem.text or '').strip())
                except ValueError:
                    pass
            elem.clear()
    except (ET.ParseError, OSError):
        return energies
    return energies


def _method_facts(source: Path) -> tuple[dict, list[str]]:
    incar = _read_text(source / 'INCAR') or ''
    kpoints = _read_text(source / 'KPOINTS')
    potcar = _read_text(source / 'POTCAR')
    poscar = _read_text(source / 'POSCAR')
    try:
        result = methods_text.extract_facts(incar, kpoints, potcar)
    except (TypeError, ValueError) as exc:
        return {}, [f'方法学字段解析失败:{exc}']
    facts = dict(result.get('facts') or {})
    # ``methods_text`` is the canonical TITEL parser.  Augment its records with
    # the ENMAX values present in the imported POTCAR so report provenance is as
    # complete as a newly generated job's ``potcar_provenance``.
    potcars = [dict(item) for item in (facts.get('potcars') or [])]
    enmax_values = _ENMAX_RE.findall(potcar or '')
    for item, value in zip(potcars, enmax_values):
        try:
            item['enmax'] = float(value)
        except ValueError:
            pass
    facts['potcars'] = potcars
    facts['input_evidence'] = {
        'INCAR': bool(incar.strip()),
        'POTCAR': bool(potcar and potcar.strip()),
        'KPOINTS': bool(kpoints and kpoints.strip()),
        'POSCAR': bool(poscar and poscar.strip()),
    }
    facts['xc_fingerprint'] = {
        key: facts.get(key) for key in (
            'functional', 'functional_class', 'base_functional', 'metagga',
            'lhfcalc', 'aexx', 'hfscreen', 'aggax', 'aggac', 'aldac',
            'lasph', 'luse_vdw',
        )
    }
    try:
        structure = parse_structure(poscar or '')
        species, counts = parse_poscar_species(poscar or '')
        facts['lattice'] = [
            [float(component) for component in vector]
            for vector in structure.get('cell') or []
        ]
        facts['composition'] = {
            element: int(count) for element, count in zip(species, counts)
        }
    except (TypeError, ValueError, IndexError, ZeroDivisionError, NotImplementedError):
        facts['lattice'] = None
        facts['composition'] = None
    # Preserve element-keyed +U parameters so projects with different element
    # counts can still compare the overlapping species correctly.
    try:
        inc = parse_incar(incar)
        elements, _counts = parse_poscar_species(poscar or '')
        ldau_parameters = {}
        for key in ('LDAUL', 'LDAUU', 'LDAUJ'):
            raw = str(inc.get(key, '')).split()
            if raw and len(raw) == len(elements):
                for element, value in zip(elements, raw):
                    ldau_parameters.setdefault(element, {})[key] = value
        facts['ldau_parameters'] = ldau_parameters
    except (TypeError, ValueError, IndexError):
        facts['ldau_parameters'] = {}
    return facts, list(result.get('warnings') or [])


def validate_vasp_quartet(source: str | os.PathLike) -> list[str]:
    """Public, side-effect-free validator shared by every VASP folder importer."""
    path = Path(source)
    present = [name for name in INPUT_FILES if _copyable_file(path / name)]
    return _validate_inputs(path, present)


def _source_fingerprint(source: Path, inspected: dict) -> str:
    """Bounded fingerprint used to bind commit to the preview the user approved."""
    record = {
        'source': str(source.resolve()),
        'force_created': bool(inspected.get('force_created')),
        'task_type': inspected.get('task_type'),
        'state': inspected.get('state_suggestion'),
        'energy_e0_eV': inspected.get('energy_e0_eV'),
        'raw_energy_e0_eV': inspected.get('raw_energy_e0_eV'),
        'files': {},
    }
    present = inspected.get('files', {}).get('present') or []
    if inspected.get('force_created'):
        # Recalculation mode explicitly discards prior outputs.  Bind approval
        # to the quartet that will be copied, not to irrelevant stale results
        # that may be archived/rotated while the user confirms the preview.
        present = [name for name in present if name in INPUT_FILES]
    for name in present:
        path = source / name
        try:
            st = path.stat()
        except OSError:
            record['files'][name] = {'missing': True}
            continue
        item = {'size': st.st_size, 'mtime_ns': st.st_mtime_ns}
        # Inputs and OSZICAR are bounded enough to hash and directly determine
        # the scientific result.  Huge OUTCAR/vasprun files use metadata plus
        # the parsed state/energy above, avoiding a second GB-scale pass.
        if name in INPUT_FILES or name == 'OSZICAR':
            try:
                item['sha256'] = manifest_mod.sha256_file(path)
            except OSError:
                item['sha256'] = None
        record['files'][name] = item
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _potcar_identity(facts: dict) -> dict:
    return {
        str(item.get('element')): (item.get('variant'), item.get('titel'))
        for item in facts.get('potcars') or [] if item.get('element')
    }


def _valid_lattice(value) -> bool:
    return bool(
        isinstance(value, list) and len(value) == 3
        and all(isinstance(row, list) and len(row) == 3 for row in value)
        and all(
            isinstance(component, (int, float)) and math.isfinite(float(component))
            for row in value for component in row
        )
    )


def _lattice_close(left, right) -> tuple[bool, float | None]:
    if not _valid_lattice(left) or not _valid_lattice(right):
        return False, None
    deltas = [
        abs(float(a) - float(b))
        for row_a, row_b in zip(left, right)
        for a, b in zip(row_a, row_b)
    ]
    matches = all(
        math.isclose(float(a), float(b), rel_tol=1e-5, abs_tol=1e-5)
        for row_a, row_b in zip(left, right)
        for a, b in zip(row_a, row_b)
    )
    return matches, max(deltas, default=0.0)


def _project_consistency(members: list[dict], kind: str) -> tuple[list[str], list[str]]:
    """Check energy-comparability across imported members.

    Adsorption energies must not combine different functionals, cutoffs,
    dispersion/+U settings, overlapping PAW datasets, or slab/config meshes.
    Missing facts are never silently treated as equal: critical clean/config
    evidence blocks an adsorption project, while non-critical/reference gaps are
    surfaced as warnings.
    """
    if not members:
        return [], []
    baseline = next((item for item in members if item['role'] == 'clean'), members[0])
    base_facts = baseline.get('methods') or {}
    errors, warnings = [], []
    common_keys = (
        ('functional', '交换关联泛函'), ('encut', 'ENCUT'),
        ('ivdw_setting', '色散校正 IVDW'), ('ldau', 'DFT+U 开关'),
        ('metagga', 'METAGGA'), ('lhfcalc', 'LHFCALC'),
        ('aexx', 'AEXX'), ('hfscreen', 'HFSCREEN'),
        ('aggax', 'AGGAX'), ('aggac', 'AGGAC'), ('aldac', 'ALDAC'),
        ('lasph', 'LASPH'), ('luse_vdw', 'LUSE_VDW'),
    )

    def label(member):
        return member.get('relative_path') or member.get('name') or '?'

    # A ΔE-ready slab/config pair needs enough evidence to prove that the
    # calculations are comparable.  Treating two missing values as "equal"
    # would be scientifically unsafe.  A molecule-library import remains useful
    # as a quarantine/staging area, so the same gaps are warnings there.
    for member in members:
        if kind != 'adsorption' or member.get('role') not in ('clean', 'config'):
            continue
        facts = member.get('methods') or {}
        evidence = facts.get('input_evidence') or {}
        missing_files = [name for name in INPUT_FILES if not evidence.get(name)]
        critical_missing = []
        if missing_files:
            critical_missing.append('四件套证据:' + '/'.join(missing_files))
        if facts.get('encut') is None:
            critical_missing.append('ENCUT')
        if not facts.get('functional') or not facts.get('functional_known'):
            critical_missing.append('可识别的交换关联泛函')
        if not facts.get('kpoints'):
            critical_missing.append('KPOINTS/k 网格')
        if not _valid_lattice(facts.get('lattice')):
            critical_missing.append('POSCAR 晶胞')
        if not facts.get('potcars'):
            critical_missing.append('POTCAR 身份')
        if not member.get('input_complete'):
            critical_missing.append('通过语法/元素身份门控的四件套')
        if critical_missing:
            errors.append(
                f'科学一致性无法核验:{label(member)} 缺少 ' + '、'.join(critical_missing))

    for member in members:
        if member is baseline:
            continue
        facts = member.get('methods') or {}
        for key, title in common_keys:
            left, right = base_facts.get(key), facts.get(key)
            if left is not None and right is not None and left != right:
                errors.append(
                    f'方法不一致:{label(baseline)} 与 {label(member)} 的 {title} '
                    f'分别为 {left!r} / {right!r}')
            elif (left is None) != (right is None):
                warnings.append(
                    f'无法完整核验 {title}:{label(baseline)} / {label(member)} 有一方缺值')

        # K mesh, spin and smearing must match the clean slab for slab+ads
        # members.  Gas/molecular references can legitimately use Γ-only boxes
        # and species-specific spin states.
        if kind == 'adsorption' and member['role'] == 'config':
            for key, title in (('ispin', 'ISPIN'), ('smearing', 'ISMEAR'),
                               ('sigma', 'SIGMA')):
                left, right = base_facts.get(key), facts.get(key)
                if left is not None and right is not None and left != right:
                    errors.append(
                        f'方法不一致:{label(baseline)} 与 {label(member)} 的 {title} '
                        f'分别为 {left!r} / {right!r}')
            left_k, right_k = base_facts.get('kpoints'), facts.get('kpoints')
            if left_k and right_k and left_k != right_k:
                errors.append(
                    f'方法不一致:{label(baseline)} 与 {label(member)} 的 KPOINTS 不同')
            elif not left_k or not right_k:
                # Usually already a critical-evidence error, but keeping the
                # pairwise message makes the remediation obvious in the UI.
                warnings.append(
                    f'无法完整核验 KPOINTS:{label(baseline)} / {label(member)} 缺值')

            same_cell, max_delta = _lattice_close(
                base_facts.get('lattice'), facts.get('lattice'))
            if max_delta is not None and not same_cell:
                errors.append(
                    f'晶胞不一致:{label(baseline)} 与 {label(member)} 的 POSCAR '
                    f'lattice 不同(最大分量差 {max_delta:.6g} Å)；'
                    '不可将不同超胞直接用于同一吸附能 ΔE')

            base_composition = base_facts.get('composition') or {}
            current_composition = facts.get('composition') or {}
            if base_composition and current_composition:
                deficits = {
                    element: count - int(current_composition.get(element, 0))
                    for element, count in base_composition.items()
                    if int(current_composition.get(element, 0)) < count
                }
                if deficits:
                    detail = ', '.join(
                        f'{element} 少 {count}' for element, count in deficits.items())
                    errors.append(
                        f'基底组成不相容:{label(member)} 相对 {label(baseline)} {detail}；'
                        'config 可增加吸附物原子，但不能缺少 clean 的基底原子')

    # Check PAW/+U identities pairwise by element, not only against clean.  An
    # adsorbate element absent from the bare slab may still occur in both a
    # config and its molecular reference and must use the same dataset/U value.
    potcar_seen: dict[str, tuple[tuple, str]] = {}
    u_seen: dict[str, tuple[dict, str]] = {}
    for member in members:
        facts = member.get('methods') or {}
        for element, identity in _potcar_identity(facts).items():
            if element in potcar_seen and potcar_seen[element][0] != identity:
                errors.append(
                    f'方法不一致:{label(member)} 的 {element} POTCAR 与 '
                    f'{potcar_seen[element][1]} 不同')
            else:
                potcar_seen[element] = (identity, label(member))
        for element, parameters in (facts.get('ldau_parameters') or {}).items():
            if element in u_seen and u_seen[element][0] != parameters:
                errors.append(
                    f'方法不一致:{label(member)} 的 {element} DFT+U 参数与 '
                    f'{u_seen[element][1]} 不同')
            else:
                u_seen[element] = (parameters, label(member))
    return list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))


def _validate_kpoints(text: str) -> str | None:
    lines = [line.strip() for line in text.splitlines()]
    if len(lines) < 4:
        return 'KPOINTS 行数不足'
    try:
        count = int(lines[1].split()[0])
    except (IndexError, ValueError):
        return 'KPOINTS 第 2 行不是合法点数'
    mode = lines[2].lower()
    if mode.startswith('l'):
        if count <= 0 or len([line for line in lines[3:] if line]) < 3:
            return 'Line-mode KPOINTS 缺分段数、坐标系或端点'
    elif count == 0:
        if not (mode.startswith('g') or mode.startswith('m')):
            return '自动 KPOINTS 第 3 行须为 Gamma 或 Monkhorst-Pack'
        try:
            grid = [int(value) for value in lines[3].split()[:3]]
        except ValueError:
            return '自动 KPOINTS 网格不是整数'
        if len(grid) != 3 or any(value <= 0 for value in grid):
            return '自动 KPOINTS 网格须为 3 个正整数'
        if len(lines) > 4 and lines[4]:
            try:
                shift = [float(value) for value in lines[4].split()]
            except ValueError:
                return '自动 KPOINTS 位移不是数值'
            if len(shift) != 3 or any(not math.isfinite(value) for value in shift):
                return '自动 KPOINTS 位移须为 3 个有限数值'
    elif count > 0 and len(lines) < count + 3:
        return f'显式 KPOINTS 声明 {count} 个点但坐标行不足'
    elif count < 0:
        return 'KPOINTS 点数不能为负数'
    return None


def _validate_inputs(source: Path, present: list[str]) -> list[str]:
    """Minimum syntax/identity gate before an imported quartet is submittable."""
    issues = [f'缺 {name}' for name in INPUT_FILES if name not in present]
    if issues:
        return issues
    texts = {name: _read_text(source / name) for name in INPUT_FILES}
    for name, text in texts.items():
        if not text or not text.strip():
            issues.append(f'{name} 为空或不可读')
    if issues:
        return issues

    try:
        structure = parse_structure(texts['POSCAR'])
        species, counts = parse_poscar_species(texts['POSCAR'])
        if not species or not counts or len(species) != len(counts):
            issues.append('POSCAR 缺合法物种/计数行')
        elif any(count <= 0 for count in counts):
            issues.append('POSCAR 原子计数须为正整数')
        elif len(structure.get('elements') or []) != sum(counts):
            issues.append('POSCAR 坐标数与物种计数不一致')
    except (ValueError, NotImplementedError, IndexError, ZeroDivisionError, TypeError) as exc:
        species = []
        issues.append(f'POSCAR 解析失败:{exc}')

    try:
        if not parse_incar(texts['INCAR']):
            issues.append('INCAR 未解析到任何 KEY=VALUE 参数')
    except (TypeError, ValueError) as exc:
        issues.append(f'INCAR 解析失败:{exc}')

    kpoints_issue = _validate_kpoints(texts['KPOINTS'])
    if kpoints_issue:
        issues.append(kpoints_issue)

    potcars = methods_text.parse_potcar_titels(texts['POTCAR'])
    potcar_elements = [item.get('element') for item in potcars]
    if len(potcars) != len(species):
        issues.append(
            f'POTCAR TITEL 段数 {len(potcars)} 与 POSCAR 物种数 {len(species)} 不一致')
    elif potcar_elements != species:
        issues.append(
            'POTCAR 元素顺序与 POSCAR 不一致:'
            f'{" ".join(str(item) for item in potcar_elements)} != {" ".join(species)}')
    return issues


def _state_preview(source: Path, *, manual_confirm: bool = False,
                   task_type_override: str | None = None,
                   force_created: bool = False) -> dict:
    """Classify one directory without allowing untrusted energy into canonical results."""
    present = [name for name in COPY_ALLOWLIST if _copyable_file(source / name)]
    present.sort(key=lambda name: (name not in INPUT_FILES, name.lower()))
    missing_inputs = [name for name in INPUT_FILES if name not in present]
    input_issues = _validate_inputs(source, present)
    input_complete = not input_issues
    source_has_output = any(
        _copyable_file(source / name) for name in RESULT_EVIDENCE_FILES)
    # Explicit recalculation imports only the validated quartet and deliberately
    # ignores stale result evidence.  Invalid quartets cannot use this escape.
    ignore_old_results = bool(force_created and input_complete)
    has_output = bool(source_has_output and not ignore_old_results)

    task_type, incar, warnings = _infer_task(source)
    if task_type_override:
        task_type = task_type_override
        warnings.append(f'task_type 由用户显式指定为 {task_type}')
    outcar = (_outcar_facts(source, task_type) if has_output else {
        'expected_marker': _expected_marker(task_type), 'marker_seen': False,
        'clean_exit': False, 'toten_eV': None,
    })
    energy = (freeenergy.read_e0(source)
              if has_output and _copyable_file(source / 'OSZICAR') else None)
    energy_source = 'OSZICAR:E0' if energy is not None else None
    # vasprun.xml is commonly hundreds of MB or more.  OSZICAR E0 is already
    # canonical, so only parse XML when that cheaper source is absent.
    vasprun = (_vasprun_energies(source) if has_output and energy is None else
               {'e_0_energy': None, 'e_fr_energy': None})
    if energy is None and vasprun['e_0_energy'] is not None:
        energy = vasprun['e_0_energy']
        energy_source = 'vasprun:e_0_energy'

    # TOTEN/free energy is useful evidence, but it is not the E0 quantity used by
    # adsorption/free-energy reports.  Never silently relabel it as energy_e0_eV.
    observed_non_e0 = None
    observed_source = None
    if energy is None and outcar['toten_eV'] is not None:
        observed_non_e0 = outcar['toten_eV']
        observed_source = 'OUTCAR:TOTEN'
    elif energy is None and vasprun['e_fr_energy'] is not None:
        observed_non_e0 = vasprun['e_fr_energy']
        observed_source = 'vasprun:e_fr_energy'

    finite_energy = bool(energy is not None and math.isfinite(float(energy)))
    sane_energy = bool(finite_energy and not diagnose.energy_implausible(energy))
    confirmation_requested = bool(manual_confirm and has_output and sane_energy)
    effective_converged = bool(outcar['marker_seen'] or confirmation_requested)
    oszicar_tail = _tail_text(source / 'OSZICAR')
    diagnosis = diagnose.classify(
        outcar_size=_file_size(source / 'OUTCAR'),
        oszicar_size=_file_size(source / 'OSZICAR'),
        converged=effective_converged,
        energy=energy,
        oszicar_tail=oszicar_tail,
        nelm=_nelm(incar),
        clean_exit=outcar['clean_exit'],
    )

    if input_complete and not has_output:
        state = 'CREATED'
        if ignore_old_results:
            evidence = '用户选择重新计算；仅采用四件套，旧输出不会复制或进入分析'
            warnings.append('已选择作为新任务重新计算；忽略源目录中的旧计算输出')
        else:
            evidence = '四件套齐全且尚无计算输出，可进入提交队列'
    elif has_output and diagnosis.state == 'DONE' and sane_energy:
        state = 'DONE'
        evidence = diagnosis.evidence
        if confirmation_requested and not outcar['marker_seen']:
            evidence = '用户显式确认该结果；能量已通过物理合理性与 NELM 门控'
            warnings.append('未见对应 task_type 的收敛标记；已使用 manual_confirm 人工确认')
    else:
        state = 'NEEDS_HUMAN'
        evidence = diagnosis.evidence
        if not has_output and not input_complete:
            evidence = '既无完整四件套，也无可核验的计算输出'
        elif has_output and not outcar['marker_seen'] and not confirmation_requested:
            warnings.append(
                f'未见 {task_type} 对应收敛标记 {outcar["expected_marker"]!r}；需人工确认')
        if energy is not None and not sane_energy:
            warnings.append(f'E0={energy} eV 未通过物理合理性门控，不会写入 energy_e0_eV')
        if energy is None and observed_non_e0 is not None:
            warnings.append(
                f'仅解析到 {observed_source}={observed_non_e0} eV；该量不是 E0，'
                '不会写入 energy_e0_eV')

    facts, method_warnings = _method_facts(source)
    warnings.extend(f'输入门控:{issue}' for issue in input_issues)
    warnings.extend(method_warnings)
    manual_confirmed = bool(confirmation_requested and state == 'DONE')
    if manual_confirm and not confirmation_requested:
        warnings.append('manual_confirm 未生效：仍缺计算输出或合理 E0')
    elif confirmation_requested and not manual_confirmed:
        warnings.append('manual_confirm 未越过 NELM/物理一致性科学门控，结果仍需人工处理')

    trusted_energy = float(energy) if state == 'DONE' and sane_energy else None
    # NaN/inf are not JSON-safe scientific evidence; record their rejection in
    # warnings, but never expose them as a numeric result.
    raw_energy = float(energy) if finite_energy and trusted_energy is None else None
    return {
        'files': {
            'present': present,
            'missing_inputs': missing_inputs,
            'input_issues': input_issues,
            'skipped_large': [name for name in LARGE_OUTPUT_FILES if name in present],
        },
        'input_complete': input_complete,
        'has_output': has_output,
        'source_has_output': source_has_output,
        'force_created': ignore_old_results,
        'task_type': task_type,
        'energy_e0_eV': trusted_energy,
        'raw_energy_e0_eV': raw_energy,
        'observed_energy_eV': (
            float(observed_non_e0) if observed_non_e0 is not None
            and math.isfinite(float(observed_non_e0)) else None),
        'observed_energy_source': observed_source,
        'energy_source': energy_source,
        'converged': bool(outcar['marker_seen']),
        'manual_confirmed': manual_confirmed,
        'manual_confirmation_requested': bool(manual_confirm),
        'expected_convergence_marker': outcar['expected_marker'],
        'state_suggestion': state,
        'diagnosis': {
            'failure_class': diagnosis.failure_class,
            'restartable': diagnosis.restartable,
            'evidence': evidence,
        },
        'methods': facts,
        'warnings': warnings,
    }


def _candidate(source: Path, root: Path, *, manual_confirm: bool = False,
               task_type_override: str | None = None,
               force_created: bool = False) -> dict:
    relative = source.relative_to(root).as_posix() or '.'
    # Parent/root labels such as ``clean_project`` or ``Li2S4_project`` must not
    # bias every child.  The only inherited root context is an explicit reference
    # container (``refs``/``molecules``); otherwise children use their relative
    # path alone.  A candidate that is the root uses its own directory name.
    if relative == '.':
        hint_path = Path(root.name)
    elif _has_reference_context(Path(root.name)):
        hint_path = Path(root.name) / relative
    else:
        hint_path = Path(relative)
    species = _species_in_path(hint_path)
    role, reason = _suggest_role(hint_path, species)
    out = {
        'id': relative,
        'relative_path': relative,
        'path': str(source),
        'name': source.name,
        'role_suggestion': role,
        'suggested_role': role,
        'role_reason': reason,
        'species_suggestion': species,
        'ambiguous_role': role is None,
    }
    out.update(_state_preview(
        source, manual_confirm=manual_confirm, task_type_override=task_type_override,
        force_created=force_created))
    return out


def _scan_dirs(root: Path) -> list[Path]:
    found: list[Path] = []
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        # Never traverse directory symlinks: importing a selected tree must not
        # unexpectedly escape through a link.
        dirnames[:] = [d for d in dirnames if not (Path(current) / d).is_symlink()]
        if any(name in SCAN_MARKERS and _copyable_file(Path(current) / name)
               for name in filenames):
            found.append(Path(current).resolve())

    def strong_calculation(path: Path) -> bool:
        # A real calculation may intentionally sit at the selected root (for
        # example the clean slab) while configs live below it.  Preserve it when
        # it has a full quartet or result evidence.  A weak POSCAR-only grouping
        # container is still hidden when a deeper calculation exists.
        return (
            all(_copyable_file(path / name) for name in INPUT_FILES)
            or any(_copyable_file(path / name) for name in RESULT_EVIDENCE_FILES)
        )

    leaves = [
        candidate for candidate in found
        if (strong_calculation(candidate)
            or not any(
                other != candidate and _is_relative_to(other, candidate)
                for other in found))
    ]
    return sorted(set(leaves), key=lambda p: p.relative_to(root).as_posix().casefold())


def scan_root(root: str | os.PathLike) -> dict:
    """Recursively discover VASP leaf directories and conservative role hints."""
    if root is None or (isinstance(root, str) and not root.strip()):
        return {
            'ok': False, 'root': '', 'preview': [], 'candidates': [],
            'role_suggestions': {}, 'warnings': [], 'error': '扫描根目录不能为空',
        }
    try:
        source_root = Path(root).expanduser().resolve()
    except (OSError, TypeError, ValueError) as exc:
        return {
            'ok': False, 'root': str(root), 'preview': [], 'candidates': [],
            'role_suggestions': {}, 'warnings': [], 'error': f'扫描根目录非法:{exc}',
        }
    if not source_root.is_dir():
        return {
            'ok': False, 'root': str(source_root), 'preview': [], 'candidates': [],
            'role_suggestions': {}, 'warnings': [],
            'error': f'扫描根目录不存在或不是目录:{source_root}',
        }

    candidates = [_candidate(path, source_root) for path in _scan_dirs(source_root)]
    roles = {
        'clean': [], 'configs': [], 'refs': [], 'molecules': {}, 'ambiguous': [],
    }
    for item in candidates:
        role = item['role_suggestion']
        rel = item['relative_path']
        if role == 'clean':
            roles['clean'].append(rel)
        elif role == 'config':
            roles['configs'].append(rel)
        elif role == 'ref':
            roles['refs'].append(rel)
        elif role == 'molecule':
            species = item.get('species_suggestion') or 'unknown'
            roles['molecules'].setdefault(species, []).append(rel)
        else:
            roles['ambiguous'].append(rel)

    # Singular aliases match the canonical assignment role vocabulary while the
    # plural keys remain convenient for UI rendering.
    roles['config'] = roles['configs']
    roles['ref'] = roles['refs']
    roles['molecule'] = roles['molecules']

    warnings: list[str] = []
    if not candidates:
        warnings.append('未发现含 VASP 输入或输出标志文件的叶目录')
    if roles['ambiguous']:
        warnings.append(f'{len(roles["ambiguous"])} 个目录角色不明确，导入前需显式映射')
    return {
        'ok': True, 'root': str(source_root),
        # ``preview`` is the public UI term; ``candidates`` is kept as a convenient
        # programmatic alias without introducing a second scan representation.
        'preview': candidates, 'candidates': candidates,
        'role_suggestions': roles, 'warnings': warnings,
    }


def _normalise_role(value) -> str | None:
    return _ROLE_ALIASES.get(str(value or '').strip().lower())


def _normalise_assignments(assignments, scan: dict) -> tuple[list[dict], list[str]]:
    by_path = {Path(item['path']).resolve(): item for item in scan.get('preview') or []}
    source_root = Path(scan['root'])
    errors: list[str] = []

    if assignments is None:
        raw_items = [
            {'path': item['path'], 'role': item['role_suggestion']}
            for item in scan.get('preview') or [] if item.get('role_suggestion')
        ]
    elif isinstance(assignments, Mapping):
        raw_items = []
        for path, spec in assignments.items():
            if isinstance(spec, Mapping):
                item = dict(spec)
                item.setdefault('path', path)
            else:
                item = {'path': path, 'role': spec}
            raw_items.append(item)
    elif isinstance(assignments, Iterable) and not isinstance(assignments, (str, bytes)):
        raw_items = list(assignments)
    else:
        return [], ['assignments 必须是列表或 {path: role/spec} 映射']

    normalised: list[dict] = []
    seen: set[Path] = set()
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, Mapping):
            errors.append(f'assignments[{index}] 不是对象')
            continue
        path_value = (raw.get('path') or raw.get('source')
                      or raw.get('relative_path') or raw.get('id'))
        if path_value is None:
            errors.append(f'assignments[{index}] 缺 path/source')
            continue
        path = Path(str(path_value)).expanduser()
        if not path.is_absolute():
            path = source_root / path
        path = path.resolve()
        candidate = by_path.get(path)
        if candidate is None:
            errors.append(f'所选目录不在 scan_root 的 VASP 叶目录中:{path}')
            continue
        if path in seen:
            errors.append(f'目录被重复映射:{path}')
            continue
        seen.add(path)
        role = _normalise_role(raw.get('role'))
        if role is None:
            errors.append(f'目录 {candidate["relative_path"]} 的 role 非法，需 clean/config/ref/molecule')
            continue
        item = dict(raw)
        item.update({'path': str(path), 'role': role, '_candidate': candidate})
        normalised.append(item)
    return normalised, errors


def _global_confirmation(manual_confirm, path: Path, relative: str) -> bool:
    if isinstance(manual_confirm, bool):
        return manual_confirm
    if isinstance(manual_confirm, Mapping):
        for raw_key, value in manual_confirm.items():
            if str(raw_key) in (str(path), relative):
                return value is True
        return False
    if isinstance(manual_confirm, Iterable) and not isinstance(manual_confirm, (str, bytes)):
        wanted = {str(v) for v in manual_confirm}
        return str(path) in wanted or relative in wanted
    return False


def _manual_confirmation_errors(value) -> list[str]:
    if isinstance(value, bool):
        return []
    if isinstance(value, Mapping):
        return ([] if all(isinstance(item, bool) for item in value.values()) else
                ['manual_confirm 映射的值必须是 JSON boolean'])
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        return ([] if all(isinstance(item, (str, os.PathLike)) for item in value) else
                ['manual_confirm 列表只能包含绝对路径或 scan relative id'])
    return ['manual_confirm 必须是 JSON boolean、路径映射或 relative id 列表']


def _target_error(source_root: Path, destination: Path) -> str | None:
    if destination.exists() or destination.is_symlink():
        return f'目标已存在，拒绝覆盖:{destination}'
    if _is_relative_to(destination, source_root) or _is_relative_to(source_root, destination):
        return f'托管目标与源目录相同或互相嵌套，拒绝导入:{destination}'
    parent = destination.parent
    if parent.exists() and not parent.is_dir():
        return f'输出根不是目录:{parent}'
    return None


def _remove_tree(path: Path) -> None:
    """Remove only this importer's explicit staging/final tree, including read-only files."""
    def _make_writable_and_retry(func, failed_path, _exc_info):
        os.chmod(failed_path, os.stat(failed_path).st_mode | stat.S_IWRITE)
        func(failed_path)

    if path.exists() or path.is_symlink():
        shutil.rmtree(path, onerror=_make_writable_and_retry)


def _unregister_project(project_path: Path) -> bool:
    """Rollback helper (adsorption currently has no public unregister function)."""
    target = Path(adsorption.default_registry_path())
    entry = str(project_path.resolve())
    items = adsorption.list_projects(path=target)
    if entry not in items:
        return False
    items = [item for item in items if item != entry]
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix('.json.tmp')
    with tmp.open('w', encoding='utf-8') as handle:
        json.dump({'projects': items}, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, target)
    return True


def _destination_names(name: str, assignments: list[dict]) -> list[str]:
    used: set[str] = set()
    result: list[str] = []

    def unique(base: str, terminal: str = '') -> str:
        candidate = base + terminal
        n = 2
        while candidate.casefold() in used:
            candidate = f'{base}_{n}{terminal}'
            n += 1
        used.add(candidate.casefold())
        return candidate

    project_stem = _safe_stem(name, 'project')
    for assignment in assignments:
        role = assignment['role']
        source = Path(assignment['path'])
        species = assignment.get('species') or assignment['_candidate'].get('species_suggestion')
        custom = assignment.get('name')
        if role == 'clean':
            base = project_stem + '_slab_clean'
        elif role == 'ref':
            base = project_stem + '_ref'
        elif role == 'molecule':
            base = 'mol_' + _safe_stem(species or source.name, 'unknown')
        else:
            source_stem = _safe_stem(custom or source.name, 'config')
            terminal = ''
            if species:
                terminal = '_' + species
                if source_stem == species:
                    source_stem = ''
                elif source_stem.endswith(terminal):
                    source_stem = source_stem[:-len(terminal)].rstrip('_')
            base = project_stem + '_ads' + (('_' + source_stem) if source_stem else '')
            result.append(unique(base, terminal))
            continue
        result.append(unique(base))
    return result


def preview_project(root: str | os.PathLike, name: str,
                    output_root: str | os.PathLike, assignments=None, *,
                    include_large_outputs: bool = False,
                    manual_confirm=False,
                    project_kind: str = 'adsorption') -> dict:
    """Build a zero-write import plan from explicit role assignments."""
    scan = scan_root(root)
    source_root = Path(scan['root'])
    raw_name = '' if name is None else str(name).strip()
    safe_name = _safe_stem(raw_name, 'project')
    missing_output = output_root is None or (
        isinstance(output_root, str) and not output_root.strip())
    output_error = None
    try:
        output_parent = (Path.cwd().resolve() if missing_output else
                         Path(output_root).expanduser().resolve())
    except (OSError, TypeError, ValueError) as exc:
        output_parent = Path.cwd().resolve()
        output_error = f'输出根目录非法:{exc}'
    destination = output_parent / safe_name
    include_large = (include_large_outputs
                     if isinstance(include_large_outputs, bool) else False)
    kind = str(project_kind or 'adsorption').strip().lower()
    result = {
        'ok': False, 'source_root': str(source_root), 'name': raw_name,
        'project_kind': kind,
        'directory_name': safe_name, 'destination': str(destination),
        'members': [], 'unassigned': [], 'errors': [], 'warnings': [],
        'fingerprints': {},
        'counts': {'created': 0, 'done': 0, 'needs_human': 0},
        'include_large_outputs': include_large,
    }
    if not scan.get('ok'):
        result['errors'].append(scan.get('error') or '扫描失败')
        return result
    if not raw_name:
        result['errors'].append('项目名不能为空')
    if missing_output:
        result['errors'].append('输出根目录不能为空')
    if output_error:
        result['errors'].append(output_error)
    if not isinstance(include_large_outputs, bool):
        result['errors'].append('include_large_outputs 必须是 JSON boolean')
    if kind not in ('adsorption', 'molecule_library'):
        result['errors'].append('project_kind 需为 adsorption 或 molecule_library')
    result['errors'].extend(_manual_confirmation_errors(manual_confirm))

    target_error = _target_error(source_root, destination)
    if target_error:
        result['errors'].append(target_error)

    selected, assignment_errors = _normalise_assignments(assignments, scan)
    result['errors'].extend(assignment_errors)
    selected_paths = {Path(item['path']).resolve() for item in selected}
    result['unassigned'] = [
        item['relative_path'] for item in scan.get('preview') or []
        if Path(item['path']).resolve() not in selected_paths
    ]
    if result['unassigned']:
        result['warnings'].append(f'{len(result["unassigned"])} 个扫描目录未映射，将不会导入')

    roles = [item['role'] for item in selected]
    if kind == 'molecule_library':
        if not roles or any(role != 'molecule' for role in roles):
            result['errors'].append('分子参考库至少需要 1 个 molecule，且不能混入其他角色')
    else:
        if roles.count('clean') != 1:
            result['errors'].append('吸附项目必须且只能映射 1 个 clean 目录')
        if not any(role == 'config' for role in roles):
            result['errors'].append('吸附项目至少需要映射 1 个 config 目录')
        if roles.count('ref') > 1:
            result['errors'].append('gas ref 最多只能映射 1 个 ref 目录')
    has_species_refs = any(role == 'molecule' for role in roles)

    species_seen: dict[str, str] = {}
    for assignment in selected:
        if assignment.get('species') is None:
            continue
        species = _normalise_species(assignment.get('species'))
        if species is None:
            result['errors'].append(
                f'{assignment["_candidate"]["relative_path"]} 的 species 不是合法化学式')
            assignment['species'] = None
        else:
            assignment['species'] = species
    destination_names = _destination_names(raw_name, selected)
    for assignment, target_name in zip(selected, destination_names):
        source = Path(assignment['path'])
        relative = assignment['_candidate']['relative_path']
        if ('manual_confirm' in assignment
                and not isinstance(assignment['manual_confirm'], bool)):
            result['errors'].append(
                f'{relative} 的 manual_confirm 必须是 JSON boolean')
            explicit_confirmation = False
        else:
            explicit_confirmation = (
                assignment['manual_confirm'] if 'manual_confirm' in assignment
                else _global_confirmation(manual_confirm, source, relative)
            )
        task_override = assignment.get('task_type')
        if task_override is not None:
            if (not isinstance(task_override, str)
                    or task_override.strip().lower() not in _IMPORT_TASK_TYPES):
                result['errors'].append(
                    f'{relative} 的 task_type 非法，需为 '
                    + '/'.join(sorted(_IMPORT_TASK_TYPES)))
                task_override = None
            else:
                task_override = task_override.strip().lower()
        force_created = assignment.get('force_created', False)
        if not isinstance(force_created, bool):
            result['errors'].append(
                f'{relative} 的 force_created 必须是 JSON boolean')
            force_created = False
        inspected = _candidate(
            source, source_root, manual_confirm=explicit_confirmation,
            task_type_override=task_override, force_created=force_created)
        role = assignment['role']
        species = assignment.get('species') or inspected.get('species_suggestion')
        if role in ('molecule', 'config') and species:
            species = _normalise_species(species)
            if species is None:
                result['errors'].append(f'{relative} 的 species 非法')
                species = ''
        if role == 'config' and has_species_refs and not species:
            result['errors'].append(
                f'吸附构型 {relative} 需填写物种，才能匹配已导入的物种参考态')
        if role == 'molecule':
            if not species:
                result['errors'].append(
                    f'分子参考 {relative} 无法识别物种，需显式填写 species')
            else:
                if species in species_seen:
                    result['errors'].append(
                        f'物种 {species} 重复映射:{species_seen[species]} 与 {relative}')
                species_seen[species] = relative

        if role == 'molecule':
            # All managed references stay at a stable direct path.  The loader
            # gates them on job.yaml state, so NEEDS_HUMAN remains unusable but
            # becomes available automatically if a later repair reaches DONE.
            member_destination = destination / 'molecules' / target_name
        else:
            member_destination = destination / target_name
        member_destination = member_destination.resolve()
        if not _is_relative_to(member_destination, destination.resolve()):
            result['errors'].append(f'成员目标越出项目目录，拒绝导入:{relative}')

        present = inspected['files']['present']
        large_present = [file for file in LARGE_OUTPUT_FILES if file in present]
        copy_files = ([file for file in INPUT_FILES if file in present]
                      if force_created else
                      [file for file in BASIC_COPY_FILES if file in present])
        if include_large and not force_created:
            copy_files.extend(file for file in LARGE_OUTPUT_FILES if file in present)
            skipped_large: list[str] = []
        else:
            skipped_large = large_present
        try:
            source_entries = list(source.iterdir())
        except OSError as exc:
            source_entries = []
            result['errors'].append(f'无法读取源目录 {relative}:{exc}')
        ignored = sorted(
            file.name for file in source_entries
            if file.is_file() and file.name not in COPY_ALLOWLIST
        )
        member_warnings = list(inspected['warnings'])
        if force_created:
            skipped_results = [name for name in RESULT_EVIDENCE_FILES if name in present]
            if skipped_results:
                member_warnings.append(
                    '重新计算模式只复制四件套，已忽略旧结果:' + ', '.join(skipped_results))
        if skipped_large:
            member_warnings.append(
                '默认跳过大文件:' + ', '.join(skipped_large)
                + '；如确需归档请启用 include_large_outputs')
        if (inspected['state_suggestion'] == 'DONE'
                and inspected.get('energy_source') == 'vasprun:e_0_energy'
                and any(name in skipped_large for name in ('vasprun.xml', 'vasprun'))):
            result['errors'].append(
                f'{relative} 的可信 E0 仅存在于 vasprun；要导入为 DONE，'
                '必须启用 include_large_outputs 保留能量证据')
        if role == 'molecule' and inspected['state_suggestion'] == 'NEEDS_HUMAN':
            member_warnings.append(
                '分子参考未通过 DONE 门控；在 job.yaml 变为 DONE 前不会被自由能采用')
        symlinks = sorted(
            file.name for file in source_entries
            if file.is_symlink() and file.name in COPY_ALLOWLIST
        )
        if symlinks:
            member_warnings.append('安全起见跳过符号链接:' + ', '.join(symlinks))

        approved_hashes: dict[str, str] = {}
        for file in copy_files:
            if file not in (*INPUT_FILES, 'OSZICAR'):
                continue
            try:
                approved_hashes[file] = manifest_mod.sha256_file(source / file)
            except OSError as exc:
                result['errors'].append(f'无法锁定源文件快照 {relative}/{file}:{exc}')

        member = {
            'source': str(source), 'relative_path': relative,
            'role': role, 'species': species,
            'force_created': force_created,
            'name': target_name, 'destination': str(member_destination),
            'state': inspected['state_suggestion'],
            'task_type': inspected['task_type'],
            'task_type_source': ('explicit' if task_override else
                                 ('INCAR' if inspected['task_type'] != 'unknown' else 'unknown')),
            'energy_e0_eV': inspected['energy_e0_eV'],
            'raw_energy_e0_eV': inspected['raw_energy_e0_eV'],
            'observed_energy_eV': inspected['observed_energy_eV'],
            'observed_energy_source': inspected['observed_energy_source'],
            'energy_source': inspected['energy_source'],
            'converged': inspected['converged'],
            'manual_confirmed': inspected['manual_confirmed'],
            'manual_confirmation_requested': inspected['manual_confirmation_requested'],
            'input_complete': inspected['input_complete'],
            'has_output': inspected['has_output'],
            'diagnosis': inspected['diagnosis'],
            'methods': inspected['methods'],
            'files': {
                **inspected['files'], 'copy': copy_files,
                'skipped_large': skipped_large, 'ignored': ignored,
                # Bind the copied method inputs and canonical OSZICAR evidence
                # to the exact bytes classified by this plan.  This closes the
                # preview/re-preview-to-copy race without hashing multi-GB files.
                'approved_sha256': approved_hashes,
            },
            'warnings': member_warnings,
        }
        member['source_fingerprint'] = _source_fingerprint(source, inspected)
        result['fingerprints'][relative] = member['source_fingerprint']
        result['members'].append(member)
        key = {'CREATED': 'created', 'DONE': 'done'}.get(member['state'], 'needs_human')
        result['counts'][key] += 1

    if any(item['role'] == 'molecule' for item in selected) and any(
            item['role'] == 'ref' for item in selected):
        result['warnings'].append(
            '同时映射了 species_refs 与 gas_ref；现有 ΔE 逻辑优先使用 species_refs')

    consistency_errors, consistency_warnings = _project_consistency(
        result['members'], kind)
    result['errors'].extend(consistency_errors)
    result['warnings'].extend(consistency_warnings)

    result['warnings'].extend(scan.get('warnings') or [])
    result['ok'] = not result['errors']
    return result


# Semantic alias used by callers that name the read-only operation after the UI.
preview_import = preview_project


def _system_name(evidence_dir: Path, fallback: str = '') -> str:
    text = _read_text(evidence_dir / 'POSCAR')
    if text:
        first = text.splitlines()[0].strip() if text.splitlines() else ''
        if first:
            return first
    return fallback or evidence_dir.name


def _manifest_for(member: dict, final_dir: Path, evidence_dir: Path) -> dict:
    source = Path(member['source'])
    poscar = evidence_dir / 'POSCAR'
    elements: list[str] = []
    counts: list[int] = []
    poscar_text = _read_text(poscar)
    if poscar_text:
        elements, counts = parse_poscar_species(poscar_text)

    facts, method_warnings = _method_facts(evidence_dir)
    kpoint_facts = facts.get('kpoints') or {}
    inputs = {
        'import_source': str(source.resolve()),
        'legacy_source': str(source.resolve()),
        'import_role': member['role'],
        'task_type_source': member.get('task_type_source'),
        'incar_source': 'imported_verbatim',
        'completions': {},
        'elements': elements,
        'counts': counts,
        'method_facts': facts,
        'kpoints': list(kpoint_facts.get('grid') or []),
        'potcar_provenance': list(facts.get('potcars') or []),
        # Structured blocker for submit/preflight consumers.  A NEEDS_HUMAN
        # state alone is not enough because legacy submit paths may not inspect
        # state before upload.
        'import_input_issues': list((member.get('files') or {}).get('input_issues') or []),
        'import_recalculation': bool(member.get('force_created')),
    }
    if poscar_text:
        inputs['poscar'] = str(final_dir / 'POSCAR')
        inputs['poscar_sha256'] = manifest_mod.sha256_file(poscar)
    potcar = evidence_dir / 'POTCAR'
    if _copyable_file(potcar):
        inputs['potcar_sha256'] = manifest_mod.sha256_file(potcar)

    calc_type = 'molecule' if member['role'] in ('ref', 'molecule') else 'slab'
    manifest = manifest_mod.new_manifest(
        job_id=f'import-{_safe_stem(member["name"])}-{uuid.uuid4().hex[:8]}',
        system=_system_name(evidence_dir, source.name), task_type=member['task_type'],
        calc_type=calc_type, inputs=inputs,
        warnings=list(dict.fromkeys([*(member.get('warnings') or []), *method_warnings])),
    )
    state = member['state']
    if state != 'CREATED':
        note = member.get('diagnosis', {}).get('evidence') or '本地结果导入'
        manifest_mod.set_state(manifest, state, note=f'本地导入:{note}')

    results = manifest.setdefault('results', {})
    if member.get('energy_e0_eV') is not None:
        results['energy_e0_eV'] = float(member['energy_e0_eV'])
    elif member.get('raw_energy_e0_eV') is not None:
        results['raw_energy_e0_eV'] = float(member['raw_energy_e0_eV'])
    if member.get('observed_energy_eV') is not None:
        results['raw_energy_eV'] = float(member['observed_energy_eV'])
        results['raw_energy_source'] = member.get('observed_energy_source')
    if member.get('energy_source'):
        results['energy_source'] = member['energy_source']
    results['convergence_marker_seen'] = bool(member.get('converged'))
    if member.get('manual_confirmation_requested'):
        results['manual_confirmation_requested'] = True
    if member.get('manual_confirmed'):
        results['manual_confirmed'] = True
        results['manual_confirmed_at'] = _now()
    results['diagnosis'] = {
        **dict(member.get('diagnosis') or {}),
        'imported_at': _now(),
        'expected_marker': (
            _expected_marker(member['task_type'])),
    }
    return manifest


def _same_number(left, right) -> bool:
    if left is None or right is None:
        return left is right
    try:
        return abs(float(left) - float(right)) <= 1e-10
    except (TypeError, ValueError):
        return False


def _copy_member(member: dict, stage_dir: Path, final_dir: Path) -> None:
    source = Path(member['source'])
    stage_dir.mkdir(parents=True, exist_ok=False)
    for name in member.get('files', {}).get('copy') or []:
        src = source / name
        if not _copyable_file(src):
            raise OSError(f'预检后源文件消失或不再是普通文件:{src}')
        shutil.copy2(src, stage_dir / name)

    for name, expected in (
            member.get('files', {}).get('approved_sha256') or {}).items():
        copied = stage_dir / name
        if (not _copyable_file(copied)
                or manifest_mod.sha256_file(copied) != expected):
            raise OSError(f'源文件在预检/复制期间发生变化，拒绝导入:{source / name}')

    # Re-run the scientific gate on the bytes that will actually be managed.
    # This detects a source changing between preview and copy, and guarantees
    # manifest hashes/method provenance describe the staged copy, not a later
    # revision of the user's source directory.  A default-skipped vasprun-only
    # result has no copied output evidence, so its source-side NEEDS_HUMAN plan is
    # retained (it cannot become DONE without explicit confirmation).
    copied_output = any(name in OUTPUT_MARKERS
                        for name in member.get('files', {}).get('copy') or [])
    if copied_output or not member.get('has_output'):
        staged = _state_preview(
            stage_dir, manual_confirm=bool(member.get('manual_confirmed')),
            task_type_override=(member['task_type']
                                if member.get('task_type_source') == 'explicit' else None))
        comparable = (
            staged['state_suggestion'] == member['state']
            and staged['task_type'] == member['task_type']
            and _same_number(staged['energy_e0_eV'], member.get('energy_e0_eV'))
            and _same_number(staged['raw_energy_e0_eV'], member.get('raw_energy_e0_eV'))
        )
        if not comparable:
            raise OSError(f'源目录在预检后发生变化，拒绝写入不一致清单:{source}')
    manifest_mod.save_manifest(stage_dir, _manifest_for(member, final_dir, stage_dir))


def import_project(root: str | os.PathLike, name: str,
                   output_root: str | os.PathLike, assignments=None, *,
                   include_large_outputs: bool = False,
                   manual_confirm=False,
                   project_kind: str = 'adsorption',
                   expected_fingerprints=None) -> dict:
    """Commit a previewed import through an atomic staging-directory rename."""
    plan = preview_project(
        root, name, output_root, assignments,
        include_large_outputs=include_large_outputs,
        manual_confirm=manual_confirm,
        project_kind=project_kind,
    )
    if not plan.get('ok'):
        plan.setdefault('error', '导入预检未通过')
        return plan
    if expected_fingerprints is not None:
        expected = (dict(expected_fingerprints)
                    if isinstance(expected_fingerprints, Mapping) else None)
        if expected is None or expected != plan.get('fingerprints'):
            plan['ok'] = False
            plan['error'] = '源数据在预检后发生变化，请重新预检并确认最新结果'
            plan.setdefault('errors', []).append(plan['error'])
            return plan

    destination = Path(plan['destination'])
    parent = destination.parent
    stage: Path | None = None
    committed = False
    project_path = destination / adsorption.PROJECT_NAME
    preexisting_jobs: set[str] = set()
    preexisting_projects: set[str] = set()
    try:
        preexisting_jobs = set(ledger.list_dirs())
        preexisting_projects = set(adsorption.list_projects())
        parent.mkdir(parents=True, exist_ok=True)
        # Re-check after mkdir to close the ordinary preview/commit race window.
        target_error = _target_error(Path(plan['source_root']), destination)
        if target_error:
            plan['ok'] = False
            plan['errors'].append(target_error)
            plan['error'] = target_error
            return plan
        stage = Path(tempfile.mkdtemp(prefix=f'.{destination.name}.import-', dir=parent))

        clean_dir = None
        ref_dir = None
        config_dirs: list[str] = []
        config_species: dict[str, str] = {}
        molecule_root = destination / 'molecules'
        species_refs: dict[str, float | None] = {}
        species_ref_jobs: dict[str, str] = {}
        final_job_dirs: list[str] = []

        for member in plan['members']:
            final_dir = Path(member['destination']).resolve()
            if not _is_relative_to(final_dir, destination.resolve()):
                raise ValueError(f'成员目标越出项目目录:{final_dir}')
            relative_target = final_dir.relative_to(destination)
            stage_dir = (stage / relative_target).resolve()
            if not _is_relative_to(stage_dir, stage.resolve()):
                raise ValueError(f'暂存目标越出 staging:{stage_dir}')
            _copy_member(member, stage_dir, final_dir)
            final_job_dirs.append(str(final_dir))
            if member['role'] == 'clean':
                clean_dir = str(final_dir)
            elif member['role'] == 'config':
                config_dirs.append(str(final_dir))
                if member.get('species'):
                    config_species[str(final_dir)] = str(member['species'])
            elif member['role'] == 'ref':
                ref_dir = str(final_dir)
            elif member['role'] == 'molecule':
                energy = member.get('energy_e0_eV')
                species_refs[str(member['species'])] = (
                    float(energy) if energy is not None else None)
                species_ref_jobs[str(member['species'])] = str(final_dir)

        project = {
            'schema': 1, 'kind': plan.get('project_kind', 'adsorption'),
            'name': str(name), 'created_at': _now(),
            'root': str(destination), 'import_source': str(Path(plan['source_root'])),
            'members': {
                'clean_slab': clean_dir, 'gas_ref': ref_dir, 'configs': config_dirs,
                'molecules': species_ref_jobs,
            },
            'method_fingerprints': {
                'schema': 1,
                'members': [
                    {
                        'path': member['destination'],
                        'role': member['role'],
                        'species': member.get('species'),
                        'fingerprint': freeenergy.method_fingerprint_from_facts(
                            member.get('methods')),
                    }
                    for member in plan['members']
                ],
            },
        }
        if config_species:
            project['config_species'] = config_species
        if any(member['role'] == 'molecule' for member in plan['members']):
            project['molecules_dir'] = str(molecule_root)
            project['species_refs'] = species_refs
            # Dynamic source-of-truth for a CREATED molecule that is submitted
            # after import.  Consumers can read its DONE manifest instead of
            # treating the numeric species_refs snapshot as permanently stale.
            project['species_ref_jobs'] = species_ref_jobs
        adsorption.save_project(stage, project)

        if destination.exists():
            raise FileExistsError(f'目标在导入期间被创建，拒绝覆盖:{destination}')
        os.replace(stage, destination)
        stage = None
        committed = True

        registered = []
        for job_dir in final_job_dirs:
            ledger.register(job_dir)
            registered.append(job_dir)
        adsorption.register_project(project_path)

        plan.update({
            'ok': True, 'project_path': str(project_path), 'project': project,
            'registered_jobs': registered,
        })
        return plan
    except Exception as exc:  # noqa: BLE001  transactional boundary; return JSON-safe API error
        plan['ok'] = False
        plan['error'] = f'导入失败:{exc}'
        plan.setdefault('errors', []).append(plan['error'])
        if committed:
            rollback_errors: list[str] = []
            # A register implementation may fail after its atomic write; inspect
            # current state rather than relying only on its return value.
            for job_dir in [item['destination'] for item in plan.get('members') or []]:
                try:
                    if job_dir not in preexisting_jobs and job_dir in set(ledger.list_dirs()):
                        ledger.unregister(job_dir)
                except Exception as rollback_exc:  # noqa: BLE001 rollback must continue
                    rollback_errors.append(f'台账回滚失败({job_dir}):{rollback_exc}')
            try:
                project_entry = str(project_path.resolve())
                if (project_entry not in preexisting_projects
                        and project_entry in set(adsorption.list_projects())):
                    _unregister_project(project_path)
            except Exception as rollback_exc:  # noqa: BLE001 rollback must continue
                rollback_errors.append(f'项目注册回滚失败:{rollback_exc}')
            try:
                _remove_tree(destination)
            except Exception as rollback_exc:  # noqa: BLE001 report incomplete rollback
                rollback_errors.append(f'目标目录回滚失败:{rollback_exc}')
            if rollback_errors:
                plan.setdefault('warnings', []).extend(rollback_errors)
        return plan
    finally:
        if stage is not None and stage.exists():
            try:
                _remove_tree(stage)
            except OSError:
                pass


__all__ = (
    'INPUT_FILES', 'OUTPUT_MARKERS', 'BASIC_COPY_FILES', 'LARGE_OUTPUT_FILES',
    'LIS_SPECIES', 'validate_vasp_quartet', 'scan_root', 'preview_project',
    'preview_import', 'import_project',
)
