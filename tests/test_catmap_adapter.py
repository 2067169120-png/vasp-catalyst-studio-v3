"""Data-only CatMAP adapter export, preview and explicit-confirm tests."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from tests.test_kinetics import _freeze, catmap_ready_network, frozen_network
from vcstudio.external import catmap_adapter
from vcstudio.project import kinetics


_MODEL_BUNDLE_FILES = {
    "catmap-descriptors.json",
    "catmap-gas-contract.json",
    "catmap-name-map.json",
    "energetics.tsv",
    "kinetics-audit.json",
    "kinetics-input.json",
    "manifest.json",
    "model-scan.mkm",
    "model.mkm",
    "process-contract.json",
}


def _tool(tmp_path):
    path = tmp_path / "catmap.exe"
    path.write_bytes(b"user-installed-catmap-process-adapter")
    return path


def _preview(network, tool):
    return catmap_adapter.preview_export(
        network, tool_path=tool, tool_version="0.4.0")


def _confirm(network, project, preview, tool):
    return catmap_adapter.confirm_export(
        network, project, preview["preview_token"],
        expected_selection_revision=0,
        expected_selected_export_sha256=None, confirmed=True,
        tool_path=tool, tool_version=preview["tool"]["version"])


def test_bounded_scandir_stops_exactly_at_limit_plus_one(
        tmp_path, monkeypatch):
    class EndlessScan:
        def __init__(self):
            self.count = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            return self

        def __next__(self):
            self.count += 1
            return object()

    scan = EndlessScan()
    monkeypatch.setattr(catmap_adapter.os, "scandir", lambda _path: scan)

    with pytest.raises(catmap_adapter.CatmapAdapterError, match="entry limit"):
        catmap_adapter._bounded_scandir(
            tmp_path, maximum=3, label="bounded test directory")

    assert scan.count == 4


def test_missing_catmap_is_unavailable_but_audit_report_is_exportable():
    preview = catmap_adapter.preview_export(catmap_ready_network(), tool_path=None)
    audit_preview = catmap_adapter.preview_audit_export(
        catmap_ready_network(), tool_path=None)

    assert preview["schema"] == catmap_adapter.PREVIEW_SCHEMA
    assert preview["audit"]["machine_pass"] is True
    assert preview["tool"]["available"] is False
    assert preview["capability_status"] == "unavailable"
    assert preview["export_ready"] is False
    assert preview["preview_token"] is None
    assert preview["explicit_confirmation_required"] is True
    assert {"kinetics-audit.json", "kinetics-input.json", "energetics.tsv", "model.mkm"} <= {
        item["name"] for item in preview["artifacts"]
    }
    assert "contents" not in preview
    assert audit_preview["export_kind"] == "audit_report"
    assert audit_preview["model_published"] is False
    assert audit_preview["preview_token"]


def test_catmap_table_and_mkm_follow_fixed_data_only_contract():
    bundle = catmap_adapter.build_export_bundle(
        catmap_ready_network(), tool_path=None)
    table = bundle["files"]["energetics.tsv"]
    model = bundle["files"]["model.mkm"]
    process = json.loads(bundle["files"]["process-contract.json"])

    assert table.splitlines()[0].split("\t") == [
        "surface_name", "site_name", "species_name", "formation_energy",
        "frequencies", "reference",
    ]
    assert "rxn_expressions" in model
    assert "g0_g + *_s0 <-> ts-0_s0 -> a0_s0" in model
    assert "g1_g + *_s0 <-> ts-1_s0 -> a1_s0" in model
    assert "gas_names = ['g0_g', 'g1_g']" in model
    assert "input_file = 'energetics.tsv'" in model
    assert "output_variables" in model
    assert "scaler = 'ThermodynamicScaler'" in model
    assert "descriptor_names = ['temperature', 'pressure']" in model
    assert "descriptors = [500.0, 1.0]" in model
    assert "concentration" in model
    assert "model-scan.mkm" in bundle["files"]
    assert "descriptor_ranges" in bundle["files"]["model-scan.mkm"]
    assert "resolution = [5, 5]" in bundle["files"]["model-scan.mkm"]
    assert "prefactor_list = ['10000000000000', '10000000000000']" in model
    gas_contract = json.loads(bundle["files"]["catmap-gas-contract.json"])
    assert gas_contract["reference_set_complete"] is True
    assert gas_contract["composition_rank"] == len(gas_contract["elements"]) == 2
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


def test_v3_input_schema_and_complete_frozen_bundle_contract(tmp_path):
    bundle = catmap_adapter.build_export_bundle(
        catmap_ready_network(), tool_path=_tool(tmp_path), tool_version="0.4.0")
    process = json.loads(bundle["files"]["process-contract.json"])

    assert kinetics.NETWORK_SCHEMA == "vcstudio.kinetics-network/v3"
    assert kinetics.RESULT_SCHEMA == "vcstudio.kinetics-result/v2"
    assert kinetics.NORMALIZED_RESULT_SCHEMA == (
        "vcstudio.kinetics-normalized-result/v2")
    assert json.loads(bundle["files"]["kinetics-input.json"])["schema"] == (
        kinetics.NETWORK_SCHEMA)
    assert process["expected_result"]["schema"] == kinetics.RESULT_SCHEMA
    assert set(bundle["files"]) == _MODEL_BUNDLE_FILES
    assert {item["name"] for item in bundle["artifacts"]} == (
        _MODEL_BUNDLE_FILES - {"manifest.json"})


def test_tool_identity_is_hash_frozen_without_executing_or_leaking_path(tmp_path):
    exe = tmp_path / "catmap.exe"
    exe.write_bytes(b"user-installed-catmap-adapter")

    preview = catmap_adapter.preview_export(
        catmap_ready_network(), tool_path=str(exe), tool_version="0.4.0")

    assert preview["tool"] == {
        "available": True,
        "name": "catmap.exe",
        "version": "0.4.0",
        "sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
        "size": exe.stat().st_size,
    }
    assert str(tmp_path) not in json.dumps(preview)


def test_unknown_tool_version_is_audit_only_and_cannot_be_confirmed(tmp_path):
    network = catmap_ready_network()
    tool = _tool(tmp_path)

    preview = catmap_adapter.preview_export(network, tool_path=tool)
    bundle = catmap_adapter.build_export_bundle(network, tool_path=tool)

    assert preview["tool"]["available"] is True
    assert preview["tool"]["version"] is None
    assert preview["export_ready"] is False
    assert preview["capability_status"] == "unavailable"
    assert preview["preview_token"] is None
    with pytest.raises(catmap_adapter.CatmapAdapterError, match="not ready"):
        catmap_adapter.confirm_export(
            network, tmp_path, bundle["preview_token"],
            expected_selection_revision=0,
            expected_selected_export_sha256=None, confirmed=True,
            tool_path=tool)
    assert not (tmp_path / ".vcstudio").exists()


@pytest.mark.parametrize("path", ["catmap.exe", "../catmap.exe", "C:\\bad\ncmd.exe"])
def test_unsafe_or_nonabsolute_tool_paths_are_unavailable_not_executed(path):
    identity = catmap_adapter.inspect_tool_path(path)

    assert identity["available"] is False
    assert identity["sha256"] is None


def test_confirm_writes_frozen_bundle_only_after_matching_preview(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    network = catmap_ready_network()
    tool = _tool(tmp_path)
    preview = catmap_adapter.preview_export(
        network, tool_path=tool, tool_version="0.4.0")

    with pytest.raises(catmap_adapter.CatmapAdapterError, match="explicit confirmation"):
        catmap_adapter.confirm_export(
            network, project, preview["preview_token"],
            expected_selection_revision=0,
            expected_selected_export_sha256=None, confirmed=False,
            tool_path=tool, tool_version="0.4.0")

    confirmed = _confirm(network, project, preview, tool)
    export_dir = Path(confirmed["export_dir"])
    manifest = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))

    assert export_dir.parent.parent == project / ".vcstudio" / "kinetics" / "exports"
    assert confirmed["input_sha256"] == network["input_sha256"]
    assert manifest["input_sha256"] == network["input_sha256"]
    assert manifest["adapter"]["id"] == catmap_adapter.ADAPTER_ID
    for item in manifest["artifacts"]:
        data = (export_dir / item["name"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == item["sha256"]
    verified = catmap_adapter.load_confirmed_manifest(
        project, network["input_sha256"])
    assert verified["adapter"]["id"] == catmap_adapter.ADAPTER_ID
    assert verified["tool"]["sha256"] == hashlib.sha256(tool.read_bytes()).hexdigest()


def test_preview_token_binds_network_adapter_tool_and_artifact_hashes(tmp_path):
    network = catmap_ready_network()
    tool = _tool(tmp_path)
    preview = _preview(network, tool)
    changed = copy.deepcopy(network)
    changed["standard_state"]["temperature"]["value"] = 550.0
    _freeze(changed)

    with pytest.raises(catmap_adapter.CatmapAdapterError, match="preview token"):
        catmap_adapter.confirm_export(
            changed, tmp_path, preview["preview_token"],
            expected_selection_revision=0,
            expected_selected_export_sha256=None, confirmed=True,
            tool_path=tool, tool_version="0.4.0")


def test_failed_audit_still_exports_audit_report_but_not_catmap_inputs():
    network = catmap_ready_network()
    network["elementary_steps"][0]["reverse_barrier"]["value"] = 1.0
    _freeze(network)

    bundle = catmap_adapter.build_export_bundle(network, tool_path=None)

    assert bundle["audit"]["export_ready"] is False
    assert set(bundle["files"]) == {"kinetics-audit.json"}
    assert bundle["preview_token"]


def test_audit_only_cannot_be_confirmed_as_model_and_has_separate_manifest(tmp_path):
    network = catmap_ready_network()
    network["elementary_steps"][0]["reverse_barrier"]["value"] = 1.0
    _freeze(network)
    tool = _tool(tmp_path)
    bundle = catmap_adapter.build_export_bundle(network, tool_path=tool)
    with pytest.raises(catmap_adapter.CatmapAdapterError, match="not ready"):
        catmap_adapter.confirm_export(
            network, tmp_path, bundle["preview_token"],
            expected_selection_revision=0,
            expected_selected_export_sha256=None, confirmed=True,
            tool_path=tool)
    assert not (tmp_path / ".vcstudio").exists()

    preview = catmap_adapter.preview_audit_export(network, tool_path=tool)
    confirmed = catmap_adapter.confirm_audit_export(
        network, tmp_path, preview["preview_token"], confirmed=True,
        tool_path=tool)
    export_dir = Path(confirmed["export_dir"])
    assert confirmed["schema"] == catmap_adapter.AUDIT_MANIFEST_SCHEMA
    assert confirmed["model_published"] is False
    assert set(confirmed["files"]) == {
        "audit-manifest.json", "kinetics-audit.json",
    }
    assert not (export_dir / "model.mkm").exists()


def test_tool_drift_publishes_immutable_versions_with_selection_cas(tmp_path):
    network = catmap_ready_network()
    tool = _tool(tmp_path)
    first_preview = _preview(network, tool)
    first = _confirm(network, tmp_path, first_preview, tool)

    tool.write_bytes(b"different-user-installed-catmap-adapter")
    second_preview = _preview(network, tool)
    assert second_preview["preview_token"] != first_preview["preview_token"]
    second = catmap_adapter.confirm_export(
        network, tmp_path, second_preview["preview_token"],
        expected_selection_revision=first["selection_revision"],
        expected_selected_export_sha256=first["selected_export_sha256"],
        confirmed=True, tool_path=tool, tool_version="0.4.0")

    root = (tmp_path / ".vcstudio" / "kinetics" / "exports" /
            network["input_sha256"])
    assert (root / first_preview["preview_token"] / "manifest.json").is_file()
    assert (root / second_preview["preview_token"] / "manifest.json").is_file()
    assert second["selection_revision"] == 2
    loaded = catmap_adapter.load_confirmed_manifest(
        tmp_path, network["input_sha256"])
    assert loaded["selected_export_sha256"] == second_preview["preview_token"]

    with pytest.raises(catmap_adapter.CatmapAdapterError, match="conflict"):
        catmap_adapter.confirm_export(
            network, tmp_path, second_preview["preview_token"],
            expected_selection_revision=0,
            expected_selected_export_sha256=None, confirmed=True,
            tool_path=tool, tool_version="0.4.0")


def test_stale_selection_loser_does_not_publish_or_grow_bundle_storage(tmp_path):
    network = catmap_ready_network()
    tool = _tool(tmp_path)
    first_preview = _preview(network, tool)
    _confirm(network, tmp_path, first_preview, tool)
    input_dir = (tmp_path / ".vcstudio" / "kinetics" / "exports" /
                 network["input_sha256"])

    tool.write_bytes(b"new-catmap-identity-for-stale-selection")
    stale_preview = _preview(network, tool)
    before = sorted(
        (str(path.relative_to(input_dir)), path.is_dir(), path.stat().st_size)
        for path in input_dir.rglob("*")
    )

    with pytest.raises(catmap_adapter.CatmapAdapterError, match="conflict"):
        catmap_adapter.confirm_export(
            network, tmp_path, stale_preview["preview_token"],
            expected_selection_revision=0,
            expected_selected_export_sha256=None, confirmed=True,
            tool_path=tool, tool_version="0.4.0")

    after = sorted(
        (str(path.relative_to(input_dir)), path.is_dir(), path.stat().st_size)
        for path in input_dir.rglob("*")
    )
    assert after == before
    assert not (input_dir / stale_preview["preview_token"]).exists()
    assert not (input_dir / catmap_adapter._RESERVATION_FILENAME).exists()


def test_publish_failure_rolls_back_reservation_and_staged_bundle(
        tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    network = catmap_ready_network()
    tool = _tool(tmp_path)
    preview = _preview(network, tool)
    input_dir = (project / ".vcstudio" / "kinetics" / "exports" /
                 network["input_sha256"])
    final_dir = input_dir / preview["preview_token"]
    original_replace = catmap_adapter.os.replace

    def fail_bundle_publish(source, destination):
        if Path(destination) == final_dir:
            raise OSError("simulated publish failure")
        return original_replace(source, destination)

    monkeypatch.setattr(catmap_adapter.os, "replace", fail_bundle_publish)

    with pytest.raises(catmap_adapter.CatmapAdapterError, match="could not be published"):
        _confirm(network, project, preview, tool)

    assert not final_dir.exists()
    assert not (input_dir / catmap_adapter._RESERVATION_FILENAME).exists()
    assert not any(
        path.name.startswith(".vcs-kinetics-stage-")
        for path in input_dir.iterdir())
    assert catmap_adapter.export_selection_snapshot(
        project, network["input_sha256"])["revision"] == 0


@pytest.mark.parametrize("quota", ["count", "bytes"])
def test_per_input_bundle_quota_rejects_new_version_without_residue(
        tmp_path, monkeypatch, quota):
    network = catmap_ready_network()
    tool = _tool(tmp_path)
    first_preview = _preview(network, tool)
    first = _confirm(network, tmp_path, first_preview, tool)
    input_dir = (tmp_path / ".vcstudio" / "kinetics" / "exports" /
                 network["input_sha256"])

    tool.write_bytes(b"new-catmap-identity-for-quota")
    second_preview = _preview(network, tool)
    second_bundle = catmap_adapter.build_export_bundle(
        network, tool_path=tool, tool_version="0.4.0")
    if quota == "count":
        monkeypatch.setattr(catmap_adapter, "_MAX_BUNDLES_PER_INPUT", 1)
    else:
        retained = sum(
            path.stat().st_size
            for path in (input_dir / first_preview["preview_token"]).iterdir())
        candidate = sum(
            len(content.encode("utf-8"))
            for content in second_bundle["files"].values())
        monkeypatch.setattr(
            catmap_adapter, "_MAX_INPUT_BUNDLE_BYTES",
            retained + candidate - 1)

    with pytest.raises(catmap_adapter.CatmapAdapterError, match="quota"):
        catmap_adapter.confirm_export(
            network, tmp_path, second_preview["preview_token"],
            expected_selection_revision=first["selection_revision"],
            expected_selected_export_sha256=first["selected_export_sha256"],
            confirmed=True, tool_path=tool, tool_version="0.4.0")

    assert not (input_dir / second_preview["preview_token"]).exists()
    assert not (input_dir / catmap_adapter._RESERVATION_FILENAME).exists()


def test_next_confirmation_recovers_unselected_reserved_bundle_and_stage(tmp_path):
    network = catmap_ready_network()
    tool = _tool(tmp_path)
    first_preview = _preview(network, tool)
    first = _confirm(network, tmp_path, first_preview, tool)
    input_dir = (tmp_path / ".vcstudio" / "kinetics" / "exports" /
                 network["input_sha256"])

    tool.write_bytes(b"new-catmap-identity-after-crash")
    next_preview = _preview(network, tool)
    next_bundle = catmap_adapter.build_export_bundle(
        network, tool_path=tool, tool_version="0.4.0")
    orphan_bundle = input_dir / next_preview["preview_token"]
    orphan_bundle.mkdir()
    for name, content in next_bundle["files"].items():
        (orphan_bundle / name).write_bytes(content.encode("utf-8"))
    transaction_id = "a" * 32
    stage_name = f".vcs-kinetics-stage-{transaction_id}"
    orphan_stage = input_dir / stage_name
    orphan_stage.mkdir()
    (orphan_stage / "partial.txt").write_text("partial", encoding="utf-8")
    reservation = {
        "schema": catmap_adapter.EXPORT_RESERVATION_SCHEMA,
        "input_sha256": network["input_sha256"],
        "preview_token": next_preview["preview_token"],
        "expected_selection_revision": first["selection_revision"],
        "expected_selected_export_sha256": first["selected_export_sha256"],
        "transaction_id": transaction_id,
        "created_by_transaction": True,
        "stage_name": stage_name,
        "bundle_integrity_sha256": catmap_adapter._bundle_integrity_sha256(
            next_bundle["files"]),
    }
    (input_dir / catmap_adapter._RESERVATION_FILENAME).write_text(
        json.dumps(reservation), encoding="utf-8")

    confirmed = catmap_adapter.confirm_export(
        network, tmp_path, next_preview["preview_token"],
        expected_selection_revision=first["selection_revision"],
        expected_selected_export_sha256=first["selected_export_sha256"],
        confirmed=True, tool_path=tool, tool_version="0.4.0")

    assert confirmed["selection_revision"] == 2
    assert not orphan_stage.exists()
    assert not (input_dir / catmap_adapter._RESERVATION_FILENAME).exists()
    assert set(path.name for path in orphan_bundle.iterdir()) == _MODEL_BUNDLE_FILES


def test_crashed_reselection_reservation_never_deletes_preexisting_bundle(
        tmp_path, monkeypatch):
    network = catmap_ready_network()
    tool = _tool(tmp_path)
    first_tool_bytes = tool.read_bytes()
    preview_a = _preview(network, tool)
    selected_a = _confirm(network, tmp_path, preview_a, tool)
    input_dir = (tmp_path / ".vcstudio" / "kinetics" / "exports" /
                 network["input_sha256"])

    second_tool_bytes = b"catmap-identity-b-for-reselection"
    tool.write_bytes(second_tool_bytes)
    preview_b = _preview(network, tool)
    selected_b = catmap_adapter.confirm_export(
        network, tmp_path, preview_b["preview_token"],
        expected_selection_revision=selected_a["selection_revision"],
        expected_selected_export_sha256=selected_a["selected_export_sha256"],
        confirmed=True, tool_path=tool, tool_version="0.4.0")

    tool.write_bytes(first_tool_bytes)
    assert _preview(network, tool)["preview_token"] == preview_a["preview_token"]
    original_atomic_write = catmap_adapter._atomic_write

    def crash_after_reservation(path, content, **kwargs):
        if Path(path).name == "current.json":
            raise KeyboardInterrupt("simulated process crash")
        return original_atomic_write(path, content, **kwargs)

    monkeypatch.setattr(catmap_adapter, "_atomic_write", crash_after_reservation)
    with pytest.raises(KeyboardInterrupt, match="process crash"):
        catmap_adapter.confirm_export(
            network, tmp_path, preview_a["preview_token"],
            expected_selection_revision=selected_b["selection_revision"],
            expected_selected_export_sha256=selected_b["selected_export_sha256"],
            confirmed=True, tool_path=tool, tool_version="0.4.0")
    reservation = json.loads(
        (input_dir / catmap_adapter._RESERVATION_FILENAME).read_text(
            encoding="utf-8"))
    assert reservation["created_by_transaction"] is False
    assert reservation["stage_name"] is None

    monkeypatch.setattr(catmap_adapter, "_atomic_write", original_atomic_write)
    tool.write_bytes(second_tool_bytes)
    with pytest.raises(catmap_adapter.CatmapAdapterError, match="conflict"):
        catmap_adapter.confirm_export(
            network, tmp_path, preview_b["preview_token"],
            expected_selection_revision=selected_b["selection_revision"],
            expected_selected_export_sha256=selected_b["selected_export_sha256"],
            confirmed=True, tool_path=tool, tool_version="0.4.0")
    recovered = catmap_adapter.export_selection_snapshot(
        tmp_path, network["input_sha256"])

    assert recovered["revision"] == 3
    assert recovered["selected_export_sha256"] == preview_a["preview_token"]
    assert (input_dir / preview_a["preview_token"]).is_dir()
    assert (input_dir / preview_b["preview_token"]).is_dir()
    assert not (input_dir / catmap_adapter._RESERVATION_FILENAME).exists()


@pytest.mark.parametrize(
    "stage", ["after_selection_prepare", "after_selection_replace"])
def test_fresh_confirmation_forward_recovers_prepared_selection_crash(
        tmp_path, monkeypatch, stage):
    network = catmap_ready_network()
    tool = _tool(tmp_path)
    preview = _preview(network, tool)

    def crash(selected):
        if selected == stage:
            raise KeyboardInterrupt(selected)

    monkeypatch.setattr(catmap_adapter, "_selection_fault", crash)
    with pytest.raises(KeyboardInterrupt, match=stage):
        _confirm(network, tmp_path, preview, tool)

    monkeypatch.setattr(catmap_adapter, "_selection_fault", lambda _stage: None)
    with pytest.raises(catmap_adapter.CatmapAdapterError, match="conflict"):
        _confirm(network, tmp_path, preview, tool)
    selection = catmap_adapter.export_selection_snapshot(
        tmp_path, network["input_sha256"])
    input_dir = (tmp_path / ".vcstudio" / "kinetics" / "exports" /
                 network["input_sha256"])
    assert selection == {
        "schema": catmap_adapter.EXPORT_SELECTION_SCHEMA,
        "input_sha256": network["input_sha256"],
        "revision": 1,
        "selected_export_sha256": preview["preview_token"],
    }
    assert (input_dir / preview["preview_token"]).is_dir()
    assert not (input_dir / catmap_adapter._RESERVATION_FILENAME).exists()


def test_selection_anchor_rejects_old_pointer_missing_anchor_and_tamper(tmp_path):
    network = catmap_ready_network()
    tool = _tool(tmp_path)
    first_preview = _preview(network, tool)
    first = _confirm(network, tmp_path, first_preview, tool)
    input_dir = (tmp_path / ".vcstudio" / "kinetics" / "exports" /
                 network["input_sha256"])
    pointer = input_dir / "current.json"
    anchor = input_dir / catmap_adapter._SELECTION_ANCHOR_FILENAME
    old_pointer = pointer.read_bytes()

    tool.write_bytes(b"second-tool-for-selection-anchor")
    second_preview = _preview(network, tool)
    catmap_adapter.confirm_export(
        network, tmp_path, second_preview["preview_token"],
        expected_selection_revision=first["selection_revision"],
        expected_selected_export_sha256=first["selected_export_sha256"],
        confirmed=True, tool_path=tool, tool_version="0.4.0")
    pointer.write_bytes(old_pointer)
    with pytest.raises(catmap_adapter.CatmapAdapterError, match="independent anchor"):
        catmap_adapter.export_selection_snapshot(
            tmp_path, network["input_sha256"])

    pointer.write_text(json.dumps({
        "schema": catmap_adapter.EXPORT_SELECTION_SCHEMA,
        "input_sha256": network["input_sha256"],
        "revision": 2,
        "selected_export_sha256": second_preview["preview_token"],
    }), encoding="utf-8")
    anchor_bytes = anchor.read_bytes()
    anchor.unlink()
    with pytest.raises(catmap_adapter.CatmapAdapterError, match="without its independent anchor"):
        catmap_adapter.export_selection_snapshot(
            tmp_path, network["input_sha256"])

    anchor.write_bytes(anchor_bytes.replace(
        b'"kind":"commit"', b'"kind":"tamper"', 1))
    with pytest.raises(catmap_adapter.CatmapAdapterError, match="anchor"):
        catmap_adapter.export_selection_snapshot(
            tmp_path, network["input_sha256"])


def test_selection_anchor_entity_replacement_during_commit_fails_closed(
        tmp_path, monkeypatch):
    network = catmap_ready_network()
    tool = _tool(tmp_path)
    preview = _preview(network, tool)
    input_dir = (tmp_path / ".vcstudio" / "kinetics" / "exports" /
                 network["input_sha256"])

    def replace_anchor(stage):
        if stage != "after_selection_prepare":
            return
        anchor = input_dir / catmap_adapter._SELECTION_ANCHOR_FILENAME
        replacement = input_dir / ".replacement-anchor"
        replacement.write_bytes(anchor.read_bytes())
        replacement.replace(anchor)

    monkeypatch.setattr(catmap_adapter, "_selection_fault", replace_anchor)
    with pytest.raises(catmap_adapter.CatmapAdapterError, match="entity changed"):
        _confirm(network, tmp_path, preview, tool)
    assert (input_dir / preview["preview_token"]).is_dir()
    assert (input_dir / catmap_adapter._RESERVATION_FILENAME).is_file()


def test_name_map_rejects_ambiguous_multisite_species():
    network = catmap_ready_network()
    network["species"][4]["sites"] = {"s": 1, "bridge": 1}
    with pytest.raises(catmap_adapter.CatmapAdapterError, match="exactly one"):
        catmap_adapter._catmap_name_map(network)


def test_missing_positive_gas_participants_is_audit_only_not_export_ready(tmp_path):
    network = frozen_network()
    tool = _tool(tmp_path)
    bundle = catmap_adapter.build_export_bundle(network, tool_path=tool)

    assert bundle["audit"]["machine_pass"] is True
    assert bundle["contract_ready"] is False
    assert bundle["export_ready"] is False
    assert "positive-pressure gas reservoirs" in bundle["adapter_issues"][0][
        "message"]
    assert set(bundle["files"]) == {
        "kinetics-audit.json", "catmap-adapter-audit.json",
    }
    preview = catmap_adapter.preview_export(network, tool_path=tool)
    assert preview["preview_token"] is None
    with pytest.raises(catmap_adapter.CatmapAdapterError, match="not ready"):
        catmap_adapter.confirm_export(
            network, tmp_path, bundle["preview_token"],
            expected_selection_revision=0,
            expected_selected_export_sha256=None, confirmed=True,
            tool_path=tool)
    assert not (tmp_path / ".vcstudio").exists()


def test_rank_deficient_participating_gases_cannot_claim_catmap_reference_set():
    network = catmap_ready_network()
    compositions = {
        "CO_g": {"C": 1, "O": 2}, "CO_s": {"C": 1, "O": 2},
        "CO_ads_ts": {"C": 1, "O": 2},
        "O2_g": {"C": 2, "O": 4}, "O2_s": {"C": 2, "O": 4},
        "O2_ads_ts": {"C": 2, "O": 4},
    }
    for record in network["species"]:
        if record["id"] in compositions:
            record["composition"] = compositions[record["id"]]
    _freeze(network)
    assert kinetics.audit_network(network)["machine_pass"] is True

    bundle = catmap_adapter.build_export_bundle(network, tool_path=None)
    assert bundle["contract_ready"] is False
    assert bundle["export_ready"] is False
    assert "independent atomic reference set" in bundle["adapter_issues"][0][
        "message"]
    assert "model.mkm" not in bundle["files"]


@pytest.mark.parametrize("mutate,reason", [
    (
        lambda network: (
            network["methodology"].update({"potential_model": "che"}),
            network["standard_state"].update({
                "potential": {"value": 0.0, "unit": "V", "reference": "RHE"},
            }),
            network["operating_range"].update({"potential_V": [-1.0, 1.0]}),
        ),
        "electrochemical potential",
    ),
    (
        lambda network: network["elementary_steps"][0]["prefactors"][
            "reverse"].update({"value": 2.0e13}),
        "equal forward/reverse",
    ),
    (
        lambda network: network["methodology"].update({
            "energy_basis": "electronic_plus_corrections",
        }),
        "frozen Gibbs",
    ),
    (
        lambda network: [
            (
                record.update({"phase": "liquid"}),
                record["activity"].update({"unit": "mol/L"}),
            )
            for record in network["species"] if record["phase"] == "gas"
        ],
        "liquid/solution",
    ),
])
def test_valid_but_unencoded_catmap_features_fail_closed_to_audit_only(
        mutate, reason):
    network = catmap_ready_network()
    mutate(network)
    _freeze(network)
    assert kinetics.audit_network(network)["machine_pass"] is True

    bundle = catmap_adapter.build_export_bundle(network, tool_path=None)

    assert bundle["export_ready"] is False
    assert bundle["adapter_issues"][0]["code"] == (
        "CATMAP_PHASE1_CONTRACT_UNSUPPORTED")
    assert reason in bundle["adapter_issues"][0]["message"]
    assert set(bundle["files"]) == {
        "kinetics-audit.json", "catmap-adapter-audit.json",
    }


def test_nonzero_empty_site_energy_is_not_silently_rebased(tmp_path):
    network = catmap_ready_network()
    for record in network["species"]:
        if record["phase"] != "gas":
            record["formation_energy"]["value"] += 1.0
    _freeze(network)
    assert kinetics.audit_network(network)["machine_pass"] is True
    tool = _tool(tmp_path)

    bundle = catmap_adapter.build_export_bundle(network, tool_path=tool)

    assert bundle["contract_ready"] is False
    assert bundle["export_ready"] is False
    assert "zero formation_energy" in bundle["adapter_issues"][0]["message"]
    assert set(bundle["files"]) == {
        "kinetics-audit.json", "catmap-adapter-audit.json",
    }
    with pytest.raises(catmap_adapter.CatmapAdapterError, match="not ready"):
        catmap_adapter.confirm_export(
            network, tmp_path, bundle["preview_token"],
            expected_selection_revision=0,
            expected_selected_export_sha256=None, confirmed=True,
            tool_path=tool)


def test_multiple_site_types_are_audit_only_without_site_totals(tmp_path):
    network = catmap_ready_network()
    empty_site = copy.deepcopy(next(
        record for record in network["species"] if record["id"] == "star_s"))
    empty_site.update({"id": "star_bridge", "sites": {"bridge": 1}})
    network["species"].append(empty_site)
    for record in network["species"]:
        if record["id"] in {"O2_s", "O2_ads_ts"}:
            record["sites"] = {"bridge": 1}
    for step in network["elementary_steps"]:
        if step["id"] == "o2_adsorption":
            step["reactants"] = {"O2_g": 1, "star_bridge": 1}
    network["feed_species"].append("star_bridge")
    _freeze(network)
    assert kinetics.audit_network(network)["machine_pass"] is True

    bundle = catmap_adapter.build_export_bundle(
        network, tool_path=_tool(tmp_path))

    assert bundle["contract_ready"] is False
    assert bundle["export_ready"] is False
    assert "exactly one independent site type" in bundle["adapter_issues"][0]["message"]
    assert set(bundle["files"]) == {
        "kinetics-audit.json", "catmap-adapter-audit.json",
    }


def test_export_identifiers_cannot_inject_python_or_tsv_rows():
    network = catmap_ready_network()
    network["species"][0]["id"] = "CO_g\n__import__('os').system('whoami')"
    _freeze(network)

    bundle = catmap_adapter.build_export_bundle(network, tool_path=None)

    assert bundle["audit"]["export_ready"] is False
    assert set(bundle["files"]) == {"kinetics-audit.json"}


def test_confirmed_manifest_rejects_artifact_tampering(tmp_path):
    network = catmap_ready_network()
    tool = _tool(tmp_path)
    preview = _preview(network, tool)
    confirmed = _confirm(network, tmp_path, preview, tool)
    export_dir = Path(confirmed["export_dir"])
    (export_dir / "model.mkm").write_text("tampered", encoding="utf-8")

    with pytest.raises(catmap_adapter.CatmapAdapterError, match="artifact (size|hash)"):
        catmap_adapter.load_confirmed_manifest(tmp_path, network["input_sha256"])


def test_export_selection_pointer_tampering_fails_closed(tmp_path):
    network = catmap_ready_network()
    tool = _tool(tmp_path)
    preview = _preview(network, tool)
    _confirm(network, tmp_path, preview, tool)
    pointer = (tmp_path / ".vcstudio" / "kinetics" / "exports" /
               network["input_sha256"] / "current.json")
    pointer.write_text('{"selected_export_sha256":"../../escape"}', encoding="utf-8")
    with pytest.raises(catmap_adapter.CatmapAdapterError, match="selection"):
        catmap_adapter.load_confirmed_manifest(tmp_path, network["input_sha256"])


def test_missing_confirmed_tree_has_stable_manifest_unavailable_error(tmp_path):
    with pytest.raises(catmap_adapter.CatmapAdapterError) as captured:
        catmap_adapter.load_confirmed_manifest(tmp_path, "a" * 64)

    assert str(captured.value) == (
        "confirmed CatMAP export manifest is unavailable")


def test_export_rejects_symlinked_directory_chain(tmp_path):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    link = project / ".vcstudio"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    network = catmap_ready_network()
    tool = _tool(tmp_path)
    preview = _preview(network, tool)

    with pytest.raises(catmap_adapter.CatmapAdapterError, match="symlink"):
        catmap_adapter.confirm_export(
            network, project, preview["preview_token"],
            expected_selection_revision=0,
            expected_selected_export_sha256=None, confirmed=True,
            tool_path=tool, tool_version="0.4.0")
    assert not any(outside.iterdir())


def test_export_rejects_authored_project_root_symlink_before_writing(tmp_path):
    project = tmp_path / "project"
    alias = tmp_path / "project-alias"
    project.mkdir()
    try:
        alias.symlink_to(project, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    network = catmap_ready_network()
    tool = _tool(tmp_path)
    preview = _preview(network, tool)

    with pytest.raises(catmap_adapter.CatmapAdapterError, match="project root"):
        _confirm(network, alias, preview, tool)

    assert not (project / ".vcstudio").exists()


def test_confirmed_export_rejects_directory_swap_to_external_symlink(tmp_path):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    network = catmap_ready_network()
    tool = _tool(tmp_path)
    preview = _preview(network, tool)
    confirmed = _confirm(network, project, preview, tool)
    export_dir = Path(confirmed["export_dir"])
    moved = outside / export_dir.name
    export_dir.rename(moved)
    try:
        export_dir.symlink_to(moved, target_is_directory=True)
    except OSError as exc:
        moved.rename(export_dir)
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(catmap_adapter.CatmapAdapterError, match="unavailable"):
        catmap_adapter.load_confirmed_manifest(project, network["input_sha256"])


@pytest.mark.parametrize("limit_name", [
    "_MAX_ARTIFACT_BYTES",
    "_MAX_TOTAL_ARTIFACT_BYTES",
])
def test_confirmation_cannot_publish_a_bundle_over_loader_limits(
        tmp_path, monkeypatch, limit_name):
    project = tmp_path / "project"
    project.mkdir()
    network = catmap_ready_network()
    tool = _tool(tmp_path)
    preview = _preview(network, tool)
    monkeypatch.setattr(catmap_adapter, limit_name, 1)

    with pytest.raises(catmap_adapter.CatmapAdapterError, match="limit"):
        catmap_adapter.confirm_export(
            network, project, preview["preview_token"],
            expected_selection_revision=0,
            expected_selected_export_sha256=None, confirmed=True,
            tool_path=tool, tool_version="0.4.0")
    assert not (project / ".vcstudio").exists()


def test_confirmed_manifest_and_artifacts_have_hard_size_limits(tmp_path):
    network = catmap_ready_network()
    tool = _tool(tmp_path)
    preview = _preview(network, tool)
    confirmed = _confirm(network, tmp_path, preview, tool)
    manifest = Path(confirmed["export_dir"]) / "manifest.json"
    manifest.write_bytes(b"{" + b" " * (1024 * 1024) + b"}")

    with pytest.raises(catmap_adapter.CatmapAdapterError, match="size limit"):
        catmap_adapter.load_confirmed_manifest(tmp_path, network["input_sha256"])


def test_confirmed_bundle_rejects_undeclared_catmap_python_file(tmp_path):
    network = catmap_ready_network()
    tool = _tool(tmp_path)
    preview = _preview(network, tool)
    confirmed = _confirm(network, tmp_path, preview, tool)
    export_dir = Path(confirmed["export_dir"])
    (export_dir / "catmap.py").write_text("raise SystemExit\n", encoding="utf-8")

    with pytest.raises(catmap_adapter.CatmapAdapterError, match="directory entries"):
        catmap_adapter.load_confirmed_manifest(tmp_path, network["input_sha256"])


def test_confirmed_bundle_rejects_nonregular_subdirectory(tmp_path):
    network = catmap_ready_network()
    tool = _tool(tmp_path)
    preview = _preview(network, tool)
    confirmed = _confirm(network, tmp_path, preview, tool)
    (Path(confirmed["export_dir"]) / "extra").mkdir()

    with pytest.raises(catmap_adapter.CatmapAdapterError, match="unsafe entries"):
        catmap_adapter.load_confirmed_manifest(tmp_path, network["input_sha256"])


def test_confirmed_bundle_rejects_symlink_or_reparse_entry(tmp_path):
    network = catmap_ready_network()
    tool = _tool(tmp_path)
    preview = _preview(network, tool)
    confirmed = _confirm(network, tmp_path, preview, tool)
    target = tmp_path / "external-catmap.py"
    target.write_text("raise SystemExit\n", encoding="utf-8")
    link = Path(confirmed["export_dir"]) / "catmap.py"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"file symlinks/reparse points unavailable: {exc}")

    with pytest.raises(catmap_adapter.CatmapAdapterError, match="unsafe entries"):
        catmap_adapter.load_confirmed_manifest(tmp_path, network["input_sha256"])


def test_confirmed_manifest_rejects_too_many_artifacts(tmp_path):
    network = catmap_ready_network()
    tool = _tool(tmp_path)
    preview = _preview(network, tool)
    confirmed = _confirm(network, tmp_path, preview, tool)
    manifest_path = Path(confirmed["export_dir"]) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    missing = catmap_adapter._MAX_ARTIFACT_COUNT - len(manifest["artifacts"]) + 1
    manifest["artifacts"].extend({
        "name": f"extra-{index}.json",
        "sha256": "0" * 64,
        "size": 0,
    } for index in range(missing))
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(catmap_adapter.CatmapAdapterError, match="artifact count"):
        catmap_adapter.load_confirmed_manifest(tmp_path, network["input_sha256"])


def test_confirmed_manifest_rejects_duplicate_artifact_names(tmp_path):
    network = catmap_ready_network()
    tool = _tool(tmp_path)
    preview = _preview(network, tool)
    confirmed = _confirm(network, tmp_path, preview, tool)
    manifest_path = Path(confirmed["export_dir"]) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"].append(copy.deepcopy(manifest["artifacts"][0]))
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(catmap_adapter.CatmapAdapterError, match="must be unique"):
        catmap_adapter.load_confirmed_manifest(tmp_path, network["input_sha256"])


@pytest.mark.parametrize("mode, message", [
    ("declared_single", "declared artifact exceeds the size limit"),
    ("actual_single", "artifact exceeds the size limit"),
    ("declared_total", "declared artifact bytes exceed the total limit"),
    ("actual_total", "actual artifact bytes exceed the total limit"),
])
def test_confirmed_artifact_byte_limits_cover_declared_and_actual_bytes(
        tmp_path, monkeypatch, mode, message):
    network = catmap_ready_network()
    tool = _tool(tmp_path)
    preview = _preview(network, tool)
    confirmed = _confirm(network, tmp_path, preview, tool)
    export_dir = Path(confirmed["export_dir"])
    manifest = json.loads(
        (export_dir / "manifest.json").read_text(encoding="utf-8"))
    largest = max(manifest["artifacts"], key=lambda item: item["size"])
    declared_total = sum(item["size"] for item in manifest["artifacts"])
    if mode == "declared_single":
        monkeypatch.setattr(
            catmap_adapter, "_MAX_ARTIFACT_BYTES", largest["size"] - 1)
    elif mode == "actual_single":
        monkeypatch.setattr(
            catmap_adapter, "_MAX_ARTIFACT_BYTES", largest["size"] + 1)
        path = export_dir / largest["name"]
        path.write_bytes(path.read_bytes() + b"xx")
    elif mode == "declared_total":
        monkeypatch.setattr(
            catmap_adapter, "_MAX_TOTAL_ARTIFACT_BYTES", declared_total - 1)
    else:
        monkeypatch.setattr(
            catmap_adapter, "_MAX_TOTAL_ARTIFACT_BYTES", declared_total + 1)
        path = export_dir / largest["name"]
        path.write_bytes(path.read_bytes() + b"xx")

    with pytest.raises(catmap_adapter.CatmapAdapterError, match=message):
        catmap_adapter.load_confirmed_manifest(tmp_path, network["input_sha256"])


def test_windows_junctions_are_classified_as_links():
    class JunctionLike:
        @staticmethod
        def is_symlink():
            return False

        @staticmethod
        def is_junction():
            return True

    assert catmap_adapter._is_link(JunctionLike()) is True
