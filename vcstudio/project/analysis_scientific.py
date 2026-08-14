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
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from vcstudio import __version__
from vcstudio.cluster import convergence
from vcstudio.generate.incar_builder import parse_incar
from vcstudio.generate.poscar import parse_poscar_species
from vcstudio.generate.structure_view import parse_positions
from vcstudio.project.analysis_registry import AnalysisSpec, get_analysis
from vcstudio.project.analysis_sources import source_identity, value_provenance


VIEW_SCHEMA = "vcstudio.analysis-view/v1"
REPORT_BINDING_SCHEMA = "vcstudio.analysis-report-binding/v1"
PARSER_MODULE = "vcstudio.project.analysis_scientific"
PARSER_VERSION = str(__version__)
CONVERGENCE_THRESHOLDS_MEV_PER_ATOM = (0.5, 1.0, 2.0)
CONVERGENCE_PRIMARY_THRESHOLD_MEV_PER_ATOM = 1.0
CONVERGENCE_MIN_PLATFORM_POINTS = 3
AIMD_SHORT_TRAJECTORY_PS = 10.0

_FRAME_RE = re.compile(r"^\d+$")
_SIGMA0_RE = re.compile(r"energy\(sigma->0\)\s*=\s*([-+0-9.Ee]+)")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _source(
    target: Mapping[str, Any], evidence_names: Sequence[str],
) -> dict[str, Any]:
    identity, _missing = source_identity(target, evidence_names)
    return identity


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


def _neb_coordinates(root: Path, frames: Sequence[str]) -> tuple[list[float | None], str]:
    structures = []
    for frame in frames:
        directory = root / frame
        path = directory / "CONTCAR"
        if not path.is_file() or not _read_text(path).strip():
            path = directory / "POSCAR"
        text = _read_text(path)
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


def _frame_energy(frame_dir: Path) -> tuple[float | None, str]:
    oszicar = _read_text(frame_dir / "OSZICAR")
    steps = convergence.parse_oszicar(oszicar)
    if steps and _finite(steps[-1].get("E0")) is not None:
        return float(steps[-1]["E0"]), "OSZICAR"
    outcar = _read_text(frame_dir / "OUTCAR")
    matches = _SIGMA0_RE.findall(outcar)
    try:
        return (float(matches[-1]), "OUTCAR") if matches else (None, "")
    except ValueError:
        return None, ""


def _endpoint_energy(
    root: Path, manifest: Mapping[str, Any], frame: str, role: str,
) -> tuple[float | None, str]:
    value, source = _frame_energy(root / frame)
    if value is not None:
        return value, source
    inputs = manifest.get("inputs") or {}
    endpoints = inputs.get("neb_endpoints") or {}
    record = endpoints.get(role) or {}
    if not isinstance(record, Mapping) or record.get("trusted") is not True:
        return None, ""
    if str(record.get("target_frame") or "") != frame:
        return None, ""
    energy = _finite(record.get("energy_e0_eV"))
    source = str(record.get("energy_source") or "")
    if energy is None or not source:
        return None, ""
    return energy, "job.yaml"


def _explicit_electronic_status(outcar: str) -> str:
    lowered = outcar.lower()
    if "aborting loop because ediff is reached" in lowered:
        return "converged"
    if "electronic convergence not reached" in lowered:
        return "not_converged"
    return "unavailable"


def _method_fingerprint_hash(value: Any) -> str:
    if isinstance(value, str) and _SHA256_RE.fullmatch(value.lower()):
        return value.lower()
    if isinstance(value, Mapping) and value:
        return _canonical_hash(value)
    return ""


def _neb_method_gate(
    target: Mapping[str, Any], method: Mapping[str, Any],
) -> tuple[bool, list[str]]:
    issues = []
    if method.get("status") != "verified":
        issues.append("NEB method identity is not verified")
    fingerprint = method.get("fingerprint")
    root_hash = _method_fingerprint_hash(fingerprint)
    if not root_hash:
        issues.append("NEB method fingerprint is unavailable")
    manifest = target.get("manifest") or {}
    inputs = manifest.get("inputs") or {}
    endpoints = inputs.get("neb_endpoints") or {}
    if not isinstance(endpoints, Mapping):
        endpoints = {}
    for role in ("start", "end"):
        record = endpoints.get(role) or {}
        if not isinstance(record, Mapping) or record.get("trusted") is not True:
            issues.append(f"{role} endpoint is not trusted")
            continue
        endpoint_hash = _method_fingerprint_hash(
            record.get("method_fingerprint_sha256") or
            record.get("method_fingerprint"))
        if not endpoint_hash:
            issues.append(f"{role} endpoint method fingerprint is unavailable")
        elif root_hash and endpoint_hash != root_hash:
            issues.append(f"{role} endpoint method differs from the NEB method")
        if record.get("source_state") != "DONE":
            issues.append(f"{role} endpoint source state is not DONE")
    return not issues, issues


def _neb_path(
    spec: AnalysisSpec, target: Mapping[str, Any],
    method_evidence: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    root = Path(str(target.get("path") or ""))
    parser = _parser("build_neb_analysis_view")
    manifest = target.get("manifest") or {}
    frames = sorted(
        (item.name for item in root.iterdir()
         if item.is_dir() and _FRAME_RE.fullmatch(item.name)),
        key=int,
    ) if root.is_dir() else []
    evidence_names = [
        f"{frame}/{name}" for frame in frames
        for name in ("POSCAR", "CONTCAR", "OSZICAR", "OUTCAR")
    ]
    source = _source(target, ["INCAR", *evidence_names])
    issues: list[str] = []
    warnings: list[str] = []
    if len(frames) < 3:
        issues.append("NEB requires at least three ordered image directories")
    coordinates, coordinate_reason = _neb_coordinates(root, frames) if frames else ([], "")
    if coordinate_reason:
        issues.append(coordinate_reason)

    inputs = manifest.get("inputs") or {}
    incar = parse_incar(_read_text(root / "INCAR"))
    ediffg = _finite(incar.get("EDIFFG"))
    force_threshold = abs(ediffg) if ediffg is not None and ediffg < 0 else None
    if force_threshold is None:
        issues.append("NEB lacks an explicit negative EDIFFG force threshold")
    climb = str(incar.get("LCLIMB") or "").strip().strip(".").upper() in {
        "T", "TRUE", "1",
    }
    if not climb:
        issues.append("LCLIMB evidence is absent; a climbing-image barrier is unavailable")
    declared_images = inputs.get("n_images")
    if isinstance(declared_images, int) and declared_images + 2 != len(frames):
        issues.append("manifest n_images does not match the image-directory count")

    points = []
    energies: list[float | None] = []
    electronic_statuses = []
    ionic_statuses = []
    for index, frame in enumerate(frames):
        role = "start" if index == 0 else "end" if index == len(frames) - 1 else "image"
        energy, energy_file = (
            _endpoint_energy(root, manifest, frame, role)
            if role in {"start", "end"} else _frame_energy(root / frame)
        )
        energies.append(energy)
        outcar_text = _read_text(root / frame / "OUTCAR")
        forces = convergence.parse_outcar_fmax(outcar_text)
        fmax = forces[-1] if forces else None
        electronic = _explicit_electronic_status(outcar_text)
        if role != "image":
            ionic = "endpoint"
        elif fmax is None or force_threshold is None:
            ionic = "unavailable"
        else:
            ionic = "converged" if fmax <= force_threshold else "not_converged"
        electronic_statuses.append(electronic)
        ionic_statuses.append(ionic)
        frame_files = [
            item for item in source["files"]
            if str(item.get("name") or "").startswith(f"{frame}/")
        ]
        energy_files = [
            item for item in frame_files
            if str(item.get("name") or "").endswith(f"/{energy_file}")
        ]
        if energy_file == "job.yaml":
            energy_files = [
                item for item in source["files"] if item.get("name") == "job.yaml"
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
                reason="image energy evidence is unavailable" if energy is None else "",
            ),
            "relative_energy": None,
            "max_force": _quantity(
                key="max_force", label="Maximum force", value=fmax,
                unit="eV/Å", precision=spec.precision,
                denominator="maximum over atoms in the final ionic step",
                source_id=source["source_id"], files=frame_files, parser=parser,
                reason="OUTCAR force block is unavailable" if fmax is None else "",
            ),
            "electronic_convergence": electronic,
            "ionic_convergence": ionic,
            "structure_evidence": [
                item for item in frame_files
                if str(item.get("name") or "").endswith(("/POSCAR", "/CONTCAR"))
            ],
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

    method = dict(method_evidence(target) or {})
    method_ok, method_issues = _neb_method_gate(target, method)
    issues.extend(method_issues)
    if target.get("state") != "DONE":
        issues.append("NEB manifest state is not DONE")
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
    barrier_files = source["files"]
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
        "status": "available" if points else "missing_prerequisite",
        "available": bool(points),
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
    }


def build_neb_analysis_view(
    spec: AnalysisSpec, targets: Sequence[Mapping[str, Any]], *,
    method_evidence: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    if spec.analysis_id != "neb-path":
        raise ValueError("NEB analysis view requires analysis_id=neb-path")
    selected = [target for target in targets if target.get("task_type") == "neb"]
    paths = [_neb_path(spec, target, method_evidence) for target in selected]
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
        figure_data["neb_profile"] = {
            "rel": values,
            "ts_index": paths[0]["barriers"]["transition_image_index"],
            "barrier_f": paths[0]["barriers"]["forward"]["value"],
            "barrier_r": paths[0]["barriers"]["reverse"]["value"],
            "provenance": {
                "parser_module": PARSER_MODULE,
                "parser_version": PARSER_VERSION,
                "source_ids": [paths[0]["source"]["source_id"]],
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


def _method_invariant(method: Mapping[str, Any], kind: str) -> str:
    fingerprint = copy.deepcopy(method.get("fingerprint") or {})
    if not isinstance(fingerprint, Mapping):
        return ""
    fingerprint = dict(fingerprint)
    if kind == "encut":
        fingerprint.pop("encut", None)
    elif kind == "kmesh":
        fingerprint.pop("kpoints_scheme", None)
    return _canonical_hash(fingerprint) if fingerprint else ""


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
    method_evidence: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    parser = _parser("build_convergence_analysis_view")
    raw = []
    issues = []
    method_invariants = set()
    for target in targets:
        manifest = target.get("manifest") or {}
        inputs = manifest.get("inputs") or {}
        x = _finite(inputs.get("series_value"))
        label = str(inputs.get("series_label") or "")
        source = _source(
            target, ["OSZICAR", "INCAR", "KPOINTS", "POSCAR", "CONTCAR", "POTCAR"],
        )
        method = dict(method_evidence(target) or {})
        invariant = _method_invariant(method, kind)
        if method.get("status") != "verified" or not invariant:
            issues.append(f'{source["source_id"]}: method evidence is unavailable')
        else:
            method_invariants.add(invariant)
        natoms = _finite(inputs.get("natoms"))
        if natoms is None:
            text = _read_text(Path(str(target.get("path") or "")) / "POSCAR")
            try:
                _symbols, counts = parse_poscar_species(text)
                natoms = float(sum(counts)) if counts else None
            except (IndexError, TypeError, ValueError):
                natoms = None
        oszicar = _read_text(Path(str(target.get("path") or "")) / "OSZICAR")
        steps = convergence.parse_oszicar(oszicar)
        energy = (
            _finite(steps[-1].get("E0"))
            if target.get("state") == "DONE" and steps else None
        )
        if x is None:
            issues.append(f'{source["source_id"]}: explicit series_value is unavailable')
        if natoms is None or natoms <= 0:
            issues.append(f'{source["source_id"]}: atom-count denominator is unavailable')
            natoms = None
        if energy is None:
            issues.append(f'{source["source_id"]}: DONE OSZICAR:E0 evidence is unavailable')
        energy_files = [item for item in source["files"] if item.get("name") == "OSZICAR"]
        raw.append({
            "x": x,
            "label": label or (str(x) if x is not None else "—"),
            "source": source,
            "method": method,
            "natoms": natoms,
            "energy": energy,
            "energy_files": energy_files,
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
    reference = next(
        (item for item in reversed(raw)
         if item["energy"] is not None and item["natoms"] is not None), None,
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
        points.append({
            "label": item["label"],
            "source": source,
            "method": item["method"],
            "parameter": _quantity(
                key="parameter", label="Scan parameter", value=item["x"],
                unit=_SERIES_UNITS[kind], precision=spec.precision,
                denominator="one declared scan coordinate",
                source_id=source["source_id"], files=source["files"], parser=parser,
                reason="series_value is unavailable" if item["x"] is None else "",
            ),
            "absolute_energy": _quantity(
                key="absolute_energy", label="Absolute energy", value=item["energy"],
                unit="eV", precision=spec.precision, denominator="per calculation cell",
                source_id=source["source_id"], files=files, parser=parser,
                reason="DONE OSZICAR:E0 evidence is unavailable"
                if item["energy"] is None else "",
            ),
            "energy_per_atom": _quantity(
                key="energy_per_atom", label="Energy per atom", value=per_atom,
                unit="eV/atom", precision=spec.precision,
                denominator=f'{int(item["natoms"])} atoms' if item["natoms"] else "unavailable",
                source_id=source["source_id"], files=files, parser=parser,
                reason="atom-count denominator is unavailable" if per_atom is None else "",
            ),
            "delta_per_atom": _quantity(
                key="delta_per_atom", label="Difference from terminal reference",
                value=delta, unit="meV/atom", precision=spec.precision,
                denominator=(
                    f'{int(item["natoms"])} atoms; reference={reference["label"]}'
                    if item["natoms"] and reference else "unavailable"
                ),
                source_id=source["source_id"], files=files, parser=parser,
                reason="terminal reference or atom denominator is unavailable"
                if delta is None else "",
            ),
            "anomalies": (["duplicate coordinate"] if item["x"] in duplicates else [])
            + (["missing energy"] if item["energy"] is None else []),
        })

    sensitivity = []
    primary_suffix = []
    primary_index = None
    series_evidence_ready = bool(
        not duplicates and len(method_invariants) == 1
        and all(point["parameter"]["value"] is not None
                and point["absolute_energy"]["value"] is not None
                and point["energy_per_atom"]["value"] is not None
                for point in points)
    )
    for threshold in CONVERGENCE_THRESHOLDS_MEV_PER_ATOM:
        suffix, recommended = _platform(points, threshold)
        if threshold == CONVERGENCE_PRIMARY_THRESHOLD_MEV_PER_ATOM:
            primary_suffix, primary_index = suffix, recommended
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
                if recommended is not None and series_evidence_ready else _quantity(
                    key="recommended_parameter", label="Recommended parameter",
                    value=None, unit=_SERIES_UNITS[kind], precision=spec.precision,
                    denominator=f"threshold={threshold:g} meV/atom",
                    source_id=(points[-1]["source"]["source_id"] if points else ""),
                    files=(points[-1]["source"]["files"] if points else []), parser=parser,
                    reason=(issues[0] if issues else
                            "insufficient contiguous terminal platform evidence"),
                )
            ),
            "platform_point_indexes": suffix,
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
    ]
    return {
        "series_id": _canonical_hash({
            "kind": kind,
            "source_ids": [point["source"]["source_id"] for point in points],
        })[:24],
        "kind": kind,
        "status": "available" if points else "missing_prerequisite",
        "available": bool(points),
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
    }


def build_convergence_analysis_view(
    spec: AnalysisSpec, targets: Sequence[Mapping[str, Any]], *,
    method_evidence: Callable[[Mapping[str, Any]], Mapping[str, Any]],
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
    series = [
        _convergence_series(spec, kind, members, method_evidence)
        for (kind, _group), members in sorted(groups.items())
    ]
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
    if len(series) == 1:
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


_XDATCAR_CONFIG_RE = re.compile(r"^\s*(?:Direct|Cartesian)\s+configuration\s*=", re.I)


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
        cell = [[float(value) * scale for value in lines[index].split()[:3]]
                for index in range(2, 5)]
        counts = [int(value) for value in lines[6].split()]
    except (IndexError, TypeError, ValueError):
        return {"frames": [], "natoms": None, "cell": None,
                "error": "XDATCAR cell or atom counts are invalid"}
    natoms = sum(counts)
    if natoms <= 0 or _matrix_inverse(cell) is None:
        return {"frames": [], "natoms": None, "cell": None,
                "error": "XDATCAR atom count or cell is invalid"}
    frames = []
    index = 7
    while index < len(lines):
        if not _XDATCAR_CONFIG_RE.match(lines[index]):
            index += 1
            continue
        mode = lines[index].strip().lower()
        index += 1
        coords = []
        for _atom in range(natoms):
            if index >= len(lines):
                return {"frames": frames, "natoms": natoms, "cell": cell,
                        "error": "XDATCAR final frame is truncated"}
            try:
                coords.append([float(value) for value in lines[index].split()[:3]])
            except (TypeError, ValueError):
                return {"frames": frames, "natoms": natoms, "cell": cell,
                        "error": "XDATCAR coordinate is invalid"}
            index += 1
        if mode.startswith("cartesian"):
            inverse = _matrix_inverse(cell)
            coords = [
                _row_vector([value * scale for value in coord], inverse)
                for coord in coords
            ]
        frames.append(coords)
    return {"frames": frames, "natoms": natoms, "cell": cell, "error": ""}


def _trajectory_metrics(text: str) -> dict[str, Any]:
    parsed = _parse_xdatcar(text)
    frames = parsed["frames"]
    natoms = parsed["natoms"]
    cell = parsed["cell"]
    if not frames or not natoms or not cell:
        return {
            "frame_count": len(frames), "natoms": natoms,
            "max_step_displacement_a": None, "final_rmsd_a": None,
            "max_rmsd_a": None, "cell_volume_a3": None,
            "error": parsed["error"] or "XDATCAR contains no complete trajectory frames",
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
        "frame_count": len(frames), "natoms": natoms,
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


def _aimd_trajectory(
    spec: AnalysisSpec, target: Mapping[str, Any],
    method_evidence: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    from vcstudio.generate.aimd_builder import parse_aimd_energy

    root = Path(str(target.get("path") or ""))
    parser = _parser("build_aimd_analysis_view")
    source = _source(target, ["INCAR", "POSCAR", "OSZICAR", "XDATCAR"])
    manifest = target.get("manifest") or {}
    inputs = manifest.get("inputs") or {}
    incar = parse_incar(_read_text(root / "INCAR"))
    potim = _finite(incar.get("POTIM"))
    declared_steps = _finite(incar.get("NSW"))
    declared_temperature = _finite(incar.get("TEBEG"))
    issues = []
    warnings = []
    if potim is None or potim <= 0:
        issues.append("AIMD requires an explicit positive POTIM in INCAR")
    if declared_steps is None or declared_steps <= 0:
        issues.append("AIMD requires an explicit positive NSW in INCAR")
    for key, actual in (
        ("potim_fs", potim), ("steps", declared_steps),
        ("temp_k", declared_temperature),
    ):
        recorded = _finite(inputs.get(key))
        if recorded is not None and actual is not None and abs(recorded - actual) > 1e-9:
            issues.append(f"manifest {key} differs from the current INCAR evidence")
    parsed = parse_aimd_energy(
        _read_text(root / "OSZICAR"), potim_fs=potim if potim else 1.0)
    raw_steps = list(parsed.get("steps") or [])
    if not raw_steps:
        issues.append("OSZICAR contains no parseable AIMD total-energy/temperature steps")
    times_fs = ([float(item["t_fs"]) for item in raw_steps]
                if potim is not None and potim > 0 else [])
    energies = [float(item["e_tot"]) for item in raw_steps]
    temperatures = [float(item["temp_k"]) for item in raw_steps]
    sampling_length_ps = (
        (times_fs[-1] - times_fs[0] + (potim or 0.0)) / 1000.0
        if times_fs else None
    )
    if sampling_length_ps is not None and sampling_length_ps < AIMD_SHORT_TRAJECTORY_PS:
        warnings.append(
            f"trajectory length {sampling_length_ps:.6g} ps is below the explicit "
            f"{AIMD_SHORT_TRAJECTORY_PS:g} ps short-trajectory diagnostic threshold"
        )
    drift_total = energies[-1] - energies[0] if energies else None
    slope_ev_ps = _linear_slope(
        [value / 1000.0 for value in times_fs], energies)
    structure = _trajectory_metrics(_read_text(root / "XDATCAR"))
    if structure["error"]:
        warnings.append(str(structure["error"]))
    method = dict(method_evidence(target) or {})
    if method.get("status") != "verified":
        issues.append("AIMD method identity is not verified")
    if target.get("state") != "DONE":
        warnings.append("AIMD manifest state is not DONE; the trajectory is partial")
    file_by_name = {item["name"]: item for item in source["files"]}
    osz_files = [file_by_name["OSZICAR"]] if "OSZICAR" in file_by_name else []
    xdat_files = [file_by_name["XDATCAR"]] if "XDATCAR" in file_by_name else []
    incar_files = [file_by_name["INCAR"]] if "INCAR" in file_by_name else []
    source_id = source["source_id"]
    samples = []
    for index, item in enumerate(raw_steps):
        time_ps = (float(item["t_fs"]) / 1000.0
                   if potim is not None and potim > 0 else None)
        samples.append({
            "sample_index": index,
            "time": _quantity(
                key="time", label="Time", value=time_ps,
                unit="ps", precision=spec.precision,
                denominator=f"POTIM={potim:g} fs" if potim else "POTIM unavailable",
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
    temperature_mean = sum(temperatures) / len(temperatures) if temperatures else None
    temperature_std = (
        math.sqrt(sum((value - temperature_mean) ** 2 for value in temperatures)
                  / len(temperatures)) if temperatures else None
    )
    metric_specs = (
        ("time_step", "Time step", potim, "fs", "INCAR POTIM", incar_files),
        ("sampling_length", "Sampling length", sampling_length_ps, "ps",
         "first through last parsed MD sample", osz_files + incar_files),
        ("sample_count", "Parsed samples", len(samples) if samples else None, "samples",
         f"declared NSW={int(declared_steps) if declared_steps else 'unavailable'}", osz_files),
        ("temperature_mean", "Mean temperature", temperature_mean, "K",
         f"{len(temperatures)} temperature samples", osz_files),
        ("temperature_std", "Temperature standard deviation", temperature_std, "K",
         f"population standard deviation over {len(temperatures)} samples", osz_files),
        ("energy_drift_total", "Endpoint total-energy drift", drift_total, "eV",
         "last minus first total-energy sample", osz_files),
        ("energy_drift_slope", "Linear total-energy drift", slope_ev_ps, "eV/ps",
         f"least-squares slope over {len(energies)} samples", osz_files),
        ("trajectory_frames", "Structure frames", structure["frame_count"], "frames",
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
            reason="required evidence is unavailable" if value is None else "",
        )
        for key, label, value, unit, denominator, files in metric_specs
    }
    return {
        "source": source,
        "parser": parser,
        "method": method,
        "status": "available" if samples else "missing_prerequisite",
        "available": bool(samples),
        "samples": samples,
        "metrics": metrics,
        "structure_diagnostics": {
            "status": "available" if structure["frame_count"] else "unavailable",
            "parser_error": structure["error"],
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
                    "step": sample["sample_index"] + 1,
                    "energy": sample["total_energy"]["value"],
                    "temperature": sample["temperature"]["value"],
                }
                for sample in samples
            ],
            "dt_fs": potim,
            "provenance": {
                "parser_module": PARSER_MODULE,
                "parser_version": PARSER_VERSION,
                "source_ids": [source_id],
                "file_hashes": copy.deepcopy(source["files"]),
                "denominator": f"{len(samples)} parsed OSZICAR MD samples",
            },
        },
    }


def build_aimd_analysis_view(
    spec: AnalysisSpec, targets: Sequence[Mapping[str, Any]], *,
    method_evidence: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    if spec.analysis_id != "aimd-diagnostics":
        raise ValueError("AIMD analysis view requires analysis_id=aimd-diagnostics")
    selected = [target for target in targets if target.get("task_type") == "aimd"]
    trajectories = [_aimd_trajectory(spec, target, method_evidence) for target in selected]
    blocking = [issue for item in trajectories for issue in item["issues"]]
    warnings = [warning for item in trajectories for warning in item["warnings"]]
    if not selected:
        blocking.append("No registered AIMD project member or descendant was resolved")
    available = sum(item["available"] is True for item in trajectories)
    figure_data = {}
    if len(trajectories) == 1 and trajectories[0]["samples"]:
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
