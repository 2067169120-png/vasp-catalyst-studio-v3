"""Regression tests for immutable scientific report contracts."""
import json

import pytest

from vcstudio.project.report_contracts import (
    REPORT_SNAPSHOT_SCHEMA,
    REPORT_SPEC_SCHEMA,
    VALIDATION_RESULT_SCHEMA,
    ClaimRecord,
    ReportSnapshot,
    ReportSpec,
    ValidationCheck,
    ValidationResult,
    canonical_json_bytes,
    sha256_json,
    validate_bindings,
)


def _spec_mapping(**overrides):
    value = {
        "schema": REPORT_SPEC_SCHEMA,
        "preset_id": "scientific-review",
        "requested_kind": "final",
        "audience": "researcher",
        "locale": "zh-CN",
        "formats": ["PDF", "html", "DOCX", "html"],
        "scope": {"kind": "project", "project_ids": ["project-1"], "job_ids": []},
        "outline": ["executive_summary", "key_findings", "limitations"],
        "theme_id": "default",
        "template_ref": {"id": "paper", "version": "1", "locator": "C:/templates/a"},
        "policy_refs": [{"id": "gate", "version": "1", "sha256": "a" * 64}],
        "options": {"include_figures": True, "layout": {"wide_tables": False}},
        "extensions": {},
        "created_at_utc": "2026-08-09T01:02:03+00:00",
    }
    value.update(overrides)
    return value


def _make_spec(**overrides):
    return ReportSpec.from_mapping(_spec_mapping(**overrides))


def _snapshot_mapping(spec, **overrides):
    value = {
        "schema": REPORT_SNAPSHOT_SCHEMA,
        "created_at_utc": "2026-08-09T02:00:00+00:00",
        "spec_sha256": spec.semantic_sha256,
        "input_fingerprint": "input-fingerprint-1",
        "resolved_scope": {"project_ids": ["project-1"], "job_ids": ["job-1"]},
        "sources": [
            {
                "source_id": "project:project-1",
                "kind": "project",
                "schema": "vcstudio.project/v1",
                "sha256": "b" * 64,
                "size": 123,
                "locator": "C:/work/project-1/project.yaml",
            }
        ],
        "payload": {
            "adsorption": {"LiS": -1.25, "Li2S": -1.84},
            "rows": [{"species": "LiS", "energy_ev": -1.25}],
        },
        "evidence": {"method_consistency": {"status": "verified"}},
        "extensions": {},
    }
    value.update(overrides)
    return value


def _make_snapshot(spec, **overrides):
    return ReportSnapshot.from_mapping(_snapshot_mapping(spec, **overrides), spec=spec)


def _validation_mapping(spec, snapshot, **overrides):
    value = {
        "schema": VALIDATION_RESULT_SCHEMA,
        "validated_at_utc": "2026-08-09T03:00:00+00:00",
        "spec_sha256": spec.semantic_sha256,
        "snapshot_sha256": snapshot.semantic_sha256,
        "validator": {
            "id": "project-report-gate",
            "version": "1",
            "policy_sha256": "c" * 64,
            "locator": "C:/policies/current.json",
        },
        "status": "passed_with_warnings",
        "effective_kind": "final",
        "final_allowed": True,
        "scientific_qualification": "adsorption_result_verified",
        "claim_ceiling": "electronic_adsorption_screen",
        "report_model_sha256": "d" * 64,
        "checks": [
            {
                "id": "method-comparability",
                "status": "pass",
                "severity": "blocking",
                "required": True,
                "message": "Methods are comparable.",
                "evidence_refs": ["snapshot:evidence/method_consistency"],
            },
            {
                "id": "kinetic-coverage",
                "status": "warn",
                "severity": "warning",
                "required": False,
                "message": "No kinetic claim is made.",
            },
        ],
        "claims": [
            {
                "id": "claim-1",
                "text": "The adsorption result passed the declared gate.",
                "qualification": "adsorption_result_verified",
                "status": "supported",
                "evidence_refs": ["check:method-comparability"],
            }
        ],
        "extensions": {},
    }
    value.update(overrides)
    return value


def _make_validation(spec, snapshot, **overrides):
    return ValidationResult.from_mapping(
        _validation_mapping(spec, snapshot, **overrides),
        spec=spec,
        snapshot=snapshot,
    )


def test_canonical_json_and_hash_ignore_mapping_insertion_order():
    left = {"b": 2, "a": {"y": 1, "x": [3, 2, 1]}}
    right = {"a": {"x": [3, 2, 1], "y": 1}, "b": 2}

    assert canonical_json_bytes(left) == b'{"a":{"x":[3,2,1],"y":1},"b":2}'
    assert canonical_json_bytes(left) == canonical_json_bytes(right)
    assert sha256_json(left) == sha256_json(right)


def test_report_spec_round_trip_and_format_normalization():
    spec = _make_spec()

    assert spec.formats == ("html", "docx", "pdf")
    assert spec.report_kind == "final"
    assert json.loads(canonical_json_bytes(spec).decode("utf-8")) == spec.to_dict()

    restored = ReportSpec.from_mapping(spec.to_dict())
    assert restored == spec
    assert restored.semantic_sha256 == spec.semantic_sha256
    assert restored.sha256 == spec.semantic_hash()


def test_outline_order_is_semantic_but_mapping_key_order_is_not():
    first = _make_spec(
        outline=["executive_summary", "key_findings", "limitations"],
        options={"alpha": 1, "nested": {"x": 2, "y": 3}},
    )
    reordered_keys = _make_spec(
        outline=["executive_summary", "key_findings", "limitations"],
        options={"nested": {"y": 3, "x": 2}, "alpha": 1},
    )
    reordered_outline = _make_spec(
        outline=["key_findings", "executive_summary", "limitations"],
        options={"alpha": 1, "nested": {"x": 2, "y": 3}},
    )

    assert first.semantic_sha256 == reordered_keys.semantic_sha256
    assert first.semantic_sha256 != reordered_outline.semantic_sha256


def test_administrative_time_and_locator_do_not_change_semantic_hash():
    spec_a = _make_spec(
        created_at_utc="2026-08-09T01:00:00+00:00",
        template_ref={"id": "paper", "version": "1", "locator": "C:/templates/a"},
    )
    spec_b = _make_spec(
        created_at_utc="2026-08-10T01:00:00+00:00",
        template_ref={"id": "paper", "version": "1", "locator": "D:/moved/b"},
    )
    assert spec_a.semantic_sha256 == spec_b.semantic_sha256

    snapshot_a = _make_snapshot(spec_a)
    snapshot_b = ReportSnapshot.from_mapping(
        _snapshot_mapping(
            spec_b,
            created_at_utc="2026-08-11T04:00:00+00:00",
            sources=[
                {
                    "source_id": "project:project-1",
                    "kind": "project",
                    "schema": "vcstudio.project/v1",
                    "sha256": "b" * 64,
                    "size": 123,
                    "locator": "Z:/relocated/project.yaml",
                }
            ],
        ),
        spec=spec_b,
    )
    assert snapshot_a.semantic_sha256 == snapshot_b.semantic_sha256

    validation_a = _make_validation(spec_a, snapshot_a)
    validation_b = ValidationResult.from_mapping(
        _validation_mapping(
            spec_b,
            snapshot_b,
            validated_at_utc="2026-08-12T05:00:00+00:00",
            validator={
                "id": "project-report-gate",
                "version": "1",
                "policy_sha256": "c" * 64,
                "locator": "Z:/moved/policy.json",
            },
        ),
        spec=spec_b,
        snapshot=snapshot_b,
    )
    assert validation_a.semantic_sha256 == validation_b.semantic_sha256


def test_scientific_payload_and_extensions_do_not_drop_locator_or_time_names():
    spec = _make_spec()
    baseline = _make_snapshot(
        spec,
        payload={
            "adsorption_site": {"locator": "top-site"},
            "trajectory": {"created_at": "step-1"},
        },
        extensions={
            "source_locator": "dataset-a",
            "generated_at_utc": "simulation-step-10",
        },
    )
    changed_payload = _make_snapshot(
        spec,
        payload={
            "adsorption_site": {"locator": "bridge-site"},
            "trajectory": {"created_at": "step-99"},
        },
        extensions={
            "source_locator": "dataset-a",
            "generated_at_utc": "simulation-step-10",
        },
    )
    changed_extensions = _make_snapshot(
        spec,
        payload={
            "adsorption_site": {"locator": "top-site"},
            "trajectory": {"created_at": "step-1"},
        },
        extensions={
            "source_locator": "dataset-b",
            "generated_at_utc": "simulation-step-11",
        },
    )

    assert baseline.semantic_sha256 != changed_payload.semantic_sha256
    assert baseline.semantic_sha256 != changed_extensions.semantic_sha256
    assert baseline.semantic_payload()["payload"]["adsorption_site"]["locator"] == "top-site"
    assert baseline.semantic_payload()["payload"]["trajectory"]["created_at"] == "step-1"


def test_scientific_data_or_source_digest_changes_snapshot_hash():
    spec = _make_spec()
    baseline = _make_snapshot(spec)
    changed_data = _make_snapshot(
        spec,
        payload={
            "adsorption": {"LiS": -1.24, "Li2S": -1.84},
            "rows": [{"species": "LiS", "energy_ev": -1.24}],
        },
    )
    changed_source = _make_snapshot(
        spec,
        sources=[
            {
                "source_id": "project:project-1",
                "kind": "project",
                "schema": "vcstudio.project/v1",
                "sha256": "d" * 64,
                "size": 123,
                "locator": "C:/work/project-1/project.yaml",
            }
        ],
    )

    assert baseline.semantic_sha256 != changed_data.semantic_sha256
    assert baseline.semantic_sha256 != changed_source.semantic_sha256


def test_contract_round_trip_and_binding_chain():
    spec = _make_spec()
    snapshot = _make_snapshot(spec)
    validation = _make_validation(spec, snapshot)

    restored_snapshot = ReportSnapshot.from_mapping(snapshot.to_dict(), spec=spec)
    restored_validation = ValidationResult.from_mapping(
        validation.to_dict(), spec=spec, snapshot=restored_snapshot
    )
    validate_bindings(spec, restored_snapshot, restored_validation)

    assert restored_snapshot == snapshot
    assert restored_validation == validation
    assert restored_validation.checks[0] == ValidationCheck.from_mapping(
        validation.to_dict()["checks"][0]
    )
    assert restored_validation.claims[0] == ClaimRecord.from_mapping(
        validation.to_dict()["claims"][0]
    )


def test_binding_mismatch_is_rejected():
    spec = _make_spec()
    other_spec = _make_spec(outline=["executive_summary", "limitations"])
    snapshot = _make_snapshot(spec)

    with pytest.raises(ValueError, match="snapshot spec binding mismatch"):
        ReportSnapshot.from_mapping(snapshot.to_dict(), spec=other_spec)

    validation = _make_validation(spec, snapshot)
    other_snapshot = _make_snapshot(
        spec, payload={"adsorption": {"LiS": -9.0}, "rows": []}
    )
    with pytest.raises(ValueError, match="validation snapshot binding mismatch"):
        ValidationResult.from_mapping(
            validation.to_dict(), spec=spec, snapshot=other_snapshot
        )

    with pytest.raises(ValueError, match="validation spec binding mismatch"):
        ValidationResult.from_mapping(validation.to_dict(), spec=other_spec)

    mismatched_chain = validation.to_dict()
    mismatched_chain["spec_sha256"] = other_spec.semantic_sha256
    with pytest.raises(ValueError, match="validation and snapshot spec bindings differ"):
        ValidationResult.from_mapping(mismatched_chain, snapshot=snapshot)


def test_final_allowed_and_required_checks_fail_closed():
    spec = _make_spec()
    snapshot = _make_snapshot(spec)

    with pytest.raises(ValueError, match="requires final_allowed"):
        _make_validation(spec, snapshot, effective_kind="final", final_allowed=False)

    with pytest.raises(ValueError, match="requires a passed validation"):
        _make_validation(
            spec,
            snapshot,
            status="blocked",
            effective_kind="diagnostic",
            final_allowed=True,
            checks=[{"id": "delivery-gate", "status": "fail"}],
            claims=[],
        )

    unknown_required = [
        {
            "id": "method-comparability",
            "status": "unknown",
            "severity": "blocking",
            "required": True,
            "message": "Evidence is missing.",
        }
    ]
    with pytest.raises(ValueError, match="non-passing required or blocking"):
        _make_validation(
            spec,
            snapshot,
            status="passed",
            effective_kind="diagnostic",
            final_allowed=False,
            checks=unknown_required,
        )

    diagnostic = _make_validation(
        spec,
        snapshot,
        status="blocked",
        effective_kind="diagnostic",
        final_allowed=False,
        scientific_qualification="diagnostic",
        checks=unknown_required,
        claims=[],
    )
    assert diagnostic.final_allowed is False
    assert diagnostic.report_kind == "diagnostic"

    for non_passing in ("warn", "not_applicable"):
        with pytest.raises(ValueError, match="non-passing required or blocking"):
            _make_validation(
                spec,
                snapshot,
                checks=[{
                    "id": f"required-{non_passing}",
                    "status": non_passing,
                    "severity": "blocking",
                    "required": True,
                    "message": "Required evidence did not pass.",
                }],
            )

    with pytest.raises(ValueError, match="at least one required or blocking check"):
        _make_validation(spec, snapshot, status="passed", checks=[])

    with pytest.raises(ValueError, match="validator.id"):
        _make_validation(spec, snapshot, validator={"version": "1"})


def test_validation_cannot_upgrade_diagnostic_spec_to_final():
    spec = _make_spec(requested_kind="diagnostic")
    snapshot = _make_snapshot(spec)

    with pytest.raises(ValueError, match="cannot upgrade"):
        _make_validation(spec, snapshot)


def test_supported_claim_cannot_exceed_validation_qualification():
    spec = _make_spec()
    snapshot = _make_snapshot(spec)

    with pytest.raises(ValueError, match="supported claims cannot exceed"):
        _make_validation(
            spec,
            snapshot,
            claims=[{
                "id": "claim-overreach",
                "text": "This claim requires a human scientific review.",
                "qualification": "human_scientific_reviewed",
                "status": "supported",
            }],
        )

    limited = _make_validation(
        spec,
        snapshot,
        claims=[{
            "id": "claim-limited",
            "text": "A higher-level claim is recorded as not yet supported.",
            "qualification": "human_scientific_reviewed",
            "status": "limited",
        }],
    )
    assert limited.claims[0].status == "limited"


def test_verified_qualification_requires_validator_and_passing_gate_evidence():
    spec = _make_spec()
    snapshot = _make_snapshot(spec)

    with pytest.raises(ValueError, match="requires validator.id"):
        _make_validation(
            spec,
            snapshot,
            validator={},
            status="blocked",
            effective_kind="diagnostic",
            final_allowed=False,
            checks=[
                {"id": "adsorption-gate", "status": "pass"},
                {"id": "publication-gate", "status": "fail"},
            ],
            claims=[],
        )

    with pytest.raises(ValueError, match="requires at least one passing"):
        _make_validation(
            spec,
            snapshot,
            status="blocked",
            effective_kind="diagnostic",
            final_allowed=False,
            checks=[{"id": "publication-gate", "status": "fail"}],
            claims=[],
        )

    with pytest.raises(ValueError, match="unknown validation status"):
        _make_validation(
            spec,
            snapshot,
            status="unknown",
            effective_kind="diagnostic",
            final_allowed=False,
            checks=[{"id": "adsorption-gate", "status": "pass"}],
            claims=[],
        )

    lower_verified = _make_validation(
        spec,
        snapshot,
        status="blocked",
        effective_kind="diagnostic",
        final_allowed=False,
        checks=[
            {"id": "adsorption-gate", "status": "pass"},
            {"id": "publication-gate", "status": "fail"},
        ],
        claims=[],
    )
    assert lower_verified.scientific_qualification == "adsorption_result_verified"


def test_human_reviewed_qualification_requires_auditable_human_approval():
    spec = _make_spec()
    snapshot = _make_snapshot(spec)

    with pytest.raises(ValueError, match="reviewer_type='human'"):
        _make_validation(
            spec,
            snapshot,
            scientific_qualification="human_scientific_reviewed",
            human_review={},
        )

    approved = _make_validation(
        spec,
        snapshot,
        scientific_qualification="human_scientific_reviewed",
        human_review={
            "reviewer_type": "human",
            "reviewed_by": "reviewer-42",
            "reviewed_at_utc": "2026-08-10T03:04:05Z",
            "decision": "approved",
            "evidence_refs": ["review-record:42"],
        },
    )
    assert approved.human_review["reviewer_type"] == "human"
    assert approved.human_review["reviewed_at_utc"].endswith("+00:00")
    assert approved.semantic_sha256 == ValidationResult.from_mapping(
        approved.to_dict(), spec=spec, snapshot=snapshot
    ).semantic_sha256


def test_validation_status_must_aggregate_check_outcomes():
    spec = _make_spec()
    snapshot = _make_snapshot(spec)
    required_pass = {
        "id": "required-pass",
        "status": "pass",
        "severity": "blocking",
        "required": True,
    }
    optional_fail = {
        "id": "optional-fail",
        "status": "fail",
        "severity": "warning",
        "required": False,
    }

    with pytest.raises(ValueError, match="every applicable check to pass"):
        _make_validation(
            spec, snapshot, status="passed",
            checks=[required_pass, optional_fail],
        )
    warned = _make_validation(
        spec, snapshot, status="passed_with_warnings",
        checks=[required_pass, optional_fail],
    )
    assert warned.status == "passed_with_warnings"

    with pytest.raises(ValueError, match="blocked validation requires"):
        _make_validation(
            spec,
            snapshot,
            status="blocked",
            effective_kind="diagnostic",
            final_allowed=False,
            checks=[required_pass],
            claims=[],
        )
    with pytest.raises(ValueError, match="requires at least one non-blocking warning"):
        _make_validation(
            spec,
            snapshot,
            status="passed_with_warnings",
            checks=[required_pass],
        )
    with pytest.raises(ValueError, match="requires an unknown check"):
        _make_validation(
            spec,
            snapshot,
            status="unknown",
            effective_kind="diagnostic",
            final_allowed=False,
            scientific_qualification="diagnostic",
            checks=[required_pass],
            claims=[],
        )


def test_draft_report_kind_is_explicitly_supported_without_final_claims():
    spec = _make_spec(requested_kind="draft")
    snapshot = _make_snapshot(spec)
    validation = _make_validation(
        spec,
        snapshot,
        status="passed",
        effective_kind="draft",
        final_allowed=False,
        scientific_qualification="diagnostic",
        checks=[],
        claims=[],
    )

    assert spec.report_kind == "draft"
    assert validation.report_kind == "draft"
    assert validation.final_allowed is False


@pytest.mark.parametrize("kind", ["preview", "publication", "FINAL", ""])
def test_unknown_report_kind_is_rejected(kind):
    with pytest.raises(ValueError, match="report_kind"):
        _make_spec(requested_kind=kind)


@pytest.mark.parametrize("qualification", ["unknown", "publication_ready", "FINAL", ""])
def test_unknown_qualification_is_rejected(qualification):
    spec = _make_spec()
    snapshot = _make_snapshot(spec)
    with pytest.raises(ValueError, match="scientific_qualification"):
        _make_validation(
            spec,
            snapshot,
            status="blocked",
            effective_kind="diagnostic",
            final_allowed=False,
            scientific_qualification=qualification,
            checks=[],
            claims=[],
        )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nan_and_infinity_are_rejected_everywhere(bad):
    with pytest.raises(ValueError, match="NaN and Infinity"):
        canonical_json_bytes({"value": bad})

    spec = _make_spec()
    with pytest.raises(ValueError, match="NaN and Infinity"):
        _make_snapshot(spec, payload={"energy": bad})

    with pytest.raises(ValueError, match="NaN and Infinity"):
        _make_spec(options={"threshold": bad})


def test_input_and_serialized_output_mutation_cannot_drift_contract():
    spec = _make_spec()
    raw_payload = {
        "adsorption": {"LiS": -1.25},
        "rows": [{"species": "LiS", "values": [-1.25, -1.20]}],
    }
    raw_sources = [
        {
            "source_id": "project:project-1",
            "sha256": "b" * 64,
            "locator": "C:/original/project.yaml",
        }
    ]
    snapshot = _make_snapshot(spec, payload=raw_payload, sources=raw_sources)
    before_dict = snapshot.to_dict()
    before_hash = snapshot.semantic_sha256

    raw_payload["adsorption"]["LiS"] = 999.0
    raw_payload["rows"][0]["values"].append(999.0)
    raw_sources[0]["sha256"] = "e" * 64
    raw_sources[0]["locator"] = "D:/mutated/project.yaml"

    exported = snapshot.to_dict()
    exported["payload"]["adsorption"]["LiS"] = 777.0
    exported["sources"][0]["sha256"] = "f" * 64

    assert snapshot.to_dict() == before_dict
    assert snapshot.semantic_sha256 == before_hash


def test_unknown_top_level_fields_are_not_silently_dropped():
    with pytest.raises(ValueError, match="unknown contract fields"):
        ReportSpec.from_mapping({**_spec_mapping(), "typo_field": True})
