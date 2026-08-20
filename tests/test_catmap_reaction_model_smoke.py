"""Conditional real-CatMAP integration smoke; CatMAP remains user-installed."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_kinetics import catmap_ready_network
from vcstudio.external import catmap_adapter


def test_generated_single_point_setup_runs_with_real_catmap(tmp_path, monkeypatch):
    catmap = pytest.importorskip(
        "catmap",
        reason="user-installed CatMAP is unavailable; external GPL smoke skipped",
    )
    bundle = catmap_adapter.build_export_bundle(
        catmap_ready_network(), tool_path=None)
    assert bundle["contract_ready"] is True
    for name, content in bundle["files"].items():
        if name in {"energetics.tsv", "model.mkm"}:
            (tmp_path / name).write_text(content, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    model = catmap.ReactionModel(setup_file=str(Path("model.mkm")))
    model.run()
    assert model.descriptor_names == ["temperature", "pressure"]
    assert list(model.descriptors) == [500.0, 1.0]
    assert model.gas_names == ["g0_g", "g1_g"]
    assert model.atomic_reservoir_list
