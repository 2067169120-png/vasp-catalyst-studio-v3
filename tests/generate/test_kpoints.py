"""generate/kpoints.py:KPOINTS 网格推荐 + KPOINTS 文件体。"""
from vcstudio.generate import kpoints


def _diag(a, b, c):
    return [[a, 0.0, 0.0], [0.0, b, 0.0], [0.0, 0.0, c]]


def test_molecule_is_gamma_only():
    assert kpoints.recommend_kpoints(_diag(12, 12, 12), 'molecule') == [1, 1, 1]


def test_slab_forces_kz_one():
    # L=3 → ceil(1/(0.03·3))=12 → 奇数化 13 → cap 9;L=15 → 3,但 slab 强制 kz=1
    assert kpoints.recommend_kpoints(_diag(3, 3, 15), 'slab') == [9, 9, 1]


def test_bulk_keeps_kz():
    # 同 cell 的 bulk:kz 保留 = 3(不强制 1)
    assert kpoints.recommend_kpoints(_diag(3, 3, 15), 'bulk') == [9, 9, 3]


def test_odd_ization():
    # L=10 → ceil(3.33)=4(偶)→ +1 = 5
    assert kpoints.recommend_kpoints(_diag(10, 10, 10), 'bulk') == [5, 5, 5]


def test_cap_at_nine():
    # L=3 → 12 → 13 → cap 9
    assert kpoints.recommend_kpoints(_diag(3, 3, 3), 'bulk') == [9, 9, 9]


def test_kpoints_str_format():
    assert kpoints.kpoints_str([3, 3, 1]) == 'Automatic\n0\nGamma\n3 3 1\n0 0 0\n'
