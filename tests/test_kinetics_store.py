"""Project-confined immutable microkinetic result storage."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_kinetics import frozen_network, valid_result
from vcstudio.project import kinetics_store


EXPECTED_ADAPTER = {
    "id": "vcstudio.catmap-process-adapter",
    "version": "1",
    "tool_sha256": "4" * 64,
}


def test_store_and_reload_revalidate_raw_result(tmp_path):
    network = frozen_network()
    stored = kinetics_store.store_result(
        tmp_path, valid_result(network), network,
        expected_adapter=EXPECTED_ADAPTER)
    loaded = kinetics_store.load_result(
        tmp_path, network, expected_adapter=EXPECTED_ADAPTER)

    assert stored["result_sha256"] == loaded["result_sha256"]
    assert loaded["normalized"]["scientific_status"] == "diagnostic"
    assert loaded["normalized"]["eligible_final"] is False
    assert str(tmp_path) not in json.dumps(loaded)
    result_root = Path(tmp_path) / ".vcstudio" / "kinetics" / "results"
    assert (result_root / network["input_sha256"] / "latest.json").is_file()


def test_tampered_latest_pointer_and_raw_result_fail_closed(tmp_path):
    network = frozen_network()
    stored = kinetics_store.store_result(
        tmp_path, valid_result(network), network,
        expected_adapter=EXPECTED_ADAPTER)
    result_root = Path(tmp_path) / ".vcstudio" / "kinetics" / "results"
    latest = result_root / network["input_sha256"] / "latest.json"
    latest.write_text('{"result_sha256":"../../escape"}', encoding="utf-8")

    with pytest.raises(kinetics_store.KineticsStoreError, match="pointer"):
        kinetics_store.load_result(
            tmp_path, network, expected_adapter=EXPECTED_ADAPTER)

    latest.write_text(json.dumps({"result_sha256": stored["result_sha256"]}),
                      encoding="utf-8")
    raw = (result_root / network["input_sha256"] /
           stored["result_sha256"] / "result.json")
    raw.write_text('{}', encoding="utf-8")
    with pytest.raises(kinetics_store.KineticsStoreError, match="hash"):
        kinetics_store.load_result(
            tmp_path, network, expected_adapter=EXPECTED_ADAPTER)


def test_missing_result_is_an_explicit_none_not_an_empty_success(tmp_path):
    assert kinetics_store.load_result(
        tmp_path, frozen_network(), expected_adapter=EXPECTED_ADAPTER) is None


def test_result_store_rejects_symlinked_directory_chain(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / ".vcstudio"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(kinetics_store.KineticsStoreError, match="symlink"):
        kinetics_store.store_result(
            tmp_path, valid_result(frozen_network()), frozen_network(),
            expected_adapter=EXPECTED_ADAPTER)
    assert not any(outside.iterdir())


def test_result_loader_rejects_directory_swap_to_external_symlink(tmp_path):
    network = frozen_network()
    stored = kinetics_store.store_result(
        tmp_path, valid_result(network), network,
        expected_adapter=EXPECTED_ADAPTER)
    result_dir = (tmp_path / ".vcstudio" / "kinetics" / "results" /
                  network["input_sha256"] / stored["result_sha256"])
    outside = tmp_path / "outside"
    outside.mkdir()
    moved = outside / result_dir.name
    result_dir.rename(moved)
    try:
        result_dir.symlink_to(moved, target_is_directory=True)
    except OSError as exc:
        moved.rename(result_dir)
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(kinetics_store.KineticsStoreError, match="unsafe"):
        kinetics_store.load_result(
            tmp_path, network, expected_adapter=EXPECTED_ADAPTER)


def test_result_size_limit_precedes_schema_normalization(tmp_path):
    network = frozen_network()
    result = valid_result(network)
    result["padding"] = "x" * (20 * 1024 * 1024)

    with pytest.raises(kinetics_store.KineticsStoreError, match="size limit"):
        kinetics_store.store_result(
            tmp_path, result, network, expected_adapter=EXPECTED_ADAPTER)


def test_windows_junctions_are_classified_as_store_links():
    class JunctionLike:
        @staticmethod
        def is_symlink():
            return False

        @staticmethod
        def is_junction():
            return True

    assert kinetics_store._is_link(JunctionLike()) is True
