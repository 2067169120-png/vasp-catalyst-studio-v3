from __future__ import annotations

import copy
import json
from types import SimpleNamespace

from tests.test_analysis_workbench import _analysis_api, _assert_public
from tests.test_catalysis_authoring import _surface
from tests.test_kinetics_projection import _real_fluid_projection
from vcstudio.project.catalysis_contracts import DomainEnvelope
from vcstudio.project.catalysis_projection import CatalysisProjectionAuthority
from vcstudio.project.catalysis_runtime import (
    ProductionCatalysisAuthoringServiceFactory,
    ProductionKineticsAuthoringServiceFactory,
    ProductionReactionDomainSource,
)
from vcstudio.gui_web.api import Api


def _ready_authority(root):
    projection = _real_fluid_projection()
    authority = CatalysisProjectionAuthority(root)
    for envelope in (
            projection["network"], *projection["surfaces"],
            *projection["states"], *projection["steps"],
            *projection["conditions"]):
        authority.domain_store.put(DomainEnvelope.from_dict(envelope))
    cas = authority.network_head_cas(projection["network"]["object_id"])
    authority.create_binding(
        network_id=cas.network_id,
        intent_id="select-ready-network",
        confirmed=True,
        expected_domain_authority_id=cas.domain_authority_id,
        expected_domain_generation=cas.domain_generation,
        expected_domain_snapshot_sha256=cas.domain_snapshot_sha256,
        expected_network_revision_id=cas.network_revision_id,
        expected_network_semantic_sha256=cas.network_semantic_sha256,
    )
    evidence = {
        "bindings": copy.deepcopy(projection["bindings"]),
        "applicability": copy.deepcopy(projection["applicability"]),
    }
    return authority, evidence


def _api(tmp_path):
    api, paths, projects = _analysis_api(tmp_path)
    project_id = api._workspace_project_id(paths[0], projects[paths[0]])
    root = tmp_path / "a"
    authority, evidence = _ready_authority(root)
    active_source = ProductionReactionDomainSource(
        authority_factory=lambda _root: authority,
        evidence_adapter=lambda **_kwargs: copy.deepcopy(evidence),
    )
    api._reaction_domain_source = active_source
    api._catalysis_authoring_service_factory = (
        ProductionCatalysisAuthoringServiceFactory(
            reaction_source=active_source,
            preview_master_key=b"a" * 32,
        ))
    api._kinetics_authoring_service_factory = (
        ProductionKineticsAuthoringServiceFactory(
            reaction_source=active_source,
            evidence_resolver_factory=lambda _context: (
                lambda reference: f"artifact:{reference}".encode()),
        ))
    return api, project_id, root, authority, active_source


def _active_request(bootstrap, intent_id):
    candidate = bootstrap["active_network"]["candidates"][0]
    return {
        "schema": "vcstudio.catalysis-active-network-preview-request/v1",
        "network_id": candidate["network_head"]["network_id"],
        "intent_id": intent_id,
        "expected_binding": bootstrap["active_network"]["binding_cas"],
        "expected_network_head": candidate["network_head"],
    }


def _active_confirmation(preview):
    return {
        "schema": "vcstudio.catalysis-active-network-confirmation/v1",
        "request_sha256": preview["request_sha256"],
        "preview_sha256": preview["preview_sha256"],
        "issued_at_ms": preview["issued_at_ms"],
        "expires_at_ms": preview["expires_at_ms"],
        "confirmed": True,
    }


def _model_draft(source, *, spec_id="fluid-model", mode="create"):
    reference = source["evidence_catalog"][0]["reference_id"]
    return {
        "schema": "vcstudio.kinetics-model-spec-draft/v1",
        "spec_id": spec_id,
        "mode": mode,
        "rate_law_policy": {
            "activity": "ideal",
            "reversibility": "explicit_reverse",
            "detailed_balance": "enforced",
            "prefactor": "explicit_per_step",
            "electrochemical": "none",
            "reactor": "mean_field_steady_state",
        },
        "assumptions": {
            "mean_field": True,
            "steady_state": True,
            "site_uniformity": "uniform",
            "lateral_interactions": "neglected",
            "mechanism_completeness": "claimed_complete",
            "evidence_ref_ids": [reference],
        },
        "feed_reservoirs": [
            {
                "species_id": species_id,
                "activity": 1.0 if index == 0 else 0.0,
                "unit": "bar",
                "source_ref_id": reference,
            }
            for index, species_id in enumerate(
                source["required_feed_reservoir_ids"])
        ],
        "target_product_ids": source["allowed_target_product_ids"][:1],
        "steps": [
            {
                "step_id": step_id,
                "prefactors": {
                    direction: {
                        "value": 1.0e13,
                        "unit": "s^-1",
                        "source_ref_id": reference,
                    }
                    for direction in ("forward", "reverse")
                },
                "bep": {
                    "used": False,
                    "source_ref_id": None,
                    "parameters_ref_id": None,
                },
                "scaling": {
                    "used": False,
                    "source_ref_id": None,
                    "parameters_ref_id": None,
                },
                "uncertainty_eV": 0.05,
                "evidence_ref_ids": [reference],
            }
            for step_id in source["required_step_ids"]
        ],
        "site_population_totals": [
            {
                "site_type": site_type,
                "value": 1.0,
                "unit": "sites",
                "basis": "surface_unit_cell",
                "evidence_ref_ids": [reference],
            }
            for site_type in source["required_site_type_ids"]
        ],
    }


def _model_confirmation(preview):
    return {
        "schema": "vcstudio.kinetics-authoring-confirmation/v1",
        "intent_id": preview["intent_id"],
        "draft_sha256": preview["draft_sha256"],
        "preview_sha256": preview["preview_sha256"],
        "source_snapshot_sha256": preview["source_cas"]["snapshot_sha256"],
        "store_snapshot_sha256": preview["store_cas"]["snapshot_sha256"],
        "selector_snapshot_sha256": preview["selector_cas"]["snapshot_sha256"],
        "confirmed": True,
    }


def test_five_authoring_apis_are_opaque_path_free_and_replay_exactly(tmp_path):
    api, project_id, root, _authority_value, _kinetics_source = _api(tmp_path)
    store_path = root / ".vcstudio" / "kinetics" / "model-spec" / "store.json"
    assert not store_path.exists()

    bootstrap = api.catalysis_authoring_bootstrap(project_id)

    assert bootstrap["ok"] is True
    assert bootstrap["active_network"]["status"] == "available"
    assert bootstrap["kinetics_authoring"]["status"] == "available"
    assert store_path.is_file()
    source = bootstrap["kinetics_authoring"]["source"]
    assert source["solver_ready"] is True
    encoded_source = json.dumps(source, sort_keys=True)
    assert "artifact_sha256" not in encoded_source
    assert "snapshot_sha256" not in encoded_source
    assert "source_projection_sha256" not in encoded_source

    active_request = _active_request(bootstrap, "active-api-intent")
    files_before_active_preview = {
        path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    active_preview = api.catalysis_active_network_preview(
        project_id, active_request)
    assert active_preview["ok"] is True
    assert active_preview["preview"]["can_confirm"] is True
    assert files_before_active_preview == {
        path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    active_confirmation = _active_confirmation(active_preview["preview"])
    active_confirm = api.catalysis_active_network_confirm(project_id, {
        "request": active_request,
        "confirmation": active_confirmation,
    })
    active_replay = api.catalysis_active_network_confirm(project_id, {
        "request": active_request,
        "confirmation": active_confirmation,
    })
    assert active_confirm["result"]["action"] == "advanced"
    assert active_replay["result"]["action"] == "replayed"

    draft = _model_draft(source)
    files_before_model_preview = {
        path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    model_preview = api.kinetics_model_spec_preview(project_id, {
        "draft": draft,
        "intent_id": "model-api-intent",
    })
    assert model_preview["ok"] is True
    assert model_preview["preview"]["can_confirm"] is True
    assert files_before_model_preview == {
        path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    model_confirmation = _model_confirmation(model_preview["preview"])
    model_confirm = api.kinetics_model_spec_confirm(project_id, {
        "draft": draft,
        "confirmation": model_confirmation,
    })
    model_replay = api.kinetics_model_spec_confirm(project_id, {
        "draft": draft,
        "confirmation": model_confirmation,
    })
    assert model_confirm["result"]["action"] == "committed"
    assert model_replay["result"]["action"] == "replayed"

    for payload in (
            bootstrap, active_preview, active_confirm, active_replay,
            model_preview, model_confirm, model_replay):
        _assert_public(payload, tmp_path)
        wire = json.dumps(payload, ensure_ascii=False).lower()
        assert "preview_master_key" not in wire
        assert "owner_id" not in wire
        assert "authorizes_execution\": true" not in wire


def test_authoring_public_boundary_rejects_paths_secrets_and_stale_cas(tmp_path):
    api, project_id, root, authority, _reaction_source = _api(tmp_path)
    bootstrap = api.catalysis_authoring_bootstrap(project_id)
    active_request = _active_request(bootstrap, "stale-active")
    active_preview = api.catalysis_active_network_preview(
        project_id, active_request)["preview"]
    source = bootstrap["kinetics_authoring"]["source"]
    draft = _model_draft(source, spec_id="stale-model")
    model_preview = api.kinetics_model_spec_preview(project_id, {
        "draft": draft, "intent_id": "stale-model-intent",
    })["preview"]
    authority.domain_store.put(DomainEnvelope.wrap(_surface("surface-late")))
    stale_active = api.catalysis_active_network_confirm(project_id, {
        "request": active_request,
        "confirmation": _active_confirmation(active_preview),
    })
    assert stale_active["result"]["action"] == "conflict"
    assert stale_active["result"]["conflict"]["retry_automatically"] is False

    stale_model = api.kinetics_model_spec_confirm(project_id, {
        "draft": draft,
        "confirmation": _model_confirmation(model_preview),
    })
    assert stale_model["result"]["action"] in {"conflict", "unavailable"}

    leak = f"{root}\\secret-token=super-secret-value"
    invalid = api.kinetics_model_spec_preview(project_id, {
        "draft": {**copy.deepcopy(draft), "project_path": leak},
        "intent_id": "bad-model",
    })
    assert invalid["ok"] is False
    assert invalid["error_code"] == "invalid_authoring_request"
    assert invalid["error_field"] == "draft"
    assert leak not in json.dumps(invalid)
    invalid_identity = api.catalysis_authoring_bootstrap(leak)
    assert invalid_identity["ok"] is False
    assert invalid_identity["error_code"] == "invalid_project_identity"
    assert leak not in json.dumps(invalid_identity)

    _assert_public(stale_active, tmp_path)
    _assert_public(stale_model, tmp_path)
    _assert_public(invalid, tmp_path)
    _assert_public(invalid_identity, tmp_path)


def test_production_authoring_cold_start_uses_registry_root_not_project_yaml(
        tmp_path):
    root = tmp_path / "rootless-project"
    root.mkdir()
    project_path = root / "project.yaml"
    project_path.write_text(
        "schema: vcstudio.project/v1\nname: Rootless project\n",
        encoding="utf-8",
    )
    project = {
        "name": "Rootless project",
        "project_uuid": "a" * 32,
    }
    adsorption = SimpleNamespace(
        list_projects=lambda: [str(project_path)],
        load_project=lambda path: (
            copy.deepcopy(project) if str(path) == str(project_path) else None),
    )
    authority, evidence = _ready_authority(root)
    reaction_source = ProductionReactionDomainSource(
        authority_factory=lambda _root: authority,
        evidence_adapter=lambda **_kwargs: copy.deepcopy(evidence),
    )
    project_id = "project-" + "a" * 32
    api = Api(
        adsorption_mod=adsorption,
        reaction_domain_source=reaction_source,
        catalysis_authoring_service_factory=(
            ProductionCatalysisAuthoringServiceFactory(
                reaction_source=reaction_source,
                preview_master_key=b"b" * 32,
            )),
        kinetics_authoring_service_factory=(
            ProductionKineticsAuthoringServiceFactory(
                reaction_source=reaction_source,
                evidence_resolver_factory=lambda _context: (
                    lambda reference: f"artifact:{reference}".encode()),
            )),
    )

    bootstrap = api.catalysis_authoring_bootstrap(project_id)

    assert bootstrap["ok"] is True
    assert bootstrap["project_id"] == project_id
    assert bootstrap["active_network"]["status"] == "available"
    assert bootstrap["kinetics_authoring"]["model_store"] == {
        "status": "available"}
    assert (
        root / ".vcstudio" / "kinetics" / "model-spec" / "store.json"
    ).is_file()
    _assert_public(bootstrap, tmp_path)


def test_prefactor_choice_catalog_matches_core_and_round_trips_all_units(tmp_path):
    api, project_id, _root, _authority_value, _kinetics_source = _api(tmp_path)
    bootstrap = api.catalysis_authoring_bootstrap(project_id)
    source = bootstrap["kinetics_authoring"]["source"]
    units = bootstrap["kinetics_authoring"]["choice_catalog"][
        "prefactor_units"]

    assert units == ["s^-1", "bar^-1 s^-1", "mol^-1 L s^-1"]
    for index, unit in enumerate(units):
        draft = _model_draft(source, spec_id=f"prefactor-unit-{index}")
        for step in draft["steps"]:
            for direction in ("forward", "reverse"):
                step["prefactors"][direction]["unit"] = unit
        preview = api.kinetics_model_spec_preview(project_id, {
            "draft": draft,
            "intent_id": f"prefactor-unit-{index}",
        })
        assert preview["ok"] is True
        assert preview["preview"]["can_confirm"] is True
        assert preview["preview"]["issues"] == []


def test_combined_bootstrap_freezes_one_domain_generation_across_barrier(
        tmp_path):
    api, paths, projects = _analysis_api(tmp_path)
    project_id = api._workspace_project_id(paths[0], projects[paths[0]])
    root = tmp_path / "a"
    authority, evidence = _ready_authority(root)
    frozen_generation = authority.domain_store.snapshot_heads().generation
    evidence_calls = []

    def evidence_adapter(**kwargs):
        evidence_calls.append(
            kwargs["catalysis_snapshot"]["domain_authority"]["generation"])
        if len(evidence_calls) == 1:
            authority.domain_store.put(DomainEnvelope.wrap(
                _surface("surface-after-frozen-bootstrap")))
        return copy.deepcopy(evidence)

    reaction_source = ProductionReactionDomainSource(
        authority_factory=lambda _root: authority,
        evidence_adapter=evidence_adapter,
    )
    active_factory = ProductionCatalysisAuthoringServiceFactory(
        reaction_source=reaction_source,
        preview_master_key=b"c" * 32,
    )
    kinetics_factory = ProductionKineticsAuthoringServiceFactory(
        reaction_source=reaction_source,
        evidence_resolver_factory=lambda _context: (
            lambda reference: f"artifact:{reference}".encode()),
    )
    validated_generations = []
    original_bootstrap = kinetics_factory.bootstrap_from_validated

    def capture_bootstrap(private_context, validated_reaction, **kwargs):
        validated_generations.append(validated_reaction.domain_generation)
        return original_bootstrap(
            private_context, validated_reaction, **kwargs)

    kinetics_factory.bootstrap_from_validated = capture_bootstrap
    api._reaction_domain_source = reaction_source
    api._catalysis_authoring_service_factory = active_factory
    api._kinetics_authoring_service_factory = kinetics_factory

    bootstrap = api.catalysis_authoring_bootstrap(project_id)

    assert bootstrap["ok"] is True
    assert bootstrap["bootstrap_status"] == "available"
    assert bootstrap["reason"] is None
    assert bootstrap["shared_authority"]["domain_generation"] == (
        frozen_generation)
    assert bootstrap["active_network"]["domain_cas"]["generation"] == (
        frozen_generation)
    assert bootstrap["kinetics_authoring"]["source"] is not None
    assert bootstrap["kinetics_authoring"]["source"]["solver_ready"] is True
    assert evidence_calls == [frozen_generation]
    assert validated_generations == [frozen_generation]
    assert authority.domain_store.snapshot_heads().generation == (
        frozen_generation + 1)
    _assert_public(bootstrap, tmp_path)


def test_combined_bootstrap_rejects_stale_binding_without_live_splice(tmp_path):
    api, paths, projects = _analysis_api(tmp_path)
    project_id = api._workspace_project_id(paths[0], projects[paths[0]])
    root = tmp_path / "a"
    authority, evidence = _ready_authority(root)
    authority.domain_store.put(DomainEnvelope.wrap(
        _surface("surface-before-stale-bootstrap")))
    evidence_calls = []

    def evidence_adapter(**_kwargs):
        evidence_calls.append(True)
        return copy.deepcopy(evidence)

    reaction_source = ProductionReactionDomainSource(
        authority_factory=lambda _root: authority,
        evidence_adapter=evidence_adapter,
    )
    active_factory = ProductionCatalysisAuthoringServiceFactory(
        reaction_source=reaction_source,
        preview_master_key=b"d" * 32,
    )
    kinetics_factory = ProductionKineticsAuthoringServiceFactory(
        reaction_source=reaction_source,
        evidence_resolver_factory=lambda _context: (
            lambda reference: f"artifact:{reference}".encode()),
    )
    api._reaction_domain_source = reaction_source
    api._catalysis_authoring_service_factory = active_factory
    api._kinetics_authoring_service_factory = kinetics_factory

    bootstrap = api.catalysis_authoring_bootstrap(project_id)

    assert bootstrap["ok"] is True
    assert bootstrap["bootstrap_status"] == "conflict"
    assert bootstrap["reason"] == "stale_active_network_binding"
    assert bootstrap["kinetics_authoring"]["status"] == "conflict"
    assert bootstrap["kinetics_authoring"]["source"] is None
    assert evidence_calls == []
    _assert_public(bootstrap, tmp_path)


def test_default_blocked_evidence_and_missing_resolver_stay_explicit(tmp_path):
    api, paths, projects = _analysis_api(tmp_path)
    project_id = api._workspace_project_id(paths[0], projects[paths[0]])
    root = tmp_path / "a"
    authority, _evidence = _ready_authority(root)
    reaction_source = ProductionReactionDomainSource(
        authority_factory=lambda _root: authority)
    api._reaction_domain_source = reaction_source
    api._catalysis_authoring_service_factory = (
        ProductionCatalysisAuthoringServiceFactory(
            reaction_source=reaction_source,
            preview_master_key=b"e" * 32,
        ))
    api._kinetics_authoring_service_factory = (
        ProductionKineticsAuthoringServiceFactory(
            reaction_source=reaction_source))

    bootstrap = api.catalysis_authoring_bootstrap(project_id)

    assert bootstrap["ok"] is True
    assert bootstrap["bootstrap_status"] == "missing_prerequisite"
    assert bootstrap["reason"] == "evidence_resolver_unavailable"
    kinetics = bootstrap["kinetics_authoring"]
    assert kinetics["status"] == "missing_prerequisite"
    assert kinetics["reason"] == "evidence_resolver_unavailable"
    assert kinetics["source"]["solver_ready"] is False
    assert "frozen_reaction_not_solver_ready" in (
        kinetics["source"]["solver_readiness_reasons"])
    assert "evidence_resolver_unavailable" in (
        kinetics["source"]["solver_readiness_reasons"])
    assert "artifact_sha256" not in json.dumps(bootstrap, sort_keys=True)
    _assert_public(bootstrap, tmp_path)
