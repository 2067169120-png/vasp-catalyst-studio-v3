"""通用表面/几何吸附位点工作台的纯 Python 合同测试。"""
from __future__ import annotations

import sys

import numpy as np
import pytest

import vcstudio.generate.surface_workbench as surface_workbench
from vcstudio.generate.structure_view import parse_positions
from vcstudio.generate.surface_workbench import (
    build_slabs,
    enumerate_terminations,
    explore_sites,
    structure_hash,
)


@pytest.fixture(autouse=True)
def _keep_numpy_in_sys_modules():
    """避免其他测试移除 numpy 后触发惰性子模块重载。"""
    sys.modules.setdefault("numpy", np)
    yield


_SIMPLE_CUBIC = """simple cubic
1.0
3.0 0.0 0.0
0.0 3.0 0.0
0.0 0.0 3.0
Cu
1
Direct
0.0 0.0 0.0
"""

_ORTHORHOMBIC = """orthorhombic
1.0
2.0 0.0 0.0
0.0 3.0 0.0
0.0 0.0 4.0
Cu
1
Direct
0.0 0.0 0.0
"""

_TWO_TERMINATIONS = """binary cubic
1.0
4.0 0.0 0.0
0.0 4.0 0.0
0.0 0.0 4.0
Na Cl
1 1
Direct
0.0 0.0 0.0
0.5 0.5 0.5
"""

_TWO_IDENTICAL_LAYER_REGISTRIES = """two identical Cu layer registries
1.0
4.0 0.0 0.0
0.0 4.0 0.0
0.0 0.0 4.0
Cu
2
Direct
0.0 0.0 0.0
0.0 0.0 0.5
"""

_NON_ORTHOGONAL = """skew bulk
1.0
3.0 0.0 0.0
1.0 3.0 0.0
0.0 0.0 3.0
Cu
1
Direct
0.0 0.0 0.0
"""

_TWO_BY_TWO_SLAB = """2x2 square slab
1.0
4.0 0.0 0.0
0.0 4.0 0.0
0.0 0.0 14.0
Cu
8
Cartesian
0.0 0.0 5.0
2.0 0.0 5.0
0.0 2.0 5.0
2.0 2.0 5.0
0.0 0.0 7.0
2.0 0.0 7.0
0.0 2.0 7.0
2.0 2.0 7.0
"""

_H_ATOM = """H atom
1.0
10.0 0.0 0.0
0.0 10.0 0.0
0.0 0.0 10.0
H
1
Cartesian
5.0 5.0 5.0
"""

_CO = """CO molecule
1.0
12.0 0.0 0.0
0.0 12.0 0.0
0.0 0.0 12.0
C O
1 1
Cartesian
6.0 6.0 6.0
6.0 6.0 7.15
"""


def _build_001(**overrides):
    request = {"miller": [0, 0, 1], "layers": 3, "vacuum": 12.0}
    request.update(overrides)
    return build_slabs(_SIMPLE_CUBIC, request)[0]


def test_structure_hash_and_builder_are_deterministic():
    first = _build_001()
    second = _build_001()
    assert first["poscar"] == second["poscar"]
    assert first["structure_hash"] == second["structure_hash"]
    assert first["poscar_sha256"] == second["poscar_sha256"]
    assert first["structure_hash"] == structure_hash(first["poscar"])
    assert first["provenance"]["builder"] == "vcstudio.surface_workbench"
    assert first["provenance"]["builder_version"] == "1.1.0"
    assert first["provenance"]["raw_structure_hash"]
    assert first["scientific_status"] == "geometric_candidate"


def test_structure_hash_ignores_same_species_coordinate_line_order():
    first = """pair
1.0
3 0 0
0 3 0
0 0 3
Cu
2
Direct
0.0 0.0 0.0
0.5 0.5 0.5
"""
    second = first.replace("0.0 0.0 0.0\n0.5 0.5 0.5", "0.5 0.5 0.5\n0.0 0.0 0.0")
    assert structure_hash(first) == structure_hash(second)


def test_termination_enumeration_keeps_each_layer_registry():
    simple = enumerate_terminations(_SIMPLE_CUBIC, [0, 0, 1])
    binary = enumerate_terminations(_TWO_TERMINATIONS, [0, 0, 1])
    assert len(simple) == 1
    assert simple[0]["classification"] == "geometric_termination_candidate"
    assert len(binary) == 2
    assert {tuple(sorted(item["surface_composition"].items())) for item in binary} == {
        (("Na", 1),),
        (("Cl", 1),),
    }
    assert [item["termination_id"] for item in binary] == [
        item["termination_id"] for item in enumerate_terminations(_TWO_TERMINATIONS, [0, 0, 1])
    ]


def test_geometrically_matching_layers_are_hints_not_deleted_terminations():
    records = enumerate_terminations(_TWO_IDENTICAL_LAYER_REGISTRIES, [0, 0, 1])
    candidates = build_slabs(
        _TWO_IDENTICAL_LAYER_REGISTRIES,
        {"miller": [0, 0, 1], "layers": 2},
    )

    assert len(records) == 2
    assert len({record["termination_id"] for record in records}) == 2
    assert len({record["geometric_signature"] for record in records}) == 1
    assert len({record["approximate_equivalence_group_id"] for record in records}) == 1
    assert [record["representative_offset"] for record in records] == [0.0, 0.5]
    assert [record["equivalent_offsets"] for record in records] == [[0.0], [0.5]]
    assert all(record["approximate_equivalent_offsets"] == [0.0, 0.5] for record in records)
    assert all(record["validated_crystallographic_symmetry"] is False for record in records)
    assert all(record["symmetry_mapping_evidence"] is None for record in records)
    assert all(record["registry_entry_preserved"] is True for record in records)
    assert {candidate["bottom_termination_id"] for candidate in candidates} == {
        record["termination_id"] for record in records
    }
    assert {candidate["top_termination_id"] for candidate in candidates} == {
        record["termination_id"] for record in records
    }


def test_opposite_signed_miller_surfaces_keep_distinct_identity_and_provenance():
    positive_terms = enumerate_terminations(_SIMPLE_CUBIC, [0, 0, 2])
    negative_terms = enumerate_terminations(_SIMPLE_CUBIC, [0, 0, -2])
    positive = build_slabs(_SIMPLE_CUBIC, {"miller": [0, 0, 2], "layers": 3})[0]
    negative = build_slabs(_SIMPLE_CUBIC, {"miller": [0, 0, -2], "layers": 3})[0]

    assert positive_terms[0]["miller"] == [0, 0, 1]
    assert negative_terms[0]["miller"] == [0, 0, -1]
    assert positive_terms[0]["termination_id"] != negative_terms[0]["termination_id"]
    assert positive_terms[0]["opposite_miller"] == negative_terms[0]["miller"]
    assert positive["parameters"]["miller"] == [0, 0, 1]
    assert negative["parameters"]["miller"] == [0, 0, -1]
    assert positive["construction_hash"] != negative["construction_hash"]
    assert positive["provenance"]["construction_hash"] == positive["construction_hash"]
    assert negative["provenance"]["construction_hash"] == negative["construction_hash"]
    # structure_hash remains a geometry-only hash and may coincide for a centrosymmetric one-atom slab.
    assert positive["poscar_sha256"] != negative["poscar_sha256"]


def test_strict_cvp_finds_high_shear_image_outside_original_plus_minus_one_box():
    a_vec = np.array([1.0, 0.0, 0.0])
    b_vec = np.array([100.49, 1.0, 0.0])
    expected = np.hypot(0.245, 0.5)

    planar = surface_workbench._pbc_xy_distance((0.0, 0.0), (0.0, 0.5), a_vec, b_vec)
    cell = np.array([a_vec, b_vec, [0.0, 0.0, 12.0]])
    periodic = surface_workbench._periodic_cross_distance(
        np.array([[0.0, 0.0, 6.0]]),
        np.array([[50.245, 0.5, 6.0]]),
        cell,
    )

    assert planar == pytest.approx(expected, abs=1.0e-12)
    assert periodic == pytest.approx(expected, abs=1.0e-12)


def test_general_cubic_111_build_has_requested_layers_vacuum_and_fixed_bottom():
    candidate = build_slabs(
        _SIMPLE_CUBIC,
        {
            "miller": [1, 1, 1],
            "layers": 4,
            "vacuum": 10.0,
            "fixed_layers": 1,
            "surface_sides": "both",
        },
    )[0]
    parsed = parse_positions(candidate["poscar"])
    heights = sorted({round(coord[2], 7) for coord in parsed["coords"]})
    assert len(heights) == 4
    assert heights[1] - heights[0] == pytest.approx(3.0 / np.sqrt(3.0), abs=1.0e-7)
    assert parsed["cell"][2][2] - (heights[-1] - heights[0]) == pytest.approx(10.0)
    assert candidate["parameters"]["surface_sides"] == "both"
    coordinate_lines = [line.split() for line in candidate["poscar"].splitlines()[9:]]
    assert sum(parts[3:] == ["F", "F", "F"] for parts in coordinate_lines) == 1


@pytest.mark.parametrize("miller", ([1, 0, 0], [0, 1, 0], [0, 0, 1], [1, 1, 0], [2, 1, 1]))
def test_orthorhombic_low_index_plane_spacing_matches_reciprocal_geometry(miller):
    candidate = build_slabs(
        _ORTHORHOMBIC,
        {"miller": miller, "layers": 3, "vacuum": 8.0},
    )[0]
    parsed = parse_positions(candidate["poscar"])
    heights = sorted({round(coord[2], 8) for coord in parsed["coords"]})
    expected = 1.0 / np.sqrt(
        (miller[0] / 2.0) ** 2 + (miller[1] / 3.0) ** 2 + (miller[2] / 4.0) ** 2
    )
    assert np.diff(heights) == pytest.approx([expected, expected], abs=1.0e-7)


@pytest.mark.parametrize("miller", ([0, 0, 0], [5, 1, 0], [1.5, 0, 1]))
def test_invalid_or_unsafe_miller_fails_closed(miller):
    with pytest.raises(ValueError, match="Miller|miller|安全实现"):
        build_slabs(_SIMPLE_CUBIC, {"miller": miller})


def test_nonorthogonal_bulk_fails_closed_instead_of_guessing():
    with pytest.raises(ValueError, match="正交体相晶胞"):
        build_slabs(_NON_ORTHOGONAL, {"miller": [0, 0, 1]})


def test_unknown_termination_token_fails_closed():
    with pytest.raises(ValueError, match="termination"):
        build_slabs(
            _SIMPLE_CUBIC,
            {"miller": [0, 0, 1], "termination_id": "term-not-from-this-structure"},
        )


def test_square_surface_enumerates_ontop_bridge_and_hollow_geometric_candidates():
    result = explore_sites(_build_001()["poscar"], {"sides": "top"})
    kinds = [site["kind"] for site in result["sites"]]
    assert kinds.count("ontop") == 1
    assert kinds.count("bridge") == 2
    assert kinds.count("hollow") == 1
    assert all(site["classification"] == "geometric_site_candidate" for site in result["sites"])
    assert all(site["equivalence_kind"] == "geometric_symmetry_candidate" for site in result["sites"])
    assert all(
        group["validated_crystallographic_symmetry"] is False
        for group in result["equivalence_groups"]
    )


def test_equivalent_ontop_sites_group_and_coverage_rounds_transparently():
    result = explore_sites(
        _TWO_BY_TWO_SLAB,
        {"sides": "top", "site_kinds": ["ontop"], "coverage": 0.5},
    )
    assert len(result["sites"]) == 4
    assert len(result["equivalence_groups"]) == 1
    assert len(result["equivalence_groups"][0]["site_ids"]) == 4
    plan = result["coverage_plan"][0]
    assert plan["denominator"] == 4
    assert plan["numerator"] == 2
    assert plan["requested_coverage"] == plan["realized_coverage"] == 0.5


def test_discrete_coverage_generates_the_planned_number_of_adsorbates():
    result = explore_sites(
        _TWO_BY_TWO_SLAB,
        {
            "sides": "top",
            "site_kinds": ["ontop"],
            "coverage": 0.5,
            "height": 2.0,
            "min_distance": 1.0,
        },
        _H_ATOM,
    )
    assert not result["rejections"]
    assert len(result["generated"]) == 1
    generated = result["generated"][0]
    assert generated["coverage"]["numerator"] == 2
    assert generated["coverage"]["denominator"] == 4
    assert generated["natoms"] == 10


def test_both_sides_are_explicit_and_stably_sorted():
    first = explore_sites(
        _build_001()["poscar"], {"sides": "both", "site_kinds": ["ontop"]}
    )
    second = explore_sites(
        _build_001()["poscar"], {"sides": "double", "site_kinds": ["ontop"]}
    )
    assert [site["side"] for site in first["sites"]] == ["top", "bottom"]
    assert [site["site_id"] for site in first["sites"]] == [
        site["site_id"] for site in second["sites"]
    ]


def test_adsorbate_generation_honors_binding_atom_orientation_and_is_deterministic():
    slab = _build_001()["poscar"]
    request = {
        "sides": "top",
        "site_kinds": ["ontop"],
        "binding_atom": "O",
        "orientation": "normal",
        "height": 2.0,
        "min_distance": 1.0,
        "rotations": [180, 0, 180],
    }
    first = explore_sites(slab, request, _CO)
    second = explore_sites(slab, request, _CO)
    assert not first["rejections"]
    assert [item["rotation_deg"] for item in first["generated"]] == [0.0, 180.0]
    assert [item["structure_hash"] for item in first["generated"]] == [
        item["structure_hash"] for item in second["generated"]
    ]
    for item in first["generated"]:
        assert item["poscar"] is not None
        assert item["min_distance"] >= 1.0
        assert item["scientific_status"] == "geometric_candidate"


def test_adsorbate_merge_preserves_slab_selective_dynamics_flags():
    slab = _build_001(fixed_layers=1)["poscar"]
    result = explore_sites(
        slab,
        {
            "sides": "top",
            "site_kinds": ["ontop"],
            "height": 2.0,
            "min_distance": 1.0,
        },
        _H_ATOM,
    )
    assert len(result["generated"]) == 1
    merged = result["generated"][0]["poscar"]
    assert merged.splitlines().count("Selective dynamics") == 1
    flags = [line.split()[3:] for line in merged.splitlines()[9:]]
    assert flags.count(["F", "F", "F"]) == 1
    assert flags.count(["T", "T", "T"]) == 3


def test_collision_rejection_returns_no_generated_poscar():
    result = explore_sites(
        _build_001()["poscar"],
        {
            "sides": "top",
            "site_kinds": ["ontop"],
            "height": 0.2,
            "min_distance": 1.0,
        },
        _H_ATOM,
    )
    assert result["generated"] == []
    assert len(result["rejections"]) == 1
    assert result["rejections"][0]["poscar"] is None
    assert "minimum distance" in result["rejections"][0]["reason"]
    assert result["rejections"][0]["classification"] == "geometry_rejected_fail_closed"


def test_unreasonably_low_collision_threshold_is_rejected():
    with pytest.raises(ValueError, match="不得低于"):
        explore_sites(
            _build_001()["poscar"],
            {"sides": "top", "min_distance": 0.2},
            _H_ATOM,
        )
