from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from vcstudio.project import research_recipes
from vcstudio.project.catalysis_contracts import CatalysisContractError, DomainEnvelope


EXPECTED_RECIPES = {
    "adsorption_energy",
    "site_screening",
    "neb_path",
    "vibrational_thermochemistry",
    "convergence_scan",
}


def _complete_request(recipe):
    return {
        "overrides": {},
        "evidence": {
            item.input_id: {
                "ref_type": item.evidence_type,
                "opaque_id": f"evidence-{index}",
                "origin": ("imported" if item.evidence_type in {
                    "publication_record", "imported_record"} else "observed"),
                "revision_id": "revision-1",
            }
            for index, item in enumerate(recipe.inputs, start=1)
        },
    }


def test_catalog_has_five_versioned_bilingual_detached_recipes():
    first = research_recipes.catalog()
    second = research_recipes.catalog()
    assert first == second
    assert first["schema"] == research_recipes.CATALOG_SCHEMA
    assert first["recipe_count"] == 5
    assert {item["recipe_id"] for item in first["recipes"]} == EXPECTED_RECIPES
    assert all(item["recipe_version"] == "1.0.0" for item in first["recipes"])
    assert all(item["label_zh"] and item["label_en"] for item in first["recipes"])
    assert all(item["nodes"] and item["scientific_limits"]
               and item["official_reference_urls"] for item in first["recipes"])
    assert first["read_only"] is True
    assert first["authorizes_execution"] is False
    first["recipes"][0]["nodes"][0]["node_id"] = "tampered"
    assert research_recipes.catalog() == second

    future = replace(research_recipes.get("neb_path"), recipe_version="1.1.0")
    indexed = research_recipes._index_recipes((research_recipes.get("neb_path"), future))
    assert set(indexed["neb_path"]) == {"1.0.0", "1.1.0"}
    envelope = DomainEnvelope.wrap(future)
    assert envelope.schema_version == "1.0.0"
    assert envelope.object_revision_id == "1.1.0"
    assert DomainEnvelope.from_dict(envelope.to_dict()) == envelope


def test_empty_evidence_preview_is_deterministic_blocked_and_side_effect_free(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("dry-run attempted a side effect")

    monkeypatch.setattr("os.makedirs", forbidden)
    monkeypatch.setattr("tempfile.mkdtemp", forbidden)
    first = research_recipes.preview("neb_path", "1.0.0", {
        "overrides": {"intermediate_images": 7}, "evidence": {},
    })
    second = research_recipes.preview("neb_path", "1.0.0", {
        "evidence": {}, "overrides": {"intermediate_images": 7},
    })
    assert first == second
    assert first["status"] == "blocked"
    assert first["parameter_sources"]["intermediate_images"] == "user_override"
    assert first["parameter_sources"]["spring_ev_a2"] == "recipe_default"
    assert first["nodes"][0]["missing_prerequisites"]
    assert first["creates_directories"] is False
    assert first["creates_jobs"] is False
    assert first["remote_side_effects"] is False
    assert first["job_source_of_truth"] == "job.yaml"
    assert first["authorizes_execution"] is False
    assert first["scientific_validation_implied"] is False
    assert len(first["preview_semantic_sha256"]) == 64


@pytest.mark.parametrize("recipe_id", sorted(EXPECTED_RECIPES))
def test_complete_typed_evidence_makes_plan_ready_not_scientifically_validated(recipe_id):
    recipe = research_recipes.get(recipe_id, "1.0.0")
    result = research_recipes.preview(recipe_id, "1.0.0", _complete_request(recipe))
    assert result["status"] == "preview_ready"
    assert all(node["status"] == "ready" for node in result["nodes"])
    assert result["missing_prerequisites"] == []
    assert result["scientific_validation_implied"] is False
    if recipe_id == "site_screening":
        limits = {item["code"]: item for item in result["scientific_limits"]}
        assert "geometry_not_activity" in limits


def test_pin_preview_freezes_version_full_parameters_and_hash():
    recipe = research_recipes.get("convergence_scan", "1.0.0")
    preview = research_recipes.preview(
        recipe.recipe_id, recipe.recipe_version,
        {**_complete_request(recipe), "overrides": {"consecutive_passes": 3}},
    )
    snapshot = research_recipes.pin_preview("run-001", preview)
    wire = snapshot.to_dict()
    assert wire["recipe_version"] == "1.0.0"
    assert set(wire["resolved_parameters"]) == {
        item.parameter_id for item in recipe.parameters
    }
    assert wire["parameter_sources"]["consecutive_passes"] == "user_override"
    assert wire["preview_semantic_sha256"] == preview["preview_semantic_sha256"]
    assert wire["scientific_status"] == "not_validated"
    assert wire["authorizes_execution"] is False

    with pytest.raises(TypeError):
        snapshot.resolved_parameters["consecutive_passes"] = 99
    detached = snapshot.to_dict()
    detached["resolved_parameters"]["consecutive_passes"] = 99
    assert snapshot.to_dict()["resolved_parameters"]["consecutive_passes"] == 3

    tampered = copy.deepcopy(preview)
    tampered["resolved_parameters"]["consecutive_passes"] = 99
    with pytest.raises(CatalysisContractError, match="hash mismatch"):
        research_recipes.pin_preview("run-002", tampered)

    forged = copy.deepcopy(preview)
    forged["resolved_parameters"]["consecutive_passes"] = 19
    forged["status"] = "preview_ready"
    forged["preview_semantic_sha256"] = research_recipes.semantic_hash({
        key: value for key, value in forged.items()
        if key != "preview_semantic_sha256"
    })
    with pytest.raises(CatalysisContractError, match="recipe resolution"):
        research_recipes.pin_preview("run-003", forged)

    with pytest.raises(CatalysisContractError):
        type(snapshot)(
            run_id="run-secret", recipe_id=snapshot.recipe_id,
            recipe_version=snapshot.recipe_version,
            recipe_semantic_sha256=snapshot.recipe_semantic_sha256,
            resolved_parameters={"github_api_key": "needle-secret"},
            parameter_sources={"github_api_key": "user_override"},
            input_evidence={},
            preview_semantic_sha256=snapshot.preview_semantic_sha256,
            plan_status="blocked",
        )
    with pytest.raises(CatalysisContractError, match="binding mismatch"):
        type(snapshot)(
            run_id="run-forged", recipe_id=snapshot.recipe_id,
            recipe_version=snapshot.recipe_version,
            recipe_semantic_sha256=snapshot.recipe_semantic_sha256,
            resolved_parameters=snapshot.to_dict()["resolved_parameters"],
            parameter_sources=snapshot.to_dict()["parameter_sources"],
            input_evidence=snapshot.to_dict()["input_evidence"],
            preview_semantic_sha256="b" * 64,
            plan_status=snapshot.plan_status,
        )


@pytest.mark.parametrize("preview_request", [
    {"project_path": r"C:\\private"},
    {"overrides": {"password": "needle"}},
    {"evidence": {"initial_state": {
        "ref_type": "structure_record", "opaque_id": r"C:\\private",
        "origin": "observed", "revision_id": None,
    }}},
])
def test_preview_rejects_paths_secrets_and_unknown_fields(preview_request):
    with pytest.raises(CatalysisContractError):
        research_recipes.preview("neb_path", "1.0.0", preview_request)


def test_wrong_evidence_type_and_unavailable_version_fail_closed():
    with pytest.raises(CatalysisContractError, match="type"):
        research_recipes.preview("neb_path", "1.0.0", {"evidence": {
            "initial_state": {
                "ref_type": "publication_record", "opaque_id": "evidence-1",
                "origin": "imported", "revision_id": None,
            },
        }})
    with pytest.raises(CatalysisContractError, match="unavailable"):
        research_recipes.get("neb_path", "9.0.0")
    with pytest.raises(CatalysisContractError, match="minimum"):
        research_recipes.preview("convergence_scan", "1.0.0", {
            "overrides": {"encut_ev": [0]}, "evidence": {},
        })
