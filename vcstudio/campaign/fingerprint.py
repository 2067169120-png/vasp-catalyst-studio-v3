"""方法指纹对象(蓝图 E4):一次能量比较里,所有能量必须共享同一指纹。

指纹字段:泛函 / 色散 / 截断能 / k 点方案 / 赝势身份(元素→TITEL hash)/ 自旋 /
Hubbard U / 参考态约定 / 电子步收敛 EDIFF / 离子步收敛 EDIFFG。

用途:
- `extract_from_inputs`:从 INCAR/KPOINTS 文本 + POTCAR 规格抽出指纹 dict。
- `fingerprint_hash`:稳定序列化 + sha256 前 12 位(跨进程可复现)。
- `check_group_consistency`:逐字段比对一组指纹,指出「哪个字段谁不一样」(中文)。

这是可复现包里「一致性证书」的数据来源,也是 accept_gate 拦截跨基组污染 ΔE 的依据。
零第三方依赖:INCAR 只做轻量 KEY=VALUE 解析,不引入 generate 层解析器(保持解耦)。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import hashlib
import json

FINGERPRINT_FIELDS = (
    'functional', 'dispersion', 'encut', 'kpoints_scheme', 'potcar_ids',
    'spin', 'u_values', 'reference_convention', 'ediff', 'ediffg',
)

_FIELD_CN = {
    'functional': '泛函', 'dispersion': '色散校正', 'encut': '截断能 ENCUT',
    'kpoints_scheme': 'k 点方案', 'potcar_ids': '赝势身份(POTCAR)',
    'spin': '自旋 ISPIN', 'u_values': 'Hubbard U', 'reference_convention': '参考态约定',
    'ediff': '电子步收敛 EDIFF', 'ediffg': '离子步收敛 EDIFFG',
}

# 数值型字段:比对/哈希前统一 float 化,规避 400 与 400.0、1e-4 与 0.0001 的伪差异
_NUMERIC_FIELDS = {'encut', 'spin', 'ediff', 'ediffg'}

_GGA_MAP = {'PE': 'PBE', 'RP': 'RPBE', 'PS': 'PBEsol', '91': 'PW91',
            'RE': 'revPBE', 'B5': 'BEEF-vdW', 'AM': 'AM05'}
_IVDW_MAP = {'0': None, '1': 'DFT-D2', '10': 'DFT-D2', '11': 'DFT-D3(zero)',
             '12': 'DFT-D3(BJ)', '2': 'TS', '20': 'TS', '21': 'TS-HI',
             '202': 'MBD@rsSCS', '4': 'dDsC', '263': 'rVV10'}


# ── 轻量 INCAR 解析 ───────────────────────────────────────────────────────────
def _parse_incar(text: str) -> dict:
    """KEY=VALUE(大写键)。剥 # / ! 注释,支持一行多项以 ; 分隔。"""
    out: dict = {}
    for raw in (text or '').splitlines():
        line = raw.split('#', 1)[0].split('!', 1)[0]
        for part in line.split(';'):
            if '=' not in part:
                continue
            k, v = part.split('=', 1)
            k = k.strip().upper()
            if k:
                out[k] = v.strip()
    return out


def _incar_bool(v) -> bool:
    if v is None:
        return False
    return str(v).strip().strip('.').upper().startswith('T')


def _to_float(v):
    if v is None:
        return None
    try:
        return float(str(v).split()[0])
    except (ValueError, IndexError):
        return None


def _to_int(v, default=None):
    f = _to_float(v)
    return int(f) if f is not None else default


def _functional(incar: dict):
    mgga = incar.get('METAGGA')
    if mgga:
        return 'metagga:' + str(mgga).strip().strip('"').strip("'").upper()
    if _incar_bool(incar.get('LHFCALC')):
        return 'hybrid:HSE' if incar.get('HFSCREEN') else 'hybrid:PBE0'
    gga = incar.get('GGA')
    if gga:
        tok = str(gga).strip().strip('"').strip("'").upper()[:2]
        return _GGA_MAP.get(tok, f'GGA:{str(gga).strip()}')
    return None


def _dispersion(incar: dict):
    ivdw = incar.get('IVDW')
    if ivdw is not None:
        return _IVDW_MAP.get(str(ivdw).strip(), f'IVDW={str(ivdw).strip()}')
    if _incar_bool(incar.get('LUSE_VDW')):
        return 'vdW-DF(family)'
    return None


def _u_values(incar: dict):
    if not _incar_bool(incar.get('LDAU')):
        return None
    out = {'LDAU': True}
    for k in ('LDAUTYPE', 'LDAUL', 'LDAUU', 'LDAUJ'):
        if k in incar:
            out[k] = str(incar[k]).strip()
    return out


def _kpoints_scheme(kpoints_text: str, incar: dict | None = None):
    """KPOINTS 文本 → 规范描述(如 'Gamma 3x3x1');无文件时回落 INCAR 的 KSPACING。"""
    incar = incar or {}
    if not (kpoints_text or '').strip():
        ksp = incar.get('KSPACING')
        return f'KSPACING {str(ksp).strip()}' if ksp else None
    lines = [ln.strip() for ln in kpoints_text.splitlines()]
    if len(lines) < 3:
        return None
    try:
        nk = int(lines[1].split()[0])
    except (ValueError, IndexError):
        return None
    if nk > 0:
        return f'explicit:{nk}'
    char = (lines[2][:1] or '').upper()
    name = {'G': 'Gamma', 'M': 'Monkhorst', 'A': 'Auto'}.get(char, char or '?')
    if name == 'Auto':
        return f'Auto({lines[3].strip()})' if len(lines) > 3 else 'Auto'
    grid = lines[3].split()[:3] if len(lines) > 3 else []
    return f'{name} {"x".join(grid)}' if grid else name


def _potcar_ids(potcar_spec) -> dict:
    """元素 → TITEL 的 sha256 前 12 位。接受 [{'element','titel'},...] 或 {元素: titel}。"""
    pairs: list = []
    if isinstance(potcar_spec, dict):
        pairs = list(potcar_spec.items())
    else:
        for p in (potcar_spec or []):
            if isinstance(p, dict):
                pairs.append((p.get('element'), p.get('titel')))
            elif isinstance(p, (list, tuple)) and len(p) >= 2:
                pairs.append((p[0], p[1]))
    out: dict = {}
    for el, titel in pairs:
        if not el:
            continue
        out[str(el)] = (hashlib.sha256(str(titel).encode('utf-8')).hexdigest()[:12]
                        if titel else None)
    return out


# ── 构造 / 抽取 ───────────────────────────────────────────────────────────────
def new_fingerprint(**fields) -> dict:
    """全字段指纹 dict(未给的字段填 None)。"""
    fp = {f: None for f in FINGERPRINT_FIELDS}
    fp.update({k: v for k, v in fields.items() if k in FINGERPRINT_FIELDS})
    return fp


def extract_from_inputs(incar_text: str, kpoints_text: str, potcar_spec,
                        *, reference_convention: str | None = None) -> dict:
    """从 INCAR/KPOINTS 文本 + POTCAR 规格抽取方法指纹。参考态约定须由调用方给出。"""
    incar = _parse_incar(incar_text)
    return {
        'functional': _functional(incar),
        'dispersion': _dispersion(incar),
        'encut': _to_float(incar.get('ENCUT')),
        'kpoints_scheme': _kpoints_scheme(kpoints_text, incar),
        'potcar_ids': _potcar_ids(potcar_spec),
        'spin': _to_int(incar.get('ISPIN'), 1),
        'u_values': _u_values(incar),
        'reference_convention': reference_convention,
        'ediff': _to_float(incar.get('EDIFF')),
        'ediffg': _to_float(incar.get('EDIFFG')),
    }


# ── 哈希 / 一致性 ─────────────────────────────────────────────────────────────
def _canon(field: str, val):
    if val is None:
        return None
    if field in _NUMERIC_FIELDS:
        try:
            return float(val)
        except (TypeError, ValueError):
            return val
    return val


def _canonical(fp: dict) -> dict:
    return {f: _canon(f, (fp or {}).get(f)) for f in FINGERPRINT_FIELDS}


def fingerprint_hash(fp: dict) -> str:
    """稳定序列化 + sha256 前 12 位。字段顺序/键序固定,跨进程可复现。"""
    blob = json.dumps(_canonical(fp), sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), default=str)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()[:12]


def _fmt(v) -> str:
    if v is None:
        return '(未设)'
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    return str(v)


def check_group_consistency(fps: list, labels: list | None = None) -> dict:
    """逐字段比对一组指纹。返回 {'consistent': bool, 'diffs': [{field,values,detail}]}。

    labels 给每份指纹起名(默认 #0/#1/...),diffs.detail 中文点名哪个字段、谁与谁不一样。
    """
    fps = list(fps or [])
    if labels is None:
        labels = [f'#{i}' for i in range(len(fps))]
    if len(fps) <= 1:
        return {'consistent': True, 'diffs': []}

    diffs: list = []
    for field in FINGERPRINT_FIELDS:
        vals = [_canon(field, fp.get(field)) for fp in fps]
        seen: list = []
        for v in vals:
            key = json.dumps(v, sort_keys=True, ensure_ascii=False, default=str)
            if key not in seen:
                seen.append(key)
        if len(seen) > 1:
            per = {labels[i]: vals[i] for i in range(len(fps))}
            detail = f'{_FIELD_CN.get(field, field)} 不一致:' + '、'.join(
                f'{labels[i]}={_fmt(vals[i])}' for i in range(len(fps)))
            diffs.append({'field': field, 'values': per, 'detail': detail})
    return {'consistent': not diffs, 'diffs': diffs}
