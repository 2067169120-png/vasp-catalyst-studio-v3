"""VASPsol 真空/溶剂配对结果校验、能量与可追溯报告。"""
from __future__ import annotations

import html
import json
import math
import os
import re
import tempfile
import time

from vcstudio.generate.incar_builder import parse_incar
from vcstudio.shared import manifest as manifest_mod

_E0_RE = re.compile(r'E0=\s*([-+.0-9Ee]+)')
_PATCH_RE = re.compile(r'(?i)\b(?:VASPsol|solvation|LSOL|EB_K)\b')


def _energy(job_dir: str) -> float:
    path = os.path.join(job_dir, 'OSZICAR')
    value = None
    try:
        with open(path, encoding='utf-8', errors='replace') as handle:
            for line in handle:
                match = _E0_RE.search(line)
                if match:
                    value = float(match.group(1))
    except OSError as exc:
        raise ValueError(f'缺少可读 OSZICAR：{path}') from exc
    if value is None or not math.isfinite(value):
        raise ValueError(f'OSZICAR 中没有有限的末态 E0：{path}')
    return value


def _manifest(job_dir: str, expected_role: str) -> dict:
    manifest = manifest_mod.load_manifest(job_dir)
    if manifest is None:
        raise ValueError(f'目录缺 job.yaml：{job_dir}')
    if manifest.get('state') != 'DONE':
        raise ValueError(f'{expected_role} 作业尚未 DONE（当前 {manifest.get("state")}）')
    if manifest.get('task_type') != 'vaspsol':
        raise ValueError(f'{expected_role} 目录不是 VASPsol 配对作业')
    meta = manifest.get('vaspsol') or (manifest.get('inputs') or {}).get('vaspsol') or {}
    if meta.get('role') != expected_role:
        raise ValueError(f'所选 {expected_role} 目录的配对角色为 {meta.get("role")!r}')
    return manifest


def _hashes(job_dir: str) -> dict[str, str]:
    names = ('INCAR', 'POSCAR', 'KPOINTS', 'POTCAR', 'OSZICAR', 'OUTCAR')
    return {name: manifest_mod.sha256_file(os.path.join(job_dir, name))
            for name in names if os.path.isfile(os.path.join(job_dir, name))}


def _method_signature(job_dir: str) -> dict:
    incar_path = os.path.join(job_dir, 'INCAR')
    try:
        with open(incar_path, encoding='utf-8', errors='replace') as handle:
            incar = parse_incar(handle.read())
    except OSError as exc:
        raise ValueError(f'缺少 INCAR：{job_dir}') from exc
    electronic = {key: value for key, value in incar.items()
                  if key not in {'LSOL', 'EB_K'}}
    hashes = _hashes(job_dir)
    return {
        'incar_without_solvation': electronic,
        'poscar_sha256': hashes.get('POSCAR'),
        'kpoints_sha256': hashes.get('KPOINTS'),
        'potcar_sha256': hashes.get('POTCAR'),
    }


def _require_patch_evidence(solvent_dir: str) -> str:
    path = os.path.join(solvent_dir, 'OUTCAR')
    try:
        with open(path, encoding='utf-8', errors='replace') as handle:
            # OUTCAR 可很大；头部参数区已足以证明 LSOL/EB_K 被程序识别。
            text = handle.read(4 * 1024 * 1024)
    except OSError as exc:
        raise ValueError('溶剂作业缺 OUTCAR，无法证明服务器运行的是 VASPsol 编译版本') from exc
    match = _PATCH_RE.search(text)
    if not match:
        raise ValueError(
            '溶剂 OUTCAR 中未找到 VASPsol/solvation/LSOL/EB_K 证据；'
            '标准 VASP 可能静默给出真空结果，已拒绝计算溶剂化能')
    return match.group(0)


def analyze_pair(vacuum_dir: str, solvent_dir: str) -> dict:
    """严格校验配对关系与方法一致性后计算 ``E_sol - E_vac``。"""
    vacuum = os.path.abspath(str(vacuum_dir))
    solvent = os.path.abspath(str(solvent_dir))
    mv = _manifest(vacuum, 'vacuum')
    ms = _manifest(solvent, 'solvent')
    vmeta = mv.get('vaspsol') or {}
    smeta = ms.get('vaspsol') or {}
    if not vmeta.get('pair_id') or vmeta.get('pair_id') != smeta.get('pair_id'):
        raise ValueError('真空与溶剂目录不属于同一个 VASPsol 配对，拒绝相减')
    sig_v = _method_signature(vacuum)
    sig_s = _method_signature(solvent)
    if sig_v != sig_s:
        differing = [key for key in sig_v if sig_v.get(key) != sig_s.get(key)]
        raise ValueError('真空/溶剂方法或几何不一致，拒绝相减：' + '、'.join(differing))
    evidence = _require_patch_evidence(solvent)
    e_vac = _energy(vacuum)
    e_sol = _energy(solvent)
    return {
        'ok': True,
        'pair_id': vmeta['pair_id'],
        'vacuum_dir': vacuum,
        'solvent_dir': solvent,
        'eb_k': smeta.get('eb_k'),
        'e_vacuum_eV': e_vac,
        'e_solvent_eV': e_sol,
        'solvation_energy_eV': e_sol - e_vac,
        'patch_evidence': evidence,
        'method_signature': sig_v,
        'hashes': {'vacuum': _hashes(vacuum), 'solvent': _hashes(solvent)},
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }


def write_report(vacuum_dir: str, solvent_dir: str, out_path: str | None = None) -> dict:
    result = analyze_pair(vacuum_dir, solvent_dir)
    target = os.path.abspath(str(out_path or os.path.join(
        solvent_dir, 'vcstudio-vaspsol-report.html')))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    payload = html.escape(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    body = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>VASPsol 配对报告</title><style>body{{font:15px/1.6 sans-serif;max-width:960px;margin:36px auto;padding:0 24px}}pre{{background:#f5f6f8;padding:16px;overflow:auto}}.v{{font-size:1.5rem}}</style></head><body>
<h1>VASPsol 真空/溶剂配对报告</h1>
<p class="v">ΔE<sub>solv</sub> = E<sub>solvent</sub> − E<sub>vacuum</sub> = <b>{result['solvation_energy_eV']:.8f} eV</b></p>
<p>真空能量：{result['e_vacuum_eV']:.8f} eV；溶剂能量：{result['e_solvent_eV']:.8f} eV；EB_K：{html.escape(str(result['eb_k']))}。</p>
<p>已验证：同一 pair_id、相同几何/KPOINTS/POTCAR、除 LSOL/EB_K 外相同 INCAR、两项均为 DONE，并在溶剂 OUTCAR 找到补丁证据。</p>
<h2>溯源证据</h2><pre>{payload}</pre></body></html>'''
    fd, tmp = tempfile.mkstemp(prefix='.vaspsol-', suffix='.tmp',
                               dir=os.path.dirname(target))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as handle:
            handle.write(body)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    result['report_file'] = target
    return result
