"""Structure Source Hub browser-boundary and candidate-creation contracts."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import types

import pytest
import yaml

from vcstudio.generate.structure_sources import parse_structure_content
from vcstudio.gui_web.api import Api


BULK_POSCAR = """Pt bulk
1.0
3.9 0 0
0 3.9 0
0 0 3.9
Pt
1
Direct
0 0 0
"""

SLAB_POSCAR = """Pt slab candidate
1.0
3.9 0 0
0 3.9 0
0 0 18.9
Pt
2
Cartesian
0 0 7.5
1.95 1.95 11.4
"""

ADSORBATE_POSCAR = """H adsorbate
1.0
10 0 0
0 10 0
0 0 10
H
1
Cartesian
5 5 5
"""

_PARSED_BULK = parse_structure_content(BULK_POSCAR, "poscar")
BULK_COMPUTED_SHA256 = _PARSED_BULK.structure_sha256
BULK_CANONICAL_POSCAR = _PARSED_BULK.canonical_poscar


def _result(token: str, *, source_id: str = "local-POSCAR") -> dict:
    return {
        "token": token,
        "source_id": source_id,
        "formula": "Pt",
        "declared_formula": "Pt",
        "computed_formula": "Pt",
        "license": "user-supplied",
        "citation": "User supplied local structure",
        "method": {"provider": "local", "format": "POSCAR"},
        "raw_structure_sha256": "a" * 64,
        "declared_raw_structure_sha256": "a" * 64,
        "computed_structure_sha256": BULK_COMPUTED_SHA256,
    }


class _FakeSources:
    def __init__(self):
        self.selected_paths = []
        self.searches = []
        self.resolved = []

    def capabilities(self):
        return [
            {"provider": "local", "available": True, "network": False},
            {"provider": "external-gateway", "available": False, "network": True,
             "reason": "gateway unavailable"},
        ]

    def select_local(self, path):
        self.selected_paths.append(str(path))
        return [_result("1" * 32)]

    def search(self, provider, query):
        self.searches.append((provider, query))
        if provider == "broken":
            raise RuntimeError(r"gateway at C:\private\gateway failed")
        return [{**_result("2" * 32, source_id="mp-1"), "provider": provider}]

    def preview(self, token):
        if token not in {"1" * 32, "2" * 32, "3" * 32}:
            raise ValueError("source token is missing or expired")
        return {
            **_result(token), "natoms": 1, "poscar": BULK_POSCAR,
            "view": {"xyz": "1\npreview\nPt 0 0 0", "natoms": 1,
                     "formula": "Pt1", "gap": {}, "notes": []},
            "top_view": {"atoms": [{"element": "Pt", "x": 0.0, "y": 0.0}]},
            "provenance": {
                "provider": "local", "database_id": "local-POSCAR", "query": None,
                "retrieved_at": "2026-08-15T00:00:00+00:00",
                "license": "user-supplied", "raw_structure_hash": "a" * 64,
                "declared_formula": "Pt", "computed_formula": "Pt",
                "declared_raw_structure_sha256": "a" * 64,
                "computed_structure_sha256": BULK_COMPUTED_SHA256,
            },
        }

    def confirm(self, token):
        if token not in {"1" * 32, "2" * 32, "3" * 32}:
            raise ValueError("source token is missing or expired")
        confirmation = {"1" * 32: "b" * 32, "2" * 32: "c" * 32,
                        "3" * 32: "d" * 32}[token]
        return {"confirmation_token": confirmation, "confirmed": True,
                "source": _result(token)}

    def resolve_confirmed(self, token):
        self.resolved.append(token)
        if token not in {"b" * 32, "c" * 32, "d" * 32}:
            raise ValueError("source confirmation is missing, expired, or already used")
        return {
            "poscar": BULK_POSCAR,
            "raw_source": BULK_POSCAR,
            "source": _result("1" * 32),
            "provenance": {
                "provider": "local", "database_id": "local-POSCAR", "query": None,
                "retrieved_at": "2026-08-15T00:00:00+00:00",
                "license": "user-supplied", "raw_structure_hash": "a" * 64,
                "declared_formula": "Pt", "computed_formula": "Pt",
                "declared_raw_structure_sha256": "a" * 64,
                "computed_structure_sha256": BULK_COMPUTED_SHA256,
            },
        }


class _FakeSurface:
    __version__ = "test-1"

    @staticmethod
    def build_slabs(bulk_poscar, request):
        assert bulk_poscar == BULK_CANONICAL_POSCAR
        if request.get("miller") == [0, 0, 0]:
            raise ValueError("Miller indices cannot all be zero")
        return [{
            "poscar": SLAB_POSCAR,
            "termination": {
                "termination_id": "term-01", "index": 0,
                "geometric_signature": "top:Pt",
            },
            "parameters": {
                "miller": [1, 1, 1], "layers": 4, "vacuum": 15.0,
                "fixed_layers": 2, "surface_sides": "top",
            },
            "provenance": {
                "builder": "vcstudio.surface_workbench", "builder_version": "test-1",
                "raw_structure_hash": "a" * 64,
                "parameters": dict(request),
            },
            "structure_hash": "e" * 64,
            "natoms": 2,
            "scientific_status": "geometric_candidate",
        }]

    @staticmethod
    def explore_sites(slab_poscar, request, adsorbate_poscar=None):
        assert slab_poscar == SLAB_POSCAR
        site = {
            "site_id": "site-top-01", "kind": "ontop",
            "classification": "geometric_site_candidate", "side": "top",
            "fractional": [0.0, 0.0, 0.6], "cartesian": [0.0, 0.0, 11.4],
            "contributors": [1], "equivalence_group_id": "eq-01",
        }
        if float(request.get("min_distance") or 0) > 5.0:
            return {
                "sites": [site], "equivalence_groups": [], "generated": [],
                "rejections": [{"site_id": site["site_id"],
                                "reason": "minimum distance is unreasonable"}],
                "parameters": dict(request), "provenance": {}, "limitations": [],
            }
        generated = []
        if adsorbate_poscar is not None:
            generated.append({
                "poscar": SLAB_POSCAR, "site_id": site["site_id"], "rotation": 0,
                "structure_hash": "f" * 64, "min_distance": 2.1,
            })
        return {
            "sites": [site],
            "equivalence_groups": [{
                "equivalence_group_id": "eq-01",
                "classification": "geometric_symmetry_candidate",
                "site_ids": [site["site_id"]],
            }],
            "generated": generated, "rejections": [],
            "parameters": dict(request),
            "provenance": {"explorer": "vcstudio.surface_workbench",
                           "explorer_version": "test-1"},
            "limitations": ["Geometric sites are not catalytic activity claims."],
        }


class _TwoCandidateSurface(_FakeSurface):
    @staticmethod
    def build_slabs(bulk_poscar, request):
        first = _FakeSurface.build_slabs(bulk_poscar, request)[0]
        second = copy.deepcopy(first)
        second["termination"]["termination_id"] = "term-02"
        second["termination"]["index"] = 1
        second["structure_hash"] = "4" * 64
        return [first, second]


class _FlakyLedger:
    def __init__(self):
        self.calls = []
        self.failed_once = False

    @staticmethod
    def load_all():
        return []

    def register(self, path):
        name = Path(path).name
        self.calls.append(name)
        if "term-02" in name and not self.failed_once:
            self.failed_once = True
            raise OSError(
                r"registration C:\ledger-private\jobs failed; "
                "password=top-secret-value"
            )


class _MaliciousSources(_FakeSources):
    @staticmethod
    def _tainted_provenance():
        return {
            "provider": "local",
            "database_id": "local-POSCAR",
            "query": None,
            "retrieved_at": "2026-08-15T00:00:00+00:00",
            "license": r"user-supplied C:\hub-private\license",
            "raw_structure_hash": "a" * 64,
            "declared_formula": "Pt",
            "computed_formula": "Pt",
            "declared_raw_structure_sha256": "a" * 64,
            "computed_structure_sha256": BULK_COMPUTED_SHA256,
            "method": {
                "note": (
                    r"from \\unc-secret-host\source and /srv/leak-posix/source "
                    "Bearer bearer-secret-value "
                    "https://url-user:url-password-secret@example.invalid/reference"
                ),
                "api_key": "api-key-secret-value",
            },
        }

    def preview(self, token):
        result = super().preview(token)
        result["provenance"] = self._tainted_provenance()
        result["view"]["notes"] = [
            r"embedded C:\preview-private\POSCAR password=preview-secret"
        ]
        return result

    def resolve_confirmed(self, token):
        result = super().resolve_confirmed(token)
        result["provenance"] = self._tainted_provenance()
        return result


class _MaliciousSurface(_FakeSurface):
    @staticmethod
    def build_slabs(bulk_poscar, request):
        rows = _FakeSurface.build_slabs(bulk_poscar, request)
        rows[0]["provenance"]["diagnostic"] = {
            "text": "/srv/leak-posix/builder secret=builder-secret-value",
            "credential": "credential-secret-value",
        }
        return rows


class _MismatchedComputedEvidenceSources(_FakeSources):
    def resolve_confirmed(self, token):
        result = super().resolve_confirmed(token)
        result["computed_structure_sha256"] = "0" * 64
        result["provenance"]["computed_structure_sha256"] = "0" * 64
        result["source"]["computed_structure_sha256"] = "0" * 64
        return result


def _api(tmp_path: Path, *, sources=None, dialog_kind=None, ledger=None, surface=None):
    sources = sources or _FakeSources()
    chosen_source = tmp_path / "private" / "POSCAR"
    chosen_output = tmp_path / "private-output"
    chosen_source.parent.mkdir(parents=True, exist_ok=True)
    chosen_source.write_text(BULK_POSCAR, encoding="utf-8")
    chosen_output.mkdir(parents=True, exist_ok=True)

    def dialog(kind):
        if dialog_kind is not None:
            dialog_kind.append(kind)
        return str(chosen_output if kind == "dir" else chosen_source)

    registrations = []
    ledger = ledger or types.SimpleNamespace(
        register=lambda path: registrations.append(str(path)), load_all=lambda: [])
    api = Api(
        dialog_fn=dialog, ledger_mod=ledger,
        structure_source_session=sources,
        surface_workbench_mod=surface or _FakeSurface,
    )
    return api, sources, registrations, chosen_source, chosen_output


def _assert_path_free(value):
    encoded = json.dumps(value, ensure_ascii=False)
    assert ":\\" not in encoded and "file://" not in encoded

    def inspect(item):
        if isinstance(item, dict):
            assert not ({"path", "project_path", "raw_source", "local_path"} & set(item))
            for nested in item.values():
                inspect(nested)
        elif isinstance(item, list):
            for nested in item:
                inspect(nested)

    inspect(value)


def _assert_sensitive_free(value):
    encoded = json.dumps(value, ensure_ascii=False)
    for forbidden in (
        "hub-private",
        "preview-private",
        "unc-secret-host",
        "leak-posix",
        "top-secret-value",
        "preview-secret",
        "bearer-secret-value",
        "api-key-secret-value",
        "builder-secret-value",
        "credential-secret-value",
        "url-user",
        "url-password-secret",
    ):
        assert forbidden not in encoded

    def inspect(item):
        if isinstance(item, dict):
            lowered = {str(key).lower() for key in item}
            assert not ({"api_key", "credential", "password", "secret"} & lowered)
            for nested in item.values():
                inspect(nested)
        elif isinstance(item, list):
            for nested in item:
                inspect(nested)

    inspect(value)


def test_source_capabilities_and_local_selection_are_path_free(tmp_path):
    seen = []
    api, sources, _registrations, source_path, _output = _api(
        tmp_path, dialog_kind=seen)

    capabilities = api.structure_source_capabilities()
    selected = api.structure_source_select_local()

    assert capabilities["ok"] is True
    assert capabilities["providers"][0]["provider"] == "local"
    assert selected["ok"] is True and selected["results"][0]["token"] == "1" * 32
    assert sources.selected_paths == [str(source_path)] and seen == ["structure_source"]
    _assert_path_free(selected)


def test_gateway_failure_does_not_break_local_source_flow(tmp_path):
    api, _sources, _registrations, _source, _output = _api(tmp_path)

    remote = api.structure_source_search("broken", "Pt")
    local = api.structure_source_select_local()

    assert remote["ok"] is False and "<local-path>" in remote["error"]
    assert local["ok"] is True and local["results"]


def test_preview_must_be_explicitly_confirmed_before_surface_dry_run(tmp_path):
    api, sources, _registrations, _source, _output = _api(tmp_path)
    selected = api.structure_source_select_local()["results"][0]

    preview = api.structure_source_preview(selected["token"])
    rejected = api.surface_dry_run(selected["token"], {"miller": [1, 1, 1]}, {})
    confirmed = api.structure_source_confirm(selected["token"])
    dry_run = api.surface_dry_run(
        confirmed["source_token"],
        {"miller": [1, 1, 1], "layers": 4, "vacuum": 15,
         "fixed_layers": 2, "surface_sides": "top"},
        {"sides": "top", "coverage": 1.0},
    )

    assert preview["ok"] is True
    assert preview["preview"]["view"]["xyz"].startswith("1\npreview")
    assert preview["preview"]["top_view"]["atoms"]
    assert rejected["ok"] is False and rejected["operation_token"] is None
    assert confirmed["ok"] is True and len(confirmed["source_token"]) == 32
    assert sources.resolved[-1] == "b" * 32
    assert dry_run["ok"] is True and dry_run["ready"] is True
    assert len(dry_run["dry_run"]["slabs"]) == 1
    assert dry_run["dry_run"]["scientific_status"] == "candidate"
    assert dry_run["operation_token"]
    assert not any(
        "poscar" in item
        for item in dry_run["dry_run"]["slabs"]
    )
    _assert_path_free(dry_run)


def test_confirm_import_is_idempotent_and_survives_failed_dry_run(tmp_path):
    api, sources, _registrations, _source, _output = _api(tmp_path)
    first = api.structure_source_confirm("1" * 32)
    replay = api.structure_source_confirm("1" * 32)

    failed = api.surface_dry_run(first["source_token"], {"miller": [0, 0, 0]}, {})
    retried = api.surface_dry_run(first["source_token"], {"miller": [1, 0, 0]}, {})

    assert replay == first
    assert sources.resolved == ["b" * 32]
    assert failed["ok"] is False and failed["operation_token"] is None
    assert retried["ok"] is True and retried["operation_token"]


def test_confirm_recomputes_canonical_content_and_rejects_forged_computed_hash(tmp_path):
    api, _sources, _registrations, _source, _output = _api(
        tmp_path, sources=_MismatchedComputedEvidenceSources())

    result = api.structure_source_confirm("1" * 32)

    assert result["ok"] is False
    assert result["confirmed"] is False
    assert result["source_token"] is None
    assert "recomputation" in result["error"]


def test_adsorbate_collision_fails_closed_without_operation_token(tmp_path):
    api, _sources, _registrations, _source, _output = _api(tmp_path)
    bulk = api.structure_source_confirm("1" * 32)["source_token"]
    adsorbate = api.structure_source_confirm("2" * 32)["source_token"]

    result = api.surface_dry_run(
        bulk, {"miller": [1, 1, 1]},
        {"adsorbate_source_token": adsorbate, "min_distance": 9.0},
    )

    assert result["ok"] is False and result["ready"] is False
    assert result["operation_token"] is None
    assert result["dry_run"]["rejections"]


def test_output_selection_is_opaque_and_does_not_expose_directory(tmp_path):
    api, _sources, _registrations, _source, output = _api(tmp_path)

    result = api.structure_output_select()

    assert result["ok"] is True and len(result["output_token"]) == 32
    assert result["selection"]["label"] == output.name
    _assert_path_free(result)


def test_output_selection_rejects_reparse_or_symlink_directory(tmp_path):
    target = tmp_path / "real-output"
    linked = tmp_path / "linked-output"
    target.mkdir()
    try:
        os.symlink(target, linked, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")

    api = Api(dialog_fn=lambda _kind: str(linked))
    result = api.structure_output_select()

    assert result["ok"] is False
    assert result["output_token"] is None
    assert "reparse" in result["error"].lower() or "symbolic" in result["error"].lower()
    _assert_path_free(result)


def test_create_rejects_directory_entity_replaced_after_selection(tmp_path):
    api, _sources, _registrations, _source, output = _api(tmp_path)
    confirmed = api.structure_source_confirm("1" * 32)["source_token"]
    dry_run = api.surface_dry_run(confirmed, {"miller": [1, 0, 0]}, {})
    destination = api.structure_output_select()
    displaced = tmp_path / "displaced-output"
    output.rename(displaced)
    output.mkdir()

    created = api.surface_create_candidates(
        dry_run["operation_token"], destination["output_token"])

    assert created["ok"] is False
    assert created["jobs"] == []
    assert "changed" in created["error"].lower()
    assert not any(output.iterdir())
    assert not any(displaced.iterdir())
    _assert_path_free(created)


def test_confirmed_candidate_creation_is_idempotent_and_stays_candidate(tmp_path):
    api, _sources, registrations, _source, output = _api(tmp_path)
    confirmed = api.structure_source_confirm("1" * 32)["source_token"]
    dry_run = api.surface_dry_run(
        confirmed,
        {"miller": [1, 1, 1], "layers": 4, "vacuum": 15,
         "fixed_layers": 2, "surface_sides": "top"},
        {"sides": "top", "coverage": 1.0},
    )
    destination = api.structure_output_select()

    first = api.surface_create_candidates(
        dry_run["operation_token"], destination["output_token"])
    replay = api.surface_create_candidates(
        dry_run["operation_token"], destination["output_token"])

    assert first["ok"] is True and first["replayed"] is False
    assert replay == {**first, "replayed": True}
    assert len(first["jobs"]) == 1 and first["jobs"][0]["scientific_status"] == "candidate"
    assert len(registrations) == 1
    batches = [path for path in output.iterdir() if path.is_dir()]
    assert len(batches) == 1
    job_dirs = [path for path in batches[0].iterdir() if path.is_dir()]
    assert len(job_dirs) == 1
    manifest = yaml.safe_load((job_dirs[0] / "job.yaml").read_text(encoding="utf-8"))
    assert manifest["state"] == "CREATED"
    assert manifest["scientific_status"] == "candidate"
    source_evidence = manifest["inputs"]["structure_hub"]["source"]
    assert source_evidence["provider"] == "local"
    assert source_evidence["declared_formula"] == "Pt"
    assert source_evidence["computed_formula"] == "Pt"
    assert source_evidence["declared_raw_structure_sha256"] == "a" * 64
    assert source_evidence["raw_structure_hash"] == "a" * 64
    assert source_evidence["computed_structure_sha256"] == BULK_COMPUTED_SHA256
    assert manifest["inputs"]["structure_hub"]["builder"]["builder_version"] == "test-1"
    assert manifest["inputs"]["structure_hub"]["operation_id"] == dry_run["operation_token"]
    assert manifest["registration_pending"] is False
    assert manifest["registration"]["status"] == "registered"
    assert manifest["results"] == {}
    _assert_path_free(first)


def test_registration_partial_failure_persists_and_replays_only_missing_job(tmp_path):
    ledger = _FlakyLedger()
    api, _sources, _registrations, _source, output = _api(
        tmp_path, ledger=ledger, surface=_TwoCandidateSurface)
    confirmed = api.structure_source_confirm("1" * 32)["source_token"]
    dry_run = api.surface_dry_run(confirmed, {"miller": [1, 0, 0]}, {})
    destination = api.structure_output_select()

    partial = api.surface_create_candidates(
        dry_run["operation_token"], destination["output_token"])

    assert partial["ok"] is False
    assert partial["partial"] is True
    assert partial["discoverability"] == "partial"
    assert len(partial["registration_pending"]) == 1
    assert len(ledger.calls) == 2
    batch = next(path for path in output.iterdir() if path.is_dir())
    manifests = {
        job.name: yaml.safe_load((job / "job.yaml").read_text(encoding="utf-8"))
        for job in batch.iterdir()
        if job.is_dir()
    }
    assert len(manifests) == 2
    pending = [item for item in manifests.values() if item["registration_pending"]]
    registered = [item for item in manifests.values() if not item["registration_pending"]]
    assert len(pending) == len(registered) == 1
    assert pending[0]["registration"]["status"] == "pending"
    assert registered[0]["registration"]["status"] == "registered"
    _assert_sensitive_free(pending[0])

    completed = api.surface_create_candidates(
        dry_run["operation_token"], destination["output_token"])
    assert completed["ok"] is True
    assert completed["replayed"] is True
    assert completed["registration_pending"] == []
    assert completed["discoverability"] == "complete"
    assert len(ledger.calls) == 3
    assert ledger.calls.count(next(name for name in ledger.calls if "term-01" in name)) == 1
    assert ledger.calls.count(next(name for name in ledger.calls if "term-02" in name)) == 2

    stable = api.surface_create_candidates(
        dry_run["operation_token"], destination["output_token"])
    assert stable == {**completed, "replayed": True}
    assert len(ledger.calls) == 3


def test_registration_recovery_rejects_tampered_poscar_same_process_and_restart(tmp_path):
    ledger = _FlakyLedger()
    api, _sources, _registrations, _source, output = _api(
        tmp_path, ledger=ledger, surface=_TwoCandidateSurface)
    confirmed = api.structure_source_confirm("1" * 32)["source_token"]
    dry_run = api.surface_dry_run(confirmed, {"miller": [1, 0, 0]}, {})
    destination = api.structure_output_select()
    partial = api.surface_create_candidates(
        dry_run["operation_token"], destination["output_token"])
    assert partial["ok"] is False and len(ledger.calls) == 2

    batch = next(path for path in output.iterdir() if path.is_dir())
    authority_path = batch / ".vcstudio-structure-recovery.json"
    authority = json.loads(authority_path.read_text(encoding="utf-8"))
    assert authority["schema"] == "vcstudio.structure-recovery/v1"
    assert authority["batch_directory_identity"]
    assert all(member["job_directory_identity"] for member in authority["members"])
    assert all(member["binding_sha256"] for member in authority["members"])
    pending_id = partial["registration_pending"][0]
    pending_member = next(
        member for member in authority["members"] if member["job_id"] == pending_id)
    pending_dir = batch / pending_member["job_name"]
    pending_manifest_path = pending_dir / "job.yaml"
    pending_manifest = yaml.safe_load(pending_manifest_path.read_text(encoding="utf-8"))
    assert pending_manifest["registration_recovery"]["binding_sha256"] == (
        pending_member["binding_sha256"])
    with (pending_dir / "POSCAR").open("a", encoding="utf-8") as handle:
        handle.write("tampered\n")

    same_process = api.surface_create_candidates(
        dry_run["operation_token"], destination["output_token"])
    assert same_process["ok"] is False
    assert same_process["corruption"] is True
    assert same_process["recovery_status"] == "corrupt"
    assert any(
        item["code"] in {"poscar_size_mismatch", "poscar_sha256_mismatch"}
        for item in same_process["corruptions"]
    )
    assert len(ledger.calls) == 2
    still_pending = yaml.safe_load(pending_manifest_path.read_text(encoding="utf-8"))
    assert still_pending["registration_pending"] is True

    restarted, _sources2, _registrations2, _source2, _output2 = _api(
        tmp_path, ledger=ledger, surface=_TwoCandidateSurface)
    restarted_destination = restarted.structure_output_select()
    after_restart = restarted.surface_recover_candidates(
        dry_run["operation_token"], restarted_destination["output_token"])
    assert after_restart["ok"] is False
    assert after_restart["corruption"] is True
    assert len(ledger.calls) == 2
    assert yaml.safe_load(pending_manifest_path.read_text(encoding="utf-8"))[
        "registration_pending"] is True


def test_registration_recovery_after_restart_only_registers_pending_member(tmp_path):
    ledger = _FlakyLedger()
    api, _sources, _registrations, _source, output = _api(
        tmp_path, ledger=ledger, surface=_TwoCandidateSurface)
    confirmed = api.structure_source_confirm("1" * 32)["source_token"]
    dry_run = api.surface_dry_run(confirmed, {"miller": [1, 0, 0]}, {})
    destination = api.structure_output_select()
    partial = api.surface_create_candidates(
        dry_run["operation_token"], destination["output_token"])
    assert partial["ok"] is False and len(ledger.calls) == 2

    restarted, _sources2, _registrations2, _source2, _output2 = _api(
        tmp_path, ledger=ledger, surface=_TwoCandidateSurface)
    restarted_destination = restarted.structure_output_select()
    completed = restarted.surface_recover_candidates(
        dry_run["operation_token"], restarted_destination["output_token"])

    assert completed["ok"] is True
    assert completed["replayed"] is True
    assert completed["registration_pending"] == []
    assert len(ledger.calls) == 3
    assert sum("term-01" in name for name in ledger.calls) == 1
    assert sum("term-02" in name for name in ledger.calls) == 2
    batch = next(path for path in output.iterdir() if path.is_dir())
    authority = json.loads(
        (batch / ".vcstudio-structure-recovery.json").read_text(encoding="utf-8"))
    assert authority["status"] == "complete"
    assert authority["registration_pending"] == []
    manifests = [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in batch.glob("*/job.yaml")
    ]
    assert manifests and all(item["registration_pending"] is False for item in manifests)

    stable = restarted.surface_recover_candidates(
        dry_run["operation_token"], restarted_destination["output_token"])
    assert stable["ok"] is True and stable["replayed"] is True
    assert len(ledger.calls) == 3


def test_registration_recovery_rejects_replaced_job_directory_with_same_files(tmp_path):
    ledger = _FlakyLedger()
    api, _sources, _registrations, _source, output = _api(
        tmp_path, ledger=ledger, surface=_TwoCandidateSurface)
    confirmed = api.structure_source_confirm("1" * 32)["source_token"]
    dry_run = api.surface_dry_run(confirmed, {"miller": [1, 0, 0]}, {})
    destination = api.structure_output_select()
    partial = api.surface_create_candidates(
        dry_run["operation_token"], destination["output_token"])
    batch = next(path for path in output.iterdir() if path.is_dir())
    authority = json.loads(
        (batch / ".vcstudio-structure-recovery.json").read_text(encoding="utf-8"))
    pending_id = partial["registration_pending"][0]
    member = next(item for item in authority["members"] if item["job_id"] == pending_id)
    original = batch / member["job_name"]
    displaced = batch / f"{member['job_name']}.displaced"
    original.rename(displaced)
    shutil.copytree(displaced, original)

    result = api.surface_create_candidates(
        dry_run["operation_token"], destination["output_token"])

    assert result["ok"] is False and result["corruption"] is True
    assert {item["code"] for item in result["corruptions"]} == {
        "job_directory_identity_mismatch"
    }
    assert len(ledger.calls) == 2
    assert yaml.safe_load((original / "job.yaml").read_text(encoding="utf-8"))[
        "registration_pending"] is True


def test_registration_recovery_rejects_manifest_operation_binding_tamper(tmp_path):
    ledger = _FlakyLedger()
    api, _sources, _registrations, _source, output = _api(
        tmp_path, ledger=ledger, surface=_TwoCandidateSurface)
    confirmed = api.structure_source_confirm("1" * 32)["source_token"]
    dry_run = api.surface_dry_run(confirmed, {"miller": [1, 0, 0]}, {})
    destination = api.structure_output_select()
    partial = api.surface_create_candidates(
        dry_run["operation_token"], destination["output_token"])
    batch = next(path for path in output.iterdir() if path.is_dir())
    authority = json.loads(
        (batch / ".vcstudio-structure-recovery.json").read_text(encoding="utf-8"))
    pending_id = partial["registration_pending"][0]
    member = next(item for item in authority["members"] if item["job_id"] == pending_id)
    manifest_path = batch / member["job_name"] / "job.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["inputs"]["structure_hub"]["operation_id"] = "0" * 32
    manifest_path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")

    result = api.surface_create_candidates(
        dry_run["operation_token"], destination["output_token"])

    assert result["ok"] is False and result["corruption"] is True
    assert {item["code"] for item in result["corruptions"]} == {
        "manifest_candidate_binding_mismatch"
    }
    assert len(ledger.calls) == 2
    assert yaml.safe_load(manifest_path.read_text(encoding="utf-8"))[
        "registration_pending"] is True


def test_success_dtos_and_manifest_provenance_are_recursively_sanitized(tmp_path):
    api, _sources, _registrations, _source, output = _api(
        tmp_path, sources=_MaliciousSources(), surface=_MaliciousSurface)
    selected = api.structure_source_select_local()
    preview = api.structure_source_preview(selected["results"][0]["token"])
    confirmed = api.structure_source_confirm(selected["results"][0]["token"])
    dry_run = api.surface_dry_run(
        confirmed["source_token"], {"miller": [1, 0, 0]}, {})
    destination = api.structure_output_select()
    created = api.surface_create_candidates(
        dry_run["operation_token"], destination["output_token"])

    assert preview["ok"] and dry_run["ok"] and created["ok"]
    for dto in (selected, preview, confirmed, dry_run, destination, created):
        _assert_sensitive_free(dto)
        _assert_path_free(dto)
    batch = next(path for path in output.iterdir() if path.is_dir())
    manifest_path = next(path for path in batch.rglob("job.yaml"))
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    _assert_sensitive_free(manifest["inputs"]["structure_hub"])
    _assert_path_free(manifest["inputs"]["structure_hub"])


def test_creation_rejects_browser_path_and_unknown_output_token(tmp_path):
    api, _sources, _registrations, _source, output = _api(tmp_path)
    confirmed = api.structure_source_confirm("1" * 32)["source_token"]
    dry_run = api.surface_dry_run(confirmed, {"miller": [1, 0, 0]}, {})

    result = api.surface_create_candidates(
        dry_run["operation_token"], str(output))

    assert result["ok"] is False and result["jobs"] == []
    assert not any(output.iterdir())
    _assert_path_free(result)


def test_real_local_provider_surface_core_and_api_complete_offline_closure(tmp_path):
    """真实模块联调：本地选择→预览确认→slab/site dry-run→candidate。"""
    source = tmp_path / "bulk" / "POSCAR"
    output = tmp_path / "candidates"
    source.parent.mkdir()
    output.mkdir()
    source.write_text(BULK_POSCAR, encoding="utf-8")
    registrations = []

    def dialog(kind):
        return str(output if kind == "dir" else source)

    api = Api(
        dialog_fn=dialog,
        ledger_mod=types.SimpleNamespace(
            register=lambda path: registrations.append(str(path)), load_all=lambda: []),
    )

    capabilities = api.structure_source_capabilities()
    selected = api.structure_source_select_local()
    preview = api.structure_source_preview(selected["results"][0]["token"])
    confirmed = api.structure_source_confirm(selected["results"][0]["token"])
    dry_run = api.surface_dry_run(
        confirmed["source_token"],
        {"miller": [1, 0, 0], "layers": 2, "vacuum": 10.0,
         "fixed_layers": 0, "surface_sides": "both"},
        {"sides": "both", "coverage": 1.0,
         "site_kinds": ["ontop", "bridge", "hollow", "other"]},
    )
    destination = api.structure_output_select()
    created = api.surface_create_candidates(
        dry_run["operation_token"], destination["output_token"])

    assert capabilities["ok"] and capabilities["providers"][0]["provider"] == "local"
    assert capabilities["providers"][0]["available"] is True
    assert set(selected["results"][0]) == {
        "token", "source_id", "formula", "license", "citation",
        "method", "raw_structure_sha256", "declared_formula",
        "declared_raw_structure_sha256", "computed_formula",
        "computed_structure_sha256",
    }
    assert selected["results"][0]["computed_structure_sha256"]
    assert preview["ok"] and preview["preview"]["view"]["natoms"] == 1
    assert preview["preview"]["top_view"]["projection"] == "xy"
    assert confirmed["ok"] and confirmed["confirmed"] is True
    assert dry_run["ok"] and dry_run["dry_run"]["candidate_count"] >= 1
    assert all(
        site["classification"] == "geometric_site_candidate"
        for slab in dry_run["dry_run"]["slabs"]
        for site in slab["sites"]
    )
    assert created["ok"] and len(created["jobs"]) == len(registrations)
    assert all(job["submission_ready"] is False for job in created["jobs"])
    _assert_path_free(selected)
    _assert_path_free(preview)
    _assert_path_free(dry_run)
    _assert_path_free(created)


def test_real_adsorbate_source_generates_site_bound_candidates_offline(tmp_path):
    bulk = tmp_path / "bulk" / "POSCAR"
    adsorbate = tmp_path / "adsorbate" / "POSCAR"
    output = tmp_path / "adsorption-candidates"
    bulk.parent.mkdir()
    adsorbate.parent.mkdir()
    output.mkdir()
    bulk.write_text(BULK_POSCAR, encoding="utf-8")
    adsorbate.write_text(ADSORBATE_POSCAR, encoding="utf-8")
    structures = iter((bulk, adsorbate))
    registrations = []

    def dialog(kind):
        return str(output if kind == "dir" else next(structures))

    api = Api(
        dialog_fn=dialog,
        ledger_mod=types.SimpleNamespace(
            register=lambda path: registrations.append(str(path)), load_all=lambda: []),
    )
    bulk_result = api.structure_source_select_local()["results"][0]
    assert api.structure_source_preview(bulk_result["token"])["ok"]
    bulk_token = api.structure_source_confirm(bulk_result["token"])["source_token"]
    ads_result = api.structure_source_select_local()["results"][0]
    assert api.structure_source_preview(ads_result["token"])["ok"]
    ads_token = api.structure_source_confirm(ads_result["token"])["source_token"]

    dry_run = api.surface_dry_run(
        bulk_token,
        {"miller": [1, 0, 0], "layers": 2, "vacuum": 10.0,
         "fixed_layers": 0, "surface_sides": "top"},
        {"adsorbate_source_token": ads_token, "sides": "top",
         "site_kinds": ["ontop"], "binding_atom": 0, "orientation": "normal",
         "rotations": [0, 180], "coverage": 1.0, "height": 2.0,
         "min_distance": 1.2},
    )
    destination = api.structure_output_select()
    created = api.surface_create_candidates(
        dry_run["operation_token"], destination["output_token"])

    assert dry_run["ok"] and dry_run["dry_run"]["candidate_count"] >= 1
    assert dry_run["dry_run"]["adsorbate_source"]["provider"] == "local"
    assert created["ok"] and created["jobs"] and len(created["jobs"]) == len(registrations)
    batch = next(path for path in output.iterdir() if path.is_dir())
    manifest = yaml.safe_load(
        (next(path for path in batch.iterdir() if path.is_dir()) / "job.yaml")
        .read_text(encoding="utf-8"))
    hub = manifest["inputs"]["structure_hub"]
    assert hub["adsorbate_source"]["provider"] == "local"
    assert hub["site"]["site_ids"]
    assert manifest["scientific_status"] == "candidate"
    _assert_path_free(dry_run)
    _assert_path_free(created)
