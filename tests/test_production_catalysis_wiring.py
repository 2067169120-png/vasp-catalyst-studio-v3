from __future__ import annotations

import copy
import hashlib
import json
import sys
from dataclasses import replace
from types import MappingProxyType, SimpleNamespace

import pytest

from vcstudio.project.catalysis_contracts import (
    AdsorbateState,
    CatalystSurface,
    ConditionSet,
    DomainEnvelope,
    ElementaryStep,
    EvidenceRef,
    ExactRational,
    FluidStandardState,
    FluidState,
    MethodFingerprint,
    ReactionNetwork,
    ReactionParticipant,
)
from vcstudio.project.catalysis_projection import CatalysisProjectionAuthority
from vcstudio.project.catalysis_runtime import (
    FixedActiveKineticsSpecSelector,
    ProductionCatalysisAuthoringServiceFactory,
    ProductionKineticsAuthoringServiceFactory,
    ProductionKineticsProviderFactory,
    ProductionReactionDomainSource,
)
from vcstudio.project.reaction_workbench import build_reaction_workbench_view


def _canonical_sha256(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _refs():
    return (EvidenceRef(
        "calculation_result", "result-1", "observed", "result-revision-1",
    ),)


def _method():
    return MethodFingerprint(
        method_id="method-1", scope="electronic_structure", sha256="a" * 64,
        evidence_refs=(EvidenceRef(
            "method_record", "method-1", "observed", "method-revision-1",
        ),),
    )


def _authority(root, *, cross_surface=False):
    root.mkdir(parents=True, exist_ok=True)
    authority = CatalysisProjectionAuthority(root)
    participant = lambda state_id: ReactionParticipant(  # noqa: E731
        state_id=state_id, coefficient=ExactRational(1), phase="adsorbed",
        charge=0, site_stoichiometry={"top": ExactRational(1)},
    )
    surface_ids = ("surface-1", "surface-2") if cross_surface else ("surface-1",)
    surfaces = tuple(CatalystSurface(
        surface_id=surface_id, composition="Pt", miller_indices=(1, 1, 1),
        termination_id=f"termination-{surface_id}", geometric_site_ids=("top",),
        provenance="observed", evidence_refs=_refs(),
        method_fingerprint=_method(), object_revision_id=f"{surface_id}-r1",
    ) for surface_id in surface_ids)
    values = (
        *surfaces,
        AdsorbateState(
            state_id="state-r", surface_id="surface-1", adsorbate_id="ads-r",
            chemical_formula="CO", geometric_site_id="top", charge=0,
            multiplicity=1, provenance="observed", evidence_refs=_refs(),
            method_fingerprint=_method(), object_revision_id="state-r-r1",
        ),
        AdsorbateState(
            state_id="state-ts", surface_id="surface-1", adsorbate_id="ads-ts",
            chemical_formula="CO", geometric_site_id="top", charge=0,
            multiplicity=1, provenance="observed", evidence_refs=_refs(),
            method_fingerprint=_method(), object_revision_id="state-ts-r1",
        ),
        AdsorbateState(
            state_id="state-p",
            surface_id="surface-2" if cross_surface else "surface-1",
            adsorbate_id="ads-p",
            chemical_formula="CO", geometric_site_id="top", charge=0,
            multiplicity=1, provenance="observed", evidence_refs=_refs(),
            method_fingerprint=_method(), object_revision_id="state-p-r1",
        ),
        ConditionSet(
            condition_set_id="condition-1", temperature_k=300.0,
            pressure_pa=100_000.0, ph=None, electrode_potential_v=None,
            provenance="observed", evidence_refs=_refs(),
            method_fingerprint=_method(), object_revision_id="condition-r1",
        ),
        ElementaryStep(
            step_id="step-1", reactants=(participant("state-r"),),
            transition_state=(participant("state-ts"),),
            products=(participant("state-p"),),
            condition_set_id="condition-1", reversible=True,
            provenance="observed", evidence_refs=_refs(),
            method_fingerprint=_method(), object_revision_id="step-r1",
        ),
        ReactionNetwork(
            network_id="network-1", surface_ids=surface_ids,
            state_ids=("state-r", "state-ts", "state-p"),
            step_ids=("step-1",),
            condition_set_ids=("condition-1",), provenance="observed",
            evidence_refs=_refs(), method_fingerprint=_method(),
            object_revision_id="network-r1",
        ),
    )
    for value in values:
        authority.domain_store.put(DomainEnvelope.wrap(value))
    cas = authority.network_head_cas("network-1")
    authority.create_binding(
        network_id="network-1", intent_id="select-network-1", confirmed=True,
        expected_domain_authority_id=cas.domain_authority_id,
        expected_domain_generation=cas.domain_generation,
        expected_domain_snapshot_sha256=cas.domain_snapshot_sha256,
        expected_network_revision_id=cas.network_revision_id,
        expected_network_semantic_sha256=cas.network_semantic_sha256,
    )
    if not cross_surface:
        assert authority.build_snapshot()["status"] == "available"
    return authority


def _fluid(state_id, *, phase="gas"):
    return FluidState(
        state_id=state_id, phase=phase, chemical_formula="CO", charge=0,
        multiplicity=1, standard_state=FluidStandardState(
            phase=phase, kind="1-bar" if phase == "gas" else "1-molar",
            value=100_000.0 if phase == "gas" else 1.0,
            unit="Pa" if phase == "gas" else "mol/L",
        ),
        provenance="observed", evidence_refs=_refs(),
        method_fingerprint=_method(), object_revision_id=f"{state_id}-r1",
    )


def _mixed_authority(root, *, phase_mismatch=False, collision=False):
    root.mkdir(parents=True, exist_ok=True)
    authority = CatalysisProjectionAuthority(root)

    def participant(state_id, *, phase="adsorbed"):
        return ReactionParticipant(
            state_id=state_id, coefficient=ExactRational(1), phase=phase,
            charge=0,
            site_stoichiometry=(
                {"top": ExactRational(1)} if phase == "adsorbed" else {}),
        )

    values = [
        CatalystSurface(
            surface_id="surface-1", composition="Pt", miller_indices=(1, 1, 1),
            termination_id="termination-1", geometric_site_ids=("top",),
            provenance="observed", evidence_refs=_refs(),
            method_fingerprint=_method(), object_revision_id="surface-r1",
        ),
        *(
            AdsorbateState(
                state_id=state_id, surface_id="surface-1",
                adsorbate_id=f"ads-{state_id}", chemical_formula="CO",
                geometric_site_id="top", charge=0, multiplicity=1,
                provenance="observed", evidence_refs=_refs(),
                method_fingerprint=_method(), object_revision_id=f"{state_id}-r1",
            )
            for state_id in ("state-r", "state-ts", "state-p")
        ),
        *(_fluid(state_id) for state_id in ("gas-r", "gas-ts", "gas-p")),
        ConditionSet(
            condition_set_id="condition-1", temperature_k=300.0,
            pressure_pa=100_000.0, ph=None, electrode_potential_v=None,
            provenance="observed", evidence_refs=_refs(),
            method_fingerprint=_method(), object_revision_id="condition-r1",
        ),
        ElementaryStep(
            step_id="step-1",
            reactants=(
                participant("state-r"),
                participant(
                    "gas-r", phase="liquid" if phase_mismatch else "gas"),
            ),
            transition_state=(
                participant("state-ts"), participant("gas-ts", phase="gas"),
            ),
            products=(
                participant("state-p"), participant("gas-p", phase="gas"),
            ),
            condition_set_id="condition-1", reversible=True,
            provenance="observed", evidence_refs=_refs(),
            method_fingerprint=_method(), object_revision_id="step-r1",
        ),
        ReactionNetwork(
            network_id="network-1", surface_ids=("surface-1",),
            state_ids=(
                "state-r", "gas-r", "state-ts", "gas-ts", "state-p", "gas-p"),
            step_ids=("step-1",), condition_set_ids=("condition-1",),
            provenance="observed", evidence_refs=_refs(),
            method_fingerprint=_method(), object_revision_id="network-r1",
        ),
    ]
    if collision:
        values.append(_fluid("state-r"))
    for value in values:
        authority.domain_store.put(DomainEnvelope.wrap(value))
    cas = authority.network_head_cas("network-1")
    authority.create_binding(
        network_id="network-1", intent_id="select-network-1", confirmed=True,
        expected_domain_authority_id=cas.domain_authority_id,
        expected_domain_generation=cas.domain_generation,
        expected_domain_snapshot_sha256=cas.domain_snapshot_sha256,
        expected_network_revision_id=cas.network_revision_id,
        expected_network_semantic_sha256=cas.network_semantic_sha256,
    )
    return authority


def _gapped_snapshot(authority):
    """Inject one negative-path evidence gap without changing the closure."""
    snapshot = authority.build_snapshot()
    assert snapshot["evidence_gaps"] == []
    snapshot["status"] = "unavailable"
    snapshot["evidence_gaps"] = [{
        "status": "unavailable",
        "code": "test_authority_gap",
        "object_type": "ReactionNetwork",
        "object_id": "network-1",
    }]
    snapshot["gap_summary"] = {
        "total": 1, "returned": 1, "omitted": 0,
        "by_code": {"test_authority_gap": 1},
    }
    snapshot.pop("projection_sha256")
    snapshot["projection_sha256"] = _canonical_sha256(snapshot)
    return snapshot


def _workbench_evidence(**_kwargs):
    binding = {
        "scientific_status": "blocked",
        "origin": "observed",
        "evidence_sha256": "b" * 64,
    }
    return {
        "bindings": {
            object_id: copy.deepcopy(binding)
            for object_id in (
                "network-1", "surface-1", "state-r", "state-ts", "state-p",
                "step-1", "condition-1")
        },
        "applicability": {
            key: {
                "schema": "vcstudio.condition-applicability-range/v1",
                "minimum": minimum,
                "maximum": maximum,
                "evidence_sha256": "c" * 64,
                "origin": "observed",
            }
            for key, minimum, maximum in (
                ("temperature_k", 250.0, 500.0),
                ("pressure_pa", 50_000.0, 200_000.0),
            )
        },
    }


class _CountingAuthority:
    def __init__(self, authority, snapshot=None):
        self.authority = authority
        self.snapshot = snapshot
        self.reads = 0

    def build_snapshot(self):
        self.reads += 1
        if self.snapshot is not None:
            return copy.deepcopy(self.snapshot)
        return self.authority.build_snapshot()


class _SequencedAuthority:
    def __init__(self, snapshots):
        self.snapshots = [copy.deepcopy(item) for item in snapshots]
        self.reads = 0

    def build_snapshot(self):
        index = min(self.reads, len(self.snapshots) - 1)
        self.reads += 1
        return copy.deepcopy(self.snapshots[index])


def test_reaction_source_reads_once_and_returns_strict_canonical_v2(tmp_path):
    real_authority = _authority(tmp_path)
    authoritative_snapshot = real_authority.build_snapshot()
    authority = _CountingAuthority(real_authority)
    source = ProductionReactionDomainSource(
        authority_factory=lambda _root: authority,
    )

    projection = source.load_reaction_projection(
        project_id="project-a", project={"root": str(tmp_path)})

    assert authority.reads == 1
    assert projection["schema"] == "vcstudio.reaction-domain-projection/v2"
    assert "transition_states" not in projection
    assert set(projection) == {
        "schema", "project_id", "authority", "network", "surfaces", "states",
        "steps", "conditions", "bindings", "applicability",
    }
    binding = authoritative_snapshot["binding_authority"]["binding"]
    assert projection["authority"] == {
        "domain_authority_id": binding["domain_authority_id"],
        "domain_generation": binding["domain_generation"],
        "domain_snapshot_sha256": binding["domain_snapshot_sha256"],
        "network_revision_id": binding["network_revision_id"],
        "network_semantic_sha256": binding["network_semantic_sha256"],
    }
    assert projection["network"] == authoritative_snapshot["network"]
    for collection in ("surfaces", "states", "steps", "conditions"):
        assert projection[collection] == authoritative_snapshot[collection]
    assert projection["applicability"] == {}
    assert {
        binding["scientific_status"]
        for binding in projection["bindings"].values()
    } == {"blocked"}
    view = build_reaction_workbench_view(
        projection, project_id="project-a", conditions={})
    assert view["migration_only"] is False
    assert view["graph"]["canonical_envelope_authority"] is True
    assert len(view["graph"]["nodes"]) == 4
    assert len(view["graph"]["edges"]) == 1
    assert view["scientific_status"] in {"unknown", "unavailable", "blocked"}
    assert view["frozen_network"]["schema"] == (
        "vcstudio.frozen-reaction-network/v2")
    assert view["frozen_network"]["version"] == "2"
    assert view["frozen_network"]["domain_authority"] == {
        "authority_id": projection["authority"]["domain_authority_id"],
        "generation": projection["authority"]["domain_generation"],
        "snapshot_sha256": projection["authority"]["domain_snapshot_sha256"],
    }
    assert view["frozen_network"]["network_identity"] == {
        "network_id": "network-1",
        "object_revision_id": projection["authority"]["network_revision_id"],
        "semantic_sha256": projection["authority"]["network_semantic_sha256"],
    }
    assert view["frozen_network"]["microkinetics_ready"] is False


def test_mixed_fluid_and_adsorbate_authority_projects_exact_state_union(tmp_path):
    authority = _mixed_authority(tmp_path)
    snapshot = authority.build_snapshot()
    assert snapshot["status"] == "available"
    source = ProductionReactionDomainSource(
        authority_factory=lambda _root: authority)

    projection = source.load_reaction_projection(
        project_id="project-mixed", project={"root": str(tmp_path)})

    assert [item["object_id"] for item in projection["states"]] == [
        "state-r", "gas-r", "state-ts", "gas-ts", "state-p", "gas-p",
    ]
    assert [item["object_type"] for item in projection["states"]] == [
        "AdsorbateState", "FluidState", "AdsorbateState", "FluidState",
        "AdsorbateState", "FluidState",
    ]
    assert set(projection["bindings"]) >= {
        "gas-r", "gas-ts", "gas-p", "state-r", "state-ts", "state-p",
    }
    view = build_reaction_workbench_view(
        projection, project_id="project-mixed", conditions={})
    catalog = {
        item["state_id"]: item["object_type"]
        for item in view["graph"]["state_catalog"]
    }
    assert catalog == {
        "state-r": "AdsorbateState", "gas-r": "FluidState",
        "state-ts": "AdsorbateState", "gas-ts": "FluidState",
        "state-p": "AdsorbateState", "gas-p": "FluidState",
    }
    assert view["frozen_network"]["state_catalog"] == (
        view["graph"]["state_catalog"])
    assert view["frozen_network"]["microkinetics_ready"] is False


def test_fluid_collision_and_phase_mismatch_fail_closed(tmp_path):
    collision_root = tmp_path / "collision"
    collision = _mixed_authority(collision_root, collision=True)
    collision_snapshot = collision.build_snapshot()
    assert collision_snapshot["status"] == "unavailable"
    assert "network_state_type_collision" in {
        item["code"] for item in collision_snapshot["evidence_gaps"]}
    assert ProductionReactionDomainSource(
        authority_factory=lambda _root: collision,
    ).load_reaction_projection(
        project_id="project-collision",
        project={"root": str(collision_root)},
    ) is None

    mismatch_root = tmp_path / "phase-mismatch"
    mismatch = _mixed_authority(mismatch_root, phase_mismatch=True)
    mismatch_snapshot = mismatch.build_snapshot()
    assert mismatch_snapshot["status"] == "unavailable"
    assert "participant_fluid_phase_disagrees" in {
        item["code"] for item in mismatch_snapshot["evidence_gaps"]}
    assert ProductionReactionDomainSource(
        authority_factory=lambda _root: mismatch,
    ).load_reaction_projection(
        project_id="project-phase-mismatch",
        project={"root": str(mismatch_root)},
    ) is None


def test_cross_surface_same_site_fails_authority_before_runtime_projection(tmp_path):
    authority = _authority(tmp_path, cross_surface=True)
    snapshot = authority.build_snapshot()
    assert snapshot["status"] == "unavailable"
    assert "formal_conservation_failed" in {
        item["code"] for item in snapshot["evidence_gaps"]}

    source = ProductionReactionDomainSource(
        authority_factory=lambda _root: authority)
    assert source.load_reaction_projection(
        project_id="project-cross-surface",
        project={"root": str(tmp_path)},
    ) is None

def test_runtime_rejects_cross_type_collision_even_if_snapshot_claims_available(
        tmp_path):
    authority = _mixed_authority(tmp_path)
    snapshot = authority.build_snapshot()
    snapshot["states"].insert(
        1, DomainEnvelope.wrap(_fluid("state-r")).to_dict())
    snapshot.pop("projection_sha256")
    snapshot["projection_sha256"] = _canonical_sha256(snapshot)
    forged = _CountingAuthority(authority, snapshot)
    source = ProductionReactionDomainSource(
        authority_factory=lambda _root: forged)

    assert source.load_reaction_projection(
        project_id="project-forged-collision",
        project={"root": str(tmp_path)},
    ) is None
    assert forged.reads == 1


def test_reaction_source_missing_authorities_and_evidence_fail_closed(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    source = ProductionReactionDomainSource(evidence_adapter=_workbench_evidence)
    assert source.load_reaction_projection(
        project_id="project-empty", project={"root": str(empty)}) is None
    assert not (empty / ".vcstudio").exists()

    unbound = tmp_path / "unbound"
    unbound.mkdir()
    CatalysisProjectionAuthority(unbound)
    assert ProductionReactionDomainSource(
        evidence_adapter=_workbench_evidence,
    ).load_reaction_projection(
        project_id="project-unbound", project={"root": str(unbound)}) is None

    bound = tmp_path / "bound"
    bound_authority = _authority(bound)
    assert ProductionReactionDomainSource(
        authority_factory=lambda _root: _CountingAuthority(
            bound_authority, _gapped_snapshot(bound_authority)),
    ).load_reaction_projection(
        project_id="project-gapped", project={"root": str(bound)}) is None
    assert ProductionReactionDomainSource(
        authority_factory=lambda _root: bound_authority,
        evidence_adapter=None,
    ).load_reaction_projection(
        project_id="project-no-adapter", project={"root": str(bound)}) is None

    missing_bindings = ProductionReactionDomainSource(
        authority_factory=lambda _root: _CountingAuthority(
            bound_authority),
        evidence_adapter=lambda **_kwargs: {"bindings": {}, "applicability": {}},
    )
    assert missing_bindings.load_reaction_projection(
        project_id="project-missing", project={"root": str(bound)}) is None


def test_reaction_source_isolates_projects_and_rejects_root_rebinding(tmp_path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    authority_a = _authority(root_a)
    authority_b = _authority(root_b)
    by_root = {
        str(root_a.resolve()): _CountingAuthority(authority_a),
        str(root_b.resolve()): _CountingAuthority(authority_b),
    }
    source = ProductionReactionDomainSource(
        authority_factory=lambda root: by_root[str(root)],
        evidence_adapter=_workbench_evidence,
    )

    projection_a = source.load_reaction_projection(
        project_id="project-a", project={"root": str(root_a)})
    projection_b = source.load_reaction_projection(
        project_id="project-b", project={"root": str(root_b)})

    assert projection_a["project_id"] == "project-a"
    assert projection_b["project_id"] == "project-b"
    assert source.load_reaction_projection(
        project_id="project-a", project={"root": str(root_b)}) is None


def test_full_private_api_context_is_required_when_project_yaml_has_no_root(tmp_path):
    from vcstudio.gui_web.api import Api

    root = tmp_path / "registered-project"
    real_authority = _authority(root)
    project_path = root / "project.yaml"
    project_path.write_text(
        "schema: vcstudio.project/v1\nname: Registered project\n",
        encoding="utf-8",
    )
    project = {
        "name": "Registered project",
        "project_uuid": "a" * 32,
        # Real project.yaml mappings are not required to persist a root field.
    }
    adsorption = SimpleNamespace(
        list_projects=lambda: [str(project_path)],
        load_project=lambda path: (
            copy.deepcopy(project) if str(path) == str(project_path) else None),
    )
    api = Api(adsorption_mod=adsorption)
    source = ProductionReactionDomainSource(
        authority_factory=lambda _root: _CountingAuthority(
            real_authority),
    )
    context = api._report_workbench_project_context(str(project_path))
    assert context["project"] == project
    assert context["project_root"] == str(root.resolve())

    # The legacy protocol loses project_root and therefore must fail closed.
    class LegacyOnly:
        def load_reaction_projection(self, *, project_id, project):
            return source.load_reaction_projection(
                project_id=project_id, project=project)

    api._reaction_domain_source = LegacyOnly()
    assert api._analysis_workbench_reaction_projection(context) is None

    # The narrow production seam consumes the already-resolved private context.
    api._reaction_domain_source = source
    projection = api._analysis_workbench_reaction_projection(context)
    assert projection["project_id"] == context["project_id"]
    assert projection["schema"] == "vcstudio.reaction-domain-projection/v2"

    # Report freshness starts from project identity alone; the API must recover
    # the same private root through its server registry, never project.yaml.
    binding = api._current_reaction_projection_binding(
        context["project"], context["project_id"])
    assert binding["state"] == "present"


def _card(response, analysis_id):
    return next(
        item for item in response["catalog"]["analyses"]
        if item["id"] == analysis_id)


def test_bootstrap_freezes_one_authority_snapshot_and_next_request_refreshes(
        tmp_path):
    from tests.test_analysis_workbench import _analysis_api, _assert_public

    api, paths, projects = _analysis_api(tmp_path)
    root = tmp_path / "a"
    authority = _authority(root)
    sequence = _SequencedAuthority([
        authority.build_snapshot(), _gapped_snapshot(authority),
    ])
    source = ProductionReactionDomainSource(
        authority_factory=lambda _root: sequence)
    api._reaction_domain_source = source
    api._kinetics_projection_provider = ProductionKineticsProviderFactory(
        reaction_source=source)
    project_id = api._workspace_project_id(paths[0], projects[paths[0]])

    first = api.analysis_workbench_bootstrap(project_id, "adsorption-energy")

    assert first["ok"] is True
    assert sequence.reads == 1
    assert _card(first, "free-energy-path")["capability_status"] == "available"
    assert _card(first, "kinetic-dashboard")["capability_status"] == (
        "missing_prerequisite")
    encoded = json.dumps(first, ensure_ascii=False)
    assert "_reaction_snapshot_token" not in encoded
    assert "ValidatedReactionSnapshot" not in encoded
    _assert_public(first, tmp_path)

    second = api.analysis_workbench_bootstrap(project_id, "adsorption-energy")

    assert second["ok"] is True
    assert sequence.reads == 2
    assert _card(second, "free-energy-path")["capability_status"] == (
        "missing_prerequisite")
    assert _card(second, "kinetic-dashboard")["capability_status"] == (
        "missing_prerequisite")
    _assert_public(second, tmp_path)


def test_kinetics_selected_bootstrap_reuses_request_snapshot_once(tmp_path):
    from tests.test_analysis_workbench import _analysis_api, _assert_public

    api, paths, projects = _analysis_api(tmp_path)
    root = tmp_path / "a"
    authority = _authority(root)
    counting = _CountingAuthority(authority)
    source = ProductionReactionDomainSource(
        authority_factory=lambda _root: counting)
    api._reaction_domain_source = source
    api._kinetics_projection_provider = ProductionKineticsProviderFactory(
        reaction_source=source)
    project_id = api._workspace_project_id(paths[0], projects[paths[0]])

    response = api.analysis_workbench_bootstrap(project_id, "kinetic-dashboard")

    assert response["ok"] is True
    assert counting.reads == 1
    assert response["view"]["capability_status"] == "missing_prerequisite"
    assert _card(response, "free-energy-path")["capability_status"] == "available"
    _assert_public(response, tmp_path)

    direct = api.kinetics_export_preview(project_id)
    assert direct["ok"] is False
    assert counting.reads == 2
    _assert_public(direct, tmp_path)


def test_legacy_load_projection_source_remains_compatible(tmp_path):
    from tests.test_analysis_workbench import _analysis_api, _assert_public

    api, paths, projects = _analysis_api(tmp_path)
    root = tmp_path / "a"
    authority = _authority(root)
    production = ProductionReactionDomainSource(
        authority_factory=lambda _root: authority)
    project_id = api._workspace_project_id(paths[0], projects[paths[0]])
    projection = production.load_reaction_projection(
        project_id=project_id, project=projects[paths[0]])

    class LegacySource:
        def __init__(self):
            self.calls = 0

        def load_reaction_projection(self, *, project_id, project):
            del project
            self.calls += 1
            value = copy.deepcopy(projection)
            value["project_id"] = project_id
            return value

    legacy = LegacySource()
    api._reaction_domain_source = legacy
    response = api.analysis_workbench_bootstrap(project_id, "free-energy-path")

    assert response["ok"] is True
    assert legacy.calls == 1
    assert response["view"]["source_projection_schema"] == (
        "vcstudio.reaction-domain-projection/v2")
    _assert_public(response, tmp_path)

def test_invalid_snapshot_never_leaks_private_data(tmp_path):
    leaked = "C:\\Users\\private-person\\secret\\artifact.json"

    class BadAuthority:
        def build_snapshot(self):
            return {"schema": leaked}

    source = ProductionReactionDomainSource(
        authority_factory=lambda _root: BadAuthority(),
        evidence_adapter=_workbench_evidence,
    )
    result = source.load_reaction_projection(
        project_id="project-a", project={"root": str(tmp_path)})
    assert result is None
    assert leaked not in repr(result)


def _ready_kinetics_runtime(*, phase="gas"):
    from tests.test_kinetics_projection import (
        _real_fluid_projection,
        _v2_spec,
    )
    from vcstudio.project.catalysis_runtime import ValidatedReactionSnapshot
    from vcstudio.project.kinetics_projection import (
        required_frozen_v2_evidence_references,
    )

    projection = _real_fluid_projection(phase=phase)
    condition = projection["conditions"][0]["payload"]
    conditions = {
        key: condition[key]
        for key in (
            "temperature_k", "pressure_pa", "ph", "electrode_potential_v")
        if condition.get(key) is not None
    }
    view = build_reaction_workbench_view(
        projection, project_id="project-1", conditions=conditions)
    frozen = view["frozen_network"]
    validated = ValidatedReactionSnapshot(
        project_id="project-1",
        projection=projection,
        source_projection_sha256=frozen["source_projection_sha256"],
        domain_authority_id=frozen["domain_authority"]["authority_id"],
        domain_generation=frozen["domain_authority"]["generation"],
        network_id=frozen["network_id"],
        network_revision=frozen["network_identity"]["object_revision_id"],
    )
    references = required_frozen_v2_evidence_references(frozen)
    artifacts = {
        reference: f"artifact:{reference}".encode()
        for reference in references
    }
    bindings = {
        reference: hashlib.sha256(artifact).hexdigest()
        for reference, artifact in artifacts.items()
    }
    return validated, frozen, artifacts, _v2_spec(
        frozen, bindings, phase=phase)


class _ReactionSource:
    def __init__(self, validated):
        self.validated = validated
        self.reads = 0

    def validated_for_project(self, _context):
        self.reads += 1
        return self.validated


class _Selector:
    def __init__(self, selection):
        self.selection = selection

    def select(self, **_kwargs):
        return self.selection


def _selection(spec):
    from vcstudio.project.kinetics_authoring import (
        ActiveKineticsModelSpecSelection,
    )

    return ActiveKineticsModelSpecSelection(
        authority_id="a" * 32, revision=1, parent_revision=None,
        expected_current_hash=None, project_id="project-1", spec_id=spec.spec_id,
        spec_revision=spec.revision, spec_sha256=spec.semantic_sha256,
        intent_id="select-spec-1", transaction_id="txn-spec-1", confirmed=True,
        selection_sha256="",
    )


def test_kinetics_factory_requires_selection_exact_spec_and_evidence(tmp_path):
    validated, frozen, artifacts, spec = _ready_kinetics_runtime()
    reaction_source = _ReactionSource(validated)
    evidence_calls = []

    class Store:
        def head(self, project_id, spec_id):
            assert (project_id, spec_id) == ("project-1", spec.spec_id)
            return spec

    def resolve(reference):
        evidence_calls.append(reference)
        return artifacts[reference]

    factory = ProductionKineticsProviderFactory(
        reaction_source=reaction_source,
        active_spec_selector=_Selector(_selection(spec)),
        evidence_resolver_factory=lambda _context: resolve,
        spec_store_factory=lambda _root: Store(),
    )
    context = {
        "project_id": "project-1", "project_root": str(tmp_path),
        "project": {"root": str(tmp_path)},
    }

    provider = factory.for_project_with_reaction(
        context, validated_reaction=validated)
    assert provider.kinetics_input()["schema"] == "vcstudio.kinetics-network/v3"
    assert provider.kinetics_input()["network_id"] == frozen["network_id"]
    assert reaction_source.reads == 0
    assert evidence_calls == sorted(artifacts)
    proxied = replace(validated, projection=MappingProxyType({
        **validated.projection,
        "network": MappingProxyType(validated.projection["network"]),
    }))
    assert proxied.detached_projection()["network"] == (
        validated.detached_projection()["network"])
    assert factory.for_project_with_reaction(
        context, validated_reaction=proxied) is not None
    assert reaction_source.reads == 0
    tampered_projection = validated.detached_projection()
    tampered_projection["authority"]["domain_generation"] += 1
    tampered = replace(validated, projection=tampered_projection)
    assert factory.for_project_with_reaction(
        context, validated_reaction=tampered) is not None
    assert reaction_source.reads == 1
    assert factory.for_project_with_reaction(
        context, validated_reaction=None) is None
    assert reaction_source.reads == 1
    assert factory.for_project_with_reaction(
        context, validated_reaction=object()) is not None
    assert reaction_source.reads == 2

    no_selection = ProductionKineticsProviderFactory(
        reaction_source=_ReactionSource(validated),
        active_spec_selector=_Selector(None),
    )
    assert no_selection.for_project(context) is None

    no_evidence = ProductionKineticsProviderFactory(
        reaction_source=_ReactionSource(validated),
        active_spec_selector=_Selector(_selection(spec)),
        spec_store_factory=lambda _root: Store(),
    )
    assert no_evidence.for_project(context) is None

    class StaleStore:
        def head(self, _project_id, _spec_id):
            stale_binding = dict(spec.source_binding)
            stale_binding["network_id"] = "another-network"
            return replace(spec, source_binding=stale_binding)

    stale = ProductionKineticsProviderFactory(
        reaction_source=_ReactionSource(validated),
        active_spec_selector=_Selector(_selection(spec)),
        evidence_resolver_factory=lambda _context: resolve,
        spec_store_factory=lambda _root: StaleStore(),
    )
    assert stale.for_project(context) is None


def test_request_evidence_snapshot_stops_at_count_and_byte_limits(monkeypatch):
    from vcstudio.project import catalysis_runtime as runtime

    calls = []
    snapshot = runtime._RequestEvidenceSnapshot(  # noqa: SLF001
        lambda reference: calls.append(reference) or b"x")
    with pytest.raises(ValueError, match="binding references"):
        snapshot.bindings(tuple(
            f"evidence-{index}" for index in range(
                runtime.MAX_KINETICS_EVIDENCE_BINDINGS)))
    assert calls == []

    monkeypatch.setattr(runtime, "MAX_KINETICS_EVIDENCE_BYTES", 5)
    bounded = runtime._RequestEvidenceSnapshot(  # noqa: SLF001
        lambda reference: b"abc" if reference == "one" else b"def")
    assert bounded("one") == b"abc"
    with pytest.raises(ValueError, match="byte limit"):
        bounded("two")


def test_provider_evidence_resolver_never_substitutes_adapted_authority():
    from vcstudio.project import catalysis_runtime as runtime

    project_calls = []
    snapshot = runtime._RequestEvidenceSnapshot(  # noqa: SLF001
        lambda reference: project_calls.append(reference) or b"project")

    class BrokenAdapted:
        def kinetics_evidence(self, reference):
            raise RuntimeError(f"broken:{reference}")

    resolver = runtime._ProviderEvidenceResolver(  # noqa: SLF001
        BrokenAdapted(), snapshot, ("adapted-ref",))
    with pytest.raises(RuntimeError, match="broken:adapted-ref"):
        resolver("adapted-ref")
    assert project_calls == []
    assert resolver("spec-only-ref") == b"project"
    assert project_calls == ["spec-only-ref"]


def test_default_authoring_bootstrap_names_missing_evidence_prerequisite(tmp_path):
    validated, _frozen, _artifacts, _spec = _ready_kinetics_runtime()
    factory = ProductionKineticsAuthoringServiceFactory(
        reaction_source=_ReactionSource(validated))
    context = {
        "project_id": "project-1",
        "project_root": str(tmp_path),
        "project": {},
    }

    bootstrap = factory.bootstrap(context)

    assert bootstrap["status"] == "missing_prerequisite"
    assert bootstrap["reason"] == "evidence_resolver_unavailable"
    assert bootstrap["source"].solver_ready is False
    assert "evidence_resolver_unavailable" in (
        bootstrap["source"].solver_readiness_reasons)


def test_multiple_surface_site_axes_never_report_solver_ready(
        tmp_path, monkeypatch):
    from vcstudio.project import catalysis_runtime as runtime

    validated, frozen, artifacts, spec = _ready_kinetics_runtime()
    multiple = copy.deepcopy(frozen)
    adsorbates = [
        item for item in multiple["state_catalog"]
        if item["object_type"] == "AdsorbateState"
        and item.get("site_stoichiometry")
    ]
    assert len(adsorbates) >= 2
    adsorbates[-1]["surface_id"] = "surface-2"
    monkeypatch.setattr(
        runtime,
        "_validated_workbench_frozen",
        lambda *_args, **_kwargs: (multiple, object()),
    )
    context = {
        "project_id": "project-1",
        "project_root": str(tmp_path),
        "project": {},
    }
    source = runtime._ProductionKineticsAuthoringSourceAuthority(  # noqa: SLF001
        reaction_source=_ReactionSource(validated),
        private_context=context,
        resolver_available=True,
    ).authoring_source_snapshot()
    assert source.solver_ready is False
    assert source.solver_readiness_reasons == (
        "multiple_surface_axes_unsupported",)

    class Store:
        def head(self, _project_id, _spec_id):
            return spec

    provider = ProductionKineticsProviderFactory(
        reaction_source=_ReactionSource(validated),
        active_spec_selector=_Selector(_selection(spec)),
        evidence_resolver_factory=lambda _context: artifacts.__getitem__,
        spec_store_factory=lambda _root: Store(),
    )
    assert provider.for_project_with_reaction(
        context, validated_reaction=validated) is None


def test_fixed_active_spec_selector_has_no_default_and_verifies_hash(tmp_path):
    from vcstudio.project.kinetics_authoring import (
        ActiveKineticsModelSpecSelection,
        ActiveKineticsModelSpecSelectorStore,
    )

    selector = FixedActiveKineticsSpecSelector()
    assert selector.select(project_root=tmp_path, project_id="project-a") is None
    store = ActiveKineticsModelSpecSelectorStore(tmp_path)
    initial = store.snapshot()
    value = ActiveKineticsModelSpecSelection(
        authority_id="a" * 32,
        revision=1,
        parent_revision=None,
        expected_current_hash=None,
        project_id="project-a",
        spec_id="spec-1",
        spec_revision="spec-r1",
        spec_sha256="f" * 64,
        intent_id="select-spec-1",
        transaction_id="txn-spec-1",
        confirmed=True,
    )
    store.compare_and_swap(
        value, expected_snapshot_sha256=initial.snapshot_sha256)
    path = store.path
    assert selector.select(
        project_root=tmp_path, project_id="project-a").spec_id == "spec-1"
    tampered = value.to_dict()
    tampered["spec_id"] = "tampered"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    assert selector.select(project_root=tmp_path, project_id="project-a") is None


def test_fixed_active_spec_selector_rejects_valid_byte_rollback(tmp_path):
    from vcstudio.project.kinetics_authoring import (
        ActiveKineticsModelSpecSelection,
        ActiveKineticsModelSpecSelectorStore,
    )

    store = ActiveKineticsModelSpecSelectorStore(tmp_path)
    empty = store.snapshot()
    first = ActiveKineticsModelSpecSelection(
        authority_id="a" * 32,
        revision=1,
        parent_revision=None,
        expected_current_hash=None,
        project_id="project-a",
        spec_id="spec-1",
        spec_revision="spec-r1",
        spec_sha256="1" * 64,
        intent_id="selector-intent-1",
        transaction_id="selector-txn-1",
        confirmed=True,
    )
    store.compare_and_swap(
        first, expected_snapshot_sha256=empty.snapshot_sha256)
    first_bytes = store.path.read_bytes()
    current = store.snapshot()
    second = ActiveKineticsModelSpecSelection(
        authority_id=first.authority_id,
        revision=2,
        parent_revision=1,
        expected_current_hash=first.selection_sha256,
        project_id="project-a",
        spec_id="spec-1",
        spec_revision="spec-r2",
        spec_sha256="2" * 64,
        intent_id="selector-intent-2",
        transaction_id="selector-txn-2",
        confirmed=True,
    )
    store.compare_and_swap(
        second, expected_snapshot_sha256=current.snapshot_sha256)
    store.path.write_bytes(first_bytes)

    selected = FixedActiveKineticsSpecSelector().select(
        project_root=tmp_path, project_id="project-a")

    assert selected is None


def test_create_api_and_both_front_ends_share_the_production_factory(monkeypatch):
    from vcstudio.gui import report_bridge
    from vcstudio.gui_web import __main__ as web_main
    from vcstudio.gui_web import composition

    api = SimpleNamespace(
        start_background_services=lambda: {"ok": True},
        stop_background_services=lambda: None,
    )
    calls = []
    monkeypatch.setattr(composition, "create_api", lambda: calls.append("api") or api)
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(
        create_window=lambda *_args, **_kwargs: None,
        start=lambda: None,
    ))
    monkeypatch.setattr(
        "vcstudio.gui_web.resources.index_html", lambda: "<html></html>")

    assert web_main.main() == 0
    assert report_bridge._default_api_factory() is api
    assert calls == ["api", "api"]


def test_default_create_api_injects_production_objects():
    from vcstudio.gui_web.composition import create_api

    api = create_api()
    assert isinstance(api._reaction_domain_source, ProductionReactionDomainSource)
    assert isinstance(
        api._kinetics_projection_provider, ProductionKineticsProviderFactory)
    assert isinstance(
        api._catalysis_authoring_service_factory,
        ProductionCatalysisAuthoringServiceFactory,
    )
    assert isinstance(
        api._kinetics_authoring_service_factory,
        ProductionKineticsAuthoringServiceFactory,
    )
