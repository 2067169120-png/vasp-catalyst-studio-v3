from __future__ import annotations

import copy
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from vcstudio.project.research_explorer import (
    ResearchExplorerError,
    ResearchIndexService,
)


def _records(tmp_path, *, engines=("vasp", "vasp"), statuses=("verified", "verified")):
    records = []
    manifests = {}
    for index, (engine, status) in enumerate(zip(engines, statuses), start=1):
        job = tmp_path / f"project-{index}" / "config"
        job.mkdir(parents=True)
        manifest = {
            "job_id": f"job-{index}",
            "system": "Pt4S",
            "task_type": "static",
            "state": "DONE",
            "inputs": {
                "engine": engine,
                "formula": "Pt4S",
                "sha256": {"POSCAR": f"input-{index}"},
                "source_id": f"source-{index}",
            },
            "results": {
                "energy_e0_eV": -100.0 - index,
                "barrier_eV": 0.4 + index / 10,
                "hashes": {"OUTCAR": f"output-{index}"},
                "parser": "vcstudio.energy",
                "validation": {"status": "passed"},
            },
            "validation": {"status": "verified"},
        }
        manifests[str(job)] = manifest
        records.append({
            "project_id": f"project-{index}",
            "identity_fingerprint": f"identity-{index}",
            "project": {
                "name": f"Project {index}",
                "formula": "Pt4S",
                "members": {"clean_slab": None, "gas_ref": None, "configs": [str(job)]},
                "config_species": {str(job): "Li2S8"},
            },
            "engine": engine,
            "method_status": status,
        })
    return records, manifests


def _rebuild(service, records, manifests, *, summaries=None, validation_resolver=None,
             report_binding_resolver=None, manifest_loader=None):
    summaries = summaries or {}

    def summary(project):
        project_id = next(
            record["project_id"] for record in records if record["project"] is project)
        return copy.deepcopy(summaries.get(project_id, {"rows": []}))

    def method(target):
        record = next(
            record for record in records
            if str(target["path"]) in record["project"]["config_species"]
            or str(target["path"]) in {
                str(value) for value in (record["project"].get("members") or {}).values()
                if isinstance(value, str)
            }
            or str(target["path"]) in {
                str(value) for value in (record["project"].get("species_ref_jobs") or {}).values()
            })
        return {
            "status": record["method_status"],
            "engine": record["engine"],
            "fingerprint": {"functional": "PBE", "dispersion": "D3"},
            "missing": [],
        }

    return service.rebuild(
        records,
        manifest_loader=manifest_loader or (lambda path: manifests.get(str(path))),
        summary_loader=summary,
        job_id_resolver=lambda _path, manifest: manifest.get("job_id", ""),
        method_resolver=method,
        validation_resolver=validation_resolver,
        report_binding_resolver=report_binding_resolver,
        registry_total=len(records),
    )


def _adsorption_summaries(records, values=(-2.1, -1.9), species=("Li2S8", "Li2S8")):
    result = {}
    for record, value, reference_species in zip(records, values, species):
        result[record["project_id"]] = {
            "reference_mode": "species",
            "rows": [{
                "name": "config",
                "species": "Li2S8",
                "delta_e": value,
                "reference_valid": True,
                "reference_species": reference_species,
                "reference_source": "OSZICAR:E0",
                "method_check": {"status": "verified"},
            }],
        }
    return result


def test_method_cohort_includes_engine_and_excludes_nonverified_rows(tmp_path):
    records, manifests = _records(
        tmp_path, engines=("vasp", "quantum-espresso"),
        statuses=("verified", "unverified"))
    service = ResearchIndexService()
    _rebuild(service, records, manifests, summaries=_adsorption_summaries(records))

    result = service.query()

    assert result["method_compatibility"]["compatible_rows"] == 1
    assert result["method_compatibility"]["nonverified_rows"] == 1
    assert result["histogram"]["sample_count"] == 1
    assert result["periodic_table"]["sample_count"] == 1
    assert result["table"]["rows"][0]["engine"] == "vasp"

    unfiltered = service.query({"filters": {"method_compatible": False}})
    assert unfiltered["table"]["sample_count"] == 2
    assert unfiltered["histogram"]["status"] == "unavailable_method_compatibility"
    assert unfiltered["periodic_table"]["status"] == "unavailable_method_compatibility"
    assert unfiltered["scatter"]["points"] == []


def test_same_method_different_engines_never_share_a_method_fingerprint(tmp_path):
    records, manifests = _records(tmp_path, engines=("vasp", "quantum-espresso"))
    service = ResearchIndexService()
    _rebuild(service, records, manifests, summaries=_adsorption_summaries(records))

    result = service.query({"filters": {"method_compatible": False}})
    rows = result["table"]["rows"]

    assert len({row["method_fingerprint"] for row in rows}) == 2
    assert result["method_compatibility"]["status"] == "mixed_or_unverified"


def test_missing_engine_cannot_be_assumed_or_enter_a_scientific_cohort(tmp_path):
    records, manifests = _records(tmp_path, engines=("",), statuses=("verified",))
    service = ResearchIndexService()
    _rebuild(service, records, manifests, summaries=_adsorption_summaries(records))

    guarded = service.query()
    assert guarded["method_compatibility"]["status"] == "blocked_missing_method"
    assert guarded["table"]["sample_count"] == 0

    diagnostic = service.query({"filters": {"method_compatible": False}})
    row = diagnostic["table"]["rows"][0]
    assert row["engine"] == "unknown"
    assert row["method_status"] == "unverified"
    assert diagnostic["histogram"]["status"] == "unavailable_method_compatibility"


def test_fingerprint_without_explicit_verified_status_stays_unverified(tmp_path):
    records, manifests = _records(tmp_path, engines=("vasp",), statuses=("",))
    service = ResearchIndexService()
    _rebuild(service, records, manifests, summaries=_adsorption_summaries(records))

    result = service.query({"filters": {"method_compatible": False}})
    assert result["table"]["rows"][0]["method_fingerprint"]
    assert result["table"]["rows"][0]["method_status"] == "unverified"
    assert result["histogram"]["status"] == "unavailable_method_compatibility"


@pytest.mark.parametrize(
    ("second_summary", "expected_contracts"),
    [
        ({"rows": []}, {"adsorption_energy", "total_energy"}),
        ({
            "reference_mode": "species",
            "rows": [{
                "name": "config", "species": "Li2S8", "delta_e": -1.9,
                "reference_valid": True, "reference_species": "S8",
                "reference_source": "OSZICAR:E0",
                "method_check": {"status": "verified"},
            }],
        }, {"adsorption_energy"}),
    ],
)
def test_energy_quantity_or_reference_mismatch_makes_aggregates_unavailable(
        tmp_path, second_summary, expected_contracts):
    records, manifests = _records(tmp_path)
    summaries = _adsorption_summaries(records)
    summaries["project-2"] = second_summary
    service = ResearchIndexService()
    _rebuild(service, records, manifests, summaries=summaries)

    result = service.query()

    assert {row["energy_quantity"] for row in result["table"]["rows"]} == expected_contracts
    assert result["energy_compatibility"]["status"] == "unavailable_incompatible_energy_contract"
    assert result["histogram"]["status"] == "unavailable_incompatible_energy_contract"
    assert result["histogram"]["bins"] == []
    assert result["scatter"]["status"] == "unavailable_incompatible_energy_contract"
    assert result["scatter"]["points"] == []
    assert result["periodic_table"]["status"] == "unavailable_incompatible_energy_contract"
    assert all(cell["mean_energy_eV"] is None
               for cell in result["periodic_table"]["cells"])


def test_manifest_validation_strings_cannot_upgrade_evidence_to_verified(tmp_path):
    records, manifests = _records(tmp_path, engines=("vasp",), statuses=("verified",))
    service = ResearchIndexService()
    _rebuild(service, records, manifests)

    row = service.query()["table"]["rows"][0]
    assert row["provenance_status"] == "observed"
    assert row["validation_status"] == "verified"
    assert row["evidence_level"] == "unverified"


def test_only_current_hash_bound_authority_can_verify_observed_numeric_evidence(tmp_path):
    records, manifests = _records(tmp_path, engines=("vasp",), statuses=("verified",))
    service = ResearchIndexService()

    def authoritative(target):
        return {
            "authority": "validation_result",
            "status": "verified",
            "hash_bound": True,
            "current": True,
            "output_hash_bound": True,
            "job_id": target["job_id"],
            "source_id": target["source_id"],
            "verified_quantities": target["quantity_sha256"],
        }

    _rebuild(service, records, manifests, validation_resolver=authoritative)
    assert service.query()["table"]["rows"][0]["evidence_level"] == "verified"

    _rebuild(
        service, records, manifests,
        validation_resolver=lambda target: {**authoritative(target), "current": False})
    assert service.query()["table"]["rows"][0]["evidence_level"] == "unverified"


def test_adsorption_provenance_lists_all_operands_and_requires_revalidated_report_binding(
        tmp_path):
    root = tmp_path / "project"
    clean, config, reference = (root / name for name in ("clean", "config", "reference"))
    for path in (clean, config, reference):
        path.mkdir(parents=True)
    manifests = {}
    for path, job_id, source_id in (
            (clean, "job-clean", "source-clean"),
            (config, "job-config", "source-config"),
            (reference, "job-reference", "source-reference")):
        manifests[str(path)] = {
            "job_id": job_id, "system": "Pt4S", "task_type": "static", "state": "DONE",
            "inputs": {"engine": "vasp", "formula": "Pt4S",
                       "sha256": {"POSCAR": f"input-{job_id}"}, "source_id": source_id},
            "results": {"energy_e0_eV": -100.0, "hashes": {"OUTCAR": f"out-{job_id}"},
                        "parser": "vcstudio.energy"},
        }
    records = [{
        "project_id": "project-1", "identity_fingerprint": "identity-1",
        "engine": "vasp", "method_status": "verified",
        "project": {
            "name": "Operands", "formula": "Pt4S",
            "members": {"clean_slab": str(clean), "gas_ref": None,
                        "configs": [str(config)]},
            "config_species": {str(config): "Li2S8"},
            "species_ref_jobs": {"Li2S8": str(reference)},
            "autopilot_report": {"revision_id": "report-r0001"},
        },
    }]
    summaries = {"project-1": {
        "reference_mode": "species", "slab": (str(clean), -100.0),
        "rows": [{
            "name": "config", "job": str(config), "species": "Li2S8",
            "delta_e": -2.0, "reference_valid": True,
            "reference_species": "Li2S8", "reference_job": str(reference),
            "reference_source": "OSZICAR:E0", "method_check": {"status": "verified"},
        }],
    }}
    service = ResearchIndexService()
    _rebuild(service, records, manifests, summaries=summaries)
    graph = service.live_provenance("project-1", job_id="job-config")

    operand_edges = [edge for edge in graph["edges"] if edge["type"] == "uses_operand"]
    assert {edge["operand_role"] for edge in operand_edges} == {
        "configuration", "clean_slab", "reference"}
    assert {node["record"].get("job_id") for node in graph["nodes"]
            if node["type"] == "job"} >= {"job-config", "job-clean", "job-reference"}
    assert not any(edge["type"] == "reported_in" for edge in graph["edges"])

    def bound_report(target):
        config_entry = next(
            entry for entry in target["entries"] if entry["job_id"] == "job-config")
        return {
            "revision_id": "report-r0001", "current": True,
            "frozen_graph_revalidated": True,
            "bound_job_ids": ["job-config", "job-clean", "job-reference"],
            "bound_analyses": [{
                "job_id": "job-config", "source_id": "source-config",
                "quantity": "energy_eV",
                "energy_contract_id": config_entry["energy_contract_id"],
                "quantity_sha256": config_entry["_quantity_sha256"]["energy_eV"],
            }],
        }

    _rebuild(
        service, records, manifests, summaries=summaries,
        report_binding_resolver=bound_report)
    bound = service.live_provenance("project-1", job_id="job-config")
    assert any(edge["type"] == "reported_in" for edge in bound["edges"])
    report = next(node for node in bound["nodes"] if node["type"] == "report")
    assert report["origin_status"] == "observed"


def test_rebuild_query_freshness_and_cursor_are_one_locked_immutable_snapshot(tmp_path):
    records, manifests = _records(
        tmp_path, engines=("vasp", "vasp"), statuses=("verified", "verified"))
    service = ResearchIndexService()
    _rebuild(service, records, manifests)
    first = service.query({"limit": 1})
    original_snapshot_id = first["freshness"]["snapshot_id"]
    assert first["table"]["next_cursor"]

    changed = copy.deepcopy(manifests)
    changed[next(iter(changed))]["results"]["energy_e0_eV"] = -999.0
    entered = threading.Event()
    release = threading.Event()

    def delayed_loader(path):
        entered.set()
        assert release.wait(timeout=5)
        return changed.get(str(path))

    with ThreadPoolExecutor(max_workers=2) as pool:
        rebuild_future = pool.submit(
            _rebuild, service, records, changed, manifest_loader=delayed_loader)
        assert entered.wait(timeout=5)
        query_future = pool.submit(service.query, {"limit": 1})
        assert not query_future.done()
        release.set()
        rebuild_future.result(timeout=5)
        second = query_future.result(timeout=5)

    assert second["freshness"]["snapshot_id"] != original_snapshot_id
    assert second["table"]["rows"][0]["energy_eV"] == -999.0
    assert second["freshness"]["snapshot_id"] == service.index_status()["snapshot_id"]
    with pytest.raises(ResearchExplorerError, match="cursor is stale"):
        service.query({"limit": 1, "cursor": first["table"]["next_cursor"]})

    changed[next(iter(changed))]["results"]["energy_e0_eV"] = -7.0
    assert service.query()["table"]["rows"][0]["energy_eV"] == -999.0
