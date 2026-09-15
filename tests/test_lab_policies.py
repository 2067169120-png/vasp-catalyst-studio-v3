from __future__ import annotations

import copy

import pytest

from vcstudio.project import lab_policies


def test_catalog_is_detached_bilingual_and_marks_recommendation_boundary():
    first = lab_policies.catalog()
    second = lab_policies.catalog()
    assert first == second
    assert len(first["policies"]) >= 3
    assert all(item["label_zh"] and item["label_en"] for item in first["policies"])
    mojibake_markers = ("鍛", "绾", "銆", "€", "?,")
    for item in first["policies"]:
        chinese_copy = item["label_zh"] + item["description_zh"]
        assert not any(marker in chinese_copy for marker in mojibake_markers)
    assert all(item["recommendation_only"] is True for item in first["policies"])
    first["policies"][0]["method"]["functional"] = "tampered"
    assert lab_policies.catalog() == second


def test_resolve_is_deterministic_and_separates_explicit_overrides():
    left = lab_policies.resolve(
        "surface-production",
        {"cores": 64, "walltime": "48:00:00", "force_tolerance_eV_A": 0.01},
        applicability="adsorption",
    )
    right = lab_policies.resolve(
        "surface-production",
        {"force_tolerance_eV_A": 0.01, "walltime": "48:00:00", "cores": 64},
        applicability="adsorption",
    )
    assert left == right
    assert left["resources"] == {"cores": 64, "walltime": "48:00:00"}
    assert left["overrides"] == {
        "cores": 64, "force_tolerance_eV_A": 0.01, "walltime": "48:00:00"
    }
    assert left["recommendation_only"] is True
    assert left["requires_user_confirmation"] is True


@pytest.mark.parametrize("overrides", [
    {"password": "secret"}, {"apiKey": "secret"}, {"remote_root": "/tmp"},
    {"command": "rm -rf"}, {"cores": 0}, {"cores": 2.5},
    {"walltime": "24:99:00"}, {"encut_enmax_multiplier": float("nan")},
    {"unknown": "x"},
    {"functional": "ghp_abcdefghijk"},
    {"functional": r"C:\\private\\method"},
    {"kpoint_policy": r"\\server\\share"},
    {"dispersion_policy": "file:///private/policy"},
    {"kpoint_policy": "../private"},
])
def test_override_boundary_rejects_paths_secrets_and_invalid_ranges(overrides):
    with pytest.raises(lab_policies.LabPolicyError):
        lab_policies.resolve("screening-pilot", overrides)


def test_wrong_applicability_and_unknown_policy_fail_closed():
    with pytest.raises(lab_policies.LabPolicyError, match="applicability"):
        lab_policies.resolve("surface-production", applicability="molecule")
    with pytest.raises(lab_policies.LabPolicyError, match="unknown"):
        lab_policies.resolve("not-a-policy")


def test_hash_changes_for_scientific_or_resource_override_only():
    base = lab_policies.resolve("reproducible-periodic-baseline")
    method = lab_policies.resolve(
        "reproducible-periodic-baseline", {"energy_tolerance_eV": 1e-6}
    )
    resource = lab_policies.resolve(
        "reproducible-periodic-baseline", {"cores": 48}
    )
    assert len({base["semantic_sha256"], method["semantic_sha256"],
                resource["semantic_sha256"]}) == 3
    assert copy.deepcopy(base) == base
