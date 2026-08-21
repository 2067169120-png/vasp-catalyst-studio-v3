"""Data-only CatMAP adapter boundary.

CatMAP is an external GPL-3.0 program.  VASP Catalyst Studio neither vendors
nor imports it.  This module only emits independently-authored table/setup
files, freezes the user-configured tool identity, and describes a fixed
non-executable process contract.  It contains no subprocess or installation
path.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import secrets
import stat
import tempfile
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vcstudio.project import kinetics


ADAPTER_ID = "vcstudio.catmap-process-adapter"
ADAPTER_VERSION = "3"
PREVIEW_SCHEMA = "vcstudio.catmap-export-preview/v3"
BUNDLE_SCHEMA = "vcstudio.catmap-export-bundle/v3"
MANIFEST_SCHEMA = "vcstudio.catmap-export-manifest/v3"
PROCESS_SCHEMA = "vcstudio.external-process-contract/v3"
AUDIT_PREVIEW_SCHEMA = "vcstudio.kinetics-audit-export-preview/v1"
AUDIT_MANIFEST_SCHEMA = "vcstudio.kinetics-audit-export-manifest/v1"
EXPORT_SELECTION_SCHEMA = "vcstudio.catmap-export-selection/v1"
EXPORT_RESERVATION_SCHEMA = "vcstudio.catmap-export-reservation/v1"
EXPORT_SELECTION_ANCHOR_SCHEMA = "vcstudio.catmap-selection-anchor-record/v1"
CATMAP_LICENSE = "GPL-3.0"
CATMAP_PROJECT = "https://github.com/SUNCAT-Center/catmap"
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
_MAX_ARTIFACT_COUNT = 64
_MAX_TOTAL_ARTIFACT_BYTES = 64 * 1024 * 1024
_MAX_BUNDLES_PER_INPUT = 32
_MAX_INPUT_BUNDLE_BYTES = 512 * 1024 * 1024
_RESERVATION_FILENAME = ".reservation.json"
_SELECTION_ANCHOR_FILENAME = ".selection-anchor.wal"
_MAX_SELECTION_ANCHOR_BYTES = 32 * 1024 * 1024
_MAX_SELECTION_ANCHOR_RECORDS = 8192
_READ_CHUNK_BYTES = 1024 * 1024
_ZERO_SHA256 = "0" * 64

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SAFE_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")
_SAFE_ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CATMAP_NAME_RE = re.compile(r"[^A-Za-z0-9_]")
_OUTPUT_VARIABLES = [
    "coverage", "production_rate", "consumption_rate", "turnover_frequency",
    "selectivity", "rate_control", "selectivity_control", "rxn_order",
    "free_energy",
]
_SCAN_RESOLUTION = [5, 5]
_SELECTION_LOCK = threading.RLock()


class CatmapAdapterError(ValueError):
    """The safe adapter preview/confirmation contract was violated."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _json_text(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False,
    ) + "\n"


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)


def _selection_fault(_stage: str) -> None:
    """Internal crash-injection seam; production never installs behavior."""


def _source_mapping(source) -> kinetics.CanonicalKineticsInput:
    try:
        return kinetics.canonicalize_kinetics_input(source)
    except kinetics.KineticsContractError as exc:
        raise CatmapAdapterError(str(exc)) from exc


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_link(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(callable(is_junction) and is_junction())


def _is_reparse(metadata: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)


def _entity_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
    )


def _file_snapshot(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        *_entity_identity(metadata),
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _lstat_regular(path: Path, *, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CatmapAdapterError(f"{label} is unavailable") from exc
    if (stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata)
            or not stat.S_ISREG(metadata.st_mode)):
        raise CatmapAdapterError(f"{label} is not a safe regular file")
    return metadata


def _lstat_directory(path: Path, *, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CatmapAdapterError(f"{label} is unavailable") from exc
    if (stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata)
            or not stat.S_ISDIR(metadata.st_mode)):
        raise CatmapAdapterError(f"{label} is unavailable")
    return metadata


def _bounded_scandir(
        path: Path, *, maximum: int, label: str) -> list[os.DirEntry[str]]:
    """Collect at most ``maximum`` entries, stopping on the next item."""
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0:
        raise CatmapAdapterError(f"{label} entry limit is invalid")
    entries: list[os.DirEntry[str]] = []
    try:
        with os.scandir(path) as iterator:
            for entry in iterator:
                if len(entries) >= maximum:
                    raise CatmapAdapterError(
                        f"{label} exceeds the entry limit")
                entries.append(entry)
    except CatmapAdapterError:
        raise
    except OSError as exc:
        raise CatmapAdapterError(f"{label} could not be inspected") from exc
    return entries


class _ProjectBoundary:
    """Pin the authored project and every adapter-created directory entity."""

    def __init__(self, project_root: str | os.PathLike[str]):
        root = Path(os.path.abspath(os.fspath(project_root)))
        if not Path(project_root).is_absolute():
            raise CatmapAdapterError("project root must be an absolute directory")
        metadata = _lstat_directory(root, label="project root")
        if (os.path.normcase(str(root)) != os.path.normcase(os.path.realpath(root))
                or _is_link(root)):
            raise CatmapAdapterError(
                "project root must not be a symlink, junction, or reparse point")
        self.root = root
        self._pins: dict[Path, tuple[int, int, int]] = {
            root: _entity_identity(metadata),
        }
        self.verify()

    def verify(self) -> None:
        for path, identity in tuple(self._pins.items()):
            metadata = _lstat_directory(path, label="CatMAP project boundary")
            if (_entity_identity(metadata) != identity
                    or os.path.normcase(str(path))
                    != os.path.normcase(os.path.realpath(path))):
                raise CatmapAdapterError(
                    "CatMAP project directory entity changed during the operation")

    def pin_directory(self, path: Path, *, label: str) -> Path:
        self.verify()
        metadata = _lstat_directory(path, label=label)
        authored = Path(os.path.abspath(path))
        try:
            if os.path.commonpath((str(self.root), str(authored))) != str(self.root):
                raise ValueError
        except ValueError as exc:
            raise CatmapAdapterError(
                "CatMAP directory escaped the project root") from exc
        if (os.path.normcase(str(authored))
                != os.path.normcase(os.path.realpath(authored))):
            raise CatmapAdapterError(
                "CatMAP directory must not use a link or reparse point")
        identity = _entity_identity(metadata)
        prior = self._pins.get(authored)
        if prior is not None and prior != identity:
            raise CatmapAdapterError(
                "CatMAP project directory entity changed during the operation")
        self._pins[authored] = identity
        self.verify()
        return authored

    def child(self, parent: Path, name: str, *, create: bool = True) -> Path:
        if (not isinstance(name, str) or not name or Path(name).name != name
                or name in {".", ".."}):
            raise CatmapAdapterError("CatMAP directory component is unsafe")
        self.verify()
        path = parent / name
        if create:
            try:
                os.mkdir(path)
            except FileExistsError:
                pass
            except OSError as exc:
                raise CatmapAdapterError(
                    "CatMAP project directory is unavailable") from exc
        if _is_link(path):
            raise CatmapAdapterError(
                "CatMAP project directory is unavailable: symlink, junction, "
                "or reparse point is forbidden")
        return self.pin_directory(path, label="CatMAP project directory")

    def unpin(self, path: Path) -> None:
        self._pins.pop(Path(os.path.abspath(path)), None)


def _read_bounded_regular_file(
        path: Path, *, maximum: int, label: str,
        expected: os.stat_result | None = None,
        collect: bool = False) -> tuple[bytes | None, int, str]:
    """Read/hash one unchanged regular-file entity through a bounded fd."""
    before = _lstat_regular(path, label=label)
    if expected is not None and _file_snapshot(before) != _file_snapshot(expected):
        raise CatmapAdapterError(f"{label} changed during validation")
    if before.st_size > maximum:
        raise CatmapAdapterError(f"{label} exceeds the size limit")
    flags = os.O_RDONLY
    for flag_name in ("O_BINARY", "O_CLOEXEC", "O_NOFOLLOW"):
        flags |= getattr(os, flag_name, 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CatmapAdapterError(f"{label} is unavailable or unsafe") from exc
    chunks = [] if collect else None
    digest = hashlib.sha256()
    count = 0
    try:
        opened = os.fstat(descriptor)
        if (stat.S_ISLNK(opened.st_mode) or _is_reparse(opened)
                or not stat.S_ISREG(opened.st_mode)
                or _entity_identity(opened) != _entity_identity(before)):
            raise CatmapAdapterError(f"{label} is not a safe regular file")
        if opened.st_size > maximum:
            raise CatmapAdapterError(f"{label} exceeds the size limit")
        while True:
            chunk = os.read(
                descriptor,
                min(_READ_CHUNK_BYTES, maximum - count + 1),
            )
            if not chunk:
                break
            count += len(chunk)
            if count > maximum:
                raise CatmapAdapterError(f"{label} exceeds the size limit")
            digest.update(chunk)
            if chunks is not None:
                chunks.append(chunk)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise CatmapAdapterError(f"{label} could not be read safely") from exc
    finally:
        os.close(descriptor)
    try:
        current = path.lstat()
    except OSError as exc:
        raise CatmapAdapterError(f"{label} changed during validation") from exc
    if (_file_snapshot(opened) != _file_snapshot(after)
            or _file_snapshot(after) != _file_snapshot(current)
            or count != after.st_size):
        raise CatmapAdapterError(f"{label} changed during validation")
    data = b"".join(chunks) if chunks is not None else None
    return data, count, digest.hexdigest()


def inspect_tool_path(tool_path: str | os.PathLike | None, *,
                      version: str | None = None) -> dict[str, Any]:
    """Hash a user-configured regular file without importing or executing it."""
    unavailable = {
        "available": False, "name": None, "version": None,
        "sha256": None, "size": None,
    }
    if not isinstance(tool_path, (str, os.PathLike)):
        return unavailable
    raw = os.fspath(tool_path)
    if not raw or _CONTROL_RE.search(raw):
        return unavailable
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        return unavailable
    try:
        if _is_link(candidate) or not candidate.is_file():
            return unavailable
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file():
            return unavailable
        safe_version = None
        if version is not None:
            if not isinstance(version, str) or not _SAFE_VERSION_RE.fullmatch(version):
                return unavailable
            safe_version = version
        return {
            "available": True,
            "name": resolved.name,
            "version": safe_version,
            "sha256": _file_sha256(resolved),
            "size": resolved.stat().st_size,
        }
    except OSError:
        return unavailable


def _catmap_base(species_id: str, phase: str) -> str:
    base = species_id
    if phase == "gas" and base.endswith("_g"):
        base = base[:-2]
    elif phase in {"adsorbate", "surface"} and base.endswith("_s"):
        base = base[:-2]
    text = _CATMAP_NAME_RE.sub("_", base).strip("_") or "species"
    if text[0].isdigit():
        text = "sp_" + text
    return text


def _catmap_name_map(network: Mapping[str, Any]) -> dict[str, Any]:
    """Build one unambiguous mapping; CatMAP reserves one underscore for site."""
    records = list(network.get("species") or [])
    unsupported_phases = sorted({
        str(record.get("phase")) for record in records
        if record.get("phase") in {"liquid", "solution"}
    })
    if unsupported_phases:
        raise CatmapAdapterError(
            "CatMAP phase-1 export does not support liquid/solution species; "
            "fluid phases are never coerced to gas")
    for record in records:
        if record.get("phase") != "gas":
            continue
        standard_state = record.get("standard_state")
        # Legacy solver-ready v3 fixtures predate per-species FluidState
        # ledgers and remain governed by the exact network-level 1-bar
        # standard state.  When a canonical FluidState ledger is present it
        # must agree with CatMAP phase-1's 1-bar reference.
        if standard_state is None:
            continue
        value = (
            standard_state.get("value")
            if isinstance(standard_state, Mapping) else None
        )
        if (
            not isinstance(standard_state, Mapping)
            or set(standard_state)
            != {"schema", "phase", "kind", "value", "unit"}
            or standard_state.get("schema")
            != "vcstudio.fluid-standard-state/v1"
            or standard_state.get("phase") != "gas"
            or standard_state.get("kind") != "1-bar"
            or standard_state.get("unit") != "Pa"
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or abs(float(value) - 100000.0) > 1.0e-12
        ):
            raise CatmapAdapterError(
                "CatMAP phase-1 export supports only the exact 1-bar gas "
                "standard state; 1-atm requires an explicit chemical-potential "
                "reference conversion and remains audit-only")
    for record in records:
        energy = record.get("formation_energy")
        value = energy.get("value") if isinstance(energy, Mapping) else None
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value))):
            raise CatmapAdapterError(
                "CatMAP phase-1 export requires an exact frozen formation energy "
                "for every species")
    site_ids = sorted({
        str(site) for record in records
        for site in (record.get("sites") or {})
    })
    if len(site_ids) != 1:
        raise CatmapAdapterError(
            "phase-1 CatMAP export supports exactly one independent site type")
    sites = {site_id: f"s{index}" for index, site_id in enumerate(site_ids)}
    empty_by_site: dict[str, str] = {}
    species = {}
    phase_counters = {"gas": 0, "adsorbate": 0, "transition_state": 0}
    for record in records:
        species_id = str(record["id"])
        phase = str(record["phase"])
        occupations = record.get("sites") or {}
        if phase == "gas":
            if occupations:
                raise CatmapAdapterError("gas species must not occupy CatMAP sites")
            base = f"g{phase_counters['gas']}"
            phase_counters["gas"] += 1
            species[species_id] = {
                "key": f"{base}_g", "name": base, "site": "gas", "phase": phase,
            }
            continue
        if phase not in {"surface", "adsorbate", "transition_state"}:
            raise CatmapAdapterError(
                f"CatMAP phase-1 export does not support species phase {phase!r}")
        if len(occupations) != 1:
            raise CatmapAdapterError(
                f"species {species_id} must map to exactly one CatMAP site")
        canonical_site, occupancy = next(iter(occupations.items()))
        if float(occupancy) != 1.0:
            raise CatmapAdapterError(
                f"species {species_id} must occupy exactly one CatMAP site")
        mapped_site = sites[str(canonical_site)]
        if phase == "surface":
            if float(record["formation_energy"]["value"]) != 0.0:
                raise CatmapAdapterError(
                    f"explicit empty-site species {species_id} must have exactly "
                    "zero formation_energy; implicit rebasing is forbidden")
            if str(canonical_site) in empty_by_site:
                raise CatmapAdapterError(
                    f"CatMAP site {canonical_site} has multiple empty-site species")
            empty_by_site[str(canonical_site)] = species_id
            species[species_id] = {
                "key": f"*_{mapped_site}", "name": "*", "site": mapped_site,
                "phase": phase,
            }
        else:
            base = (
                f"a{phase_counters[phase]}" if phase == "adsorbate"
                else f"ts-{phase_counters[phase]}")
            phase_counters[phase] += 1
            species[species_id] = {
                "key": f"{base}_{mapped_site}", "name": base,
                "site": mapped_site, "phase": phase,
            }
    missing = sorted(set(site_ids) - set(empty_by_site))
    if missing:
        raise CatmapAdapterError(
            "each CatMAP site requires exactly one frozen empty-site species")
    keys = [item["key"] for item in species.values()]
    if len(keys) != len(set(keys)):
        raise CatmapAdapterError("CatMAP name map contains a collision")
    return {
        "schema": "vcstudio.catmap-name-map/v1",
        "surface": "surface0", "sites": sites, "species": species,
    }


def _species_names(network: Mapping[str, Any]) -> dict[str, str]:
    return {
        key: value["key"]
        for key, value in _catmap_name_map(network)["species"].items()
    }


def _matrix_rank(rows: list[list[float]], columns: int) -> int:
    """Return a deterministic numeric rank for small composition matrices."""
    matrix = [list(map(float, row)) for row in rows]
    rank = 0
    for column in range(columns):
        pivot = next(
            (index for index in range(rank, len(matrix))
             if abs(matrix[index][column]) > 1.0e-12),
            None,
        )
        if pivot is None:
            continue
        matrix[rank], matrix[pivot] = matrix[pivot], matrix[rank]
        divisor = matrix[rank][column]
        matrix[rank] = [value / divisor for value in matrix[rank]]
        for index, row in enumerate(matrix):
            if index == rank:
                continue
            factor = row[column]
            if abs(factor) > 1.0e-12:
                matrix[index] = [
                    value - factor * reference
                    for value, reference in zip(row, matrix[rank])
                ]
        rank += 1
        if rank == len(matrix):
            break
    return rank


def _catmap_gas_contract(
        network: Mapping[str, Any], name_map: Mapping[str, Any]) -> dict[str, Any]:
    """Prove gas participation and an independent CatMAP atomic reservoir."""
    species = {
        str(record["id"]): record for record in network.get("species") or []
    }
    participants = set()
    participating_species = set()
    for step in network.get("elementary_steps") or []:
        for state_name in ("reactants", "transition_state", "products"):
            state = step.get(state_name) or {}
            participating_species.update(str(species_id) for species_id in state)
    for species_id in participating_species:
        if (species.get(species_id) or {}).get("phase") == "gas":
            participants.add(species_id)
    if not participants:
        raise CatmapAdapterError(
            "CatMAP setup requires at least one gas-phase reaction participant")

    positive = {
        species_id for species_id, record in species.items()
        if record.get("phase") == "gas"
        and float((record.get("activity") or {}).get("value", 0.0)) > 0.0
    }
    omitted_positive = sorted(positive - participants)
    if omitted_positive:
        raise CatmapAdapterError(
            "positive-pressure gas reservoirs are absent from every elementary "
            f"reaction: {', '.join(omitted_positive)}")
    feed_gases = {
        str(species_id) for species_id in network.get("feed_species") or []
        if (species.get(str(species_id)) or {}).get("phase") == "gas"
    }
    if not feed_gases <= participants:
        raise CatmapAdapterError(
            "gas-phase feed species must participate in an elementary reaction")

    elements = sorted({
        str(element)
        for species_id in participating_species
        for element in (species.get(species_id) or {}).get("composition") or {}
        if str(element) != "e-"
    })
    if not elements:
        raise CatmapAdapterError(
            "CatMAP gas reservoir has no conserved atomic elements")
    gas_ids = sorted(participants)
    rows = [
        [float((species[species_id].get("composition") or {}).get(element, 0.0))
         for element in elements]
        for species_id in gas_ids
    ]
    rank = _matrix_rank(rows, len(elements))
    if rank != len(elements):
        raise CatmapAdapterError(
            "gas-phase reaction participants cannot form an independent atomic "
            "reference set for CatMAP")
    total_pressure = float(network["standard_state"]["pressure"]["value"])
    concentration_sum = sum(
        float(species[species_id]["activity"]["value"]) / total_pressure
        for species_id in gas_ids
    )
    if not math.isclose(concentration_sum, 1.0, rel_tol=0.0, abs_tol=1.0e-8):
        raise CatmapAdapterError(
            "participating CatMAP gas concentrations must sum to one")
    return {
        "schema": "vcstudio.catmap-gas-contract/v1",
        "canonical_gas_ids": gas_ids,
        "gas_names": [name_map["species"][species_id]["key"] for species_id in gas_ids],
        "positive_pressure_gas_ids": sorted(positive),
        "elements": elements,
        "composition_rank": rank,
        "reference_set_complete": True,
        "all_positive_reservoirs_participate": True,
    }


def _format_number(value: Any) -> str:
    number = float(value)
    if not math.isfinite(number):
        raise CatmapAdapterError("CatMAP export requires finite numbers")
    return format(number, ".15g")


def _format_frequencies(values: Any) -> str:
    return "[" + ", ".join(_format_number(value) for value in values or []) + "]"


def _table_text(network: Mapping[str, Any], name_map: Mapping[str, Any]) -> str:
    header = [
        "surface_name", "site_name", "species_name", "formation_energy",
        "frequencies", "reference",
    ]
    surface_name = str(name_map["surface"])
    rows = ["\t".join(header)]
    for record in network.get("species") or []:
        phase = str(record["phase"])
        if phase == "surface":
            # Empty-site energies are represented by the CatMAP site balance.
            continue
        species_id = str(record["id"])
        mapped = name_map["species"][species_id]
        energy = record["formation_energy"]
        source = energy["source"]
        if phase == "gas":
            table_surface, site_name = "None", "gas"
            species_name = mapped["name"]
        elif phase in {"adsorbate", "transition_state"}:
            table_surface = surface_name
            site_name = mapped["site"]
            species_name = mapped["name"]
        else:
            raise CatmapAdapterError(
                f"CatMAP phase-1 table does not support {phase!r}")
        reference = (
            f"{source['kind']}:{source['reference']}:"
            f"sha256={source['evidence_sha256']}")
        fields = [
            table_surface, site_name, species_name,
            _format_number(energy["value"]),
            _format_frequencies(record.get("frequencies_cm1")), reference,
        ]
        if any("\t" in field or "\r" in field or "\n" in field for field in fields):
            raise CatmapAdapterError("CatMAP table fields must not contain control characters")
        rows.append("\t".join(fields))
    return "\n".join(rows) + "\n"


def _expanded_state(state: Mapping[str, Any], names: Mapping[str, str]) -> str:
    output = []
    for species_id, raw_count in sorted(state.items()):
        count = float(raw_count)
        rounded = round(count)
        if abs(count - rounded) > 1.0e-9 or not 1 <= rounded <= 64:
            raise CatmapAdapterError(
                "CatMAP phase-1 reaction expressions require integer coefficients 1..64")
        output.extend([names[species_id]] * rounded)
    return " + ".join(output)


def _model_text(network: Mapping[str, Any], name_map: Mapping[str, Any],
                gas_contract: Mapping[str, Any], *, scan: bool) -> str:
    policy = network.get("rate_law_policy") or {}
    required_policy = {
        "activity": "ideal",
        "reversibility": "explicit_reverse",
        "detailed_balance": "enforced",
        "prefactor": "explicit_per_step",
        "electrochemical": "none",
        "reactor": "mean_field_steady_state",
    }
    if policy.get("electrochemical") != "none":
        raise CatmapAdapterError(
            "phase-1 CatMAP export does not encode electrochemical potential dependence")
    if policy != required_policy:
        raise CatmapAdapterError(
            "phase-1 CatMAP export requires the exact explicit ideal, reversible, "
            "detailed-balance, per-step-prefactor, non-electrochemical, "
            "mean-field steady-state rate-law policy")
    methodology = network.get("methodology") or {}
    if methodology.get("energy_basis") != "gibbs_free_energy":
        raise CatmapAdapterError(
            "phase-1 CatMAP export requires frozen Gibbs formation energies")
    if methodology.get("potential_model") != "none":
        raise CatmapAdapterError(
            "phase-1 CatMAP export does not encode electrochemical potential dependence")
    reactions = []
    prefactors = []
    names = {
        key: item["key"] for key, item in name_map["species"].items()
    }
    for step in network.get("elementary_steps") or []:
        reactions.append(
            f"{_expanded_state(step['reactants'], names)} <-> "
            f"{_expanded_state(step['transition_state'], names)} -> "
            f"{_expanded_state(step['products'], names)}")
        records = step.get("prefactors") or {}
        forward = records.get("forward") or {}
        reverse = records.get("reverse") or {}
        if (forward.get("unit") != "s^-1" or reverse.get("unit") != "s^-1"):
            raise CatmapAdapterError(
                "phase-1 CatMAP export supports only symmetric s^-1 prefactors")
        forward_value = float(forward.get("value"))
        reverse_value = float(reverse.get("value"))
        if (not math.isfinite(forward_value) or not math.isfinite(reverse_value)
                or not math.isclose(
                    forward_value, reverse_value, rel_tol=1.0e-12, abs_tol=0.0)):
            raise CatmapAdapterError(
                "phase-1 CatMAP export requires equal forward/reverse prefactors")
        prefactors.append(_format_number(forward_value))
    raw_totals = network.get("site_population_totals")
    if not isinstance(raw_totals, list) or not raw_totals:
        raise CatmapAdapterError(
            "phase-1 CatMAP export requires evidence-bound site population totals")
    totals = {}
    for record in raw_totals:
        if not isinstance(record, Mapping):
            raise CatmapAdapterError("site population total must be an object")
        canonical_site = str(record.get("site_type"))
        if canonical_site not in name_map["sites"] or canonical_site in totals:
            raise CatmapAdapterError(
                "site population totals must cover CatMAP site types exactly once")
        if (record.get("unit") not in {"sites", "dimensionless"}
                or record.get("basis") not in {
                    "surface_unit_cell", "normalized_site_population"}):
            raise CatmapAdapterError(
                "CatMAP site population total has an unsupported unit or basis")
        total = float(record.get("value"))
        if not math.isfinite(total) or total <= 0.0:
            raise CatmapAdapterError(
                "CatMAP site population totals must be finite and positive")
        totals[canonical_site] = total
    if set(totals) != set(name_map["sites"]):
        raise CatmapAdapterError(
            "site population totals must cover CatMAP site types exactly once")
    species_definitions = {
        mapped: {"site_names": [mapped], "total": totals[canonical]}
        for canonical, mapped in sorted(name_map["sites"].items())
    }
    for record in network.get("species") or []:
        species_id = str(record["id"])
        phase = record.get("phase")
        mapped = name_map["species"][species_id]
        if phase == "surface":
            continue
        definition = {
            "composition": copy.deepcopy(record.get("composition") or {}),
            "n_sites": 0 if phase == "gas" else 1,
        }
        if phase == "gas":
            total_pressure = float(network["standard_state"]["pressure"]["value"])
            if total_pressure <= 0:
                raise CatmapAdapterError(
                    "pressure descriptor requires positive standard pressure")
            definition["concentration"] = (
                float(record["activity"]["value"]) / total_pressure)
        species_definitions[mapped["key"]] = definition
    temperature = float(network["standard_state"]["temperature"]["value"])
    pressure = float(network["standard_state"]["pressure"]["value"])
    operating = network.get("operating_range") or {}
    if scan:
        descriptor_ranges = [
            list(operating["temperature_K"]),
            list(operating["pressure_bar"]),
        ]
        condition_lines = [
            f"descriptor_ranges = {descriptor_ranges!r}",
            f"resolution = {_SCAN_RESOLUTION!r}",
        ]
    else:
        condition_lines = [f"descriptors = {[temperature, pressure]!r}"]
    lines = [
        "# Independently generated CatMAP setup data; CatMAP is not bundled.",
        "# All formation energies are frozen Gibbs values in eV; no extra thermal correction.",
        "input_file = 'energetics.tsv'",
        f"rxn_expressions = {reactions!r}",
        f"surface_names = [{name_map['surface']!r}]",
        f"gas_names = {list(gas_contract['gas_names'])!r}",
        f"species_definitions = {species_definitions!r}",
        "scaler = 'ThermodynamicScaler'",
        "descriptor_names = ['temperature', 'pressure']",
        *condition_lines,
        f"prefactor_list = {prefactors!r}",
        "gas_thermo_mode = 'frozen_gas'",
        "adsorbate_thermo_mode = 'frozen_adsorbate'",
        "adsorbate_interaction_model = 'ideal'",
        "numerical_representation = 'mpmath'",
        f"output_variables = {_OUTPUT_VARIABLES!r}",
        f"data_file = {'catmap-scan-output.pkl' if scan else 'catmap-output.pkl'!r}",
        "",
    ]
    return "\n".join(lines)


def _descriptor_contract(network: Mapping[str, Any]) -> dict[str, Any]:
    operating = network.get("operating_range") or {}
    standard = network.get("standard_state") or {}
    return {
        "schema": "vcstudio.catmap-descriptor-contract/v1",
        "names": ["temperature", "pressure"],
        "single_point": {
            "descriptors": [
                float(standard["temperature"]["value"]),
                float(standard["pressure"]["value"]),
            ],
        },
        "scan": {
            "descriptor_ranges": [
                list(operating["temperature_K"]),
                list(operating["pressure_bar"]),
            ],
            "resolution": list(_SCAN_RESOLUTION),
            "resolution_policy": "adapter-v2-fixed-diagnostic-grid",
        },
    }


def _process_contract(tool: Mapping[str, Any], input_sha256: str) -> dict[str, Any]:
    return {
        "schema": PROCESS_SCHEMA,
        "adapter": {"id": ADAPTER_ID, "version": ADAPTER_VERSION},
        "external_tool": {
            "name": tool.get("name"),
            "version": tool.get("version"),
            "sha256": tool.get("sha256"),
            "license": CATMAP_LICENSE,
            "project": CATMAP_PROJECT,
            "bundled": False,
            "copied": False,
        },
        "input_sha256": input_sha256,
        "execution_permitted": False,
        "requires_external_user_action": True,
        "auto_install": False,
        "shell": False,
        "accepts_user_arguments": False,
        "argv_template": [
            "<configured-catmap-adapter>", "--setup", "model.mkm",
            "--result", "kinetics-result.json",
        ],
        "expected_result": {
            "name": "kinetics-result.json",
            "schema": kinetics.RESULT_SCHEMA,
            "input_sha256": input_sha256,
            "units": copy.deepcopy(kinetics.RESULT_UNITS),
        },
    }


def _artifact_records(files: Mapping[str, str]) -> list[dict[str, Any]]:
    if len(files) > _MAX_ARTIFACT_COUNT:
        raise CatmapAdapterError("CatMAP export artifact count exceeds the limit")
    records = []
    total = 0
    for name, text in sorted(files.items()):
        if (not _SAFE_ARTIFACT_NAME_RE.fullmatch(name)
                or name == "manifest.json"):
            raise CatmapAdapterError("CatMAP export artifact name is unsafe")
        encoded = text.encode("utf-8")
        size = len(encoded)
        if size > _MAX_ARTIFACT_BYTES:
            raise CatmapAdapterError(
                f"CatMAP export artifact exceeds the size limit: {name}")
        total += size
        if total > _MAX_TOTAL_ARTIFACT_BYTES:
            raise CatmapAdapterError(
                "CatMAP export artifact bytes exceed the total limit")
        records.append({
            "name": name,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "size": size,
        })
    return records


def _bundle_integrity_sha256(files: Mapping[str, str]) -> str:
    if len(files) > _MAX_ARTIFACT_COUNT + 1:
        raise CatmapAdapterError("CatMAP bundle exceeds the entry limit")
    records = []
    total = 0
    for name, text in sorted(files.items()):
        if not _SAFE_ARTIFACT_NAME_RE.fullmatch(name):
            raise CatmapAdapterError("CatMAP bundle artifact name is unsafe")
        encoded = text.encode("utf-8")
        maximum = _MAX_MANIFEST_BYTES if name == "manifest.json" else _MAX_ARTIFACT_BYTES
        if len(encoded) > maximum:
            raise CatmapAdapterError("CatMAP bundle artifact exceeds the size limit")
        total += len(encoded)
        if total > _MAX_TOTAL_ARTIFACT_BYTES + _MAX_MANIFEST_BYTES:
            raise CatmapAdapterError("CatMAP bundle exceeds the total size limit")
        records.append({
            "name": name,
            "size": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        })
    return hashlib.sha256(_canonical_bytes(records)).hexdigest()


def build_export_bundle(network_source, *, tool_path=None,
                        tool_version: str | None = None) -> dict[str, Any]:
    """Build an in-memory frozen bundle.  This function never executes a tool."""
    try:
        canonical = _source_mapping(network_source)
    except CatmapAdapterError:
        # A missing/invalid exact model-spec or evidence snapshot is still a
        # useful diagnostic audit, but can never produce CatMAP model files.
        canonical = None
        network = {}
        audit = kinetics.audit_network(network_source)
    else:
        network = canonical.to_mapping()
        audit = kinetics.audit_network(canonical)
    tool = inspect_tool_path(tool_path, version=tool_version)
    files = {"kinetics-audit.json": _json_text(audit)}
    adapter_issues = []
    if audit["export_ready"] is True:
        try:
            name_map = _catmap_name_map(network)
            gas_contract = _catmap_gas_contract(network, name_map)
            files.update({
                "kinetics-input.json": _json_text(network),
                "catmap-name-map.json": _json_text(name_map),
                "catmap-gas-contract.json": _json_text(gas_contract),
                "catmap-descriptors.json": _json_text(
                    _descriptor_contract(network)),
                "energetics.tsv": _table_text(network, name_map),
                "model.mkm": _model_text(
                    network, name_map, gas_contract, scan=False),
                "model-scan.mkm": _model_text(
                    network, name_map, gas_contract, scan=True),
                "process-contract.json": _json_text(
                    _process_contract(tool, audit["input_sha256"])),
            })
        except CatmapAdapterError as exc:
            adapter_issues.append({
                "severity": "error",
                "code": "CATMAP_PHASE1_CONTRACT_UNSUPPORTED",
                "message": str(exc),
            })
            files["catmap-adapter-audit.json"] = _json_text({
                "schema": "vcstudio.catmap-adapter-audit/v1",
                "input_sha256": audit.get("input_sha256"),
                "export_ready": False,
                "issues": adapter_issues,
            })
    contract_ready = audit["export_ready"] is True and not adapter_issues
    tool_ready = (
        tool["available"] is True
        and isinstance(tool.get("version"), str)
        and bool(tool["version"])
    )
    export_ready = contract_ready and tool_ready
    artifacts = _artifact_records(files)
    token_payload = {
        "schema": BUNDLE_SCHEMA,
        "input_sha256": audit.get("input_sha256"),
        "adapter": {"id": ADAPTER_ID, "version": ADAPTER_VERSION},
        "tool": tool,
        "artifacts": artifacts,
    }
    preview_token = hashlib.sha256(_canonical_bytes(token_payload)).hexdigest()
    if export_ready:
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "input_sha256": audit["input_sha256"],
            "source_projection_sha256": audit["source_projection_sha256"],
            "adapter": {"id": ADAPTER_ID, "version": ADAPTER_VERSION},
            "external_tool": tool,
            "catmap_license": CATMAP_LICENSE,
            "catmap_bundled": False,
            "catmap_copied": False,
            "preview_token": preview_token,
            "artifacts": artifacts,
            "scientific_status": "diagnostic",
            "eligible_final": False,
        }
        files["manifest.json"] = _json_text(manifest)
    return {
        "schema": BUNDLE_SCHEMA,
        "input_sha256": audit.get("input_sha256"),
        "adapter": {"id": ADAPTER_ID, "version": ADAPTER_VERSION},
        "tool": tool,
        "audit": audit,
        "adapter_issues": adapter_issues,
        "contract_ready": contract_ready,
        "export_ready": export_ready,
        "files": files,
        "artifacts": artifacts,
        "preview_token": preview_token,
    }


def preview_export(network_source, *, tool_path=None,
                   tool_version: str | None = None) -> dict[str, Any]:
    """Return hashes and audit only; do not expose file contents or local paths."""
    bundle = build_export_bundle(
        network_source, tool_path=tool_path, tool_version=tool_version)
    export_ready = bundle["export_ready"] is True
    return {
        "schema": PREVIEW_SCHEMA,
        "input_sha256": bundle["input_sha256"],
        "adapter": copy.deepcopy(bundle["adapter"]),
        "tool": copy.deepcopy(bundle["tool"]),
        "audit": copy.deepcopy(bundle["audit"]),
        "adapter_issues": copy.deepcopy(bundle["adapter_issues"]),
        "artifacts": copy.deepcopy(bundle["artifacts"]),
        "preview_token": bundle["preview_token"] if export_ready else None,
        "export_kind": "model",
        "model_published": False,
        "explicit_confirmation_required": True,
        "capability_status": (
            "available" if export_ready else "unavailable"),
        "export_ready": export_ready,
        "scientific_status": "diagnostic" if export_ready else "unavailable",
        "eligible_final": False,
        "limitations": {
            "catmap_bundled": False,
            "catmap_auto_installed": False,
            "adapter_executes": False,
            "audit_report_available": True,
        },
    }


def _atomic_write(path: Path, content: str, *, replace: bool = False,
                  boundary: _ProjectBoundary | None = None) -> None:
    encoded = content.encode("utf-8")
    if boundary is not None:
        boundary.verify()
    exists = os.path.lexists(path)
    if exists and not replace:
        metadata = _lstat_regular(path, label=f"export file {path.name}")
        data, _, _ = _read_bounded_regular_file(
            path, maximum=max(len(encoded), 1),
            label=f"export file {path.name}", expected=metadata, collect=True)
        if data == encoded:
            return
        raise CatmapAdapterError(f"refusing to overwrite changed export file {path.name}")
    if exists:
        _lstat_regular(path, label=f"export file {path.name}")
    fd, temp_name = tempfile.mkstemp(prefix=".vcs-kinetics-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if boundary is not None:
            boundary.verify()
        os.replace(temp_name, path)
        _fsync_directory(path.parent)
        if boundary is not None:
            boundary.verify()
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


@contextmanager
def _advisory_lock(path: Path, boundary: _ProjectBoundary):
    boundary.verify()
    if _is_link(path):
        raise CatmapAdapterError(
            "export selection lock must not be a symlink or reparse point")
    flags = os.O_RDWR | os.O_CREAT | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_CLOEXEC", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise CatmapAdapterError("export selection lock is unavailable") from exc
    with os.fdopen(descriptor, "a+b") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > 4096:
            raise CatmapAdapterError("export selection lock is unsafe")
        try:
            current = os.lstat(path)
        except OSError as exc:
            raise CatmapAdapterError("export selection lock changed") from exc
        if (_entity_identity(opened) != _entity_identity(current)
                or _is_reparse(current)):
            raise CatmapAdapterError("export selection lock changed")
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.read(1) == b"":
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                boundary.verify()
                yield
                boundary.verify()
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                boundary.verify()
                yield
                boundary.verify()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    boundary.verify()


def _safe_child_directory(parent: Path, name: str, root: Path,
                          boundary: _ProjectBoundary | None = None) -> Path:
    binding = boundary or _ProjectBoundary(root)
    return binding.child(parent, name)


def _project_root(project_root) -> Path:
    return _ProjectBoundary(project_root).root


def _read_json(path: Path, *, maximum: int) -> Any:
    try:
        data, _, _ = _read_bounded_regular_file(
            path, maximum=maximum, label=f"export file {path.name}", collect=True)
        assert data is not None
        return json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CatmapAdapterError(f"export file {path.name} is invalid") from exc


def _selection_default(input_sha256: str) -> dict[str, Any]:
    return {
        "schema": EXPORT_SELECTION_SCHEMA, "input_sha256": input_sha256,
        "revision": 0, "selected_export_sha256": None,
    }


def _validated_selection(value: Any, input_sha256: str) -> dict[str, Any]:
    if (not isinstance(value, Mapping)
            or set(value) != {
                "schema", "input_sha256", "revision", "selected_export_sha256"}
            or value.get("schema") != EXPORT_SELECTION_SCHEMA
            or value.get("input_sha256") != input_sha256
            or isinstance(value.get("revision"), bool)
            or not isinstance(value.get("revision"), int)
            or value["revision"] < 1
            or not isinstance(value.get("selected_export_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["selected_export_sha256"])):
        raise CatmapAdapterError("confirmed CatMAP export selection is invalid")
    return dict(value)


def _read_selection(input_dir: Path, input_sha256: str) -> dict[str, Any]:
    path = input_dir / "current.json"
    try:
        path.lstat()
    except FileNotFoundError:
        return _selection_default(input_sha256)
    except OSError as exc:
        raise CatmapAdapterError("confirmed CatMAP export selection is invalid") from exc
    return _validated_selection(_read_json(path, maximum=4096), input_sha256)


_SELECTION_ANCHOR_DESCRIPTOR_FIELDS = frozenset({
    "input_sha256", "revision", "selected_export_sha256",
    "selection_sha256", "selection", "chain_sha256",
})
_SELECTION_ANCHOR_PREPARE_FIELDS = frozenset({
    "schema", "kind", "sequence", "previous_record_sha256",
    "transaction_id", "base", "target", "reservation_sha256",
    "record_sha256",
})
_SELECTION_ANCHOR_COMMIT_FIELDS = frozenset({
    "schema", "kind", "sequence", "previous_record_sha256",
    "transaction_id", "prepare_record_sha256", "target",
    "reservation_sha256", "record_sha256",
})


def _selection_sha256(selection: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(selection)).hexdigest()


def _selection_anchor_record_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes({
        key: item for key, item in value.items() if key != "record_sha256"
    })).hexdigest()


def _seal_selection_anchor_record(value: Mapping[str, Any]) -> dict[str, Any]:
    record = dict(value)
    record["record_sha256"] = _selection_anchor_record_sha256(record)
    return record


def _selection_anchor_chain_sha256(
        selection: Mapping[str, Any], previous_chain_sha256: str) -> str:
    return hashlib.sha256(_canonical_bytes({
        "previous_chain_sha256": previous_chain_sha256,
        "input_sha256": selection["input_sha256"],
        "revision": selection["revision"],
        "selection_sha256": _selection_sha256(selection),
    })).hexdigest()


def _selection_anchor_descriptor(
        selection: Mapping[str, Any], previous_chain_sha256: str) -> dict[str, Any]:
    normalized = _validated_selection(selection, str(selection.get("input_sha256")))
    return {
        "input_sha256": normalized["input_sha256"],
        "revision": normalized["revision"],
        "selected_export_sha256": normalized["selected_export_sha256"],
        "selection_sha256": _selection_sha256(normalized),
        "selection": normalized,
        "chain_sha256": _selection_anchor_chain_sha256(
            normalized, previous_chain_sha256),
    }


def _validated_selection_anchor_descriptor(
        value: Any, input_sha256: str) -> dict[str, Any]:
    if (not isinstance(value, Mapping)
            or set(value) != set(_SELECTION_ANCHOR_DESCRIPTOR_FIELDS)):
        raise CatmapAdapterError("CatMAP selection anchor descriptor is invalid")
    selection = _validated_selection(value.get("selection"), input_sha256)
    selection_sha = _selection_sha256(selection)
    if (value.get("input_sha256") != input_sha256
            or value.get("revision") != selection["revision"]
            or value.get("selected_export_sha256")
            != selection["selected_export_sha256"]
            or value.get("selection_sha256") != selection_sha
            or not isinstance(value.get("chain_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["chain_sha256"])):
        raise CatmapAdapterError("CatMAP selection anchor descriptor is inconsistent")
    return {
        "input_sha256": input_sha256,
        "revision": selection["revision"],
        "selected_export_sha256": selection["selected_export_sha256"],
        "selection_sha256": selection_sha,
        "selection": selection,
        "chain_sha256": value["chain_sha256"],
    }


@dataclass(frozen=True)
class _SelectionAnchorState:
    records: tuple[Mapping[str, Any], ...]
    committed: Mapping[str, Any] | None
    pending: Mapping[str, Any] | None
    last_record_sha256: str
    committed_transaction_id: str | None = None
    committed_reservation_sha256: str | None = None


def _validated_selection_anchor_log(
        payload: bytes, input_sha256: str) -> _SelectionAnchorState:
    if payload and not payload.endswith(b"\n"):
        raise CatmapAdapterError("CatMAP selection anchor WAL has an incomplete tail")
    raw_lines = payload.splitlines()
    if len(raw_lines) > _MAX_SELECTION_ANCHOR_RECORDS:
        raise CatmapAdapterError("CatMAP selection anchor WAL exceeds its record limit")
    records = []
    committed = None
    pending = None
    previous_record_sha = _ZERO_SHA256
    committed_transaction_id = None
    committed_reservation_sha = None
    for sequence, raw_line in enumerate(raw_lines, start=1):
        try:
            raw = json.loads(raw_line.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise CatmapAdapterError("CatMAP selection anchor WAL is invalid") from exc
        kind = raw.get("kind") if isinstance(raw, Mapping) else None
        fields = (
            _SELECTION_ANCHOR_PREPARE_FIELDS if kind == "prepare"
            else _SELECTION_ANCHOR_COMMIT_FIELDS if kind == "commit"
            else frozenset())
        if not fields or not isinstance(raw, Mapping) or set(raw) != set(fields):
            raise CatmapAdapterError("CatMAP selection anchor WAL record is invalid")
        transaction_id = raw.get("transaction_id")
        reservation_sha = raw.get("reservation_sha256")
        if (raw.get("schema") != EXPORT_SELECTION_ANCHOR_SCHEMA
                or raw.get("sequence") != sequence
                or raw.get("previous_record_sha256") != previous_record_sha
                or not isinstance(transaction_id, str)
                or not re.fullmatch(r"[0-9a-f]{32}", transaction_id)
                or not isinstance(reservation_sha, str)
                or not re.fullmatch(r"[0-9a-f]{64}", reservation_sha)):
            raise CatmapAdapterError("CatMAP selection anchor WAL chain is invalid")
        declared_record_sha = raw.get("record_sha256")
        if (not isinstance(declared_record_sha, str)
                or not re.fullmatch(r"[0-9a-f]{64}", declared_record_sha)
                or _selection_anchor_record_sha256(raw) != declared_record_sha):
            raise CatmapAdapterError("CatMAP selection anchor WAL seal is invalid")
        target = _validated_selection_anchor_descriptor(
            raw.get("target"), input_sha256)
        if kind == "prepare":
            if pending is not None:
                raise CatmapAdapterError(
                    "CatMAP selection anchor contains nested transactions")
            if committed is None:
                if raw.get("base") is not None or target["revision"] != 1:
                    raise CatmapAdapterError(
                        "CatMAP selection anchor initialization is invalid")
                previous_chain = _ZERO_SHA256
            else:
                base = _validated_selection_anchor_descriptor(
                    raw.get("base"), input_sha256)
                if (base != committed
                        or target["revision"] != committed["revision"] + 1):
                    raise CatmapAdapterError(
                        "CatMAP selection anchor transition is invalid")
                previous_chain = committed["chain_sha256"]
            if target["chain_sha256"] != _selection_anchor_chain_sha256(
                    target["selection"], previous_chain):
                raise CatmapAdapterError(
                    "CatMAP selection anchor transition seal is invalid")
            pending = {
                "record": dict(raw),
                "transaction_id": transaction_id,
                "reservation_sha256": reservation_sha,
                "base": None if committed is None else dict(committed),
                "target": target,
            }
        else:
            if (pending is None
                    or raw.get("prepare_record_sha256")
                    != pending["record"]["record_sha256"]
                    or transaction_id != pending["transaction_id"]
                    or reservation_sha != pending["reservation_sha256"]
                    or target != pending["target"]):
                raise CatmapAdapterError("CatMAP selection anchor commit is invalid")
            committed = target
            committed_transaction_id = transaction_id
            committed_reservation_sha = reservation_sha
            pending = None
        normalized = dict(raw)
        normalized["target"] = target
        if normalized.get("base") is not None:
            normalized["base"] = _validated_selection_anchor_descriptor(
                normalized["base"], input_sha256)
        records.append(normalized)
        previous_record_sha = declared_record_sha
    return _SelectionAnchorState(
        records=tuple(records), committed=committed, pending=pending,
        last_record_sha256=previous_record_sha,
        committed_transaction_id=committed_transaction_id,
        committed_reservation_sha256=committed_reservation_sha,
    )


def _truncate_selection_anchor_tail(
        path: Path, length: int, boundary: _ProjectBoundary) -> None:
    boundary.verify()
    before = _lstat_regular(path, label="CatMAP selection anchor WAL")
    if not 0 <= length <= before.st_size:
        raise CatmapAdapterError("CatMAP selection anchor WAL is unsafe")
    flags = os.O_RDWR
    for flag_name in ("O_BINARY", "O_CLOEXEC", "O_NOFOLLOW"):
        flags |= getattr(os, flag_name, 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CatmapAdapterError("CatMAP selection anchor WAL is unavailable") from exc
    try:
        opened = os.fstat(descriptor)
        if (_entity_identity(opened) != _entity_identity(before)
                or int(opened.st_size) != int(before.st_size)):
            raise CatmapAdapterError("CatMAP selection anchor WAL changed")
        os.ftruncate(descriptor, length)
        os.fsync(descriptor)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise CatmapAdapterError(
            "CatMAP selection anchor WAL could not be recovered") from exc
    finally:
        os.close(descriptor)
    current = _lstat_regular(path, label="CatMAP selection anchor WAL")
    if (_entity_identity(after) != _entity_identity(current)
            or int(current.st_size) != length):
        raise CatmapAdapterError("CatMAP selection anchor WAL changed")
    boundary.verify()


class _SelectionAnchorFile:
    def __init__(self, input_dir: Path, input_sha256: str,
                 boundary: _ProjectBoundary):
        self.path = input_dir / _SELECTION_ANCHOR_FILENAME
        self.input_sha256 = input_sha256
        self.boundary = boundary
        self._identity: tuple[int, int, int] | None = None

    def _verify_entity(self) -> None:
        if self._identity is None:
            return
        metadata = _lstat_regular(
            self.path, label="CatMAP selection anchor WAL")
        if _entity_identity(metadata) != self._identity:
            raise CatmapAdapterError("CatMAP selection anchor WAL entity changed")

    def _pin_entity(self) -> None:
        if not os.path.lexists(self.path):
            return
        metadata = _lstat_regular(
            self.path, label="CatMAP selection anchor WAL")
        identity = _entity_identity(metadata)
        if self._identity is None:
            self._identity = identity
        elif self._identity != identity:
            raise CatmapAdapterError("CatMAP selection anchor WAL entity changed")

    def read(self) -> _SelectionAnchorState:
        self._verify_entity()
        if not os.path.lexists(self.path):
            return _SelectionAnchorState((), None, None, _ZERO_SHA256)
        payload, _size, _sha = _read_bounded_regular_file(
            self.path, maximum=_MAX_SELECTION_ANCHOR_BYTES,
            label="CatMAP selection anchor WAL", collect=True)
        assert payload is not None
        self._pin_entity()
        self._verify_entity()
        if payload and not payload.endswith(b"\n"):
            complete_length = payload.rfind(b"\n") + 1
            complete = payload[:complete_length]
            state = _validated_selection_anchor_log(
                complete, self.input_sha256)
            _truncate_selection_anchor_tail(
                self.path, complete_length, self.boundary)
            return state
        return _validated_selection_anchor_log(payload, self.input_sha256)

    def append(self, record: Mapping[str, Any]) -> None:
        payload = _canonical_bytes(record) + b"\n"
        self.boundary.verify()
        self._verify_entity()
        try:
            before = os.lstat(self.path)
        except FileNotFoundError:
            before = None
        except OSError as exc:
            raise CatmapAdapterError(
                "CatMAP selection anchor WAL is unavailable") from exc
        if before is not None and (stat.S_ISLNK(before.st_mode)
                                   or _is_reparse(before)
                                   or not stat.S_ISREG(before.st_mode)):
            raise CatmapAdapterError("CatMAP selection anchor WAL is unsafe")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        for flag_name in ("O_BINARY", "O_CLOEXEC", "O_NOFOLLOW"):
            flags |= getattr(os, flag_name, 0)
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise CatmapAdapterError(
                "CatMAP selection anchor WAL is unavailable") from exc
        try:
            opened = os.fstat(descriptor)
            if (not stat.S_ISREG(opened.st_mode)
                    or (before is not None
                        and _entity_identity(opened) != _entity_identity(before))
                    or opened.st_size + len(payload) > _MAX_SELECTION_ANCHOR_BYTES):
                raise CatmapAdapterError("CatMAP selection anchor WAL is unsafe")
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise CatmapAdapterError(
                        "CatMAP selection anchor WAL append made no progress")
                offset += written
            os.fsync(descriptor)
            after = os.fstat(descriptor)
        except OSError as exc:
            raise CatmapAdapterError(
                "CatMAP selection anchor WAL append failed") from exc
        finally:
            os.close(descriptor)
        current = _lstat_regular(
            self.path, label="CatMAP selection anchor WAL")
        if (_entity_identity(after) != _entity_identity(current)
                or int(after.st_size) != int(current.st_size)):
            raise CatmapAdapterError("CatMAP selection anchor WAL changed")
        if before is None:
            _fsync_directory(self.path.parent)
        self._pin_entity()
        self._verify_entity()
        self.boundary.verify()


def _selection_anchor_prepare_record(
        state: _SelectionAnchorState, selection: Mapping[str, Any],
        reservation: Mapping[str, Any]) -> dict[str, Any]:
    if len(state.records) >= _MAX_SELECTION_ANCHOR_RECORDS:
        raise CatmapAdapterError("CatMAP selection anchor WAL exceeds its record limit")
    previous_chain = (
        _ZERO_SHA256 if state.committed is None
        else str(state.committed["chain_sha256"]))
    return _seal_selection_anchor_record({
        "schema": EXPORT_SELECTION_ANCHOR_SCHEMA,
        "kind": "prepare",
        "sequence": len(state.records) + 1,
        "previous_record_sha256": state.last_record_sha256,
        "transaction_id": reservation["transaction_id"],
        "base": None if state.committed is None else dict(state.committed),
        "target": _selection_anchor_descriptor(selection, previous_chain),
        "reservation_sha256": hashlib.sha256(
            _canonical_bytes(reservation)).hexdigest(),
    })


def _selection_anchor_commit_record(
        state: _SelectionAnchorState, prepare: Mapping[str, Any]) -> dict[str, Any]:
    if len(state.records) >= _MAX_SELECTION_ANCHOR_RECORDS:
        raise CatmapAdapterError("CatMAP selection anchor WAL exceeds its record limit")
    return _seal_selection_anchor_record({
        "schema": EXPORT_SELECTION_ANCHOR_SCHEMA,
        "kind": "commit",
        "sequence": len(state.records) + 1,
        "previous_record_sha256": state.last_record_sha256,
        "transaction_id": prepare["transaction_id"],
        "prepare_record_sha256": prepare["record_sha256"],
        "target": dict(prepare["target"]),
        "reservation_sha256": prepare["reservation_sha256"],
    })


def _read_reservation(input_dir: Path, input_sha256: str) -> dict[str, Any] | None:
    path = input_dir / _RESERVATION_FILENAME
    if not os.path.lexists(path):
        return None
    value = _read_json(path, maximum=8192)
    if (not isinstance(value, Mapping)
            or set(value) != {
                "schema", "input_sha256", "preview_token",
                "expected_selection_revision", "expected_selected_export_sha256",
                "transaction_id", "created_by_transaction", "stage_name",
                "bundle_integrity_sha256"}
            or value.get("schema") != EXPORT_RESERVATION_SCHEMA
            or value.get("input_sha256") != input_sha256
            or not isinstance(value.get("preview_token"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["preview_token"])
            or isinstance(value.get("expected_selection_revision"), bool)
            or not isinstance(value.get("expected_selection_revision"), int)
            or value["expected_selection_revision"] < 0
            or (value.get("expected_selected_export_sha256") is not None
                and (not isinstance(value["expected_selected_export_sha256"], str)
                     or not re.fullmatch(
                         r"[0-9a-f]{64}",
                         value["expected_selected_export_sha256"])))
            or not isinstance(value.get("transaction_id"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", value["transaction_id"])
            or not isinstance(value.get("created_by_transaction"), bool)
            or (value.get("stage_name") is not None
                and (not isinstance(value["stage_name"], str)
                     or not re.fullmatch(
                         r"\.vcs-kinetics-stage-[0-9a-f]{32}",
                         value["stage_name"])))
            or ((value.get("stage_name") is None)
                != (value.get("created_by_transaction") is False))
            or not isinstance(value.get("bundle_integrity_sha256"), str)
            or not re.fullmatch(
                r"[0-9a-f]{64}", value["bundle_integrity_sha256"])):
        raise CatmapAdapterError("CatMAP export reservation is invalid")
    return dict(value)


def _unlink_regular(path: Path, *, label: str,
                    boundary: _ProjectBoundary) -> None:
    if not os.path.lexists(path):
        return
    _lstat_regular(path, label=label)
    boundary.verify()
    try:
        os.unlink(path)
    except OSError as exc:
        raise CatmapAdapterError(f"{label} could not be removed") from exc
    _fsync_directory(path.parent)
    boundary.verify()


def _remove_safe_bundle_directory(
        path: Path, input_dir: Path, boundary: _ProjectBoundary) -> None:
    if not os.path.lexists(path):
        return
    boundary.pin_directory(path, label="CatMAP residual bundle")
    if (Path(os.path.abspath(path)).parent != input_dir
            or os.path.normcase(os.path.abspath(path))
            != os.path.normcase(os.path.realpath(path))):
        raise CatmapAdapterError("CatMAP residual bundle escaped its input directory")
    entries = _bounded_scandir(
        path, maximum=_MAX_ARTIFACT_COUNT + 1,
        label="CatMAP residual bundle")
    for entry in entries:
        entry_path = path / entry.name
        entry_metadata = os.lstat(entry_path)
        if (stat.S_ISLNK(entry_metadata.st_mode) or _is_reparse(entry_metadata)
                or not stat.S_ISREG(entry_metadata.st_mode)):
            raise CatmapAdapterError("CatMAP residual bundle contains unsafe entries")
    boundary.verify()
    for entry in entries:
        os.unlink(path / entry.name)
    boundary.unpin(path)
    try:
        os.rmdir(path)
    except OSError as exc:
        raise CatmapAdapterError("CatMAP residual bundle could not be removed") from exc
    _fsync_directory(input_dir)
    boundary.verify()


def _bundle_directory_integrity_sha256(
        path: Path, input_dir: Path, boundary: _ProjectBoundary) -> str:
    boundary.pin_directory(path, label="CatMAP reserved bundle")
    if (Path(os.path.abspath(path)).parent != input_dir
            or os.path.normcase(os.path.abspath(path))
            != os.path.normcase(os.path.realpath(path))):
        raise CatmapAdapterError("CatMAP reserved bundle escaped its input directory")
    entries = sorted(
        _bounded_scandir(
            path, maximum=_MAX_ARTIFACT_COUNT + 1,
            label="CatMAP reserved bundle"),
        key=lambda entry: entry.name,
    )
    records = []
    total = 0
    for entry in entries:
        if not _SAFE_ARTIFACT_NAME_RE.fullmatch(entry.name):
            raise CatmapAdapterError("CatMAP reserved bundle contains unsafe entries")
        maximum = (
            _MAX_MANIFEST_BYTES
            if entry.name == "manifest.json" else _MAX_ARTIFACT_BYTES)
        _data, size, digest = _read_bounded_regular_file(
            path / entry.name, maximum=maximum,
            label=f"reserved CatMAP bundle file {entry.name}")
        total += size
        if total > _MAX_TOTAL_ARTIFACT_BYTES + _MAX_MANIFEST_BYTES:
            raise CatmapAdapterError("CatMAP reserved bundle exceeds the total limit")
        records.append({"name": entry.name, "size": size, "sha256": digest})
        boundary.verify()
    return hashlib.sha256(_canonical_bytes(records)).hexdigest()


def _selection_matches_descriptor(
        current: Mapping[str, Any], descriptor: Mapping[str, Any] | None) -> bool:
    if descriptor is None:
        return dict(current) == _selection_default(str(current["input_sha256"]))
    return (
        dict(current) == dict(descriptor["selection"])
        and _selection_sha256(current) == descriptor["selection_sha256"])


def _reservation_sha256(reservation: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(reservation)).hexdigest()


def _validate_reservation_for_transition(
        reservation: Mapping[str, Any], base: Mapping[str, Any] | None,
        target: Mapping[str, Any], input_dir: Path,
        boundary: _ProjectBoundary) -> None:
    base_selection = (
        _selection_default(str(target["input_sha256"]))
        if base is None else base["selection"])
    target_selection = target["selection"]
    if (reservation["input_sha256"] != target["input_sha256"]
            or reservation["expected_selection_revision"]
            != base_selection["revision"]
            or reservation["expected_selected_export_sha256"]
            != base_selection["selected_export_sha256"]
            or reservation["preview_token"]
            != target_selection["selected_export_sha256"]
            or target_selection["revision"] != base_selection["revision"] + 1):
        raise CatmapAdapterError(
            "CatMAP export reservation does not match its selection transition")
    stage_name = reservation["stage_name"]
    if stage_name is not None and os.path.lexists(input_dir / stage_name):
        raise CatmapAdapterError(
            "prepared CatMAP selection still has a staged bundle")
    final_dir = input_dir / reservation["preview_token"]
    if not os.path.lexists(final_dir):
        raise CatmapAdapterError(
            "prepared CatMAP selection bundle is unavailable")
    actual_integrity = _bundle_directory_integrity_sha256(
        final_dir, input_dir, boundary)
    if actual_integrity != reservation["bundle_integrity_sha256"]:
        raise CatmapAdapterError(
            "prepared CatMAP selection bundle changed")


def _discard_unprepared_reservation(
        input_dir: Path, current: Mapping[str, Any],
        reservation: Mapping[str, Any], boundary: _ProjectBoundary) -> None:
    if (reservation["expected_selection_revision"] != current["revision"]
            or reservation["expected_selected_export_sha256"]
            != current["selected_export_sha256"]):
        raise CatmapAdapterError(
            "unprepared CatMAP export reservation is not based on current selection")
    token = reservation["preview_token"]
    stage_name = reservation["stage_name"]
    if stage_name is not None and os.path.lexists(input_dir / stage_name):
        _remove_safe_bundle_directory(
            input_dir / stage_name, input_dir, boundary)
    final_dir = input_dir / token
    if (reservation["created_by_transaction"]
            and current.get("selected_export_sha256") != token
            and os.path.lexists(final_dir)):
        actual_integrity = _bundle_directory_integrity_sha256(
            final_dir, input_dir, boundary)
        if actual_integrity != reservation["bundle_integrity_sha256"]:
            raise CatmapAdapterError(
                "reserved CatMAP bundle changed; refusing recovery deletion")
        _remove_safe_bundle_directory(final_dir, input_dir, boundary)
    _unlink_regular(
        input_dir / _RESERVATION_FILENAME,
        label="CatMAP export reservation", boundary=boundary)


def _read_consistent_selection(
        input_dir: Path, input_sha256: str,
        boundary: _ProjectBoundary) -> dict[str, Any]:
    anchor = _SelectionAnchorFile(input_dir, input_sha256, boundary)
    state = anchor.read()
    current = _read_selection(input_dir, input_sha256)
    if state.pending is not None:
        raise CatmapAdapterError("CatMAP selection anchor transition is pending")
    if state.committed is None:
        if current["revision"] != 0:
            raise CatmapAdapterError(
                "CatMAP selection exists without its independent anchor")
    elif not _selection_matches_descriptor(current, state.committed):
        raise CatmapAdapterError(
            "CatMAP selection does not match its independent anchor")
    boundary.verify()
    return current


def _recover_selection_axis(
        input_dir: Path, input_sha256: str, boundary: _ProjectBoundary,
        anchor: _SelectionAnchorFile,
) -> tuple[dict[str, Any], _SelectionAnchorState]:
    state = anchor.read()
    current = _read_selection(input_dir, input_sha256)
    reservation = _read_reservation(input_dir, input_sha256)
    if state.pending is not None:
        pending = state.pending
        if (reservation is None
                or reservation["transaction_id"] != pending["transaction_id"]
                or _reservation_sha256(reservation)
                != pending["reservation_sha256"]):
            raise CatmapAdapterError(
                "prepared CatMAP selection reservation is unavailable or changed")
        _validate_reservation_for_transition(
            reservation, pending["base"], pending["target"],
            input_dir, boundary)
        if _selection_matches_descriptor(current, pending["base"]):
            _atomic_write(
                input_dir / "current.json",
                _json_text(pending["target"]["selection"]),
                replace=True, boundary=boundary)
            current = _read_selection(input_dir, input_sha256)
        if not _selection_matches_descriptor(current, pending["target"]):
            raise CatmapAdapterError(
                "CatMAP selection is neither the exact prepared base nor target")
        commit = _selection_anchor_commit_record(state, pending["record"])
        anchor.append(commit)
        state = anchor.read()
        if (state.pending is not None
                or state.committed != pending["target"]):
            raise CatmapAdapterError(
                "CatMAP selection forward recovery did not commit exactly")
        _unlink_regular(
            input_dir / _RESERVATION_FILENAME,
            label="CatMAP export reservation", boundary=boundary)
        reservation = None
    if state.committed is None:
        if current["revision"] != 0:
            raise CatmapAdapterError(
                "CatMAP selection exists without its independent anchor")
    elif not _selection_matches_descriptor(current, state.committed):
        raise CatmapAdapterError(
            "CatMAP selection does not match its independent anchor")
    if reservation is not None:
        if (state.committed_transaction_id == reservation["transaction_id"]
                and state.committed_reservation_sha256
                == _reservation_sha256(reservation)):
            if len(state.records) < 2:
                raise CatmapAdapterError(
                    "CatMAP selection anchor commit has no prepare")
            prepare = state.records[-2]
            _validate_reservation_for_transition(
                reservation, prepare.get("base"), state.committed,
                input_dir, boundary)
            _unlink_regular(
                input_dir / _RESERVATION_FILENAME,
                label="CatMAP export reservation", boundary=boundary)
        else:
            _discard_unprepared_reservation(
                input_dir, current, reservation, boundary)
    return current, state


def _bundle_directory_size(
        path: Path, input_dir: Path, boundary: _ProjectBoundary) -> int:
    boundary.pin_directory(path, label="CatMAP retained bundle")
    _lstat_directory(path, label="CatMAP retained bundle")
    if (Path(os.path.abspath(path)).parent != input_dir
            or os.path.normcase(os.path.abspath(path))
            != os.path.normcase(os.path.realpath(path))):
        raise CatmapAdapterError("CatMAP retained bundle escaped its input directory")
    total = 0
    entries = _bounded_scandir(
        path, maximum=_MAX_ARTIFACT_COUNT + 1,
        label="CatMAP retained bundle")
    for entry in entries:
        boundary.verify()
        metadata = os.lstat(path / entry.name)
        if (stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata)
                or not stat.S_ISREG(metadata.st_mode)):
            raise CatmapAdapterError("CatMAP retained bundle contains unsafe entries")
        total += int(metadata.st_size)
        if total > _MAX_INPUT_BUNDLE_BYTES:
            raise CatmapAdapterError("CatMAP retained bundles exceed the input byte quota")
    boundary.verify()
    return total


def _input_bundle_usage(
        input_dir: Path, boundary: _ProjectBoundary) -> tuple[int, int]:
    boundary.verify()
    count = 0
    total = 0
    allowed_files = {
        "current.json", ".selection.lock", _RESERVATION_FILENAME,
        _SELECTION_ANCHOR_FILENAME,
    }
    entries = _bounded_scandir(
        input_dir,
        maximum=_MAX_BUNDLES_PER_INPUT + len(allowed_files),
        label="CatMAP input directory",
    )
    for entry in entries:
        path = input_dir / entry.name
        metadata = os.lstat(path)
        if re.fullmatch(r"[0-9a-f]{64}", entry.name):
            if (stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata)
                    or not stat.S_ISDIR(metadata.st_mode)):
                raise CatmapAdapterError("CatMAP retained bundle is unsafe")
            count += 1
            total += _bundle_directory_size(path, input_dir, boundary)
        elif entry.name in allowed_files:
            if (stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata)
                    or not stat.S_ISREG(metadata.st_mode)):
                raise CatmapAdapterError("CatMAP input metadata is unsafe")
        else:
            raise CatmapAdapterError("CatMAP input directory contains unknown entries")
        if count > _MAX_BUNDLES_PER_INPUT or total > _MAX_INPUT_BUNDLE_BYTES:
            raise CatmapAdapterError("CatMAP retained bundles exceed the input quota")
    return count, total


def _verify_existing_export(
        final_dir: Path, input_dir: Path, files: Mapping[str, str],
        boundary: _ProjectBoundary) -> Path:
    boundary.pin_directory(final_dir, label="existing CatMAP export")
    if final_dir.parent != input_dir:
        raise CatmapAdapterError("existing CatMAP export escaped its input directory")
    entries = {
        entry.name: entry
        for entry in _bounded_scandir(
            final_dir, maximum=_MAX_ARTIFACT_COUNT + 1,
            label="existing CatMAP export")
    }
    if set(entries) != set(files):
        raise CatmapAdapterError("existing export directory does not match the preview")
    for name, content in files.items():
        metadata = _lstat_regular(
            final_dir / name, label=f"existing CatMAP export file {name}")
        expected = content.encode("utf-8")
        data, _, _ = _read_bounded_regular_file(
            final_dir / name, maximum=max(len(expected), 1),
            label=f"existing CatMAP export file {name}",
            expected=metadata, collect=True)
        if data != expected:
            raise CatmapAdapterError(
                f"refusing to overwrite changed export file {name}")
    boundary.verify()
    return final_dir


def export_selection_snapshot(project_root, input_sha256: str) -> dict[str, Any]:
    """Read the current immutable export selection without creating directories."""
    if not isinstance(input_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", input_sha256):
        raise CatmapAdapterError("input_sha256 must be a SHA-256 value")
    boundary = _ProjectBoundary(project_root)
    current = boundary.root
    for name in (".vcstudio", "kinetics", "exports", input_sha256):
        candidate = current / name
        if not os.path.lexists(candidate):
            boundary.verify()
            return _selection_default(input_sha256)
        current = boundary.child(current, name, create=False)
    value = _read_consistent_selection(current, input_sha256, boundary)
    boundary.verify()
    return value


def confirm_export(network_source, project_root, preview_token: str, *,
                   expected_selection_revision: int,
                   expected_selected_export_sha256: str | None,
                   confirmed: bool, tool_path=None,
                   tool_version: str | None = None) -> dict[str, Any]:
    """Write one project-confined bundle after exact preview confirmation."""
    if confirmed is not True:
        raise CatmapAdapterError("explicit confirmation is required before export")
    if not isinstance(preview_token, str) or not re.fullmatch(r"[0-9a-f]{64}", preview_token):
        raise CatmapAdapterError("preview token must be a SHA-256 value")
    bundle = build_export_bundle(
        network_source, tool_path=tool_path, tool_version=tool_version)
    if preview_token != bundle["preview_token"]:
        raise CatmapAdapterError(
            "preview token does not match the frozen input, adapter, tool, or artifacts")
    if bundle["export_ready"] is not True:
        raise CatmapAdapterError(
            "model export is not ready; use the independent audit export")
    if (isinstance(expected_selection_revision, bool)
            or not isinstance(expected_selection_revision, int)
            or expected_selection_revision < 0):
        raise CatmapAdapterError("expected selection revision must be non-negative")
    if (expected_selected_export_sha256 is not None
            and (not isinstance(expected_selected_export_sha256, str)
                 or not re.fullmatch(
                     r"[0-9a-f]{64}", expected_selected_export_sha256))):
        raise CatmapAdapterError("expected selected export hash is invalid")
    boundary = _ProjectBoundary(project_root)
    root = boundary.root
    export_parent = root
    for name in (".vcstudio", "kinetics", "exports"):
        export_parent = boundary.child(export_parent, name)
    input_dir = boundary.child(export_parent, str(bundle["input_sha256"]))
    final_dir = input_dir / bundle["preview_token"]
    resolved_export: Path | None = None
    with _SELECTION_LOCK, _advisory_lock(
            input_dir / ".selection.lock", boundary):
        anchor = _SelectionAnchorFile(
            input_dir, str(bundle["input_sha256"]), boundary)
        current, anchor_state = _recover_selection_axis(
            input_dir, str(bundle["input_sha256"]), boundary, anchor)
        if (current["revision"] != expected_selection_revision
                or current["selected_export_sha256"]
                != expected_selected_export_sha256):
            raise CatmapAdapterError("export selection conflict")
        count, retained_bytes = _input_bundle_usage(input_dir, boundary)
        existed = os.path.lexists(final_dir)
        if existed:
            resolved_export = _verify_existing_export(
                final_dir, input_dir, bundle["files"], boundary)
        else:
            candidate_bytes = sum(
                len(content.encode("utf-8")) for content in bundle["files"].values())
            if (count + 1 > _MAX_BUNDLES_PER_INPUT
                    or retained_bytes + candidate_bytes > _MAX_INPUT_BUNDLE_BYTES):
                raise CatmapAdapterError(
                    "CatMAP retained bundles exceed the per-input quota")
        transaction_id = secrets.token_hex(16)
        stage_name = (
            f".vcs-kinetics-stage-{transaction_id}" if not existed else None)
        reservation = {
            "schema": EXPORT_RESERVATION_SCHEMA,
            "input_sha256": bundle["input_sha256"],
            "preview_token": bundle["preview_token"],
            "expected_selection_revision": expected_selection_revision,
            "expected_selected_export_sha256": expected_selected_export_sha256,
            "transaction_id": transaction_id,
            "created_by_transaction": not existed,
            "stage_name": stage_name,
            "bundle_integrity_sha256": _bundle_integrity_sha256(bundle["files"]),
        }
        reservation_path = input_dir / _RESERVATION_FILENAME
        published_new = False
        anchor_started = False
        anchor_committed = False
        stage: Path | None = None
        try:
            _atomic_write(
                reservation_path, _json_text(reservation),
                replace=True, boundary=boundary)
            if not existed:
                assert stage_name is not None
                stage = input_dir / stage_name
                try:
                    os.mkdir(stage)
                except OSError as exc:
                    raise CatmapAdapterError(
                        "staged CatMAP export could not be created") from exc
                stage_metadata = _lstat_directory(
                    stage, label="staged CatMAP export")
                if (stage.parent != input_dir or _is_reparse(stage_metadata)
                        or os.path.normcase(os.path.abspath(stage))
                        != os.path.normcase(os.path.realpath(stage))):
                    raise CatmapAdapterError(
                        "staged export escaped the project boundary")
                stage = boundary.pin_directory(
                    stage, label="staged CatMAP export")
                for name, content in sorted(bundle["files"].items()):
                    if Path(name).name != name or name in {".", ".."}:
                        raise CatmapAdapterError("export artifact name is unsafe")
                    _atomic_write(
                        stage / name, content, boundary=boundary)
                boundary.verify()
                try:
                    os.replace(stage, final_dir)
                except OSError as exc:
                    raise CatmapAdapterError(
                        "CatMAP export could not be published") from exc
                _fsync_directory(input_dir)
                boundary.unpin(stage)
                stage = None
                published_new = True
                resolved_export = boundary.pin_directory(
                    final_dir, label="published CatMAP export")
            assert resolved_export is not None
            selection = {
                "schema": EXPORT_SELECTION_SCHEMA,
                "input_sha256": bundle["input_sha256"],
                "revision": current["revision"] + 1,
                "selected_export_sha256": bundle["preview_token"],
            }
            actual_integrity = _bundle_directory_integrity_sha256(
                resolved_export, input_dir, boundary)
            if actual_integrity != reservation["bundle_integrity_sha256"]:
                raise CatmapAdapterError(
                    "published CatMAP export does not match its reservation")
            prepare = _selection_anchor_prepare_record(
                anchor_state, selection, reservation)
            anchor_started = True
            anchor.append(prepare)
            _selection_fault("after_selection_prepare")
            _atomic_write(
                input_dir / "current.json", _json_text(selection),
                replace=True, boundary=boundary)
            _selection_fault("after_selection_replace")
            prepared_state = anchor.read()
            if (prepared_state.pending is None
                    or prepared_state.pending["record"]["record_sha256"]
                    != prepare["record_sha256"]):
                raise CatmapAdapterError(
                    "CatMAP selection anchor prepare could not be revalidated")
            committed_current = _read_selection(
                input_dir, str(bundle["input_sha256"]))
            if not _selection_matches_descriptor(
                    committed_current, prepared_state.pending["target"]):
                raise CatmapAdapterError(
                    "CatMAP selection replacement does not match its prepared anchor")
            commit = _selection_anchor_commit_record(prepared_state, prepare)
            anchor.append(commit)
            anchor_committed = True
            _selection_fault("after_selection_commit")
            _unlink_regular(
                reservation_path, label="CatMAP export reservation",
                boundary=boundary)
        except Exception:
            if not anchor_started:
                if stage is not None and os.path.lexists(stage):
                    _remove_safe_bundle_directory(stage, input_dir, boundary)
                if published_new:
                    _remove_safe_bundle_directory(final_dir, input_dir, boundary)
                if os.path.lexists(reservation_path):
                    _unlink_regular(
                        reservation_path, label="CatMAP export reservation",
                        boundary=boundary)
            raise
        if not anchor_committed:
            raise CatmapAdapterError("CatMAP selection anchor did not commit")
        boundary.verify()
    assert resolved_export is not None
    return {
        "ok": True,
        "schema": MANIFEST_SCHEMA,
        "input_sha256": bundle["input_sha256"],
        "preview_token": bundle["preview_token"],
        "selection_revision": selection["revision"],
        "selected_export_sha256": selection["selected_export_sha256"],
        "export_dir": str(resolved_export),
        "files": sorted(bundle["files"]),
        "audit": copy.deepcopy(bundle["audit"]),
        "tool": copy.deepcopy(bundle["tool"]),
        "scientific_status": "diagnostic",
        "eligible_final": False,
    }


def load_confirmed_manifest(project_root, input_sha256: str, *,
                            export_sha256: str | None = None) -> dict[str, Any]:
    """Revalidate one fixed export manifest and every declared artifact hash."""
    if not isinstance(input_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", input_sha256):
        raise CatmapAdapterError("input_sha256 must be a SHA-256 value")
    boundary = _ProjectBoundary(project_root)
    root = boundary.root
    export_dir = root
    directory_chain: list[tuple[Path, tuple[int, int, int]]] = []
    for name in (".vcstudio", "kinetics", "exports", input_sha256):
        candidate = export_dir / name
        if not os.path.lexists(candidate):
            boundary.verify()
            raise CatmapAdapterError(
                "confirmed CatMAP export manifest is unavailable")
        export_dir = boundary.child(export_dir, name, create=False)
        metadata = _lstat_directory(
            export_dir, label="confirmed CatMAP export manifest")
        directory_chain.append((export_dir, _entity_identity(metadata)))
    selection = _read_consistent_selection(
        export_dir, input_sha256, boundary)
    selected_sha256 = export_sha256 or selection["selected_export_sha256"]
    if selected_sha256 is None:
        raise CatmapAdapterError("confirmed CatMAP export manifest is unavailable")
    if export_sha256 is not None and export_sha256 != selection["selected_export_sha256"]:
        raise CatmapAdapterError("requested CatMAP export is not the current selection")
    candidate = export_dir / selected_sha256
    if not os.path.lexists(candidate):
        boundary.verify()
        raise CatmapAdapterError(
            "confirmed CatMAP export manifest is unavailable")
    export_dir = boundary.child(export_dir, selected_sha256, create=False)
    metadata = _lstat_directory(
        export_dir, label="confirmed CatMAP export manifest")
    directory_chain.append((export_dir, _entity_identity(metadata)))
    entries: dict[str, os.stat_result] = {}
    for entry in _bounded_scandir(
            export_dir, maximum=_MAX_ARTIFACT_COUNT + 1,
            label="confirmed CatMAP export directory"):
        entry_path = export_dir / entry.name
        try:
            entry_metadata = entry_path.lstat()
        except OSError as exc:
            raise CatmapAdapterError(
                "confirmed CatMAP export directory could not be inspected") from exc
        if (stat.S_ISLNK(entry_metadata.st_mode)
                or _is_reparse(entry_metadata)
                or not stat.S_ISREG(entry_metadata.st_mode)):
            raise CatmapAdapterError(
                "confirmed CatMAP export directory contains unsafe entries")
        entries[entry.name] = entry_metadata
    manifest_metadata = entries.get("manifest.json")
    if manifest_metadata is None:
        raise CatmapAdapterError("confirmed CatMAP export manifest is unavailable")
    manifest_path = export_dir / "manifest.json"
    try:
        manifest_bytes, _, _ = _read_bounded_regular_file(
            manifest_path, maximum=_MAX_MANIFEST_BYTES,
            label="confirmed CatMAP export manifest",
            expected=manifest_metadata, collect=True)
        assert manifest_bytes is not None
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CatmapAdapterError("confirmed CatMAP export manifest is invalid") from exc
    if not isinstance(manifest, Mapping) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise CatmapAdapterError("confirmed CatMAP export manifest schema mismatch")
    if manifest.get("input_sha256") != input_sha256:
        raise CatmapAdapterError("confirmed CatMAP export input hash mismatch")
    adapter = manifest.get("adapter")
    if adapter != {"id": ADAPTER_ID, "version": ADAPTER_VERSION}:
        raise CatmapAdapterError("confirmed CatMAP export adapter mismatch")
    tool = manifest.get("external_tool")
    if (not isinstance(tool, Mapping)
            or set(tool) != {"available", "name", "version", "sha256", "size"}
            or tool.get("available") is not True
            or not isinstance(tool.get("name"), str)
            or not tool["name"]
            or Path(tool["name"]).name != tool["name"]
            or _CONTROL_RE.search(tool["name"])
            or not isinstance(tool.get("version"), str)
            or not _SAFE_VERSION_RE.fullmatch(tool["version"])
            or not isinstance(tool.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", tool["sha256"])
            or isinstance(tool.get("size"), bool)
            or not isinstance(tool.get("size"), int)
            or tool["size"] < 0):
        raise CatmapAdapterError("confirmed CatMAP export tool identity is invalid")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise CatmapAdapterError("confirmed CatMAP export artifact manifest is empty")
    if len(artifacts) > _MAX_ARTIFACT_COUNT:
        raise CatmapAdapterError(
            "confirmed CatMAP export artifact count exceeds the limit")
    normalized_artifacts = []
    artifact_names: set[str] = set()
    folded_artifact_names: set[str] = set()
    declared_total = 0
    for index, item in enumerate(artifacts):
        if (not isinstance(item, Mapping)
                or set(item) != {"name", "sha256", "size"}):
            raise CatmapAdapterError(
                f"confirmed CatMAP export artifact {index} is invalid")
        name = item.get("name")
        digest = item.get("sha256")
        size = item.get("size")
        if (not isinstance(name, str)
                or not _SAFE_ARTIFACT_NAME_RE.fullmatch(name)
                or name == "manifest.json"
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or isinstance(size, bool) or not isinstance(size, int)
                or size < 0):
            raise CatmapAdapterError(
                f"confirmed CatMAP export artifact {index} metadata is unsafe")
        if size > _MAX_ARTIFACT_BYTES:
            raise CatmapAdapterError(
                f"confirmed CatMAP export declared artifact exceeds the size limit: {name}")
        folded_name = name.casefold()
        if name in artifact_names or folded_name in folded_artifact_names:
            raise CatmapAdapterError(
                "confirmed CatMAP export artifact names must be unique")
        artifact_names.add(name)
        folded_artifact_names.add(folded_name)
        declared_total += size
        if declared_total > _MAX_TOTAL_ARTIFACT_BYTES:
            raise CatmapAdapterError(
                "confirmed CatMAP export declared artifact bytes exceed the total limit")
        normalized_artifacts.append(dict(item))
    if set(entries) != {"manifest.json"} | artifact_names:
        raise CatmapAdapterError(
            "confirmed CatMAP export directory entries do not match the manifest")
    actual_total = 0
    for name in artifact_names:
        actual_size = entries[name].st_size
        if actual_size > _MAX_ARTIFACT_BYTES:
            raise CatmapAdapterError(
                f"confirmed CatMAP export artifact exceeds the size limit: {name}")
        actual_total += actual_size
        if actual_total > _MAX_TOTAL_ARTIFACT_BYTES:
            raise CatmapAdapterError(
                "confirmed CatMAP export actual artifact bytes exceed the total limit")
    for item in normalized_artifacts:
        name = item["name"]
        digest = item["sha256"]
        size = item["size"]
        artifact_path = export_dir / name
        if entries[name].st_size != size:
            raise CatmapAdapterError(
                f"confirmed CatMAP export artifact size mismatch: {name}")
        _, actual_size, actual_digest = _read_bounded_regular_file(
            artifact_path, maximum=_MAX_ARTIFACT_BYTES,
            label=f"confirmed CatMAP export artifact {name}",
            expected=entries[name])
        if actual_size != size or actual_digest != digest:
            raise CatmapAdapterError(f"confirmed CatMAP export artifact hash mismatch: {name}")
    final_entries: dict[str, os.stat_result] = {}
    try:
        for entry in _bounded_scandir(
                export_dir, maximum=len(entries),
                label="confirmed CatMAP export directory changed during validation"):
            final_entries[entry.name] = (export_dir / entry.name).lstat()
    except CatmapAdapterError:
        raise
    except OSError as exc:
        raise CatmapAdapterError(
            "confirmed CatMAP export directory changed during validation") from exc
    if (set(final_entries) != set(entries)
            or any(
                _file_snapshot(final_entries[name]) != _file_snapshot(metadata)
                for name, metadata in entries.items()
            )):
        raise CatmapAdapterError(
            "confirmed CatMAP export directory changed during validation")
    for directory, identity in directory_chain:
        current = _lstat_directory(
            directory, label="confirmed CatMAP export directory")
        if _entity_identity(current) != identity:
            raise CatmapAdapterError(
                "confirmed CatMAP export directory changed during validation")
    boundary.verify()
    token_payload = {
        "schema": BUNDLE_SCHEMA,
        "input_sha256": input_sha256,
        "adapter": {"id": ADAPTER_ID, "version": ADAPTER_VERSION},
        "tool": dict(tool),
        "artifacts": normalized_artifacts,
    }
    token = hashlib.sha256(_canonical_bytes(token_payload)).hexdigest()
    if manifest.get("preview_token") != token or token != selected_sha256:
        raise CatmapAdapterError("confirmed CatMAP export preview token mismatch")
    if (manifest.get("catmap_bundled") is not False
            or manifest.get("catmap_copied") is not False
            or manifest.get("scientific_status") != "diagnostic"
            or manifest.get("eligible_final") is not False):
        raise CatmapAdapterError("confirmed CatMAP export boundary metadata mismatch")
    return {
        "schema": MANIFEST_SCHEMA,
        "input_sha256": input_sha256,
        "preview_token": token,
        "selection_revision": selection["revision"],
        "selected_export_sha256": selected_sha256,
        "adapter": {"id": ADAPTER_ID, "version": ADAPTER_VERSION},
        "tool": dict(tool),
        "artifacts": normalized_artifacts,
        "scientific_status": "diagnostic",
        "eligible_final": False,
    }


def _audit_export_bundle(network_source, *, tool_path=None,
                         tool_version: str | None = None) -> dict[str, Any]:
    model_bundle = build_export_bundle(
        network_source, tool_path=tool_path, tool_version=tool_version)
    files = {
        name: content for name, content in model_bundle["files"].items()
        if name in {"kinetics-audit.json", "catmap-adapter-audit.json"}
    }
    artifacts = _artifact_records(files)
    token_payload = {
        "schema": AUDIT_MANIFEST_SCHEMA,
        "input_sha256": model_bundle["input_sha256"],
        "artifacts": artifacts,
        "model_published": False,
    }
    preview_token = hashlib.sha256(_canonical_bytes(token_payload)).hexdigest()
    manifest = {
        **token_payload, "preview_sha256": preview_token,
        "audit_export_status": "audit_only", "scientific_status": "diagnostic",
        "eligible_final": False,
    }
    files["audit-manifest.json"] = _json_text(manifest)
    return {
        "input_sha256": model_bundle["input_sha256"], "files": files,
        "artifacts": artifacts, "audit": model_bundle["audit"],
        "adapter_issues": model_bundle["adapter_issues"],
        "preview_token": preview_token,
    }


def preview_audit_export(network_source, *, tool_path=None,
                         tool_version: str | None = None) -> dict[str, Any]:
    bundle = _audit_export_bundle(
        network_source, tool_path=tool_path, tool_version=tool_version)
    return {
        "schema": AUDIT_PREVIEW_SCHEMA,
        "input_sha256": bundle["input_sha256"],
        "artifacts": copy.deepcopy(bundle["artifacts"]),
        "audit": copy.deepcopy(bundle["audit"]),
        "adapter_issues": copy.deepcopy(bundle["adapter_issues"]),
        "preview_token": bundle["preview_token"],
        "export_kind": "audit_report", "audit_export_status": "preview",
        "model_published": False, "explicit_confirmation_required": True,
        "scientific_status": "diagnostic", "eligible_final": False,
    }


def confirm_audit_export(network_source, project_root, preview_token: str, *,
                         confirmed: bool, tool_path=None,
                         tool_version: str | None = None) -> dict[str, Any]:
    if confirmed is not True:
        raise CatmapAdapterError("explicit confirmation is required before audit export")
    bundle = _audit_export_bundle(
        network_source, tool_path=tool_path, tool_version=tool_version)
    if preview_token != bundle["preview_token"]:
        raise CatmapAdapterError("audit preview token mismatch")
    boundary = _ProjectBoundary(project_root)
    parent = boundary.root
    for name in (".vcstudio", "kinetics", "audits", bundle["input_sha256"]):
        parent = boundary.child(parent, str(name))
    final_dir = parent / preview_token
    if os.path.lexists(final_dir):
        try:
            resolved_export = _verify_existing_export(
                final_dir, parent, bundle["files"], boundary)
        except CatmapAdapterError as exc:
            raise CatmapAdapterError("existing audit export differs") from exc
    else:
        stage = Path(tempfile.mkdtemp(prefix=".vcs-audit-stage-", dir=parent))
        published_new = False
        try:
            stage_metadata = _lstat_directory(
                stage, label="staged CatMAP audit export")
            if (stage.parent != parent or _is_reparse(stage_metadata)
                    or os.path.normcase(os.path.abspath(stage))
                    != os.path.normcase(os.path.realpath(stage))):
                raise CatmapAdapterError("staged audit export escaped project boundary")
            stage = boundary.pin_directory(
                stage, label="staged CatMAP audit export")
            for name, content in sorted(bundle["files"].items()):
                if Path(name).name != name or name in {".", ".."}:
                    raise CatmapAdapterError("audit export artifact name is unsafe")
                _atomic_write(stage / name, content, boundary=boundary)
            boundary.verify()
            try:
                os.replace(stage, final_dir)
            except OSError as exc:
                raise CatmapAdapterError(
                    "CatMAP audit export could not be published") from exc
            boundary.unpin(stage)
            published_new = True
            resolved_export = boundary.pin_directory(
                final_dir, label="published CatMAP audit export")
        except Exception:
            if os.path.lexists(stage):
                _remove_safe_bundle_directory(stage, parent, boundary)
            if published_new and os.path.lexists(final_dir):
                _remove_safe_bundle_directory(final_dir, parent, boundary)
            raise
    boundary.verify()
    return {
        "ok": True, "schema": AUDIT_MANIFEST_SCHEMA,
        "input_sha256": bundle["input_sha256"], "files": sorted(bundle["files"]),
        "audit_export_status": "published", "model_published": False,
        "export_dir": str(resolved_export), "scientific_status": "diagnostic",
        "eligible_final": False,
    }


__all__ = [
    "ADAPTER_ID", "ADAPTER_VERSION", "AUDIT_MANIFEST_SCHEMA",
    "AUDIT_PREVIEW_SCHEMA", "BUNDLE_SCHEMA", "CATMAP_LICENSE",
    "CatmapAdapterError", "EXPORT_SELECTION_SCHEMA", "MANIFEST_SCHEMA",
    "PREVIEW_SCHEMA", "PROCESS_SCHEMA", "build_export_bundle",
    "confirm_audit_export", "confirm_export", "export_selection_snapshot",
    "inspect_tool_path", "load_confirmed_manifest", "preview_audit_export",
    "preview_export",
]
