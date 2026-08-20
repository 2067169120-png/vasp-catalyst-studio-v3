"""Fail-closed project clone, move, and copied-folder adoption.

The project registry and ``project.yaml`` are the authorities for location and
identity.  Every mutation is preceded by a hash-bound :class:`LifecyclePlan`;
``apply`` rejects a plan when either authority changed after preflight.

Only schema-known project-local locators are rebased.  Provenance strings,
report markers, notes, and arbitrary YAML values are deliberately left alone.
"""
from __future__ import annotations

import copy
import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from vcstudio.cluster import ledger as cluster_ledger


PROJECT_FILE = "project.yaml"
_OWNER_FILE = ".vcstudio-lifecycle-owner.json"
_UUID_RE = re.compile(r"[a-fA-F0-9]{32}")
_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_REGISTRY_LOCK = threading.RLock()


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterable[None]:
    """Take a non-blocking OS advisory lock on one stable lock-file inode.

    The in-process lock below keeps threads and service instances ordered.  The
    file lock is the authority across GUI processes; unlike the transaction
    journal it is never replaced, so two processes cannot both pass an
    ``exists`` check and publish competing journals.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = path.open("a+b")
    except OSError as exc:
        raise ProjectLifecycleError(
            "lifecycle_lock_unavailable",
            "The global project lifecycle lock could not be opened.",
        ) from exc
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except (BlockingIOError, OSError) as exc:
            raise ProjectLifecycleError(
                "lifecycle_busy",
                "Another project lifecycle transaction is currently running.",
            ) from exc
        yield
    finally:
        if locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                # Closing the descriptor also releases the kernel lock.
                pass
        handle.close()


class ProjectLifecycleError(RuntimeError):
    """A lifecycle operation failed without claiming partial success."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = str(code)


class ProjectLifecyclePartialRollbackError(ProjectLifecycleError):
    """The operation failed and automatic rollback could not be completed."""


@dataclass(frozen=True)
class JobLifecycleChange:
    """One jobs.json projection and, when reminted, one job.yaml identity edit."""

    source_path: str
    target_path: str
    ledger_action: str
    source_identity: str | None
    target_identity: str | None
    source_manifest_sha256: str | None
    label: str
    remint_identity: bool


@dataclass(frozen=True)
class LifecyclePlan:
    """Immutable preflight output consumed by :meth:`ProjectLifecycleService.apply`."""

    action: str
    source_project_file: str
    source_root: str
    destination_root: str | None
    registry_path: str
    registry_sha256: str
    ledger_path: str
    ledger_sha256: str
    project_sha256: str
    source_project_uuid: str | None
    target_project_uuid: str | None
    identity_mode: str
    source_name: str
    target_name: str
    locator_rewrites: int
    external_locators: int
    job_changes: tuple[JobLifecycleChange, ...]
    conflicts: tuple[dict[str, str], ...]
    warnings: tuple[dict[str, str], ...]
    token: str

    @property
    def ready(self) -> bool:
        return not self.conflicts

    def public_summary(self) -> dict[str, Any]:
        """Return a path-free representation suitable for a GUI bridge."""
        return {
            "ok": self.ready,
            "ready": self.ready,
            "action": self.action,
            "project": {
                "name": self.source_name,
                "project_uuid": self.source_project_uuid,
            },
            "target": {
                "name": self.target_name,
                "project_uuid": self.target_project_uuid,
            },
            "identity_mode": self.identity_mode,
            "impact": {
                "registry_update": True,
                "locator_rewrites": self.locator_rewrites,
                "external_locators_preserved": self.external_locators,
                "project_uuid_preserved": (
                    self.target_project_uuid is not None
                    and self.target_project_uuid == self.source_project_uuid
                ),
                "project_uuid_reminted": (
                    self.target_project_uuid is not None
                    and self.target_project_uuid != self.source_project_uuid
                ),
                "jobs_ledger_updates": len(self.job_changes),
            },
            "jobs": {
                "count": len(self.job_changes),
                "entries": [
                    {
                        "label": item.label,
                        "action": item.ledger_action,
                        "source_identity": item.source_identity,
                        "target_identity": item.target_identity,
                        "identity_reminted": item.remint_identity,
                    }
                    for item in self.job_changes
                ],
            },
            "conflicts": [dict(item) for item in self.conflicts],
            "warnings": [dict(item) for item in self.warnings],
            "error": None if self.ready else self.conflicts[0]["message"],
        }


def _issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ProjectLifecycleError(
            "project_unreadable", "The selected project.yaml could not be read."
        ) from exc


def _path_key(path: str | os.PathLike[str]) -> str:
    value = os.path.realpath(os.path.abspath(os.path.expanduser(os.fspath(path))))
    return os.path.normcase(os.path.normpath(value))


def _canonical_path(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.realpath(os.path.abspath(
        os.path.expanduser(os.fspath(path)))))


def _same_filesystem(source: Path, destination_parent: Path) -> bool:
    try:
        return os.stat(source).st_dev == os.stat(destination_parent).st_dev
    except OSError:
        return False


def _rename_noreplace(source: str | os.PathLike[str],
                      destination: str | os.PathLike[str]) -> None:
    """Atomically rename a directory while refusing an existing target."""
    source_value = os.fspath(source)
    destination_value = os.fspath(destination)
    if os.name == "nt":
        # MoveFile on Windows already fails when the destination exists.
        os.rename(source_value, destination_value)
        return
    if os.name == "posix":
        # Linux renameat2 closes the empty-directory replacement race present
        # in plain os.rename.  Keep a guarded fallback for other POSIX hosts.
        libc = None
        try:
            import ctypes
            import errno

            libc = ctypes.CDLL(None, use_errno=True)
            renameat2 = libc.renameat2
        except (AttributeError, OSError):
            renameat2 = None
        if renameat2 is not None:
            renameat2.argtypes = [
                ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                ctypes.c_char_p, ctypes.c_uint,
            ]
            renameat2.restype = ctypes.c_int
            result = renameat2(
                -100, os.fsencode(source_value), -100,
                os.fsencode(destination_value), 1)
            if result == 0:
                return
            code = ctypes.get_errno()
            if code == errno.EEXIST:
                raise FileExistsError(code, os.strerror(code), destination_value)
            raise OSError(code, os.strerror(code), destination_value)
        try:
            import sys

            renamex_np = (libc.renamex_np
                          if libc is not None and sys.platform == "darwin" else None)
        except (AttributeError, OSError):
            renamex_np = None
        if renamex_np is not None:
            renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            renamex_np.restype = ctypes.c_int
            result = renamex_np(
                os.fsencode(source_value), os.fsencode(destination_value), 0x4)
            if result == 0:
                return
            code = ctypes.get_errno()
            if code == errno.EEXIST:
                raise FileExistsError(code, os.strerror(code), destination_value)
            raise OSError(code, os.strerror(code), destination_value)
    raise ProjectLifecycleError(
        "atomic_rename_unavailable",
        "This platform cannot publish a project directory without an overwrite race.",
    )


def _project_file(path: str | os.PathLike[str], *, must_exist: bool = True) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_dir():
        candidate = candidate / PROJECT_FILE
    candidate = Path(os.path.realpath(os.path.abspath(os.fspath(candidate))))
    if must_exist and not candidate.is_file():
        raise ProjectLifecycleError(
            "project_not_found", "The selected folder does not contain project.yaml."
        )
    return candidate


def _parse_project(payload: bytes) -> dict[str, Any]:
    try:
        value = yaml.safe_load(payload.decode("utf-8"))
    except UnicodeError as exc:
        raise ProjectLifecycleError(
            "project_invalid", "project.yaml is not valid UTF-8."
        ) from exc
    except yaml.YAMLError as exc:
        raise ProjectLifecycleError(
            "project_invalid", "project.yaml is not valid YAML."
        ) from exc
    if not isinstance(value, dict) or not isinstance(value.get("members"), dict):
        raise ProjectLifecycleError(
            "project_invalid", "project.yaml is not a recognized project document."
        )
    return value


def _load_project(path: Path) -> dict[str, Any]:
    return _parse_project(_read_bytes(path))


def _project_uuid(project: dict[str, Any]) -> str | None:
    preparation = project.get("preparation")
    prepared = preparation.get("project_uuid") if isinstance(preparation, dict) else None
    for value in (project.get("project_uuid"), prepared):
        compact = str(value or "").strip().lower().replace("-", "")
        if _UUID_RE.fullmatch(compact):
            return compact
    return None


def _identity_conflict(project: dict[str, Any]) -> bool:
    preparation = project.get("preparation")
    prepared = preparation.get("project_uuid") if isinstance(preparation, dict) else None
    top = str(project.get("project_uuid") or "").strip().lower().replace("-", "")
    nested = str(prepared or "").strip().lower().replace("-", "")
    return bool(_UUID_RE.fullmatch(top) and _UUID_RE.fullmatch(nested) and top != nested)


def _recorded_root(project: dict[str, Any], actual_root: Path) -> Path:
    raw = str(project.get("root") or "").strip()
    if not raw:
        return actual_root
    candidate = Path(os.path.expanduser(raw))
    if not candidate.is_absolute():
        candidate = actual_root / candidate
    candidate = Path(os.path.realpath(os.path.abspath(os.fspath(candidate))))
    return candidate


def _relative_within(path: str, root: Path) -> str | None:
    """Return a lexical relative path when ``path`` is inside ``root``."""
    if not path or not os.path.isabs(os.path.expanduser(path)):
        return None
    candidate = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    root_path = os.path.realpath(os.path.abspath(os.path.expanduser(os.fspath(root))))
    candidate_key = os.path.normcase(os.path.normpath(candidate))
    root_key = os.path.normcase(os.path.normpath(root_path))
    try:
        common = os.path.commonpath((candidate_key, root_key))
    except ValueError:
        return None
    if common != root_key:
        return None
    relative = os.path.relpath(candidate, root_path)
    return relative


def _rewrite_locator(value: Any, roots: Iterable[Path], target_root: Path) -> tuple[Any, bool]:
    if not isinstance(value, str) or not value.strip():
        return value, False
    roots = tuple(roots)
    expanded = os.path.expanduser(value)
    drive, _tail = os.path.splitdrive(expanded)
    if not os.path.isabs(expanded):
        if drive:
            raise ProjectLifecycleError(
                "unsafe_relative_locator",
                "A drive-relative project locator cannot be rebased safely.",
            )
        candidate = roots[0] / expanded
        if _relative_within(str(candidate), roots[0]) is None:
            raise ProjectLifecycleError(
                "unsafe_relative_locator",
                "A relative project locator escapes the selected project folder.",
            )
        # A project-local relative locator already follows the project root and
        # therefore needs no textual change after clone/move/adopt.
        return value, True
    for root in roots:
        relative = _relative_within(value, root)
        if relative is None:
            continue
        target = target_root if relative == "." else target_root / relative
        return str(target), True
    return value, False


def _rewrite_tree_values(value: Any, roots: tuple[Path, ...], target_root: Path,
                         counts: dict[str, int]) -> Any:
    if isinstance(value, str):
        rewritten, changed = _rewrite_locator(value, roots, target_root)
        counts["rewritten" if changed else "external"] += 1
        return rewritten
    if isinstance(value, list):
        return [_rewrite_tree_values(item, roots, target_root, counts) for item in value]
    if isinstance(value, tuple):
        return [_rewrite_tree_values(item, roots, target_root, counts) for item in value]
    if isinstance(value, dict):
        return {
            key: _rewrite_tree_values(item, roots, target_root, counts)
            for key, item in value.items()
        }
    return copy.deepcopy(value)


def _rewrite_mapping_keys(value: Any, roots: tuple[Path, ...], target_root: Path,
                          counts: dict[str, int]) -> Any:
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    result = {}
    for key, item in value.items():
        new_key, changed = _rewrite_locator(key, roots, target_root)
        if isinstance(key, str):
            counts["rewritten" if changed else "external"] += 1
        if new_key in result:
            raise ProjectLifecycleError(
                "locator_collision",
                "Rebasing project-local locators would create a duplicate mapping.",
            )
        result[new_key] = copy.deepcopy(item)
    return result


def _rewrite_project(project: dict[str, Any], actual_root: Path,
                     target_root: Path) -> tuple[dict[str, Any], dict[str, int]]:
    """Rebase only schema-known member/reference locators."""
    rewritten = copy.deepcopy(project)
    recorded = _recorded_root(project, actual_root)
    roots = tuple(dict.fromkeys((actual_root, recorded)))
    counts = {"rewritten": 0, "external": 0}
    rewritten["root"] = str(target_root)

    for key in ("members", "species_ref_jobs", "standalone"):
        if key in rewritten:
            rewritten[key] = _rewrite_tree_values(
                rewritten[key], roots, target_root, counts
            )
    for key in ("molecules_dir", "reference_project"):
        if key in rewritten and rewritten[key] is not None:
            value, changed = _rewrite_locator(rewritten[key], roots, target_root)
            if isinstance(rewritten[key], str):
                counts["rewritten" if changed else "external"] += 1
            rewritten[key] = value
    for key in ("config_species", "config_species_evidence"):
        if key in rewritten:
            rewritten[key] = _rewrite_mapping_keys(
                rewritten[key], roots, target_root, counts
            )
    evidence_map = rewritten.get("config_species_evidence")
    if isinstance(evidence_map, dict):
        for evidence in evidence_map.values():
            if not isinstance(evidence, dict):
                continue
            for key in ("clean_member", "config_member", "reference_job"):
                if evidence.get(key) is None:
                    continue
                value, changed = _rewrite_locator(evidence[key], roots, target_root)
                if isinstance(evidence[key], str):
                    counts["rewritten" if changed else "external"] += 1
                evidence[key] = value

    groups = rewritten.get("dataset_groups")
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict):
                continue
            if "configs" in group:
                group["configs"] = _rewrite_tree_values(
                    group["configs"], roots, target_root, counts
                )
            if group.get("reference_job") is not None:
                value, changed = _rewrite_locator(
                    group["reference_job"], roots, target_root
                )
                if isinstance(group["reference_job"], str):
                    counts["rewritten" if changed else "external"] += 1
                group["reference_job"] = value
    return rewritten, counts


def _namespace(name: str, project_uuid: str) -> str:
    stem = _NAME_RE.sub("_", str(name or "project")).strip("._-")[:80] or "project"
    return f"{stem}-{project_uuid}"


def _apply_identity(project: dict[str, Any], project_uuid: str) -> str | None:
    project["project_uuid"] = project_uuid
    preparation = project.get("preparation")
    if isinstance(preparation, dict):
        preparation["project_uuid"] = project_uuid
    namespace = _namespace(str(project.get("name") or "project"), project_uuid)
    project["remote_namespace"] = namespace
    return namespace


def _iter_locator_values(project: dict[str, Any]) -> Iterable[str]:
    def walk(value: Any) -> Iterable[str]:
        if isinstance(value, str):
            yield value
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from walk(item)
        elif isinstance(value, dict):
            for item in value.values():
                yield from walk(item)

    for key in ("members", "species_ref_jobs", "standalone"):
        yield from walk(project.get(key))
    for key in ("molecules_dir", "reference_project"):
        value = project.get(key)
        if isinstance(value, str):
            yield value


def _manifest_identity(manifest: Any) -> str | None:
    if not isinstance(manifest, dict):
        return None
    value = str(manifest.get("job_uuid") or manifest.get("job_id") or "").strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        return value
    return None


def _local_job_directories(project: dict[str, Any], root: Path) -> list[Path]:
    directories = []
    seen = set()

    def remember(candidate: Path) -> None:
        job_file = candidate / "job.yaml"
        if (not job_file.is_file()
                or _relative_within(str(job_file), root) is None):
            return
        key = _path_key(candidate)
        if key not in seen:
            seen.add(key)
            directories.append(Path(os.path.realpath(candidate)))

    for locator in _iter_locator_values(project):
        candidate = Path(locator).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        if _relative_within(str(candidate), root) is None:
            continue
        if candidate.is_file():
            continue
        remember(candidate)

    # Jobs can live under schema-owned collection roots (for example
    # ``molecules_dir``) without every leaf being repeated in project.yaml.
    # Walk without following links and retain only physically project-local
    # manifests so clone/adopt cannot leave copied jobs sharing an identity.
    for current, child_dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        child_dirs[:] = [
            name for name in child_dirs
            if _relative_within(str(current_path / name), root) is not None
        ]
        if "job.yaml" in files:
            remember(current_path)
    return directories


def _job_manifest_updates(project: dict[str, Any], locator_root: Path,
                          namespace: str | None, *, physical_root: Path | None = None,
                          job_identity_by_target: dict[str, str] | None = None
                          ) -> list[tuple[Path, bytes, dict[str, Any]]]:
    identity_map = job_identity_by_target or {}
    if not namespace and not identity_map:
        return []
    updates = []
    physical_root = physical_root or locator_root
    seen = set()
    candidates: list[Path] = []
    for locator in _iter_locator_values(project):
        candidate = Path(locator).expanduser()
        if not candidate.is_absolute():
            candidate = locator_root / candidate
        candidates.append(candidate)
    # Identity preflight discovers nested project-local manifests as well as
    # direct locators.  Include every bound target so the manifest projection
    # exactly matches the jobs ledger projection.
    candidates.extend(Path(value) for value in identity_map)
    for candidate in candidates:
        relative = _relative_within(str(candidate), locator_root)
        if relative is None:
            continue
        physical = physical_root if relative == "." else physical_root / relative
        job_file = physical / "job.yaml"
        if _relative_within(str(job_file), physical_root) is None:
            # A project-local-looking symlink must never turn identity repair
            # into a write outside the cloned/adopted project tree.
            continue
        key = _path_key(job_file)
        if key in seen or not job_file.is_file():
            continue
        seen.add(key)
        original = _read_bytes(job_file)
        try:
            manifest = yaml.safe_load(original.decode("utf-8"))
        except (UnicodeError, yaml.YAMLError) as exc:
            raise ProjectLifecycleError(
                "job_manifest_invalid",
                "A project-local job.yaml could not be updated safely.",
            ) from exc
        if not isinstance(manifest, dict):
            raise ProjectLifecycleError(
                "job_manifest_invalid",
                "A project-local job.yaml could not be updated safely.",
            )
        inputs = manifest.setdefault("inputs", {})
        if not isinstance(inputs, dict):
            raise ProjectLifecycleError(
                "job_manifest_invalid", "A job.yaml inputs section is invalid."
            )
        if namespace:
            inputs["remote_namespace"] = namespace
        target_identity = identity_map.get(_path_key(candidate))
        if target_identity:
            manifest["job_uuid"] = target_identity
        updates.append((job_file, original, manifest))
    return updates


def _yaml_bytes(value: dict[str, Any]) -> bytes:
    return yaml.safe_dump(
        value, allow_unicode=True, sort_keys=False
    ).encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temp, "xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


class ProjectLifecycleService:
    """Coordinate project filesystem changes with the authoritative registry."""

    def __init__(self, registry_path: str | os.PathLike[str], *,
                 ledger_path: str | os.PathLike[str] | None = None,
                 journal_path: str | os.PathLike[str] | None = None,
                 lock_path: str | os.PathLike[str] | None = None):
        self.registry_path = _canonical_path(registry_path)
        self.ledger_path = (_canonical_path(ledger_path) if ledger_path is not None
                            else self.registry_path.with_name("jobs.json"))
        self.journal_path = (_canonical_path(journal_path) if journal_path is not None
                             else self.registry_path.with_name(
                                 "project-lifecycle-journal.json"))
        # This path is derived from the canonical registry, not from the
        # replaceable journal.  All processes coordinating that registry use
        # the same stable inode even when a test injects a custom journal path.
        self.lock_path = (_canonical_path(lock_path) if lock_path is not None
                          else self.registry_path.with_name(
                              ".vcstudio-project-lifecycle.lock"))
        self._copytree = shutil.copytree
        self._move_path = _rename_noreplace
        self._remove_tree = shutil.rmtree
        self.recover_pending()

    @contextmanager
    def _transaction_lock(self) -> Iterable[None]:
        with _REGISTRY_LOCK:
            with _exclusive_file_lock(self.lock_path):
                yield

    def _read_registry(self) -> tuple[dict[str, Any], list[str], str]:
        target = self.registry_path
        if not target.exists():
            return {"projects": []}, [], _sha256_bytes(b"")
        try:
            raw = target.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProjectLifecycleError(
                "registry_invalid", "The project registry could not be read safely."
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("projects"), list):
            raise ProjectLifecycleError(
                "registry_invalid", "The project registry has an invalid schema."
            )
        if not all(isinstance(item, str) and item.strip() for item in payload["projects"]):
            raise ProjectLifecycleError(
                "registry_invalid", "The project registry contains an invalid locator."
            )
        return payload, list(payload["projects"]), _sha256_bytes(raw)

    def _write_registry(self, payload: dict[str, Any], entries: list[str]) -> None:
        _atomic_write(self.registry_path, self._authority_projection(
            payload, "projects", entries))

    def _read_ledger(self) -> tuple[dict[str, Any], list[str], str]:
        try:
            snapshot = cluster_ledger.projection_snapshot(path=self.ledger_path)
        except cluster_ledger.LedgerProjectionError as exc:
            raise ProjectLifecycleError(
                "ledger_invalid", "The jobs ledger could not be read safely."
            ) from exc
        return (copy.deepcopy(snapshot["payload"]), list(snapshot["job_dirs"]),
                str(snapshot["sha256"]))

    def _write_ledger(self, *, transaction_id: str, base_sha256: str,
                      base_entries: list[str],
                      entries: list[str]) -> dict[str, object]:
        return cluster_ledger.merge_projection(
            transaction_id=transaction_id, base_sha256=base_sha256,
            base_entries=base_entries,
            projected_entries=entries, path=self.ledger_path)

    @staticmethod
    def _authority_projection(payload: dict[str, Any], key: str,
                              entries: list[str]) -> bytes:
        updated = copy.deepcopy(payload)
        updated[key] = entries
        return json.dumps(updated, ensure_ascii=False, indent=2).encode("utf-8")

    @staticmethod
    def _snapshot_payload(snapshot: dict[str, Any]) -> bytes:
        if not snapshot.get("exists"):
            return b""
        try:
            return base64.b64decode(
                str(snapshot.get("data_b64") or ""), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ProjectLifecycleError(
                "journal_invalid", "A recovery snapshot is invalid."
            ) from exc

    @classmethod
    def _restore_authority_snapshot(cls, snapshot: dict[str, Any],
                                    written_sha256: str | None,
                                    projection_b64: str | None,
                                    list_key: str,
                                    transaction_id: str | None = None) -> None:
        """Undo only this transaction's projection, preserving other writers.

        A CAS failure can mean a non-lifecycle writer legitimately changed the
        registry or ledger while filesystem staging was in progress.  Recovery
        must not erase that newer state.  It restores the snapshot only when
        the current bytes are either the original bytes or the exact projection
        recorded by this journal.
        """
        path = Path(str(snapshot.get("path") or ""))
        if not path.name:
            raise ProjectLifecycleError(
                "journal_invalid", "A recovery snapshot path is invalid."
            )
        if list_key == "job_dirs":
            if not transaction_id:
                raise ProjectLifecycleError(
                    "journal_invalid", "A lifecycle ledger transaction id is missing.")
            try:
                cluster_ledger.rollback_projection(
                    transaction_id=transaction_id, path=path)
            except cluster_ledger.LedgerProjectionError as exc:
                raise ProjectLifecycleError(
                    "journal_recovery_failed",
                    "A concurrently changed jobs ledger could not be merged safely.",
                ) from exc
            return
        current = path.read_bytes() if path.is_file() else b""
        original = cls._snapshot_payload(snapshot)
        current_sha = _sha256_bytes(current)
        original_sha = _sha256_bytes(original)
        if current_sha == original_sha:
            return
        if not written_sha256 and not projection_b64:
            # This authority was only snapshotted; the transaction never
            # reached its projection phase, so a different value belongs to a
            # concurrent writer and must be left intact.
            return
        if written_sha256 and current_sha == written_sha256:
            cls._restore_snapshot(snapshot)
            return
        # A different writer won or extended an atomic update.  Undo just this
        # transaction's list delta, preserving unrelated entries and metadata.
        # This covers both CAS-before-write conflicts and writers that observed
        # our projection before adding their own record.
        try:
            projected_bytes = base64.b64decode(
                str(projection_b64 or ""), validate=True)
            current_payload = (json.loads(current.decode("utf-8"))
                               if current else {list_key: []})
            original_payload = (json.loads(original.decode("utf-8"))
                                if original else {list_key: []})
            projected_payload = json.loads(projected_bytes.decode("utf-8"))
            current_entries = current_payload[list_key]
            original_entries = original_payload[list_key]
            projected_entries = projected_payload[list_key]
            if not all(isinstance(value, list) for value in (
                    current_entries, original_entries, projected_entries)):
                raise ValueError("authority projection is not a list")
            if not all(isinstance(item, str) for values in (
                    current_entries, original_entries, projected_entries)
                       for item in values):
                raise ValueError("authority projection contains a non-string locator")
        except (ValueError, KeyError, TypeError, UnicodeError,
                binascii.Error, json.JSONDecodeError) as exc:
            raise ProjectLifecycleError(
                "journal_recovery_failed",
                "A concurrently changed authority could not be merged safely.",
            ) from exc
        original_by_key = {_path_key(item): item for item in original_entries}
        projected_by_key = {_path_key(item): item for item in projected_entries}
        added = set(projected_by_key) - set(original_by_key)
        removed = set(original_by_key) - set(projected_by_key)
        merged = [item for item in current_entries if _path_key(item) not in added]
        merged_keys = {_path_key(item) for item in merged}
        for key in removed:
            if key not in merged_keys:
                merged.append(original_by_key[key])
                merged_keys.add(key)
        current_payload[list_key] = merged
        _atomic_write(path, json.dumps(
            current_payload, ensure_ascii=False, indent=2).encode("utf-8"))

    def _prepare_authority_projection(self, record: dict[str, Any], *,
                                      authority: str, payload: dict[str, Any],
                                      list_key: str, entries: list[str]) -> None:
        try:
            projected = (cluster_ledger.render_projection(payload, entries)
                         if list_key == "job_dirs"
                         else self._authority_projection(payload, list_key, entries))
        except cluster_ledger.LedgerProjectionError as exc:
            raise ProjectLifecycleError(
                "ledger_invalid", "The jobs ledger projection could not be prepared safely."
            ) from exc
        record[f"{authority}_written_sha256"] = _sha256_bytes(projected)
        record[f"{authority}_projection_b64"] = base64.b64encode(
            projected).decode("ascii")
        self._journal_phase(record, f"{authority}_committing")

    def _commit_registry(self, plan: LifecyclePlan, payload: dict[str, Any],
                         entries: list[str]) -> None:
        """Recheck registry CAS immediately before the atomic replacement."""
        _current_payload, _current_entries, current_sha = self._read_registry()
        if current_sha != plan.registry_sha256:
            raise ProjectLifecycleError(
                "preflight_stale",
                "The project registry changed during the operation; filesystem changes were rolled back.",
            )
        self._write_registry(payload, entries)

    def _commit_ledger(self, record: dict[str, Any], plan: LifecyclePlan,
                       payload: dict[str, Any],
                       entries: list[str]) -> dict[str, object]:
        try:
            result = self._write_ledger(
                transaction_id=str(record["transaction_id"]),
                base_sha256=plan.ledger_sha256,
                base_entries=list(payload.get("job_dirs") or []), entries=entries)
            record["ledger_undo_receipt"] = copy.deepcopy(result.get("undo_receipt"))
            return result
        except cluster_ledger.LedgerProjectionError as exc:
            raise ProjectLifecycleError(
                "ledger_merge_failed",
                "The jobs ledger could not be merged safely; all projections were rolled back.",
            ) from exc

    def _ledger_journal_snapshot(self) -> dict[str, Any]:
        try:
            snapshot = cluster_ledger.projection_snapshot(path=self.ledger_path)
        except cluster_ledger.LedgerProjectionError as exc:
            raise ProjectLifecycleError(
                "ledger_invalid", "The jobs ledger could not be snapshotted safely."
            ) from exc
        return {
            "path": str(snapshot["path"]), "exists": bool(snapshot["exists"]),
            "data_b64": base64.b64encode(snapshot["raw"]).decode("ascii"),
        }

    @staticmethod
    def _snapshot(path: Path) -> dict[str, Any]:
        exists = path.is_file()
        return {
            "path": str(path),
            "exists": exists,
            "data_b64": base64.b64encode(path.read_bytes()).decode("ascii") if exists else "",
        }

    @staticmethod
    def _restore_snapshot(snapshot: dict[str, Any]) -> None:
        path = Path(str(snapshot.get("path") or ""))
        if not path.name:
            raise ProjectLifecycleError("journal_invalid", "A recovery snapshot path is invalid.")
        if snapshot.get("exists"):
            try:
                payload = base64.b64decode(str(snapshot.get("data_b64") or ""), validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ProjectLifecycleError(
                    "journal_invalid", "A recovery snapshot is invalid."
                ) from exc
            _atomic_write(path, payload)
        elif os.path.lexists(path):
            if path.is_dir():
                raise ProjectLifecycleError(
                    "journal_recovery_failed", "Recovery refused to replace a directory with a file."
                )
            path.unlink()

    @classmethod
    def _restore_file_projection(cls, snapshot: dict[str, Any], *,
                                 current_path: Path | None = None) -> None:
        path = current_path or Path(str(snapshot.get("path") or ""))
        if not path.name:
            raise ProjectLifecycleError(
                "journal_invalid", "A recovery file path is invalid."
            )
        current = path.read_bytes() if path.is_file() else b""
        original = cls._snapshot_payload(snapshot)
        if current == original:
            return
        written_sha = str(snapshot.get("written_sha256") or "")
        if not written_sha:
            if snapshot.get("exists") and not path.is_file():
                raise ProjectLifecycleError(
                    "journal_recovery_failed",
                    "A lifecycle-managed file disappeared during recovery.",
                )
            # No lifecycle write was attempted; preserve a concurrent edit.
            return
        if written_sha and _sha256_bytes(current) == written_sha:
            if snapshot.get("exists"):
                _atomic_write(path, original)
            elif os.path.lexists(path):
                path.unlink()
            return
        raise ProjectLifecycleError(
            "journal_recovery_failed",
            "A lifecycle-managed file changed concurrently and was not overwritten.",
        )

    def _prepare_file_projections(
            self, record: dict[str, Any],
            projections: Iterable[tuple[Path, bytes]]) -> None:
        by_key = {
            _path_key(str(item.get("path") or "")): item
            for item in record.get("backups") or []
        }
        for path, payload in projections:
            snapshot = by_key.get(_path_key(path))
            if snapshot is None:
                raise ProjectLifecycleError(
                    "journal_invalid", "A lifecycle file projection lacks a backup."
                )
            snapshot["written_sha256"] = _sha256_bytes(payload)
        self._journal_phase(record, "filesystem_committing")

    def _write_journal(self, record: dict[str, Any]) -> None:
        encoded = json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")
        _atomic_write(self.journal_path, encoded)

    def _journal_begin(self, plan: LifecyclePlan, stage: Path,
                       backups: list[tuple[Path, bytes]]) -> dict[str, Any]:
        if self.journal_path.exists():
            raise ProjectLifecycleError(
                "journal_busy", "A previous project lifecycle transaction needs recovery."
            )
        move_source_evidence = None
        if plan.action == "move":
            source_root = Path(plan.source_root)
            jobs = []
            for change in plan.job_changes:
                relative = _relative_within(change.source_path, source_root)
                if relative is None or not change.source_manifest_sha256:
                    continue
                jobs.append({
                    "relative_path": relative,
                    "manifest_sha256": change.source_manifest_sha256,
                })
            move_source_evidence = {
                "schema": 1,
                "project_sha256": plan.project_sha256,
                "project_uuid": plan.source_project_uuid,
                "jobs": sorted(jobs, key=lambda item: item["relative_path"]),
            }
        record = {
            "schema": 1,
            "transaction_id": uuid.uuid4().hex,
            "status": "prepared",
            "action": plan.action,
            "source_root": plan.source_root,
            "destination_root": plan.destination_root,
            "stage_root": str(stage),
            "registry": self._snapshot(self.registry_path),
            "ledger": self._ledger_journal_snapshot(),
            "registry_written_sha256": None,
            "ledger_written_sha256": None,
            "registry_projection_b64": None,
            "ledger_projection_b64": None,
            "ledger_undo_receipt": None,
            "move_source_evidence": move_source_evidence,
            "backups": [
                {
                    "path": str(path), "exists": True,
                    "data_b64": base64.b64encode(payload).decode("ascii"),
                    "written_sha256": None,
                }
                for path, payload in backups
            ],
        }
        self._write_journal(record)
        return record

    def _journal_phase(self, record: dict[str, Any], status: str) -> None:
        record["status"] = status
        self._write_journal(record)

    @staticmethod
    def _owner_path(root: Path) -> Path:
        return root / _OWNER_FILE

    def _write_owner(self, root: Path, record: dict[str, Any]) -> None:
        payload = json.dumps({
            "schema": 1,
            "transaction_id": record["transaction_id"],
            "action": record["action"],
        }, ensure_ascii=False, indent=2).encode("utf-8")
        _atomic_write(self._owner_path(root), payload)

    @staticmethod
    def _owner_matches(root: Path, record: dict[str, Any]) -> bool:
        path = root / _OWNER_FILE
        if not path.is_file():
            return False
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return (isinstance(value, dict)
                and value.get("transaction_id") == record.get("transaction_id"))

    @classmethod
    def _move_source_is_authoritative(
            cls, source: Path, record: dict[str, Any], *,
            allow_unowned_prepared: bool = False) -> bool:
        """Verify that ``source`` is the complete frozen pre-move instance.

        A directory merely reappearing at the old path is not recovery evidence.
        New journals freeze every project-local job manifest so an untouched
        ``prepared`` transaction can be verified.  Once the filesystem move may
        have happened, only the transaction owner marker establishes that the
        lifecycle service itself moved the complete owned tree back.
        """
        owner_matches = cls._owner_matches(source, record)
        untouched_prepared = (
            allow_unowned_prepared and record.get("status") == "prepared"
        )
        if not owner_matches and not untouched_prepared:
            return False
        if not source.is_dir():
            return False
        lexical_source = os.path.normcase(os.path.abspath(os.fspath(source)))
        resolved_source = os.path.normcase(os.path.realpath(lexical_source))
        if source.is_symlink() or lexical_source != resolved_source:
            return False
        project_file = source / PROJECT_FILE
        if not project_file.is_file():
            return False
        backups = record.get("backups") or []
        if not backups:
            return False
        try:
            original = cls._snapshot_payload(backups[0])
            current = _read_bytes(project_file)
            original_project = _parse_project(original)
            current_project = _parse_project(current)
        except ProjectLifecycleError:
            return False
        if current != original or _identity_conflict(current_project):
            return False

        evidence = record.get("move_source_evidence")
        expected_uuid = _project_uuid(original_project)
        if isinstance(evidence, dict) and evidence.get("schema") == 1:
            frozen_sha = str(evidence.get("project_sha256") or "")
            if (not re.fullmatch(r"[0-9a-f]{64}", frozen_sha)
                    or _sha256_bytes(current) != frozen_sha):
                return False
            frozen_uuid = str(evidence.get("project_uuid") or "")
            if frozen_uuid and frozen_uuid != expected_uuid:
                return False
            jobs = evidence.get("jobs")
            if not isinstance(jobs, list):
                return False
            for item in jobs:
                if not isinstance(item, dict):
                    return False
                relative = str(item.get("relative_path") or "")
                manifest_sha = str(item.get("manifest_sha256") or "")
                if (not relative or os.path.isabs(relative)
                        or not re.fullmatch(r"[0-9a-f]{64}", manifest_sha)):
                    return False
                job_dir = source if relative == "." else source / relative
                job_file = job_dir / "job.yaml"
                if (_relative_within(str(job_file), source) is None
                        or not job_file.is_file()):
                    return False
                try:
                    if _sha256_bytes(_read_bytes(job_file)) != manifest_sha:
                        return False
                except ProjectLifecycleError:
                    return False
            return _project_uuid(current_project) == expected_uuid

        return (owner_matches or untouched_prepared) \
            and _project_uuid(current_project) == expected_uuid

    def _cleanup_committed_journal(self, record: dict[str, Any]) -> None:
        ledger_snapshot = record.get("ledger") or {}
        ledger_path = Path(str(ledger_snapshot.get("path") or ""))
        transaction_id = str(record.get("transaction_id") or "")
        if ledger_path.name and transaction_id:
            try:
                cluster_ledger.release_projection_receipt(
                    transaction_id=transaction_id, path=ledger_path)
            except cluster_ledger.LedgerProjectionError as exc:
                raise ProjectLifecyclePartialRollbackError(
                    "ledger_receipt_release_failed",
                    "Committed lifecycle ledger authority still requires recovery.",
                ) from exc
        for raw in (record.get("stage_root"), record.get("destination_root")):
            root = Path(str(raw or ""))
            if root.name and self._owner_matches(root, record):
                self._owner_path(root).unlink()
        self.journal_path.unlink(missing_ok=True)

    def _recover_record(self, record: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(record, dict) or record.get("schema") != 1:
            raise ProjectLifecyclePartialRollbackError(
                "journal_invalid", "The lifecycle recovery journal is invalid."
            )
        if record.get("status") == "committed":
            self._cleanup_committed_journal(record)
            return {"ok": True, "status": "finalized"}

        errors = []
        action = str(record.get("action") or "")
        source = Path(str(record.get("source_root") or ""))
        destination = Path(str(record.get("destination_root") or ""))
        stage = Path(str(record.get("stage_root") or ""))
        try:
            if action == "clone":
                if stage.name and os.path.lexists(stage):
                    self._remove_tree(stage)
                if destination.name and self._owner_matches(destination, record):
                    self._remove_tree(destination)
            elif action == "move":
                owned = None
                if destination.name and self._owner_matches(destination, record):
                    owned = destination
                elif stage.name and os.path.lexists(stage):
                    owned = stage
                if owned is not None:
                    backups = record.get("backups") or []
                    if backups:
                        self._restore_file_projection(
                            backups[0], current_path=owned / PROJECT_FILE)
                    if source.exists():
                        if self._move_source_is_authoritative(source, record):
                            self._remove_tree(owned)
                        else:
                            raise OSError(
                                "the original source path was replaced by an "
                                "unverified filesystem instance"
                            )
                    else:
                        self._move_path(str(owned), str(source))
                if not self._move_source_is_authoritative(
                        source, record, allow_unowned_prepared=True):
                    raise OSError(
                        "the original project instance was not restored"
                    )
            elif action == "adopt":
                for snapshot in record.get("backups") or []:
                    self._restore_file_projection(snapshot)
            else:
                raise OSError("unsupported journal action")
        except Exception as exc:  # noqa: BLE001 - preserve journal for restart retry
            errors.append(str(exc))

        for key in ("ledger", "registry"):
            try:
                self._restore_authority_snapshot(
                    record.get(key) or {}, record.get(f"{key}_written_sha256"),
                    record.get(f"{key}_projection_b64"),
                    "job_dirs" if key == "ledger" else "projects",
                    str(record.get("transaction_id") or "") if key == "ledger" else None)
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
        if errors:
            raise ProjectLifecyclePartialRollbackError(
                "partial_rollback",
                "Lifecycle recovery is incomplete; the journal was retained for retry.",
            )
        if action == "move" and self._owner_matches(source, record):
            try:
                self._owner_path(source).unlink()
            except OSError as exc:
                raise ProjectLifecyclePartialRollbackError(
                    "partial_rollback",
                    "Lifecycle recovery is incomplete; the journal was retained for retry.",
                ) from exc
        self.journal_path.unlink(missing_ok=True)
        return {"ok": True, "status": "rolled_back"}

    def _recover_pending_locked(self) -> dict[str, Any]:
        if not self.journal_path.is_file():
            return {"ok": True, "status": "clean"}
        try:
            record = json.loads(self.journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProjectLifecyclePartialRollbackError(
                "journal_invalid", "The lifecycle recovery journal cannot be read."
            ) from exc
        return self._recover_record(record)

    def recover_pending(self) -> dict[str, Any]:
        """Finish or roll back a journal left by an interrupted process."""
        with self._transaction_lock():
            return self._recover_pending_locked()

    @staticmethod
    def _registry_records(entries: Iterable[str]) -> tuple[list[dict[str, Any]], int]:
        records = []
        unreadable = 0
        for entry in entries:
            path = _project_file(entry, must_exist=False)
            project = None
            if path.is_file():
                try:
                    project = _load_project(path)
                except ProjectLifecycleError:
                    unreadable += 1
            else:
                unreadable += 1
            records.append({
                "entry": entry,
                "path": path,
                "path_key": _path_key(path),
                "project": project,
                "project_uuid": _project_uuid(project) if project else None,
            })
        return records, unreadable

    @staticmethod
    def _new_uuid(records: Iterable[dict[str, Any]]) -> str:
        known = {record.get("project_uuid") for record in records}
        while True:
            candidate = uuid.uuid4().hex
            if candidate not in known:
                return candidate

    @staticmethod
    def _job_changes(action: str, project: dict[str, Any], source_root: Path,
                     target_root: Path, identity_mode: str,
                     ledger_entries: list[str]
                     ) -> tuple[list[JobLifecycleChange], list[dict[str, str]]]:
        issues: list[dict[str, str]] = []
        source_view, _counts = _rewrite_project(project, source_root, source_root)
        canonical_dirs = _local_job_directories(source_view, source_root)
        candidates = {_path_key(path): path for path in canonical_dirs}
        ledger_by_key: dict[str, list[str]] = {}
        ledger_identity_paths: dict[str, set[str]] = {}
        for entry in ledger_entries:
            entry_key = _path_key(entry)
            ledger_by_key.setdefault(entry_key, []).append(entry)
            if _relative_within(entry, source_root) is not None:
                candidates.setdefault(entry_key, Path(os.path.realpath(entry)))
            manifest_path = Path(entry) / "job.yaml"
            if manifest_path.is_file():
                try:
                    ledger_manifest = yaml.safe_load(
                        _read_bytes(manifest_path).decode("utf-8"))
                except (UnicodeError, yaml.YAMLError, ProjectLifecycleError):
                    ledger_manifest = None
                ledger_identity = _manifest_identity(ledger_manifest)
                if ledger_identity:
                    ledger_identity_paths.setdefault(
                        ledger_identity, set()).add(entry_key)
        for values in ledger_by_key.values():
            if len(values) > 1:
                issues.append(_issue(
                    "duplicate_ledger_path",
                    "The jobs ledger contains the same canonical job path more than once.",
                ))
                break

        changes: list[JobLifecycleChange] = []
        for source_key, source_dir in sorted(candidates.items()):
            relative = _relative_within(str(source_dir), source_root)
            if relative is None:
                continue
            target_dir = target_root if relative == "." else target_root / relative
            target_key = _path_key(target_dir)
            manifest = None
            manifest_sha = None
            job_file = source_dir / "job.yaml"
            if job_file.is_file():
                try:
                    manifest_bytes = _read_bytes(job_file)
                    manifest = yaml.safe_load(manifest_bytes.decode("utf-8"))
                    if not isinstance(manifest, dict):
                        raise ValueError("job manifest is not a mapping")
                    manifest_sha = _sha256_bytes(manifest_bytes)
                except (UnicodeError, yaml.YAMLError, ValueError):
                    issues.append(_issue(
                        "job_manifest_invalid",
                        "A project-local job.yaml is not readable YAML.",
                    ))
                    continue
            elif source_key in ledger_by_key:
                issues.append(_issue(
                    "ledger_job_unreadable",
                    "A project-local jobs ledger entry does not contain a readable job.yaml.",
                ))
            source_identity = _manifest_identity(manifest)
            remint = action == "clone" or (
                action == "adopt" and identity_mode == "remint")
            target_identity = uuid.uuid4().hex if remint and manifest else source_identity
            if action == "move" and manifest is not None and not source_identity:
                issues.append(_issue(
                    "job_identity_missing",
                    "A moved job must have a stable job_id or job_uuid before its path can change.",
                ))
            if (source_identity and (action == "move" or (
                    action == "adopt" and identity_mode == "preserve"))
                    and any(key != source_key for key in
                            ledger_identity_paths.get(source_identity, set()))):
                issues.append(_issue(
                    "duplicate_job_identity",
                    "A preserved job identity already belongs to another jobs ledger entry.",
                ))
            if action == "move":
                ledger_action = "rewrite" if source_key in ledger_by_key else "register"
            elif target_key in ledger_by_key:
                ledger_action = "retain"
            else:
                ledger_action = "register"
            if action in {"clone", "move"} and target_key != source_key \
                    and target_key in ledger_by_key:
                issues.append(_issue(
                    "target_job_already_registered",
                    "A destination job path is already present in the jobs ledger.",
                ))
            changes.append(JobLifecycleChange(
                source_path=str(source_dir),
                target_path=str(target_dir),
                ledger_action=ledger_action,
                source_identity=source_identity,
                target_identity=target_identity,
                source_manifest_sha256=manifest_sha,
                label=target_dir.name or "job",
                remint_identity=bool(remint and manifest),
            ))
        return changes, issues

    @staticmethod
    def _project_ledger(plan: LifecyclePlan, entries: list[str]) -> list[str]:
        projected = list(entries)
        for change in plan.job_changes:
            source_key = _path_key(change.source_path)
            target_key = _path_key(change.target_path)
            if plan.action == "move":
                replaced = False
                next_entries = []
                for entry in projected:
                    if _path_key(entry) == source_key:
                        if not replaced:
                            next_entries.append(change.target_path)
                            replaced = True
                    else:
                        next_entries.append(entry)
                projected = next_entries
                if not replaced and not any(
                        _path_key(entry) == target_key for entry in projected):
                    projected.append(change.target_path)
            elif not any(_path_key(entry) == target_key for entry in projected):
                projected.append(change.target_path)
        return projected

    @staticmethod
    def _plan_token(values: dict[str, Any]) -> str:
        encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _build_plan(self, action: str, source: str | os.PathLike[str],
                    destination: str | os.PathLike[str] | None,
                    identity_mode: str) -> LifecyclePlan:
        if action not in {"clone", "move", "adopt"}:
            raise ValueError("unsupported lifecycle action")
        if identity_mode not in {"preserve", "remint"}:
            raise ProjectLifecycleError(
                "identity_mode_invalid", "Identity mode must be preserve or remint."
            )
        source_file = _project_file(source)
        source_root = source_file.parent
        source_data = _load_project(source_file)
        source_bytes = _read_bytes(source_file)
        payload, entries, registry_sha = self._read_registry()
        ledger_payload, ledger_entries, ledger_sha = self._read_ledger()
        del payload
        del ledger_payload
        records, unreadable = self._registry_records(entries)
        source_key = _path_key(source_file)
        source_matches = [record for record in records if record["path_key"] == source_key]
        source_uuid = _project_uuid(source_data)
        conflicts: list[dict[str, str]] = []
        warnings: list[dict[str, str]] = []

        if _identity_conflict(source_data):
            conflicts.append(_issue(
                "identity_mismatch",
                "project.yaml contains conflicting project identity fields.",
            ))
        if action in {"clone", "move"}:
            if not source_matches:
                conflicts.append(_issue(
                    "source_not_registered",
                    "The source project is not present in the authoritative registry.",
                ))
            elif len(source_matches) != 1:
                conflicts.append(_issue(
                    "duplicate_registry_path",
                    "The source project appears more than once in the registry.",
                ))
        elif source_matches:
            conflicts.append(_issue(
                "path_already_registered",
                "The selected project folder is already registered.",
            ))

        if source_uuid and action in {"clone", "move"}:
            duplicates = [
                record for record in records
                if record["path_key"] != source_key
                and record.get("project_uuid") == source_uuid
            ]
            if duplicates:
                conflicts.append(_issue(
                    "duplicate_project_uuid",
                    "The source project identity is already ambiguous in the registry.",
                ))
            if action == "move" and unreadable:
                conflicts.append(_issue(
                    "registry_identity_incomplete",
                    "Project identity uniqueness cannot be proven while registered projects are unreadable.",
                ))

        destination_root: Path | None = None
        if destination is not None:
            destination_root = Path(os.path.realpath(
                os.path.abspath(os.path.expanduser(os.fspath(destination)))))
            if os.path.lexists(destination_root):
                conflicts.append(_issue(
                    "destination_exists",
                    "The destination already exists; lifecycle operations never overwrite it.",
                ))
            parent = destination_root.parent
            if not parent.is_dir():
                conflicts.append(_issue(
                    "destination_parent_missing",
                    "The selected destination parent does not exist.",
                ))
            elif action == "move" and not _same_filesystem(source_root, parent):
                conflicts.append(_issue(
                    "cross_volume_move_unsupported",
                    "Atomic move requires the source and destination to be on the same filesystem.",
                ))
            if _relative_within(str(destination_root), source_root) is not None:
                conflicts.append(_issue(
                    "destination_inside_source",
                    "The destination cannot be inside the source project.",
                ))
            destination_file_key = _path_key(destination_root / PROJECT_FILE)
            if any(record["path_key"] == destination_file_key for record in records):
                conflicts.append(_issue(
                    "destination_registered",
                    "The destination is already present in the project registry.",
                ))

        if action == "clone":
            target_uuid = self._new_uuid(records)
            target_root = destination_root
        elif action == "move":
            target_uuid = source_uuid
            target_root = destination_root
            if source_uuid is None:
                warnings.append(_issue(
                    "legacy_identity",
                    "This legacy project has no UUID; move preserves that state exactly.",
                ))
        else:
            target_root = source_root
            if identity_mode == "preserve":
                target_uuid = source_uuid
                if source_uuid is None:
                    conflicts.append(_issue(
                        "identity_missing",
                        "Preserve mode requires a valid existing project UUID.",
                    ))
                if unreadable:
                    conflicts.append(_issue(
                        "registry_identity_incomplete",
                        "Identity uniqueness cannot be proven while registered projects are unreadable.",
                    ))
                if source_uuid and any(
                    record.get("project_uuid") == source_uuid
                    and record["path_key"] != source_key for record in records
                ):
                    conflicts.append(_issue(
                        "duplicate_project_uuid",
                        "That project UUID already belongs to another registered folder.",
                    ))
            else:
                target_uuid = self._new_uuid(records)
                if source_uuid:
                    warnings.append(_issue(
                        "copy_identity_reminted",
                        "Safe-copy mode will mint a new project UUID before registration.",
                    ))

        if target_root is None:
            raise ProjectLifecycleError(
                "destination_missing", "A destination is required for this operation."
            )
        try:
            _, counts = _rewrite_project(source_data, source_root, target_root)
        except ProjectLifecycleError as exc:
            conflicts.append(_issue(exc.code, str(exc)))
            counts = {"rewritten": 0, "external": 0}
        try:
            job_changes, job_issues = self._job_changes(
                action, source_data, source_root, target_root,
                identity_mode, ledger_entries)
            conflicts.extend(job_issues)
        except ProjectLifecycleError as exc:
            job_changes = []
            conflicts.append(_issue(exc.code, str(exc)))
        source_name = str(source_data.get("name") or source_root.name or "project")
        target_name = target_root.name or source_name
        token_values = {
            "action": action,
            "source": _path_key(source_file),
            "source_root": _path_key(source_root),
            "destination": _path_key(target_root),
            "registry": _path_key(self.registry_path),
            "registry_sha256": registry_sha,
            "ledger": _path_key(self.ledger_path),
            "ledger_sha256": ledger_sha,
            "project_sha256": _sha256_bytes(source_bytes),
            "source_uuid": source_uuid,
            "target_uuid": target_uuid,
            "identity_mode": identity_mode,
            "job_changes": [
                {
                    "source": _path_key(item.source_path),
                    "target": _path_key(item.target_path),
                    "ledger_action": item.ledger_action,
                    "source_identity": item.source_identity,
                    "target_identity": item.target_identity,
                    "source_manifest_sha256": item.source_manifest_sha256,
                    "remint_identity": item.remint_identity,
                }
                for item in job_changes
            ],
            "conflicts": conflicts,
        }
        token = self._plan_token(token_values)
        return LifecyclePlan(
            action=action,
            source_project_file=str(source_file),
            source_root=str(source_root),
            destination_root=str(target_root),
            registry_path=str(Path(os.path.realpath(os.path.abspath(
                os.path.expanduser(os.fspath(self.registry_path)))))),
            registry_sha256=registry_sha,
            ledger_path=str(Path(os.path.realpath(os.path.abspath(
                os.path.expanduser(os.fspath(self.ledger_path)))))),
            ledger_sha256=ledger_sha,
            project_sha256=_sha256_bytes(source_bytes),
            source_project_uuid=source_uuid,
            target_project_uuid=target_uuid,
            identity_mode=identity_mode,
            source_name=source_name,
            target_name=target_name,
            locator_rewrites=counts["rewritten"],
            external_locators=counts["external"],
            job_changes=tuple(job_changes),
            conflicts=tuple(conflicts),
            warnings=tuple(warnings),
            token=token,
        )

    def preflight_clone(self, source: str | os.PathLike[str],
                        destination: str | os.PathLike[str]) -> LifecyclePlan:
        with self._transaction_lock():
            self._recover_pending_locked()
            return self._build_plan("clone", source, destination, "remint")

    def preflight_move(self, source: str | os.PathLike[str],
                       destination: str | os.PathLike[str]) -> LifecyclePlan:
        with self._transaction_lock():
            self._recover_pending_locked()
            return self._build_plan("move", source, destination, "preserve")

    def preflight_adopt(self, source: str | os.PathLike[str],
                        identity_mode: str = "remint") -> LifecyclePlan:
        with self._transaction_lock():
            self._recover_pending_locked()
            return self._build_plan("adopt", source, None, identity_mode)

    def _verify_plan(self, plan: LifecyclePlan) -> tuple[
            dict[str, Any], list[str], dict[str, Any], list[str]]:
        if not isinstance(plan, LifecyclePlan):
            raise ProjectLifecycleError("preflight_invalid", "A valid preflight plan is required.")
        if not plan.ready:
            raise ProjectLifecycleError(
                "preflight_blocked", "The preflight contains unresolved conflicts."
            )
        if _path_key(plan.registry_path) != _path_key(self.registry_path):
            raise ProjectLifecycleError(
                "preflight_invalid", "The preflight belongs to a different project registry."
            )
        if _path_key(plan.ledger_path) != _path_key(self.ledger_path):
            raise ProjectLifecycleError(
                "preflight_invalid", "The preflight belongs to a different jobs ledger."
            )
        expected_token = self._plan_token({
            "action": plan.action,
            "source": _path_key(plan.source_project_file),
            "source_root": _path_key(plan.source_root),
            "destination": _path_key(plan.destination_root or plan.source_root),
            "registry": _path_key(plan.registry_path),
            "registry_sha256": plan.registry_sha256,
            "ledger": _path_key(plan.ledger_path),
            "ledger_sha256": plan.ledger_sha256,
            "project_sha256": plan.project_sha256,
            "source_uuid": plan.source_project_uuid,
            "target_uuid": plan.target_project_uuid,
            "identity_mode": plan.identity_mode,
            "job_changes": [
                {
                    "source": _path_key(item.source_path),
                    "target": _path_key(item.target_path),
                    "ledger_action": item.ledger_action,
                    "source_identity": item.source_identity,
                    "target_identity": item.target_identity,
                    "source_manifest_sha256": item.source_manifest_sha256,
                    "remint_identity": item.remint_identity,
                }
                for item in plan.job_changes
            ],
            "conflicts": list(plan.conflicts),
        })
        if plan.token != expected_token:
            raise ProjectLifecycleError(
                "preflight_invalid", "The preflight plan signature is invalid."
            )
        source_file = Path(plan.source_project_file)
        if _sha256_bytes(_read_bytes(source_file)) != plan.project_sha256:
            raise ProjectLifecycleError(
                "preflight_stale", "project.yaml changed after preflight; run preflight again."
            )
        payload, entries, registry_sha = self._read_registry()
        if registry_sha != plan.registry_sha256:
            raise ProjectLifecycleError(
                "preflight_stale", "The project registry changed after preflight; run it again."
            )
        ledger_payload, ledger_entries, ledger_sha = self._read_ledger()
        if ledger_sha != plan.ledger_sha256:
            raise ProjectLifecycleError(
                "preflight_stale", "The jobs ledger changed after preflight; run it again."
            )
        self._assert_jobs_cas(plan)
        return payload, entries, ledger_payload, ledger_entries

    @staticmethod
    def _assert_project_cas(path: Path, plan: LifecyclePlan) -> bytes:
        payload = _read_bytes(path)
        if _sha256_bytes(payload) != plan.project_sha256:
            raise ProjectLifecycleError(
                "preflight_stale",
                "project.yaml changed during the operation; filesystem changes were rolled back.",
            )
        return payload

    @staticmethod
    def _assert_jobs_cas(plan: LifecyclePlan,
                         physical_root: Path | None = None) -> None:
        logical_root = Path(plan.source_root)
        physical_root = physical_root or logical_root
        for change in plan.job_changes:
            relative = _relative_within(change.source_path, logical_root)
            if relative is None:
                raise ProjectLifecycleError(
                    "preflight_invalid", "A preflight job locator is outside the project."
                )
            directory = (physical_root if relative == "."
                         else physical_root / relative)
            job_file = directory / "job.yaml"
            current_sha = (_sha256_bytes(_read_bytes(job_file))
                           if job_file.is_file() else None)
            if current_sha != change.source_manifest_sha256:
                raise ProjectLifecycleError(
                    "preflight_stale",
                    "A project-local job.yaml changed after preflight; run it again.",
                )

    @staticmethod
    def _rewritten_project(plan: LifecyclePlan, target_root: Path,
                           project: dict[str, Any] | None = None
                           ) -> tuple[dict[str, Any], str | None]:
        source_root = Path(plan.source_root)
        project = project or _load_project(Path(plan.source_project_file))
        rewritten, _counts = _rewrite_project(project, source_root, target_root)
        namespace = None
        if plan.action == "clone" or (
            plan.action == "adopt" and plan.identity_mode == "remint"
        ):
            if not plan.target_project_uuid:
                raise ProjectLifecycleError(
                    "identity_missing", "A reminted project UUID is required."
                )
            namespace = _apply_identity(rewritten, plan.target_project_uuid)
        return rewritten, namespace

    @staticmethod
    def _restore_files(backups: list[tuple[Path, bytes]], written: list[Path]) -> list[str]:
        errors = []
        original = {_path_key(path): (path, payload) for path, payload in backups}
        for path in reversed(written):
            item = original.get(_path_key(path))
            if not item:
                continue
            try:
                _atomic_write(item[0], item[1])
            except Exception as exc:  # noqa: BLE001 - collect every rollback failure
                errors.append(f"{item[0].name}: {exc}")
        return errors

    def _apply_clone(self, plan: LifecyclePlan, payload: dict[str, Any],
                     entries: list[str], ledger_payload: dict[str, Any],
                     ledger_entries: list[str]) -> dict[str, Any]:
        source_root = Path(plan.source_root)
        destination = Path(plan.destination_root or "")
        if os.path.lexists(destination):
            raise ProjectLifecycleError(
                "destination_exists", "The destination appeared after preflight."
            )
        stage = destination.parent / f".{destination.name}.vcs-clone-{uuid.uuid4().hex}.tmp"
        record = self._journal_begin(plan, stage, [])
        identity_map = {
            _path_key(item.target_path): str(item.target_identity)
            for item in plan.job_changes
            if item.remint_identity and item.target_identity
        }
        projected_ledger = self._project_ledger(plan, ledger_entries)
        try:
            self._copytree(source_root, stage, symlinks=True)
            self._write_owner(stage, record)
            self._assert_project_cas(Path(plan.source_project_file), plan)
            self._assert_jobs_cas(plan)
            self._assert_jobs_cas(plan, stage)
            staged_payload = _read_bytes(stage / PROJECT_FILE)
            if _sha256_bytes(staged_payload) != plan.project_sha256:
                raise ProjectLifecycleError(
                    "preflight_stale",
                    "The copied project.yaml did not match the preflight snapshot.",
                )
            rewritten, namespace = self._rewritten_project(
                plan, destination, _parse_project(staged_payload))
            _atomic_write(stage / PROJECT_FILE, _yaml_bytes(rewritten))
            staged_project = _load_project(stage / PROJECT_FILE)
            for job_file, _original, manifest in _job_manifest_updates(
                    staged_project, destination, namespace, physical_root=stage,
                    job_identity_by_target=identity_map):
                _atomic_write(job_file, _yaml_bytes(manifest))
            if os.path.lexists(destination):
                raise ProjectLifecycleError(
                    "destination_exists", "The destination appeared during the clone."
                )
            try:
                _rename_noreplace(stage, destination)
            except FileExistsError as exc:
                raise ProjectLifecycleError(
                    "destination_exists", "The destination appeared during the clone."
                ) from exc
            self._journal_phase(record, "filesystem_published")
            target_file = destination / PROJECT_FILE
            projected_registry = entries + [str(target_file)]
            self._prepare_authority_projection(
                record, authority="registry", payload=payload,
                list_key="projects", entries=projected_registry)
            self._commit_registry(plan, payload, projected_registry)
            self._journal_phase(record, "registry_written")
            self._prepare_authority_projection(
                record, authority="ledger", payload=ledger_payload,
                list_key="job_dirs", entries=projected_ledger)
            self._commit_ledger(record, plan, ledger_payload, projected_ledger)
            self._journal_phase(record, "ledger_written")
            self._journal_phase(record, "committed")
            try:
                self._cleanup_committed_journal(record)
            except Exception:
                pass
            return {
                "ok": True,
                "action": "clone",
                "project_path": str(target_file),
                "project": rewritten,
                "project_uuid": plan.target_project_uuid,
                "jobs_updated": len(plan.job_changes),
            }
        except Exception as exc:
            try:
                self._recover_record(record)
            except ProjectLifecyclePartialRollbackError as recovery_exc:
                raise recovery_exc from exc
            if isinstance(exc, ProjectLifecycleError):
                raise
            raise ProjectLifecycleError("clone_failed", "The project clone failed safely.") from exc

    def _rollback_move(self, source_root: Path, owned_path: Path,
                       original_project: bytes) -> list[str]:
        errors = []
        if os.path.lexists(owned_path):
            try:
                _atomic_write(owned_path / PROJECT_FILE, original_project)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"project.yaml: {exc}")
            if not source_root.exists():
                try:
                    self._move_path(str(owned_path), str(source_root))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"filesystem: {exc}")
            else:
                try:
                    self._remove_tree(owned_path)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"temporary destination: {exc}")
        if not source_root.exists():
            errors.append("source project was not restored")
        return errors

    def _apply_move(self, plan: LifecyclePlan, payload: dict[str, Any],
                    entries: list[str], ledger_payload: dict[str, Any],
                    ledger_entries: list[str]) -> dict[str, Any]:
        source_root = Path(plan.source_root)
        destination = Path(plan.destination_root or "")
        original_project = self._assert_project_cas(
            Path(plan.source_project_file), plan)
        if os.path.lexists(destination):
            raise ProjectLifecycleError(
                "destination_exists", "The destination appeared after preflight."
            )
        stage = destination.parent / f".{destination.name}.vcs-move-{uuid.uuid4().hex}.tmp"
        record = self._journal_begin(
            plan, stage, [(Path(plan.source_project_file), original_project)])
        projected_ledger = self._project_ledger(plan, ledger_entries)
        # Resolve registry entries while the source directory still exists.
        # A legacy registry may store the project root rather than project.yaml;
        # after the move ``Path.is_dir`` can no longer normalize that locator.
        source_registry_matches = [
            _path_key(_project_file(item, must_exist=False))
            == _path_key(plan.source_project_file)
            for item in entries
        ]
        try:
            # Stage beside the final destination.  This avoids shutil.move's
            # "move inside an existing directory" behavior if a destination
            # conflict appears while a cross-volume move is in progress.
            self._move_path(str(source_root), str(stage))
            self._write_owner(stage, record)
            if source_root.exists() or not stage.is_dir():
                raise ProjectLifecycleError(
                    "move_incomplete", "The filesystem move did not reach one staging folder."
                )
            if _sha256_bytes(_read_bytes(stage / PROJECT_FILE)) != plan.project_sha256:
                raise ProjectLifecycleError(
                    "preflight_stale",
                    "project.yaml changed during the move; the original location will be restored.",
                )
            self._assert_jobs_cas(plan, stage)
            moved_project = _load_project(stage / PROJECT_FILE)
            rewritten, _counts = _rewrite_project(
                moved_project, source_root, destination
            )
            if _project_uuid(rewritten) != plan.source_project_uuid:
                raise ProjectLifecycleError(
                    "identity_changed", "Move must preserve the canonical project UUID."
                )
            rewritten_bytes = _yaml_bytes(rewritten)
            self._prepare_file_projections(
                record, [(Path(plan.source_project_file), rewritten_bytes)])
            _atomic_write(stage / PROJECT_FILE, rewritten_bytes)
            if os.path.lexists(destination):
                raise ProjectLifecycleError(
                    "destination_exists", "The destination appeared during the move."
                )
            try:
                _rename_noreplace(stage, destination)
            except FileExistsError as exc:
                raise ProjectLifecycleError(
                    "destination_exists", "The destination appeared during the move."
                ) from exc
            self._journal_phase(record, "filesystem_published")
            target_file = destination / PROJECT_FILE
            new_entries = [
                str(target_file) if source_registry_matches[index] else item
                for index, item in enumerate(entries)
            ]
            self._prepare_authority_projection(
                record, authority="registry", payload=payload,
                list_key="projects", entries=new_entries)
            self._commit_registry(plan, payload, new_entries)
            self._journal_phase(record, "registry_written")
            self._prepare_authority_projection(
                record, authority="ledger", payload=ledger_payload,
                list_key="job_dirs", entries=projected_ledger)
            self._commit_ledger(record, plan, ledger_payload, projected_ledger)
            self._journal_phase(record, "ledger_written")
            self._journal_phase(record, "committed")
            try:
                self._cleanup_committed_journal(record)
            except Exception:
                pass
            return {
                "ok": True,
                "action": "move",
                "project_path": str(target_file),
                "project": rewritten,
                "project_uuid": plan.source_project_uuid,
                "jobs_updated": len(plan.job_changes),
            }
        except Exception as exc:
            try:
                self._recover_record(record)
            except ProjectLifecyclePartialRollbackError as recovery_exc:
                raise recovery_exc from exc
            if isinstance(exc, ProjectLifecycleError):
                raise
            raise ProjectLifecycleError("move_failed", "The project move failed safely.") from exc

    def _apply_adopt(self, plan: LifecyclePlan, payload: dict[str, Any],
                     entries: list[str], ledger_payload: dict[str, Any],
                     ledger_entries: list[str]) -> dict[str, Any]:
        root = Path(plan.source_root)
        project_file = Path(plan.source_project_file)
        project_payload = self._assert_project_cas(project_file, plan)
        self._assert_jobs_cas(plan)
        rewritten, namespace = self._rewritten_project(
            plan, root, _parse_project(project_payload))
        identity_map = {
            _path_key(item.target_path): str(item.target_identity)
            for item in plan.job_changes
            if item.remint_identity and item.target_identity
        }
        updates = _job_manifest_updates(
            rewritten, root, namespace,
            job_identity_by_target=identity_map)
        backups = [(project_file, _read_bytes(project_file))]
        backups.extend((path, original) for path, original, _manifest in updates)
        stage = root / f".vcstudio-adopt-{uuid.uuid4().hex}.stage"
        record = self._journal_begin(plan, stage, backups)
        projected_ledger = self._project_ledger(plan, ledger_entries)
        try:
            self._assert_jobs_cas(plan)
            project_bytes = _yaml_bytes(rewritten)
            job_projections = [
                (path, _yaml_bytes(manifest))
                for path, _original, manifest in updates
            ]
            self._prepare_file_projections(
                record, [(project_file, project_bytes), *job_projections])
            _atomic_write(project_file, project_bytes)
            for path, manifest_bytes in job_projections:
                _atomic_write(path, manifest_bytes)
            self._journal_phase(record, "filesystem_published")
            projected_registry = entries + [str(project_file)]
            self._prepare_authority_projection(
                record, authority="registry", payload=payload,
                list_key="projects", entries=projected_registry)
            self._commit_registry(plan, payload, projected_registry)
            self._journal_phase(record, "registry_written")
            self._prepare_authority_projection(
                record, authority="ledger", payload=ledger_payload,
                list_key="job_dirs", entries=projected_ledger)
            self._commit_ledger(record, plan, ledger_payload, projected_ledger)
            self._journal_phase(record, "ledger_written")
            self._journal_phase(record, "committed")
            try:
                self._cleanup_committed_journal(record)
            except Exception:
                pass
            return {
                "ok": True,
                "action": "adopt",
                "project_path": str(project_file),
                "project": rewritten,
                "project_uuid": _project_uuid(rewritten),
                "identity_mode": plan.identity_mode,
                "jobs_updated": len(plan.job_changes),
            }
        except Exception as exc:
            try:
                self._recover_record(record)
            except ProjectLifecyclePartialRollbackError as recovery_exc:
                raise recovery_exc from exc
            if isinstance(exc, ProjectLifecycleError):
                raise
            raise ProjectLifecycleError("adopt_failed", "The project was not adopted.") from exc

    def apply(self, plan: LifecyclePlan) -> dict[str, Any]:
        """Apply one fresh successful preflight plan exactly once."""
        with self._transaction_lock():
            self._recover_pending_locked()
            payload, entries, ledger_payload, ledger_entries = self._verify_plan(plan)
            if plan.action == "clone":
                return self._apply_clone(
                    plan, payload, entries, ledger_payload, ledger_entries)
            if plan.action == "move":
                return self._apply_move(
                    plan, payload, entries, ledger_payload, ledger_entries)
            if plan.action == "adopt":
                return self._apply_adopt(
                    plan, payload, entries, ledger_payload, ledger_entries)
            raise ProjectLifecycleError("action_invalid", "Unsupported lifecycle action.")


__all__ = [
    "JobLifecycleChange",
    "LifecyclePlan",
    "ProjectLifecycleError",
    "ProjectLifecyclePartialRollbackError",
    "ProjectLifecycleService",
]
