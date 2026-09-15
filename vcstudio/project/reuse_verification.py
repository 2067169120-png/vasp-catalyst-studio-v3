"""Hash-bound, task-aware output verification for calculation reuse.

The manifest may describe an earlier parser decision, but it cannot make stale
or contradictory output reusable.  This module always parses the files whose
current bytes still match the manifest and records the parser contract used for
that decision.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import re
import stat
from typing import Any, BinaryIO, Iterator, Mapping

from vcstudio.project import result_import
from vcstudio.shared import manifest as manifest_mod


OUTPUT_PARSER_SCHEMA = "vcstudio.reuse-output-parser/v1"
OUTPUT_PARSER_NAME = "vcstudio.result-import-evidence"
OUTPUT_PARSER_VERSION = "1"
MAX_RESULT_FILES = 128
MAX_RESULT_FILE_BYTES = 8 * 1024 * 1024 * 1024
MAX_RESULT_TOTAL_BYTES = 32 * 1024 * 1024 * 1024
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_VASP_VERSION_RE = re.compile(rb"\bvasp\.([0-9]+(?:\.[0-9]+){1,3})\b", re.I)
_FRAME_RESULT_RE = re.compile(
    r"^(?P<frame>[0-9]{2})/(?P<name>OUTCAR|OSZICAR|vasprun\.xml|CONTCAR)$"
)
_COMMON_RESULTS = frozenset({"OUTCAR", "OSZICAR", "vasprun.xml", "CONTCAR"})
_TASK_RESULTS = {
    "static": frozenset({
        "CHGCAR", "CHG", "WAVECAR", "AECCAR0", "AECCAR2", "ELFCAR",
        "LOCPOT", "PROCAR", "EIGENVAL",
    }),
    "dos_pdos": frozenset({
        "DOSCAR", "PROCAR", "EIGENVAL", "CHGCAR", "WAVECAR", "AECCAR0",
        "AECCAR2", "ELFCAR", "LOCPOT",
    }),
    "bands": frozenset({"PROCAR", "EIGENVAL", "CHGCAR", "WAVECAR"}),
    "bader": frozenset({"CHGCAR", "AECCAR0", "AECCAR2", "ACF.dat"}),
    "chgdiff": frozenset({"CHGCAR", "CHGDIFF.vasp", "AECCAR0", "AECCAR2"}),
    "elf": frozenset({"ELFCAR", "CHGCAR", "WAVECAR"}),
    "workfunction": frozenset({"LOCPOT", "CHGCAR", "WAVECAR"}),
    "aimd": frozenset({"XDATCAR", "WAVECAR", "CHGCAR"}),
    "freq": frozenset(),
    "relax": frozenset({"WAVECAR", "CHGCAR"}),
    "cellopt": frozenset({"WAVECAR", "CHGCAR"}),
    "dimer": frozenset({"WAVECAR", "CHGCAR"}),
    "spin_scan": frozenset({"WAVECAR", "CHGCAR"}),
    "vaspsol": frozenset({"WAVECAR", "CHGCAR", "LOCPOT"}),
}
_STATIC_LIKE_TASKS = frozenset({
    "adsorption_project", "eos", "surface_energy", "formation_binding",
    "conv_encut", "conv_kmesh", "conv_vacuum", "conv_thickness", "conv_scan",
    "quick",
})
_IONIC_TASKS = frozenset({"relax", "cellopt", "dimer"})


@dataclass(frozen=True)
class _ResultSnapshot:
    """One declared output bound to one open regular-file entity."""

    name: str
    path: Path
    handle: BinaryIO
    opened_stat: os.stat_result
    component_stats: tuple[tuple[Path, os.stat_result], ...]
    sha256: str

    @property
    def size(self) -> int:
        return int(self.opened_stat.st_size)


def _mtime_ns(value: os.stat_result) -> int:
    return int(getattr(value, "st_mtime_ns", int(value.st_mtime * 1_000_000_000)))


def _same_entity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        int(left.st_dev), int(left.st_ino), stat.S_IFMT(left.st_mode)
    ) == (
        int(right.st_dev), int(right.st_ino), stat.S_IFMT(right.st_mode)
    )


def _same_immutable_stat(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        _same_entity(left, right)
        and int(left.st_size) == int(right.st_size)
        and _mtime_ns(left) == _mtime_ns(right)
    )


def _is_reparse(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & marker)


def _snapshot_components(root: Path, path: Path) -> tuple[tuple[Path, os.stat_result], ...]:
    """Capture every path entity without following links or reparse points."""
    components: list[tuple[Path, os.stat_result]] = []
    root_stat = root.lstat()
    if not stat.S_ISDIR(root_stat.st_mode) or _is_reparse(root_stat):
        raise OSError("unsafe result root")
    components.append((root, root_stat))
    current = root
    relative = path.relative_to(root)
    for index, part in enumerate(relative.parts):
        current = current / part
        current_stat = current.lstat()
        final = index == len(relative.parts) - 1
        expected = stat.S_ISREG if final else stat.S_ISDIR
        if not expected(current_stat.st_mode) or _is_reparse(current_stat):
            raise OSError("unsafe linked/non-regular result path")
        components.append((current, current_stat))
    return tuple(components)


def _open_result_snapshot(
    stack: ExitStack, root: Path, name: str, *, remaining_bytes: int,
) -> _ResultSnapshot:
    path = root.joinpath(*name.split("/"))
    components = _snapshot_components(root, path)
    root_resolved = root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    if resolved == root_resolved or root_resolved not in resolved.parents:
        raise OSError("result path escapes job root")
    path_stat = components[-1][1]
    if int(path_stat.st_size) > MAX_RESULT_FILE_BYTES:
        raise ValueError(f"result file exceeds resource limit:{name}")
    if int(path_stat.st_size) > remaining_bytes:
        raise ValueError("result bundle exceeds resource limit")

    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    descriptor = os.open(path, flags)
    try:
        handle = os.fdopen(descriptor, "rb", closefd=True)
    except BaseException:
        os.close(descriptor)
        raise
    stack.callback(handle.close)
    opened = os.fstat(handle.fileno())
    if not stat.S_ISREG(opened.st_mode) or not _same_immutable_stat(path_stat, opened):
        raise OSError("result entity changed while opening")

    digest = hashlib.sha256()
    bytes_read = 0
    while True:
        chunk = handle.read(1024 * 1024)
        if not chunk:
            break
        bytes_read += len(chunk)
        if bytes_read > opened.st_size:
            raise OSError("result size changed while hashing")
        digest.update(chunk)
    hashed = os.fstat(handle.fileno())
    if bytes_read != opened.st_size or not _same_immutable_stat(opened, hashed):
        raise OSError("result changed while hashing")
    handle.seek(0)
    return _ResultSnapshot(
        name=name,
        path=path,
        handle=handle,
        opened_stat=opened,
        component_stats=components,
        sha256=digest.hexdigest(),
    )


def _snapshot_integrity_issue(snapshot: _ResultSnapshot) -> str | None:
    """Revalidate both the open entity and its reachable path after parsing."""
    try:
        current_fd = os.fstat(snapshot.handle.fileno())
        if not _same_immutable_stat(snapshot.opened_stat, current_fd):
            return f"result file changed during verification:{snapshot.name}"
        for path, original in snapshot.component_stats:
            current = path.lstat()
            if _is_reparse(current) or not _same_immutable_stat(original, current):
                return f"result path changed during verification:{snapshot.name}"
    except (OSError, ValueError):
        return f"result file unavailable after verification:{snapshot.name}"
    return None


@contextmanager
def _verified_result_snapshots(
    root: Path, manifest: Mapping[str, Any]
) -> Iterator[tuple[dict[str, _ResultSnapshot], list[str]]]:
    declared = _declared_hashes(manifest)
    if not declared:
        yield {}, ["results.fetched_sha256"]
        return
    if len(declared) > MAX_RESULT_FILES:
        yield {}, [f"result declaration exceeds {MAX_RESULT_FILES} files"]
        return
    task = str(manifest.get("task_type") or "").strip().lower()
    try:
        task = manifest_mod.normalize_task_type(task)
    except ValueError:
        yield {}, [f"unknown result task type:{task or '?'}"]
        return

    root = Path(root)
    snapshots: dict[str, _ResultSnapshot] = {}
    issues: list[str] = []
    total_bytes = 0
    with ExitStack() as stack:
        for raw_name, raw_digest in sorted(declared.items(), key=lambda item: str(item[0])):
            name = str(raw_name or "").strip().replace("\\", "/")
            digest = str(raw_digest or "").strip().lower()
            if not _allowed_result(name, task) or not _HEX64_RE.fullmatch(digest):
                issues.append(f"result file is not allowlisted/hash-valid:{name or '?'}")
                continue
            try:
                snapshot = _open_result_snapshot(
                    stack, root, name,
                    remaining_bytes=MAX_RESULT_TOTAL_BYTES - total_bytes,
                )
            except ValueError as exc:
                issues.append(str(exc))
                continue
            except OSError:
                issues.append(f"result file unavailable/unsafe:{name}")
                continue
            total_bytes += snapshot.size
            if snapshot.sha256 != digest:
                issues.append(f"result file changed:{name}")
                continue
            snapshots[name] = snapshot
        if not snapshots:
            issues.append("no result file remains verifiable")
        yield snapshots, issues


def _observed_vasp_version(snapshot: _ResultSnapshot | None) -> str | None:
    if snapshot is None:
        return None
    try:
        snapshot.handle.seek(0)
        payload = snapshot.handle.read(256 * 1024)
    except (OSError, ValueError):
        return None
    match = _VASP_VERSION_RE.search(payload)
    return match.group(1).decode("ascii") if match else None


def _planned_vasp_version(manifest: Mapping[str, Any]) -> str | None:
    inputs = manifest.get("inputs") if isinstance(manifest.get("inputs"), Mapping) else {}
    environment = inputs.get("execution_environment")
    if not isinstance(environment, Mapping):
        return None
    version = str(environment.get("vasp_version") or "").strip()
    return version or None


def _manifest_results(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    value = manifest.get("results")
    return value if isinstance(value, Mapping) else {}


def _diagnosis(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    value = _manifest_results(manifest).get("diagnosis")
    return value if isinstance(value, Mapping) else {}


def _declared_hashes(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    results = _manifest_results(manifest)
    value = results.get("fetched_sha256")
    if not isinstance(value, Mapping):
        value = results.get("output_sha256")
    return value if isinstance(value, Mapping) else {}


def _allowed_result(name: str, task: str) -> bool:
    if task == "neb":
        return bool(_FRAME_RESULT_RE.fullmatch(name))
    if "/" in name or "\\" in name:
        return False
    return name in (_COMMON_RESULTS | _TASK_RESULTS.get(task, frozenset()))


def verified_result_hashes(
    root: Path, manifest: Mapping[str, Any]
) -> tuple[dict[str, str], list[str]]:
    """Return current allowlisted output hashes and all integrity failures."""
    with _verified_result_snapshots(Path(root), manifest) as (snapshots, issues):
        verified = {name: snapshot.sha256 for name, snapshot in snapshots.items()}
        for snapshot in snapshots.values():
            issue = _snapshot_integrity_issue(snapshot)
            if issue:
                issues.append(issue)
        return verified, sorted(set(issues))


def _explicit_contradictions(manifest: Mapping[str, Any], task: str) -> list[str]:
    diagnosis = _diagnosis(manifest)
    results = _manifest_results(manifest)
    issues: list[str] = []
    failure = str(diagnosis.get("failure_class") or "").strip().upper()
    if failure and failure != "CONVERGED":
        issues.append(f"explicit failure_class={failure}")
    for field in ("failed", "max_steps_hit"):
        if diagnosis.get(field) is True or results.get(field) is True:
            issues.append(f"explicit {field}=true")
    for field in ("task_converged", "electronic_converged"):
        if diagnosis.get(field) is False or results.get(field) is False:
            issues.append(f"explicit {field}=false")
    if task in (_IONIC_TASKS | {"neb"}):
        if diagnosis.get("ionic_converged") is False or results.get("ionic_converged") is False:
            issues.append("explicit ionic_converged=false")
    for field in ("clean_exit", "normal_termination"):
        if diagnosis.get(field) is False or results.get(field) is False:
            issues.append(f"explicit {field}=false")
    exit_code = diagnosis.get("exit_code", results.get("exit_code"))
    if isinstance(exit_code, bool) or (
        exit_code is not None and str(exit_code).strip() not in {"", "0", "0.0"}
    ):
        issues.append(f"explicit exit_code={exit_code}")
    return issues


def _contradiction_status(manifest: Mapping[str, Any], task: str) -> str | None:
    diagnosis = _diagnosis(manifest)
    results = _manifest_results(manifest)
    failure = str(diagnosis.get("failure_class") or "").strip().upper()
    exit_code = diagnosis.get("exit_code", results.get("exit_code"))
    failed_termination = any(
        diagnosis.get(field) is False or results.get(field) is False
        for field in ("clean_exit", "normal_termination")
    )
    nonzero_exit = isinstance(exit_code, bool) or (
        exit_code is not None and str(exit_code).strip() not in {"", "0", "0.0"}
    )
    if (
        failure in {"FAILED", "FATAL", "CRASHED"}
        or diagnosis.get("failed") is True
        or results.get("failed") is True
        or failed_termination
        or nonzero_exit
    ):
        return "failed"
    if failure in {
        "UNCONVERGED", "MAX_STEPS", "MAX_STEPS_HIT",
        "ELECTRONIC_NONCONVERGENCE", "IONIC_NONCONVERGENCE",
    }:
        return "unconverged"
    if failure not in {"", "CONVERGED", "NEEDS_HUMAN", "UNKNOWN"}:
        return "failed"
    if diagnosis.get("max_steps_hit") is True or results.get("max_steps_hit") is True:
        return "unconverged"
    if any(
        diagnosis.get(field) is False or results.get(field) is False
        for field in ("task_converged", "electronic_converged")
    ):
        return "unconverged"
    if task in (_IONIC_TASKS | {"neb"}) and (
        diagnosis.get("ionic_converged") is False
        or results.get("ionic_converged") is False
    ):
        return "unconverged"
    return None


def _energy_consistency(values: list[tuple[str, Any]]) -> tuple[float | None, list[str]]:
    finite = [
        (name, float(value))
        for name, value in values
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    ]
    if not finite:
        return None, ["hash-bound output has no finite E0 energy"]
    reference_name, reference = finite[0]
    issues = [
        f"hash-bound E0 energies disagree:{reference_name}/{name}"
        for name, value in finite[1:]
        if abs(reference - value) > 1e-3
    ]
    return reference, issues


def _verify_single(
    manifest: Mapping[str, Any], snapshots: Mapping[str, _ResultSnapshot]
) -> dict:
    task = str(manifest.get("task_type") or "").strip().lower()
    issues = _explicit_contradictions(manifest, task)
    if "OSZICAR" not in snapshots:
        issues.append("OSZICAR is not hash-bound and verifiable")
    if not {"OUTCAR", "vasprun.xml"}.intersection(snapshots):
        issues.append("OUTCAR or vasprun.xml is not hash-bound and verifiable")
    osz_snapshot = snapshots.get("OSZICAR")
    out_snapshot = snapshots.get("OUTCAR")
    xml_snapshot = snapshots.get("vasprun.xml")
    osz = (
        result_import._parse_oszicar_handle(osz_snapshot.handle, osz_snapshot.size)
        if osz_snapshot else result_import._parse_oszicar(None)
    )
    out = (
        result_import._parse_outcar_handle(out_snapshot.handle, out_snapshot.size)
        if out_snapshot else result_import._parse_outcar(None)
    )
    xml = (
        result_import._parse_vasprun_handle(xml_snapshot.handle)
        if xml_snapshot else result_import._parse_vasprun(None)
    )
    observed_version = _observed_vasp_version(out_snapshot)
    planned_version = _planned_vasp_version(manifest)
    if observed_version and planned_version and observed_version != planned_version:
        issues.append(
            f"OUTCAR VASP version {observed_version} conflicts with planned {planned_version}"
        )
    if osz.get("trailing_incomplete_scf"):
        issues.append("OSZICAR ends with an incomplete electronic step")
    if out.get("fatal_error"):
        issues.append(f"OUTCAR fatal evidence:{out['fatal_error']}")
    if out.get("soft_stopped"):
        issues.append("OUTCAR contains soft-stop evidence")
    if out.get("present") and not out.get("normal_footer"):
        issues.append("OUTCAR normal completion footer is missing")
    if xml.get("present") and not xml.get("complete"):
        issues.append("vasprun.xml is not complete")
    nelm = out.get("parameters", {}).get("NELM") or xml.get("parameters", {}).get("NELM")
    final_scf = osz.get("final_scf_steps") or xml.get("final_scf_steps")
    electronic_positive = bool(out.get("electronic_converged_marker")) or bool(
        xml.get("complete") and xml.get("calculations")
    ) or bool(
        out.get("normal_footer") and isinstance(final_scf, int)
        and isinstance(nelm, int) and final_scf < nelm
    )
    if not electronic_positive:
        issues.append("electronic convergence is not positively established")
    if task in _IONIC_TASKS:
        ediffg = out.get("parameters", {}).get("EDIFFG")
        ionic_positive = bool(out.get("ionic_converged_marker"))
        if isinstance(ediffg, (int, float)) and not isinstance(ediffg, bool):
            if ediffg < 0 and isinstance(out.get("final_force_max_eV_A"), (int, float)):
                ionic_positive = out["final_force_max_eV_A"] <= abs(ediffg)
            elif ediffg > 0 and isinstance(osz.get("final_ionic_dE_eV"), (int, float)):
                ionic_positive = abs(osz["final_ionic_dE_eV"]) <= ediffg
        if not ionic_positive:
            issues.append(f"{task} ionic convergence is not positively established")
    if task == "aimd":
        inputs = manifest.get("inputs") if isinstance(manifest.get("inputs"), Mapping) else {}
        expected = inputs.get("steps") or inputs.get("nsw")
        if isinstance(expected, int) and osz.get("ionic_steps", 0) < expected:
            issues.append("AIMD output has fewer steps than requested")
    energy, energy_issues = _energy_consistency([
        ("OSZICAR", osz.get("energy_e0_eV")),
        ("OUTCAR", out.get("energy_e0_eV")),
        ("vasprun.xml", xml.get("energy_e0_eV")),
    ])
    issues.extend(energy_issues)
    parser = {
        "schema": OUTPUT_PARSER_SCHEMA,
        "name": OUTPUT_PARSER_NAME,
        "version": OUTPUT_PARSER_VERSION,
        "task_type": task,
        "source_sha256": {
            name: snapshots[name].sha256
            for name in ("OUTCAR", "OSZICAR", "vasprun.xml")
            if name in snapshots
        },
        "observed_vasp_version": observed_version,
        "matrix": "ionic" if task in _IONIC_TASKS else "completed-run",
    }
    return {"issues": issues, "energy_e0_eV": energy, "parser": parser}


def _verify_neb(
    root: Path, manifest: Mapping[str, Any], snapshots: Mapping[str, _ResultSnapshot]
) -> dict:
    issues = _explicit_contradictions(manifest, "neb")
    inputs = manifest.get("inputs") if isinstance(manifest.get("inputs"), Mapping) else {}
    try:
        n_images = int(inputs.get("n_images"))
    except (TypeError, ValueError):
        n_images = -1
    frames = [f"{index:02d}" for index in range(n_images + 2)] if 1 <= n_images <= 98 else []
    if not frames:
        issues.append("NEB image count is invalid")
    energies: list[float] = []
    parser_sources: dict[str, str] = {}
    image_forces: dict[str, float | None] = {}
    image_versions: dict[str, str] = {}
    planned_version = _planned_vasp_version(manifest)
    for frame in frames:
        osz_name = f"{frame}/OSZICAR"
        out_name = f"{frame}/OUTCAR"
        xml_name = f"{frame}/vasprun.xml"
        if osz_name not in snapshots:
            issues.append(f"NEB {frame} OSZICAR is not hash-bound")
        if out_name not in snapshots and xml_name not in snapshots:
            issues.append(f"NEB {frame} OUTCAR/vasprun.xml is not hash-bound")
        osz_snapshot = snapshots.get(osz_name)
        out_snapshot = snapshots.get(out_name)
        xml_snapshot = snapshots.get(xml_name)
        osz = (
            result_import._parse_oszicar_handle(osz_snapshot.handle, osz_snapshot.size)
            if osz_snapshot else result_import._parse_oszicar(None)
        )
        out = (
            result_import._parse_outcar_handle(out_snapshot.handle, out_snapshot.size)
            if out_snapshot else result_import._parse_outcar(None)
        )
        xml = (
            result_import._parse_vasprun_handle(xml_snapshot.handle)
            if xml_snapshot else result_import._parse_vasprun(None)
        )
        observed_version = _observed_vasp_version(out_snapshot)
        if observed_version:
            image_versions[frame] = observed_version
            if planned_version and observed_version != planned_version:
                issues.append(
                    f"NEB {frame} VASP version {observed_version} conflicts with "
                    f"planned {planned_version}"
                )
        parser_sources.update({
            name: snapshots[name].sha256
            for name in (osz_name, out_name, xml_name) if name in snapshots
        })
        if osz.get("trailing_incomplete_scf"):
            issues.append(f"NEB {frame} has an incomplete electronic step")
        if out.get("fatal_error") or out.get("soft_stopped"):
            issues.append(f"NEB {frame} has fatal/stop evidence")
        if out.get("present") and not out.get("normal_footer"):
            issues.append(f"NEB {frame} OUTCAR normal footer is missing")
        if xml.get("present") and not xml.get("complete"):
            issues.append(f"NEB {frame} vasprun.xml is incomplete")
        nelm = out.get("parameters", {}).get("NELM") or xml.get("parameters", {}).get("NELM")
        final_scf = osz.get("final_scf_steps") or xml.get("final_scf_steps")
        electronic_positive = bool(out.get("electronic_converged_marker")) or bool(
            xml.get("complete") and xml.get("calculations")
        ) or bool(
            out.get("normal_footer")
            and isinstance(final_scf, int) and isinstance(nelm, int) and final_scf < nelm
        )
        if not electronic_positive:
            issues.append(f"NEB {frame} electronic convergence is not established")
        image_forces[frame] = out.get("final_force_max_eV_A") or xml.get(
            "final_force_max_eV_A"
        )
        energy, energy_issues = _energy_consistency([
            (osz_name, osz.get("energy_e0_eV")),
            (out_name, out.get("energy_e0_eV")),
            (xml_name, xml.get("energy_e0_eV")),
        ])
        issues.extend(energy_issues)
        if energy is not None:
            energies.append(energy)
    if len(energies) != len(frames):
        issues.append("NEB energy evidence is incomplete")
    ediffg = None
    try:
        from vcstudio.generate.incar_builder import parse_incar
        incar = parse_incar((root / "INCAR").read_text(
            encoding="utf-8", errors="replace"
        ))
        ediffg = float(incar.get("EDIFFG"))
    except (OSError, TypeError, ValueError):
        pass
    force_limit = abs(ediffg) if isinstance(ediffg, float) and ediffg < 0 else 0.05
    intermediate = frames[1:-1]
    if not intermediate or not all(
        isinstance(image_forces.get(frame), (int, float))
        and float(image_forces[frame]) <= force_limit
        for frame in intermediate
    ):
        issues.append("NEB climbing images are not force-converged")
    parser = {
        "schema": OUTPUT_PARSER_SCHEMA,
        "name": OUTPUT_PARSER_NAME,
        "version": OUTPUT_PARSER_VERSION,
        "task_type": "neb",
        "source_sha256": parser_sources,
        "observed_vasp_versions": image_versions,
        "matrix": "all-images-complete",
    }
    return {
        "issues": issues,
        "energy_e0_eV": None,
        "parser": parser,
        "neb_energies_eV": energies,
    }


def verify_outputs(root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    root = Path(root)
    with _verified_result_snapshots(root, manifest) as (snapshots, hash_issues):
        hashes = {name: snapshot.sha256 for name, snapshot in snapshots.items()}
        task = str(manifest.get("task_type") or "").strip().lower()
        try:
            task = manifest_mod.normalize_task_type(task)
        except ValueError:
            for snapshot in snapshots.values():
                issue = _snapshot_integrity_issue(snapshot)
                if issue:
                    hash_issues.append(issue)
            return {
                "issues": sorted(set([
                    *hash_issues, f"unknown result task type:{task or '?'}"
                ])),
                "result_files": hashes, "energy_e0_eV": None,
                "parser": {
                    "schema": OUTPUT_PARSER_SCHEMA, "name": OUTPUT_PARSER_NAME,
                    "version": OUTPUT_PARSER_VERSION, "task_type": task,
                    "source_sha256": {}, "matrix": "unknown-fail-closed",
                },
            }
        if task in _STATIC_LIKE_TASKS:
            _TASK_RESULTS.setdefault(task, frozenset())
        parsed = (
            _verify_neb(root, manifest, snapshots)
            if task == "neb" else _verify_single(manifest, snapshots)
        )
        for snapshot in snapshots.values():
            issue = _snapshot_integrity_issue(snapshot)
            if issue:
                hash_issues.append(issue)
        issues = sorted(set([*hash_issues, *parsed.pop("issues")]))
        return {
            "issues": issues,
            "result_files": hashes,
            "contradiction_status": _contradiction_status(manifest, task),
            **parsed,
        }


__all__ = [
    "MAX_RESULT_FILES", "MAX_RESULT_TOTAL_BYTES", "OUTPUT_PARSER_NAME", "OUTPUT_PARSER_SCHEMA",
    "OUTPUT_PARSER_VERSION", "verified_result_hashes", "verify_outputs",
]
