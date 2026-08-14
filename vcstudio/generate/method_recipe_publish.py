"""Recoverable publication transaction for confirmed Method Recipe input bundles.

The VASP inputs and recipe sidecar are prepared in a sibling staging directory.  A durable journal
is written before the first target mutation, ``job.yaml`` is the final bundle commit marker, and
ledger registration happens only after every published hash is rechecked.  A stable OS-level lock
serializes the same target across processes.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Mapping

from vcstudio.generate import method_recipes
from vcstudio.shared.config import user_config_dir


TRANSACTION_SCHEMA = "vcstudio.method-recipe-publish-transaction/v1"
TRANSACTION_DIR = "method-recipe-transactions"
_BUNDLE_FILES = (
    "INCAR", "POSCAR", "KPOINTS", "POTCAR", method_recipes.SIDECAR_NAME, "job.yaml",
)
_PRE_MANIFEST_FILES = tuple(name for name in _BUNDLE_FILES if name != "job.yaml")
_STATES = frozenset({
    "prepared", "publishing", "manifest_published", "ledger_registered",
    "recovery_required",
})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class MethodRecipePublishError(RuntimeError):
    """A safe publication failure; paths and injected exception text are never reflected."""

    def __init__(self, message: str, *, recovery_required: bool = False):
        super().__init__(message)
        self.recovery_required = bool(recovery_required)


def default_transaction_dir() -> Path:
    return user_config_dir() / TRANSACTION_DIR


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_id(target: Path) -> str:
    canonical = os.path.normcase(str(target.resolve(strict=False)))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _advisory_lock(path: Path, *, timeout: float):
    """Cross-process lock retaining one stable inode on Windows and POSIX."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + max(0.0, float(timeout))
        if os.name == "nt":
            import msvcrt

            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    if time.monotonic() >= deadline:
                        raise MethodRecipePublishError("method recipe target is busy") from exc
                    time.sleep(0.025)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise MethodRecipePublishError("method recipe target is busy") from exc
                    time.sleep(0.025)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class MethodRecipePublisher:
    """Stage, verify, journal, publish, register, and recover one confirmed input bundle."""

    def __init__(self, *, job_builder_mod, manifest_mod, ledger_mod,
                 registry_dir: str | os.PathLike | None = None,
                 lock_timeout: float = 10.0,
                 fault_hook: Callable[[str], None] | None = None):
        self.job_builder = job_builder_mod
        self.manifest = manifest_mod
        self.ledger = ledger_mod
        self.registry_dir = (Path(registry_dir) if registry_dir is not None
                             else default_transaction_dir())
        self.lock_timeout = float(lock_timeout)
        self.fault_hook = fault_hook

    def _fault(self, point: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(point)

    def _journal_path(self, path_id: str) -> Path:
        return self.registry_dir / f"{path_id}.json"

    def _lock_path(self, path_id: str) -> Path:
        return self.registry_dir / f"{path_id}.lock"

    def _ledger_lock(self) -> Path:
        return self.registry_dir / ".ledger.lock"

    def _ledger_register(self, target: Path) -> None:
        with _advisory_lock(self._ledger_lock(), timeout=self.lock_timeout):
            self.ledger.register(str(target))

    def _ledger_unregister(self, target: Path) -> None:
        if not hasattr(self.ledger, "unregister"):
            raise MethodRecipePublishError("job ledger cannot roll back recipe publication")
        with _advisory_lock(self._ledger_lock(), timeout=self.lock_timeout):
            self.ledger.unregister(str(target))

    @staticmethod
    def _target_has_managed_files(target: Path) -> bool:
        return any((target / name).exists() for name in _BUNDLE_FILES) if target.is_dir() else False

    @staticmethod
    def _assert_hashes(root: Path, expected: Mapping[str, str], names) -> None:
        for name in names:
            path = root / name
            if not path.is_file() or _sha256_file(path) != expected[name]:
                raise MethodRecipePublishError("staged or published recipe hash mismatch")

    def _write_journal(self, journal_path: Path, payload: dict[str, Any]) -> None:
        _atomic_json(journal_path, payload)

    def _read_journal(self, journal_path: Path) -> dict[str, Any]:
        try:
            if journal_path.stat().st_size > 256 * 1024:
                raise ValueError
            value = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise MethodRecipePublishError(
                "method recipe recovery journal is unreadable", recovery_required=True) from exc
        required = {
            "schema", "path_id", "target", "stage", "state", "files", "published",
            "target_id", "target_preexisted", "created_at_unix",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise MethodRecipePublishError(
                "method recipe recovery journal is invalid", recovery_required=True)
        target = Path(str(value["target"])).resolve(strict=False)
        stage = Path(str(value["stage"])).resolve(strict=False)
        path_id = str(value["path_id"])
        files = value["files"]
        if (value["schema"] != TRANSACTION_SCHEMA or not _HEX64.fullmatch(path_id)
                or journal_path.name != f"{path_id}.json" or _path_id(target) != path_id
                or value["state"] not in _STATES or not isinstance(files, dict)
                or set(files) != set(_BUNDLE_FILES)
                or any(not _HEX64.fullmatch(str(item)) for item in files.values())
                or not isinstance(value["published"], list)
                or not set(value["published"]).issubset(_BUNDLE_FILES)
                or not isinstance(value["target_preexisted"], bool)
                or stage.parent != target.parent
                or not stage.name.startswith(f".vcstudio-method-recipe-{path_id[:16]}-")):
            raise MethodRecipePublishError(
                "method recipe recovery journal is invalid", recovery_required=True)
        value["target"] = target
        value["stage"] = stage
        return value

    def _cleanup_journal(self, journal_path: Path, stage: Path) -> None:
        if stage.exists():
            shutil.rmtree(stage)
        journal_path.unlink(missing_ok=True)

    def _rollback_locked(self, journal_path: Path, journal: dict[str, Any]) -> bool:
        """Withdraw commit marker first, unregister, then remove only hash-bound members."""
        target: Path = journal["target"]
        stage: Path = journal["stage"]
        expected = journal["files"]
        errors = []
        try:
            self._ledger_unregister(target)
        except Exception:  # noqa: BLE001 - journal remains the recovery authority
            errors.append("ledger")
        ordered = ("job.yaml",) + tuple(reversed(_PRE_MANIFEST_FILES))
        for name in ordered:
            path = target / name
            if not path.exists():
                continue
            try:
                if not path.is_file() or _sha256_file(path) != expected[name]:
                    errors.append(name)
                    continue
                path.unlink()
            except OSError:
                errors.append(name)
        try:
            if stage.exists():
                shutil.rmtree(stage)
        except OSError:
            errors.append("stage")
        if not journal["target_preexisted"] and target.is_dir():
            try:
                target.rmdir()
            except OSError:
                # Preserve an externally created or non-empty directory.  Its changed target
                # identity will force a fresh preview instead of deleting unrelated content.
                if not any(target.iterdir()):
                    errors.append("target")
        if errors:
            journal["state"] = "recovery_required"
            self._write_journal(journal_path, journal)
            return False
        journal_path.unlink(missing_ok=True)
        return True

    def _recover_locked(self, journal_path: Path) -> str:
        journal = self._read_journal(journal_path)
        target: Path = journal["target"]
        expected = journal["files"]
        complete = target.is_dir() and all(
            (target / name).is_file() and _sha256_file(target / name) == expected[name]
            for name in _BUNDLE_FILES
        )
        if complete:
            try:
                self._ledger_register(target)
            except Exception as exc:  # noqa: BLE001 - retain journal for next startup
                journal["state"] = "manifest_published"
                self._write_journal(journal_path, journal)
                raise MethodRecipePublishError(
                    "valid recipe bundle awaits ledger recovery", recovery_required=True) from exc
            self._cleanup_journal(journal_path, journal["stage"])
            return "finalized"
        if self._rollback_locked(journal_path, journal):
            return "rolled_back"
        raise MethodRecipePublishError(
            "method recipe transaction requires manual recovery", recovery_required=True)

    def recover_all(self) -> dict[str, int]:
        """Recover every durable transaction after process restart; no directory scan is needed."""
        result = {"finalized": 0, "rolled_back": 0, "blocked": 0}
        if not self.registry_dir.is_dir():
            return result
        for journal_path in sorted(self.registry_dir.glob("*.json")):
            path_id = journal_path.stem
            if not _HEX64.fullmatch(path_id):
                result["blocked"] += 1
                continue
            try:
                with _advisory_lock(self._lock_path(path_id), timeout=self.lock_timeout):
                    outcome = self._recover_locked(journal_path)
                result[outcome] += 1
            except MethodRecipePublishError:
                result["blocked"] += 1
        return result

    def publish(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        target = Path(plan["target"]).resolve(strict=False)
        path_id = _path_id(target)
        journal_path = self._journal_path(path_id)
        stage: Path | None = None
        journal_written = False
        with _advisory_lock(self._lock_path(path_id), timeout=self.lock_timeout):
            if journal_path.is_file():
                self._recover_locked(journal_path)
            # This is the required second evidence validation, now inside the stable target lock.
            plan["revalidate"]()
            if self._target_has_managed_files(target):
                raise MethodRecipePublishError("method recipe target is no longer empty")
            target_preexisted = target.exists()
            target.parent.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(
                prefix=f".vcstudio-method-recipe-{path_id[:16]}-", dir=str(target.parent)))
            try:
                payload = self.job_builder.build_job_dir(
                    str(plan["poscar_path"]), plan["final_incar"], str(stage),
                    calc_type=plan["calc_type"], kpoints=plan["kpoints"],
                    validate=False, lib_root=plan["lib_root"])
                expected = dict(plan["expected_hashes"])
                self._assert_hashes(stage, expected, ("INCAR", "POSCAR", "KPOINTS", "POTCAR"))
                self._fault("after_build")

                incar_hash = expected["INCAR"]
                record = method_recipes.sidecar_record(plan, incar_sha256=incar_hash)
                _sidecar, sidecar_hash = method_recipes.write_sidecar(stage, record)
                expected[method_recipes.SIDECAR_NAME] = sidecar_hash
                self._fault("after_sidecar")
                reference = method_recipes.manifest_reference(
                    record, sidecar_sha256=sidecar_hash)
                written = self.manifest.create_from_build(
                    stage, payload, poscar_path=str(plan["poscar_path"]), validate=False,
                    task_type=plan["task"], method_recipe_ref=reference,
                    job_id=f"method-recipe-{plan['target_id'][:24]}")
                expected["job.yaml"] = _sha256_file(stage / "job.yaml")
                self._assert_hashes(stage, expected, _BUNDLE_FILES)
                manifest_inputs = written.get("inputs") or {}
                if (dict(manifest_inputs.get("sha256") or {}) != {
                        key: expected[key] for key in ("INCAR", "POSCAR", "KPOINTS", "POTCAR")}
                        or manifest_inputs.get("poscar_sha256") != expected["POSCAR"]
                        or manifest_inputs.get("potcar_sha256") != expected["POTCAR"]
                        or (manifest_inputs.get("method_recipe") or {}).get("sidecar_sha256")
                        != sidecar_hash
                        or (manifest_inputs.get("method_recipe") or {}).get("incar_sha256")
                        != expected["INCAR"]
                        or record["incar_sha256"] != expected["INCAR"]):
                    raise MethodRecipePublishError("staged manifest lineage mismatch")
                self._fault("after_manifest_stage")

                # Recheck the target immediately before journaling the first mutation.  Another
                # recipe publisher is excluded by the OS lock; unrelated external mutations abort.
                if self._target_has_managed_files(target):
                    raise MethodRecipePublishError("method recipe target changed during staging")
                journal = {
                    "schema": TRANSACTION_SCHEMA, "path_id": path_id,
                    "target": str(target), "stage": str(stage), "state": "prepared",
                    "files": expected, "published": [], "target_id": plan["target_id"],
                    "target_preexisted": target_preexisted,
                    "created_at_unix": int(time.time()),
                }
                self._write_journal(journal_path, journal)
                journal_written = True
                self._fault("after_journal")
                target.mkdir(parents=True, exist_ok=True)
                journal["state"] = "publishing"
                for name in _PRE_MANIFEST_FILES:
                    os.replace(stage / name, target / name)
                    journal["published"].append(name)
                    self._write_journal(journal_path, journal)
                    self._fault(f"after_publish_{name.lower()}")
                self._assert_hashes(target, expected, _PRE_MANIFEST_FILES)
                os.replace(stage / "job.yaml", target / "job.yaml")
                journal["published"].append("job.yaml")
                journal["state"] = "manifest_published"
                self._write_journal(journal_path, journal)
                self._fault("after_manifest_publish")
                self._assert_hashes(target, expected, _BUNDLE_FILES)
                self._ledger_register(target)
                journal["state"] = "ledger_registered"
                self._write_journal(journal_path, journal)
                self._fault("after_ledger_register")
                self._cleanup_journal(journal_path, stage)
                journal_written = False
                return {
                    "payload": payload, "manifest": written, "record": record,
                    "sidecar_sha256": sidecar_hash,
                    "input_hashes": {
                        key: expected[key] for key in ("INCAR", "POSCAR", "KPOINTS", "POTCAR")
                    },
                }
            except Exception as exc:  # noqa: BLE001 - transaction owns rollback and recovery
                recovery_required = False
                if journal_written and journal_path.is_file():
                    try:
                        journal = self._read_journal(journal_path)
                        recovery_required = not self._rollback_locked(journal_path, journal)
                        journal_written = recovery_required
                    except Exception:  # noqa: BLE001 - durable journal remains for startup recovery
                        recovery_required = True
                raise MethodRecipePublishError(
                    "confirmed recipe publication failed",
                    recovery_required=recovery_required) from exc
            finally:
                if stage is not None and stage.exists() and not journal_written:
                    shutil.rmtree(stage, ignore_errors=True)


__all__ = [
    "MethodRecipePublishError", "MethodRecipePublisher", "TRANSACTION_SCHEMA",
    "default_transaction_dir",
]
