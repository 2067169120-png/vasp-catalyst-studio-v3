"""合成 VASP 输出格式变体 → 本项目解析 vs ASE 交叉校验(对拍最终能量)。

夹具:``tests/fixtures/vasp_outputs/<变体>/{OUTCAR,OSZICAR}``(见该目录 README:
均为合成的逼真格式变体样本,非真实 VASP 产物)。对每组:
  - 本项目侧:``freeenergy.read_e0`` 读 OSZICAR 末个 E0(σ→0 外推能);
  - ASE 侧:``ase.io.read(OUTCAR, format='vasp-out', index=-1).get_potential_energy()``。
二者指向同一物理量(末离子步 σ→0 能),容差 1e-6。

参数化按**目录扫描**:把真实 VASP 样本目录(含 OUTCAR+OSZICAR)丢进夹具目录即
自动纳入,无需改测试。ase 为可选对拍依赖,缺失则整文件 skip。
"""
import os

import pytest

# ase 是可选对拍依赖:未安装则整文件跳过(pyproject 的 dev extra 已声明)。
ase_io = pytest.importorskip(
    'ase.io', reason='ase 未安装(可选交叉校验依赖):pip install ase')

from vcstudio.cluster import convergence  # noqa: E402
from vcstudio.project import freeenergy  # noqa: E402

FIXTURE_ROOT = os.path.join(os.path.dirname(__file__), 'fixtures', 'vasp_outputs')


def _fixture_dirs():
    """夹具目录下所有同时含 OUTCAR 与 OSZICAR 的子目录名(排序稳定)。"""
    if not os.path.isdir(FIXTURE_ROOT):
        return []
    names = []
    for name in sorted(os.listdir(FIXTURE_ROOT)):
        d = os.path.join(FIXTURE_ROOT, name)
        if (os.path.isfile(os.path.join(d, 'OUTCAR'))
                and os.path.isfile(os.path.join(d, 'OSZICAR'))):
            names.append(name)
    return names


def test_fixture_dirs_discovered():
    """守卫:参数化不能空跑(否则 0 用例假绿)。至少 3 组自带格式变体。"""
    names = _fixture_dirs()
    assert len(names) >= 3, f'期望 ≥3 组格式变体夹具,实得:{names}'


@pytest.mark.parametrize('name', _fixture_dirs())
def test_read_e0_matches_ase_outcar_energy(name):
    """本项目 read_e0(OSZICAR)与 ASE 读 OUTCAR 的末步 σ→0 能对拍,容差 1e-6。"""
    d = os.path.join(FIXTURE_ROOT, name)
    ours = freeenergy.read_e0(d)
    assert ours is not None, f'{name}:read_e0 未从 OSZICAR 取到 E0'
    atoms = ase_io.read(os.path.join(d, 'OUTCAR'), format='vasp-out', index=-1)
    ase_energy = atoms.get_potential_energy()
    assert ours == pytest.approx(ase_energy, abs=1e-6), (
        f'{name}:本项目 read_e0={ours} 与 ASE OUTCAR E={ase_energy} 不一致')


@pytest.mark.parametrize('name', _fixture_dirs())
def test_oszicar_ionic_steps_match_ase_images(name):
    """离子步数鲁棒性:OSZICAR 解析出的离子步数 == ASE 从 OUTCAR 切出的构型数。"""
    d = os.path.join(FIXTURE_ROOT, name)
    with open(os.path.join(d, 'OSZICAR'), encoding='utf-8') as f:
        steps = convergence.parse_oszicar(f.read())
    images = ase_io.read(os.path.join(d, 'OUTCAR'), format='vasp-out', index=':')
    assert len(steps) == len(images), (
        f'{name}:OSZICAR 离子步 {len(steps)} ≠ ASE 构型数 {len(images)}')
