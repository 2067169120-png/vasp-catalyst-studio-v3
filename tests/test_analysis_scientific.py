from __future__ import annotations

import copy
import hashlib
import json
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


def _write_manifest(root: Path, value: dict) -> None:
    (root / "job.yaml").write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _poscar(x: float) -> str:
    return (
        "H path\n1.0\n10 0 0\n0 10 0\n0 0 10\n"
        f"H\n1\nDirect\n{x:.6f} 0.0 0.0\n"
    )


def _slab_poscar(
    layers: int, stacking: str = "ABC", *, inner_buckle_a: float = 0.02,
) -> str:
    spacing = 2.0
    vacuum = 10.0
    c_length = vacuum + spacing * (layers - 1)
    shifts = {
        "AA": ((0.0, 0.0),),
        "ABC": ((0.0, 0.0), (1.0 / 3.0, 1.0 / 3.0),
                (2.0 / 3.0, 2.0 / 3.0)),
    }[stacking]
    rows = []
    for index in range(layers):
        x, y = shifts[index % len(shifts)]
        center = vacuum / 2.0 + spacing * index
        atoms = (
            (x, y, center - 0.10),
            ((x + 0.25) % 1.0, y, center - inner_buckle_a),
            (x, (y + 0.25) % 1.0, center + inner_buckle_a),
            ((x + 0.25) % 1.0, (y + 0.25) % 1.0, center + 0.10),
        )
        rows.extend(
            f"{atom_x:.10f} {atom_y:.10f} {atom_z / c_length:.10f}"
            for atom_x, atom_y, atom_z in atoms
        )
    return (
        f"H {stacking} slab\n1.0\n10 0 0\n0 10 0\n0 0 {c_length:.10f}\n"
        f"H\n{layers * 4}\nDirect\n" + "\n".join(rows) + "\n"
    )


def _outcar(energy: float, force: float) -> str:
    return (
        " NIONS =      1 ions\n"
        "aborting loop because EDIFF is reached\n"
        f" energy(sigma->0) = {energy:.8f}\n"
        " POSITION                                       TOTAL-FORCE (eV/Angst)\n"
        " -------------------------------------------------------------------\n"
        f" 0.0 0.0 0.0 {force:.8f} 0.0 0.0\n"
        " -------------------------------------------------------------------\n"
        " General timing and accounting informations for this job:\n"
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
    root.mkdir(parents=True)
    (root / "INCAR").write_text(
        "EDIFFG = -0.05\nLCLIMB = .TRUE.\nIMAGES = 1\n",
        encoding="utf-8",
    )
    (root / "OUTCAR").write_text(
        " General timing and accounting informations for this job:\n",
        encoding="utf-8",
    )
    energies = (-10.0, -9.5, -10.2)
    endpoint_targets = []
    endpoint_records = {}
    for index, (energy, coordinate) in enumerate(zip(energies, (0.0, 0.1, 0.2))):
        frame = root / f"{index:02d}"
        frame.mkdir()
        (frame / "POSCAR").write_text(_poscar(coordinate), encoding="utf-8")
        (frame / "OSZICAR").write_text(
            f" 1 F= {energy:.8f} E0= {energy:.8f} d E =0\n",
            encoding="utf-8",
        )
        (frame / "OUTCAR").write_text(_outcar(energy, 0.02), encoding="utf-8")
    for role, frame in (("start", "00"), ("end", "02")):
        source_root = tmp_path / f"{role}-source"
        source_root.mkdir()
        for name in ("POSCAR", "OSZICAR", "OUTCAR"):
            (source_root / name).write_bytes((root / frame / name).read_bytes())
        source_manifest = {
            "state": "DONE", "task_type": "relax", "inputs": {},
            "results": {"diagnosis": {
                "failure_class": "CONVERGED", "exit_code": 0,
                "clean_exit": True,
            }},
        }
        _write_manifest(source_root, source_manifest)
        source_id = f"{role}-endpoint-source"
        endpoint_targets.append(
            _target(source_root, source_id, "relax", source_manifest))
        endpoint_records[role] = {
            # These deliberately forged hints must not participate in authority.
            "trusted": False, "source_state": "FAILED",
            "method_fingerprint": {"forged": True},
            "target_frame": frame, "source_job_id": source_id,
            "files": [
                {"name": name, "sha256": _sha(root / frame / name)}
                for name in ("POSCAR", "OSZICAR", "OUTCAR")
            ],
        }
    manifest = {
        "state": "DONE", "task_type": "neb",
        "inputs": {"n_images": 1, "neb_endpoints": endpoint_records},
        "results": {"diagnosis": {
            "failure_class": "CONVERGED", "exit_code": 0,
            "clean_exit": True,
        }},
    }
    _write_manifest(root, manifest)
    target = _target(root, "neb-source", "neb", manifest)
    target["_endpoint_targets"] = endpoint_targets
    return target


def _neb_targets(target: dict) -> list[dict]:
    return [target, *target["_endpoint_targets"]]


def test_neb_view_only_releases_barriers_after_all_evidence_gates(tmp_path):
    target = _neb_target(tmp_path)
    spec = normalize_analysis_request(
        {"analysis_id": "neb-path", "precision": 5}, project_id=PROJECT)

    view = build_neb_analysis_view(spec, _neb_targets(target), method_evidence=_method)

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
    assert path["endpoint_evidence"]["start"]["status"] == "verified"
    assert path["endpoint_evidence"]["start"]["source"]["source_id"] == (
        "start-endpoint-source")
    assert "does not establish a complete mechanism" in path["scientific_boundary"]
    assert "Γ-point frequencies are not a phonon dispersion" in path["scientific_boundary"]
    assert view["figure_data"]["neb_profile"]["barrier_f"] == 0.5
    _assert_quantities_have_provenance(view)

    target["manifest"]["inputs"]["neb_endpoints"]["end"].pop("source_job_id")
    _write_manifest(Path(target["path"]), target["manifest"])
    withheld = build_neb_analysis_view(
        spec, _neb_targets(target), method_evidence=_method)
    blocked_path = withheld["paths"][0]
    assert blocked_path["barriers"]["status"] == "unavailable"
    assert blocked_path["barriers"]["forward"]["value"] is None
    assert blocked_path["points"][1]["relative_energy"]["value"] == 0.5
    assert any("source_job_id" in item
               for item in blocked_path["barriers"]["blocking"])

    target = _neb_target(tmp_path / "second")
    source = Path(target["_endpoint_targets"][0]["path"])
    (source / "OSZICAR").write_text(
        " 1 F= -99 E0= -99 d E =0\n", encoding="utf-8")
    changed_source = build_neb_analysis_view(
        spec, _neb_targets(target), method_evidence=_method)
    assert changed_source["paths"][0]["barriers"]["status"] == "unavailable"
    assert any("no longer matches" in item for item in
               changed_source["paths"][0]["barriers"]["blocking"])


def test_neb_rejects_oszicar_energy_from_a_different_final_ionic_event(tmp_path):
    target = _neb_target(tmp_path)
    root = Path(target["path"])
    (root / "01" / "OSZICAR").write_text(
        " 1 F= -1.00000000 E0= -1.00000000 d E =0\n",
        encoding="utf-8",
    )
    spec = normalize_analysis_request(
        {"analysis_id": "neb-path"}, project_id=PROJECT)

    view = build_neb_analysis_view(
        spec, _neb_targets(target), method_evidence=_method)

    path = view["paths"][0]
    assert path["points"][1]["absolute_energy"]["value"] is None
    assert path["points"][1]["max_force"]["value"] is None
    assert path["barriers"]["status"] == "unavailable"
    assert any("energy disagree" in issue for issue in path["barriers"]["blocking"])


def test_neb_requires_exact_continuous_image_names_and_expected_endpoint_frame(
        tmp_path, monkeypatch):
    target = _neb_target(tmp_path)
    root = Path(target["path"])
    (root / "02").rename(root / "03")
    target["manifest"]["inputs"]["neb_endpoints"]["end"]["target_frame"] = "03"
    _write_manifest(root, target["manifest"])
    spec = normalize_analysis_request(
        {"analysis_id": "neb-path"}, project_id=PROJECT)

    view = build_neb_analysis_view(
        spec, _neb_targets(target), method_evidence=_method)

    path = view["paths"][0]
    assert path["barriers"]["status"] == "unavailable"
    assert path["endpoint_evidence"]["end"]["status"] == "unavailable"
    assert any("exact continuous set" in issue
               for issue in path["barriers"]["blocking"])
    assert any("target_frame does not match 02" in issue
               for issue in path["endpoint_evidence"]["end"]["issues"])

    input_mismatch = _neb_target(tmp_path / "input-mismatch")
    (Path(input_mismatch["path"]) / "INCAR").write_text(
        "EDIFFG = -0.05\nLCLIMB = .TRUE.\nIMAGES = 2\n",
        encoding="utf-8",
    )
    mismatched = build_neb_analysis_view(
        spec, _neb_targets(input_mismatch), method_evidence=_method)
    assert mismatched["paths"][0]["barriers"]["status"] == "unavailable"
    assert any("does not match current INCAR IMAGES" in issue
               for issue in mismatched["paths"][0]["barriers"]["blocking"])

    missing_manifest_n = _neb_target(tmp_path / "missing-manifest-n")
    missing_manifest_n["manifest"]["inputs"].pop("n_images")
    _write_manifest(
        Path(missing_manifest_n["path"]), missing_manifest_n["manifest"])
    missing_n = build_neb_analysis_view(
        spec, _neb_targets(missing_manifest_n), method_evidence=_method)
    assert missing_n["paths"][0]["barriers"]["status"] == "unavailable"
    assert any("positive integer n_images" in issue
               for issue in missing_n["paths"][0]["barriers"]["blocking"])

    import vcstudio.project.analysis_scientific as analysis_scientific
    bounded_target = _neb_target(tmp_path / "bounded")
    monkeypatch.setattr(analysis_scientific, "MAX_NEB_FRAMES", 2)
    bounded = build_neb_analysis_view(
        spec, _neb_targets(bounded_target), method_evidence=_method)
    bounded_path = bounded["paths"][0]
    assert bounded_path["status"] == "unavailable"
    assert bounded_path["available"] is False
    assert any("hard limit" in issue
               for issue in bounded_path["barriers"]["blocking"])


def test_neb_main_and_endpoint_require_consistent_done_diagnosis(tmp_path):
    spec = normalize_analysis_request(
        {"analysis_id": "neb-path"}, project_id=PROJECT)
    main_target = _neb_target(tmp_path / "main")
    main_target["manifest"]["results"]["diagnosis"] = {
        "failure_class": "NEB_IMAGE_MISSING", "exit_code": 137,
        "clean_exit": False,
    }
    _write_manifest(Path(main_target["path"]), main_target["manifest"])

    main_view = build_neb_analysis_view(
        spec, _neb_targets(main_target), method_evidence=_method)

    main_path = main_view["paths"][0]
    assert main_path["completion_evidence"]["status"] == "unavailable"
    assert main_path["barriers"]["status"] == "unavailable"
    assert any("CONVERGED" in issue for issue in main_path["barriers"]["blocking"])

    endpoint_target = _neb_target(tmp_path / "endpoint")
    endpoint = endpoint_target["_endpoint_targets"][0]
    endpoint["manifest"]["results"]["diagnosis"] = {
        "failure_class": "NEB_IMAGE_MISSING", "exit_code": 137,
        "clean_exit": False,
    }
    _write_manifest(Path(endpoint["path"]), endpoint["manifest"])

    endpoint_view = build_neb_analysis_view(
        spec, _neb_targets(endpoint_target), method_evidence=_method)

    endpoint_path = endpoint_view["paths"][0]
    assert endpoint_path["endpoint_evidence"]["start"]["status"] == "unavailable"
    assert endpoint_path["barriers"]["status"] == "unavailable"
    assert any("CONVERGED" in issue
               for issue in endpoint_path["endpoint_evidence"]["start"]["issues"])


@pytest.mark.parametrize(("diagnosis", "keep_footer", "message"), [
    ({"failure_class": "CONVERGED", "exit_code": 137, "clean_exit": True},
     True, "退出码"),
    ({"failure_class": "CONVERGED", "exit_code": 0, "clean_exit": False},
     True, "clean_exit=false"),
    ({"failure_class": "CONVERGED", "exit_code": 0, "clean_exit": True},
     False, "OUTCAR timing"),
])
def test_neb_main_completion_gate_fails_closed(
        tmp_path, diagnosis, keep_footer, message):
    target = _neb_target(tmp_path)
    target["manifest"]["results"]["diagnosis"] = diagnosis
    root = Path(target["path"])
    _write_manifest(root, target["manifest"])
    if not keep_footer:
        (root / "OUTCAR").write_text("incomplete run\n", encoding="utf-8")
    spec = normalize_analysis_request(
        {"analysis_id": "neb-path"}, project_id=PROJECT)

    view = build_neb_analysis_view(
        spec, _neb_targets(target), method_evidence=_method)

    path = view["paths"][0]
    assert path["completion_evidence"]["status"] == "unavailable"
    assert path["barriers"]["status"] == "unavailable"
    assert any(message in issue for issue in path["barriers"]["blocking"])


def _convergence_targets(tmp_path: Path) -> list[dict]:
    targets = []
    for index, (encut, energy) in enumerate((
            (400, -1.0040), (450, -1.0008), (500, -1.0003), (550, -1.0000))):
        root = tmp_path / f"encut-{encut}"
        root.mkdir(parents=True)
        (root / "INCAR").write_text(f"ENCUT={encut}\n", encoding="utf-8")
        (root / "KPOINTS").write_text(
            "mesh\n0\nGamma\n3 3 1\n0 0 0\n", encoding="utf-8")
        (root / "POTCAR").write_text(
            "TITEL = PAW_PBE H\n", encoding="utf-8")
        (root / "POSCAR").write_text(_poscar(0.0), encoding="utf-8")
        (root / "OUTCAR").write_text(
            _convergence_outcar(energy), encoding="utf-8")
        (root / "OSZICAR").write_text(
            f" 1 F= {energy:.8f} E0= {energy:.8f} d E =0\n",
            encoding="utf-8",
        )
        method = copy.deepcopy(METHOD)
        method["encut"] = float(encut)
        manifest = {
            "state": "DONE", "task_type": "conv_scan", "parent_job": "parent",
            "inputs": {
                "parent_job": "parent", "series": "encut",
                "series_value": encut, "series_label": f"{encut} eV", "natoms": 1,
                "sha256": {"POSCAR": _sha(root / "POSCAR")},
            },
            "results": {
                "energy_e0_eV": energy,
                "diagnosis": {
                    "failure_class": "CONVERGED", "exit_code": 0,
                    "clean_exit": True,
                },
            },
        }
        _write_manifest(root, manifest)
        target = _target(root, f"conv-{index}", "conv_scan", manifest)
        target["method"] = {"status": "verified", "fingerprint": method}
        targets.append(target)
    return targets


def _convergence_outcar(
    *energies: float, clean: bool = True, nions: int = 1,
) -> str:
    lines = [f" NIONS = {nions} ions"]
    lines.extend(
        f" energy without entropy= {energy:.8f} energy(sigma->0) = {energy:.8f}"
        for energy in energies
    )
    if clean:
        lines.append(" General timing and accounting informations for this job:")
    return "\n".join(lines) + "\n"


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
    assert {item["name"] for item in
            series["points"][0]["energy_per_atom"]["file_hashes"]} >= {
                "job.yaml", "POSCAR", "OUTCAR", "OSZICAR"}
    _assert_quantities_have_provenance(view)

    targets[2]["state"] = "RUNNING"
    targets[2]["manifest"]["state"] = "RUNNING"
    _write_manifest(Path(targets[2]["path"]), targets[2]["manifest"])
    missing = build_convergence_analysis_view(
        spec, targets, method_evidence=lambda target: target["method"])
    assert missing["series"][0]["platform"]["status"] == "unavailable"
    assert missing["series"][0]["platform"]["recommendation"]["value"] is None


def test_convergence_any_method_issue_or_natoms_mismatch_blocks_recommendation(tmp_path):
    spec = normalize_analysis_request(
        {"analysis_id": "convergence-scan"}, project_id=PROJECT)
    targets = _convergence_targets(tmp_path / "method")
    targets[1]["method"] = {
        "status": "verified", "fingerprint": copy.deepcopy(METHOD),
        "warnings": ["input drift"],
    }

    method_blocked = build_convergence_analysis_view(
        spec, targets, method_evidence=lambda target: target["method"])

    assert method_blocked["series"][0]["platform"]["status"] == "unavailable"
    assert method_blocked["series"][0]["platform"]["point_indexes"] == []
    assert any("every convergence point" in issue
               for issue in method_blocked["series"][0]["issues"])

    targets = _convergence_targets(tmp_path / "natoms")
    bad = targets[2]
    (Path(bad["path"]) / "OUTCAR").write_text(
        " NIONS = 2 ions\n", encoding="utf-8")

    denominator_blocked = build_convergence_analysis_view(
        spec, targets, method_evidence=lambda target: target["method"])

    point = denominator_blocked["series"][0]["points"][2]
    assert point["atom_count"]["value"] is None
    assert point["energy_per_atom"]["value"] is None
    assert denominator_blocked["series"][0]["platform"]["status"] == "unavailable"
    assert any("atom counts disagree" in issue
               for issue in denominator_blocked["series"][0]["issues"])


def test_convergence_coordinate_must_match_current_input_bytes(tmp_path):
    targets = _convergence_targets(tmp_path)
    for target in targets:
        (Path(target["path"]) / "INCAR").write_text(
            "ENCUT=400\n", encoding="utf-8")
    spec = normalize_analysis_request(
        {"analysis_id": "convergence-scan"}, project_id=PROJECT)

    view = build_convergence_analysis_view(
        spec, targets, method_evidence=lambda target: target["method"])

    series = view["series"][0]
    assert [point["parameter"]["value"] for point in series["points"]] == [
        400.0, 400.0, 400.0, 400.0]
    assert sum(point["coordinate_verified"] for point in series["points"]) == 1
    assert series["platform"]["status"] == "unavailable"
    assert series["platform"]["recommendation"]["value"] is None
    assert any("does not match the current input value" in issue
               for issue in series["issues"])


def test_convergence_rejects_geometry_drift_between_otherwise_valid_points(tmp_path):
    targets = _convergence_targets(tmp_path)
    for target, coordinate in zip(targets, (0.0, 0.2, 0.4, 0.6)):
        root = Path(target["path"])
        (root / "POSCAR").write_text(_poscar(coordinate), encoding="utf-8")
        target["manifest"]["inputs"]["sha256"]["POSCAR"] = _sha(root / "POSCAR")
        _write_manifest(root, target["manifest"])
    spec = normalize_analysis_request(
        {"analysis_id": "convergence-scan"}, project_id=PROJECT)

    view = build_convergence_analysis_view(
        spec, targets, method_evidence=lambda target: target["method"])

    series = view["series"][0]
    assert all(point["absolute_energy"]["value"] is not None
               for point in series["points"])
    assert all(point["frozen_input_evidence"]["status"] == "verified"
               for point in series["points"])
    assert series["platform"]["status"] == "unavailable"
    assert series["platform"]["recommendation"]["value"] is None
    assert any("frozen inputs/structure differ" in issue
               for issue in series["issues"])


def test_slab_thickness_invariant_retains_ordered_stacking_registration(tmp_path):
    targets = _convergence_targets(tmp_path)
    per_atom_energies = (-1.0040, -1.0008, -1.0003, -1.0000)
    for target, layers, per_atom in zip(targets, range(3, 7), per_atom_energies):
        root = Path(target["path"])
        natoms = layers * 4
        energy = natoms * per_atom
        (root / "INCAR").write_text("ENCUT=500\n", encoding="utf-8")
        (root / "POSCAR").write_text(
            _slab_poscar(layers, "ABC"), encoding="utf-8")
        (root / "OUTCAR").write_text(
            _convergence_outcar(energy, nions=natoms), encoding="utf-8")
        (root / "OSZICAR").write_text(
            f" 1 F= {energy:.8f} E0= {energy:.8f} d E =0\n",
            encoding="utf-8",
        )
        target["manifest"]["inputs"].update({
            "series": "slab_thickness", "series_value": layers,
            "series_label": f"{layers} layers", "natoms": natoms,
            "sha256": {"POSCAR": _sha(root / "POSCAR")},
        })
        target["manifest"]["results"]["energy_e0_eV"] = energy
        target["method"] = {
            "status": "verified", "fingerprint": copy.deepcopy(METHOD)}
        _write_manifest(root, target["manifest"])
    spec = normalize_analysis_request(
        {"analysis_id": "convergence-scan"}, project_id=PROJECT)

    consistent = build_convergence_analysis_view(
        spec, targets, method_evidence=lambda target: target["method"])
    series = consistent["series"][0]
    assert [point["parameter"]["value"] for point in series["points"]] == [
        3.0, 4.0, 5.0, 6.0]
    assert series["platform"]["status"] == "available"
    assert series["platform"]["recommendation"]["value"] == 4.0
    assert len({point["frozen_input_evidence"]["invariant_sha256"]
                for point in series["points"]}) == 1

    drifted = targets[2]
    drifted_root = Path(drifted["path"])
    (drifted_root / "POSCAR").write_text(
        _slab_poscar(5, "ABC", inner_buckle_a=0.05), encoding="utf-8")
    drifted["manifest"]["inputs"]["sha256"]["POSCAR"] = _sha(
        drifted_root / "POSCAR")
    _write_manifest(drifted_root, drifted["manifest"])

    mixed = build_convergence_analysis_view(
        spec, targets, method_evidence=lambda target: target["method"])
    mixed_series = mixed["series"][0]
    assert all(point["absolute_energy"]["value"] is not None
               for point in mixed_series["points"])
    assert mixed_series["platform"]["status"] == "unavailable"
    assert mixed_series["platform"]["recommendation"]["value"] is None
    assert any("frozen inputs/structure differ" in issue
               for issue in mixed_series["issues"])

    (drifted_root / "POSCAR").write_text(
        _slab_poscar(5, "AA"), encoding="utf-8")
    drifted["manifest"]["inputs"]["sha256"]["POSCAR"] = _sha(
        drifted_root / "POSCAR")
    _write_manifest(drifted_root, drifted["manifest"])
    stacking_mixed = build_convergence_analysis_view(
        spec, targets, method_evidence=lambda target: target["method"])
    assert stacking_mixed["series"][0]["platform"]["status"] == "unavailable"
    assert any("frozen inputs/structure differ" in issue
               for issue in stacking_mixed["series"][0]["issues"])


def test_convergence_requires_current_clean_completion_and_matching_energy(tmp_path):
    targets = _convergence_targets(tmp_path)
    root = Path(targets[1]["path"])
    energy = targets[1]["manifest"]["results"]["energy_e0_eV"]
    (root / "OUTCAR").write_text(
        _convergence_outcar(energy, clean=False), encoding="utf-8")
    spec = normalize_analysis_request(
        {"analysis_id": "convergence-scan"}, project_id=PROJECT)

    no_footer = build_convergence_analysis_view(
        spec, targets, method_evidence=lambda target: target["method"])

    series = no_footer["series"][0]
    assert series["points"][1]["absolute_energy"]["value"] is None
    assert series["platform"]["status"] == "unavailable"
    assert any("OUTCAR timing" in issue for issue in series["issues"])

    (root / "OUTCAR").write_text(
        _convergence_outcar(energy), encoding="utf-8",
    )
    targets[1]["manifest"]["results"]["diagnosis"]["exit_code"] = 7
    _write_manifest(root, targets[1]["manifest"])
    bad_exit = build_convergence_analysis_view(
        spec, targets, method_evidence=lambda target: target["method"])
    assert bad_exit["series"][0]["points"][1]["absolute_energy"]["value"] is None
    assert any("退出码" in issue for issue in bad_exit["series"][0]["issues"])

    targets[1]["manifest"]["results"]["diagnosis"]["exit_code"] = 0
    targets[1]["manifest"]["results"]["diagnosis"]["clean_exit"] = False
    _write_manifest(root, targets[1]["manifest"])
    explicitly_unclean = build_convergence_analysis_view(
        spec, targets, method_evidence=lambda target: target["method"])
    assert explicitly_unclean["series"][0]["points"][1][
        "absolute_energy"]["value"] is None
    assert explicitly_unclean["series"][0]["platform"]["status"] == "unavailable"
    assert any("clean_exit=false" in issue
               for issue in explicitly_unclean["series"][0]["issues"])

    targets[1]["manifest"]["results"]["diagnosis"]["clean_exit"] = True
    targets[1]["manifest"]["results"]["energy_e0_eV"] = -99.0
    _write_manifest(root, targets[1]["manifest"])
    mismatch = build_convergence_analysis_view(
        spec, targets, method_evidence=lambda target: target["method"])
    assert mismatch["series"][0]["points"][1]["absolute_energy"]["value"] is None
    assert any("不一致" in issue for issue in mismatch["series"][0]["issues"])


def test_convergence_fake_plateau_rejects_foreign_outcar_energy_events(tmp_path):
    targets = _convergence_targets(tmp_path)
    first_energy = targets[0]["manifest"]["results"]["energy_e0_eV"]
    first_root = Path(targets[0]["path"])
    (first_root / "OSZICAR").write_text(
        f" 2 F= {first_energy:.8f} E0= {first_energy:.8f} d E =0\n",
        encoding="utf-8",
    )

    second_energy = targets[1]["manifest"]["results"]["energy_e0_eV"]
    (Path(targets[1]["path"]) / "OUTCAR").write_text(
        _convergence_outcar(-99.0, second_energy), encoding="utf-8")
    third_energy = targets[2]["manifest"]["results"]["energy_e0_eV"]
    (Path(targets[2]["path"]) / "OUTCAR").write_text(
        _convergence_outcar(third_energy - 5.0), encoding="utf-8")
    fourth_energy = targets[3]["manifest"]["results"]["energy_e0_eV"]
    (Path(targets[3]["path"]) / "OUTCAR").write_text(
        " NIONS = 1 ions\n"
        " General timing and accounting informations for this job:\n"
        f" energy(sigma->0) = {fourth_energy:.8f}\n",
        encoding="utf-8",
    )
    spec = normalize_analysis_request(
        {"analysis_id": "convergence-scan"}, project_id=PROJECT)

    view = build_convergence_analysis_view(
        spec, targets, method_evidence=lambda target: target["method"])

    series = view["series"][0]
    assert all(point["absolute_energy"]["value"] is None
               for point in series["points"])
    assert series["platform"]["status"] == "unavailable"
    assert series["platform"]["recommendation"]["value"] is None
    assert view["denominator"]["available_recommendations"] == 0
    assert any("序列/终态不连续" in issue for issue in series["issues"])
    assert any("事件计数不一致" in issue for issue in series["issues"])
    assert any("最终离子事件能量不一致" in issue for issue in series["issues"])
    assert any("页脚不在最终离子能量事件之后" in issue for issue in series["issues"])


def test_convergence_fails_whole_projection_if_source_changes_mid_parse(tmp_path):
    from vcstudio.project.analysis_sources import SourceSnapshotChanged

    targets = _convergence_targets(tmp_path)
    spec = normalize_analysis_request(
        {"analysis_id": "convergence-scan"}, project_id=PROJECT)
    changed = False

    def mutate_after_capture(target, _snapshot):
        nonlocal changed
        if not changed:
            changed = True
            (Path(target["path"]) / "OSZICAR").write_text(
                " 1 F= -99 E0= -99 d E =0\n", encoding="utf-8")
        return target["method"]

    with pytest.raises(SourceSnapshotChanged, match="changed during analysis"):
        build_convergence_analysis_view(
            spec, targets, method_evidence=mutate_after_capture)


def test_convergence_enforces_one_total_snapshot_budget_for_the_whole_view(
        tmp_path, monkeypatch):
    import vcstudio.project.analysis_sources as analysis_sources

    targets = _convergence_targets(tmp_path)
    first_root = Path(targets[0]["path"])
    first_total = sum(
        path.stat().st_size for path in first_root.iterdir() if path.is_file())
    monkeypatch.setattr(
        analysis_sources, "MAX_EVIDENCE_TOTAL_BYTES", first_total + 8)
    spec = normalize_analysis_request(
        {"analysis_id": "convergence-scan"}, project_id=PROJECT)

    view = build_convergence_analysis_view(
        spec, targets, method_evidence=lambda target: target["method"])

    series = view["series"][0]
    assert series["status"] == "unavailable"
    assert series["available"] is False
    assert series["platform"]["status"] == "unavailable"
    assert any("remaining total byte limit" in issue for issue in series["issues"])


def test_convergence_points_without_lineage_are_never_combined(tmp_path):
    targets = _convergence_targets(tmp_path)[:2]
    for target in targets:
        target["manifest"].pop("parent_job", None)
        target["manifest"]["inputs"].pop("parent_job", None)
        _write_manifest(Path(target["path"]), target["manifest"])
    spec = normalize_analysis_request(
        {"analysis_id": "convergence-scan"}, project_id=PROJECT)

    view = build_convergence_analysis_view(
        spec, targets, method_evidence=lambda target: target["method"])

    assert len(view["series"]) == 2
    assert all(len(series["points"]) == 1 for series in view["series"])
    assert all(series["platform"]["status"] == "unavailable"
               for series in view["series"])


def _xdatcar() -> str:
    return (
        "H trajectory\n1.0\n10 0 0\n0 10 0\n0 0 10\nH\n1\n"
        "Direct configuration=     1\n0.0000 0 0\n"
        "Direct configuration=     2\n0.0100 0 0\n"
        "Direct configuration=     3\n0.0200 0 0\n"
    )


def test_aimd_view_is_diagnostic_and_never_promotes_short_trajectory(
        tmp_path, monkeypatch):
    root = tmp_path / "aimd"
    root.mkdir()
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
    manifest = {
        "state": "DONE", "task_type": "aimd",
        "inputs": {"potim_fs": 1.0, "steps": 3, "temp_k": 300.0},
    }
    _write_manifest(root, manifest)
    target = _target(root, "aimd-source", "aimd", manifest)
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

    import vcstudio.project.analysis_sources as analysis_sources
    monkeypatch.setattr(analysis_sources, "MAX_XDATCAR_FRAMES", 2)
    over_limit = build_aimd_analysis_view(
        spec, [target], method_evidence=_method)
    rejected = over_limit["trajectories"][0]
    assert rejected["status"] == "unavailable"
    assert rejected["available"] is False
    assert all(rejected["metrics"][key]["value"] is None for key in (
        "trajectory_frames", "max_step_displacement", "final_rmsd", "max_rmsd"))
    assert any("frame limit" in issue for issue in rejected["issues"])


def test_aimd_done_rejects_incomplete_or_unbound_xdatcar(tmp_path):
    root = tmp_path / "aimd-incomplete-xdatcar"
    root.mkdir()
    (root / "INCAR").write_text(
        "IBRION=0\nNSW=3\nPOTIM=1.0\nTEBEG=300\n", encoding="utf-8")
    (root / "POSCAR").write_text(_poscar(0.0), encoding="utf-8")
    (root / "OSZICAR").write_text(
        " 1 T= 300 E= -10.000 F= -10 E0= -10 EK= 0.1\n"
        " 2 T= 301 E= -9.990 F= -10 E0= -10 EK= 0.1\n"
        " 3 T= 302 E= -9.980 F= -10 E0= -10 EK= 0.1\n",
        encoding="utf-8",
    )
    one_frame = "\n".join(_xdatcar().splitlines()[:9]) + "\n"
    (root / "XDATCAR").write_text(one_frame, encoding="utf-8")
    manifest = {
        "state": "DONE", "task_type": "aimd",
        "inputs": {"potim_fs": 1.0, "steps": 3, "temp_k": 300.0},
    }
    _write_manifest(root, manifest)
    target = _target(root, "aimd-incomplete-xdatcar", "aimd", manifest)
    spec = normalize_analysis_request(
        {"analysis_id": "aimd-diagnostics"}, project_id=PROJECT)

    view = build_aimd_analysis_view(spec, [target], method_evidence=_method)

    trajectory = view["trajectories"][0]
    assert trajectory["structure_diagnostics"]["status"] == "unavailable"
    assert trajectory["structure_diagnostics"]["nblock"] == 1
    assert trajectory["structure_diagnostics"]["observed_frame_count"] == 1
    for key in ("trajectory_frames", "max_step_displacement", "final_rmsd", "max_rmsd"):
        assert trajectory["metrics"][key]["value"] is None
    assert any("configuration sequence" in issue for issue in trajectory["issues"])
    assert any("at least two bound frames" in issue for issue in trajectory["issues"])

    mismatched = _xdatcar().replace("\nH\n1\n", "\nHe\n1\n").replace(
        "10 0 0", "9 0 0", 1)
    (root / "XDATCAR").write_text(mismatched, encoding="utf-8")
    rebound = build_aimd_analysis_view(spec, [target], method_evidence=_method)
    rebound_trajectory = rebound["trajectories"][0]
    assert rebound_trajectory["structure_diagnostics"]["status"] == "unavailable"
    assert any("atom count/elements" in issue for issue in rebound_trajectory["issues"])
    assert any("cell does not match" in issue for issue in rebound_trajectory["issues"])


def test_aimd_variable_cell_repeated_header_blocks_all_structure_metrics(tmp_path):
    root = tmp_path / "aimd-variable-cell"
    root.mkdir()
    (root / "INCAR").write_text(
        "IBRION=0\nNSW=3\nPOTIM=1.0\nTEBEG=300\n", encoding="utf-8")
    (root / "POSCAR").write_text(_poscar(0.0), encoding="utf-8")
    (root / "OSZICAR").write_text("".join(
        f" {step} T= 300 E= -10.0 F= -10 E0= -10 EK= 0.1\n"
        for step in range(1, 4)
    ), encoding="utf-8")
    first_header = "H trajectory\n1.0\n10 0 0\n0 10 0\n0 0 10\nH\n1\n"
    changed_header = "H trajectory\n1.0\n11 0 0\n0 11 0\n0 0 11\nH\n1\n"
    (root / "XDATCAR").write_text(
        first_header
        + "Direct configuration=     1\n0.0000 0 0\n"
        + changed_header
        + "Direct configuration=     2\n0.0000 0 0\n"
        + changed_header
        + "Direct configuration=     3\n0.0000 0 0\n",
        encoding="utf-8",
    )
    manifest = {
        "state": "DONE", "task_type": "aimd",
        "inputs": {"potim_fs": 1.0, "steps": 3, "temp_k": 300.0},
    }
    _write_manifest(root, manifest)
    target = _target(root, "aimd-variable-cell", "aimd", manifest)
    spec = normalize_analysis_request(
        {"analysis_id": "aimd-diagnostics"}, project_id=PROJECT)

    view = build_aimd_analysis_view(spec, [target], method_evidence=_method)

    trajectory = view["trajectories"][0]
    assert trajectory["structure_diagnostics"]["status"] == "unavailable"
    assert "variable-cell" in trajectory["structure_diagnostics"]["parser_error"]
    for key in ("trajectory_frames", "max_step_displacement", "final_rmsd", "max_rmsd"):
        assert trajectory["metrics"][key]["value"] is None
    assert any("variable-cell" in issue for issue in trajectory["issues"])


def test_aimd_xdatcar_sequence_honors_explicit_nblock(tmp_path):
    root = tmp_path / "aimd-nblock"
    root.mkdir()
    (root / "INCAR").write_text(
        "IBRION=0\nNSW=4\nNBLOCK=2\nPOTIM=1.0\nTEBEG=300\n", encoding="utf-8")
    (root / "POSCAR").write_text(_poscar(0.0), encoding="utf-8")
    (root / "OSZICAR").write_text("".join(
        f" {step} T= 300 E= {-10 + step / 1000:.6f} F= -10 E0= -10 EK= 0.1\n"
        for step in range(1, 5)
    ), encoding="utf-8")
    header = "\n".join(_xdatcar().splitlines()[:7])
    (root / "XDATCAR").write_text(
        header + "\n"
        "Direct configuration=     2\n0.0000 0 0\n"
        "Direct configuration=     4\n0.0200 0 0\n",
        encoding="utf-8",
    )
    manifest = {
        "state": "DONE", "task_type": "aimd",
        "inputs": {"potim_fs": 1.0, "steps": 4, "temp_k": 300.0},
    }
    _write_manifest(root, manifest)
    target = _target(root, "aimd-nblock", "aimd", manifest)
    spec = normalize_analysis_request(
        {"analysis_id": "aimd-diagnostics"}, project_id=PROJECT)

    view = build_aimd_analysis_view(spec, [target], method_evidence=_method)

    trajectory = view["trajectories"][0]
    assert trajectory["structure_diagnostics"]["status"] == "available"
    assert trajectory["structure_diagnostics"]["expected_frame_steps"] == [2, 4]
    assert trajectory["structure_diagnostics"]["observed_frame_steps"] == [2, 4]
    assert trajectory["metrics"]["trajectory_frames"]["value"] == 2
    assert trajectory["metrics"]["max_step_displacement"]["value"] == pytest.approx(0.2)


def test_aimd_restart_is_segmented_without_cross_segment_duration_or_drift(tmp_path):
    root = tmp_path / "aimd-restart"
    root.mkdir()
    (root / "INCAR").write_text(
        "IBRION=0\nNSW=4\nPOTIM=1.0\nTEBEG=300\n", encoding="utf-8")
    (root / "POSCAR").write_text(_poscar(0.0), encoding="utf-8")
    (root / "OSZICAR").write_text(
        " 1 T= 300 E= -10.000 F= -10 E0= -10 EK= 0.1\n"
        " 2 T= 301 E= -9.990 F= -10 E0= -10 EK= 0.1\n"
        " 1 T= 302 E= -9.800 F= -10 E0= -10 EK= 0.1\n"
        " 2 T= 303 E= -9.790 F= -10 E0= -10 EK= 0.1\n",
        encoding="utf-8",
    )
    (root / "XDATCAR").write_text(_xdatcar(), encoding="utf-8")
    manifest = {
        "state": "DONE", "task_type": "aimd",
        "inputs": {"potim_fs": 1.0, "steps": 4, "temp_k": 300.0},
    }
    _write_manifest(root, manifest)
    target = _target(root, "aimd-restart", "aimd", manifest)
    spec = normalize_analysis_request(
        {"analysis_id": "aimd-diagnostics", "precision": 6}, project_id=PROJECT)

    view = build_aimd_analysis_view(spec, [target], method_evidence=_method)

    trajectory = view["trajectories"][0]
    assert len(trajectory["segments"]) == 2
    assert trajectory["metrics"]["sampling_length"]["value"] is None
    assert trajectory["metrics"]["energy_drift_total"]["value"] is None
    assert trajectory["metrics"]["energy_drift_slope"]["value"] is None
    assert [sample["step"]["value"] for sample in trajectory["samples"]] == [1, 2, 1, 2]
    assert [sample["segment_index"] for sample in trajectory["samples"]] == [0, 0, 1, 1]
    assert [sample["time"]["value"] for sample in trajectory["samples"]] == [
        0.001, 0.002, 0.001, 0.002]
    assert view["figure_data"] == {}
    assert any("restart, regression, or gap" in issue for issue in trajectory["issues"])


def test_aimd_step_gap_splits_segments_and_blocks_done_aggregates(tmp_path):
    root = tmp_path / "aimd-gap"
    root.mkdir()
    (root / "INCAR").write_text(
        "IBRION=0\nNSW=100\nPOTIM=1.0\nTEBEG=300\n", encoding="utf-8")
    (root / "POSCAR").write_text(_poscar(0.0), encoding="utf-8")
    (root / "OSZICAR").write_text(
        " 1 T= 300 E= -10.000 F= -10 E0= -10 EK= 0.1\n"
        " 2 T= 301 E= -9.990 F= -10 E0= -10 EK= 0.1\n"
        " 100 T= 302 E= -9.500 F= -10 E0= -10 EK= 0.1\n",
        encoding="utf-8",
    )
    (root / "XDATCAR").write_text(_xdatcar(), encoding="utf-8")
    manifest = {
        "state": "DONE", "task_type": "aimd",
        "inputs": {"potim_fs": 1.0, "steps": 100, "temp_k": 300.0},
    }
    _write_manifest(root, manifest)
    target = _target(root, "aimd-gap", "aimd", manifest)
    spec = normalize_analysis_request(
        {"analysis_id": "aimd-diagnostics", "precision": 6}, project_id=PROJECT)

    view = build_aimd_analysis_view(spec, [target], method_evidence=_method)

    trajectory = view["trajectories"][0]
    assert len(trajectory["segments"]) == 2
    assert [sample["segment_index"] for sample in trajectory["samples"]] == [0, 0, 1]
    for key in ("sampling_length", "temperature_mean", "temperature_std",
                "energy_drift_total", "energy_drift_slope"):
        assert trajectory["metrics"][key]["value"] is None
    assert view["figure_data"] == {}
    assert any("does not cover every declared NSW step" in issue
               for issue in trajectory["issues"])


def test_report_binding_uses_registry_sections_and_frozen_view_hashes(tmp_path):
    target = _neb_target(tmp_path)
    spec = normalize_analysis_request(
        {"analysis_id": "neb-path"}, project_id=PROJECT)
    view = build_neb_analysis_view(spec, _neb_targets(target), method_evidence=_method)
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
    api._analysis_workbench_targets = lambda _context: _neb_targets(target)
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
