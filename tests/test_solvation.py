"""solvation 显式溶剂化复合物组装测试:确定性 + 间距校验 + 预设 + 健全性。"""
from __future__ import annotations

import re

import numpy as np
import pytest

from vcstudio.generate import molecules, solvation
from vcstudio.generate.poscar import parse_poscar_species


def _mol_size(name):
    return len(molecules.molecule_geometry(name))


# ── 确定性 ────────────────────────────────────────────────────────────────────

def test_deterministic_same_seed():
    a = solvation.build_solvated_complex('Li2S3', [('DOL', 2), ('DME', 1)],
                                         box=18.0, seed=42)
    b = solvation.build_solvated_complex('Li2S3', [('DOL', 2), ('DME', 1)],
                                         box=18.0, seed=42)
    assert a['poscar'] == b['poscar']
    assert a['n_atoms'] == b['n_atoms']


def test_different_seed_changes_structure():
    a = solvation.build_solvated_complex('Li2S3', [('DOL', 2), ('DME', 1)],
                                         box=18.0, seed=1)
    b = solvation.build_solvated_complex('Li2S3', [('DOL', 2), ('DME', 1)],
                                         box=18.0, seed=999)
    assert a['poscar'] != b['poscar']


# ── 原子数 / POSCAR 往返 ──────────────────────────────────────────────────────

def test_atom_count_and_poscar_roundtrip():
    res = solvation.build_solvated_complex('Li2S3', [('DOL', 2), ('DME', 1)],
                                           box=18.0, seed=42)
    expect = _mol_size('Li2S3') + 2 * _mol_size('DOL') + _mol_size('DME')
    assert res['n_atoms'] == expect
    els, counts = parse_poscar_species(res['poscar'])
    assert sum(counts) == expect
    assert set(els) <= {'Li', 'S', 'C', 'O', 'H'}
    assert dict(zip(els, counts)).get('Li') == 2      # Li2S3 提供 2 个 Li


# ── 间距校验(健全性)──────────────────────────────────────────────────────────

def test_interfragment_spacing_respects_min_sep():
    min_sep = 2.5
    res = solvation.build_solvated_complex('Li2S3', [('DOL', 2), ('DME', 1)],
                                           box=20.0, min_sep=min_sep, seed=42)
    # note 报告"片段间实测最小间距";组装强制 ≥ min_sep,健全下限 1.5
    m = re.search(r'片段间实测最小间距 ([\d.]+)', res['note'])
    assert m, res['note']
    dmin = float(m.group(1))
    assert dmin >= min_sep - 1e-6
    assert dmin > 1.5


def test_min_interfragment_helper():
    # 两簇分离:跨片段最近对是 x=1 与 x=5,间距 4.0(片段内 0-1 / 5-6 不计)
    coords = np.array([[0., 0., 0.], [1., 0., 0.],      # 片段 0
                       [5., 0., 0.], [6., 0., 0.]])     # 片段 1
    d = solvation._min_interfragment(coords, [[0, 1], [2, 3]])
    assert d == pytest.approx(4.0)


def test_impossible_packing_raises():
    # 极大 min_sep 于小盒 → 找不到无叠置位
    with pytest.raises(ValueError):
        solvation.build_solvated_complex('Li2S3', [('DME', 3)],
                                         box=10.0, min_sep=9.0, seed=42)


# ── 预设 / 校验 ───────────────────────────────────────────────────────────────

def test_solvent_presets_registry():
    assert solvation.SOLVENT_PRESETS['lis_electrolyte'] == [('DOL', 2), ('DME', 1)]


def test_build_from_preset_name():
    res = solvation.build_solvated_complex('Li2S3', 'lis_electrolyte',
                                           box=18.0, seed=5)
    assert res['n_atoms'] == _mol_size('Li2S3') + 2 * _mol_size('DOL') + _mol_size('DME')


def test_unknown_preset_raises():
    with pytest.raises(ValueError):
        solvation.build_solvated_complex('Li2S3', 'not_a_preset', box=18.0)


def test_unknown_core_molecule_raises():
    with pytest.raises(ValueError):
        solvation.build_solvated_complex('Xx99', [('DOL', 1)], box=18.0)


def test_nonpositive_box_raises():
    with pytest.raises(ValueError):
        solvation.build_solvated_complex('Li2S3', [('DOL', 1)], box=0.0)


def test_core_only_no_solvents():
    res = solvation.build_solvated_complex('Li2S3', [], box=15.0, seed=3)
    assert res['n_atoms'] == _mol_size('Li2S3')
