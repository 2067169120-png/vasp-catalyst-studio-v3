"""Scientific gates for total-energy subtraction workflows.

Formation/binding and surface energies subtract large VASP total energies.  A
parseable last ``E0`` is not enough: every operand must be a managed DONE job
with clean completion evidence, and known electronic-method conflicts must
stop the calculation.  Missing provenance remains explicit ``unverified``
evidence so old imported results are never silently presented as comparable.
"""
from __future__ import annotations

import json
import math
import os
import re


_CLEAN_RE = re.compile(
    r'General\s+timing\s+and\s+accounting\s+information(?:s)?\s+for\s+this\s+job'
    r'|Total\s+CPU\s+time\s+used|Voluntary\s+context\s+switches', re.I)


def _read_named(job_dir, name):
    path = os.path.join(str(job_dir), name)
    try:
        with open(path, encoding='utf-8', errors='replace') as handle:
            return handle.read()
    except OSError:
        return ''


def _read_structure(job_dir):
    return _read_named(job_dir, 'CONTCAR') or _read_named(job_dir, 'POSCAR')


def parse_oszicar_energy(job_dir):
    """Return the last finite OSZICAR E0, or ``None``."""
    energy = None
    for line in _read_named(job_dir, 'OSZICAR').splitlines():
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


def clean_completion_from_files(job_dir):
    """Read bounded file tails and return ``(clean, evidence_label)``."""
    try:
        outcar = os.path.join(str(job_dir), 'OUTCAR')
        if os.path.isfile(outcar):
            with open(outcar, 'rb') as handle:
                handle.seek(max(os.path.getsize(outcar) - 2 * 1024 * 1024, 0))
                tail = handle.read().decode('utf-8', errors='replace')
            if _CLEAN_RE.search(tail):
                return True, 'OUTCAR timing 页脚'
        vasprun = os.path.join(str(job_dir), 'vasprun.xml')
        if os.path.isfile(vasprun):
            with open(vasprun, 'rb') as handle:
                handle.seek(max(os.path.getsize(vasprun) - 128 * 1024, 0))
                tail = handle.read().decode('utf-8', errors='replace')
            if '</modeling>' in tail:
                return True, 'vasprun.xml 完整闭合'
    except OSError:
        pass
    return False, ''


def validate_done_energy(job_dir, label, manifest_mod):
    """Return ``(energy, manifest, evidence_messages)`` after hard validation."""
    manifest = manifest_mod.load_manifest(job_dir)
    if manifest is None:
        raise ValueError(
            f'{label}缺少可读 job.yaml；请先通过「导入已算结果」建立可追溯作业')
    state = str(manifest.get('state') or '')
    if state != 'DONE':
        raise ValueError(
            f'{label}未通过 DONE 门（当前 {state or "未知"}），拒绝使用未收敛能量')
    results = manifest.get('results') or {}
    diagnosis = results.get('diagnosis') or {}
    failure_class = diagnosis.get('failure_class')
    if failure_class and failure_class != 'CONVERGED':
        raise ValueError(f'{label}诊断为 {failure_class}，与 DONE 矛盾，拒绝计算')
    exit_code = diagnosis.get('exit_code')
    try:
        nonzero_exit = exit_code is not None and int(exit_code) != 0
    except (TypeError, ValueError):
        nonzero_exit = True
    if nonzero_exit:
        raise ValueError(f'{label}记录非零退出码 {exit_code}，不得作为已完成能量')

    completion = (results.get('convergence_evidence') or {}).get('completion') or {}
    clean = (diagnosis.get('clean_exit') is True
             or completion.get('outcar_footer') is True
             or completion.get('vasprun_complete') is True)
    clean_source = 'job.yaml 完整性证据' if clean else ''
    if not clean:
        clean, clean_source = clean_completion_from_files(job_dir)
    if not clean:
        raise ValueError(
            f'{label}无 OUTCAR timing 页脚/完整 vasprun.xml；旧收敛串不能证明完整完成')

    try:
        energy = float(results.get('energy_e0_eV'))
    except (TypeError, ValueError):
        raise ValueError(f'{label}的 DONE job.yaml 未记录可用 energy_e0_eV') from None
    if not math.isfinite(energy):
        raise ValueError(f'{label}的 energy_e0_eV 非有限数')
    parsed = parse_oszicar_energy(job_dir)
    if parsed is not None and abs(parsed - energy) > 1e-3:
        raise ValueError(
            f'{label}的 job.yaml 能量 {energy:.8f} eV 与 OSZICAR '
            f'{parsed:.8f} eV 不一致，疑混入不同轮结果')
    return energy, manifest, [f'{label}完成证据：{clean_source}']


def method_record(job_dir, manifest, label):
    """Extract a comparable method record from files with manifest fallbacks."""
    from vcstudio.campaign import fingerprint as fingerprint_mod

    incar = _read_named(job_dir, 'INCAR')
    kpoints = _read_named(job_dir, 'KPOINTS')
    inputs = (manifest or {}).get('inputs') or {}
    results = (manifest or {}).get('results') or {}
    signature = dict(results.get('reference_method_signature')
                     or inputs.get('reference_method_signature') or {})
    provenance = list(inputs.get('potcar_provenance') or inputs.get('potcar') or [])

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
        potcar_text = _read_named(job_dir, 'POTCAR')
        titels = [match.group(1).strip() for match in re.finditer(
            r'^\s*TITEL\s*=\s*(.+?)\s*$', potcar_text, re.I | re.M)]
        elements = []
        structure = _read_structure(job_dir)
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
    known = {
        'functional': bool(incar), 'dispersion': bool(incar),
        'encut': bool(incar and fp.get('encut') is not None),
        'spin': bool(incar), 'u_values': bool(incar),
        'kpoints_scheme': bool(fp.get('kpoints_scheme')),
        'potcar_ids': bool(fp.get('potcar_ids')),
    }
    if incar:
        # 标准 VASP + PAW_PBE 下，未显式写 GGA 与 GGA=PE 同为 PBE，
        # 不应因「是否显式写出默认值」产生伪冲突。
        fp['functional'] = fp.get('functional') or 'PBE'
        fp['dispersion'] = fp.get('dispersion') or 'none'
        fp['u_values'] = fp.get('u_values') or {'LDAU': False}
    # Apply every trustworthy output signature field even when input files are
    # present: current INCAR is only a fallback and must not rewrite history.
    signature_hybrid = str(signature.get('lhfcalc') or '').strip().strip('.').upper() \
        in {'T', 'TRUE', '1'}
    signature_metagga = str(signature.get('metagga') or '').strip().strip('"').strip("'")
    if signature_hybrid:
        fp['functional'] = (
            f'hybrid:AEXX={signature.get("aexx")!r},HFSCREEN={signature.get("hfscreen")!r}')
        known['functional'] = True
    elif signature_metagga and signature_metagga.upper() not in {'F', 'FALSE', 'NONE'}:
        fp['functional'] = 'metagga:' + signature_metagga.upper()
        known['functional'] = True
    elif signature.get('functional') is not None:
        fp['functional'] = signature.get('functional')
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
            fp['spin'] = int(signature['ispin'])
            known['spin'] = True
        except (TypeError, ValueError):
            pass
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
    return {'label': label, 'fingerprint': fp, 'known': known}


def compare_methods(records, *, require_same_kpoints):
    """Known conflicts block; missing provenance yields explicit warnings."""
    records = list(records or [])
    issues, warnings = [], []
    checked = 0
    labels = [row['label'] for row in records]
    display = {
        'functional': '泛函', 'dispersion': '色散校正', 'encut': 'ENCUT',
        'spin': 'ISPIN', 'u_values': 'DFT+U',
    }
    for field in ('functional', 'dispersion', 'encut', 'spin', 'u_values'):
        if not all(row['known'].get(field) for row in records):
            missing = [row['label'] for row in records if not row['known'].get(field)]
            warnings.append(f'{display[field]} 证据不完整：{"、".join(missing)}')
            continue
        values = [row['fingerprint'].get(field) for row in records]
        canonical = [json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
                     for value in values]
        if len(set(canonical)) != 1:
            issues.append(f'{display[field]} 不一致：' + '、'.join(
                f'{labels[index]}={values[index]!r}' for index in range(len(records))))
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
    'clean_completion_from_files', 'compare_methods', 'method_record',
    'parse_oszicar_energy', 'validate_done_energy',
]
