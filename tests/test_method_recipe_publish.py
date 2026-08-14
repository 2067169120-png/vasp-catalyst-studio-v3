from __future__ import annotations

import hashlib
import json
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
        self._lock = threading.Lock()

    def register(self, path: str):
        with self._lock:
            self.register_calls += 1
            if self.fail_registers:
                self.fail_registers -= 1
                raise OSError("injected ledger failure with a private path")
            if path not in self.entries:
                self.entries.append(path)
        return True

    def unregister(self, path: str):
        with self._lock:
            self.unregister_calls += 1
            if path in self.entries:
                self.entries.remove(path)
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
    "fault_point",
    [
        "after_sidecar", "after_manifest_stage", "after_publish_method-recipe.json",
        "after_manifest_publish", "after_ledger_register",
    ],
)
def test_every_publish_write_failure_rolls_back_and_allows_safe_retry(tmp_path, fault_point):
    _service, _preview, _confirmation, plan, target, _poscar = _recipe_context(tmp_path)
    ledger = FakeLedger()

    def fail(point):
        if point == fault_point:
            raise RuntimeError("injected writer failure")

    with pytest.raises(MethodRecipePublishError) as exc:
        _publisher(tmp_path, ledger, fault_hook=fail).publish(plan)
    assert exc.value.recovery_required is False
    _assert_no_managed_bundle(target)
    assert ledger.entries == []
    assert not list((tmp_path / "transactions").glob("*.json"))

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
    assert first["retryable"] is True and first["recovery_required"] is False
    assert str(tmp_path) not in json.dumps(first)
    _assert_no_managed_bundle(target)
    assert ledger.entries == []

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

    with pytest.raises(MethodRecipePublishError, match="publication failed"):
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

    with pytest.raises(MethodRecipePublishError, match="publication failed"):
        _publisher(tmp_path, ledger, builder=LateSwappingBuilder).publish(plan)
    _assert_no_managed_bundle(target)
    assert ledger.entries == []


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize(
    "fault_point, expected_outcome",
    [("after_publish_incar", "rolled_back"), ("after_manifest_publish", "finalized")],
)
def test_restart_recovery_withdraws_partial_or_finalizes_complete_bundle(
        tmp_path, fault_point, expected_outcome):
    _service, _preview, _confirmation, plan, target, _poscar = _recipe_context(tmp_path)
    ledger = FakeLedger()

    def crash(point):
        if point == fault_point:
            raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        _publisher(tmp_path, ledger, fault_hook=crash).publish(plan)
    assert list((tmp_path / "transactions").glob("*.json"))

    recovery = _publisher(tmp_path, ledger).recover_all()
    assert recovery[expected_outcome] == 1
    assert recovery["blocked"] == 0
    assert not list((tmp_path / "transactions").glob("*.json"))
    if expected_outcome == "rolled_back":
        _assert_no_managed_bundle(target)
        assert ledger.entries == []
    else:
        assert (target / "job.yaml").is_file()
        assert ledger.entries == [str(target.resolve())]


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
