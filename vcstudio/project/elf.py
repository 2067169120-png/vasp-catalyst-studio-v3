"""Read-only quantitative ELFCAR distribution summaries.

The parser reports only bounded distribution statistics over the main VASP
volumetric grid.  It does not infer bonds, basins, critical points, topology,
or chemical conclusions from ELF values.
"""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
from typing import Any

from vcstudio.project.chgdiff import read_chgcar


SCHEMA = "vcstudio.elf-distribution/v1"
PARSER_ID = "vcstudio.project.elf:summarize_elfcar"
RANGE_TOLERANCE = 1e-6
DEFAULT_HISTOGRAM_BINS = 20
MAX_GRID_POINTS = 100_000_000
_QUANTILES = (0.0, 0.05, 0.25, 0.5, 0.75, 0.95, 1.0)


class ELFParseError(ValueError):
    """ELFCAR is malformed or outside the bounded distribution contract."""


def _source_bytes(value: Any) -> bytes:
    if isinstance(value, os.PathLike):
        return Path(value).read_bytes()
    if isinstance(value, str) and "\n" not in value:
        return Path(value).read_bytes()
    return str(value).encode("utf-8")


def _quantile(ordered: list[float], fraction: float) -> float:
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def summarize_elfcar(
    path_or_text: Any,
    *,
    histogram_bins: int = DEFAULT_HISTOGRAM_BINS,
    range_tolerance: float = RANGE_TOLERANCE,
) -> dict[str, Any]:
    """Return deterministic, path-free statistics for the main ELFCAR grid."""
    if (isinstance(histogram_bins, bool) or not isinstance(histogram_bins, int)
            or not 2 <= histogram_bins <= 200):
        raise ELFParseError("histogram_bins must be an integer between 2 and 200")
    if (isinstance(range_tolerance, bool)
            or not isinstance(range_tolerance, (int, float))
            or not math.isfinite(float(range_tolerance))
            or not 0.0 <= float(range_tolerance) <= 1e-3):
        raise ELFParseError("range_tolerance must be finite and between 0 and 0.001")
    try:
        source = _source_bytes(path_or_text)
        parsed = read_chgcar(path_or_text)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ELFParseError(f"ELFCAR could not be parsed: {exc}") from exc

    shape = tuple(parsed.get(key) for key in ("ngx", "ngy", "ngz"))
    if (any(isinstance(item, bool) or not isinstance(item, int) or item <= 0
            for item in shape)):
        raise ELFParseError("ELFCAR grid dimensions must be positive integers")
    points = shape[0] * shape[1] * shape[2]
    if not 1 <= points <= MAX_GRID_POINTS:
        raise ELFParseError("ELFCAR grid size is outside the supported bound")
    grid = parsed.get("grid")
    if not isinstance(grid, list) or len(grid) != points:
        raise ELFParseError("ELFCAR grid length does not match the grid dimensions")
    natoms = parsed.get("natoms")
    counts = parsed.get("counts")
    if (isinstance(natoms, bool) or not isinstance(natoms, int) or natoms <= 0
            or not isinstance(counts, list)
            or any(isinstance(item, bool) or not isinstance(item, int) or item < 0
                   for item in counts)
            or sum(counts) != natoms):
        raise ELFParseError("ELFCAR atom counts are invalid")

    tolerance = float(range_tolerance)
    values: list[float] = []
    adjusted = 0
    for raw in grid:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ELFParseError("ELFCAR grid contains a non-numeric value")
        value = float(raw)
        if not math.isfinite(value):
            raise ELFParseError("ELFCAR grid contains a non-finite value")
        if value < -tolerance or value > 1.0 + tolerance:
            raise ELFParseError("ELFCAR grid contains a value outside the physical ELF range")
        bounded = min(1.0, max(0.0, value))
        adjusted += int(bounded != value)
        values.append(bounded)

    ordered = sorted(values)
    mean = math.fsum(values) / points
    variance = math.fsum((value - mean) ** 2 for value in values) / points
    counts_by_bin = [0] * histogram_bins
    for value in values:
        index = min(int(value * histogram_bins), histogram_bins - 1)
        counts_by_bin[index] += 1
    width = 1.0 / histogram_bins
    histogram = [{
        "low": index * width,
        "high": (index + 1) * width,
        "count": count,
    } for index, count in enumerate(counts_by_bin)]
    quantiles = [{"fraction": fraction, "value": _quantile(ordered, fraction)}
                 for fraction in _QUANTILES]
    return {
        "schema": SCHEMA,
        "analysis_kind": "elf_distribution_summary",
        "interpretation": (
            "ELF distribution summary only; no bond, basin, critical-point, "
            "or topology conclusion is inferred."
        ),
        "grid_shape": list(shape),
        "natoms": natoms,
        "minimum": ordered[0],
        "maximum": ordered[-1],
        "mean": mean,
        "std": math.sqrt(variance),
        "quantiles": quantiles,
        "histogram": histogram,
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "range_tolerance": tolerance,
        "range_adjusted_points": adjusted,
        "denominator": {
            "grid_points": points,
            "validated_points": len(values),
            "histogram_bins": histogram_bins,
            "quantiles": len(quantiles),
        },
    }


__all__ = [
    "DEFAULT_HISTOGRAM_BINS", "ELFParseError", "MAX_GRID_POINTS",
    "PARSER_ID", "RANGE_TOLERANCE", "SCHEMA", "summarize_elfcar",
]
