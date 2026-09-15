"""molbuild.smiles3d 测试:SMILES→3D(假 RDKit 注入)+ xyz/mol/POSCAR 导出 + 真实冒烟。"""
import importlib.util
import types

import pytest

from vcstudio.molbuild import smiles3d

_HAS_RDKIT = importlib.util.find_spec('rdkit') is not None


# ── 假 RDKit 构件 ──────────────────────────────────────────────────────────────

class _Atom:
    def __init__(self, sym, radicals=0):
        self._sym, self._rad = sym, radicals

    def GetSymbol(self):
        return self._sym

    def GetNumRadicalElectrons(self):
        return self._rad


class _Pos:
    def __init__(self, xyz):
        self.x, self.y, self.z = xyz


class _Conf:
    def __init__(self, coords):
        self._coords = coords

    def GetAtomPosition(self, i):
        return _Pos(self._coords[i])


class _Mol:
    def __init__(self, atoms, coords):
        self._atoms, self._coords = atoms, coords

    def GetAtoms(self):
        return list(self._atoms)

    def GetNumAtoms(self):
        return len(self._atoms)

    def GetConformer(self):
        return _Conf(self._coords)


class _Params:
    def __init__(self):
        self.randomSeed = None


def _water_mol(radicals=0):
    atoms = [_Atom('O', radicals), _Atom('H'), _Atom('H')]
    coords = [[0.0, 0.0, 0.0], [0.96, 0.0, 0.0], [-0.24, 0.93, 0.0]]
    return _Mol(atoms, coords)


def _make_rdkit(*, hmol=None, charge=0, mmff_ok=True, embed_code=0,
                mol_from=None):
    """构造假 (Chem, AllChem) + record(记录 seed/所用力场/嵌入)。"""
    hmol = hmol if hmol is not None else _water_mol()
    record = {'opt': [], 'seed': None, 'embedded': 0}

    chem = types.SimpleNamespace(
        MolFromSmiles=(mol_from if mol_from is not None else (lambda smi: object())),
        AddHs=lambda mol: hmol,
        GetFormalCharge=lambda mol: charge,
    )

    def _embed(mol, params):
        record['seed'] = params.randomSeed
        record['embedded'] += 1
        return embed_code

    allchem = types.SimpleNamespace(
        ETKDGv3=_Params,
        EmbedMolecule=_embed,
        MMFFHasAllMoleculeParams=lambda mol: mmff_ok,
        MMFFOptimizeMolecule=lambda mol: record['opt'].append('mmff'),
        UFFOptimizeMolecule=lambda mol: record['opt'].append('uff'),
    )
    return chem, allchem, record


# ── smiles_to_3d ───────────────────────────────────────────────────────────────

def test_smiles_to_3d_missing_rdkit(monkeypatch):
    monkeypatch.setattr(smiles3d, '_lazy', lambda: None)
    r = smiles3d.smiles_to_3d('CCO')
    assert not r['ok'] and 'rdkit' in r['error']


def test_smiles_to_3d_unknown_forcefield():
    # 力场校验在 _lazy 之前,无需 RDKit
    r = smiles3d.smiles_to_3d('CCO', forcefield='xtb')
    assert not r['ok'] and '未知力场' in r['error']


def test_smiles_to_3d_bad_smiles(monkeypatch):
    chem, allchem, _ = _make_rdkit(mol_from=lambda smi: None)
    monkeypatch.setattr(smiles3d, '_lazy', lambda: (chem, allchem))
    r = smiles3d.smiles_to_3d('BAD')
    assert not r['ok'] and 'SMILES 解析失败' in r['error']


def test_smiles_to_3d_success_auto_mmff(monkeypatch):
    chem, allchem, record = _make_rdkit(mmff_ok=True)
    monkeypatch.setattr(smiles3d, '_lazy', lambda: (chem, allchem))
    r = smiles3d.smiles_to_3d('O', forcefield='auto')
    assert r['ok']
    assert r['elements'] == ['O', 'H', 'H']
    assert r['formula'] == 'H2O'
    assert r['n_atoms'] == 3
    assert r['charge'] == 0
    assert r['coords'][0] == [0.0, 0.0, 0.0]
    assert record['opt'] == ['mmff']                 # auto + MMFF 可用 → 走 MMFF
    assert any('MMFF' in w for w in r['warnings'])


def test_smiles_to_3d_auto_falls_back_to_uff(monkeypatch):
    chem, allchem, record = _make_rdkit(mmff_ok=False)
    monkeypatch.setattr(smiles3d, '_lazy', lambda: (chem, allchem))
    r = smiles3d.smiles_to_3d('O', forcefield='auto')
    assert r['ok'] and record['opt'] == ['uff']
    assert any('UFF' in w for w in r['warnings'])


def test_smiles_to_3d_uff_explicit(monkeypatch):
    chem, allchem, record = _make_rdkit(mmff_ok=True)   # 即便 MMFF 可用
    monkeypatch.setattr(smiles3d, '_lazy', lambda: (chem, allchem))
    r = smiles3d.smiles_to_3d('O', forcefield='uff')
    assert r['ok'] and record['opt'] == ['uff']


def test_smiles_to_3d_mmff_requested_but_unavailable(monkeypatch):
    chem, allchem, record = _make_rdkit(mmff_ok=False)
    monkeypatch.setattr(smiles3d, '_lazy', lambda: (chem, allchem))
    r = smiles3d.smiles_to_3d('O', forcefield='mmff')
    assert r['ok'] and record['opt'] == ['uff']
    assert any('回退' in w for w in r['warnings'])


def test_smiles_to_3d_seed_passthrough(monkeypatch):
    chem, allchem, record = _make_rdkit()
    monkeypatch.setattr(smiles3d, '_lazy', lambda: (chem, allchem))
    smiles3d.smiles_to_3d('O', seed=12345)
    assert record['seed'] == 12345                   # ETKDGv3.randomSeed 被透传


def test_smiles_to_3d_embed_failure(monkeypatch):
    chem, allchem, _ = _make_rdkit(embed_code=-1)
    monkeypatch.setattr(smiles3d, '_lazy', lambda: (chem, allchem))
    r = smiles3d.smiles_to_3d('O')
    assert not r['ok'] and 'ETKDG' in r['error']


def test_smiles_to_3d_charge_and_multiplicity_hint(monkeypatch):
    # 带 1 个未成对电子 + 形式电荷 −1 → charge=-1, multiplicity_hint=2
    hmol = _water_mol(radicals=1)
    chem, allchem, _ = _make_rdkit(hmol=hmol, charge=-1)
    monkeypatch.setattr(smiles3d, '_lazy', lambda: (chem, allchem))
    r = smiles3d.smiles_to_3d('[OH-]')
    assert r['ok'] and r['charge'] == -1
    assert r['multiplicity_hint'] == 2
    assert any('未成对电子' in w for w in r['warnings'])


# ── 导出格式 ────────────────────────────────────────────────────────────────────

def test_to_xyz_format():
    txt = smiles3d.to_xyz(['O', 'H'], [[0.0, 0.0, 0.0], [0.96, 0.0, 0.0]], 'water')
    lines = txt.splitlines()
    assert lines[0] == '2'
    assert lines[1] == 'water'
    assert lines[2].split()[0] == 'O'
    assert lines[3].split()[0] == 'H'
    assert txt.endswith('\n')


def test_to_xyz_length_mismatch_raises():
    with pytest.raises(ValueError, match='不一致'):
        smiles3d.to_xyz(['O'], [[0, 0, 0], [1, 1, 1]])


def test_to_mol_v2000_no_bonds():
    txt = smiles3d.to_mol(['O', 'H', 'H'],
                          [[0, 0, 0], [0.96, 0, 0], [-0.24, 0.93, 0]])
    lines = txt.splitlines()
    assert lines[3].strip().startswith('3  0')       # counts:3 原子 0 键
    assert 'V2000' in lines[3]
    assert lines[-1] == 'M  END'


def test_to_mol_v2000_with_bonds():
    txt = smiles3d.to_mol(['O', 'H', 'H'],
                          [[0, 0, 0], [0.96, 0, 0], [-0.24, 0.93, 0]],
                          bonds=[(1, 2), (1, 3, 1)])
    lines = txt.splitlines()
    assert lines[3].split()[:2] == ['3', '2']        # 3 原子 2 键
    # 键块在原子块之后、M END 之前
    assert lines[-2].split()[:3] == ['1', '3', '1']
    assert lines[-1] == 'M  END'


def test_to_poscar_box_matches_molecules_convention():
    # 与 molecules.molecule_in_box 同款:居中 + 立方盒;可被 povray 解析器读回
    from vcstudio.external.povray_render import parse_poscar_atoms
    els = ['O', 'H', 'H']
    coords = [[0.0, 0.0, 0.0], [0.96, 0.0, 0.0], [-0.24, 0.93, 0.0]]
    txt = smiles3d.to_poscar_box(els, coords, box=15.0)
    syms, back = parse_poscar_atoms(txt)
    assert sorted(syms) == ['H', 'H', 'O']
    # 质心应落在盒中心 (7.5, 7.5, 7.5)
    cx = sum(p[0] for p in back) / 3.0
    cy = sum(p[1] for p in back) / 3.0
    cz = sum(p[2] for p in back) / 3.0
    assert cx == pytest.approx(7.5, abs=1e-6)
    assert cy == pytest.approx(7.5, abs=1e-6)
    assert cz == pytest.approx(7.5, abs=1e-6)


def test_to_poscar_box_no_center():
    els = ['H', 'H']
    coords = [[1.0, 1.0, 1.0], [2.0, 1.0, 1.0]]
    txt = smiles3d.to_poscar_box(els, coords, box=10.0, center=False)
    from vcstudio.external.povray_render import parse_poscar_atoms
    _syms, back = parse_poscar_atoms(txt)
    assert back[0] == pytest.approx([1.0, 1.0, 1.0])


# ── 真实冒烟(仅本机装了 rdkit 时)────────────────────────────────────────────

@pytest.mark.skipif(not _HAS_RDKIT, reason='未安装 rdkit,跳过真实 3D 建模冒烟')
def test_real_smiles_to_3d_ethanol():
    r = smiles3d.smiles_to_3d('CCO', seed=42)
    assert r['ok']
    assert r['formula'] == 'C2H6O'
    assert r['n_atoms'] == 9                          # C2H6O 含显式 H 共 9 原子
    assert len(r['coords']) == 9
    # 复现性:同 seed 两次坐标一致
    r2 = smiles3d.smiles_to_3d('CCO', seed=42)
    assert r2['coords'][0] == pytest.approx(r['coords'][0], abs=1e-6)


@pytest.mark.skipif(not _HAS_RDKIT, reason='未安装 rdkit,跳过真实导出冒烟')
def test_real_smiles_to_3d_to_poscar():
    r = smiles3d.smiles_to_3d('O', seed=1)
    txt = smiles3d.to_poscar_box(r['elements'], r['coords'], box=12.0)
    assert 'Cartesian' in txt and txt.startswith('molecule in 12 A')
