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


def _tool(tmp_path):
    path = tmp_path / "catmap.exe"
    path.write_bytes(b"user-installed-catmap-process-adapter")
    return path


def _confirm(network, project, preview, tool):
    return catmap_adapter.confirm_export(
        network, project, preview["preview_token"],
        expected_selection_revision=0,
        expected_selected_export_sha256=None, confirmed=True,
        tool_path=tool, tool_version=preview["tool"]["version"])


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
    preview = catmap_adapter.preview_export(network, tool_path=tool)
    changed = copy.deepcopy(network)
    changed["standard_state"]["temperature"]["value"] = 550.0
    _freeze(changed)

    with pytest.raises(catmap_adapter.CatmapAdapterError, match="preview token"):
        catmap_adapter.confirm_export(
            changed, tmp_path, preview["preview_token"],
            expected_selection_revision=0,
            expected_selected_export_sha256=None, confirmed=True,
            tool_path=tool)


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
    first_preview = catmap_adapter.preview_export(network, tool_path=tool)
    first = _confirm(network, tmp_path, first_preview, tool)

    tool.write_bytes(b"different-user-installed-catmap-adapter")
    second_preview = catmap_adapter.preview_export(network, tool_path=tool)
    assert second_preview["preview_token"] != first_preview["preview_token"]
    second = catmap_adapter.confirm_export(
        network, tmp_path, second_preview["preview_token"],
        expected_selection_revision=first["selection_revision"],
        expected_selected_export_sha256=first["selected_export_sha256"],
        confirmed=True, tool_path=tool)

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
            tool_path=tool)


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
    preview = catmap_adapter.preview_export(network, tool_path=tool)
    confirmed = _confirm(network, tmp_path, preview, tool)
    export_dir = Path(confirmed["export_dir"])
    (export_dir / "model.mkm").write_text("tampered", encoding="utf-8")

    with pytest.raises(catmap_adapter.CatmapAdapterError, match="artifact (size|hash)"):
        catmap_adapter.load_confirmed_manifest(tmp_path, network["input_sha256"])


def test_export_selection_pointer_tampering_fails_closed(tmp_path):
    network = catmap_ready_network()
    tool = _tool(tmp_path)
    preview = catmap_adapter.preview_export(network, tool_path=tool)
    _confirm(network, tmp_path, preview, tool)
    pointer = (tmp_path / ".vcstudio" / "kinetics" / "exports" /
               network["input_sha256"] / "current.json")
    pointer.write_text('{"selected_export_sha256":"../../escape"}', encoding="utf-8")
    with pytest.raises(catmap_adapter.CatmapAdapterError, match="selection"):
        catmap_adapter.load_confirmed_manifest(tmp_path, network["input_sha256"])


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
    preview = catmap_adapter.preview_export(network, tool_path=tool)

    with pytest.raises(catmap_adapter.CatmapAdapterError, match="symlink"):
        catmap_adapter.confirm_export(
            network, project, preview["preview_token"],
            expected_selection_revision=0,
            expected_selected_export_sha256=None, confirmed=True,
            tool_path=tool)
    assert not any(outside.iterdir())


def test_confirmed_export_rejects_directory_swap_to_external_symlink(tmp_path):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    network = catmap_ready_network()
    tool = _tool(tmp_path)
    preview = catmap_adapter.preview_export(network, tool_path=tool)
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


def test_confirmed_manifest_and_artifacts_have_hard_size_limits(tmp_path):
    network = catmap_ready_network()
    tool = _tool(tmp_path)
    preview = catmap_adapter.preview_export(network, tool_path=tool)
    confirmed = _confirm(network, tmp_path, preview, tool)
    manifest = Path(confirmed["export_dir"]) / "manifest.json"
    manifest.write_bytes(b"{" + b" " * (1024 * 1024) + b"}")

    with pytest.raises(catmap_adapter.CatmapAdapterError, match="size limit"):
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
