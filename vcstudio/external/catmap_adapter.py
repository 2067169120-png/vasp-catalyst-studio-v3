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
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from vcstudio.project import kinetics


ADAPTER_ID = "vcstudio.catmap-process-adapter"
ADAPTER_VERSION = "1"
PREVIEW_SCHEMA = "vcstudio.catmap-export-preview/v1"
BUNDLE_SCHEMA = "vcstudio.catmap-export-bundle/v1"
MANIFEST_SCHEMA = "vcstudio.catmap-export-manifest/v1"
PROCESS_SCHEMA = "vcstudio.external-process-contract/v1"
CATMAP_LICENSE = "GPL-3.0"
CATMAP_PROJECT = "https://github.com/SUNCAT-Center/catmap"
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_ARTIFACT_BYTES = 20 * 1024 * 1024

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SAFE_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")
_CATMAP_NAME_RE = re.compile(r"[^A-Za-z0-9_]")
_OUTPUT_VARIABLES = [
    "turnover_frequency", "coverage", "selectivity", "rate_control",
    "selectivity_control", "rxn_order", "apparent_activation_energy",
    "free_energy", "directional_rates",
]


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


def _source_mapping(source) -> dict[str, Any]:
    if isinstance(source, Mapping):
        value = source
    else:
        provider = getattr(source, "kinetics_input", None)
        if not callable(provider):
            raise CatmapAdapterError(
                "network must be a mapping or KineticsInputProvider")
        value = provider()
    if not isinstance(value, Mapping):
        raise CatmapAdapterError("KineticsInputProvider must return a mapping")
    return copy.deepcopy(dict(value))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_link(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(callable(is_junction) and is_junction())


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


def _species_names(network: Mapping[str, Any]) -> dict[str, str]:
    names = {}
    used = set()
    for record in network.get("species") or []:
        species_id = str(record["id"])
        phase = str(record["phase"])
        sites = record.get("sites") or {}
        if phase == "surface":
            site = next(iter(sites))
            name = f"*_{site}"
        elif phase == "gas":
            name = _catmap_base(species_id, phase) + "_g"
        elif phase in {"adsorbate", "transition_state"}:
            site = next(iter(sites))
            name = _catmap_base(species_id, phase) + f"_{site}"
        else:
            raise CatmapAdapterError(
                f"CatMAP phase-1 export does not support species phase {phase!r}")
        if name in used:
            raise CatmapAdapterError(
                f"CatMAP identifier collision after normalization: {name}")
        used.add(name)
        names[species_id] = name
    return names


def _format_number(value: Any) -> str:
    number = float(value)
    if not math.isfinite(number):
        raise CatmapAdapterError("CatMAP export requires finite numbers")
    return format(number, ".15g")


def _format_frequencies(values: Any) -> str:
    return "[" + ", ".join(_format_number(value) for value in values or []) + "]"


def _table_text(network: Mapping[str, Any], names: Mapping[str, str]) -> str:
    header = [
        "surface_name", "site_name", "species_name", "formation_energy",
        "frequencies", "reference",
    ]
    surface_name = _catmap_base(str(network["network_id"]), "adsorbate")
    rows = ["\t".join(header)]
    for record in network.get("species") or []:
        phase = str(record["phase"])
        if phase == "surface":
            # Empty-site energies are represented by the CatMAP site balance.
            continue
        species_id = str(record["id"])
        energy = record["formation_energy"]
        source = energy["source"]
        if phase == "gas":
            table_surface, site_name = "None", "gas"
            species_name = names[species_id][:-2]
        elif phase in {"adsorbate", "transition_state"}:
            table_surface = surface_name
            site_name = next(iter(record.get("sites") or {}))
            suffix = "_" + site_name
            species_name = names[species_id][:-len(suffix)]
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


def _model_text(network: Mapping[str, Any], names: Mapping[str, str]) -> str:
    methodology = network.get("methodology") or {}
    if methodology.get("energy_basis") != "gibbs_free_energy":
        raise CatmapAdapterError(
            "phase-1 CatMAP export requires frozen Gibbs formation energies")
    if methodology.get("potential_model") != "none":
        raise CatmapAdapterError(
            "phase-1 CatMAP export does not encode electrochemical potential dependence")
    reactions = []
    prefactors = []
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
    site_types = sorted({
        site
        for species in network.get("species") or []
        for site in (species.get("sites") or {})
    })
    species_definitions = {
        site: {"site_names": [site], "total": 1.0}
        for site in site_types
    }
    for record in network.get("species") or []:
        if record.get("phase") == "gas":
            species_definitions[names[str(record["id"])]] = {
                "pressure": float(record["activity"]["value"]),
            }
    temperature = float(network["standard_state"]["temperature"]["value"])
    lines = [
        "# Independently generated CatMAP setup data; CatMAP is not bundled.",
        "# All formation energies are frozen Gibbs values in eV; no extra thermal correction.",
        "input_file = 'energetics.tsv'",
        f"rxn_expressions = {reactions!r}",
        f"surface_names = [{_catmap_base(str(network['network_id']), 'adsorbate')!r}]",
        f"species_definitions = {species_definitions!r}",
        f"temperature = {_format_number(temperature)}",
        f"prefactor_list = {prefactors!r}",
        "gas_thermo_mode = 'frozen_gas'",
        "adsorbate_thermo_mode = 'frozen_adsorbate'",
        "adsorbate_interaction_model = 'ideal'",
        "numerical_representation = 'mpmath'",
        f"output_variables = {_OUTPUT_VARIABLES!r}",
        "data_file = 'catmap-output.pkl'",
        "",
    ]
    return "\n".join(lines)


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
    return [
        {
            "name": name,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "size": len(text.encode("utf-8")),
        }
        for name, text in sorted(files.items())
    ]


def build_export_bundle(network_source, *, tool_path=None,
                        tool_version: str | None = None) -> dict[str, Any]:
    """Build an in-memory frozen bundle.  This function never executes a tool."""
    network = _source_mapping(network_source)
    audit = kinetics.audit_network(network)
    tool = inspect_tool_path(tool_path, version=tool_version)
    files = {"kinetics-audit.json": _json_text(audit)}
    adapter_issues = []
    if audit["export_ready"] is True:
        try:
            names = _species_names(network)
            files.update({
                "kinetics-input.json": _json_text(network),
                "energetics.tsv": _table_text(network, names),
                "model.mkm": _model_text(network, names),
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
    export_ready = audit["export_ready"] is True and not adapter_issues
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
        "preview_token": bundle["preview_token"],
        "explicit_confirmation_required": True,
        "capability_status": (
            "available" if export_ready and bundle["tool"]["available"] is True
            else "unavailable"),
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


def _atomic_write(path: Path, content: str) -> None:
    encoded = content.encode("utf-8")
    if path.exists():
        if path.is_file() and path.read_bytes() == encoded:
            return
        raise CatmapAdapterError(f"refusing to overwrite changed export file {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".vcs-kinetics-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _safe_child_directory(parent: Path, name: str, root: Path) -> Path:
    path = parent / name
    if path.exists() and _is_link(path):
        raise CatmapAdapterError(f"export directory {name} must not be a symlink")
    path.mkdir(exist_ok=True)
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise CatmapAdapterError("export directory escaped the project root") from exc
    if not resolved.is_dir():
        raise CatmapAdapterError(f"export directory {name} is not a directory")
    return resolved


def confirm_export(network_source, project_root, preview_token: str, *,
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
    root = Path(project_root)
    if not root.is_absolute():
        root = root.resolve()
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise CatmapAdapterError("project root does not exist") from exc
    if not root.is_dir():
        raise CatmapAdapterError("project root must be a directory")
    export_parent = root
    for name in (".vcstudio", "kinetics", "exports"):
        export_parent = _safe_child_directory(export_parent, name, root)
    identity = bundle.get("input_sha256") or bundle["preview_token"]
    final_dir = export_parent / identity
    expected_files = set(bundle["files"])
    if final_dir.exists():
        if _is_link(final_dir) or not final_dir.is_dir():
            raise CatmapAdapterError("final export directory is unsafe")
        resolved_export = final_dir.resolve(strict=True)
        try:
            resolved_export.relative_to(root)
        except ValueError as exc:
            raise CatmapAdapterError("final export directory escaped project root") from exc
        entries = list(resolved_export.iterdir())
        if any(_is_link(item) or not item.is_file() for item in entries):
            raise CatmapAdapterError("existing export directory contains unsafe entries")
        actual = {item.name for item in entries}
        if actual != expected_files:
            raise CatmapAdapterError("existing export directory does not match the preview")
        for name, content in bundle["files"].items():
            if (resolved_export / name).read_bytes() != content.encode("utf-8"):
                raise CatmapAdapterError(
                    f"refusing to overwrite changed export file {name}")
    else:
        stage = Path(tempfile.mkdtemp(prefix=".vcs-kinetics-stage-", dir=export_parent))
        try:
            resolved_stage = stage.resolve(strict=True)
            if resolved_stage.parent != export_parent:
                raise CatmapAdapterError("staged export escaped project boundary")
            for name, content in sorted(bundle["files"].items()):
                if Path(name).name != name or name in {".", ".."}:
                    raise CatmapAdapterError("export artifact name is unsafe")
                _atomic_write(resolved_stage / name, content)
            os.replace(resolved_stage, final_dir)
            resolved_export = final_dir.resolve(strict=True)
        finally:
            if stage.exists():
                resolved_stage = stage.resolve(strict=True)
                if resolved_stage.parent != export_parent:
                    raise CatmapAdapterError("refusing to clean unsafe staged export")
                shutil.rmtree(resolved_stage)
    return {
        "ok": True,
        "schema": MANIFEST_SCHEMA,
        "input_sha256": bundle["input_sha256"],
        "preview_token": bundle["preview_token"],
        "export_dir": str(resolved_export),
        "files": sorted(bundle["files"]),
        "audit": copy.deepcopy(bundle["audit"]),
        "tool": copy.deepcopy(bundle["tool"]),
        "scientific_status": "diagnostic",
        "eligible_final": False,
    }


def load_confirmed_manifest(project_root, input_sha256: str) -> dict[str, Any]:
    """Revalidate one fixed export manifest and every declared artifact hash."""
    if not isinstance(input_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", input_sha256):
        raise CatmapAdapterError("input_sha256 must be a SHA-256 value")
    root = Path(project_root)
    if not root.is_absolute():
        root = root.resolve()
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise CatmapAdapterError("project root does not exist") from exc
    export_dir = root
    for name in (".vcstudio", "kinetics", "exports", input_sha256):
        candidate = export_dir / name
        if _is_link(candidate) or not candidate.is_dir():
            raise CatmapAdapterError("confirmed CatMAP export manifest is unavailable")
        export_dir = candidate.resolve(strict=True)
        try:
            export_dir.relative_to(root)
        except ValueError as exc:
            raise CatmapAdapterError(
                "confirmed CatMAP export directory escaped project root") from exc
    manifest_path = export_dir / "manifest.json"
    if _is_link(manifest_path) or not manifest_path.is_file():
        raise CatmapAdapterError("confirmed CatMAP export manifest is unavailable")
    try:
        if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
            raise CatmapAdapterError(
                "confirmed CatMAP export manifest exceeds the size limit")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CatmapAdapterError("confirmed CatMAP export manifest is invalid") from exc
    if not isinstance(manifest, Mapping) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise CatmapAdapterError("confirmed CatMAP export manifest schema mismatch")
    if manifest.get("input_sha256") != input_sha256:
        raise CatmapAdapterError("confirmed CatMAP export input hash mismatch")
    adapter = manifest.get("adapter")
    if adapter != {"id": ADAPTER_ID, "version": ADAPTER_VERSION}:
        raise CatmapAdapterError("confirmed CatMAP export adapter mismatch")
    tool = manifest.get("external_tool")
    if not isinstance(tool, Mapping):
        raise CatmapAdapterError("confirmed CatMAP export tool identity is invalid")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise CatmapAdapterError("confirmed CatMAP export artifact manifest is empty")
    normalized_artifacts = []
    for index, item in enumerate(artifacts):
        if (not isinstance(item, Mapping)
                or set(item) != {"name", "sha256", "size"}):
            raise CatmapAdapterError(
                f"confirmed CatMAP export artifact {index} is invalid")
        name = item.get("name")
        digest = item.get("sha256")
        size = item.get("size")
        if (not isinstance(name, str) or Path(name).name != name
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or isinstance(size, bool) or not isinstance(size, int)
                or not 0 <= size <= _MAX_ARTIFACT_BYTES):
            raise CatmapAdapterError(
                f"confirmed CatMAP export artifact {index} metadata is unsafe")
        artifact_path = export_dir / name
        if _is_link(artifact_path) or not artifact_path.is_file():
            raise CatmapAdapterError(f"confirmed CatMAP export artifact {name} is missing")
        if artifact_path.stat().st_size != size:
            raise CatmapAdapterError(
                f"confirmed CatMAP export artifact size mismatch: {name}")
        data = artifact_path.read_bytes()
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise CatmapAdapterError(f"confirmed CatMAP export artifact hash mismatch: {name}")
        normalized_artifacts.append(dict(item))
    token_payload = {
        "schema": BUNDLE_SCHEMA,
        "input_sha256": input_sha256,
        "adapter": {"id": ADAPTER_ID, "version": ADAPTER_VERSION},
        "tool": dict(tool),
        "artifacts": normalized_artifacts,
    }
    token = hashlib.sha256(_canonical_bytes(token_payload)).hexdigest()
    if manifest.get("preview_token") != token:
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
        "adapter": {"id": ADAPTER_ID, "version": ADAPTER_VERSION},
        "tool": dict(tool),
        "artifacts": normalized_artifacts,
        "scientific_status": "diagnostic",
        "eligible_final": False,
    }


__all__ = [
    "ADAPTER_ID", "ADAPTER_VERSION", "BUNDLE_SCHEMA", "CATMAP_LICENSE",
    "CatmapAdapterError", "MANIFEST_SCHEMA", "PREVIEW_SCHEMA", "PROCESS_SCHEMA",
    "build_export_bundle", "confirm_export", "inspect_tool_path",
    "load_confirmed_manifest", "preview_export",
]
