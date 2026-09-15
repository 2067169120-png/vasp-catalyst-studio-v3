from __future__ import annotations

import hashlib
import math

import pytest

from vcstudio.gui_web.api import Api
from vcstudio.project.elf import ELFParseError, summarize_elfcar


def _elfcar(values, *, shape=(2, 2, 2), counts=(2,)):
    coordinates = "\n".join("0 0 0" for _ in range(sum(counts)))
    grid = " ".join(str(value) for value in values)
    return (
        "ELF distribution fixture\n1.0\n1 0 0\n0 1 0\n0 0 1\n"
        f"Si\n{' '.join(str(value) for value in counts)}\nDirect\n{coordinates}\n\n"
        f"{shape[0]} {shape[1]} {shape[2]}\n{grid}\n"
    )


def test_elf_distribution_summary_is_deterministic_path_free_and_bounded(tmp_path):
    values = [0.0, 0.1, 0.2, 0.4, 0.6, 0.8, 0.9, 1.0]
    raw = _elfcar(values)
    path = tmp_path / "ELFCAR"
    path.write_text(raw, encoding="utf-8")

    first = summarize_elfcar(path)
    second = summarize_elfcar(path)

    assert first == second
    assert first["analysis_kind"] == "elf_distribution_summary"
    assert first["grid_shape"] == [2, 2, 2]
    assert first["natoms"] == 2
    assert first["minimum"] == 0.0 and first["maximum"] == 1.0
    assert first["mean"] == pytest.approx(0.5)
    assert first["std"] == pytest.approx(math.sqrt(0.1275))
    assert sum(item["count"] for item in first["histogram"]) == 8
    assert first["quantiles"][3] == {"fraction": 0.5, "value": 0.5}
    assert first["source_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert first["denominator"] == {
        "grid_points": 8, "validated_points": 8,
        "histogram_bins": 20, "quantiles": 7,
    }
    assert str(tmp_path) not in str(first)
    assert "topology conclusion" in first["interpretation"]


def test_small_range_tolerance_is_clamped_and_counted():
    result = summarize_elfcar(
        _elfcar([-1e-8, 1.0 + 1e-8], shape=(2, 1, 1), counts=(1,)))
    assert result["minimum"] == 0.0
    assert result["maximum"] == 1.0
    assert result["range_adjusted_points"] == 2


@pytest.mark.parametrize("raw,match", [
    (_elfcar([0.0, 1.01], shape=(2, 1, 1), counts=(1,)), "physical ELF range"),
    (_elfcar([0.0, "nan"], shape=(2, 1, 1), counts=(1,)), "non-finite"),
    (_elfcar([0.0], shape=(0, 1, 1), counts=(1,)), "positive integers"),
    (_elfcar([0.0], shape=(2, 1, 1), counts=(1,)), "不足|grid data"),
])
def test_invalid_range_finite_grid_and_length_fail_closed(raw, match):
    with pytest.raises(ELFParseError, match=match):
        summarize_elfcar(raw)


def test_histogram_configuration_is_bounded():
    raw = _elfcar([0.5], shape=(1, 1, 1), counts=(1,))
    with pytest.raises(ELFParseError, match="histogram_bins"):
        summarize_elfcar(raw, histogram_bins=1)


def test_api_analyze_task_routes_to_real_elf_parser(tmp_path):
    (tmp_path / "ELFCAR").write_text(
        _elfcar([0.25, 0.75], shape=(2, 1, 1), counts=(1,)), encoding="utf-8")
    result = Api().analyze_task(str(tmp_path), kind="elf")
    assert result["ok"] is True
    assert result["analysis_status"] == "integrated"
    assert result["result"]["mean"] == pytest.approx(0.5)
    assert result["result"]["denominator"]["grid_points"] == 2
    assert "topology conclusion" in result["summary"]
