from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

import pytest
import yaml

from vcstudio.generate import job_builder, method_recipes
from vcstudio.generate.method_recipe_publish import (
    MethodRecipePublishError,
    MethodRecipePublisher,
)
from vcstudio.gui_web.api import Api
from vcstudio.shared import manifest


POSCAR = """FeO slab
1.0
4.0 0.0 0.0
0.0 4.0 0.0
0.0 0.0 20.0
Fe O
1 1
Direct
0.5 0.5 0.45
0.5 0.5 0.55
"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeLedger:
    def __init__(self, *, fail_registers: int = 0):
        self.entries: list[str] = []
        self.register_calls = 0
        self.unregister_calls = 0
        self.fail_registers = fail_registers
        self.owners: dict[str, str] = {}
        self._lock = threading.Lock()

    def register(self, path: str):
        with self._lock:
            if path in self.entries:
                return False
            self.entries.append(path)
            return True

    def register_owned(self, path: str, transaction_id: str):
        with self._lock:
            self.register_calls += 1
            if self.fail_registers:
                self.fail_registers -= 1
                raise OSError("injected ledger failure with a private path")
            if path in self.entries:
                owned = self.owners.get(path) == transaction_id
                return {"added": owned, "preexisting": not owned, "owned": owned}
            self.entries.append(path)
            self.owners[path] = transaction_id
            return {"added": True, "preexisting": False, "owned": True}

    def registration_state(self, path: str, transaction_id: str):
        with self._lock:
            present = path in self.entries
            owned = present and self.owners.get(path) == transaction_id
            return {"present": present, "owned": owned, "preexisting": present and not owned}

    def unregister_owned(self, path: str, transaction_id: str):
        with self._lock:
            self.unregister_calls += 1
            if path not in self.entries or self.owners.get(path) != transaction_id:
                return False
            self.entries.remove(path)
            self.owners.pop(path, None)
            return True

    def release_registration_owner(self, path: str, transaction_id: str):
        with self._lock:
            if path not in self.entries or self.owners.get(path) != transaction_id:
                return False
            self.owners.pop(path, None)
            return True


def _recipe_context(tmp_path: Path, *, out: Path | None = None, intent: str = "publish-intent"):
    source = tmp_path / f"source-{intent}"
    source.mkdir(parents=True)
    poscar = source / "POSCAR"
    poscar.write_text(POSCAR, encoding="utf-8")
    library = source / "potentials"
    for element, enmax in (("Fe", 300.0), ("O", 400.0)):
        target = library / element
        target.mkdir(parents=True)
        (target / "POTCAR").write_text(
            f"TITEL = PAW_PBE {element} test\nENMAX = {enmax}; ENMIN = 1\n",
            encoding="utf-8",
        )
    draft = method_recipes.suggested_draft("slab", "relax")["draft"]
    draft.update({
        "dispersion": "none", "spin_mode": "nonspin", "hubbard_mode": "off",
        "dipole_mode": "off",
    })
    destination = out or (tmp_path / "job")
    request = {
        "poscar_path": str(poscar), "incar_path": "", "out_dir": str(destination),
        "lib_root": str(library), "draft": draft, "policy_id": None,
        "project_id": None, "client_intent_id": intent,
    }
    service = method_recipes.MethodRecipeService()
    preview = service.preview(request)
    confirmation = {
        "token": preview["token"], "preview_sha256": preview["preview_sha256"],
        "target_id": preview["target_id"], "client_intent_id": preview["client_intent_id"],
        "confirmed": True, "idempotency_key": f"confirm-{intent}", "resolutions": {},
    }
    captured = {}
    assert service.confirm(
        confirmation, lambda plan: captured.update(plan) or {"ok": False}) == {"ok": False}
    return service, preview, confirmation, captured, destination, poscar


def _publisher(tmp_path: Path, ledger: FakeLedger, *, fault_hook=None, builder=job_builder):
    return MethodRecipePublisher(
        job_builder_mod=builder, manifest_mod=manifest, ledger_mod=ledger,
        registry_dir=tmp_path / "transactions", lock_timeout=5, fault_hook=fault_hook,
    )


def _assert_no_managed_bundle(target: Path):
    managed = {
        "INCAR", "POSCAR", "KPOINTS", "POTCAR", method_recipes.SIDECAR_NAME, "job.yaml",
    }
    assert not any((target / name).exists() for name in managed)


def test_publish_asserts_full_lineage_before_registration(tmp_path):
    _service, _preview, _confirmation, plan, target, _poscar = _recipe_context(tmp_path)
    ledger = FakeLedger()

    result = _publisher(tmp_path, ledger).publish(plan)

    assert ledger.entries == [str(target.resolve())]
    assert result["input_hashes"] == {
        name: _sha256(target / name) for name in ("INCAR", "POSCAR", "KPOINTS", "POTCAR")
    }
    sidecar = json.loads((target / method_recipes.SIDECAR_NAME).read_text(encoding="utf-8"))
    written = yaml.safe_load((target / "job.yaml").read_text(encoding="utf-8"))
    assert written["inputs"]["sha256"] == result["input_hashes"]
    assert sidecar["incar_sha256"] == result["input_hashes"]["INCAR"]
    assert written["inputs"]["method_recipe"]["sidecar_sha256"] == _sha256(
        target / method_recipes.SIDECAR_NAME)
    assert written["inputs"]["method_recipe"]["recipe_semantic_sha256"] == (
        sidecar["recipe_semantic_sha256"])
    assert not list((tmp_path / "transactions").glob("*.json"))


@pytest.mark.parametrize(
    "fault_point, journal_expected",
    [("after_sidecar", False), ("after_manifest_stage", False),
     ("after_journal", True), ("after_bundle_publish", True),
     ("after_ledger_register", True)],
)
def test_every_publish_failure_is_clean_or_journaled_and_allows_safe_retry(
        tmp_path, fault_point, journal_expected):
    _service, _preview, _confirmation, plan, target, _poscar = _recipe_context(tmp_path)
    ledger = FakeLedger()

    def fail(point):
        if point == fault_point:
            raise RuntimeError("injected writer failure")

    with pytest.raises(MethodRecipePublishError) as exc:
        _publisher(tmp_path, ledger, fault_hook=fail).publish(plan)
    assert exc.value.recovery_required is journal_expected
    journals = list((tmp_path / "transactions").glob("*.json"))
    assert bool(journals) is journal_expected
    if not journal_expected:
        assert not target.exists()
    if fault_point == "after_ledger_register":
        assert ledger.entries == [], "only this transaction's newly added entry is withdrawn"
        journal = json.loads(journals[0].read_text(encoding="utf-8"))
        assert journal["ledger_added"] is True
        assert journal["ledger_preexisting"] is False
        assert ledger.unregister_calls == 1
    else:
        assert ledger.unregister_calls == 0, "no pre-registration failure may unregister"
    if fault_point == "after_journal":
        journal = json.loads(journals[0].read_text(encoding="utf-8"))
        expected_kind = "windows_file_id" if os.name == "nt" else "posix_inode"
        assert journal["parent_identity"]["kind"] == expected_kind
        assert journal["stage_identity"]["kind"] == expected_kind
        assert journal["target_identity"] is None
    if fault_point == "after_bundle_publish":
        journal = json.loads(journals[0].read_text(encoding="utf-8"))
        assert journal["target_identity"] == journal["stage_identity"]

    result = _publisher(tmp_path, ledger).publish(plan)
    assert result["manifest"]["state"] == "CREATED"
    assert ledger.entries == [str(target.resolve())]


def test_writer_false_does_not_consume_token_and_ledger_failure_is_retryable(tmp_path):
    service, _preview, confirmation, _plan, target, _poscar = _recipe_context(tmp_path)
    ledger = FakeLedger(fail_registers=1)
    publisher = _publisher(tmp_path, ledger)
    api = Api(method_recipe_service=service, method_recipe_publisher=publisher)

    first = api.method_recipe_confirm(confirmation)
    assert first["ok"] is False
    assert first["retryable"] is True and first["recovery_required"] is True
    assert str(tmp_path) not in json.dumps(first)
    assert (target / "job.yaml").is_file()
    assert ledger.entries == []
    assert list((tmp_path / "transactions").glob("*.json"))

    second = api.method_recipe_confirm(confirmation)
    third = api.method_recipe_confirm(confirmation)
    assert second == third
    assert second["ok"] is True and second["state"] == "CREATED"
    assert ledger.entries == [str(target.resolve())]
    assert ledger.register_calls == 2


def test_source_replacement_after_locked_revalidation_is_detected_before_publish(tmp_path):
    _service, _preview, _confirmation, plan, target, poscar = _recipe_context(tmp_path)
    ledger = FakeLedger()

    class SwappingBuilder:
        @staticmethod
        def build_job_dir(*args, **kwargs):
            poscar.write_text(POSCAR.replace("FeO slab", "swapped source"), encoding="utf-8")
            return job_builder.build_job_dir(*args, **kwargs)

    with pytest.raises(MethodRecipePublishError):
        _publisher(tmp_path, ledger, builder=SwappingBuilder).publish(plan)
    _assert_no_managed_bundle(target)
    assert ledger.entries == []


def test_source_replacement_after_staging_cannot_fork_manifest_lineage(tmp_path):
    _service, _preview, _confirmation, plan, target, poscar = _recipe_context(tmp_path)
    ledger = FakeLedger()

    class LateSwappingBuilder:
        @staticmethod
        def build_job_dir(*args, **kwargs):
            result = job_builder.build_job_dir(*args, **kwargs)
            poscar.write_text(POSCAR.replace("FeO slab", "late swapped source"), encoding="utf-8")
            return result

    with pytest.raises(MethodRecipePublishError):
        _publisher(tmp_path, ledger, builder=LateSwappingBuilder).publish(plan)
    _assert_no_managed_bundle(target)
    assert ledger.entries == []


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize(
    "fault_point",
    ["after_journal", "after_bundle_publish"],
)
def test_restart_recovery_resumes_precommit_or_complete_atomic_bundle(tmp_path, fault_point):
    _service, _preview, _confirmation, plan, target, _poscar = _recipe_context(tmp_path)
    ledger = FakeLedger()

    def crash(point):
        if point == fault_point:
            raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        _publisher(tmp_path, ledger, fault_hook=crash).publish(plan)
    assert list((tmp_path / "transactions").glob("*.json"))

    recovery = _publisher(tmp_path, ledger).recover_all()
    assert recovery["finalized"] == 1
    assert recovery["blocked"] == 0
    assert not list((tmp_path / "transactions").glob("*.json"))
    assert (target / "job.yaml").is_file()
    assert ledger.entries == [str(target.resolve())]


def test_atomic_publish_never_clobbers_external_target_or_sentinel(tmp_path):
    _service, _preview, _confirmation, plan, target, _poscar = _recipe_context(tmp_path)
    ledger = FakeLedger()

    def insert_target(point):
        if point == "before_bundle_publish":
            target.mkdir()
            (target / "SENTINEL.txt").write_text("external authority", encoding="utf-8")

    with pytest.raises(MethodRecipePublishError) as exc:
        _publisher(tmp_path, ledger, fault_hook=insert_target).publish(plan)
    assert exc.value.recovery_required is True
    assert (target / "SENTINEL.txt").read_text(encoding="utf-8") == "external authority"
    assert not (target / "job.yaml").exists()
    assert ledger.entries == []
    assert list((tmp_path / "transactions").glob("*.json"))
    recovery = _publisher(tmp_path, ledger).recover_all()
    assert recovery["blocked"] == 1
    assert (target / "SENTINEL.txt").is_file()


def test_external_file_inserted_into_published_entity_blocks_registration(tmp_path):
    _service, _preview, _confirmation, plan, target, _poscar = _recipe_context(tmp_path)
    ledger = FakeLedger()

    def insert_sentinel(point):
        if point == "after_bundle_publish":
            (target / "SENTINEL.txt").write_text("do not remove", encoding="utf-8")

    with pytest.raises(MethodRecipePublishError) as exc:
        _publisher(tmp_path, ledger, fault_hook=insert_sentinel).publish(plan)
    assert exc.value.recovery_required is True
    assert (target / "SENTINEL.txt").read_text(encoding="utf-8") == "do not remove"
    assert ledger.entries == []
    assert list((tmp_path / "transactions").glob("*.json"))


def test_directory_exchange_between_rename_and_handle_binding_is_rejected(tmp_path):
    _service, _preview, _confirmation, plan, target, _poscar = _recipe_context(tmp_path)
    ledger = FakeLedger()
    displaced = tmp_path / "displaced-original"

    def exchange(point):
        if point == "after_bundle_rename_before_bind":
            target.rename(displaced)
            target.mkdir()
            (target / "SENTINEL.txt").write_text("replacement", encoding="utf-8")

    with pytest.raises(MethodRecipePublishError) as exc:
        _publisher(tmp_path, ledger, fault_hook=exchange).publish(plan)
    assert exc.value.recovery_required is True
    assert (displaced / "job.yaml").is_file()
    assert (target / "SENTINEL.txt").is_file()
    assert ledger.entries == []
    assert list((tmp_path / "transactions").glob("*.json"))


def test_preexisting_ledger_entry_is_never_unregistered_by_transaction_rollback(tmp_path):
    _service, _preview, _confirmation, plan, target, _poscar = _recipe_context(tmp_path)
    ledger = FakeLedger()
    assert ledger.register(str(target.resolve())) is True

    def fail_after_registration(point):
        if point == "after_ledger_register":
            raise RuntimeError("force post-registration rollback")

    with pytest.raises(MethodRecipePublishError):
        _publisher(tmp_path, ledger, fault_hook=fail_after_registration).publish(plan)
    journal_path = next((tmp_path / "transactions").glob("*.json"))
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["ledger_preexisting"] is True
    assert journal["ledger_added"] is False
    assert ledger.unregister_calls == 0
    assert ledger.entries == [str(target.resolve())]

    recovery = _publisher(tmp_path, ledger).recover_all()
    assert recovery["finalized"] == 1
    assert ledger.entries == [str(target.resolve())]


def test_post_registration_bundle_mutation_is_detected_and_owned_entry_withdrawn(tmp_path):
    _service, _preview, _confirmation, plan, target, _poscar = _recipe_context(tmp_path)

    class MutatingLedger(FakeLedger):
        def register_owned(self, path: str, transaction_id: str):
            result = super().register_owned(path, transaction_id)
            (target / "SENTINEL.txt").write_text("inserted during register", encoding="utf-8")
            return result

    ledger = MutatingLedger()
    with pytest.raises(MethodRecipePublishError) as exc:
        _publisher(tmp_path, ledger).publish(plan)
    assert exc.value.recovery_required is True
    assert ledger.entries == []
    assert ledger.unregister_calls == 1
    assert (target / "SENTINEL.txt").is_file()
    assert list((tmp_path / "transactions").glob("*.json"))


def test_concurrent_publishers_serialize_same_target_and_only_one_registers(tmp_path):
    shared_target = tmp_path / "shared-job"
    _s1, _p1, _c1, plan1, _target1, _src1 = _recipe_context(
        tmp_path, out=shared_target, intent="parallel-one")
    _s2, _p2, _c2, plan2, _target2, _src2 = _recipe_context(
        tmp_path, out=shared_target, intent="parallel-two")
    ledger = FakeLedger()
    entered = threading.Event()
    release = threading.Event()
    results: list[tuple[str, object]] = []

    def hold_after_journal(point):
        if point == "after_journal":
            entered.set()
            assert release.wait(5)

    def run(label, publisher, plan):
        try:
            results.append((label, publisher.publish(plan)))
        except Exception as exc:  # noqa: BLE001 - the losing request must fail closed
            results.append((label, exc))

    first = threading.Thread(
        target=run, args=("first", _publisher(tmp_path, ledger, fault_hook=hold_after_journal),
                          plan1))
    second = threading.Thread(
        target=run, args=("second", _publisher(tmp_path, ledger), plan2))
    first.start()
    assert entered.wait(5)
    second.start()
    release.set()
    first.join(10)
    second.join(10)

    assert not first.is_alive() and not second.is_alive()
    successes = [value for _label, value in results if isinstance(value, dict)]
    failures = [value for _label, value in results if isinstance(value, Exception)]
    assert len(successes) == len(failures) == 1
    assert ledger.entries == [str(shared_target.resolve())]
    written = yaml.safe_load((shared_target / "job.yaml").read_text(encoding="utf-8"))
    assert written["state"] == "CREATED"
    assert not list((tmp_path / "transactions").glob("*.json"))
