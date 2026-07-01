"""pytest 共享:sys.path 兜底 + mini POTCAR 库 fixture。

- sys.path 兜底:未 `pip install -e .` 时也能 `import vcstudio`。
- mini_lib:造临时 POTCAR 库(variant -> ENMAX),测试不依赖真实 potpaw54 库。
"""
import os
import sys

import pytest

# ── sys.path 兜底(包根 = tests/ 的上一级) ──────────────────────────────────
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# mini POTCAR 库:variant 目录名 -> ENMAX(eV)。目录名用【变体名】(与 potcar.variant 一致)。
# Li_sv(499)超 400,专供测 ENMAX>ENCUT 抛错(variant('Li')='Li',仅经 _force_variant 触达)。
_MINI_ENMAX = {
    'C': 400.0, 'N': 400.0, 'Li': 140.0, 'S': 260.0,
    'Fe': 270.0, 'Co': 270.0, 'V_sv': 270.0, 'Pt': 230.0,
    'Li_sv': 499.0,
}


def _write_mini_potcar(path, enmax):
    # 头部含 ENMAX 行,格式贴近真实 POTCAR(potcar.read_enmax 正则 ENMAX\s*=\s*([0-9.]+))。
    with open(path, 'w', encoding='utf-8') as f:
        f.write(' PAW_PBE dummy 01Jan2000\n')
        f.write(f'   ENMAX  = {enmax:.3f}; ENMIN  = {enmax * 0.75:.3f} eV\n')
        f.write(' END of PSCTR-controll parameters\n')


@pytest.fixture
def mini_lib(tmp_path):
    """造 {tmp}/paw_pbe/<variant>/POTCAR mini 库,返回 lib_root 路径(str)。"""
    root = tmp_path / 'paw_pbe'
    for v, enmax in _MINI_ENMAX.items():
        d = root / v
        d.mkdir(parents=True)
        _write_mini_potcar(d / 'POTCAR', enmax)
    return str(root)
