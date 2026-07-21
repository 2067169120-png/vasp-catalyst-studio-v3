"""Import already-computed local VASP results into managed projects.

The source tree is always read-only.  Discovery accepts result-only folders (for
example ``CONTCAR`` + ``OSZICAR`` + ``OUTCAR``) and deliberately separates
"VASP produced an energy" from "the requested calculation converged".  A DONE
suggestion therefore needs several mutually consistent pieces of evidence;
ambiguous output remains NEEDS_HUMAN and can only be promoted by an auditable
manual confirmation when no hard failure is present.

This module contains no GUI or cluster code.  ``scan_folder`` and
``commit_import`` return JSON-friendly dictionaries so both the web UI and
tests can use the same scientific gate.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from vcstudio.cluster import diagnose
from vcstudio.generate import methods_text
from vcstudio.generate.incar_builder import parse_incar
from vcstudio.generate.poscar import parse_poscar_species, read_cell_vectors


_RESULT_NAMES = ('OUTCAR', 'OSZICAR', 'vasprun.xml', 'CONTCAR')
_OUTPUT_EVIDENCE_NAMES = ('OUTCAR', 'OSZICAR', 'vasprun.xml')
_INPUT_NAMES = ('INCAR', 'POSCAR', 'KPOINTS', 'POTCAR')
_COPY_NAMES = (
    'INCAR', 'POSCAR', 'KPOINTS', 'POTCAR',
    'CONTCAR', 'OSZICAR', 'OUTCAR', 'vasprun.xml',
)
_ROLE_OPTIONS = (
    'clean_slab', 'config', 'gas_ref', 'molecule_ref', 'standalone', 'ignore',
)
_IMPORT_TASK_TYPES = frozenset({'relax', 'static', 'dos', 'band', 'freq'})
_SCF_RE = re.compile(
    r'^\s*(?:DAV|RMM|EDDAV|CG|DMP|QDMP|DIA|NONE):\s*(\d+)\b', re.I)
_E0_RE = re.compile(r'\bE0\s*=\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)', re.I)
_IONIC_DE_RE = re.compile(
    r'\bd\s*E\s*=\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)', re.I)
_OUTCAR_PARAM_RE = {
    key: re.compile(rf'\b{key}\s*=\s*([^;\s]+)', re.I)
    for key in ('NELM', 'NSW', 'IBRION', 'EDIFF', 'EDIFFG')
}
_FMAX_RE = re.compile(
    r'FORCES:\s*max\s+atom\s*,\s*RMS\s+([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)',
    re.I,
)
_SIGMA0_RE = re.compile(
    r'energy\s*\(\s*sigma\s*->\s*0\s*\)\s*=\s*'
    r'([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)', re.I)
_TOTEN_RE = re.compile(
    r'free\s+energy\s+TOTEN\s*=\s*'
    r'([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)', re.I)
_IONIC_OK_RE = re.compile(
    r'reached\s+required\s+accuracy(?:\s*-?\s*stopping\s+structural\s+energy\s+minimi[sz]ation)?',
    re.I,
)
_ELECTRONIC_OK_RE = re.compile(
    r'aborting\s+loop\s+because\s+EDIFF\s+is\s+reached', re.I)
_FOOTER_RE = re.compile(
    r'(?:General\s+timing\s+and\s+accounting\s+information(?:s)?\s+for\s+this\s+job'
    r'|Total\s+CPU\s+time\s+used|Voluntary\s+context\s+switches)',
    re.I,
)
_STOP_RE = re.compile(r'soft\s+stop\s+encountered|STOPCAR', re.I)
_FATAL_RE = re.compile(
    r'SIGSEGV|segmentation\s+fault|ZBRENT:\s*(?:fatal|bracketing)'
    r'|VERY\s+BAD\s+NEWS|No\s+space\s+left\s+on\s+device'
    r'|Disk\s+quota\s+exceeded|out\s+of\s+memory',
    re.I,
)
_VASP_START_RE = re.compile(r'^\s*vasp\.\d', re.I | re.M)
_FORMULA_RE = re.compile(r'(?:[A-Z][a-z]?\d*){1,12}')
_ELEMENTS = frozenset(
    'H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn '
    'Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La '
    'Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po '
    'At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg '
    'Cn Nh Fl Mc Lv Ts Og'.split())
_MAX_OUTCAR_HEAD = 4 * 1024 * 1024
_MAX_OUTCAR_TAIL = 16 * 1024 * 1024
_MAX_OSZICAR_TAIL = 8 * 1024 * 1024
# OSZICAR E0, OUTCAR energy(sigma->0), and vasprun's corresponding value should
# describe the same final ionic step.  Differences below one meV can be caused
# by printed precision; larger differences usually mean files from different
# restarts/runs were mixed in one directory.
_ENERGY_CONSISTENCY_TOL_EV = 1e-3


def _read_edges(path: Path, head_bytes: int, tail_bytes: int) -> tuple[str, bool]:
    """Read a bounded head+tail view; return (text, middle_was_omitted)."""
    try:
        size = path.stat().st_size
        with open(path, 'rb') as f:
            if size <= head_bytes + tail_bytes:
                raw = f.read()
                omitted = False
            else:
                raw = f.read(head_bytes)
                f.seek(max(size - tail_bytes, 0))
                raw += b'\n... vcstudio omitted middle of large file ...\n' + f.read(tail_bytes)
                omitted = True
        return raw.decode('utf-8', errors='replace'), omitted
    except OSError:
        return '', False


def _files_by_canonical(folder: Path) -> dict[str, Path]:
    """Return known files case-insensitively, preferring the canonical spelling."""
    found: dict[str, Path] = {}
    wanted = {name.casefold(): name for name in (*_COPY_NAMES,)}
    try:
        entries = sorted(folder.iterdir(), key=lambda p: p.name.casefold())
    except OSError:
        return found
    for entry in entries:
        if entry.is_symlink() or not entry.is_file():
            continue
        canonical = wanted.get(entry.name.casefold())
        if canonical is None:
            continue
        old = found.get(canonical)
        if old is None or entry.name == canonical:
            found[canonical] = entry
    return found


def _number(value, default=None):
    try:
        out = float(str(value).strip())
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _integer(value, default=None):
    n = _number(value)
    return int(n) if n is not None else default


def _numeric_vector(value, *, integer: bool = False):
    """Parse an OUTCAR numeric vector, including VASP's ``n*value`` shorthand."""
    result = []
    for token in str(value or '').replace(',', ' ').split():
        repeated = re.fullmatch(r'(\d+)\*([^*]+)', token)
        count, raw = (int(repeated.group(1)), repeated.group(2)) if repeated else (1, token)
        number = _number(raw)
        if number is None or (integer and number != int(number)):
            return None
        result.extend([int(number) if integer else float(number)] * count)
    return result or None


def _titel_element(titel: str) -> str | None:
    """Best-effort element identity from e.g. ``PAW_PBE Fe_pv 06Sep2000``."""
    tokens = str(titel or '').split()
    for token in tokens[1:]:
        base = token.split('_', 1)[0]
        if base in _ELEMENTS:
            return base
    return None


def _reasonable_energy(value) -> bool:
    """Conservative VASP total-energy sanity gate used by diagnose as well."""
    return value is not None and not diagnose.energy_implausible(value)


def _validate_kpoints(text: str) -> str | None:
    """Validate automatic, explicit, and line-mode KPOINTS without guessing."""
    lines = [line.strip() for line in str(text or '').splitlines()]
    if len(lines) < 4:
        return 'KPOINTS 行数不足'
    try:
        count = int(lines[1].split()[0])
    except (IndexError, ValueError):
        return 'KPOINTS 第 2 行不是合法点数'
    def _values(line: str, expected: int, label: str) -> list[float] | str:
        raw = line.split('!', 1)[0].split('#', 1)[0].split()
        if len(raw) < expected:
            return f'{label}须至少包含 {expected} 个数值'
        try:
            values = [float(value) for value in raw[:expected]]
        except ValueError:
            return f'{label}含非数值字段'
        if any(not math.isfinite(value) for value in values):
            return f'{label}须使用有限数值'
        return values

    mode = lines[2].lower()
    if mode.startswith('l'):
        if count <= 0 or len(lines) < 6:
            return 'Line-mode KPOINTS 缺分段数、坐标系或端点'
        coordinate_mode = lines[3].lower()
        if not coordinate_mode.startswith(('r', 'k', 'c')):
            return 'Line-mode KPOINTS 第 4 行须为 Reciprocal 或 Cartesian'
        points = [line for line in lines[4:] if line]
        if len(points) < 2:
            return 'Line-mode KPOINTS 至少需要两个端点'
        for index, line in enumerate(points, start=1):
            parsed = _values(line, 3, f'Line-mode 第 {index} 个端点')
            if isinstance(parsed, str):
                return parsed
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
            shift = _values(lines[4], 3, '自动 KPOINTS 位移')
            if isinstance(shift, str):
                return shift
    elif count > 0:
        if not mode.startswith(('r', 'k', 'c')):
            return '显式 KPOINTS 第 3 行须为 Reciprocal 或 Cartesian'
        points = [line for line in lines[3:] if line]
        if len(points) < count:
            return f'显式 KPOINTS 声明 {count} 个点但坐标行不足'
        for index, line in enumerate(points[:count], start=1):
            parsed = _values(line, 4, f'显式 KPOINTS 第 {index} 个点（含权重）')
            if isinstance(parsed, str):
                return parsed
    elif count < 0:
        return 'KPOINTS 点数不能为负数'
    return None


def _validate_poscar_coordinates(text: str, counts: list[int]) -> str | None:
    lines = str(text or '').splitlines()
    if len(lines) < 5:
        return 'POSCAR 行数不足以解析晶格矢量'
    try:
        vectors = read_cell_vectors(str(text or ''))
    except (ValueError, IndexError, TypeError) as exc:
        return f'POSCAR 晶格解析失败：{exc}'
    determinant = (
        vectors[0][0] * (vectors[1][1] * vectors[2][2] - vectors[1][2] * vectors[2][1])
        - vectors[0][1] * (vectors[1][0] * vectors[2][2] - vectors[1][2] * vectors[2][0])
        + vectors[0][2] * (vectors[1][0] * vectors[2][1] - vectors[1][1] * vectors[2][0])
    )
    if abs(determinant) <= 1e-14:
        return 'POSCAR 三个晶格矢量线性相关，晶胞体积为 0'
    index = 7
    if len(lines) <= index:
        return 'POSCAR 缺坐标模式与原子坐标'
    if lines[index].strip().lower().startswith('s'):
        index += 1
    if len(lines) <= index or not lines[index].strip().lower().startswith(('d', 'c', 'k')):
        return 'POSCAR 坐标模式须为 Direct 或 Cartesian'
    index += 1
    expected = sum(counts)
    coordinates = lines[index:index + expected]
    if len(coordinates) != expected:
        return f'POSCAR 坐标数不足：应有 {expected} 行，实际 {len(coordinates)} 行'
    for offset, line in enumerate(coordinates, start=1):
        try:
            values = [float(value) for value in line.split()[:3]]
        except ValueError:
            return f'POSCAR 第 {offset} 个原子坐标不是数值'
        if len(values) != 3 or any(not math.isfinite(value) for value in values):
            return f'POSCAR 第 {offset} 个原子坐标须为 3 个有限数值'
    return None


def validate_vasp_quartet(source: str | os.PathLike) -> list[str]:
    """Side-effect-free scientific gate for a user-supplied VASP quartet."""
    folder = Path(source).expanduser()
    if not folder.is_dir():
        return [f'四件套目录不存在或不可读：{folder}']
    files = _files_by_canonical(folder)
    required = ('INCAR', 'POSCAR', 'KPOINTS', 'POTCAR')
    issues = [f'缺 {name}' for name in required if name not in files]
    if issues:
        return issues
    texts = {}
    for name in required:
        try:
            texts[name] = files[name].read_text(encoding='utf-8', errors='replace')
        except OSError as exc:
            issues.append(f'{name} 不可读：{exc}')
            texts[name] = ''
        if not texts[name].strip():
            issues.append(f'{name} 为空或不可读')
    if issues:
        return list(dict.fromkeys(issues))

    try:
        incar = parse_incar(texts['INCAR'])
        if not incar:
            issues.append('INCAR 未解析到任何 KEY=VALUE 参数')
    except (TypeError, ValueError) as exc:
        issues.append(f'INCAR 解析失败：{exc}')

    try:
        species, counts = parse_poscar_species(texts['POSCAR'])
    except (TypeError, ValueError, IndexError, NotImplementedError) as exc:
        species, counts = [], []
        issues.append(f'POSCAR 物种/计数解析失败：{exc}')
    if not species or not counts or len(species) != len(counts):
        issues.append('POSCAR 缺合法物种/计数行')
    elif any(count <= 0 for count in counts):
        issues.append('POSCAR 原子计数须为正整数')
    else:
        coordinate_issue = _validate_poscar_coordinates(texts['POSCAR'], counts)
        if coordinate_issue:
            issues.append(coordinate_issue)

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
            'POTCAR 元素顺序与 POSCAR 不一致：'
            f'{" ".join(str(item) for item in potcar_elements)} != {" ".join(species)}')
    return list(dict.fromkeys(issues))


def _input_method_signature(files: dict[str, Path]) -> dict:
    """Build a comparable method signature from a validated input quartet."""
    try:
        incar_text = files['INCAR'].read_text(encoding='utf-8', errors='replace')
        kpoints_text = files['KPOINTS'].read_text(encoding='utf-8', errors='replace')
        potcar_text = files['POTCAR'].read_text(encoding='utf-8', errors='replace')
    except (KeyError, OSError):
        return {}
    payload = methods_text.extract_facts(incar_text, kpoints_text, potcar_text)
    facts = payload.get('facts') or {}
    incar = parse_incar(incar_text)
    potcars = list(facts.get('potcars') or [])
    signature = {
        'functional': facts.get('functional'),
        'gga': facts.get('gga'),
        'metagga': facts.get('metagga') or 'F',
        'ivdw': facts.get('ivdw_setting'),
        'ispin': facts.get('ispin') or 1,
        'encut': facts.get('encut'),
        'ldau': 'T' if facts.get('ldau') else 'F',
        'lhfcalc': 'T' if facts.get('lhfcalc') else 'F',
        'aexx': facts.get('aexx'),
        'hfscreen': facts.get('hfscreen'),
        'potcar_titel': [row.get('titel') for row in potcars if row.get('titel')],
        'potcar_elements': [row.get('element') for row in potcars if row.get('element')],
    }
    for key, integer in (('LDAUTYPE', True), ('LDAUL', True),
                         ('LDAUU', False), ('LDAUJ', False)):
        if key not in incar:
            continue
        if key == 'LDAUTYPE':
            parsed = _integer(incar[key])
        else:
            parsed = _numeric_vector(incar[key], integer=integer)
        if parsed is not None:
            signature[key.lower()] = parsed
    return {key: value for key, value in signature.items()
            if value not in (None, '', [])}


def _now_iso() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def _parse_oszicar(path: Path | None) -> dict:
    facts = {
        'present': bool(path and path.is_file()), 'energy_e0_eV': None,
        'ionic_steps': 0, 'final_scf_steps': None, 'final_ionic_dE_eV': None,
        'trailing_incomplete_scf': False, 'read_truncated': False,
    }
    if not facts['present']:
        return facts
    text, omitted = _read_edges(path, 0, _MAX_OSZICAR_TAIL)
    facts['read_truncated'] = omitted
    current_scf = 0
    summaries: list[tuple[float, int, float | None]] = []
    last_step_no = 0
    after_last_summary_scf = False
    for line in text.splitlines():
        m_scf = _SCF_RE.match(line)
        if m_scf:
            current_scf = max(current_scf, int(m_scf.group(1)))
            after_last_summary_scf = bool(summaries)
            continue
        if 'F=' not in line.upper() or 'E0' not in line.upper():
            continue
        m_e = _E0_RE.search(line)
        if not m_e:
            current_scf = 0
            continue
        energy = _number(m_e.group(1))
        if energy is None:
            current_scf = 0
            continue
        m_de = _IONIC_DE_RE.search(line)
        try:
            step_no = int(line.split()[0])
        except (ValueError, IndexError):
            step_no = last_step_no + 1
        # Concatenated/restarted OSZICAR files commonly reset the ionic counter.
        # Only the terminal run may decide DONE.
        if summaries and step_no <= last_step_no:
            summaries.clear()
        summaries.append((energy, current_scf, _number(m_de.group(1)) if m_de else None))
        last_step_no = step_no
        current_scf = 0
        after_last_summary_scf = False
    if summaries:
        energy, scf, ionic_de = summaries[-1]
        facts.update({
            'energy_e0_eV': energy,
            'ionic_steps': len(summaries),
            'final_scf_steps': scf or None,
            'final_ionic_dE_eV': ionic_de,
            'trailing_incomplete_scf': bool(after_last_summary_scf and current_scf),
        })
    return facts


def _parse_outcar(path: Path | None) -> dict:
    facts = {
        'present': bool(path and path.is_file()), 'read_truncated': False,
        'ionic_converged_marker': False, 'electronic_converged_marker': False,
        'normal_footer': False, 'soft_stopped': False, 'fatal_error': None,
        'final_force_max_eV_A': None, 'energy_e0_eV': None,
        'energy_source': None, 'observed_energy_eV': None,
        'observed_energy_source': None, 'parameters': {}, 'method_signature': {},
    }
    if not facts['present']:
        return facts
    text, omitted = _read_edges(path, _MAX_OUTCAR_HEAD, _MAX_OUTCAR_TAIL)
    facts['read_truncated'] = omitted
    terminal_basis = (text.rsplit('... vcstudio omitted middle of large file ...', 1)[-1]
                      if omitted else text)
    starts = list(_VASP_START_RE.finditer(terminal_basis))
    terminal = terminal_basis[starts[-1].start():] if len(starts) > 1 else terminal_basis
    facts['ionic_converged_marker'] = bool(_IONIC_OK_RE.search(terminal))
    facts['electronic_converged_marker'] = bool(_ELECTRONIC_OK_RE.search(terminal))
    facts['normal_footer'] = bool(_FOOTER_RE.search(terminal))
    facts['soft_stopped'] = bool(_STOP_RE.search(terminal))
    fatal = _FATAL_RE.search(terminal)
    if fatal:
        facts['fatal_error'] = fatal.group(0).strip()
    elif not facts['normal_footer']:
        known = diagnose.scan_vasp_error(terminal)
        hard = diagnose.scan_log(terminal)
        if known:
            facts['fatal_error'] = f'{known[0]}: {known[1]}'
        elif hard:
            facts['fatal_error'] = hard
    forces = [_number(v) for v in _FMAX_RE.findall(terminal)]
    facts['final_force_max_eV_A'] = next((v for v in reversed(forces) if v is not None), None)
    sigma0 = [_number(v) for v in _SIGMA0_RE.findall(terminal)]
    energy = next((v for v in reversed(sigma0) if _reasonable_energy(v)), None)
    if energy is not None:
        facts['energy_e0_eV'], facts['energy_source'] = energy, 'OUTCAR:energy(sigma->0)'
    else:
        toten = [_number(v) for v in _TOTEN_RE.findall(terminal)]
        energy = next((v for v in reversed(toten) if _reasonable_energy(v)), None)
        if energy is not None:
            # TOTEN is the finite-temperature free energy, not the sigma->0 E0
            # used by this project's adsorption-energy convention.  Keep it as
            # visible evidence, but never relabel it as energy_e0_eV.
            facts['observed_energy_eV'] = energy
            facts['observed_energy_source'] = 'OUTCAR:TOTEN'
    for key, regex in _OUTCAR_PARAM_RE.items():
        m = regex.search(terminal) or regex.search(text)
        if m:
            value = m.group(1)
            facts['parameters'][key] = (_integer(value) if key in {'NELM', 'NSW', 'IBRION'}
                                        else _number(value))
    signature = {}
    scalar_patterns = {
        'GGA': r'\bGGA\s*=\s*([^;\s]+)',
        'LEXCH': r'\bLEXCH\s*=\s*([^;\s]+)',
        'METAGGA': r'\bMETAGGA\s*=\s*([^;\s]+)',
        'IVDW': r'\bIVDW\s*=\s*([^;\s]+)',
        'ISPIN': r'\bISPIN\s*=\s*([^;\s]+)',
        'ENCUT': r'\bENCUT\s*=\s*([-+0-9.Ee]+)',
        'LDAU': r'\bLDAU\s*=\s*([^;\s]+)',
        'LDAUTYPE': r'\bLDAUTYPE\s*=\s*([^;\s]+)',
        'LHFCALC': r'\bLHFCALC\s*=\s*([^;\s]+)',
        'AEXX': r'\bAEXX\s*=\s*([^;\s]+)',
        'HFSCREEN': r'\bHFSCREEN\s*=\s*([^;\s]+)',
    }
    for key, pattern in scalar_patterns.items():
        hits = re.findall(pattern, text, re.I)
        if hits:
            value = hits[-1]
            signature[key.lower()] = (_number(value) if key in {'ENCUT', 'AEXX', 'HFSCREEN'}
                                      else _integer(value) if key in {'IVDW', 'ISPIN', 'LDAUTYPE'}
                                      else str(value).strip())
    vector_patterns = {
        'ldaul': (r'^\s*LDAUL\s*=\s*(.*?)\s*$', True),
        'ldauu': (r'^\s*LDAUU\s*=\s*(.*?)\s*$', False),
        'ldauj': (r'^\s*LDAUJ\s*=\s*(.*?)\s*$', False),
    }
    for key, (pattern, integer) in vector_patterns.items():
        hits = re.findall(pattern, text, re.I | re.M)
        if hits:
            parsed = _numeric_vector(hits[-1], integer=integer)
            if parsed is not None:
                signature[key] = parsed
    gga = str(signature.get('gga') or '').upper()
    signature['functional'] = {
        'RP': 'RPBE', 'PE': 'PBE', 'PS': 'PBEsol', '91': 'PW91',
    }.get(gga, gga or str(signature.get('lexch') or '').upper() or None)
    titels = []
    for titel in re.findall(r'^\s*TITEL\s*=\s*(.+?)\s*$', text, re.I | re.M):
        value = titel.strip()
        if value and value not in titels:
            titels.append(value)
    if titels:
        signature['potcar_titel'] = titels
        titel_elements = [_titel_element(titel) for titel in titels]
        if all(titel_elements) and len(set(titel_elements)) == len(titel_elements):
            signature['potcar_elements'] = titel_elements
    facts['method_signature'] = {key: value for key, value in signature.items()
                                 if value not in (None, '')}
    return facts


def _parse_incar(path: Path | None) -> dict:
    if not path or not path.is_file():
        return {}
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return {}
    out = {}
    for raw in text.splitlines():
        line = raw.split('#', 1)[0].split('!', 1)[0]
        if '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip().upper()
        if key in {'NELM', 'NSW', 'IBRION'}:
            out[key] = _integer(value.strip())
        elif key in {'EDIFF', 'EDIFFG'}:
            out[key] = _number(value.strip())
    return {k: v for k, v in out.items() if v is not None}


def _parse_vasprun(path: Path | None) -> dict:
    """Stream the XML and prove it is well-formed through ``</modeling>``.

    VASP variants exist where the final ionic ``e_0_energy`` is zero although
    ``e_wo_entrp`` and OSZICAR E0 are valid.  We retain that anomaly as evidence
    and never let it replace a reasonable OSZICAR value.
    """
    facts = {
        'present': bool(path and path.is_file()), 'checked': False, 'complete': False,
        'parse_error': None, 'calculations': 0, 'final_scf_steps': None,
        'energy_e0_eV': None, 'energy_source': None,
        'observed_energy_eV': None, 'observed_energy_source': None,
        'raw_final_energies': {}, 'final_force_max_eV_A': None, 'parameters': {},
        'method_signature': {}, 'e0_anomaly': False,
    }
    if not facts['present']:
        return facts
    facts['checked'] = True
    params = {'NELM', 'NSW', 'IBRION', 'EDIFF', 'EDIFFG'}
    in_calc = False
    in_scstep = False
    force_varray = False
    calc_scf = 0
    calc_energies: dict[str, float] = {}
    calc_forces: list[float] = []
    root_tag = None
    method_scalars = {
        'GGA', 'LEXCH', 'METAGGA', 'IVDW', 'ISPIN', 'ENCUT', 'LDAU',
        'LDAUTYPE', 'LHFCALC', 'AEXX', 'HFSCREEN',
    }
    method_vectors = {'LDAUL', 'LDAUU', 'LDAUJ'}
    try:
        for event, elem in ET.iterparse(path, events=('start', 'end')):
            tag = elem.tag.rsplit('}', 1)[-1]
            if event == 'start':
                if root_tag is None:
                    root_tag = tag
                if tag == 'calculation':
                    in_calc = True
                    calc_scf = 0
                    calc_energies = {}
                    calc_forces = []
                elif in_calc and tag == 'scstep':
                    in_scstep = True
                    calc_scf += 1
                elif in_calc and tag == 'varray' and elem.attrib.get('name') == 'forces':
                    force_varray = True
                continue

            if tag == 'i':
                name = elem.attrib.get('name', '')
                value = (elem.text or '').strip()
                if not in_calc and name in params and name not in facts['parameters']:
                    facts['parameters'][name] = (
                        _integer(value) if name in {'NELM', 'NSW', 'IBRION'} else _number(value))
                if not in_calc and name in method_scalars:
                    facts['method_signature'][name.lower()] = (
                        _number(value) if name in {'ENCUT', 'AEXX', 'HFSCREEN'}
                        else _integer(value) if name in {'IVDW', 'ISPIN', 'LDAUTYPE'}
                        else value)
                elif in_calc and not in_scstep and name in {
                        'e_0_energy', 'e_wo_entrp', 'e_fr_energy'}:
                    n = _number(value)
                    if n is not None:
                        calc_energies[name] = n
            elif tag == 'v' and in_calc and force_varray:
                try:
                    xyz = [float(v) for v in (elem.text or '').split()[:3]]
                    if len(xyz) == 3 and all(math.isfinite(v) for v in xyz):
                        calc_forces.append(math.sqrt(sum(v * v for v in xyz)))
                except ValueError:
                    pass
            elif tag == 'v' and not in_calc and elem.attrib.get('name', '') in method_vectors:
                name = elem.attrib['name']
                parsed = _numeric_vector(elem.text, integer=(name == 'LDAUL'))
                if parsed is not None:
                    facts['method_signature'][name.lower()] = parsed
            elif tag == 'scstep':
                in_scstep = False
            elif tag == 'varray' and force_varray:
                force_varray = False
            elif tag == 'calculation':
                facts['calculations'] += 1
                facts['final_scf_steps'] = calc_scf or None
                facts['raw_final_energies'] = dict(calc_energies)
                facts['final_force_max_eV_A'] = max(calc_forces) if calc_forces else None
                in_calc = False
            elem.clear()
        facts['complete'] = root_tag == 'modeling'
    except (ET.ParseError, OSError) as exc:
        facts['parse_error'] = str(exc)
        return facts

    signature = facts['method_signature']
    gga = str(signature.get('gga') or '').upper()
    signature['functional'] = {
        'RP': 'RPBE', 'PE': 'PBE', 'PS': 'PBEsol', '91': 'PW91',
    }.get(gga, gga or str(signature.get('lexch') or '').upper() or None)
    facts['method_signature'] = {
        key: value for key, value in signature.items() if value not in (None, '')
    }

    energies = facts['raw_final_energies']
    raw_e0 = energies.get('e_0_energy')
    if raw_e0 is not None and not _reasonable_energy(raw_e0):
        facts['e0_anomaly'] = True
    if _reasonable_energy(raw_e0):
        facts['energy_e0_eV'] = raw_e0
        facts['energy_source'] = 'vasprun.xml:e_0_energy'
    else:
        # VASP's sigma->0 estimate is 1/2*(free energy + energy without
        # entropy).  Some versions write a bogus zero into the final ionic
        # e_0_energy node; derive the documented quantity instead of silently
        # substituting e_wo_entrp (which differs at finite smearing).
        free = energies.get('e_fr_energy')
        without = energies.get('e_wo_entrp')
        derived = ((free + without) / 2.0
                   if _reasonable_energy(free) and _reasonable_energy(without) else None)
        if _reasonable_energy(derived):
            facts['energy_e0_eV'] = derived
            facts['energy_source'] = 'vasprun.xml:derived_sigma0'
        else:
            # A lone e_wo_entrp or e_fr_energy is not E0.  Preserve the value
            # for diagnostics, but force the adsorption-energy gate to wait for
            # a real e_0_energy or the documented paired sigma->0 derivation.
            for name in ('e_wo_entrp', 'e_fr_energy'):
                value = energies.get(name)
                if _reasonable_energy(value):
                    facts['observed_energy_eV'] = value
                    facts['observed_energy_source'] = f'vasprun.xml:{name}'
                    break
    return facts


def _infer_task_type(parameters: dict) -> str:
    ibrion = _integer(parameters.get('IBRION'))
    nsw = _integer(parameters.get('NSW'))
    if ibrion in (5, 6, 7, 8):
        return 'freq'
    if nsw == 0 or ibrion == -1:
        return 'static'
    return 'relax'


def _species_from(folder: Path, files: dict[str, Path]) -> str | None:
    names = [folder.name]
    for key in ('CONTCAR', 'POSCAR'):
        p = files.get(key)
        if p:
            try:
                names.append(p.read_text(encoding='utf-8', errors='replace').splitlines()[0])
            except (OSError, IndexError):
                pass
    for name in names:
        valid = []
        for hit in _FORMULA_RE.findall(name):
            parts = re.findall(r'([A-Z][a-z]?)(\d*)', hit)
            if parts and ''.join(el + count for el, count in parts) == hit \
                    and all(el in _ELEMENTS for el, _count in parts):
                valid.append(hit)
        if valid:
            return max(valid, key=len)
    # Molecule folders often have a generic title.  As a last resort, build a
    # formula from a VASP5 species/count header (useful for Li2Sx libraries).
    for key in ('CONTCAR', 'POSCAR'):
        p = files.get(key)
        if not p:
            continue
        try:
            lines = p.read_text(encoding='utf-8', errors='replace').splitlines()
            elements = lines[5].split()
            counts = [int(v) for v in lines[6].split()]
        except (OSError, IndexError, ValueError):
            continue
        if elements and len(elements) == len(counts) and all(el in _ELEMENTS for el in elements):
            return ''.join(el + (str(n) if n != 1 else '')
                           for el, n in zip(elements, counts))
    return None


def _formula_composition(formula: str) -> dict[str, int] | None:
    parts = re.findall(r'([A-Z][a-z]?)(\d*)', str(formula or '').strip())
    if not parts or ''.join(el + count for el, count in parts) != str(formula or '').strip():
        return None
    composition: dict[str, int] = {}
    for element, count in parts:
        if element not in _ELEMENTS:
            return None
        composition[element] = composition.get(element, 0) + int(count or 1)
    return composition


def _structure_composition(files: dict[str, Path]) -> dict[str, int] | None:
    """Read a VASP5 CONTCAR/POSCAR header, preferring the final CONTCAR."""
    for key in ('CONTCAR', 'POSCAR'):
        path = files.get(key)
        if not path:
            continue
        try:
            lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
            elements = lines[5].split()
            counts = [int(value) for value in lines[6].split()]
        except (OSError, IndexError, ValueError):
            continue
        if (elements and len(elements) == len(counts)
                and all(element in _ELEMENTS for element in elements)
                and all(count > 0 for count in counts)):
            return dict(zip(elements, counts))
    return None


def _suggested_role(folder: Path, species: str | None) -> str:
    low = folder.name.casefold()
    if any(token in low for token in ('clean', 'bare', 'pristine')) or low.endswith('_slab'):
        return 'clean_slab'
    if any(token in low for token in ('gas_ref', 'gas-ref', 'reference', '_ref')):
        return 'gas_ref'
    if low.startswith(('mol_', 'molecule_')):
        return 'molecule_ref'
    if any(token in low for token in ('ads', 'config', 'site', 'top', 'bridge', 'hollow')):
        return 'config'
    if species and (species == 'S8' or re.fullmatch(r'Li\d*S\d*', species)):
        return 'molecule_ref'
    return 'standalone'


def _state_preview(folder: Path, files: dict[str, Path], task_type_override=None) -> dict:
    osz = _parse_oszicar(files.get('OSZICAR'))
    out = _parse_outcar(files.get('OUTCAR'))
    # OUTCAR/vasprun describe what actually ran; a user may have edited INCAR
    # afterwards.  Input values are therefore fallback only, never allowed to
    # overwrite output provenance.
    parameters = {k: v for k, v in _parse_incar(files.get('INCAR')).items()
                  if v is not None}
    parameters.update(out['parameters'])
    task_type = _infer_task_type(parameters)

    # Stream every available XML once.  Besides serving as a fallback, it is a
    # cross-file consistency witness: silently trusting a stale OSZICAR is much
    # more dangerous for adsorption energies than the extra scan time.
    xml = _parse_vasprun(files.get('vasprun.xml'))
    for key, value in xml['parameters'].items():
        if key not in out['parameters']:
            parameters[key] = value
    task_type = _infer_task_type(parameters)
    inferred_task_type = task_type
    if task_type_override is not None:
        requested = str(task_type_override).strip().lower()
        if requested not in _IMPORT_TASK_TYPES:
            raise ValueError(
                f'任务类型 {requested!r} 不受结果导入支持；'
                f'可选：{", ".join(sorted(_IMPORT_TASK_TYPES))}')
        if inferred_task_type == 'relax' and requested != 'relax':
            raise ValueError(
                f'OUTCAR/vasprun 明确表明原任务为 relax，不能改为 {requested} '
                '来降低离子收敛门；请保持 relax 并完成结构收敛')
        if inferred_task_type == 'freq' and requested != 'freq':
            raise ValueError('输出参数明确为频率任务，不能改为其他类型')
        task_type = requested

    energy = osz['energy_e0_eV'] if _reasonable_energy(osz['energy_e0_eV']) else None
    energy_source = 'OSZICAR:E0' if energy is not None else None
    if (energy is None and _reasonable_energy(out['energy_e0_eV'])
            and out['energy_source'] != 'OUTCAR:TOTEN'):
        energy, energy_source = out['energy_e0_eV'], out['energy_source']
    if energy is None and _reasonable_energy(xml['energy_e0_eV']):
        energy, energy_source = xml['energy_e0_eV'], xml['energy_source']

    comparable_energies = []
    if _reasonable_energy(osz['energy_e0_eV']):
        comparable_energies.append(('OSZICAR:E0', float(osz['energy_e0_eV'])))
    if (_reasonable_energy(out['energy_e0_eV'])
            and out['energy_source'] == 'OUTCAR:energy(sigma->0)'):
        comparable_energies.append((out['energy_source'], float(out['energy_e0_eV'])))
    if _reasonable_energy(xml['energy_e0_eV']):
        comparable_energies.append((xml['energy_source'], float(xml['energy_e0_eV'])))
    energy_spread = (max(value for _source, value in comparable_energies)
                     - min(value for _source, value in comparable_energies)
                     if len(comparable_energies) >= 2 else 0.0)
    energy_consistent = energy_spread <= _ENERGY_CONSISTENCY_TOL_EV

    nelm = _integer(parameters.get('NELM'), 60)
    final_scf = osz['final_scf_steps'] or xml['final_scf_steps']
    nelm_saturated = bool(final_scf and final_scf >= max(nelm, 1))
    incomplete_scf = bool(osz['trailing_incomplete_scf'])
    fatal = out['fatal_error']
    soft_stopped = out['soft_stopped']
    # If OUTCAR exists, its terminal segment decides whether that run finished.
    # A complete but stale XML from an earlier restart must not mask a truncated
    # terminal OUTCAR segment.
    footer = out['normal_footer'] if out['present'] else xml['complete']
    electronic_ok = (out['electronic_converged_marker'] if out['present'] else (
        bool(xml['complete']) and bool(xml['final_scf_steps']) and not nelm_saturated))

    fmax = out['final_force_max_eV_A']
    if not out['present']:
        fmax = xml['final_force_max_eV_A']
    ediffg = _number(parameters.get('EDIFFG'))
    force_gate = bool(ediffg is not None and ediffg < 0 and fmax is not None
                      and fmax <= abs(ediffg) * (1 + 1e-8))
    energy_gate = bool(ediffg is not None and ediffg > 0
                       and osz['final_ionic_dE_eV'] is not None
                       and abs(osz['final_ionic_dE_eV']) <= ediffg * (1 + 1e-8))

    blockers: list[str] = []
    reasons: list[str] = []
    warnings: list[str] = []
    suggestions: list[str] = []
    if energy is None:
        blockers.append('未找到物理合理的最终总能量')
        suggestions.append('请提供含末个 E0 的 OSZICAR，或完整且能量可解析的 vasprun.xml')
        observed = out.get('observed_energy_eV')
        observed_source = out.get('observed_energy_source')
        if observed is None:
            observed = xml.get('observed_energy_eV')
            observed_source = xml.get('observed_energy_source')
        if observed is not None:
            warnings.append(
                f'检测到 {observed_source}={observed:.10g} eV，但它不是 E0，未用于吸附能')
    if not energy_consistent:
        detail = '；'.join(f'{source}={value:.10g} eV'
                          for source, value in comparable_energies)
        blockers.append(
            f'末能量跨文件不一致（差 {energy_spread:.6g} eV > '
            f'{_ENERGY_CONSISTENCY_TOL_EV:g} eV）：{detail}')
        suggestions.append(
            '请确认 OSZICAR、OUTCAR 和 vasprun.xml 来自同一次计算/续算，'
            '移除陈旧文件后重新扫描')
    if fatal:
        blockers.append(f'输出命中致命错误：{fatal}')
        suggestions.append('请先修复计算错误并重新计算，不能靠人工确认提升为 DONE')
    if nelm_saturated:
        blockers.append(f'末个电子步达到 NELM={nelm}，电子自洽不能视为收敛')
        suggestions.append('调整电子收敛参数并续算；NELM 饱和结果不能人工确认为 DONE')
    if incomplete_scf:
        blockers.append('OSZICAR 在末个完整离子步后还有未收尾的电子步，疑似输出被截断')
        suggestions.append('请检查作业是否仍在运行，或提供完整输出')
    if xml['checked'] and not xml['complete']:
        warnings.append('vasprun.xml 不完整或解析失败，未把它作为正常结束证据')
    if xml['e0_anomaly']:
        warnings.append('vasprun.xml 末个 e_0_energy 非物理，已忽略并保留其他能量来源')
    if osz['read_truncated']:
        warnings.append('OSZICAR 很大，仅检查了尾部；离子步计数是尾部可见数量')
    if out['read_truncated']:
        warnings.append('OUTCAR 很大，扫描使用文件头和尾部；中间内容未加载')

    converged = False
    if not blockers and _reasonable_energy(energy):
        if task_type == 'relax':
            if out['ionic_converged_marker']:
                converged = True
                reasons.append('OUTCAR 明确写出结构优化达到要求精度')
            elif footer and electronic_ok and (force_gate or energy_gate):
                converged = True
                reasons.append('正常结束、末电子步收敛，且末结构满足 EDIFFG')
            else:
                suggestions.append(
                    '弛豫结果需结构收敛标志，或“正常结束 + 末电子收敛 + EDIFFG 门槛”联合证据')
        else:  # static/DOS/band/frequency: no ionic accuracy sentence is expected
            if footer and electronic_ok:
                converged = True
                reasons.append('静态/频率类任务正常结束且末电子步未打满 NELM')
            else:
                suggestions.append('请提供完整 OUTCAR 页脚或完整 vasprun.xml，并保留末电子步证据')

    if converged and soft_stopped:
        converged = False
        warnings.append('检测到 STOPCAR/soft stop；即使有能量，也需人工核对停止时机')
    if footer:
        reasons.append('检测到 VASP 正常结束证据')
    if electronic_ok:
        reasons.append(f'末电子自洽未打满 NELM（{final_scf or "未知"}/{nelm}）')
    if task_type == 'relax' and force_gate:
        reasons.append(f'末最大力 {fmax:.6g} eV/Å ≤ |EDIFFG|={abs(ediffg):.6g} eV/Å')
    if energy is not None:
        reasons.append(f'最终能量 {energy:.10g} eV，来源 {energy_source}')

    state = 'DONE' if converged else 'NEEDS_HUMAN'
    terminal_electronic_evidence = bool(
        electronic_ok or (final_scf and final_scf < max(nelm, 1)))
    confirmation_eligible = bool(
        state != 'DONE' and _reasonable_energy(energy) and not blockers
        and footer and terminal_electronic_evidence
        and not fatal and not nelm_saturated and not incomplete_scf
    )
    if state == 'DONE':
        code, summary, action = 'converged_multi_evidence', '多项证据一致：计算已收敛', 'import_done'
    elif blockers:
        code, summary, action = 'hard_gate_failed', '存在不可人工绕过的结果质量问题', 'repair_or_recalculate'
    elif confirmation_eligible:
        code, summary, action = 'ambiguous_completion', '能量可用，但自动证据不足，需人工确认', 'manual_confirm'
    else:
        code, summary, action = 'insufficient_evidence', '无法证明计算已收敛', 'inspect_output'

    method_signature = dict(xml.get('method_signature') or {})
    method_signature.update(out.get('method_signature') or {})
    evidence = {
        'energy': {
            'value_eV': energy, 'source': energy_source,
            'reasonable': _reasonable_energy(energy),
            'observed_non_e0_eV': (out.get('observed_energy_eV')
                                   if out.get('observed_energy_eV') is not None
                                   else xml.get('observed_energy_eV')),
            'observed_non_e0_source': (out.get('observed_energy_source')
                                       or xml.get('observed_energy_source')),
        },
        'cross_file_energy': {
            'values_eV': {source: value for source, value in comparable_energies},
            'spread_eV': energy_spread, 'tolerance_eV': _ENERGY_CONSISTENCY_TOL_EV,
            'consistent': energy_consistent,
        },
        'electronic': {'converged_marker': out['electronic_converged_marker'],
                       'final_scf_steps': final_scf, 'nelm': nelm,
                       'nelm_saturated': nelm_saturated,
                       'trailing_incomplete_scf': incomplete_scf},
        'ionic': {'converged_marker': out['ionic_converged_marker'],
                  'ionic_steps': osz['ionic_steps'], 'nsw': _integer(parameters.get('NSW')),
                  'ediffg': ediffg, 'final_force_max_eV_A': fmax,
                  'force_gate_passed': force_gate, 'energy_gate_passed': energy_gate},
        'completion': {'outcar_footer': out['normal_footer'],
                       'vasprun_checked': xml['checked'], 'vasprun_complete': xml['complete'],
                       'soft_stopped': soft_stopped},
        'fatal_error': fatal,
        'method_signature': method_signature,
    }
    return {
        'task_type': task_type, 'state_suggestion': state, 'energy_e0_eV': energy,
        'energy_source': energy_source,
        'diagnosis': {'code': code, 'summary': summary, 'blockers': blockers,
                      'reasons': reasons, 'suggestions': suggestions},
        'warnings': warnings, 'convergence_evidence': evidence,
        'reference_method_signature': method_signature,
        'confirmation_eligible': confirmation_eligible, 'recommended_action': action,
    }


def _has_output_evidence(files: dict[str, Path]) -> bool:
    return any(name in files for name in _OUTPUT_EVIDENCE_NAMES)


def _created_preview(folder: Path, files: dict[str, Path], task_type_override=None) -> dict:
    """Describe a validated input-only quartet without inventing result truth."""
    parameters = _parse_incar(files.get('INCAR'))
    inferred_task_type = _infer_task_type(parameters)
    task_type = inferred_task_type
    if task_type_override is not None:
        requested = str(task_type_override).strip().lower()
        if requested not in _IMPORT_TASK_TYPES:
            raise ValueError(
                f'任务类型 {requested!r} 不受结果导入支持；'
                f'可选：{", ".join(sorted(_IMPORT_TASK_TYPES))}')
        # Static, DOS and band inputs share a no-ionic-motion signature.  A
        # relax/frequency INCAR is distinguishable and must never be relabelled
        # to weaken the completion rule used after the calculation returns.
        if inferred_task_type == 'relax' and requested != 'relax':
            raise ValueError(
                f'INCAR 明确表明任务为 relax，不能改为 {requested}')
        if inferred_task_type == 'freq' and requested != 'freq':
            raise ValueError('INCAR 明确表明任务为频率任务，不能改为其他类型')
        task_type = requested

    issues = validate_vasp_quartet(folder)
    method_signature = _input_method_signature(files) if not issues else {}
    ready = not issues
    state = 'CREATED' if ready else 'NEEDS_HUMAN'
    diagnosis = {
        'code': 'input_ready' if ready else 'input_gate_failed',
        'summary': ('四件套已通过检查，可直接提交'
                    if ready else '四件套未通过输入检查'),
        'blockers': list(issues),
        'reasons': ['INCAR、POSCAR、KPOINTS 和 POTCAR 已齐全且可解析']
        if ready else [],
        'suggestions': [] if ready else ['按提示修复四件套后重新扫描'],
    }
    evidence = {
        'energy': {
            'value_eV': None, 'source': None, 'reasonable': False,
            'observed_non_e0_eV': None, 'observed_non_e0_source': None,
        },
        'input_quartet': {
            'complete': all(name in files for name in _INPUT_NAMES),
            'valid': ready, 'issues': list(issues),
        },
        'method_signature': method_signature,
    }
    return {
        'task_type': task_type, 'state_suggestion': state,
        'energy_e0_eV': None, 'energy_source': None,
        'diagnosis': diagnosis, 'warnings': [],
        'convergence_evidence': evidence,
        'reference_method_signature': method_signature,
        'confirmation_eligible': False,
        'recommended_action': 'submit_created' if ready else 'repair_inputs',
    }


def _preview(folder: Path, files: dict[str, Path], task_type_override=None) -> dict:
    if _has_output_evidence(files):
        return _state_preview(folder, files, task_type_override=task_type_override)
    return _created_preview(folder, files, task_type_override=task_type_override)


def _candidate(folder: Path, source_root: Path, files: dict[str, Path]) -> dict:
    has_output = _has_output_evidence(files)
    input_issues = validate_vasp_quartet(folder)
    input_complete = all(name in files for name in _INPUT_NAMES) and not input_issues
    preview = _preview(folder, files)
    fingerprint, source_hashes, snapshot_stable = _source_snapshot(folder, files)
    species = _species_from(folder, files)
    role = _suggested_role(folder, species)
    rel = '.' if folder == source_root else folder.relative_to(source_root).as_posix()
    return {
        'path': str(folder.resolve()), 'relative_path': rel, 'name': folder.name,
        'files': sorted(files), 'task_type': preview['task_type'],
        'state_suggestion': preview['state_suggestion'],
        'energy_e0_eV': preview['energy_e0_eV'], 'energy_source': preview['energy_source'],
        'diagnosis': preview['diagnosis'], 'warnings': preview['warnings'],
        'convergence_evidence': preview['convergence_evidence'],
        'reference_method_signature': preview.get('reference_method_signature') or {},
        'confirmation_eligible': preview['confirmation_eligible'],
        'recommended_action': preview['recommended_action'],
        'suggested_role': role, 'role_options': list(_ROLE_OPTIONS), 'species': species,
        'source_has_output': has_output, 'has_output': has_output,
        'input_complete': input_complete, 'input_issues': input_issues,
        'importable': (preview['state_suggestion'] in {'DONE', 'CREATED'}
                       and snapshot_stable),
        'source_fingerprint': fingerprint, 'source_sha256': source_hashes,
        'source_snapshot_stable': snapshot_stable,
    }


def _source_snapshot(folder: Path, files: dict[str, Path]) -> tuple[str, dict[str, str], bool]:
    """Bind a reviewed scan to full content, including large result evidence."""
    record = {'source': str(folder.resolve()), 'files': {}}
    hashes: dict[str, str] = {}
    stable = True
    for name in sorted(files):
        path = files[name]
        try:
            before = path.stat()
            digest = _sha256(path)
            after = path.stat()
        except OSError:
            record['files'][name] = {'missing': True}
            stable = False
            continue
        unchanged = ((before.st_size, before.st_mtime_ns)
                     == (after.st_size, after.st_mtime_ns))
        stable = stable and unchanged
        hashes[name] = digest
        item = {
            'size': after.st_size, 'mtime_ns': after.st_mtime_ns,
            'sha256': digest, 'stable_during_scan': unchanged,
        }
        record['files'][name] = item
    encoded = json.dumps(
        record, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest(), hashes, stable


def _source_fingerprint(folder: Path, files: dict[str, Path]) -> str:
    return _source_snapshot(folder, files)[0]


def scan_folder(source_root) -> dict:
    """Recursively discover local VASP result directories without changing them."""
    root = Path(source_root).expanduser()
    if not root.is_dir():
        raise ValueError(f'导入源目录不存在或不是文件夹：{root}')
    root = root.resolve()
    candidates = []
    skipped_symlink_dirs = 0
    for current, dirnames, _filenames in os.walk(root, followlinks=False):
        base = Path(current)
        safe_dirs = []
        for dirname in sorted(dirnames, key=str.casefold):
            child = base / dirname
            if child.is_symlink():
                skipped_symlink_dirs += 1
            else:
                safe_dirs.append(dirname)
        dirnames[:] = safe_dirs
        files = _files_by_canonical(base)
        if (any(name in files for name in _RESULT_NAMES)
                or all(name in files for name in _INPUT_NAMES)):
            candidates.append(_candidate(base, root, files))
    candidates.sort(key=lambda c: c['relative_path'].casefold())
    counts = {state: sum(c['state_suggestion'] == state for c in candidates)
              for state in ('CREATED', 'DONE', 'NEEDS_HUMAN')}
    return {
        'ok': True, 'source_root': str(root), 'candidates': candidates,
        'summary': {'total': len(candidates), 'created': counts['CREATED'],
                    'done': counts['DONE'],
                    'needs_human': counts['NEEDS_HUMAN'],
                    'confirmation_eligible': sum(c['confirmation_eligible'] for c in candidates),
                    'skipped_symlink_dirs': skipped_symlink_dirs},
        # The UI defaults out_root to source.parent.  Reusing source.name would
        # point the managed destination straight back at the read-only source.
        'suggested_name': f'{root.name}_导入项目' if root.name else 'imported_results',
        'suggested_out_root': str(root.parent),
        'source_read_only': True,
    }


def _safe_stem(value: str, fallback='result') -> str:
    value = re.sub(r'[^\w.-]+', '_', str(value or '').strip(), flags=re.UNICODE).strip('._')
    return (value[:100] or fallback)


def _normalise_selections(selections) -> list[dict]:
    if selections is None:
        return []
    if isinstance(selections, dict) and 'items' in selections:
        selections = selections['items']
    if isinstance(selections, dict):
        items = []
        for path, value in selections.items():
            if isinstance(value, str):
                items.append({'path': path, 'role': value})
            elif isinstance(value, dict):
                if value.get('selected', True):
                    items.append({'path': path, **value})
            else:
                raise ValueError(f'非法导入选择：{path}')
        return items
    if not isinstance(selections, (list, tuple)):
        raise ValueError('selections 必须是列表或按路径索引的字典')
    return [dict(item) for item in selections
            if isinstance(item, dict) and item.get('selected', True)]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _copy_candidate(src: Path, dst: Path) -> tuple[list[str], dict[str, str]]:
    files = _files_by_canonical(src)
    copied, hashes = [], {}
    dst.mkdir(parents=True, exist_ok=False)
    for canonical in _COPY_NAMES:
        source = files.get(canonical)
        if source is None:
            continue
        before = source.stat()
        target = dst / canonical
        shutil.copy2(source, target)
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError(f'导入期间源文件发生变化，请重新扫描：{source}')
        copied.append(canonical)
        hashes[canonical] = _sha256(target)
    return copied, hashes


def _unique_destination(parent: Path, stem: str, used: set[Path]) -> Path:
    candidate = parent / _safe_stem(stem)
    n = 2
    while candidate in used:
        candidate = parent / f'{_safe_stem(stem)}_{n}'
        n += 1
    used.add(candidate)
    return candidate


def commit_import(source_root, out_root, project_name, selections, *,
                  adsorption_mod=None, ledger_mod=None, manifest_mod=None) -> dict:
    """Copy selected results into ``out_root/project_name`` and register them.

    Each selection is ``{'path', 'role', 'species?', 'manual_confirm?'}``.  It
    must refer to a candidate returned by a fresh scan.  Manual confirmation is
    rejected for NELM saturation, fatal output, incomplete trailing SCF, or an
    implausible/missing energy, and the override is recorded in ``job.yaml``.
    """
    if adsorption_mod is None:
        from vcstudio.project import adsorption as adsorption_mod
    if ledger_mod is None:
        from vcstudio.cluster import ledger as ledger_mod
    if manifest_mod is None:
        from vcstudio.shared import manifest as manifest_mod

    source = Path(source_root).expanduser().resolve()
    scan = scan_folder(source)
    by_path = {str(Path(c['path']).resolve()): c for c in scan['candidates']}
    chosen = _normalise_selections(selections)
    if not chosen:
        raise ValueError('至少选择一个结果目录')
    project_stem = _safe_stem(project_name, 'imported_results')
    final_root = Path(out_root).expanduser().resolve() / project_stem
    try:
        final_root.relative_to(source)
    except ValueError:
        pass
    else:
        raise ValueError('导入目标不能位于源目录内部；源结果必须保持只读')
    if final_root.exists():
        raise FileExistsError(f'目标项目已存在，为避免覆盖请更换名称：{final_root}')
    final_root.parent.mkdir(parents=True, exist_ok=True)
    stage = final_root.parent / f'.{project_stem}.import-{uuid.uuid4().hex}'
    stage.mkdir(parents=False, exist_ok=False)

    seen_roles = {'clean_slab': 0, 'gas_ref': 0}
    used_destinations: set[Path] = set()
    imported = []
    final_members = {'clean_slab': None, 'gas_ref': None, 'configs': []}
    molecule_jobs: dict[str, str] = {}
    molecule_energies: dict[str, float | None] = {}
    config_species: dict[str, str] = {}
    standalone: list[str] = []
    seen_sources: set[str] = set()
    renamed = False
    registered_jobs: list[str] = []
    project_registered = False
    project_path = final_root / 'project.yaml'
    try:
        for item in chosen:
            raw_path = item.get('path')
            if not raw_path:
                raise ValueError('每个导入选择都必须包含 path')
            path = str(Path(raw_path).expanduser().resolve())
            candidate = by_path.get(path)
            if candidate is None:
                raise ValueError(f'选择不在本次扫描候选中：{raw_path}')
            expected_fingerprint = str(item.get('source_fingerprint') or '').strip()
            if (expected_fingerprint
                    and expected_fingerprint != candidate.get('source_fingerprint')):
                raise ValueError(
                    f'{candidate["name"]} 在扫描确认后发生变化；为避免混用不同轮结果，'
                    '请重新检查文件夹后再导入')
            if not candidate.get('source_snapshot_stable', False):
                raise ValueError(
                    f'{candidate["name"]} 在扫描期间仍在变化；'
                    '请等文件写入完成后重新扫描')
            if path in seen_sources:
                raise ValueError(f'同一结果目录不能重复导入为多个角色：{raw_path}')
            seen_sources.add(path)
            role = str(item.get('role') or candidate['suggested_role'])
            if role not in _ROLE_OPTIONS:
                raise ValueError(f'未知角色 {role!r}')
            if role == 'ignore':
                continue
            if role in seen_roles:
                seen_roles[role] += 1
                if seen_roles[role] > 1:
                    raise ValueError(f'一个项目只能有一个 {role}')

            task_type = str(item.get('task_type') or candidate['task_type']).strip().lower()
            if task_type not in _IMPORT_TASK_TYPES:
                raise ValueError(
                    f'{candidate["name"]} 的任务类型 {task_type!r} 不受结果导入支持；'
                    f'可选：{", ".join(sorted(_IMPORT_TASK_TYPES))}')
            if task_type != candidate['task_type']:
                # A task override changes which convergence evidence is required.
                # Re-evaluate from the freshly scanned source instead of retaining
                # a DONE decision made under the original inferred task type.
                candidate = {
                    **candidate,
                    **_preview(
                        Path(path), _files_by_canonical(Path(path)),
                        task_type_override=task_type),
                }
            species = str(item.get('species') or candidate.get('species') or '').strip() or None
            if role == 'clean_slab':
                rel_dst = Path('clean_slab')
            elif role == 'gas_ref':
                rel_dst = Path('gas_ref')
            elif role == 'config':
                rel_dst = _unique_destination(Path('configs'), species or candidate['name'],
                                               used_destinations)
            elif role == 'molecule_ref':
                if not species:
                    raise ValueError(f'{candidate["name"]} 作为 molecule_ref 时必须指定 species')
                rel_dst = _unique_destination(Path('molecules'), f'mol_{species}', used_destinations)
            else:
                rel_dst = _unique_destination(Path('standalone'), candidate['name'], used_destinations)

            stage_dst = stage / rel_dst
            copied, hashes = _copy_candidate(Path(path), stage_dst)
            expected_hashes = candidate.get('source_sha256') or {}
            staged_hashes = {name: _sha256(stage_dst / name) for name in copied}
            if (set(copied) != set(expected_hashes)
                    or hashes != staged_hashes
                    or any(staged_hashes.get(name) != expected_hashes.get(name)
                           for name in copied)):
                raise ValueError(
                    f'{candidate["name"]} 在导入期间源文件内容发生变化；'
                    '为避免混用不同轮计算，请重新扫描后再导入')

            # The staged copy is the delivered truth.  Re-run all scientific
            # gates there so a scan→copy race cannot leave a manifest describing
            # evidence that was not actually imported.
            staged_files = _files_by_canonical(stage_dst)
            candidate = {
                **candidate,
                **_preview(stage_dst, staged_files, task_type_override=task_type),
            }
            input_issues = validate_vasp_quartet(stage_dst)
            input_complete = (all(name in staged_files for name in _INPUT_NAMES)
                              and not input_issues)
            candidate.update({
                'source_has_output': _has_output_evidence(staged_files),
                'has_output': _has_output_evidence(staged_files),
                'input_complete': input_complete,
                'input_issues': input_issues,
            })

            if (role == 'molecule_ref'
                    and candidate['state_suggestion'] not in {'DONE', 'CREATED'}):
                raise ValueError(
                    f'{candidate["name"]} 尚未被自动判定为 DONE，不能写入分子参考库；'
                    '请先补齐收敛证据，或改选 standalone 保留待核数据')

            manual = (bool(item.get('manual_confirm'))
                      and candidate['state_suggestion'] != 'DONE')
            if manual and not candidate['confirmation_eligible']:
                raise ValueError(
                    f'{candidate["name"]} 含不可绕过的质量问题，不能人工确认为 DONE：'
                    + '；'.join(candidate['diagnosis']['blockers']))
            confirmation_reason = str(item.get('confirmation_reason') or '').strip()
            if manual and not confirmation_reason:
                raise ValueError(f'{candidate["name"]} 人工确认时必须填写核对理由')
            if candidate['state_suggestion'] == 'CREATED':
                state = 'CREATED'
            elif candidate['state_suggestion'] == 'DONE' or manual:
                state = 'DONE'
            else:
                state = 'NEEDS_HUMAN'

            if role == 'molecule_ref':
                actual = _structure_composition(staged_files)
                declared = _formula_composition(species or '')
                if actual is None:
                    raise ValueError(
                        f'{candidate["name"]} 缺少可解析的 CONTCAR/POSCAR 元素计数，'
                        '不能安全作为分子参考')
                if declared is None or declared != actual:
                    actual_formula = ''.join(
                        element + (str(count) if count != 1 else '')
                        for element, count in actual.items())
                    raise ValueError(
                        f'参考物种 {species or "(空)"} 与结构组成 {actual_formula} '
                        '不一致；文件夹名或人工标签不能覆盖真实原子计数')

            final_dst = final_root / rel_dst
            m = manifest_mod.new_manifest(
                job_id=f'import-{project_stem}-{_safe_stem(candidate["name"])}-{int(time.time())}',
                system=candidate['name'], task_type=task_type,
                calc_type='molecule' if role in {'gas_ref', 'molecule_ref'} else 'slab',
                inputs={'imported_from': path, 'imported_files': copied,
                        'source_sha256': staged_hashes,
                        'source_fingerprint': candidate.get('source_fingerprint'),
                        'import_role': role, 'species': species,
                        'input_complete': input_complete,
                        'input_issues': input_issues},
                warnings=candidate['warnings'],
            )
            if state != 'CREATED':
                if manual:
                    note = '用户人工确认：自动证据不足，但已通过不可绕过的质量门槛'
                elif state == 'DONE':
                    note = '本地结果多证据自动判定收敛'
                else:
                    note = '本地结果证据不足，保留为待人工处理'
                manifest_mod.set_state(m, state, note=note)
            m['inputs']['reference_method_signature'] = (
                candidate.get('reference_method_signature') or {})
            if state != 'CREATED':
                m.setdefault('results', {}).update({
                    'energy_e0_eV': candidate['energy_e0_eV'],
                    'energy_source': candidate['energy_source'],
                    'diagnosis': candidate['diagnosis'],
                    'convergence_evidence': candidate['convergence_evidence'],
                    'reference_method_signature': candidate.get('reference_method_signature') or {},
                    'import_confirmation': {
                        'manual': manual, 'eligible': candidate['confirmation_eligible'],
                        'confirmed_at': _now_iso() if manual else None,
                        'reason': confirmation_reason or None,
                    },
                })
            manifest_mod.save_manifest(stage_dst, m)

            final_str = str(final_dst)
            if role == 'clean_slab':
                final_members['clean_slab'] = final_str
            elif role == 'gas_ref':
                final_members['gas_ref'] = final_str
            elif role == 'config':
                final_members['configs'].append(final_str)
                if species:
                    config_species[final_str] = species
            elif role == 'molecule_ref':
                if species in molecule_jobs:
                    raise ValueError(f'分子参考 species 重复：{species}')
                molecule_jobs[species] = final_str
                if state == 'DONE':
                    molecule_energies[species] = float(candidate['energy_e0_eV'])
                else:
                    molecule_energies[species] = None
            else:
                standalone.append(final_str)
            imported.append({'source': path, 'path': final_str, 'role': role,
                             'species': species, 'state': state,
                             'manual_confirmed': manual})

        if not imported:
            raise ValueError('所有候选都被忽略，没有可导入内容')
        project_warnings = []
        if not final_members['clean_slab']:
            project_warnings.append(
                '尚未导入 clean_slab：结果已安全保存，但在补齐清洁表面前不能计算吸附能 ΔE。')
        if not final_members['configs']:
            project_warnings.append(
                '尚未导入 config：可继续向该项目补充吸附构型；当前不会生成吸附能数据点。')
        project = {
            'schema': 1, 'name': str(project_name).strip() or project_stem,
            'created_at': _now_iso(), 'root': str(final_root),
            'import_source': str(source), 'members': final_members,
            'config_species': config_species,
            'species_refs': molecule_energies, 'species_ref_jobs': molecule_jobs,
            'molecules_dir': str(final_root / 'molecules') if molecule_jobs else None,
            'standalone': standalone, 'warnings': project_warnings,
        }
        adsorption_mod.save_project(stage, project)
        os.replace(stage, final_root)
        renamed = True
        for row in imported:
            if ledger_mod.register(row['path']):
                registered_jobs.append(row['path'])
        project_registered = bool(adsorption_mod.register_project(project_path))
        return {'ok': True, 'project_path': str(project_path),
                'project_name': project['name'], 'project': project,
                'warnings': project_warnings, 'imported': imported, 'summary': {
                    'total': len(imported), 'done': sum(r['state'] == 'DONE' for r in imported),
                    'created': sum(r['state'] == 'CREATED' for r in imported),
                    'needs_human': sum(r['state'] == 'NEEDS_HUMAN' for r in imported),
                    'manual_confirmed': sum(r['manual_confirmed'] for r in imported),
                }}
    except Exception as exc:
        # rename 是交付边界；其后任意一步失败都必须撤销已完成的
        # 台账/项目登记，并删除刚搬到最终位置的半成品。源目录始终不动。
        rollback_errors = []
        if project_registered:
            unregister_project = getattr(adsorption_mod, 'unregister_project', None)
            if callable(unregister_project):
                try:
                    unregister_project(project_path)
                except Exception as rollback_exc:       # noqa: BLE001
                    rollback_errors.append(f'项目注册回滚失败:{rollback_exc}')
            else:
                rollback_errors.append('项目注册回滚失败:注入模块无 unregister_project')
        unregister_job = getattr(ledger_mod, 'unregister', None)
        for job_path in reversed(registered_jobs):
            if not callable(unregister_job):
                rollback_errors.append('作业台账回滚失败:注入模块无 unregister')
                break
            try:
                unregister_job(job_path)
            except Exception as rollback_exc:           # noqa: BLE001
                rollback_errors.append(f'作业台账回滚失败({job_path}):{rollback_exc}')
        cleanup_target = final_root if renamed else stage
        if cleanup_target.exists():
            try:
                shutil.rmtree(cleanup_target)
            except OSError as rollback_exc:
                rollback_errors.append(f'半成品目录清理失败:{rollback_exc}')
        if rollback_errors:
            raise RuntimeError(f'{exc}; ' + '；'.join(rollback_errors)) from exc
        raise


__all__ = ['scan_folder', 'commit_import', 'validate_vasp_quartet']
