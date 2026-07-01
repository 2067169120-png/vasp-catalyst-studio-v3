"""真实 PAW_PBE 库端到端集成(skipif:E: 库不可达时跳过)。

锚定 E: 真实赝势库 + 真实 Co SAC 母本(C44/N4/Co1,含磁),验证生成区全链路:
原文透传 INCAR + 追加补全(ENCUT/MAGMOM/ISPIN)、POTCAR TITEL 顺序、slab KPOINTS。
无 E: 库的环境自动跳过(仿 E: tests 的 _REF_CO skipif 惯例)。
"""
import os

import pytest

from vcstudio.generate import job_builder

_REAL_LIB = 'E:/V2.0.0/results/inputs/potpaw54/potpaw54/potpaw54/potpaw_PBE/paw_pbe'
_FIXTURE = os.path.normpath(
    os.path.join(os.path.dirname(__file__), '..', 'fixtures', 'Co_SAC_POSCAR.vasp'))

pytestmark = pytest.mark.skipif(
    not os.path.isfile(os.path.join(_REAL_LIB, 'Co', 'POTCAR')),
    reason='真实 PAW_PBE 库不可达(仅在 E: 库存在的机器上跑)')


def test_co_sac_real_lib_end_to_end(tmp_path):
    out = tmp_path / 'job'
    res = job_builder.build_job_dir(
        _FIXTURE, 'IBRION = 2\nNSW = 200\nEDIFF = 1E-6\n', str(out),
        calc_type='slab', lib_root=_REAL_LIB)

    assert res['ok'] is True
    assert res['elements'] == ['C', 'N', 'Co']
    assert res['kpoints'][2] == 1  # slab kz=1

    # POTCAR:3 个 TITEL,元素顺序对齐 POSCAR(C/N/Co)
    potcar = (out / 'POTCAR').read_text(encoding='utf-8', errors='replace')
    titels = [ln for ln in potcar.splitlines() if 'TITEL' in ln]
    assert len(titels) == 3
    assert [ln.split()[3] for ln in titels] == ['C', 'N', 'Co']

    # INCAR:原文透传(1E-6 不变形)+ 追加补全
    incar = (out / 'INCAR').read_text(encoding='utf-8')
    assert 'EDIFF = 1E-6' in incar
    assert 'ENCUT = 550' in incar                 # D1: 1.3×max(ENMAX)=1.3×400→550
    assert 'MAGMOM = 44*0 4*0 1*5' in incar        # D3: Co 磁性,顺序对齐 C44/N4/Co1
    assert 'ISPIN = 2' in incar
