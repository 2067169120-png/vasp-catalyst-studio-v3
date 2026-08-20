"""Project-confined immutable microkinetic result storage."""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path

import pytest

from tests.test_kinetics import frozen_network, valid_result
from vcstudio.project import kinetics_store


EXPECTED_ADAPTER = {
    "id": "vcstudio.catmap-process-adapter",
    "version": "2",
    "tool_sha256": "4" * 64,
}
EXPORT_SHA256 = "e" * 64


def _upload(tmp_path, network, result=None):
    return kinetics_store.store_result(
        tmp_path, result or valid_result(network), network,
        expected_adapter=EXPECTED_ADAPTER,
        confirmed_export_sha256=EXPORT_SHA256)


def _select(tmp_path, network, stored, *, revision=0, latest=None):
    return kinetics_store.select_result(
        tmp_path, stored["result_sha256"], network,
        confirmed_export_sha256=EXPORT_SHA256,
        expected_latest_sha256=latest, expected_revision=revision,
        confirmed=True, expected_adapter=EXPECTED_ADAPTER)


def test_store_and_reload_revalidate_raw_result(tmp_path):
    network = frozen_network()
    stored = _upload(tmp_path, network)
    assert kinetics_store.selection_snapshot(tmp_path, network)["revision"] == 0
    assert not (tmp_path / ".vcstudio" / "kinetics" / "results" /
                network["input_sha256"] / "latest.json").exists()
    selected = _select(tmp_path, network, stored)
    loaded = kinetics_store.load_result(
        tmp_path, network, expected_adapter=EXPECTED_ADAPTER,
        confirmed_export_sha256=EXPORT_SHA256)

    assert selected["result_sha256"] == loaded["result_sha256"]
    assert loaded["normalized"]["scientific_status"] == "diagnostic"
    assert loaded["normalized"]["eligible_final"] is False
    assert str(tmp_path) not in json.dumps(loaded)
    result_root = Path(tmp_path) / ".vcstudio" / "kinetics" / "results"
    assert (result_root / network["input_sha256"] / "latest.json").is_file()


def test_tampered_latest_pointer_and_raw_result_fail_closed(tmp_path):
    network = frozen_network()
    stored = _upload(tmp_path, network)
    _select(tmp_path, network, stored)
    result_root = Path(tmp_path) / ".vcstudio" / "kinetics" / "results"
    latest = result_root / network["input_sha256"] / "latest.json"
    pointer = kinetics_store.selection_snapshot(tmp_path, network)
    latest.write_text('{"result_sha256":"../../escape"}', encoding="utf-8")

    with pytest.raises(kinetics_store.KineticsStoreError, match="pointer"):
        kinetics_store.load_result(
            tmp_path, network, expected_adapter=EXPECTED_ADAPTER,
            confirmed_export_sha256=EXPORT_SHA256)

    latest.write_text(json.dumps(pointer), encoding="utf-8")
    raw = (result_root / network["input_sha256"] /
           stored["result_sha256"] / "result.json")
    raw.write_text('{}', encoding="utf-8")
    with pytest.raises(kinetics_store.KineticsStoreError, match="hash"):
        kinetics_store.load_result(
            tmp_path, network, expected_adapter=EXPECTED_ADAPTER,
            confirmed_export_sha256=EXPORT_SHA256)


def test_missing_result_is_an_explicit_none_not_an_empty_success(tmp_path):
    assert kinetics_store.load_result(
        tmp_path, frozen_network(), expected_adapter=EXPECTED_ADAPTER,
        confirmed_export_sha256=EXPORT_SHA256) is None


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
            expected_adapter=EXPECTED_ADAPTER,
            confirmed_export_sha256=EXPORT_SHA256)
    assert not any(outside.iterdir())


def test_result_loader_rejects_directory_swap_to_external_symlink(tmp_path):
    network = frozen_network()
    stored = _upload(tmp_path, network)
    _select(tmp_path, network, stored)
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
            tmp_path, network, expected_adapter=EXPECTED_ADAPTER,
            confirmed_export_sha256=EXPORT_SHA256)


def test_result_size_limit_precedes_schema_normalization(tmp_path):
    network = frozen_network()
    result = valid_result(network)
    result["padding"] = "x" * (20 * 1024 * 1024)

    with pytest.raises(kinetics_store.KineticsStoreError, match="size limit"):
        kinetics_store.store_result(
            tmp_path, result, network, expected_adapter=EXPECTED_ADAPTER,
            confirmed_export_sha256=EXPORT_SHA256)


def test_windows_junctions_are_classified_as_store_links():
    class JunctionLike:
        @staticmethod
        def is_symlink():
            return False

        @staticmethod
        def is_junction():
            return True

    assert kinetics_store._is_link(JunctionLike()) is True


def test_upload_and_selection_are_split_and_stale_cas_cannot_last_win(tmp_path):
    network = frozen_network()
    first = _upload(tmp_path, network)
    changed_result = copy.deepcopy(valid_result(network))
    changed_result["points"][0]["tof"][0]["value"] = 3.5
    second = _upload(tmp_path, network, changed_result)
    assert kinetics_store.selection_snapshot(tmp_path, network)[
        "latest_result_sha256"] is None

    first_selected = _select(tmp_path, network, first)
    with pytest.raises(kinetics_store.KineticsStoreError, match="conflict"):
        _select(tmp_path, network, second)
    snapshot = kinetics_store.selection_snapshot(tmp_path, network)
    assert snapshot["latest_result_sha256"] == first["result_sha256"]

    second_selected = _select(
        tmp_path, network, second, revision=snapshot["revision"],
        latest=snapshot["latest_result_sha256"])
    assert second_selected["selection_revision"] == 2
    history = (tmp_path / ".vcstudio" / "kinetics" / "results" /
               network["input_sha256"] / "selection-history")
    assert len(list(history.glob("*.json"))) == 2
    assert first_selected["selection_event_sha256"] != second_selected[
        "selection_event_sha256"]


def test_two_concurrent_result_selections_have_exactly_one_cas_winner(tmp_path):
    network = frozen_network()
    results = []
    for value in (3.0, 4.0):
        payload = copy.deepcopy(valid_result(network))
        payload["points"][0]["tof"][0]["value"] = value
        results.append(_upload(tmp_path, network, payload))
    barrier = threading.Barrier(2)
    outcomes = []

    def worker(stored):
        barrier.wait()
        try:
            _select(tmp_path, network, stored)
            outcomes.append("selected")
        except kinetics_store.KineticsStoreError as exc:
            outcomes.append("conflict" if "conflict" in str(exc) else str(exc))

    threads = [threading.Thread(target=worker, args=(stored,)) for stored in results]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ["conflict", "selected"]


def test_result_uploaded_for_one_export_cannot_be_selected_for_another(tmp_path):
    network = frozen_network()
    stored = _upload(tmp_path, network)
    with pytest.raises(kinetics_store.KineticsStoreError, match="receipt"):
        kinetics_store.select_result(
            tmp_path, stored["result_sha256"], network,
            confirmed_export_sha256="f" * 64,
            expected_latest_sha256=None, expected_revision=0, confirmed=True,
            expected_adapter=EXPECTED_ADAPTER)


def test_selection_history_tampering_fails_closed(tmp_path):
    network = frozen_network()
    stored = _upload(tmp_path, network)
    selected = _select(tmp_path, network, stored)
    history = (tmp_path / ".vcstudio" / "kinetics" / "results" /
               network["input_sha256"] / "selection-history")
    event = next(history.glob(
        f"*-{selected['selection_event_sha256']}.json"))
    event.write_text("{}", encoding="utf-8")
    with pytest.raises(kinetics_store.KineticsStoreError, match="history"):
        kinetics_store.load_result(
            tmp_path, network, expected_adapter=EXPECTED_ADAPTER,
            confirmed_export_sha256=EXPORT_SHA256)
