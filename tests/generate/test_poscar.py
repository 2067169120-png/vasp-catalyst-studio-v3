"""generate/poscar.py:物种解析、晶格矢量(修正缩放因子)、读文件。"""
import pytest

from vcstudio.generate import poscar

# VASP5:第6行元素、第7行计数;scale=1.0
VASP5 = """Fe C slab
1.0
3.0 0.0 0.0
0.0 3.0 0.0
0.0 0.0 15.0
Fe C
2 4
Direct
0.0 0.0 0.0
"""

# VASP5,scale=2.0(测缩放因子线性放大)
VASP5_SCALE2 = """title
2.0
3.0 0.0 0.0
0.0 3.0 0.0
0.0 0.0 15.0
Fe C
2 4
"""

# VASP4:无元素符号行,第6行即计数
VASP4 = """title
1.0
3.0 0.0 0.0
0.0 3.0 0.0
0.0 0.0 15.0
2 4
Direct
"""

# VASP5 但计数行是符号(畸形)→ elements 有、counts 空
VASP5_BADCOUNT = """title
1.0
3.0 0.0 0.0
0.0 3.0 0.0
0.0 0.0 15.0
Fe C
Fe C
"""

# 负 scale = 目标体积(diag(2)^3 体积=8;s=-64 → V=64 → factor=(64/8)^(1/3)=2)
VASP_NEGSCALE = """title
-64.0
2.0 0.0 0.0
0.0 2.0 0.0
0.0 0.0 2.0
Fe
1
"""


# ── parse_poscar_species ─────────────────────────────────────────────────────
def test_parse_species_vasp5():
    assert poscar.parse_poscar_species(VASP5) == (['Fe', 'C'], [2, 4])


def test_parse_species_vasp4_degrades_empty():
    assert poscar.parse_poscar_species(VASP4) == ([], [])


def test_parse_species_too_short_degrades_empty():
    assert poscar.parse_poscar_species('title\n1.0\n3 0 0\n') == ([], [])


def test_parse_species_bad_counts_keeps_elements():
    elems, counts = poscar.parse_poscar_species(VASP5_BADCOUNT)
    assert elems == ['Fe', 'C']
    assert counts == []


# ── read_cell_vectors ────────────────────────────────────────────────────────
def test_cell_vectors_scale_one():
    assert poscar.read_cell_vectors(VASP5) == [[3.0, 0.0, 0.0],
                                               [0.0, 3.0, 0.0],
                                               [0.0, 0.0, 15.0]]


def test_cell_vectors_scale_multiplies():
    # scale=2.0 → 每个分量 ×2(E: convergence 漏乘,此处修正)
    assert poscar.read_cell_vectors(VASP5_SCALE2) == [[6.0, 0.0, 0.0],
                                                      [0.0, 6.0, 0.0],
                                                      [0.0, 0.0, 30.0]]


def test_cell_vectors_too_few_lines_raises():
    with pytest.raises(ValueError):
        poscar.read_cell_vectors('title\n1.0\n3 0 0\n')


def test_cell_vectors_zero_scale_raises():
    # scale=0 无物理意义 → 报错(审 P2-2)
    with pytest.raises(ValueError):
        poscar.read_cell_vectors('t\n0.0\n2 0 0\n0 2 0\n0 0 2\nFe\n1\n')


@pytest.mark.xfail(reason='负 scale=目标体积 未实现;M1 用 factor=1.0 占位', strict=True)
def test_cell_vectors_negative_scale_is_target_volume():
    assert poscar.read_cell_vectors(VASP_NEGSCALE) == [[4.0, 0.0, 0.0],
                                                       [0.0, 4.0, 0.0],
                                                       [0.0, 0.0, 4.0]]


# ── read_poscar ──────────────────────────────────────────────────────────────
def test_read_poscar_returns_text(tmp_path):
    p = tmp_path / 'POSCAR'
    p.write_text(VASP5, encoding='utf-8')
    assert poscar.read_poscar(str(p)) == VASP5
