from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

from vcstudio.project.analysis_registry import get_analysis, normalize_analysis_request
from vcstudio.project.analysis_scientific import (
    REPORT_BINDING_SCHEMA,
    build_aimd_analysis_view,
    build_convergence_analysis_view,
    build_neb_analysis_view,
)
from tests.test_analysis_workbench import _analysis_api, _assert_public


PROJECT = "project-advanced-analysis"
METHOD = {
    "functional": "PBE",
    "dispersion": "D3",
    "encut": 500.0,
    "spin": 2,
    "kpoints_scheme": "Gamma 3x3x1",
    "potcar_ids": {"H": "PAW_PBE H"},
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _poscar(x: float) -> str:
    return (
        "H path\n1.0\n10 0 0\n0 10 0\n0 0 10\n"
        f"H\n1\nDirect\n{x:.6f} 0.0 0.0\n"
    )


def _outcar(energy: float, force: float) -> str:
    return (
        "aborting loop because EDIFF is reached\n"
        f" energy(sigma->0) = {energy:.8f}\n"
        " POSITION                                       TOTAL-FORCE (eV/Angst)\n"
        " -------------------------------------------------------------------\n"
        f" 0.0 0.0 0.0 {force:.8f} 0.0 0.0\n"
        " -------------------------------------------------------------------\n"
    )


def _target(path: Path, source_id: str, task_type: str, manifest: dict) -> dict:
    return {
        "path": str(path), "path_key": str(path), "source_id": source_id,
        "relation": "descendant", "task_type": task_type,
        "state": manifest.get("state", "DONE"), "manifest": manifest,
    }


def _method(_target):
    return {"status": "verified", "fingerprint": copy.deepcopy(METHOD)}


def _assert_quantities_have_provenance(value):
    if isinstance(value, dict):
        if {"value", "display", "unit", "denominator"} <= set(value):
            assert value["parser_module"] == "vcstudio.project.analysis_scientific"
            assert value["parser_version"]
            assert value["source_id"]
            assert isinstance(value["file_hashes"], list)
            for item in value["file_hashes"]:
                assert len(item["sha256"]) == 64
        for item in value.values():
            _assert_quantities_have_provenance(item)
    elif isinstance(value, list):
        for item in value:
            _assert_quantities_have_provenance(item)


def _neb_target(tmp_path: Path) -> dict:
    root = tmp_path / "neb"
    root.mkdir()
    (root / "INCAR").write_text(
        "EDIFFG = -0.05\nLCLIMB = .TRUE.\nIMAGES = 1\n",
        encoding="utf-8",
    )
    energies = (-10.0, -9.5, -10.2)
    for index, (energy, coordinate) in enumerate(zip(energies, (0.0, 0.1, 0.2))):
        frame = root / f"{index:02d}"
        frame.mkdir()
        (frame / "POSCAR").write_text(_poscar(coordinate), encoding="utf-8")
        (frame / "OSZICAR").write_text(
            f" 1 F= {energy:.8f} E0= {energy:.8f} d E =0\n",
            encoding="utf-8",
        )
        (frame / "OUTCAR").write_text(_outcar(energy, 0.02), encoding="utf-8")
    (root / "job.yaml").write_text("state: DONE\n", encoding="utf-8")
    endpoints = {
        role: {
            "trusted": True, "target_frame": frame, "source_state": "DONE",
            "method_fingerprint": copy.deepcopy(METHOD),
        }
        for role, frame in (("start", "00"), ("end", "02"))
    }
    manifest = {
        "state": "DONE", "task_type": "neb",
        "inputs": {"n_images": 1, "neb_endpoints": endpoints},
    }
    return _target(root, "neb-source", "neb", manifest)


def test_neb_view_only_releases_barriers_after_all_evidence_gates(tmp_path):
    target = _neb_target(tmp_path)
    spec = normalize_analysis_request(
        {"analysis_id": "neb-path", "precision": 5}, project_id=PROJECT)

    view = build_neb_analysis_view(spec, [target], method_evidence=_method)

    assert view["scientific_status"] == "diagnostic"
    assert view["denominator"]["barrier_qualified_paths"] == 1
    path = view["paths"][0]
    assert path["barriers"]["forward"]["display"] == "0.50000"
    assert path["barriers"]["reverse"]["display"] == "0.70000"
    assert [point["reaction_coordinate"]["value"] for point in path["points"]] == [
        0.0, 1.0, 2.0]
    assert all(point["electronic_convergence"] == "converged"
               for point in path["points"])
    assert path["points"][1]["ionic_convergence"] == "converged"
    assert "does not establish a complete mechanism" in path["scientific_boundary"]
    assert "Γ-point frequencies are not a phonon dispersion" in path["scientific_boundary"]
    assert view["figure_data"]["neb_profile"]["barrier_f"] == 0.5
    _assert_quantities_have_provenance(view)

    target["manifest"]["inputs"]["neb_endpoints"]["end"].pop(
        "method_fingerprint")
    withheld = build_neb_analysis_view(spec, [target], method_evidence=_method)
    blocked_path = withheld["paths"][0]
    assert blocked_path["barriers"]["status"] == "unavailable"
    assert blocked_path["barriers"]["forward"]["value"] is None
    assert blocked_path["points"][1]["relative_energy"]["value"] == 0.5
    assert any("endpoint method fingerprint" in item
               for item in blocked_path["barriers"]["blocking"])


def _convergence_targets(tmp_path: Path) -> list[dict]:
    targets = []
    for index, (encut, energy) in enumerate((
            (400, -1.0040), (450, -1.0008), (500, -1.0003), (550, -1.0000))):
        root = tmp_path / f"encut-{encut}"
        root.mkdir()
        (root / "job.yaml").write_text("state: DONE\n", encoding="utf-8")
        (root / "INCAR").write_text(f"ENCUT={encut}\n", encoding="utf-8")
        (root / "POSCAR").write_text(_poscar(0.0), encoding="utf-8")
        (root / "OSZICAR").write_text(
            f" 1 F= {energy:.8f} E0= {energy:.8f} d E =0\n",
            encoding="utf-8",
        )
        method = copy.deepcopy(METHOD)
        method["encut"] = float(encut)
        target = _target(root, f"conv-{index}", "conv_scan", {
            "state": "DONE", "task_type": "conv_scan", "parent_job": "parent",
            "inputs": {
                "parent_job": "parent", "series": "encut",
                "series_value": encut, "series_label": f"{encut} eV", "natoms": 1,
            },
        })
        target["method"] = {"status": "verified", "fingerprint": method}
        targets.append(target)
    return targets


def test_convergence_view_exposes_raw_points_platform_and_threshold_sensitivity(tmp_path):
    targets = _convergence_targets(tmp_path)
    spec = normalize_analysis_request(
        {"analysis_id": "convergence-scan", "precision": 4}, project_id=PROJECT)
    view = build_convergence_analysis_view(
        spec, targets, method_evidence=lambda target: target["method"])

    series = view["series"][0]
    assert [point["parameter"]["value"] for point in series["points"]] == [
        400.0, 450.0, 500.0, 550.0]
    assert series["points"][0]["delta_per_atom"]["value"] == pytest.approx(-4.0)
    assert series["platform"]["threshold_mev_per_atom"] == 1.0
    assert series["platform"]["minimum_points"] == 3
    assert series["platform"]["status"] == "available"
    assert series["platform"]["recommendation"]["value"] == 450.0
    assert [item["threshold"]["value"] for item in series["sensitivity"]] == [
        0.5, 1.0, 2.0]
    assert series["sensitivity"][0]["recommended_parameter"]["value"] is None
    assert view["denominator"]["available_recommendations"] == 1
    assert view["figure_data"]["convergence_curve"]["points"][-1]["energy"] == -1.0
    _assert_quantities_have_provenance(view)

    targets[2]["state"] = "RUNNING"
    targets[2]["manifest"]["state"] = "RUNNING"
    missing = build_convergence_analysis_view(
        spec, targets, method_evidence=lambda target: target["method"])
    assert missing["series"][0]["platform"]["status"] == "unavailable"
    assert missing["series"][0]["platform"]["recommendation"]["value"] is None


def _xdatcar() -> str:
    return (
        "H trajectory\n1.0\n10 0 0\n0 10 0\n0 0 10\nH\n1\n"
        "Direct configuration=     1\n0.0000 0 0\n"
        "Direct configuration=     2\n0.0100 0 0\n"
        "Direct configuration=     3\n0.0200 0 0\n"
    )


def test_aimd_view_is_diagnostic_and_never_promotes_short_trajectory(tmp_path):
    root = tmp_path / "aimd"
    root.mkdir()
    (root / "job.yaml").write_text("state: DONE\n", encoding="utf-8")
    (root / "INCAR").write_text(
        "IBRION=0\nNSW=3\nPOTIM=1.0\nTEBEG=300\n", encoding="utf-8")
    (root / "POSCAR").write_text(_poscar(0.0), encoding="utf-8")
    (root / "OSZICAR").write_text(
        " 1 T= 300.0 E= -10.0000 F= -10.1 E0= -10.2 EK= 0.1\n"
        " 2 T= 310.0 E= -9.9900 F= -10.1 E0= -10.2 EK= 0.1\n"
        " 3 T= 290.0 E= -9.9800 F= -10.1 E0= -10.2 EK= 0.1\n",
        encoding="utf-8",
    )
    (root / "XDATCAR").write_text(_xdatcar(), encoding="utf-8")
    target = _target(root, "aimd-source", "aimd", {
        "state": "DONE", "task_type": "aimd",
        "inputs": {"potim_fs": 1.0, "steps": 3, "temp_k": 300.0},
    })
    spec = normalize_analysis_request(
        {"analysis_id": "aimd-diagnostics", "precision": 6}, project_id=PROJECT)

    view = build_aimd_analysis_view(spec, [target], method_evidence=_method)

    trajectory = view["trajectories"][0]
    assert view["scientific_status"] == "diagnostic"
    assert trajectory["metrics"]["sampling_length"]["value"] == 0.003
    assert trajectory["metrics"]["energy_drift_total"]["value"] == pytest.approx(0.02)
    assert trajectory["metrics"]["temperature_mean"]["value"] == 300.0
    assert trajectory["metrics"]["trajectory_frames"]["value"] == 3.0
    assert trajectory["metrics"]["max_step_displacement"]["value"] == 0.1
    assert any("short-trajectory diagnostic threshold" in item
               for item in trajectory["warnings"])
    assert "does not prove thermal stability" in trajectory["scientific_boundary"]
    assert view["report_binding"]["final_allowed"] is False
    assert view["figure_data"]["aimd_diagnostic"]["steps"][0]["temperature"] == 300.0
    _assert_quantities_have_provenance(view)


def test_report_binding_uses_registry_sections_and_frozen_view_hashes(tmp_path):
    target = _neb_target(tmp_path)
    spec = normalize_analysis_request(
        {"analysis_id": "neb-path"}, project_id=PROJECT)
    view = build_neb_analysis_view(spec, [target], method_evidence=_method)
    binding = view["report_binding"]

    assert binding["schema"] == REPORT_BINDING_SCHEMA
    assert binding["report_sections"] == get_analysis("neb-path")["report_sections"]
    assert binding["view_data_fingerprint"] == view["data_fingerprint"]
    assert binding["figures"][0]["preset_key"] == "neb_profile"
    assert binding["figures"][0]["data_sha256"]
    assert binding["report_kind"] == "diagnostic"
    assert binding["scientific_qualification"] == "diagnostic"
    assert binding["final_allowed"] is False
    assert len(binding["binding_sha256"]) == 64
    assert _sha(Path(target["path"]) / "00" / "OSZICAR") in str(view)


def test_api_bootstrap_dispatches_registered_neb_capability_without_paths(tmp_path):
    api, project_paths, projects = _analysis_api(tmp_path)
    project_id = api._workspace_project_id(
        project_paths[0], projects[project_paths[0]])
    target = _neb_target(tmp_path)
    api._analysis_workbench_targets = lambda _context: [target]
    api._analysis_workbench_method_evidence = _method

    response = api.analysis_workbench_bootstrap(project_id, "neb-path")

    assert response["ok"] is True
    assert response["default_spec"]["analysis_id"] == "neb-path"
    assert response["view"]["denominator"]["barrier_qualified_paths"] == 1
    record = next(item for item in response["catalog"]["analyses"]
                  if item["id"] == "neb-path")
    assert record["implementation_status"] == "live"
    assert record["capability_status"] == "available"
    assert record["activatable"] is True
    _assert_public(response, tmp_path)
