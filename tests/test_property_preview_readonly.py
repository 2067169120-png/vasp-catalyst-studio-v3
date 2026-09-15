from __future__ import annotations

import hashlib
from pathlib import Path

from vcstudio.gui_web.api import Api
from vcstudio.shared import manifest as manifest_mod


def _poscar(species, counts):
    return (
        "readonly fixture\n1.0\n10 0 0\n0 10 0\n0 0 15\n"
        f"{' '.join(species)}\n{' '.join(str(value) for value in counts)}\nDirect\n"
        + "0 0 0\n" * sum(counts)
    )


def _done_job(root: Path, source_id: str, energy: float, species, counts):
    root.mkdir()
    (root / "POSCAR").write_text(_poscar(species, counts), encoding="utf-8")
    (root / "INCAR").write_text(
        "GGA=PE\nENCUT=500\nISPIN=2\nIVDW=12\nLDAU=F\n", encoding="utf-8")
    (root / "KPOINTS").write_text(
        "Gamma\n0\nGamma\n3 3 1\n0 0 0\n", encoding="utf-8")
    (root / "POTCAR").write_text("".join(
        f"TITEL = PAW_PBE {element} readonly\n" for element in species),
        encoding="utf-8")
    (root / "OSZICAR").write_text(
        f"1 F= -.1E+02 E0= {energy:.8f} d E =0.0\n", encoding="utf-8")
    (root / "OUTCAR").write_text(
        "General timing and accounting informations for this job:\n", encoding="utf-8")
    manifest = manifest_mod.new_manifest(
        job_id=source_id, system=source_id, task_type="static", calc_type="slab",
        inputs={"potcar": [
            {"element": element, "titel": f"PAW_PBE {element} readonly"}
            for element in species
        ]},
    )
    manifest_mod.set_state(manifest, "DONE")
    manifest["results"] = {
        "energy_e0_eV": energy,
        "diagnosis": {
            "failure_class": "CONVERGED", "clean_exit": True, "exit_code": 0,
        },
    }
    manifest_mod.save_manifest(root, manifest)
    return manifest


def _owner(root: Path, source_id: str, task_type: str, operands):
    root.mkdir()
    manifest = manifest_mod.new_manifest(
        job_id=source_id, system=source_id, task_type=task_type, calc_type="slab",
        inputs={"analysis_operands": operands},
    )
    manifest_mod.set_state(manifest, "DONE")
    manifest_mod.save_manifest(root, manifest)
    return manifest


def _target(api, root, source_id, task_type, manifest):
    return {
        "path": str(root),
        "path_key": api._analysis_workbench_path_key(root),
        "source_id": source_id,
        "relation": "descendant",
        "task_type": task_type,
        "state": "DONE",
        "manifest": manifest,
    }


def _snapshot(root: Path):
    result = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        stat = path.stat()
        result[path.relative_to(root).as_posix()] = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    return result


def test_property_workbench_calculators_are_strictly_read_only(tmp_path):
    api = Api()
    slab = tmp_path / "slab"
    bulk = tmp_path / "bulk"
    sac = tmp_path / "sac"
    substrate = tmp_path / "substrate"
    atom = tmp_path / "atom"
    chemical = tmp_path / "chemical"
    manifests = {
        "slab": _done_job(slab, "slab", -60.0, ["Si"], [6]),
        "bulk": _done_job(bulk, "bulk", -20.0, ["Si"], [2]),
        "sac": _done_job(sac, "sac", -300.0, ["Fe", "N", "C"], [1, 4, 22]),
        "substrate": _done_job(substrate, "substrate", -295.0, ["N", "C"], [4, 22]),
        # Reference labels are bound by the owner manifest; shared N/C POTCAR
        # identities keep the outer all-operand method audit comparable.
        "atom": _done_job(atom, "atom", -3.0, ["N", "C"], [1, 1]),
        "chemical": _done_job(chemical, "chemical", -4.0, ["N", "C"], [1, 1]),
    }
    surface_owner = tmp_path / "surface-owner"
    formation_owner = tmp_path / "formation-owner"
    manifests["surface-owner"] = _owner(
        surface_owner, "surface-owner", "surface_energy",
        {"slab_job": "slab", "bulk_job": "bulk"})
    manifests["formation-owner"] = _owner(
        formation_owner, "formation-owner", "formation_binding", {
            "sac_job": "sac", "substrate_job": "substrate",
            "atom_energy_jobs": {"Fe": "atom"},
            "chemical_potential_jobs": {"Fe": "chemical"},
        })
    roots = {
        "slab": slab, "bulk": bulk, "sac": sac, "substrate": substrate,
        "atom": atom, "chemical": chemical,
        "surface-owner": surface_owner, "formation-owner": formation_owner,
    }
    tasks = {
        **{key: "static" for key in ("slab", "bulk", "sac", "substrate", "atom", "chemical")},
        "surface-owner": "surface_energy",
        "formation-owner": "formation_binding",
    }
    targets = [
        _target(api, roots[key], key, tasks[key], manifests[key])
        for key in roots
    ]
    before = _snapshot(tmp_path)

    surface = api._analysis_workbench_property_results("surface_energy", targets)
    formation = api._analysis_workbench_property_results("formation_binding", targets)

    after = _snapshot(tmp_path)
    assert before == after
    assert surface[0]["ok"] is True and surface[0]["gamma_jm2"] is not None
    assert formation[0]["ok"] is True, formation
    assert formation[0]["binding_energy_eV"] == -2.0
    assert formation[0]["formation_energy_eV"] == -1.0
    assert not list(tmp_path.rglob("vcstudio-*-report.html"))


def test_property_preview_source_uses_only_readonly_calculator_seams():
    names = Api._analysis_workbench_property_results.__code__.co_names
    assert "_surface_energy_calc" in names
    assert "_formation_binding_calc" in names
    assert "surface_energy_calc" not in names
    assert "formation_binding_calc" not in names
