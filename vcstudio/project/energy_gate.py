"""Scientific gates for total-energy subtraction workflows.

Formation/binding and surface energies subtract large VASP total energies.  A
parseable last ``E0`` is not enough: every operand must be a managed DONE job
with clean completion evidence, and known electronic-method conflicts must
stop the calculation.  Missing provenance remains explicit ``unverified``
evidence so old imported results are never silently presented as comparable.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
import hashlib
import math
import os
import re


_CLEAN_RE = re.compile(
    r'General\s+timing\s+and\s+accounting\s+information(?:s)?\s+for\s+this\s+job'
    r'|Total\s+CPU\s+time\s+used|Voluntary\s+context\s+switches', re.I)
_FINITE_NUMBER = r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?'
_OSZICAR_STEP_RE = re.compile(r'^\s*(\d+)\s+F=', re.I)
_OSZICAR_E0_RE = re.compile(rf'\bE0\s*=\s*({_FINITE_NUMBER})', re.I)
_OUTCAR_SIGMA0_RE = re.compile(
    rf'energy\(sigma->0\)\s*=\s*({_FINITE_NUMBER})', re.I)


def _read_named(job_dir, name):
    path = os.path.join(str(job_dir), name)
    try:
        with open(path, encoding='utf-8', errors='replace') as handle:
            return handle.read()
    except OSError:
        return ''


def _read_structure(job_dir):
    return _read_named(job_dir, 'CONTCAR') or _read_named(job_dir, 'POSCAR')


def _expanded_numbers(value, *, integer=False):
    """Expand VASP vectors such as ``2*-1 3*0`` without guessing bad input."""
    if isinstance(value, (list, tuple)):
        tokens = list(value)
    elif value is None or isinstance(value, bool):
        return None
    else:
        tokens = str(value).replace(',', ' ').split()
    result = []
    for token in tokens:
        match = re.fullmatch(r'(\d+)\*([^*]+)', str(token).strip())
        count, raw = (int(match.group(1)), match.group(2)) if match else (1, token)
        try:
            number = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or (integer and number != int(number)):
            return None
        result.extend([int(number) if integer else number] * count)
    return result or None


def _element_u_map(incar_text, signature, provenance):
    """Return effective Hubbard settings by element, or ``None`` if ambiguous.

    Raw LDAU vectors from molecule and slab naturally have different lengths and
    orders.  Energy comparison must only compare elements shared by both jobs.
    """
    from vcstudio.generate.incar_builder import parse_incar

    incar = parse_incar(incar_text or '')
    elements = [str(row.get('element') or '') for row in (provenance or [])]
    if not elements or any(not element or element.startswith('#') for element in elements):
        elements = list(signature.get('potcar_elements') or [])
    if not elements or len(set(elements)) != len(elements):
        return None

    plan = {
        'ldau': (signature.get('ldau') if 'ldau' in signature
                 else incar.get('LDAU', False)),
        'ldautype': (signature.get('ldautype') if 'ldautype' in signature
                     else incar.get('LDAUTYPE')),
        'ldaul': (signature.get('ldaul') if 'ldaul' in signature
                  else incar.get('LDAUL')),
        'ldauu': (signature.get('ldauu') if 'ldauu' in signature
                  else incar.get('LDAUU')),
        'ldauj': (signature.get('ldauj') if 'ldauj' in signature
                  else incar.get('LDAUJ')),
        'element_orders': [elements],
        'potcar_titel': [row.get('titel') for row in (provenance or [])],
    }
    from vcstudio.project.method_policy import effective_u_by_element
    return effective_u_by_element(plan)


def parse_oszicar_energy_text(text):
    """Return the last finite ``E0`` from one already captured OSZICAR."""
    energy = None
    for line in str(text or '').splitlines():
        match = re.search(
            r'\bE0\s*=\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)',
            line, re.I)
        if match:
            try:
                candidate = float(match.group(1))
            except ValueError:
                continue
            if math.isfinite(candidate):
                energy = candidate
    return energy


def parse_oszicar_energy_events(text):
    """Return finite ``(ionic_step, E0)`` events from one OSZICAR snapshot."""
    events = []
    for line in str(text or '').splitlines():
        step_match = _OSZICAR_STEP_RE.match(line)
        energy_match = _OSZICAR_E0_RE.search(line)
        if not step_match or not energy_match:
            continue
        try:
            step = int(step_match.group(1))
            energy = float(energy_match.group(1))
        except (TypeError, ValueError):
            continue
        if step > 0 and math.isfinite(energy):
            events.append((step, energy))
    return events


def parse_outcar_energy_events(text):
    """Return finite OUTCAR ``energy(sigma->0)`` events in file order."""
    events = []
    for match in _OUTCAR_SIGMA0_RE.finditer(str(text or '')):
        try:
            energy = float(match.group(1))
        except (TypeError, ValueError):
            continue
        if math.isfinite(energy):
            events.append(energy)
    return events


def parse_oszicar_energy(job_dir):
    """Return the last finite OSZICAR E0, or ``None``."""
    return parse_oszicar_energy_text(_read_named(job_dir, 'OSZICAR'))


def clean_completion_from_text(outcar_text='', vasprun_text=''):
    """Return clean-completion evidence from one immutable text snapshot."""
    outcar_tail = str(outcar_text or '')[-2 * 1024 * 1024:]
    if _CLEAN_RE.search(outcar_tail):
        return True, 'OUTCAR timing 页脚'
    vasprun_tail = str(vasprun_text or '')[-128 * 1024:]
    if '</modeling>' in vasprun_tail:
        return True, 'vasprun.xml 完整闭合'
    return False, ''


def clean_completion_from_files(job_dir):
    """Read bounded file tails and return ``(clean, evidence_label)``."""
    try:
        outcar = os.path.join(str(job_dir), 'OUTCAR')
        if os.path.isfile(outcar):
            with open(outcar, 'rb') as handle:
                handle.seek(max(os.path.getsize(outcar) - 2 * 1024 * 1024, 0))
                tail = handle.read().decode('utf-8', errors='replace')
            clean, evidence = clean_completion_from_text(outcar_text=tail)
            if clean:
                return clean, evidence
        vasprun = os.path.join(str(job_dir), 'vasprun.xml')
        if os.path.isfile(vasprun):
            with open(vasprun, 'rb') as handle:
                handle.seek(max(os.path.getsize(vasprun) - 128 * 1024, 0))
                tail = handle.read().decode('utf-8', errors='replace')
            clean, evidence = clean_completion_from_text(vasprun_text=tail)
            if clean:
                return clean, evidence
    except OSError:
        pass
    return False, ''


def validate_done_completion_evidence(
        manifest, label, *, outcar_text='', vasprun_text='',
        require_current_completion=False, require_explicit_diagnosis=False,
        reject_explicit_unclean=False):
    """Validate DONE state, diagnosis, exit code, and completion evidence."""
    if not isinstance(manifest, Mapping):
        raise ValueError(
            f'{label}缺少可读 job.yaml；请先通过「导入已算结果」建立可追溯作业')
    state = str(manifest.get('state') or '')
    if state != 'DONE':
        raise ValueError(
            f'{label}未通过 DONE 门（当前 {state or "未知"}），拒绝使用未收敛能量')
    results = manifest.get('results') or {}
    if not isinstance(results, Mapping):
        raise ValueError(f'{label}的 job.yaml results 证据格式无效')
    diagnosis = results.get('diagnosis') or {}
    if not isinstance(diagnosis, Mapping):
        raise ValueError(f'{label}的 job.yaml diagnosis 证据格式无效')
    failure_class = diagnosis.get('failure_class')
    if require_explicit_diagnosis and failure_class != 'CONVERGED':
        raise ValueError(
            f'{label}缺少显式 CONVERGED 诊断或诊断为 {failure_class or "未知"}')
    if not require_explicit_diagnosis and failure_class and failure_class != 'CONVERGED':
        raise ValueError(f'{label}诊断为 {failure_class}，与 DONE 矛盾，拒绝计算')
    exit_code = diagnosis.get('exit_code')
    if require_explicit_diagnosis and exit_code is None:
        raise ValueError(f'{label}缺少显式零退出码证据')
    try:
        nonzero_exit = exit_code is not None and int(exit_code) != 0
    except (TypeError, ValueError):
        nonzero_exit = True
    if nonzero_exit:
        raise ValueError(f'{label}记录非零退出码 {exit_code}，不得作为已完成能量')
    if reject_explicit_unclean and diagnosis.get('clean_exit') is False:
        raise ValueError(f'{label}显式记录 clean_exit=false，与 DONE 矛盾')

    current_clean, current_source = clean_completion_from_text(
        outcar_text=outcar_text, vasprun_text=vasprun_text)
    convergence_evidence = results.get('convergence_evidence') or {}
    completion = (
        convergence_evidence.get('completion') or {}
        if isinstance(convergence_evidence, Mapping) else {}
    )
    if not isinstance(completion, Mapping):
        completion = {}
    cached_clean = (diagnosis.get('clean_exit') is True
                    or completion.get('outcar_footer') is True
                    or completion.get('vasprun_complete') is True)
    if require_current_completion:
        clean, clean_source = current_clean, current_source
    else:
        clean = bool(cached_clean or current_clean)
        clean_source = ('job.yaml 完整性证据' if cached_clean else current_source)
    if not clean:
        raise ValueError(
            f'{label}无当前 OUTCAR timing 页脚/完整 vasprun.xml；'
            '旧收敛串不能证明完整完成')
    return manifest, clean_source, [f'{label}完成证据：{clean_source}']


def validate_done_energy_evidence(
        manifest, label, *, oszicar_text='', outcar_text='', vasprun_text='',
        require_oszicar=False, require_current_completion=False,
        require_outcar_ionic_event=False, reject_explicit_unclean=False):
    """Validate DONE energy against already captured authoritative bytes.

    ``require_current_completion`` deliberately ignores cached completion flags:
    callers at an immutable analysis boundary must see an OUTCAR timing footer or
    a closed vasprun.xml in the same byte snapshot used for energy parsing.
    """
    manifest, clean_source, evidence = validate_done_completion_evidence(
        manifest, label, outcar_text=outcar_text, vasprun_text=vasprun_text,
        require_current_completion=require_current_completion,
        reject_explicit_unclean=reject_explicit_unclean,
    )
    results = manifest.get('results') or {}
    if not isinstance(results, Mapping):
        raise ValueError(f'{label}的 job.yaml results 证据格式无效')

    oszicar_events = parse_oszicar_energy_events(oszicar_text)
    outcar_events = parse_outcar_energy_events(outcar_text)
    if require_outcar_ionic_event:
        if not oszicar_events:
            raise ValueError(f'{label}缺少可解析的 OSZICAR 最终离子能量事件')
        oszicar_steps = [step for step, _energy in oszicar_events]
        expected_steps = list(range(1, len(oszicar_events) + 1))
        if oszicar_steps != expected_steps:
            raise ValueError(
                f'{label}的 OSZICAR 离子事件序列/终态不连续：'
                f'{oszicar_steps!r}，期望 {expected_steps!r}')
        if not outcar_events:
            raise ValueError(
                f'{label}缺少当前 OUTCAR:energy(sigma->0) 最终离子能量事件')
        if len(outcar_events) != len(oszicar_events):
            raise ValueError(
                f'{label}的 OUTCAR/OSZICAR 离子能量事件计数不一致：'
                f'{len(outcar_events)} != {len(oszicar_events)}')
        if clean_source == 'OUTCAR timing 页脚':
            last_energy = list(_OUTCAR_SIGMA0_RE.finditer(str(outcar_text or '')))[-1]
            completion_events = list(_CLEAN_RE.finditer(str(outcar_text or '')))
            if (not completion_events
                    or completion_events[-1].start() <= last_energy.end()):
                raise ValueError(
                    f'{label}的 OUTCAR timing 页脚不在最终离子能量事件之后')

    try:
        energy = float(results.get('energy_e0_eV'))
    except (TypeError, ValueError):
        raise ValueError(f'{label}的 DONE job.yaml 未记录可用 energy_e0_eV') from None
    if not math.isfinite(energy):
        raise ValueError(f'{label}的 energy_e0_eV 非有限数')
    parsed = (
        oszicar_events[-1][1]
        if require_outcar_ionic_event and oszicar_events
        else parse_oszicar_energy_text(oszicar_text)
    )
    if require_oszicar and parsed is None:
        raise ValueError(
            f'{label}缺少可解析的当前轮 OSZICAR:E0；拒绝仅凭 job.yaml 缓存能量计算')
    if (not require_outcar_ionic_event and parsed is not None
            and abs(parsed - energy) > 1e-3):
        raise ValueError(
            f'{label}的 job.yaml 能量 {energy:.8f} eV 与 OSZICAR '
            f'{parsed:.8f} eV 不一致，疑混入不同轮结果')
    mismatches = []
    if (require_outcar_ionic_event and parsed is not None
            and abs(parsed - energy) > 1e-3):
        mismatches.append(
            f'job.yaml={energy:.8f} eV 与 OSZICAR={parsed:.8f} eV')
    outcar_energy = outcar_events[-1] if outcar_events else None
    if require_outcar_ionic_event and outcar_energy is not None:
        if parsed is not None and abs(outcar_energy - parsed) > 1e-3:
            mismatches.append(
                f'OUTCAR={outcar_energy:.8f} eV 与 OSZICAR={parsed:.8f} eV')
        if abs(outcar_energy - energy) > 1e-3:
            mismatches.append(
                f'OUTCAR={outcar_energy:.8f} eV 与 job.yaml={energy:.8f} eV')
    if mismatches:
        raise ValueError(
            f'{label}的最终离子事件能量不一致，疑混入不同轮结果：'
            + '；'.join(mismatches))
    if parsed is not None:
        evidence.append(f'{label}能量证据：OSZICAR:E0={parsed:.8f} eV')
    if require_outcar_ionic_event and outcar_energy is not None:
        evidence.append(
            f'{label}能量证据：OUTCAR:energy(sigma->0)={outcar_energy:.8f} eV')
    return energy, manifest, evidence


def validate_done_energy(job_dir, label, manifest_mod, *, require_oszicar=False):
    """Return ``(energy, manifest, evidence_messages)`` after hard validation.

    ``require_oszicar`` is used by final total-energy subtraction workflows.
    A manifest value alone is then insufficient: the downloaded/current
    OSZICAR must contain the same final ``E0``.  Other callers retain the
    legacy compatibility path because some imported calculations only carry a
    complete vasprun.xml plus an audited manifest energy.
    """
    manifest = manifest_mod.load_manifest(job_dir)
    return validate_done_energy_evidence(
        manifest, label,
        oszicar_text=_read_named(job_dir, 'OSZICAR'),
        outcar_text=_read_named(job_dir, 'OUTCAR'),
        vasprun_text=_read_named(job_dir, 'vasprun.xml'),
        require_oszicar=require_oszicar,
    )


def method_record(job_dir, manifest, label, *, source_snapshot=None):
    """Extract a comparable method record from one immutable file snapshot.

    ``source_snapshot`` is optional for legacy callers. Analysis Workbench
    callers provide it so method parsing and evidence hashes consume identical
    bytes instead of reopening mutable files.
    """
    from vcstudio.campaign import fingerprint as fingerprint_mod

    def _text(name):
        return (source_snapshot.text(name) if source_snapshot is not None
                else _read_named(job_dir, name))

    incar = _text('INCAR')
    kpoints = _text('KPOINTS')
    potcar_text = _text('POTCAR')
    inputs = (manifest or {}).get('inputs') or {}
    results = (manifest or {}).get('results') or {}
    expected_hashes = inputs.get('sha256') or inputs.get('source_sha256') or {}

    def _current_hash(name):
        if source_snapshot is not None:
            item = source_snapshot.file(name)
            return str((item or {}).get('sha256') or '')
        path = os.path.join(str(job_dir), name)
        try:
            with open(path, 'rb') as handle:
                return hashlib.sha256(handle.read()).hexdigest()
        except OSError:
            return ''

    drifted_inputs = {
        name for name in ('INCAR', 'KPOINTS', 'POTCAR')
        if re.fullmatch(r'[0-9a-fA-F]{64}', str(expected_hashes.get(name) or ''))
        and _current_hash(name).lower() != str(expected_hashes[name]).lower()
    }
    signature = dict(results.get('reference_method_signature')
                     or inputs.get('reference_method_signature') or {})
    provenance = list(inputs.get('potcar_provenance') or inputs.get('potcar') or [])
    manifest_has_potcar_provenance = bool(provenance)

    # OUTCAR/vasprun describe what was actually run.  Prefer that recorded
    # identity over a quartet that may have been edited after the calculation.
    signature_titels = list(signature.get('potcar_titel') or [])
    if signature_titels:
        signature_elements = list(signature.get('potcar_elements') or [])
        provenance = [
            {'element': (signature_elements[index]
                         if index < len(signature_elements) else f'#{index + 1}'),
             'titel': titel}
            for index, titel in enumerate(signature_titels)
        ]

    if not provenance:
        titels = [match.group(1).strip() for match in re.finditer(
            r'^\s*TITEL\s*=\s*(.+?)\s*$', potcar_text, re.I | re.M)]
        elements = []
        structure = (_text('CONTCAR') or _text('POSCAR'))
        if structure:
            try:
                from vcstudio.generate.poscar import parse_poscar_species
                elements = list(parse_poscar_species(structure)[0] or [])
            except Exception:                             # noqa: BLE001
                elements = []
        if not titels:
            titels = list(signature.get('potcar_titel') or [])
            elements = list(signature.get('potcar_elements') or elements)
        provenance = [
            {'element': elements[index] if index < len(elements) else f'#{index + 1}',
             'titel': titel}
            for index, titel in enumerate(titels)
        ]

    fp = fingerprint_mod.extract_from_inputs(incar, kpoints, provenance)
    if fp.get('encut') is None and potcar_text:
        enmax_values = []
        for match in re.finditer(
                r'\bENMAX\s*=\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)',
                potcar_text, re.I):
            try:
                value = float(match.group(1))
            except ValueError:
                continue
            if math.isfinite(value) and value > 0:
                enmax_values.append(value)
        if enmax_values:
            # VASP's effective default ENCUT is the largest ENMAX in POTCAR.
            fp['encut'] = max(enmax_values)
    u_by_element = _element_u_map(incar, signature, provenance)
    potcar_ids = fp.get('potcar_ids') or {}
    potcar_identity_complete = bool(potcar_ids) and all(
        str(element or '').strip() and str(identity or '').strip()
        for element, identity in potcar_ids.items())
    known = {
        'functional': bool(incar), 'dispersion': bool(incar),
        'encut': bool(fp.get('encut') is not None),
        'spin': bool(incar), 'u_values': bool(incar),
        'u_by_element': u_by_element is not None,
        'kpoints_scheme': bool(fp.get('kpoints_scheme')),
        # A legacy/partial provenance map such as ``{'C': None}`` is missing
        # evidence, not a known pseudopotential identity.  Treating it as known
        # invents a hard conflict against a fully identified reference job.
        'potcar_ids': potcar_identity_complete,
    }
    if incar:
        # 标准 VASP + PAW_PBE 下，未显式写 GGA 与 GGA=PE 同为 PBE，
        # 不应因「是否显式写出默认值」产生伪冲突。
        from vcstudio.generate.incar_builder import parse_incar
        incar_values = parse_incar(incar)
        special_functional_requested = bool(
            any(key in incar_values for key in ('AEXX', 'HFSCREEN'))
            or str(incar_values.get('LHFCALC', 'F')).strip().strip('.').upper()
            not in {'F', 'FALSE', '0'}
            or str(incar_values.get('METAGGA', 'F')).strip().strip('.').upper()
            not in {'F', 'FALSE', 'NONE', '--', '0'})
        if fp.get('functional') is None and special_functional_requested:
            known['functional'] = False
        else:
            fp['functional'] = fp.get('functional') or 'PBE'
        fp['dispersion'] = fp.get('dispersion') or 'none'
        fp['u_values'] = fp.get('u_values') or {'LDAU': False}
    # Apply every trustworthy output signature field even when input files are
    # present: current INCAR is only a fallback and must not rewrite history.
    raw_signature_hybrid = signature.get('lhfcalc')
    signature_hybrid_token = str(raw_signature_hybrid or '').strip().strip('.').upper()
    signature_hybrid_value = (
        True if signature_hybrid_token in {'T', 'TRUE', '1'} else
        False if signature_hybrid_token in {'F', 'FALSE', '0'} else
        None)
    signature_hybrid = signature_hybrid_value is True
    signature_method_present = any(
        key in signature
        for key in ('functional', 'base_functional', 'gga', 'metagga', 'lhfcalc'))
    if signature_method_present:
        signature_base = (signature.get('base_functional')
                          or signature.get('gga') or signature.get('functional'))
        canonical_functional = (
            None if ('lhfcalc' in signature and signature_hybrid_value is None)
            else fingerprint_mod.canonical_functional(
                base=signature_base, metagga=signature.get('metagga'),
                lhfcalc=(signature_hybrid if 'lhfcalc' in signature else None),
                aexx=signature.get('aexx'),
                hfscreen=signature.get('hfscreen')))
        if canonical_functional is None:
            known['functional'] = False
        else:
            fp['functional'] = canonical_functional
            known['functional'] = True
    if 'ivdw' in signature:
        probe = fingerprint_mod.extract_from_inputs(
            f'IVDW={signature.get("ivdw")}', '', [])
        fp['dispersion'] = probe.get('dispersion') or 'none'
        known['dispersion'] = True
    if signature.get('encut') is not None:
        try:
            fp['encut'] = float(signature['encut'])
            known['encut'] = True
        except (TypeError, ValueError):
            pass
    if signature.get('ispin') is not None:
        try:
            spin = float(signature['ispin'])
        except (TypeError, ValueError):
            known['spin'] = False
        else:
            if math.isfinite(spin):
                fp['spin'] = int(spin) if spin == int(spin) else spin
                known['spin'] = True
            else:
                known['spin'] = False
    if 'ldau' in signature:
        enabled = str(signature.get('ldau')).strip().strip('.').upper() in {
            'T', 'TRUE', '1'}
        fp['u_values'] = ({'LDAU': False} if not enabled else {
            'LDAU': True, **{
                key.upper(): signature[key]
                for key in ('ldautype', 'ldaul', 'ldauu', 'ldauj')
                if key in signature}})
        known['u_values'] = True
    if not known['kpoints_scheme'] and inputs.get('kpoints'):
        fp['kpoints_scheme'] = 'mesh ' + 'x'.join(str(value) for value in inputs['kpoints'])
        known['kpoints_scheme'] = True

    evidence_warnings = []
    if 'INCAR' in drifted_inputs:
        evidence_warnings.append('当前 INCAR 与 job.yaml 记录的实际输入哈希不一致')
        if not any(key in signature for key in ('functional', 'metagga', 'lhfcalc')):
            known['functional'] = False
        if 'ivdw' not in signature:
            known['dispersion'] = False
        if 'encut' not in signature:
            known['encut'] = False
        if 'ispin' not in signature:
            known['spin'] = False
        if 'ldau' not in signature:
            known['u_values'] = False
            known['u_by_element'] = False
    if 'KPOINTS' in drifted_inputs:
        evidence_warnings.append('当前 KPOINTS 与 job.yaml 记录的实际输入哈希不一致')
        known['kpoints_scheme'] = False
    if 'POTCAR' in drifted_inputs:
        evidence_warnings.append('当前 POTCAR 与 job.yaml 记录的实际输入哈希不一致')
        if not manifest_has_potcar_provenance and not signature_titels:
            known['potcar_ids'] = False
            known['u_by_element'] = False
        if 'encut' not in signature and fp.get('encut') is not None:
            # An omitted INCAR ENCUT was inferred from the now-drifted POTCAR.
            known['encut'] = False
    return {'label': label, 'fingerprint': fp, 'known': known,
            'u_by_element': u_by_element,
            'evidence_warnings': evidence_warnings}


def compare_methods(records, *, require_same_kpoints):
    """Known conflicts block; missing provenance yields explicit warnings."""
    records = list(records or [])
    issues, warnings = [], []
    warnings.extend(
        f'{row["label"]}：{message}'
        for row in records for message in (row.get('evidence_warnings') or []))
    checked = 0
    labels = [row['label'] for row in records]
    display = {
        'functional': '泛函', 'dispersion': '色散校正', 'encut': 'ENCUT',
        'spin': 'ISPIN', 'u_values': 'DFT+U',
    }
    for field in ('functional', 'dispersion', 'encut', 'spin'):
        if not all(row['known'].get(field) for row in records):
            missing = [row['label'] for row in records if not row['known'].get(field)]
            warnings.append(f'{display[field]} 证据不完整：{"、".join(missing)}')
            continue
        values = [row['fingerprint'].get(field) for row in records]
        if field == 'spin':
            invalid = [
                f'{labels[index]}={value!r}' for index, value in enumerate(values)
                if value not in {1, 2}
            ]
            if invalid:
                issues.append('ISPIN 非法（仅允许 1 或 2）：' + '、'.join(invalid))
                continue
        canonical = [json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
                     for value in values]
        if len(set(canonical)) != 1:
            issues.append(f'{display[field]} 不一致：' + '、'.join(
                f'{labels[index]}={values[index]!r}' for index in range(len(records))))
        else:
            checked += 1

    if not all(row['known'].get('u_by_element') for row in records):
        missing = [row['label'] for row in records
                   if not row['known'].get('u_by_element')]
        warnings.append('DFT+U 元素映射证据不完整：' + '、'.join(missing))
    else:
        maps = [row.get('u_by_element') or {} for row in records]
        shared = set(maps[0]) if maps else set()
        for mapping in maps[1:]:
            shared &= set(mapping)
        if not shared:
            warnings.append('各作业没有共同元素，跳过 DFT+U 逐元素比较')
        else:
            for element in sorted(shared):
                values = [mapping[element] for mapping in maps]
                canonical = [json.dumps(value, sort_keys=True, ensure_ascii=False)
                             for value in values]
                if len(set(canonical)) != 1:
                    issues.append(f'{element} 的 DFT+U 参数不一致：' + '、'.join(
                        f'{labels[index]}={values[index]!r}'
                        for index in range(len(records))))
                else:
                    checked += 1

    if all(row['known'].get('kpoints_scheme') for row in records):
        values = [row['fingerprint'].get('kpoints_scheme') for row in records]
        if len(set(values)) != 1:
            detail = 'K 点方案不一致：' + '、'.join(
                f'{labels[index]}={values[index]}' for index in range(len(records)))
            if require_same_kpoints:
                issues.append(detail)
            else:
                warnings.append(detail + '；slab/体相晶胞不同，请核对倒空间密度')
        else:
            checked += 1
    else:
        warnings.append('K 点证据不完整，无法核对采样密度')

    potcars = [row['fingerprint'].get('potcar_ids') or {} for row in records]
    if not all(row['known'].get('potcar_ids') for row in records):
        warnings.append('POTCAR 身份证据不完整，无法全量核对赝势')
    else:
        shared = set(potcars[0])
        for item in potcars[1:]:
            shared &= set(item)
        if not shared:
            issues.append('各作业 POTCAR 没有可核对的共同元素身份')
        else:
            for element in sorted(shared):
                values = [item.get(element) for item in potcars]
                if len(set(values)) != 1:
                    issues.append(f'{element} 的 POTCAR TITEL 不一致')
                else:
                    checked += 1

    status = 'incompatible' if issues else ('verified' if not warnings else 'unverified')
    return {'status': status, 'issues': issues, 'warnings': warnings,
            'checked_fields': checked, 'labels': labels}


__all__ = [
    'clean_completion_from_files', 'clean_completion_from_text',
    'compare_methods', 'method_record', 'parse_oszicar_energy',
    'parse_oszicar_energy_events', 'parse_oszicar_energy_text',
    'parse_outcar_energy_events', 'validate_done_completion_evidence',
    'validate_done_energy',
    'validate_done_energy_evidence',
]
