"""Data-only CatMAP adapter export, preview and explicit-confirm tests."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from tests.test_kinetics import frozen_network
from vcstudio.external import catmap_adapter
from vcstudio.project import kinetics


def test_missing_catmap_is_unavailable_but_audit_and_inputs_are_exportable():
    preview = catmap_adapter.preview_export(frozen_network(), tool_path=None)

    assert preview["schema"] == catmap_adapter.PREVIEW_SCHEMA
    assert preview["audit"]["machine_pass"] is True
    assert preview["tool"]["available"] is False
    assert preview["capability_status"] == "unavailable"
    assert preview["explicit_confirmation_required"] is True
    assert {"kinetics-audit.json", "kinetics-input.json", "energetics.tsv", "model.mkm"} <= {
        item["name"] for item in preview["artifacts"]
    }
    assert "contents" not in preview


def test_catmap_table_and_mkm_follow_fixed_data_only_contract():
    bundle = catmap_adapter.build_export_bundle(frozen_network(), tool_path=None)
    table = bundle["files"]["energetics.tsv"]
    model = bundle["files"]["model.mkm"]
    process = json.loads(bundle["files"]["process-contract.json"])

    assert table.splitlines()[0].split("\t") == [
        "surface_name", "site_name", "species_name", "formation_energy",
        "frequencies", "reference",
    ]
    assert "rxn_expressions" in model
    assert "CO_s + O_s <-> COO_ts_s + *_s -> CO2_g + *_s + *_s" in model
    assert "input_file = 'energetics.tsv'" in model
    assert "output_variables" in model
    assert all(token not in model for token in (
        "import ", "subprocess", "os.system", "eval(", "exec(", "__import__",
    ))
    assert process["execution_permitted"] is False
    assert process["shell"] is False
    assert process["accepts_user_arguments"] is False
    assert process["auto_install"] is False
    assert process["argv_template"] == [
        "<configured-catmap-adapter>", "--setup", "model.mkm",
        "--result", "kinetics-result.json",
    ]


def test_tool_identity_is_hash_frozen_without_executing_or_leaking_path(tmp_path):
    exe = tmp_path / "catmap.exe"
    exe.write_bytes(b"user-installed-catmap-adapter")

    preview = catmap_adapter.preview_export(
        frozen_network(), tool_path=str(exe), tool_version="0.4.0")

    assert preview["tool"] == {
        "available": True,
        "name": "catmap.exe",
        "version": "0.4.0",
        "sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
        "size": exe.stat().st_size,
    }
    assert str(tmp_path) not in json.dumps(preview)


@pytest.mark.parametrize("path", ["catmap.exe", "../catmap.exe", "C:\\bad\ncmd.exe"])
def test_unsafe_or_nonabsolute_tool_paths_are_unavailable_not_executed(path):
    identity = catmap_adapter.inspect_tool_path(path)

    assert identity["available"] is False
    assert identity["sha256"] is None


def test_confirm_writes_frozen_bundle_only_after_matching_preview(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    network = frozen_network()
    preview = catmap_adapter.preview_export(network, tool_path=None)

    with pytest.raises(catmap_adapter.CatmapAdapterError, match="explicit confirmation"):
        catmap_adapter.confirm_export(
            network, project, preview["preview_token"], confirmed=False)

    confirmed = catmap_adapter.confirm_export(
        network, project, preview["preview_token"], confirmed=True)
    export_dir = Path(confirmed["export_dir"])
    manifest = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))

    assert export_dir.parent == project / ".vcstudio" / "kinetics" / "exports"
    assert confirmed["input_sha256"] == network["input_sha256"]
    assert manifest["input_sha256"] == network["input_sha256"]
    assert manifest["adapter"]["id"] == catmap_adapter.ADAPTER_ID
    for item in manifest["artifacts"]:
        data = (export_dir / item["name"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == item["sha256"]


def test_preview_token_binds_network_adapter_tool_and_artifact_hashes(tmp_path):
    network = frozen_network()
    preview = catmap_adapter.preview_export(network, tool_path=None)
    changed = copy.deepcopy(network)
    changed["standard_state"]["temperature"]["value"] = 550.0
    changed["input_sha256"] = kinetics.compute_input_sha256(changed)

    with pytest.raises(catmap_adapter.CatmapAdapterError, match="preview token"):
        catmap_adapter.confirm_export(
            changed, tmp_path, preview["preview_token"], confirmed=True)


def test_failed_audit_still_exports_audit_report_but_not_catmap_inputs():
    network = frozen_network()
    network["elementary_steps"][0]["reverse_barrier"]["value"] = 1.0
    network["input_sha256"] = kinetics.compute_input_sha256(network)

    bundle = catmap_adapter.build_export_bundle(network, tool_path=None)

    assert bundle["audit"]["export_ready"] is False
    assert set(bundle["files"]) == {"kinetics-audit.json"}
    assert bundle["preview_token"]


def test_export_identifiers_cannot_inject_python_or_tsv_rows():
    network = frozen_network()
    network["species"][0]["id"] = "CO_g\n__import__('os').system('whoami')"
    network["input_sha256"] = kinetics.compute_input_sha256(network)

    bundle = catmap_adapter.build_export_bundle(network, tool_path=None)

    assert bundle["audit"]["export_ready"] is False
    assert set(bundle["files"]) == {"kinetics-audit.json"}
