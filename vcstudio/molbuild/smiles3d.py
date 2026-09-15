"""SMILES → 3D 建模(RDKit,延迟依赖)——对标 starpivot-DFT 的"SMILES→3D建模"环节。

链路:``MolFromSmiles``(加显式 H ``AddHs``)→ ETKDGv3 距离几何嵌入(固定 seed 可复现)
→ MMFF94 力场优化(参数不全或指定则退 UFF)→ 导出 elements/coords。电荷取 SMILES 形式
电荷之和;多重度按未成对(自由基)电子数**给提示不拍板**。

RDKit 为可选重依赖(pip install rdkit):模块级 ``_lazy()`` 延迟 import 并返回
``(Chem, AllChem)``,缺失时返回 None,调用方降级为结构化中文 error(绝不 ImportError 崩)。
``_lazy`` 独立成函数亦为便于测试 monkeypatch 注入假 RDKit。

导出工具 to_xyz / to_mol(V2000)/ to_poscar_box 为纯 python(numpy 与 write_poscar
延迟到 to_poscar_box 内 import,保持模块顶层零重依赖)。中文注释允许,英文标识符。
"""
from __future__ import annotations

from vcstudio.molbuild import molinfo

_RDKIT_MISSING = ('未安装 rdkit(pip install rdkit):无法从 SMILES 生成 3D 结构。'
                  'rdkit 提供 SMILES 解析、ETKDG 构象嵌入与 MMFF/UFF 力场优化。')


def _lazy():
    """延迟 import RDKit,返回 ``(Chem, AllChem)``;缺失 → None。独立成函数便于测试注入。"""
    try:
        from rdkit import Chem
        from rdkit.Chem import AllChem
    except ImportError:
        return None
    return Chem, AllChem


def smiles_to_3d(smiles, *, forcefield: str = 'auto', seed: int = 42) -> dict:
    """SMILES → 3D 结构。

    参数:
        forcefield: 'auto'(MMFF 可用则 MMFF,否则 UFF)/'mmff'(参数不全时退 UFF)/'uff'。
        seed: ETKDGv3 随机种子(固定 → 构象可复现)。

    返回 ``{'ok','elements','coords','formula','n_atoms','charge',
    'multiplicity_hint','warnings','error'}``:
        - coords 为 [[x,y,z],...] Å;charge 取 SMILES 形式电荷之和。
        - multiplicity_hint = 未成对电子数 + 1(**仅提示**,自旋态须自旋极化核验)。
        - warnings 记录实际所用力场及回退原因。任何失败 → ok=False + 中文 error(不抛)。
    """
    ff = str(forcefield).lower()
    if ff not in ('auto', 'mmff', 'uff'):
        return _fail(f'未知力场 {forcefield!r};可选 auto/mmff/uff')

    lazy = _lazy()
    if lazy is None:
        return _fail(_RDKIT_MISSING)
    chem, allchem = lazy

    mol = chem.MolFromSmiles(smiles)
    if mol is None:
        return _fail(f'SMILES 解析失败:{smiles!r}(非法或含 RDKit 不支持的记法)')
    mol = chem.AddHs(mol)                                       # 补显式 H(3D 建模必须)

    params = allchem.ETKDGv3()                                 # 距离几何 + 经验扭转修正
    params.randomSeed = int(seed)                              # 固定 seed → 可复现
    if allchem.EmbedMolecule(mol, params) != 0:
        return _fail('ETKDG 构象嵌入失败(分子过大/受限,建议换 seed 或分步建模)')

    warnings: list[str] = []
    used = _optimise(allchem, mol, ff, warnings)               # 力场优化 + 记录实际力场
    warnings.append(f'实际所用力场:{used.upper()}')

    conf = mol.GetConformer()
    elements: list[str] = []
    coords: list[list[float]] = []
    for i, atom in enumerate(mol.GetAtoms()):
        elements.append(atom.GetSymbol())
        pos = conf.GetAtomPosition(i)
        coords.append([float(pos.x), float(pos.y), float(pos.z)])

    charge = int(chem.GetFormalCharge(mol))                    # 形式电荷之和
    n_radical = sum(int(atom.GetNumRadicalElectrons()) for atom in mol.GetAtoms())
    multiplicity_hint = n_radical + 1                          # 仅提示,不拍板
    if n_radical:
        warnings.append(f'检测到 {n_radical} 个未成对电子 → 多重度提示 '
                        f'{multiplicity_hint}(仅初猜,须自旋极化核验)')

    return {
        'ok': True,
        'elements': elements,
        'coords': coords,
        'formula': molinfo.formula(elements),
        'n_atoms': len(elements),
        'charge': charge,
        'multiplicity_hint': multiplicity_hint,
        'warnings': warnings,
        'error': '',
    }


def _optimise(allchem, mol, ff: str, warnings: list) -> str:
    """按 ff 选择并运行力场优化,返回实际所用力场名('mmff'/'uff')。auto/mmff/uff 语义见上。"""
    mmff_ok = bool(allchem.MMFFHasAllMoleculeParams(mol))
    if ff == 'uff':
        used = 'uff'
    elif ff == 'mmff':
        if mmff_ok:
            used = 'mmff'
        else:
            used = 'uff'
            warnings.append('指定 MMFF 但该分子 MMFF 参数不全,已回退 UFF')
    else:                                                       # auto
        used = 'mmff' if mmff_ok else 'uff'
    if used == 'mmff':
        allchem.MMFFOptimizeMolecule(mol)
    else:
        allchem.UFFOptimizeMolecule(mol)
    return used


def _fail(msg: str) -> dict:
    """统一失败返回(字段齐全,ok=False)。"""
    return {
        'ok': False, 'elements': [], 'coords': [], 'formula': '',
        'n_atoms': 0, 'charge': 0, 'multiplicity_hint': None,
        'warnings': [], 'error': msg,
    }


# ── 导出格式(纯 python,不依赖 RDKit) ─────────────────────────────────────────

def to_xyz(elements, coords, comment: str = '') -> str:
    """elements/coords → 标准 XYZ 文本(首行原子数,次行注释,后接 ``El x y z``)。"""
    elements = list(elements)
    coords = list(coords)
    if len(elements) != len(coords):
        raise ValueError('元素数与坐标数不一致')
    safe_comment = str(comment).replace('\n', ' ').replace('\r', ' ')
    lines = [str(len(elements)), safe_comment]
    for el, xyz in zip(elements, coords):
        x, y, z = (float(xyz[0]), float(xyz[1]), float(xyz[2]))
        lines.append(f'{el:<2} {x:>15.8f} {y:>15.8f} {z:>15.8f}')
    return '\n'.join(lines) + '\n'


def to_mol(elements, coords, bonds=None, *, title: str = 'vcstudio') -> str:
    """elements/coords(+ 可选 bonds)→ MDL MOL V2000 文本。

    bonds 为 ``[(a1, a2[, order]), ...]``(原子序号 **1 起**,order 缺省单键)。无键信息时
    仅原子块 + 0 键亦为合法 V2000。
    """
    elements = list(elements)
    coords = list(coords)
    if len(elements) != len(coords):
        raise ValueError('元素数与坐标数不一致')
    bonds = list(bonds or [])
    n_atoms, n_bonds = len(elements), len(bonds)
    safe_title = str(title).replace('\n', ' ').replace('\r', ' ')
    lines = [safe_title, '  vcstudio', '']                     # 标题 / 程序行 / 注释行
    lines.append(f'{n_atoms:>3}{n_bonds:>3}  0  0  0  0  0  0  0  0999 V2000')
    for el, xyz in zip(elements, coords):
        x, y, z = (float(xyz[0]), float(xyz[1]), float(xyz[2]))
        lines.append(f'{x:>10.4f}{y:>10.4f}{z:>10.4f} {el:<3}'
                     ' 0  0  0  0  0  0  0  0  0  0  0  0')
    for b in bonds:
        a1, a2 = int(b[0]), int(b[1])
        order = int(b[2]) if len(b) > 2 else 1
        lines.append(f'{a1:>3}{a2:>3}{order:>3}  0  0  0  0')
    lines.append('M  END')
    return '\n'.join(lines) + '\n'


def to_poscar_box(elements, coords, box: float = 15.0, *, center: bool = True,
                  comment: str = '') -> str:
    """elements/coords 装入立方盒(边长 box Å)→ POSCAR 文本(口径对齐 molecules.py)。

    center=True 时质心置盒中心(与 molecules.molecule_in_box 同款)。numpy 与
    write_poscar 延迟 import(保持模块顶层零重依赖)。
    """
    import numpy as np

    from vcstudio.generate.sac_builder import write_poscar

    elements = list(elements)
    arr = np.asarray(coords, dtype=float)
    if arr.ndim != 2 or arr.shape[1] != 3:
        raise ValueError('coords 需为 (N, 3) 坐标数组')
    if len(elements) != len(arr):
        raise ValueError('元素数与坐标数不一致')
    box = float(box)
    if center and len(arr):
        arr = arr - arr.mean(axis=0) + np.array([box / 2.0] * 3)
    cell = np.diag([box] * 3)
    title = comment or f'molecule in {box:g} A cubic box'
    return write_poscar(title, cell, elements, arr, mode='Cartesian')
