from __future__ import annotations

import json

import pytest

from vcstudio.project.research_explorer import (
    PROVENANCE_SCHEMA,
    QUERY_SCHEMA,
    ResearchExplorerError,
    ResearchIndexService,
)


def _fixture(tmp_path):
    roots = [tmp_path / name for name in ("alpha", "beta", "gamma")]
    manifests = {}
    records = []
    methods = ["PBE-D3", "PBE-D3", "SCAN-rVV10"]
    energies = [-2.2, -1.7, -2.8]
    barriers = [0.45, 0.62, 0.31]
    formulas = ["Pt4S", "Pt3NiS", "Fe2O3"]
    for index, (root, method, energy, barrier, formula) in enumerate(zip(
            roots, methods, energies, barriers, formulas), start=1):
        job = root / "config"
        job.mkdir(parents=True)
        manifest = {
            "job_id": f"job-{index}",
            "system": formula,
            "task_type": "neb" if index == 3 else "static",
            "state": "DONE",
            "inputs": {
                "engine": "vasp",
                "formula": formula,
                "facet": "111" if index < 3 else "110",
                "sha256": {"POSCAR": f"hash-{index}"},
                "source_id": f"source-{index}",
            },
            "results": {
                "energy_e0_eV": -100.0 - index,
                "barrier_eV": barrier,
                "hashes": {"OUTCAR": f"out-{index}"},
                "parser": "vcstudio.neb" if index == 3 else "vcstudio.energy",
                "validation": {"status": "passed"},
            },
            "validation": {"status": "passed"},
            "attempts": ([{"kind": "resume"}] if index == 2 else []),
        }
        manifests[str(job)] = manifest
        project = {
            "name": f"Project {index}",
            "formula": formula,
            "facet": "111" if index < 3 else "110",
            "members": {"clean_slab": None, "gas_ref": None, "configs": [str(job)]},
            "config_species": {str(job): "Li2S8" if index < 3 else "CO"},
            "autopilot_report": (
                {"revision_id": f"report-{index}", "contracts": {"manifest": {}}}
                if index == 1 else {}),
        }
        records.append({
            "project_id": f"project-{index}",
            "identity_fingerprint": f"identity-{index}",
            "project": project,
            "summary_energy": energy,
            "method": method,
        })
    return records, manifests


def _build(tmp_path, *, failures=(), max_age_seconds=300, monotonic=None):
    records, manifests = _fixture(tmp_path)
    service = ResearchIndexService(
        max_age_seconds=max_age_seconds,
        **({"monotonic": monotonic} if monotonic else {}),
    )

    def summary(project):
        record = next(item for item in records if item["project"] is project)
        species = next(iter(project["config_species"].values()))
        return {"reference_mode": "species", "rows": [{
            "name": "config", "species": next(iter(project["config_species"].values())),
            "configuration_id": f"job-{records.index(record) + 1}",
            "delta_e": record["summary_energy"], "reference_valid": True,
            "reference_species": species, "reference_source": "OSZICAR:E0",
            "method_check": {"status": "verified"},
        }]}

    def method(target):
        record = next(
            item for item in records
            if str(target["path"]) in item["project"]["config_species"])
        return {
            "status": "verified", "engine": "vasp",
            "schema": "vcstudio.method-fingerprint/vasp/v1",
            "fingerprint": {"recipe": record["method"]}, "missing": [],
        }

    service.rebuild(
        records,
        manifest_loader=lambda path: manifests.get(str(path)),
        summary_loader=summary,
        job_id_resolver=lambda _path, manifest: manifest.get("job_id", ""),
        method_resolver=method,
        registry_total=3 + len(failures),
        registry_failures=failures,
    )
    return service, records


def test_query_defaults_to_one_compatible_method_and_server_finalizes_all_dtos(tmp_path):
    service, _records = _build(tmp_path)

    result = service.query({"limit": 1})

    assert result["ok"] is True
    assert result["schema"] == QUERY_SCHEMA
    assert result["freshness"]["status"] == "ready"
    assert result["freshness"]["registry_total"] == 3
    compatibility = result["method_compatibility"]
    assert compatibility["enabled"] is True
    assert compatibility["status"] == "compatible"
    assert compatibility["input_rows"] == 3
    assert compatibility["compatible_rows"] == 2
    assert compatibility["excluded_rows"] == 1
    assert result["table"]["sample_count"] == 2
    assert result["table"]["visible_count"] == 1
    assert result["table"]["next_cursor"]
    row = result["table"]["rows"][0]
    assert row["units"] == {"energy_eV": "eV", "barrier_eV": "eV"}
    assert row["drilldown"]["job"] == {
        "project_id": row["project_id"], "job_id": row["job_id"]}
    assert result["histogram"]["status"] == "ready"
    assert result["histogram"]["sample_count"] == 2
    assert result["scatter"]["status"] == "ready"
    assert result["scatter"]["sample_count"] == 2
    assert all("x_percent" in point and "y_percent" in point
               for point in result["scatter"]["points"])
    periodic = {cell["element"]: cell for cell in result["periodic_table"]["cells"]}
    assert periodic["Pt"]["sample_count"] == 2
    assert periodic["Pt"]["group"] == 10
    assert result["denominator"]["missing_by_field"]["barrier_eV"] == 0

    second = service.query({"limit": 1, "cursor": result["table"]["next_cursor"]})
    assert second["table"]["offset"] == 1
    assert second["table"]["rows"][0]["job_id"] != row["job_id"]
    assert second["table"]["next_cursor"] is None


def test_filters_ranges_sort_and_missing_values_are_server_owned(tmp_path):
    service, _records = _build(tmp_path)
    result = service.query({
        "filters": {
            "elements": ["Pt"], "facet": "111", "adsorbate": "Li2S8",
            "energy_min_eV": -2.3, "energy_max_eV": -1.8,
            "method_compatible": True,
        },
        "sort": {"key": "energy_eV", "direction": "desc"},
        "axes": {"x": "barrier_eV", "y": "energy_eV"},
        "limit": 20,
    })

    assert result["table"]["sample_count"] == 1
    row = result["table"]["rows"][0]
    assert row["formula"] == "Pt4S"
    assert row["energy_eV"] == pytest.approx(-2.2)
    assert row["energy_quantity"] == "adsorption_energy"
    assert result["scatter"]["axes"] == {
        "x": "barrier_eV", "y": "energy_eV"}
    assert result["histogram"]["metric"] == "barrier_eV"


def test_mixed_method_scatter_is_blocked_when_user_explicitly_disables_filter(tmp_path):
    service, _records = _build(tmp_path)
    result = service.query({
        "filters": {"method_compatible": False},
        "axes": {"x": "energy_eV", "y": "barrier_eV"},
    })

    assert result["table"]["sample_count"] == 3
    assert result["method_compatibility"]["status"] == "mixed_or_unverified"
    assert result["scatter"]["status"] == "blocked_mixed_or_unverified_methods"
    assert result["scatter"]["points"] == []


def test_partial_and_stale_indexes_fail_closed_without_scientific_rows(tmp_path):
    partial, _records = _build(
        tmp_path / "partial",
        failures=[{"project_ref": "registered-project-4", "code": "unreadable"}],
    )
    result = partial.query()
    assert result["ok"] is False
    assert result["status"] == "partial"
    assert result["table"]["rows"] == []
    assert result["histogram"] is None
    assert result["denominator"]["registry_total"] == 4
    assert result["denominator"]["indexed_projects"] == 3

    ticks = iter([0.0, 20.0, 20.0, 20.0])
    stale, _records = _build(
        tmp_path / "stale", max_age_seconds=5,
        monotonic=lambda: next(ticks),
    )
    stale_result = stale.query()
    assert stale_result["status"] == "stale"
    assert stale_result["table"]["rows"] == []


def test_live_provenance_separates_layers_and_never_merges_frozen_report_graph(tmp_path):
    service, _records = _build(tmp_path)
    result = service.query({"filters": {
        "project_ids": ["project-1"], "method_compatible": True}})
    row = result["table"]["rows"][0]

    graph = service.live_provenance(
        row["project_id"], job_id=row["job_id"], source_id=row["source_id"])

    assert graph["schema"] == PROVENANCE_SCHEMA
    assert graph["graph_kind"] == "live_derived"
    assert graph["report_frozen_graph_included"] is False
    assert set(graph["layers"]) == {"data", "logical"}
    node_types = {node["type"] for node in graph["nodes"]}
    assert {"input", "job", "parser", "analysis", "method", "validation", "report"} <= node_types
    assert {node["layer"] for node in graph["nodes"]} == {"data", "logical"}
    origins = {node["origin_status"] for node in graph["nodes"]}
    assert origins <= {"observed", "imported", "inferred", "missing"}
    report = next(node for node in graph["nodes"] if node["type"] == "report")
    assert report["origin_status"] == "missing"
    assert report["record"]["frozen_graph_separate"] is True
    assert report["record"]["frozen_graph_endpoint"] == "report_evidence_graph"
    assert not any(edge["type"] == "reported_in" for edge in graph["edges"])

    encoded = json.dumps({"query": result, "graph": graph}, ensure_ascii=False)
    assert str(tmp_path) not in encoded
    assert "project.yaml" not in encoded


def test_query_contract_rejects_client_results_and_stale_cursors(tmp_path):
    service, _records = _build(tmp_path)
    with pytest.raises(ResearchExplorerError, match="unknown fields"):
        service.query({"scientific_rows": [{"energy_eV": -99}]})
    with pytest.raises(ResearchExplorerError, match="limit"):
        service.query({"limit": 1000})
    with pytest.raises(ResearchExplorerError, match="cursor"):
        service.query({"cursor": "not-a-valid-cursor"})
