"""Authoritative NEB, convergence-scan, and AIMD workbench projections.

The legacy task-results parsers remain available for backwards compatibility.
This module is the stricter Analysis Workbench boundary: it reads only
server-resolved jobs, attaches parser and file identities to every quantitative
value, and withholds derived conclusions whenever their evidence gates are not
complete.

None of the projections mutate VASP inputs, derive jobs, or submit work.
"""
from __future__ import annotations

import copy
import hashlib
import inspect
import json
import math
import os
import re
import stat
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from vcstudio import __version__
from vcstudio.generate.incar_builder import parse_incar
from vcstudio.generate.method_recipe import (
    METHOD_RECIPE_AUTHORITY,
    METHOD_RECIPE_SCHEMA,
)
from vcstudio.generate.methods_text import parse_kpoints_scheme
from vcstudio.generate.poscar import parse_poscar_species
from vcstudio.generate.slab_builder import LAYER_TOL, count_layers, vacuum_thickness
from vcstudio.generate.structure_view import parse_positions
from vcstudio.project.analysis_registry import AnalysisSpec, get_analysis
from vcstudio.project.analysis_sources import (
    SourceSnapshot,
    SourceSnapshotBudget,
    SourceSnapshotChanged,
    capture_source_snapshot,
    value_provenance,
    verify_neb_endpoint_record,
)
from vcstudio.project.energy_gate import (
    validate_done_completion_evidence,
    validate_done_energy_evidence,
)
from vcstudio.project.neb import parse_final_neb_image_event
from vcstudio.project.calculation_reuse import FINGERPRINT_SCHEMA
from vcstudio.shared.execution_environment import (
    EXECUTION_ENVIRONMENT_AUTHORITY,
    EXECUTION_ENVIRONMENT_SCHEMA,
)
from vcstudio.shared.scientific_inputs import INPUT_CLOSURE_SCHEMA
from vcstudio.shared.vasp_identity import canonical_kpoints_effective_text


VIEW_SCHEMA = "vcstudio.analysis-view/v1"
REPORT_BINDING_SCHEMA = "vcstudio.analysis-report-binding/v1"
PARSER_MODULE = "vcstudio.project.analysis_scientific"
PARSER_VERSION = str(__version__)
CONVERGENCE_THRESHOLDS_MEV_PER_ATOM = (0.5, 1.0, 2.0)
CONVERGENCE_PRIMARY_THRESHOLD_MEV_PER_ATOM = 1.0
CONVERGENCE_MIN_PLATFORM_POINTS = 3
AIMD_SHORT_TRAJECTORY_PS = 10.0
MAX_NEB_FRAMES = 100

_FRAME_RE = re.compile(r"^\d+$")
_NIONS_RE = re.compile(r"\bNIONS\s*=\s*(\d+)")
_VASPRUN_ATOMS_RE = re.compile(r"<atoms>\s*(\d+)\s*</atoms>", re.I)
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _capture(
    target: Mapping[str, Any], evidence_names: Sequence[Any], *,
    budget: SourceSnapshotBudget | None = None,
) -> tuple[SourceSnapshot, dict[str, Any]]:
    snapshot = capture_source_snapshot(target, evidence_names, budget=budget)
    manifest = snapshot.manifest()
    if snapshot.has("job.yaml"):
        snapshot.assert_manifest_matches(target.get("manifest") or {})
    return snapshot, manifest


def _method_evidence(
    resolver: Callable[..., Mapping[str, Any]], target: Mapping[str, Any],
    snapshot: SourceSnapshot,
) -> dict[str, Any]:
    try:
        signature = inspect.signature(resolver)
        signature.bind(target, snapshot)
    except (TypeError, ValueError):
        return dict(resolver(target) or {})
    return dict(resolver(target, snapshot) or {})


def _verified_method(method: Mapping[str, Any]) -> bool:
    return bool(
        method.get("status") == "verified" and method.get("fingerprint")
        and not any(method.get(key) for key in (
            "missing", "warnings", "errors", "issues",
        ))
    )


def _parser(callable_name: str) -> dict[str, str]:
    return {
        "module": PARSER_MODULE,
        "callable": callable_name,
        "version": PARSER_VERSION,
        "version_source": "vcstudio.__version__",
    }


def _display(value: Any, precision: int) -> str:
    number = _finite(value)
    return "—" if number is None else f"{number:.{precision}f}"


def _quantity(
    *, key: str, label: str, value: float | int | None, unit: str,
    precision: int, denominator: str, source_id: str,
    files: Sequence[Mapping[str, Any]], parser: Mapping[str, Any],
    reason: str = "",
) -> dict[str, Any]:
    safe_value = _finite(value)
    provenance = value_provenance(source_id, files, parser)
    return {
        "key": key,
        "label": label,
        "value": safe_value,
        "display": _display(safe_value, precision),
        "unit": unit,
        "denominator": denominator,
        **provenance,
        "status": "available" if safe_value is not None else "unavailable",
        "reason": str(reason or ""),
    }


def _report_binding(
    payload: Mapping[str, Any], *, figure_presets: Sequence[str],
    table_ids: Sequence[str], figure_data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    capability = get_analysis(str(payload.get("analysis_id") or ""))
    fingerprint = str(payload.get("data_fingerprint") or "")
    figures = []
    for preset in figure_presets:
        data = (figure_data or {}).get(preset)
        if data is None:
            continue
        figures.append({
            "preset_key": preset,
            "data_sha256": _canonical_hash(data),
            "view_data_fingerprint": fingerprint,
        })
    binding = {
        "schema": REPORT_BINDING_SCHEMA,
        "analysis_id": payload.get("analysis_id"),
        "analysis_spec_sha256": payload.get("spec_sha256"),
        "view_data_fingerprint": fingerprint,
        "report_kind": "diagnostic",
        "scientific_qualification": "diagnostic",
        "claim_ceiling": "diagnostic_only",
        # This is intentionally the only source of report sections.
        "report_sections": list(capability["report_sections"]),
        "figures": figures,
        "tables": [
            {"table_id": table_id, "view_data_fingerprint": fingerprint}
            for table_id in table_ids
        ],
        "final_allowed": False,
    }
    binding["binding_sha256"] = _canonical_hash(binding)
    return binding


def _finalize(
    payload: dict[str, Any], *, figure_presets: Sequence[str],
    table_ids: Sequence[str], figure_data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    core = copy.deepcopy(payload)
    core.pop("data_fingerprint", None)
    core.pop("report_binding", None)
    core.pop("figure_data", None)
    payload["data_fingerprint"] = _canonical_hash(core)
    payload["figure_data"] = copy.deepcopy(dict(figure_data or {}))
    payload["report_binding"] = _report_binding(
        payload, figure_presets=figure_presets, table_ids=table_ids,
        figure_data=figure_data,
    )
    return payload


def _matrix_inverse(matrix: Sequence[Sequence[float]]) -> list[list[float]] | None:
    try:
        a, b, c = matrix
        det = (
            a[0] * (b[1] * c[2] - b[2] * c[1])
            - a[1] * (b[0] * c[2] - b[2] * c[0])
            + a[2] * (b[0] * c[1] - b[1] * c[0])
        )
    except (IndexError, TypeError):
        return None
    if not math.isfinite(det) or abs(det) < 1e-12:
        return None
    return [
        [
            (b[1] * c[2] - b[2] * c[1]) / det,
            (a[2] * c[1] - a[1] * c[2]) / det,
            (a[1] * b[2] - a[2] * b[1]) / det,
        ],
        [
            (b[2] * c[0] - b[0] * c[2]) / det,
            (a[0] * c[2] - a[2] * c[0]) / det,
            (a[2] * b[0] - a[0] * b[2]) / det,
        ],
        [
            (b[0] * c[1] - b[1] * c[0]) / det,
            (a[1] * c[0] - a[0] * c[1]) / det,
            (a[0] * b[1] - a[1] * b[0]) / det,
        ],
    ]


def _row_vector(vector: Sequence[float], matrix: Sequence[Sequence[float]]) -> list[float]:
    return [sum(float(vector[i]) * float(matrix[i][j]) for i in range(3))
            for j in range(3)]


def _same_cell(left: Sequence[Sequence[float]], right: Sequence[Sequence[float]]) -> bool:
    try:
        return all(abs(float(left[i][j]) - float(right[i][j])) <= 1e-6
                   for i in range(3) for j in range(3))
    except (IndexError, TypeError, ValueError):
        return False


def _neb_coordinates(
    snapshot: SourceSnapshot, frames: Sequence[str],
) -> tuple[list[float | None], str]:
    structures = []
    for frame in frames:
        contcar = f"{frame}/CONTCAR"
        poscar = f"{frame}/POSCAR"
        text = snapshot.text(contcar)
        if not text.strip():
            text = snapshot.text(poscar)
        if not text.strip():
            return [None] * len(frames), f"{frame} lacks POSCAR/CONTCAR structure evidence"
        try:
            structures.append(parse_positions(text))
        except (IndexError, TypeError, ValueError) as exc:
            return [None] * len(frames), f"{frame} structure parsing failed: {exc}"
    natoms = len(structures[0].get("coords") or [])
    cell = structures[0].get("cell") or []
    elements = list(structures[0].get("elements") or [])
    inverse = _matrix_inverse(cell)
    if not natoms or inverse is None:
        return [None] * len(frames), "NEB structure atom count or cell is unavailable"
    if any(len(item.get("coords") or []) != natoms or
           list(item.get("elements") or []) != elements or
           not _same_cell(cell, item.get("cell") or []) for item in structures[1:]):
        return [None] * len(frames), "NEB images do not have one fixed cell and atom ordering"
    cumulative = [0.0]
    for previous, current in zip(structures, structures[1:]):
        squared = 0.0
        for left, right in zip(previous["coords"], current["coords"]):
            fractional = _row_vector(
                [float(right[i]) - float(left[i]) for i in range(3)], inverse)
            fractional = [value - round(value) for value in fractional]
            displacement = _row_vector(fractional, cell)
            squared += sum(value * value for value in displacement)
        cumulative.append(cumulative[-1] + math.sqrt(squared / natoms))
    return cumulative, ""


def _frame_natoms(snapshot: SourceSnapshot, frame: str) -> tuple[int | None, str]:
    for name in (f"{frame}/CONTCAR", f"{frame}/POSCAR"):
        text = snapshot.text(name)
        if not text.strip():
            continue
        try:
            _symbols, counts = parse_poscar_species(text)
        except (IndexError, TypeError, ValueError):
            continue
        if counts and sum(counts) > 0:
            return int(sum(counts)), name
    return None, ""


def _discover_neb_frames(root: Path) -> tuple[list[str], bool, bool]:
    frames = []
    overflow = False
    unsafe = False
    try:
        for item in root.iterdir():
            if not _FRAME_RE.fullmatch(item.name):
                continue
            try:
                metadata = item.lstat()
            except OSError:
                unsafe = True
                continue
            if (not stat.S_ISDIR(metadata.st_mode)
                    or int(getattr(metadata, "st_file_attributes", 0)) & 0x400):
                unsafe = True
                continue
            if len(frames) >= MAX_NEB_FRAMES:
                overflow = True
                continue
            frames.append(item.name)
    except OSError:
        unsafe = True
    frames.sort(key=int)
    return frames, overflow, unsafe


def _assert_neb_frame_set(
    root: Path, expected: Sequence[str], *, overflow: bool, unsafe: bool,
) -> None:
    if overflow or unsafe:
        return
    current, current_overflow, current_unsafe = _discover_neb_frames(root)
    if list(expected) != current or current_overflow or current_unsafe:
        raise SourceSnapshotChanged(
            "NEB image-directory set changed during analysis; retry")


def _neb_path(
    spec: AnalysisSpec, target: Mapping[str, Any],
    method_evidence: Callable[..., Mapping[str, Any]],
    targets_by_source_id: Mapping[str, Mapping[str, Any]],
    snapshot_budget: SourceSnapshotBudget,
) -> dict[str, Any]:
    root = Path(str(target.get("path") or ""))
    parser = _parser("build_neb_analysis_view")
    frames, frame_overflow, unsafe_frame_directory = _discover_neb_frames(root)
    evidence_names = [
        f"{frame}/{name}" for frame in frames
        for name in ("POSCAR", "CONTCAR", "OSZICAR", "OUTCAR")
    ]
    snapshot, manifest = _capture(target, [
        "INCAR", "KPOINTS", "POTCAR", "POSCAR", "CONTCAR",
        "OUTCAR", "vasprun.xml", *evidence_names,
    ], budget=snapshot_budget)
    source = snapshot.identity()
    issues: list[str] = []
    warnings: list[str] = []
    evidence_blocked = bool(
        snapshot.rejected or frame_overflow or unsafe_frame_directory)
    issues.extend(
        f'{source["source_id"]}: {issue}' for issue in snapshot.evidence_issues())
    completion_issues = []
    if frame_overflow:
        issues.append(
            f"NEB image count exceeds the hard limit of {MAX_NEB_FRAMES}")
    if unsafe_frame_directory:
        issues.append("NEB numeric image entries must be no-follow regular directories")
    try:
        _manifest, _completion_source, _completion_notes = (
            validate_done_completion_evidence(
                manifest, source["source_id"],
                outcar_text=snapshot.text("OUTCAR"),
                vasprun_text=snapshot.text("vasprun.xml"),
                require_current_completion=True,
                require_explicit_diagnosis=True,
                reject_explicit_unclean=True,
                require_outcar_completion=True,
            ))
    except ValueError as exc:
        completion_issues.append(str(exc))
        issues.append(str(exc))
    if len(frames) < 3:
        issues.append("NEB requires at least three ordered image directories")
    coordinates, coordinate_reason = (
        _neb_coordinates(snapshot, frames) if frames else ([], "")
    )
    if coordinate_reason:
        issues.append(coordinate_reason)

    inputs = manifest.get("inputs") or {}
    declared_images = inputs.get("n_images")
    declared_images_valid = bool(
        isinstance(declared_images, int) and not isinstance(declared_images, bool)
        and declared_images >= 1)
    incar = parse_incar(snapshot.text("INCAR"))
    incar_images_value = _finite(incar.get("IMAGES"))
    incar_images = (
        int(incar_images_value) if incar_images_value is not None
        and incar_images_value == int(incar_images_value)
        and incar_images_value >= 1 else None)
    if not declared_images_valid:
        issues.append("NEB manifest requires an explicit positive integer n_images")
    if incar_images is None:
        issues.append("current NEB INCAR requires an explicit positive integer IMAGES")
    if (declared_images_valid and incar_images is not None
            and declared_images != incar_images):
        issues.append(
            f"manifest n_images={declared_images} does not match current "
            f"INCAR IMAGES={incar_images}")
    authoritative_images = (
        declared_images if declared_images_valid and declared_images == incar_images
        else None)
    expected_count = (
        authoritative_images + 2 if authoritative_images is not None else 0)
    expected_frames = (
        [f"{index:02d}" for index in range(expected_count)]
        if 3 <= expected_count <= MAX_NEB_FRAMES else [])
    if frame_overflow or expected_count > MAX_NEB_FRAMES:
        issues.append(
            f"NEB image count exceeds the hard limit of {MAX_NEB_FRAMES}")
    if frames != expected_frames:
        issues.append(
            "NEB image directories must be the exact continuous set "
            f"00..N+1; observed={frames!r}, expected={expected_frames!r}")
    ediffg = _finite(incar.get("EDIFFG"))
    force_threshold = abs(ediffg) if ediffg is not None and ediffg < 0 else None
    if force_threshold is None:
        issues.append("NEB lacks an explicit negative EDIFFG force threshold")
    climb = str(incar.get("LCLIMB") or "").strip().strip(".").upper() in {
        "T", "TRUE", "1",
    }
    if not climb:
        issues.append("LCLIMB evidence is absent; a climbing-image barrier is unavailable")
    method = _method_evidence(method_evidence, target, snapshot)
    method_ok = _verified_method(method)
    if not method_ok:
        issues.append("NEB method identity is not verified")
    endpoints = inputs.get("neb_endpoints") or {}
    if not isinstance(endpoints, Mapping):
        endpoints = {}
    endpoint_verifications: dict[str, dict[str, Any]] = {}
    endpoint_evidence: dict[str, dict[str, Any]] = {}
    endpoint_snapshots: list[SourceSnapshot] = []
    expected_start = "00"
    expected_end = (
        f"{expected_count - 1:02d}" if expected_count >= 3 else "unavailable")
    for role, frame in (("start", expected_start), ("end", expected_end)):
        verification = verify_neb_endpoint_record(
            record=endpoints.get(role) or {}, role=role, frame=frame,
            neb_snapshot=snapshot, targets_by_source_id=targets_by_source_id,
            method_resolver=lambda endpoint, endpoint_snapshot: _method_evidence(
                method_evidence, endpoint, endpoint_snapshot),
            neb_method_fingerprint=method.get("fingerprint"),
            snapshot_budget=snapshot_budget,
        )
        endpoint_verifications[role] = verification
        issues.extend(verification.get("issues") or [])
        source_snapshot = verification.get("source_snapshot")
        if isinstance(source_snapshot, SourceSnapshot):
            endpoint_snapshots.append(source_snapshot)
        endpoint_evidence[role] = {
            "status": "verified" if verification.get("ok") else "unavailable",
            "source": (
                source_snapshot.identity() if isinstance(source_snapshot, SourceSnapshot)
                else {"source_id": verification.get("source_id") or "", "files": []}
            ),
            "verified_files": list(verification.get("verified_files") or []),
            "method": copy.deepcopy(verification.get("method") or {}),
            "issues": list(verification.get("issues") or []),
        }

    points = []
    energies: list[float | None] = []
    electronic_statuses = []
    ionic_statuses = []
    for index, frame in enumerate(frames):
        role = "start" if index == 0 else "end" if index == len(frames) - 1 else "image"
        expected_natoms, structure_name = _frame_natoms(snapshot, frame)
        final_step = parse_final_neb_image_event(
            snapshot.text(f"{frame}/OSZICAR"),
            snapshot.text(f"{frame}/OUTCAR"), expected_natoms,
        )
        energy = final_step.get("energy") if final_step.get("status") == "complete" else None
        if evidence_blocked:
            energy = None
        endpoint_verification = endpoint_verifications.get(role)
        if endpoint_verification is not None and (
                not endpoint_verification.get("ok")
                or not {"OSZICAR", "OUTCAR"}.issubset(
                    set(endpoint_verification.get("verified_files", [])))):
            energy = None
        energies.append(energy)
        fmax = (final_step.get("fmax")
                if final_step.get("status") == "complete" and not evidence_blocked
                else None)
        electronic = (
            str(final_step.get("electronic_status") or "unavailable")
            if not evidence_blocked else "unavailable")
        if role != "image":
            ionic = "endpoint"
        elif fmax is None or force_threshold is None:
            ionic = "unavailable"
        else:
            ionic = "converged" if fmax <= force_threshold else "not_converged"
        electronic_statuses.append(electronic)
        ionic_statuses.append(ionic)
        for item in final_step.get("issues") or []:
            issues.append(f"{frame}: {item}")
        frame_files = [
            item for item in source["files"]
            if str(item.get("name") or "").startswith(f"{frame}/")
        ]
        energy_files = [
            item for item in frame_files
            if str(item.get("name") or "").endswith(("/OSZICAR", "/OUTCAR"))
        ]
        coordinate = coordinates[index] if index < len(coordinates) else None
        points.append({
            "image_index": index,
            "image_label": frame,
            "reaction_coordinate": _quantity(
                key="reaction_coordinate", label="Reaction coordinate",
                value=coordinate, unit="Å RMS cumulative", precision=spec.precision,
                denominator=f"{len(frames)} ordered images",
                source_id=source["source_id"], files=frame_files, parser=parser,
                reason=coordinate_reason if coordinate is None else "",
            ),
            "absolute_energy": _quantity(
                key="absolute_energy", label="Absolute energy", value=energy,
                unit="eV", precision=spec.precision,
                denominator="per NEB image", source_id=source["source_id"],
                files=energy_files, parser=parser,
                reason=("OSZICAR energy is not bound to the final complete OUTCAR "
                        "ionic event" if energy is None else ""),
            ),
            "relative_energy": None,
            "max_force": _quantity(
                key="max_force", label="Maximum force", value=fmax,
                unit="eV/Å", precision=spec.precision,
                denominator="maximum over atoms in the final ionic step",
                source_id=source["source_id"], files=frame_files, parser=parser,
                reason=("final ionic energy/force/EDIFF event is unavailable"
                        if fmax is None else ""),
            ),
            "electronic_convergence": electronic,
            "ionic_convergence": ionic,
            "structure_evidence": snapshot.files([structure_name]) if structure_name else [],
            "file_evidence": frame_files,
        })

    initial = energies[0] if energies else None
    for point, energy in zip(points, energies):
        relative = None if initial is None or energy is None else energy - initial
        energy_files = point["absolute_energy"]["file_hashes"]
        point["relative_energy"] = _quantity(
            key="relative_energy", label="Relative energy", value=relative,
            unit="eV", precision=spec.precision,
            denominator="relative to image 00",
            source_id=source["source_id"], files=energy_files, parser=parser,
            reason="initial or image energy is unavailable" if relative is None else "",
        )

    if any(value is None for value in energies):
        issues.append("one or more NEB image energies are unavailable")
    if any(status != "converged" for status in electronic_statuses[1:-1]):
        issues.append("one or more intermediate images lack electronic convergence evidence")
    if any(status != "converged" for status in ionic_statuses[1:-1]):
        issues.append("one or more intermediate images fail the explicit force threshold")

    relative_values = [
        point["relative_energy"]["value"] for point in points
    ]
    finite_relative = [value for value in relative_values if value is not None]
    ts_index = None
    if len(finite_relative) == len(relative_values) and finite_relative:
        ts_index = max(range(len(finite_relative)), key=finite_relative.__getitem__)
        if ts_index in {0, len(finite_relative) - 1}:
            issues.append("the maximum-energy image is an endpoint")
        if len(finite_relative) >= 3:
            increasing = all(
                finite_relative[index + 1] >= finite_relative[index] - 1e-12
                for index in range(len(finite_relative) - 1)
            )
            decreasing = all(
                finite_relative[index + 1] <= finite_relative[index] + 1e-12
                for index in range(len(finite_relative) - 1)
            )
            if increasing or decreasing:
                issues.append("the sampled path is monotonic and has no interior maximum")
    if coordinate_reason:
        warnings.append("geometric reaction coordinates are withheld until structures align")

    barrier_ok = bool(
        len(frames) >= 3 and method_ok and not issues and
        ts_index not in (None, 0, len(frames) - 1)
    )
    forward = finite_relative[ts_index] if barrier_ok and ts_index is not None else None
    reverse = (
        finite_relative[ts_index] - finite_relative[-1]
        if barrier_ok and ts_index is not None else None
    )
    barrier_files = _merge_files(
        source["files"],
        *(item["source"].get("files") or [] for item in endpoint_evidence.values()),
    )
    barriers = {
        "status": "available" if barrier_ok else "unavailable",
        "forward": _quantity(
            key="forward_barrier", label="Forward barrier", value=forward,
            unit="eV", precision=spec.precision,
            denominator="maximum along this supplied endpoint path minus image 00",
            source_id=source["source_id"], files=barrier_files, parser=parser,
            reason=issues[0] if issues else "",
        ),
        "reverse": _quantity(
            key="reverse_barrier", label="Reverse barrier", value=reverse,
            unit="eV", precision=spec.precision,
            denominator="maximum along this supplied endpoint path minus final image",
            source_id=source["source_id"], files=barrier_files, parser=parser,
            reason=issues[0] if issues else "",
        ),
        "transition_image_index": ts_index if barrier_ok else None,
        "blocking": list(dict.fromkeys(issues)),
    }
    return {
        "source": source,
        "parser": parser,
        "method": method,
        "completion_evidence": {
            "status": "verified" if not completion_issues else "unavailable",
            "file_hashes": snapshot.files(["job.yaml", "OUTCAR", "vasprun.xml"]),
            "issues": completion_issues,
        },
        "endpoint_evidence": endpoint_evidence,
        "status": ("unavailable" if evidence_blocked else
                   "available" if points else "missing_prerequisite"),
        "available": bool(points and not evidence_blocked),
        "points": points,
        "barriers": barriers,
        "path_quality": {
            "status": "pass" if barrier_ok else "diagnostic",
            "issues": list(dict.fromkeys(issues)),
            "warnings": list(dict.fromkeys(warnings)),
        },
        "scientific_boundary": (
            "This NEB result tests only the supplied path near its endpoints; it does not "
            "establish a complete mechanism. Alternative paths and transition-state "
            "frequency evidence require independent review. Γ-point frequencies are not "
            "a phonon dispersion."
        ),
        "_snapshots": [snapshot, *endpoint_snapshots],
        "_frame_root": root,
        "_frames": list(frames),
        "_frame_overflow": frame_overflow,
        "_unsafe_frame_directory": unsafe_frame_directory,
    }


def build_neb_analysis_view(
    spec: AnalysisSpec, targets: Sequence[Mapping[str, Any]], *,
    method_evidence: Callable[..., Mapping[str, Any]],
) -> dict[str, Any]:
    if spec.analysis_id != "neb-path":
        raise ValueError("NEB analysis view requires analysis_id=neb-path")
    targets_by_source_id = {
        str(target.get("source_id") or ""): target for target in targets
        if target.get("source_id")
    }
    selected = [target for target in targets if target.get("task_type") == "neb"]
    snapshot_budget = SourceSnapshotBudget()
    paths = [
        _neb_path(
            spec, target, method_evidence, targets_by_source_id, snapshot_budget)
        for target in selected
    ]
    for path in paths:
        for snapshot in path.pop("_snapshots", []):
            snapshot.assert_unchanged()
        _assert_neb_frame_set(
            path.pop("_frame_root"), path.pop("_frames"),
            overflow=path.pop("_frame_overflow"),
            unsafe=path.pop("_unsafe_frame_directory"),
        )
    blocking = [
        issue for path in paths for issue in path["path_quality"]["issues"]
    ]
    warnings = [
        warning for path in paths for warning in path["path_quality"]["warnings"]
    ]
    if not selected:
        blocking.append("No registered NEB project member or descendant was resolved")
    available = sum(path["available"] is True for path in paths)
    barrier_count = sum(path["barriers"]["status"] == "available" for path in paths)
    figure_data = {}
    if len(paths) == 1 and paths[0]["points"]:
        values = [point["relative_energy"]["value"] for point in paths[0]["points"]]
        if sum(_finite(value) is not None for value in values) >= 2:
            figure_data["neb_profile"] = {
                "rel": values,
                "ts_index": paths[0]["barriers"]["transition_image_index"],
                "barrier_f": paths[0]["barriers"]["forward"]["value"],
                "barrier_r": paths[0]["barriers"]["reverse"]["value"],
                "provenance": {
                    "parser_module": PARSER_MODULE,
                    "parser_version": PARSER_VERSION,
                    "source_ids": [paths[0]["source"]["source_id"], *[
                        item["source"].get("source_id")
                        for item in paths[0]["endpoint_evidence"].values()
                        if item["source"].get("source_id")
                    ]],
                    "file_hashes": copy.deepcopy(paths[0]["source"]["files"]),
                    "denominator": "complete ordered NEB image path",
                },
            }
    payload = {
        "schema": VIEW_SCHEMA,
        "analysis_id": spec.analysis_id,
        "spec": spec.to_dict(),
        "spec_sha256": spec.semantic_sha256,
        "scientific_status": "diagnostic",
        "capability_status": "available" if available else "missing_prerequisite",
        "available": bool(available),
        "paths": paths,
        "rows": paths,
        "missing": [] if selected else [blocking[-1]],
        "blocking": list(dict.fromkeys(blocking)),
        "warnings": list(dict.fromkeys(warnings)),
        "next_action": (
            "Review each image, convergence gate, endpoint method identity, and path boundary."
            if available else "Complete a registered NEB job and refresh."
        ),
        "denominator": {
            "resolved_targets": len(targets),
            "matching_targets": len(selected),
            "available_paths": available,
            "barrier_qualified_paths": barrier_count,
            "diagnostic_only_paths": available - barrier_count,
            "visible_rows": sum(len(path["points"]) for path in paths),
        },
    }
    return _finalize(
        payload, figure_presets=("neb_profile",), table_ids=("neb-image-table",),
        figure_data=figure_data,
    )


_SERIES_FROM_TASK = {
    "conv_encut": "encut",
    "conv_kmesh": "kmesh",
    "conv_vacuum": "vacuum",
    "conv_thickness": "slab_thickness",
}
_SERIES_UNITS = {
    "encut": "eV",
    "kmesh": "k-point product",
    "vacuum": "Å",
    "slab_thickness": "layers",
}


def _series_kind(target: Mapping[str, Any]) -> str:
    task_type = str(target.get("task_type") or "")
    if task_type in _SERIES_FROM_TASK:
        return _SERIES_FROM_TASK[task_type]
    manifest = target.get("manifest") or {}
    inputs = manifest.get("inputs") or {}
    value = str(inputs.get("series") or "")
    return value if value in _SERIES_UNITS else ""


def _series_group_key(target: Mapping[str, Any], kind: str) -> str:
    manifest = target.get("manifest") or {}
    inputs = manifest.get("inputs") or {}
    parent = str(manifest.get("parent_job") or inputs.get("parent_job") or "")
    if not parent:
        # Missing lineage never authorizes unrelated scan points to be combined.
        parent = f'isolated-source:{target.get("source_id") or "unknown"}'
    return _canonical_hash({"kind": kind, "parent": os.path.normcase(parent)})[:20]


def _series_coordinate(
    snapshot: SourceSnapshot, kind: str,
) -> tuple[float | None, list[dict[str, Any]], str]:
    """Read the scan coordinate from the current immutable input bytes."""
    if kind == "encut":
        files = snapshot.files(["job.yaml", "INCAR"])
        value = _finite(parse_incar(snapshot.text("INCAR")).get("ENCUT"))
        return value, files, "current INCAR ENCUT is unavailable" if value is None else ""
    if kind == "kmesh":
        files = snapshot.files(["job.yaml", "KPOINTS"])
        parsed = parse_kpoints_scheme(snapshot.text("KPOINTS")) or {}
        grid = parsed.get("grid")
        if (not isinstance(grid, list) or len(grid) != 3
                or any(isinstance(value, bool) or not isinstance(value, int)
                       or value <= 0 for value in grid)):
            return None, files, "current KPOINTS automatic mesh is unavailable"
        return float(math.prod(grid)), files, ""
    files = snapshot.files(["job.yaml", "POSCAR"])
    try:
        value = (vacuum_thickness(snapshot.text("POSCAR"))
                 if kind == "vacuum" else count_layers(snapshot.text("POSCAR")))
    except (IndexError, TypeError, ValueError, ZeroDivisionError):
        value = None
    finite = _finite(value)
    label = "vacuum thickness" if kind == "vacuum" else "slab layer count"
    return finite, files, f"current POSCAR {label} is unavailable" if finite is None else ""


def _coordinate_matches(kind: str, declared: float, actual: float) -> bool:
    if kind in {"kmesh", "slab_thickness"}:
        return declared == int(declared) and actual == int(actual) and declared == actual
    return math.isclose(declared, actual, rel_tol=1e-9, abs_tol=1e-6)


def _rounded_geometry(value: float) -> float:
    rounded = round(float(value), 8)
    return 0.0 if rounded == 0 else rounded


def _poscar_selective_flags(poscar_text: str, natoms: int) -> list[list[str]]:
    lines = str(poscar_text or "").splitlines()
    if len(lines) < 8:
        raise ValueError("POSCAR coordinate header is incomplete")
    index = 7
    selective = lines[index].strip()[:1].lower() == "s"
    if selective:
        index += 1
    index += 1
    if index + natoms > len(lines):
        raise ValueError("POSCAR coordinate rows are incomplete")
    flags = []
    for offset in range(natoms):
        tokens = lines[index + offset].split()
        if len(tokens) < 3:
            raise ValueError("POSCAR coordinate row is incomplete")
        if selective:
            if len(tokens) < 6:
                raise ValueError("POSCAR selective-dynamics flags are incomplete")
            flags.append([str(token).upper() for token in tokens[3:6]])
        else:
            flags.append(["T", "T", "T"])
    return flags


def _poscar_geometry(poscar_text: str) -> dict[str, Any]:
    parsed = parse_positions(poscar_text)
    elements = list(parsed.get("elements") or [])
    coordinates = list(parsed.get("coords") or [])
    cell = list(parsed.get("cell") or [])
    if not elements or len(elements) != len(coordinates) or len(cell) != 3:
        raise ValueError("POSCAR structure evidence is incomplete")
    inverse = _matrix_inverse(cell)
    if inverse is None:
        raise ValueError("POSCAR cell is singular")
    c_length = math.sqrt(sum(float(value) ** 2 for value in cell[2]))
    if not math.isfinite(c_length) or c_length <= 0:
        raise ValueError("POSCAR c vector is unavailable")
    c_direction = [float(value) / c_length for value in cell[2]]
    flags = _poscar_selective_flags(poscar_text, len(elements))
    fractional = [_row_vector(coordinate, inverse) for coordinate in coordinates]
    return {
        "elements": elements,
        "coordinates": coordinates,
        "fractional": fractional,
        "flags": flags,
        "cell": cell,
        "c_direction": c_direction,
    }


def _vacuum_normalized_structure(poscar_text: str) -> dict[str, Any]:
    geometry = _poscar_geometry(poscar_text)
    cell = geometry["cell"]
    c_direction = geometry["c_direction"]
    if math.sqrt(float(cell[2][0]) ** 2 + float(cell[2][1]) ** 2) > (
            0.05 * math.sqrt(sum(float(value) ** 2 for value in cell[2]))):
        raise ValueError("vacuum normalization requires a slab c vector along z")
    projections = [
        sum(float(coordinate[index]) * c_direction[index] for index in range(3))
        for coordinate in geometry["coordinates"]
    ]
    origin = min(projections)
    atoms = []
    for element, coordinate, projection, flags in zip(
            geometry["elements"], geometry["coordinates"], projections,
            geometry["flags"]):
        perpendicular = [
            float(coordinate[index]) - projection * c_direction[index]
            for index in range(3)
        ]
        atoms.append({
            "element": element,
            "perpendicular_a": [_rounded_geometry(value) for value in perpendicular],
            "relative_c_a": _rounded_geometry(projection - origin),
            "selective": flags,
        })
    return {
        "cell_ab_a": [
            [_rounded_geometry(value) for value in vector]
            for vector in cell[:2]
        ],
        "c_direction": [_rounded_geometry(value) for value in c_direction],
        "atoms": atoms,
    }


def _canonical_ordered_layers(
    layers: Sequence[Sequence[int]], geometry: Mapping[str, Any],
) -> list[list[list[Any]]]:
    """Normalize the whole slab with one anchor, retaining layer registration."""
    fractional = geometry["fractional"]
    c_direction = geometry["c_direction"]
    projections = [
        sum(float(coordinate[index]) * c_direction[index] for index in range(3))
        for coordinate in geometry["coordinates"]
    ]
    centers = [
        sum(projections[index] for index in indexes) / len(indexes)
        for indexes in layers
    ]
    candidates = []
    for anchor in layers[0]:
        anchor_x = float(fractional[anchor][0])
        anchor_y = float(fractional[anchor][1])
        ordered = []
        for layer_index, indexes in enumerate(layers):
            records = sorted([
                [
                    geometry["elements"][index],
                    _rounded_geometry(
                        (float(fractional[index][0]) - anchor_x) % 1.0),
                    _rounded_geometry(
                        (float(fractional[index][1]) - anchor_y) % 1.0),
                    _rounded_geometry(
                        projections[index] - centers[layer_index]),
                    geometry["flags"][index],
                ]
                for index in indexes
            ], key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True))
            ordered.append(records)
        candidates.append(ordered)
    return min(
        candidates,
        key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
    )


def _minimal_ordered_prefix_period(values: Sequence[Any]) -> list[Any]:
    sequence = list(values)
    for period in range(1, len(sequence) + 1):
        if all(sequence[index] == sequence[index % period]
               for index in range(len(sequence))):
            return copy.deepcopy(sequence[:period])
    return copy.deepcopy(sequence)


def _slab_thickness_normalized_structure(poscar_text: str) -> dict[str, Any]:
    geometry = _poscar_geometry(poscar_text)
    cell = geometry["cell"]
    c_direction = geometry["c_direction"]
    c_length = math.sqrt(sum(float(value) ** 2 for value in cell[2]))
    if math.sqrt(float(cell[2][0]) ** 2 + float(cell[2][1]) ** 2) > 0.05 * c_length:
        raise ValueError("slab-thickness normalization requires a slab c vector along z")
    projections = [
        sum(float(coordinate[index]) * c_direction[index] for index in range(3))
        for coordinate in geometry["coordinates"]
    ]
    order = sorted(range(len(projections)), key=projections.__getitem__)
    layers: list[list[int]] = []
    centers: list[float] = []
    for index in order:
        if not layers or projections[index] - projections[layers[-1][-1]] > LAYER_TOL:
            layers.append([index])
        else:
            layers[-1].append(index)
    for indexes in layers:
        centers.append(sum(projections[index] for index in indexes) / len(indexes))
    if len(layers) < 2:
        raise ValueError(
            "slab-thickness normalization requires at least two ordered layers")
    ordered_layers = _canonical_ordered_layers(layers, geometry)
    ordered_layer_hashes = [
        _canonical_hash(layer) for layer in ordered_layers
    ]
    spacings = [
        _rounded_geometry(right - left)
        for left, right in zip(centers, centers[1:])
    ]
    return {
        "cell_ab_a": [
            [_rounded_geometry(value) for value in vector]
            for vector in cell[:2]
        ],
        "c_direction": [_rounded_geometry(value) for value in c_direction],
        "vacuum_a": _rounded_geometry(vacuum_thickness(poscar_text)),
        "ordered_layer_pattern_sha256": _minimal_ordered_prefix_period(
            ordered_layer_hashes),
        "ordered_interlayer_spacing_pattern_a": _minimal_ordered_prefix_period(
            spacings),
    }


def _scan_frozen_input_invariant(
    snapshot: SourceSnapshot, kind: str,
) -> tuple[str, list[str], list[dict[str, Any]]]:
    names = ["INCAR", "KPOINTS", "POTCAR", "POSCAR"]
    files = snapshot.files(names)
    present = {str(item.get("name") or "") for item in files}
    issues = [f"current {name} evidence is unavailable" for name in names
              if name not in present]
    if issues:
        return "", issues, files
    incar_text = snapshot.text("INCAR")
    kpoints_text = snapshot.text("KPOINTS")
    if not incar_text.strip() or not kpoints_text.strip():
        return "", ["current INCAR/KPOINTS evidence is empty"], files
    incar = dict(parse_incar(incar_text))
    if kind == "encut":
        incar.pop("ENCUT", None)
    record: dict[str, Any] = {
        "kind": kind,
        "incar_except_target": incar,
        "potcar_sha256": str(
            (snapshot.file("POTCAR") or {}).get("sha256") or ""),
    }
    if kind == "kmesh":
        kpoints = parse_kpoints_scheme(kpoints_text) or {}
        shift = kpoints.get("shift")
        if (kpoints.get("scheme") not in {"Gamma", "Monkhorst-Pack"}
                or not isinstance(shift, list) or len(shift) != 3
                or any(_finite(value) is None for value in shift)):
            return "", ["current KPOINTS center/shift evidence is unavailable"], files
        record["kpoints_except_grid"] = {
            "scheme": kpoints["scheme"],
            "shift": [_rounded_geometry(value) for value in shift],
        }
    else:
        effective_kpoints = canonical_kpoints_effective_text(kpoints_text)
        if not effective_kpoints:
            return "", ["current effective KPOINTS evidence is unavailable"], files
        record["kpoints_effective"] = effective_kpoints
    poscar_file = snapshot.file("POSCAR") or {}
    try:
        if kind in {"encut", "kmesh"}:
            record["poscar_sha256"] = str(poscar_file.get("sha256") or "")
        elif kind == "vacuum":
            record["poscar_except_vacuum"] = _vacuum_normalized_structure(
                snapshot.text("POSCAR"))
        else:
            record["poscar_except_layer_count"] = _slab_thickness_normalized_structure(
                snapshot.text("POSCAR"))
        invariant = _canonical_hash(record)
    except (IndexError, TypeError, ValueError, ZeroDivisionError) as exc:
        return "", [f"current frozen-input invariant is unavailable: {exc}"], files
    return invariant, [], files


def _method_invariant(
    method: Mapping[str, Any], kind: str, *, coordinate_verified: bool,
) -> str:
    fingerprint = copy.deepcopy(method.get("fingerprint") or {})
    if not _verified_method(method) or not isinstance(fingerprint, Mapping):
        return ""
    fingerprint = dict(fingerprint)
    if "identity" in fingerprint:
        identity = fingerprint.get("identity")
        fingerprint_schema = str(fingerprint.get("schema") or "")
        if (
            not isinstance(identity, Mapping)
            or not fingerprint_schema
            or fingerprint_schema != str(method.get("schema") or "")
        ):
            return ""
        fingerprint = copy.deepcopy(dict(identity))
    required = {
        "functional", "dispersion", "encut", "spin",
        "kpoints_scheme", "potcar_ids",
    }
    if kind == "encut" and coordinate_verified:
        fingerprint.pop("encut", None)
        required.remove("encut")
    elif kind == "kmesh" and coordinate_verified:
        fingerprint.pop("kpoints_scheme", None)
        required.remove("kpoints_scheme")
    if any(key not in fingerprint or fingerprint[key] in (None, "", {}, [])
           for key in required):
        return ""
    return _canonical_hash(fingerprint)


def _strict_authority_invariants(
    method: Mapping[str, Any],
) -> tuple[dict[str, str], list[str]]:
    """Validate the path-free strict authority projection used by convergence."""
    authority = method.get("strict_authority")
    if not isinstance(authority, Mapping):
        return {}, ["strict scientific fingerprint authority is unavailable"]
    issues = []
    if (authority.get("schema") != FINGERPRINT_SCHEMA
            or authority.get("status") != "complete"
            or not _HEX64_RE.fullmatch(str(authority.get("fingerprint_sha256") or ""))):
        issues.append("strict scientific fingerprint is incomplete")
    if any(authority.get(key) for key in (
            "missing", "integrity_issues", "binding_issues")):
        issues.append("strict scientific fingerprint authority has unresolved issues")
    closure = authority.get("input_closure") or {}
    closure_sha = str(closure.get("component_sha256") or "")
    if (closure.get("schema") != INPUT_CLOSURE_SCHEMA
            or closure.get("status") != "complete"
            or not _HEX64_RE.fullmatch(closure_sha)):
        issues.append("scientific input-closure authority is incomplete")
    recipe = authority.get("method_recipe") or {}
    if (recipe.get("schema") != METHOD_RECIPE_SCHEMA
            or recipe.get("authority") != METHOD_RECIPE_AUTHORITY
            or not _HEX64_RE.fullmatch(str(recipe.get("semantic_sha256") or ""))):
        issues.append("method-recipe authority is incomplete")
    environment = authority.get("execution_environment") or {}
    environment_sha = str(environment.get("component_sha256") or "")
    build_sha = str(environment.get("build_identity_sha256") or "")
    if (environment.get("schema") != EXECUTION_ENVIRONMENT_SCHEMA
            or environment.get("authority") != EXECUTION_ENVIRONMENT_AUTHORITY
            or environment.get("engine") != "vasp"
            or not str(environment.get("vasp_version") or "")
            or not _HEX64_RE.fullmatch(build_sha)
            or not str(environment.get("evidence_kind") or "")
            or not _HEX64_RE.fullmatch(str(environment.get("evidence_sha256") or ""))
            or not _HEX64_RE.fullmatch(environment_sha)):
        issues.append("execution-environment authority is incomplete")
    if issues:
        return {}, issues
    return {
        "input_closure": closure_sha,
        "execution_environment": environment_sha,
        "build_identity": build_sha,
    }, []


def _merge_files(*groups: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    merged = {}
    for group in groups:
        for item in group:
            key = (str(item.get("name") or ""), str(item.get("sha256") or ""))
            merged[key] = dict(item)
    return list(merged.values())


def _runtime_natoms(
    snapshot: SourceSnapshot, manifest: Mapping[str, Any],
) -> tuple[int | None, list[str], list[dict[str, Any]]]:
    """Cross-check runtime, frozen POSCAR, manifest count, and frozen hash."""
    issues = []
    runtime_values: dict[str, int] = {}
    nions = {int(value) for value in _NIONS_RE.findall(snapshot.text("OUTCAR"))}
    if len(nions) == 1:
        runtime_values["OUTCAR:NIONS"] = next(iter(nions))
    elif len(nions) > 1:
        issues.append("OUTCAR contains inconsistent NIONS values")
    atoms = {int(value) for value in _VASPRUN_ATOMS_RE.findall(
        snapshot.text("vasprun.xml"))}
    if len(atoms) == 1:
        runtime_values["vasprun.xml:atoms"] = next(iter(atoms))
    elif len(atoms) > 1:
        issues.append("vasprun.xml contains inconsistent atom counts")
    if not runtime_values:
        issues.append("runtime atom count is absent from OUTCAR/vasprun.xml")
    elif len(set(runtime_values.values())) != 1:
        issues.append("OUTCAR and vasprun.xml atom counts disagree")

    poscar_natoms = None
    try:
        _symbols, counts = parse_poscar_species(snapshot.text("POSCAR"))
        if counts and sum(counts) > 0:
            poscar_natoms = int(sum(counts))
    except (IndexError, TypeError, ValueError):
        pass
    if poscar_natoms is None:
        issues.append("frozen POSCAR atom count is unavailable")

    inputs = manifest.get("inputs") or {}
    manifest_natoms = inputs.get("natoms")
    if (isinstance(manifest_natoms, bool)
            or not isinstance(manifest_natoms, int) or manifest_natoms <= 0):
        issues.append("manifest inputs.natoms is unavailable")
        manifest_natoms = None
    recorded_hashes = inputs.get("sha256") or {}
    recorded_poscar = str(
        recorded_hashes.get("POSCAR") if isinstance(recorded_hashes, Mapping) else ""
    ).lower()
    poscar_file = snapshot.file("POSCAR")
    if (not re.fullmatch(r"[0-9a-f]{64}", recorded_poscar)
            or not poscar_file or poscar_file.get("sha256") != recorded_poscar):
        issues.append("frozen POSCAR hash does not match manifest inputs.sha256")

    runtime_natoms = next(iter(runtime_values.values()), None)
    values = [runtime_natoms, poscar_natoms, manifest_natoms]
    if all(value is not None for value in values) and len(set(values)) != 1:
        issues.append("runtime, POSCAR, and manifest atom counts disagree")
    files = snapshot.files(["job.yaml", "POSCAR", "OUTCAR", "vasprun.xml"])
    return (int(runtime_natoms) if not issues and runtime_natoms is not None else None,
            issues, files)


def _platform(
    points: Sequence[Mapping[str, Any]], threshold: float,
) -> tuple[list[int], int | None]:
    valid = [
        index for index, point in enumerate(points)
        if point["delta_per_atom"]["value"] is not None
        and abs(point["delta_per_atom"]["value"]) <= threshold + 1e-12
    ]
    suffix = []
    for index in range(len(points) - 1, -1, -1):
        if index not in valid:
            break
        suffix.append(index)
    suffix.reverse()
    recommended = suffix[0] if len(suffix) >= CONVERGENCE_MIN_PLATFORM_POINTS else None
    return suffix, recommended


def _convergence_series(
    spec: AnalysisSpec, kind: str, targets: Sequence[Mapping[str, Any]],
    method_evidence: Callable[..., Mapping[str, Any]],
    snapshot_budget: SourceSnapshotBudget,
) -> dict[str, Any]:
    parser = _parser("build_convergence_analysis_view")
    raw = []
    issues = []
    method_invariants = set()
    closure_invariants = set()
    environment_invariants = set()
    build_invariants = set()
    frozen_input_invariants = set()
    snapshots = []
    for target in targets:
        manifest_hint = target.get("manifest") or {}
        inputs_hint = manifest_hint.get("inputs") or {}
        closure_hint = inputs_hint.get("input_closure") or {}
        closure_files = closure_hint.get("files") or {}
        evidence_names = [
            "OSZICAR", "OUTCAR", "vasprun.xml", "INCAR", "KPOINTS",
            "POSCAR", "CONTCAR", "POTCAR",
        ]
        if isinstance(closure_files, Mapping):
            evidence_names.extend(str(name) for name in closure_files)
        snapshot, manifest = _capture(
            target, evidence_names, budget=snapshot_budget)
        snapshots.append(snapshot)
        inputs = manifest.get("inputs") or {}
        declared_x = _finite(inputs.get("series_value"))
        label = str(inputs.get("series_label") or "")
        source = snapshot.identity()
        snapshot_issues = snapshot.evidence_issues()
        issues.extend(
            f'{source["source_id"]}: {issue}' for issue in snapshot_issues)
        frozen_input_invariant, frozen_issues, frozen_files = (
            _scan_frozen_input_invariant(snapshot, kind))
        issues.extend(
            f'{source["source_id"]}: {issue}' for issue in frozen_issues)
        if frozen_input_invariant:
            frozen_input_invariants.add(frozen_input_invariant)
        x, coordinate_files, coordinate_issue = _series_coordinate(snapshot, kind)
        coordinate_verified = bool(
            declared_x is not None and x is not None
            and _coordinate_matches(kind, declared_x, x)
        )
        if coordinate_issue:
            issues.append(f'{source["source_id"]}: {coordinate_issue}')
        elif declared_x is None:
            issues.append(
                f'{source["source_id"]}: explicit series_value is unavailable')
        elif not coordinate_verified:
            issues.append(
                f'{source["source_id"]}: manifest series_value {declared_x:g} does not '
                f'match the current input value {x:g}')
        method = _method_evidence(method_evidence, target, snapshot)
        invariant = _method_invariant(
            method, kind, coordinate_verified=coordinate_verified)
        strict_invariants, strict_issues = _strict_authority_invariants(method)
        point_method_verified = bool(
            _verified_method(method) and invariant and coordinate_verified
            and strict_invariants and not strict_issues)
        issues.extend(
            f'{source["source_id"]}: {issue}' for issue in strict_issues)
        if not point_method_verified:
            issues.append(
                f'{source["source_id"]}: method evidence/invariant is incomplete')
        else:
            method_invariants.add(invariant)
            closure_invariants.add(strict_invariants["input_closure"])
            environment_invariants.add(strict_invariants["execution_environment"])
            build_invariants.add(strict_invariants["build_identity"])
        natoms, natom_issues, denominator_files = _runtime_natoms(snapshot, manifest)
        issues.extend(f'{source["source_id"]}: {issue}' for issue in natom_issues)
        try:
            if snapshot.rejected:
                raise ValueError("bounded source evidence is unavailable")
            energy, _validated_manifest, _energy_evidence = validate_done_energy_evidence(
                manifest, source["source_id"],
                oszicar_text=snapshot.text("OSZICAR"),
                outcar_text=snapshot.text("OUTCAR"),
                vasprun_text=snapshot.text("vasprun.xml"),
                require_oszicar=True, require_current_completion=True,
                require_outcar_ionic_event=True, reject_explicit_unclean=True,
            )
        except ValueError as exc:
            energy = None
            issues.append(f'{source["source_id"]}: {exc}')
        energy_files = snapshot.files(["job.yaml", "OSZICAR", "OUTCAR", "vasprun.xml"])
        raw.append({
            "x": x,
            "declared_x": declared_x,
            "coordinate_verified": coordinate_verified,
            "coordinate_files": coordinate_files,
            "frozen_input_invariant": frozen_input_invariant,
            "frozen_input_files": frozen_files,
            "label": label or (str(x) if x is not None else "—"),
            "source": source,
            "method": method,
            "natoms": natoms,
            "energy": energy,
            "energy_files": energy_files,
            "denominator_files": denominator_files,
            "method_verified": point_method_verified,
        })
    raw.sort(key=lambda item: (item["x"] is None, item["x"] or 0.0, item["label"]))
    duplicates = {
        item["x"] for item in raw if item["x"] is not None
        and sum(other["x"] == item["x"] for other in raw) > 1
    }
    if duplicates:
        issues.append("duplicate series coordinates are present")
    if len(method_invariants) > 1:
        issues.append("non-target method settings differ across convergence points")
    if len(closure_invariants) > 1:
        issues.append("scientific input-closure authority differs across convergence points")
    if len(environment_invariants) > 1:
        issues.append("execution environments differ across convergence points")
    if len(build_invariants) > 1:
        issues.append("VASP build identities differ across convergence points")
    if len(frozen_input_invariants) > 1:
        issues.append(
            "non-target frozen inputs/structure differ across convergence points")
    if raw and (not all(item["frozen_input_invariant"] for item in raw)
                or len(frozen_input_invariants) != 1):
        issues.append(
            "every convergence point must share one verified frozen-input invariant")
    if raw and not all(item["method_verified"] for item in raw):
        issues.append("every convergence point must have a complete verified method invariant")
    reference = (
        raw[-1] if raw and raw[-1]["energy"] is not None
        and raw[-1]["natoms"] is not None else None
    )
    points = []
    for item in raw:
        per_atom = (
            item["energy"] / item["natoms"]
            if item["energy"] is not None and item["natoms"] else None
        )
        reference_per_atom = (
            reference["energy"] / reference["natoms"] if reference else None
        )
        delta = (
            (per_atom - reference_per_atom) * 1000.0
            if per_atom is not None and reference_per_atom is not None else None
        )
        source = item["source"]
        files = item["energy_files"]
        denominator_files = item["denominator_files"]
        normalized_files = _merge_files(files, denominator_files)
        delta_files = _merge_files(
            normalized_files,
            reference["energy_files"] if reference else [],
            reference["denominator_files"] if reference else [],
        )
        points.append({
            "label": item["label"],
            "source": source,
            "method": item["method"],
            "method_verified": item["method_verified"],
            "coordinate_verified": item["coordinate_verified"],
            "frozen_input_evidence": {
                "status": ("verified" if item["frozen_input_invariant"]
                           else "unavailable"),
                "invariant_sha256": item["frozen_input_invariant"],
                "file_hashes": copy.deepcopy(item["frozen_input_files"]),
            },
            "parameter": _quantity(
                key="parameter", label="Scan parameter", value=item["x"],
                unit=_SERIES_UNITS[kind], precision=spec.precision,
                denominator="current input field matched to manifest series_value",
                source_id=source["source_id"], files=item["coordinate_files"], parser=parser,
                reason="current scan coordinate is unavailable" if item["x"] is None else "",
            ),
            "declared_parameter": _quantity(
                key="declared_parameter", label="Declared scan parameter",
                value=item["declared_x"], unit=_SERIES_UNITS[kind],
                precision=spec.precision, denominator="job.yaml inputs.series_value",
                source_id=source["source_id"],
                files=[file for file in item["coordinate_files"]
                       if file.get("name") == "job.yaml"],
                parser=parser,
                reason="manifest series_value is unavailable"
                if item["declared_x"] is None else "",
            ),
            "absolute_energy": _quantity(
                key="absolute_energy", label="Absolute energy", value=item["energy"],
                unit="eV", precision=spec.precision, denominator="per calculation cell",
                source_id=source["source_id"], files=files, parser=parser,
                reason="DONE OSZICAR:E0 evidence is unavailable"
                if item["energy"] is None else "",
            ),
            "atom_count": _quantity(
                key="atom_count", label="Verified atom denominator",
                value=item["natoms"], unit="atoms", precision=0,
                denominator="OUTCAR/vasprun runtime = frozen POSCAR = manifest",
                source_id=source["source_id"], files=denominator_files,
                parser=parser,
                reason="atom-count evidence is unavailable"
                if item["natoms"] is None else "",
            ),
            "energy_per_atom": _quantity(
                key="energy_per_atom", label="Energy per atom", value=per_atom,
                unit="eV/atom", precision=spec.precision,
                denominator=f'{int(item["natoms"])} atoms' if item["natoms"] else "unavailable",
                source_id=source["source_id"], files=normalized_files, parser=parser,
                reason="atom-count denominator is unavailable" if per_atom is None else "",
            ),
            "delta_per_atom": _quantity(
                key="delta_per_atom", label="Difference from terminal reference",
                value=delta, unit="meV/atom", precision=spec.precision,
                denominator=(
                    f'{int(item["natoms"])} atoms; reference={reference["label"]}'
                    if item["natoms"] and reference else "unavailable"
                ),
                source_id=source["source_id"], files=delta_files, parser=parser,
                reason="terminal reference or atom denominator is unavailable"
                if delta is None else "",
            ),
            "anomalies": (["duplicate coordinate"] if item["x"] in duplicates else [])
            + (["declared coordinate does not match current input"]
               if not item["coordinate_verified"] else [])
            + (["missing energy"] if item["energy"] is None else []),
        })

    sensitivity = []
    primary_suffix = []
    primary_index = None
    series_evidence_ready = bool(
        not duplicates and len(method_invariants) == 1
        and len(closure_invariants) == 1
        and len(environment_invariants) == 1
        and len(build_invariants) == 1
        and len(frozen_input_invariants) == 1
        and not any(snapshot.rejected for snapshot in snapshots)
        and all(item["frozen_input_invariant"] for item in raw)
        and all(item["method_verified"] for item in raw)
        and all(point["parameter"]["value"] is not None
                and point["absolute_energy"]["value"] is not None
                and point["energy_per_atom"]["value"] is not None
                for point in points)
    )
    for threshold in CONVERGENCE_THRESHOLDS_MEV_PER_ATOM:
        suffix, recommended = _platform(points, threshold)
        qualified_suffix = suffix if series_evidence_ready else []
        qualified_recommended = recommended if series_evidence_ready else None
        if threshold == CONVERGENCE_PRIMARY_THRESHOLD_MEV_PER_ATOM:
            primary_suffix, primary_index = qualified_suffix, qualified_recommended
        sensitivity.append({
            "threshold": _quantity(
                key="threshold", label="Platform threshold", value=threshold,
                unit="meV/atom", precision=spec.precision,
                denominator=f"at least {CONVERGENCE_MIN_PLATFORM_POINTS} contiguous terminal points",
                source_id=(points[-1]["source"]["source_id"] if points else ""),
                files=(points[-1]["source"]["files"] if points else []), parser=parser,
            ),
            "recommended_parameter": (
                copy.deepcopy(points[recommended]["parameter"])
                if qualified_recommended is not None else _quantity(
                    key="recommended_parameter", label="Recommended parameter",
                    value=None, unit=_SERIES_UNITS[kind], precision=spec.precision,
                    denominator=f"threshold={threshold:g} meV/atom",
                    source_id=(points[-1]["source"]["source_id"] if points else ""),
                    files=(points[-1]["source"]["files"] if points else []), parser=parser,
                    reason=(issues[0] if issues else
                            "insufficient contiguous terminal platform evidence"),
                )
            ),
            "platform_point_indexes": qualified_suffix,
        })
    recommendation_available = bool(
        primary_index is not None and series_evidence_ready
    )
    primary_members = set(primary_suffix)
    for index, point in enumerate(points):
        value = point["delta_per_atom"]["value"]
        point["platform_member"] = index in primary_members
        if (value is not None and index not in primary_members
                and abs(value) <= CONVERGENCE_PRIMARY_THRESHOLD_MEV_PER_ATOM + 1e-12):
            point["anomalies"].append("non-terminal threshold crossing")
    recommendation = (
        copy.deepcopy(points[primary_index]["parameter"])
        if recommendation_available and primary_index is not None else _quantity(
            key="recommended_parameter", label="Recommended parameter", value=None,
            unit=_SERIES_UNITS[kind], precision=spec.precision,
            denominator=(
                f"threshold={CONVERGENCE_PRIMARY_THRESHOLD_MEV_PER_ATOM:g} meV/atom; "
                f"minimum {CONVERGENCE_MIN_PLATFORM_POINTS} contiguous terminal points"
            ),
            source_id=(points[-1]["source"]["source_id"] if points else ""),
            files=(points[-1]["source"]["files"] if points else []), parser=parser,
            reason=(issues[0] if issues else "insufficient platform evidence"),
        )
    )
    figure_points = [
        {
            "x": point["parameter"]["value"],
            # conv_plot receives already-normalized eV/atom values and natoms=1;
            # it does not get to choose or reconstruct the scientific denominator.
            "energy": point["energy_per_atom"]["value"],
        }
        for point in points
        if (_finite(point["parameter"]["value"]) is not None
            and _finite(point["energy_per_atom"]["value"]) is not None)
    ]
    return {
        "series_id": _canonical_hash({
            "kind": kind,
            "source_ids": [point["source"]["source_id"] for point in points],
        })[:24],
        "kind": kind,
        "status": (
            "unavailable" if any(snapshot.rejected for snapshot in snapshots)
            else "available" if points else "missing_prerequisite"),
        "available": bool(points and not any(
            snapshot.rejected for snapshot in snapshots)),
        "parser": parser,
        "points": points,
        "platform": {
            "status": "available" if recommendation_available else "unavailable",
            "threshold_mev_per_atom": CONVERGENCE_PRIMARY_THRESHOLD_MEV_PER_ATOM,
            "minimum_points": CONVERGENCE_MIN_PLATFORM_POINTS,
            "point_indexes": primary_suffix,
            "recommendation": recommendation,
        },
        "sensitivity": sensitivity,
        "issues": list(dict.fromkeys(issues)),
        "figure_data": {
            "points": figure_points,
            "converged_at": recommendation["value"],
            "threshold_mev": CONVERGENCE_PRIMARY_THRESHOLD_MEV_PER_ATOM,
            "natoms": 1,
            "xlabel": kind,
            "provenance": {
                "parser_module": PARSER_MODULE,
                "parser_version": PARSER_VERSION,
                "source_ids": [
                    point["source"]["source_id"] for point in points
                ],
                "file_hashes": [
                    file for point in points for file in point["source"]["files"]
                ],
                "denominator": "server-finalized eV/atom points",
            },
        },
        "scientific_boundary": (
            "The platform is defined only by the declared meV/atom threshold and "
            "the terminal reference point. It is not a threshold-free proof of convergence; "
            "a slab-thickness per-atom plateau does not replace a bulk-referenced surface-"
            "energy analysis."
        ),
        "_snapshots": snapshots,
    }


def build_convergence_analysis_view(
    spec: AnalysisSpec, targets: Sequence[Mapping[str, Any]], *,
    method_evidence: Callable[..., Mapping[str, Any]],
) -> dict[str, Any]:
    if spec.analysis_id != "convergence-scan":
        raise ValueError(
            "convergence analysis view requires analysis_id=convergence-scan")
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    unsupported = []
    for target in targets:
        kind = _series_kind(target)
        if not kind:
            if str(target.get("task_type") or "").startswith("conv"):
                unsupported.append(str(target.get("source_id") or "unknown"))
            continue
        key = (kind, _series_group_key(target, kind))
        groups.setdefault(key, []).append(target)
    snapshot_budget = SourceSnapshotBudget()
    series = [
        _convergence_series(
            spec, kind, members, method_evidence, snapshot_budget)
        for (kind, _group), members in sorted(groups.items())
    ]
    for item in series:
        for snapshot in item.pop("_snapshots", []):
            snapshot.assert_unchanged()
    blocking = [issue for item in series for issue in item["issues"]]
    if unsupported:
        blocking.append(
            "Convergence jobs without an explicit recognized series are not implemented: "
            + ", ".join(unsupported)
        )
    if not series:
        blocking.append("No registered convergence scan series was resolved")
    available = sum(item["available"] is True for item in series)
    recommendations = sum(
        item["platform"]["status"] == "available" for item in series
    )
    figure_data = {}
    if (len(series) == 1
            and len(series[0]["figure_data"].get("points") or []) >= 2):
        figure_data["convergence_curve"] = copy.deepcopy(series[0]["figure_data"])
    payload = {
        "schema": VIEW_SCHEMA,
        "analysis_id": spec.analysis_id,
        "spec": spec.to_dict(),
        "spec_sha256": spec.semantic_sha256,
        "scientific_status": "diagnostic",
        "capability_status": "available" if available else "missing_prerequisite",
        "available": bool(available),
        "series": series,
        "rows": series,
        "missing": [] if series else [blocking[-1]],
        "blocking": list(dict.fromkeys(blocking)),
        "warnings": [],
        "next_action": (
            "Inspect raw points, missing values, platform threshold, and sensitivity."
            if available else "Complete an explicit ENCUT, k-mesh, vacuum, or slab-thickness series."
        ),
        "denominator": {
            "resolved_targets": len(targets),
            "matching_targets": sum(len(members) for members in groups.values()),
            "scan_series": len(series),
            "raw_points": sum(len(item["points"]) for item in series),
            "available_recommendations": recommendations,
            "unavailable_recommendations": len(series) - recommendations,
            "visible_rows": sum(len(item["points"]) for item in series),
        },
    }
    return _finalize(
        payload, figure_presets=("convergence_curve",),
        table_ids=("convergence-points-table",), figure_data=figure_data,
    )


_XDATCAR_CONFIG_RE = re.compile(
    r"^[ \t]*(?:Direct|Cartesian)[ \t]+configuration[ \t]*=[ \t]*(\d+)[ \t]*$",
    re.I,
)


def _cell_volume(cell: Sequence[Sequence[float]]) -> float:
    a, b, c = cell
    return abs(
        a[0] * (b[1] * c[2] - b[2] * c[1])
        - a[1] * (b[0] * c[2] - b[2] * c[0])
        + a[2] * (b[0] * c[1] - b[1] * c[0])
    )


def _parse_xdatcar(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    if len(lines) < 8:
        return {"frames": [], "natoms": None, "cell": None,
                "error": "XDATCAR header is incomplete"}
    try:
        scale = float(lines[1].split()[0])
        cell_tokens = [lines[index].split() for index in range(2, 5)]
        if (not math.isfinite(scale) or scale == 0
                or any(len(row) != 3 for row in cell_tokens)):
            raise ValueError("invalid cell")
        cell = [[float(value) * scale for value in row]
                for row in cell_tokens]
        if any(not math.isfinite(value) for row in cell for value in row):
            raise ValueError("non-finite cell")
        symbols = lines[5].split()
        counts = [int(value) for value in lines[6].split()]
    except (IndexError, TypeError, ValueError):
        return {"frames": [], "frame_steps": [], "elements": [],
                "natoms": None, "cell": None,
                "error": "XDATCAR cell or atom counts are invalid"}
    if (not symbols or len(symbols) != len(counts)
            or any(count <= 0 for count in counts)):
        return {"frames": [], "frame_steps": [], "elements": [],
                "natoms": None, "cell": None,
                "error": "XDATCAR elements or atom counts are invalid"}
    natoms = sum(counts)
    if natoms <= 0 or _matrix_inverse(cell) is None:
        return {"frames": [], "frame_steps": [], "elements": [],
                "natoms": None, "cell": None,
                "error": "XDATCAR atom count or cell is invalid"}
    elements = [symbol for symbol, count in zip(symbols, counts)
                for _index in range(count)]
    frames = []
    frame_steps = []
    index = 7
    while index < len(lines):
        while index < len(lines) and not lines[index].strip():
            index += 1
        if index >= len(lines):
            break
        config_match = _XDATCAR_CONFIG_RE.match(lines[index])
        if not config_match:
            return {
                "frames": frames, "frame_steps": frame_steps,
                "elements": elements, "natoms": natoms, "cell": cell,
                "error": (
                    "XDATCAR contains a repeated/variable-cell header or unexpected "
                    "content; fixed-cell structure metrics are unavailable"),
            }
        mode = lines[index].strip().lower()
        frame_step = int(config_match.group(1))
        index += 1
        coords = []
        for _atom in range(natoms):
            if index >= len(lines):
                return {"frames": frames, "frame_steps": frame_steps,
                        "elements": elements, "natoms": natoms, "cell": cell,
                        "error": "XDATCAR final frame is truncated"}
            try:
                tokens = lines[index].split()
                if len(tokens) != 3:
                    raise ValueError("coordinate arity")
                coordinate = [float(value) for value in tokens]
                if any(not math.isfinite(value) for value in coordinate):
                    raise ValueError("non-finite coordinate")
                coords.append(coordinate)
            except (TypeError, ValueError):
                return {"frames": frames, "frame_steps": frame_steps,
                        "elements": elements, "natoms": natoms, "cell": cell,
                        "error": "XDATCAR coordinate is invalid"}
            index += 1
        if mode.startswith("cartesian"):
            inverse = _matrix_inverse(cell)
            coords = [
                _row_vector([value * scale for value in coord], inverse)
                for coord in coords
            ]
        frames.append(coords)
        frame_steps.append(frame_step)
    return {
        "frames": frames, "frame_steps": frame_steps, "elements": elements,
        "natoms": natoms, "cell": cell, "error": "",
    }


def _trajectory_metrics(text: str) -> dict[str, Any]:
    parsed = _parse_xdatcar(text)
    frames = parsed["frames"]
    natoms = parsed["natoms"]
    cell = parsed["cell"]
    if not frames or not natoms or not cell:
        return {
            "frame_count": len(frames), "frame_steps": parsed.get("frame_steps") or [],
            "elements": parsed.get("elements") or [], "cell": cell, "natoms": natoms,
            "max_step_displacement_a": None, "final_rmsd_a": None,
            "max_rmsd_a": None, "cell_volume_a3": None,
            "error": parsed["error"] or "XDATCAR contains no complete trajectory frames",
        }
    if len(frames) < 2:
        return {
            "frame_count": len(frames), "frame_steps": parsed["frame_steps"],
            "elements": parsed["elements"], "cell": cell, "natoms": natoms,
            "max_step_displacement_a": None, "final_rmsd_a": None,
            "max_rmsd_a": None, "cell_volume_a3": _cell_volume(cell),
            "error": (parsed["error"] or
                      "XDATCAR requires at least two bound frames for structure metrics"),
        }
    unwrapped = [[list(atom) for atom in frames[0]]]
    max_step = 0.0
    for previous, current in zip(frames, frames[1:]):
        next_frame = []
        previous_unwrapped = unwrapped[-1]
        for atom_index, (left, right) in enumerate(zip(previous, current)):
            delta = [right[i] - left[i] for i in range(3)]
            delta = [value - round(value) for value in delta]
            cart = _row_vector(delta, cell)
            max_step = max(max_step, math.sqrt(sum(value * value for value in cart)))
            next_frame.append([
                previous_unwrapped[atom_index][i] + delta[i] for i in range(3)
            ])
        unwrapped.append(next_frame)
    rmsds = []
    initial = unwrapped[0]
    for frame in unwrapped:
        displacements = [
            _row_vector([frame[index][i] - initial[index][i] for i in range(3)], cell)
            for index in range(natoms)
        ]
        centroid = [
            sum(vector[i] for vector in displacements) / natoms for i in range(3)
        ]
        squared = sum(
            sum((vector[i] - centroid[i]) ** 2 for i in range(3))
            for vector in displacements
        )
        rmsds.append(math.sqrt(squared / natoms))
    return {
        "frame_count": len(frames), "frame_steps": parsed["frame_steps"],
        "elements": parsed["elements"], "cell": cell, "natoms": natoms,
        "max_step_displacement_a": max_step if len(frames) > 1 else 0.0,
        "final_rmsd_a": rmsds[-1], "max_rmsd_a": max(rmsds),
        "cell_volume_a3": _cell_volume(cell), "error": parsed["error"],
    }


def _linear_slope(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    denominator = sum((value - mean_x) ** 2 for value in xs)
    if denominator <= 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator


def _aimd_segments(raw_steps: Sequence[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    segments: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    for item in raw_steps:
        step = item.get("step")
        if (not isinstance(step, int) or isinstance(step, bool) or step <= 0):
            return []
        if current and step != current[-1]["step"] + 1:
            segments.append(current)
            current = []
        current.append(item)
    if current:
        segments.append(current)
    return segments


def _explicit_restart_lineage(inputs: Mapping[str, Any]) -> bool:
    lineage = inputs.get("restart_lineage")
    if isinstance(lineage, (list, tuple)) and lineage:
        return all(isinstance(item, Mapping) and item.get("source_job_id")
                   for item in lineage)
    return any(str(inputs.get(key) or "").strip() for key in (
        "restart_from_job_id", "restart_parent_job_id",
    ))


def _aimd_trajectory(
    spec: AnalysisSpec, target: Mapping[str, Any],
    method_evidence: Callable[..., Mapping[str, Any]],
    snapshot_budget: SourceSnapshotBudget,
) -> dict[str, Any]:
    from vcstudio.generate.aimd_builder import parse_aimd_energy

    parser = _parser("build_aimd_analysis_view")
    snapshot, manifest = _capture(target, [
        "INCAR", "KPOINTS", "POTCAR", "POSCAR", "CONTCAR",
        "OSZICAR", "XDATCAR",
    ], budget=snapshot_budget)
    source = snapshot.identity()
    inputs = manifest.get("inputs") or {}
    incar = parse_incar(snapshot.text("INCAR"))
    potim = _finite(incar.get("POTIM"))
    declared_steps = _finite(incar.get("NSW"))
    declared_steps_int = (
        int(declared_steps) if declared_steps is not None
        and declared_steps == int(declared_steps) else None
    )
    raw_nblock = incar.get("NBLOCK", 1)
    finite_nblock = _finite(raw_nblock)
    nblock = (
        int(finite_nblock) if finite_nblock is not None
        and finite_nblock == int(finite_nblock) and finite_nblock > 0 else None
    )
    declared_temperature = _finite(incar.get("TEBEG"))
    issues = []
    warnings = []
    evidence_blocked = bool(snapshot.rejected)
    issues.extend(
        f'{source["source_id"]}: {issue}' for issue in snapshot.evidence_issues())
    if potim is None or potim <= 0:
        issues.append("AIMD requires an explicit positive POTIM in INCAR")
    if declared_steps_int is None or declared_steps_int <= 0:
        issues.append("AIMD requires an explicit positive NSW in INCAR")
    if nblock is None:
        issues.append("AIMD requires a positive integer NBLOCK (default 1)")
    for key, actual in (
        ("potim_fs", potim), ("steps", declared_steps),
        ("temp_k", declared_temperature),
    ):
        recorded = _finite(inputs.get(key))
        if recorded is not None and actual is not None and abs(recorded - actual) > 1e-9:
            issues.append(f"manifest {key} differs from the current INCAR evidence")
    parsed = parse_aimd_energy(
        snapshot.text("OSZICAR"), potim_fs=potim if potim else 1.0)
    raw_steps = (
        list(parsed.get("steps") or []) if not evidence_blocked else [])
    if not raw_steps:
        issues.append("OSZICAR contains no parseable AIMD total-energy/temperature steps")
    segments = _aimd_segments(raw_steps)
    valid_steps = bool(raw_steps and segments)
    segmented = len(segments) > 1
    if raw_steps and not valid_steps:
        issues.append("AIMD step identifiers must be positive integers")
    if segmented:
        lineage = _explicit_restart_lineage(inputs)
        issues.append(
            "AIMD step sequence has a restart, regression, or gap; aggregate diagnostics "
            + ("remain unavailable because this parser does not stitch restart snapshots"
               if lineage else "are unavailable without explicit restart lineage"))
    state_done = manifest.get("state") == "DONE"
    coverage_ok = bool(
        valid_steps and not segmented and declared_steps_int is not None
        and raw_steps[0]["step"] == 1
        and raw_steps[-1]["step"] == declared_steps_int
        and len(raw_steps) == declared_steps_int
    )
    if state_done and not coverage_ok:
        issues.append(
            "DONE AIMD evidence does not cover every declared NSW step from 1 through NSW")
    aggregate_ready = bool(
        valid_steps and not segmented and (not state_done or coverage_ok))
    energies = [float(item["e_tot"]) for item in raw_steps]
    temperatures = [float(item["temp_k"]) for item in raw_steps]
    sampling_length_ps = (
        len(raw_steps) * potim / 1000.0
        if aggregate_ready and potim is not None and potim > 0 else None
    )
    if sampling_length_ps is not None and sampling_length_ps < AIMD_SHORT_TRAJECTORY_PS:
        warnings.append(
            f"trajectory length {sampling_length_ps:.6g} ps is below the explicit "
            f"{AIMD_SHORT_TRAJECTORY_PS:g} ps short-trajectory diagnostic threshold"
        )
    drift_total = (
        energies[-1] - energies[0]
        if energies and aggregate_ready else None
    )
    slope_ev_ps = _linear_slope(
        [float(item["step"]) * potim / 1000.0 for item in raw_steps], energies,
    ) if aggregate_ready and potim is not None and potim > 0 else None
    structure = _trajectory_metrics(
        snapshot.text("XDATCAR") if not evidence_blocked else "")
    structure_issues = []
    if structure["error"]:
        structure_issues.append(str(structure["error"]))
    expected_frame_steps = (
        [int(item["step"]) for item in raw_steps if item["step"] % nblock == 0]
        if valid_steps and nblock is not None else []
    )
    if list(structure.get("frame_steps") or []) != expected_frame_steps:
        structure_issues.append(
            "XDATCAR configuration sequence does not match OSZICAR steps selected by "
            f"NBLOCK={nblock if nblock is not None else 'unavailable'}")
    try:
        poscar = parse_positions(snapshot.text("POSCAR"))
    except (IndexError, TypeError, ValueError) as exc:
        structure_issues.append(f"POSCAR structure evidence is unavailable: {exc}")
        poscar = {"elements": [], "coords": [], "cell": []}
    if (structure.get("natoms") != len(poscar.get("elements") or [])
            or list(structure.get("elements") or [])
            != list(poscar.get("elements") or [])):
        structure_issues.append(
            "XDATCAR atom count/elements do not match the frozen POSCAR")
    if (not structure.get("cell") or not poscar.get("cell")
            or not _same_cell(structure["cell"], poscar["cell"])):
        structure_issues.append("XDATCAR cell does not match the frozen POSCAR")
    if segmented:
        structure_issues.append(
            "XDATCAR frames cannot be mapped across restart/gap segments")
    if state_done and not coverage_ok:
        structure_issues.append(
            "DONE OSZICAR/NSW coverage is incomplete, so XDATCAR cannot be qualified")
    structure_ready = bool(not evidence_blocked and not structure_issues)
    if not structure_ready:
        structure["max_step_displacement_a"] = None
        structure["final_rmsd_a"] = None
        structure["max_rmsd_a"] = None
    issues.extend(structure_issues)
    method = _method_evidence(method_evidence, target, snapshot)
    if not _verified_method(method):
        issues.append("AIMD method identity is not verified")
    if not state_done:
        warnings.append("AIMD manifest state is not DONE; the trajectory is partial")
    file_by_name = {item["name"]: item for item in source["files"]}
    osz_files = [file_by_name["OSZICAR"]] if "OSZICAR" in file_by_name else []
    xdat_files = [file_by_name["XDATCAR"]] if "XDATCAR" in file_by_name else []
    incar_files = [file_by_name["INCAR"]] if "INCAR" in file_by_name else []
    source_id = source["source_id"]
    segment_by_item = {
        id(item): (segment_index, segment[0]["step"])
        for segment_index, segment in enumerate(segments)
        for item in segment
    }
    samples = []
    for index, item in enumerate(raw_steps):
        segment_index, segment_start = segment_by_item.get(id(item), (None, None))
        time_ps = (
            (item["step"] - segment_start + 1) * potim / 1000.0
            if segment_start is not None and potim is not None and potim > 0 else None
        )
        samples.append({
            "sample_index": index,
            "step": _quantity(
                key="step", label="MD step", value=item.get("step"),
                unit="MD step", precision=0, denominator="OSZICAR step identifier",
                source_id=source_id, files=osz_files, parser=parser,
            ),
            "segment_index": segment_index,
            "time": _quantity(
                key="time", label="Time", value=time_ps,
                unit="ps", precision=spec.precision,
                denominator=(
                    f"segment-local elapsed time; POTIM={potim:g} fs"
                    if potim else "POTIM unavailable"),
                source_id=source_id, files=osz_files + incar_files, parser=parser,
            ),
            "total_energy": _quantity(
                key="total_energy", label="Total energy", value=item["e_tot"],
                unit="eV", precision=spec.precision,
                denominator="per AIMD simulation cell and MD sample",
                source_id=source_id, files=osz_files, parser=parser,
            ),
            "temperature": _quantity(
                key="temperature", label="Temperature", value=item["temp_k"],
                unit="K", precision=spec.precision,
                denominator="instantaneous OSZICAR MD sample",
                source_id=source_id, files=osz_files, parser=parser,
            ),
        })
    temperature_mean = (
        sum(temperatures) / len(temperatures)
        if temperatures and aggregate_ready else None
    )
    temperature_std = (
        math.sqrt(sum((value - temperature_mean) ** 2 for value in temperatures)
                  / len(temperatures))
        if temperatures and temperature_mean is not None and aggregate_ready else None
    )
    segment_rows = []
    for segment_index, segment in enumerate(segments):
        start_step = segment[0]["step"]
        end_step = segment[-1]["step"]
        duration = (
            (end_step - start_step + 1) * potim / 1000.0
            if potim is not None and potim > 0 else None
        )
        segment_rows.append({
            "segment_index": segment_index,
            "start_step": _quantity(
                key="start_step", label="Segment start step", value=start_step,
                unit="MD step", precision=0, denominator="OSZICAR step identifier",
                source_id=source_id, files=osz_files, parser=parser,
            ),
            "end_step": _quantity(
                key="end_step", label="Segment end step", value=end_step,
                unit="MD step", precision=0, denominator="OSZICAR step identifier",
                source_id=source_id, files=osz_files, parser=parser,
            ),
            "sample_count": _quantity(
                key="segment_sample_count", label="Segment samples", value=len(segment),
                unit="samples", precision=0, denominator="strictly increasing segment",
                source_id=source_id, files=osz_files, parser=parser,
            ),
            "duration": _quantity(
                key="segment_duration", label="Segment duration", value=duration,
                unit="ps", precision=spec.precision,
                denominator=f"inclusive segment steps; POTIM={potim:g} fs"
                if potim else "POTIM unavailable",
                source_id=source_id, files=osz_files + incar_files, parser=parser,
                reason="POTIM is unavailable" if duration is None else "",
            ),
        })
    metric_specs = (
        ("time_step", "Time step", potim if not evidence_blocked else None,
         "fs", "INCAR POTIM", incar_files),
        ("sampling_length", "Sampling length", sampling_length_ps, "ps",
         "single strictly increasing step segment", osz_files + incar_files),
        ("sample_count", "Parsed samples", len(samples) if samples else None, "samples",
         f"declared NSW={int(declared_steps) if declared_steps else 'unavailable'}", osz_files),
        ("temperature_mean", "Mean temperature", temperature_mean, "K",
         f"{len(temperatures)} temperature samples", osz_files),
        ("temperature_std", "Temperature standard deviation", temperature_std, "K",
         f"population standard deviation over {len(temperatures)} samples", osz_files),
        ("energy_drift_total", "Endpoint total-energy drift", drift_total, "eV",
         "last minus first sample of one strictly increasing segment", osz_files),
        ("energy_drift_slope", "Linear total-energy drift", slope_ev_ps, "eV/ps",
         f"least-squares slope over one {len(energies)}-sample segment", osz_files),
        ("trajectory_frames", "Structure frames",
         structure["frame_count"] if structure_ready else None, "frames",
         f'XDATCAR; {structure["natoms"] or "unavailable"} atoms', xdat_files),
        ("max_step_displacement", "Maximum per-step atom displacement",
         structure["max_step_displacement_a"], "Å",
         "maximum over atoms and adjacent periodic-image-corrected frames", xdat_files),
        ("final_rmsd", "Final translation-corrected RMS displacement",
         structure["final_rmsd_a"], "Å", "relative to first complete XDATCAR frame",
         xdat_files),
        ("max_rmsd", "Maximum translation-corrected RMS displacement",
         structure["max_rmsd_a"], "Å", "maximum over complete XDATCAR frames", xdat_files),
    )
    metrics = {
        key: _quantity(
            key=key, label=label, value=value, unit=unit,
            precision=spec.precision, denominator=denominator,
            source_id=source_id, files=files, parser=parser,
            reason=(
                "step gap/restart or incomplete DONE coverage prevents one aggregate value"
                if value is None and not aggregate_ready and key in {
                    "sampling_length", "energy_drift_total", "energy_drift_slope",
                    "temperature_mean", "temperature_std",
                } else "XDATCAR/POSCAR/NBLOCK binding is unavailable"
                if value is None and key in {
                    "trajectory_frames", "max_step_displacement", "final_rmsd", "max_rmsd",
                } else "required evidence is unavailable" if value is None else ""
            ),
        )
        for key, label, value, unit, denominator, files in metric_specs
    }
    return {
        "source": source,
        "parser": parser,
        "method": method,
        "status": ("unavailable" if evidence_blocked else
                   "available" if samples else "missing_prerequisite"),
        "available": bool(samples and not evidence_blocked),
        "samples": samples,
        "segments": segment_rows,
        "metrics": metrics,
        "structure_diagnostics": {
            "status": "available" if structure_ready else "unavailable",
            "nblock": nblock,
            "expected_frame_steps": expected_frame_steps,
            "observed_frame_steps": list(structure.get("frame_steps") or []),
            "observed_frame_count": structure["frame_count"],
            "parser_error": structure["error"],
            "issues": list(dict.fromkeys(structure_issues)),
        },
        "issues": list(dict.fromkeys(issues)),
        "warnings": list(dict.fromkeys(warnings)),
        "scientific_boundary": (
            "AIMD metrics are trajectory diagnostics only. A short trajectory does not prove "
            "thermal stability or long-time structural survival, and no observed RMS "
            "displacement threshold is promoted to a stability claim."
        ),
        "figure_data": {
            "steps": [
                {
                    "step": sample["step"]["value"],
                    "energy": sample["total_energy"]["value"],
                    "temperature": sample["temperature"]["value"],
                }
                for sample in samples
                if (_finite(sample["step"]["value"]) is not None
                    and _finite(sample["total_energy"]["value"]) is not None
                    and _finite(sample["temperature"]["value"]) is not None)
            ] if aggregate_ready else [],
            "dt_fs": potim,
            "provenance": {
                "parser_module": PARSER_MODULE,
                "parser_version": PARSER_VERSION,
                "source_ids": [source_id],
                "file_hashes": copy.deepcopy(source["files"]),
                "denominator": f"{len(samples)} parsed OSZICAR MD samples",
            },
        },
        "_snapshots": [snapshot],
    }


def build_aimd_analysis_view(
    spec: AnalysisSpec, targets: Sequence[Mapping[str, Any]], *,
    method_evidence: Callable[..., Mapping[str, Any]],
) -> dict[str, Any]:
    if spec.analysis_id != "aimd-diagnostics":
        raise ValueError("AIMD analysis view requires analysis_id=aimd-diagnostics")
    selected = [target for target in targets if target.get("task_type") == "aimd"]
    snapshot_budget = SourceSnapshotBudget()
    trajectories = [
        _aimd_trajectory(spec, target, method_evidence, snapshot_budget)
        for target in selected
    ]
    for trajectory in trajectories:
        for snapshot in trajectory.pop("_snapshots", []):
            snapshot.assert_unchanged()
    blocking = [issue for item in trajectories for issue in item["issues"]]
    warnings = [warning for item in trajectories for warning in item["warnings"]]
    if not selected:
        blocking.append("No registered AIMD project member or descendant was resolved")
    available = sum(item["available"] is True for item in trajectories)
    figure_data = {}
    if (len(trajectories) == 1
            and len(trajectories[0]["figure_data"].get("steps") or []) >= 2):
        figure_data["aimd_diagnostic"] = copy.deepcopy(
            trajectories[0]["figure_data"])
    payload = {
        "schema": VIEW_SCHEMA,
        "analysis_id": spec.analysis_id,
        "spec": spec.to_dict(),
        "spec_sha256": spec.semantic_sha256,
        "scientific_status": "diagnostic",
        "capability_status": "available" if available else "missing_prerequisite",
        "available": bool(available),
        "trajectories": trajectories,
        "rows": trajectories,
        "missing": [] if selected else [blocking[-1]],
        "blocking": list(dict.fromkeys(blocking)),
        "warnings": list(dict.fromkeys(warnings)),
        "next_action": (
            "Inspect sampling, drift, temperature, and structure diagnostics without "
            "promoting them to a long-time stability claim."
            if available else "Complete a registered AIMD job and refresh."
        ),
        "denominator": {
            "resolved_targets": len(targets),
            "matching_targets": len(selected),
            "available_trajectories": available,
            "parsed_samples": sum(len(item["samples"]) for item in trajectories),
            "structure_frames": sum(
                int(item["metrics"]["trajectory_frames"]["value"] or 0)
                for item in trajectories
            ),
            "visible_rows": sum(len(item["samples"]) for item in trajectories),
        },
    }
    return _finalize(
        payload, figure_presets=("aimd_diagnostic",),
        table_ids=("aimd-samples-table", "aimd-diagnostics-table"),
        figure_data=figure_data,
    )


__all__ = [
    "AIMD_SHORT_TRAJECTORY_PS",
    "CONVERGENCE_MIN_PLATFORM_POINTS",
    "CONVERGENCE_PRIMARY_THRESHOLD_MEV_PER_ATOM",
    "CONVERGENCE_THRESHOLDS_MEV_PER_ATOM",
    "PARSER_MODULE",
    "PARSER_VERSION",
    "REPORT_BINDING_SCHEMA",
    "build_aimd_analysis_view",
    "build_convergence_analysis_view",
    "build_neb_analysis_view",
]
