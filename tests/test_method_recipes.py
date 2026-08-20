from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

from vcstudio.generate import method_recipes
from vcstudio.generate.incar_builder import parse_incar
from vcstudio.cluster import ledger as cluster_ledger
from vcstudio.gui_web.api import Api


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


def _inputs(tmp_path: Path, *, existing: str | None = None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    poscar = tmp_path / "POSCAR.source"
    poscar.write_text(POSCAR, encoding="utf-8")
    incar = None
    if existing is not None:
        incar = tmp_path / "INCAR.source"
        incar.write_text(existing, encoding="utf-8")
    library = tmp_path / "potentials"
    for element, enmax in (("Fe", 300.0), ("O", 400.0)):
        target = library / element
        target.mkdir(parents=True)
        (target / "POTCAR").write_text(
            f"TITEL = PAW_PBE {element} test\nENMAX = {enmax}; ENMIN = 1\n",
            encoding="utf-8",
        )
    return poscar, incar, library, tmp_path / "new-job"


def _draft(*, policy_id: str | None = None):
    draft = method_recipes.suggested_draft("slab", "relax", policy_id)["draft"]
    draft.update({
        "dispersion": "none",
        "spin_mode": "nonspin",
        "hubbard_mode": "off",
        "dipole_mode": "off",
    })
    return draft


def _request(tmp_path: Path, *, existing: str | None = None, draft=None,
             policy_id: str | None = None):
    poscar, incar, library, out = _inputs(tmp_path, existing=existing)
    return {
        "poscar_path": str(poscar),
        "incar_path": str(incar) if incar else "",
        "out_dir": str(out),
        "lib_root": str(library),
        "draft": copy.deepcopy(draft or _draft(policy_id=policy_id)),
        "policy_id": policy_id,
        "project_id": None,
        "client_intent_id": "test-method-recipe-intent",
    }, out


def _confirmation(preview, *, resolutions=None, key="recipe-confirm-1"):
    return {
        "token": preview["token"],
        "preview_sha256": preview["preview_sha256"],
        "target_id": preview["target_id"],
        "client_intent_id": preview["client_intent_id"],
        "confirmed": True,
        "idempotency_key": key,
        "resolutions": resolutions or {},
    }


def test_recipe_dto_is_strict_versioned_explainable_and_preview_is_read_only(tmp_path):
    request, out = _request(tmp_path, policy_id="surface-production")
    request["draft"]["precision"] = "normal"
    service = method_recipes.MethodRecipeService()

    preview = service.preview(request, project_defaults={"precision": "normal"})

    assert preview["schema"] == method_recipes.PREVIEW_SCHEMA
    assert preview["recipe"]["schema"] == method_recipes.RECIPE_SCHEMA
    assert preview["recipe"]["version"] == 1
    assert preview["recipe"]["scientific_status"] == "candidate"
    assert preview["recipe"]["scientifically_validated"] is False
    assert preview["authorizes_submission"] is False
    assert not out.exists(), "preview must not create its target directory"

    required = {
        "value", "source", "reason", "risk", "evidence", "user_override",
        "semantic_sha256",
    }
    for group in preview["recipe"]["final"].values():
        for entry in group.values():
            assert set(entry) == required
            assert entry["source"] in method_recipes.SOURCES
            assert entry["risk"] in method_recipes.RISKS
            assert set(entry["reason"]) == {"zh", "en"}
            assert len(entry["semantic_sha256"]) == 64
    assert preview["recipe"]["final"]["INCAR"]["EDIFF"]["source"] == "policy"
    assert preview["recipe"]["final"]["INCAR"]["PREC"]["source"] == "project"
    assert preview["recipe"]["final"]["INCAR"]["ISPIN"]["source"] == "user"


def test_existing_keys_default_to_keep_and_each_conflict_requires_explicit_choice(tmp_path):
    request, _out = _request(tmp_path, existing="ENCUT = 450\nSYSTEM = keep-me\n")
    service = method_recipes.MethodRecipeService()
    preview = service.preview(request)

    encut = next(item for item in preview["diff"] if item["key"] == "ENCUT")
    assert encut == {
        **encut,
        "status": "conflict",
        "existing": 450,
        "proposed": 520,
        "default_action": "existing",
        "requires_resolution": True,
    }
    captured = {}

    with pytest.raises(method_recipes.MethodRecipeError, match="every INCAR conflict"):
        service.confirm(_confirmation(preview), lambda plan: {"ok": True})

    result = service.confirm(
        _confirmation(preview, resolutions={"ENCUT": "existing"}),
        lambda plan: captured.update(plan) or {"ok": True, "job_id": "job-1"},
    )
    parsed = parse_incar(captured["final_incar"])
    assert result["job_id"] == "job-1"
    assert parsed["ENCUT"] == 450
    assert parsed["SYSTEM"] == "keep-me"
    entry = captured["recipe"]["final"]["INCAR"]["ENCUT"]
    assert entry["source"] == "user" and entry["user_override"] is True


def test_preview_token_is_single_use_tamper_bound_and_idempotent(tmp_path):
    request, _out = _request(tmp_path)
    service = method_recipes.MethodRecipeService()
    preview = service.preview(request)
    calls = []

    tampered = _confirmation(preview)
    tampered["preview_sha256"] = "0" * 64
    with pytest.raises(method_recipes.MethodRecipeTokenError, match="hash"):
        service.confirm(tampered, lambda plan: {"ok": True})
    forged_target = _confirmation(preview)
    forged_target["target_id"] = "0" * 64
    with pytest.raises(method_recipes.MethodRecipeTokenError, match="target identity"):
        service.confirm(forged_target, lambda plan: {"ok": True})
    forged_intent = _confirmation(preview)
    forged_intent["client_intent_id"] = "different-browser-intent"
    with pytest.raises(method_recipes.MethodRecipeTokenError, match="client intent"):
        service.confirm(forged_intent, lambda plan: {"ok": True})
    forged_token = _confirmation(preview)
    forged_token["token"] += "tampered"
    with pytest.raises(method_recipes.MethodRecipeTokenError, match="unknown"):
        service.confirm(forged_token, lambda plan: {"ok": True})

    confirm = _confirmation(preview)
    first = service.confirm(
        confirm, lambda plan: calls.append(plan["target_id"]) or {"ok": True, "job_id": "j1"})
    second = service.confirm(confirm, lambda plan: pytest.fail("idempotent replay wrote twice"))
    assert first == second == {"ok": True, "job_id": "j1"}
    assert len(calls) == 1

    replay = dict(confirm, idempotency_key="different-key")
    with pytest.raises(method_recipes.MethodRecipeTokenError, match="already been used"):
        service.confirm(replay, lambda plan: {"ok": True})


def test_preview_token_expires_and_source_tamper_requires_new_preview(tmp_path):
    now = [1000.0]
    service = method_recipes.MethodRecipeService(clock=lambda: now[0], ttl_seconds=30)
    request, _out = _request(tmp_path)
    preview = service.preview(request)
    now[0] = 1031.0
    with pytest.raises(method_recipes.MethodRecipeTokenError, match="unknown or expired"):
        service.confirm(_confirmation(preview), lambda plan: {"ok": True})

    request2, _out2 = _request(tmp_path / "second")
    preview2 = service.preview(request2)
    Path(request2["poscar_path"]).write_text(POSCAR.replace("FeO slab", "changed"), encoding="utf-8")
    with pytest.raises(method_recipes.MethodRecipeTokenError, match="POSCAR changed"):
        service.confirm(_confirmation(preview2), lambda plan: {"ok": True})

    request3, _out3 = _request(tmp_path / "potcar-tamper")
    preview3 = service.preview(request3)
    oxygen = Path(request3["lib_root"]) / "O" / "POTCAR"
    oxygen.write_text(oxygen.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
    with pytest.raises(method_recipes.MethodRecipeTokenError, match="pseudopotential evidence"):
        service.confirm(_confirmation(preview3), lambda plan: {"ok": True})

    request4, out4 = _request(tmp_path / "target-tamper")
    preview4 = service.preview(request4)
    out4.mkdir()
    (out4 / "INCAR").write_text("ENCUT = 999\n", encoding="utf-8")
    with pytest.raises(method_recipes.MethodRecipeTokenError, match="evidence"):
        service.confirm(_confirmation(preview4), lambda plan: {"ok": True})


@pytest.mark.parametrize("kind", ["empty-directory", "sentinel-directory"])
def test_preview_requires_target_name_to_be_completely_absent(tmp_path, kind):
    request, target = _request(tmp_path)
    target.mkdir()
    if kind == "sentinel-directory":
        (target / "SENTINEL.txt").write_text("external", encoding="utf-8")

    with pytest.raises(method_recipes.MethodRecipeError, match="does not already exist"):
        method_recipes.MethodRecipeService().preview(request)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda draft: draft.update(xc="SCAN"), "pseudopotential mapping is unknown"),
        (lambda draft: draft.update(spin_mode="unresolved"), "magnetic state"),
        (lambda draft: draft.update(hubbard_mode="unresolved"), r"DFT\+U"),
        (lambda draft: draft.update(hubbard_mode="manual", hubbard_u={"Fe": {"l": 2, "u": 4, "j": 0}}),
         "map every POSCAR species"),
    ],
)
def test_unknown_functional_magnetic_state_and_u_fail_closed(tmp_path, mutate, message):
    draft = _draft()
    mutate(draft)
    request, _out = _request(tmp_path, draft=draft)
    with pytest.raises(method_recipes.MethodRecipeError, match=message):
        method_recipes.MethodRecipeService().preview(request)


def test_preview_and_errors_do_not_expose_paths_or_secret_values(tmp_path):
    request, _out = _request(tmp_path)
    preview = method_recipes.MethodRecipeService().preview(request)
    serialized = json.dumps(preview, ensure_ascii=False)
    assert str(tmp_path) not in serialized
    assert request["lib_root"] not in serialized

    api = Api(method_recipe_service=method_recipes.MethodRecipeService())
    forged = dict(request)
    forged["private_key_path"] = r"C:\private\needle.pem"
    result = api.method_recipe_preview(forged)
    assert result["ok"] is False
    assert "needle" not in json.dumps(result)
    secret_id = dict(request, project_id="ghp_abcdefghijk")
    rejected_id = api.method_recipe_preview(secret_id)
    assert rejected_id["ok"] is False
    assert "ghp_abcdefghijk" not in json.dumps(rejected_id)

    secret_request, _secret_out = _request(
        tmp_path / "secret-existing", existing=r"ENCUT = C:\private\ghp_abcdefghijk" + "\n")
    secret_service = method_recipes.MethodRecipeService()
    secret_preview = secret_service.preview(secret_request)
    encut = next(item for item in secret_preview["diff"] if item["key"] == "ENCUT")
    assert encut["existing"] == "[redacted]"
    assert "ghp_abcdefghijk" not in json.dumps(secret_preview)
    with pytest.raises(method_recipes.MethodRecipeError, match="unsafe"):
        secret_service.confirm(
            _confirmation(secret_preview, resolutions={"ENCUT": "existing"}),
            lambda plan: {"ok": True},
        )


def test_installed_pseudopotential_family_mismatch_fails_closed_without_path(tmp_path):
    request, _out = _request(tmp_path)
    oxygen = Path(request["lib_root"]) / "O" / "POTCAR"
    oxygen.write_text("TITEL = PAW_LDA O test\nENMAX = 400.0\n", encoding="utf-8")

    with pytest.raises(method_recipes.MethodRecipeError, match="mapping is unverified") as exc:
        method_recipes.MethodRecipeService().preview(request)
    assert str(tmp_path) not in str(exc.value)


def test_keep_existing_cannot_bypass_fail_closed_functional_mapping(tmp_path):
    request, _out = _request(tmp_path, existing="GGA = RP\n")
    service = method_recipes.MethodRecipeService()
    preview = service.preview(request)
    assert preview["conflicts"] == ["GGA"]

    with pytest.raises(method_recipes.MethodRecipeError, match="PBE/PAW_PBE"):
        service.confirm(
            _confirmation(preview, resolutions={"GGA": "existing"}),
            lambda plan: {"ok": True},
        )


@pytest.mark.parametrize(
    "tag, value",
    [
        ("LUSE_VDW", ".TRUE."),
        ("LNONCOLLINEAR", ".TRUE."),
        ("LSORBIT", ".TRUE."),
        ("SAXIS", "0 0 1"),
        ("AEXX", 0.25),
        ("MYSTERY_SCIENCE_CONTROL", 1),
    ],
)
def test_existing_method_significant_or_unknown_tags_cannot_pass_through(
        tmp_path, tag, value):
    request, _out = _request(tmp_path, existing=f"{tag} = {value}\n")
    service = method_recipes.MethodRecipeService()
    preview = service.preview(request)

    item = next(entry for entry in preview["diff"] if entry["key"] == tag)
    assert item["status"] == "remove_conflict"
    assert item["proposed"] is None
    assert item["requires_resolution"] is True
    assert preview["recipe"]["final"]["INCAR"][tag]["risk"] == "blocking"

    with pytest.raises(method_recipes.MethodRecipeError, match="method-significant"):
        service.confirm(
            _confirmation(preview, resolutions={tag: "existing"}),
            lambda plan: {"ok": True},
        )

    captured = {}
    result = service.confirm(
        _confirmation(preview, resolutions={tag: "recipe"}, key=f"remove-{tag}"),
        lambda plan: captured.update(plan) or {"ok": True},
    )
    assert result["ok"] is True
    assert tag not in parse_incar(captured["final_incar"])


def test_explicit_false_toggles_and_bounded_neutral_tags_are_hashed_passthrough(tmp_path):
    request, _out = _request(
        tmp_path, existing="LSORBIT = .FALSE.\nLUSE_VDW = .FALSE.\nNCORE = 4\nSYSTEM = retained\n")
    service = method_recipes.MethodRecipeService()
    preview = service.preview(request)

    assert preview["conflicts"] == []
    captured = {}
    service.confirm(
        _confirmation(preview), lambda plan: captured.update(plan) or {"ok": True})
    parsed = parse_incar(captured["final_incar"])
    assert parsed["LSORBIT"] is False and parsed["LUSE_VDW"] is False
    assert parsed["NCORE"] == 4 and parsed["SYSTEM"] == "retained"
    for key in ("LSORBIT", "LUSE_VDW", "NCORE", "SYSTEM"):
        entry = captured["recipe"]["final"]["INCAR"][key]
        assert entry["value"] == parsed[key]
        assert entry["source"] == "user"
        assert len(entry["semantic_sha256"]) == 64


def test_unsupported_ldautype_99_requires_removal_and_cannot_be_retained(tmp_path):
    request, _out = _request(tmp_path, existing="LDAUTYPE = 99\n")
    service = method_recipes.MethodRecipeService()
    preview = service.preview(request)
    assert preview["conflicts"] == ["LDAUTYPE"]
    with pytest.raises(method_recipes.MethodRecipeError, match=r"DFT\+U parameters"):
        service.confirm(
            _confirmation(preview, resolutions={"LDAUTYPE": "existing"}),
            lambda plan: {"ok": True},
        )


@pytest.mark.parametrize(
    "tag, value, message",
    [
        ("MAGMOM", "2*1", "nonspin"),
        ("IDIPOL", 3, "dipole"),
        ("DIPOL", "0.5 0.5 0.5", "dipole"),
        ("EB_K", 78.4, "VASPsol"),
    ],
)
def test_inactive_method_parameters_are_not_compatible_passthrough(
        tmp_path, tag, value, message):
    request, _out = _request(tmp_path, existing=f"{tag} = {value}\n")
    service = method_recipes.MethodRecipeService()
    preview = service.preview(request)
    assert preview["conflicts"] == [tag]

    with pytest.raises(method_recipes.MethodRecipeError, match=message):
        service.confirm(
            _confirmation(preview, resolutions={tag: "existing"}),
            lambda plan: {"ok": True},
        )


@pytest.mark.parametrize(
    "system_type, task", [("molecule", "cellopt"), ("bulk", "workfunction")],
)
def test_system_task_compatibility_matrix_rejects_unsupported_pairs(
        tmp_path, system_type, task):
    with pytest.raises(method_recipes.MethodRecipeError, match="incompatible"):
        method_recipes.suggested_draft(system_type, task)

    draft = _draft()
    draft.update(system_type=system_type, task=task)
    request, _out = _request(tmp_path, draft=draft)
    with pytest.raises(method_recipes.MethodRecipeError, match="incompatible"):
        method_recipes.MethodRecipeService().preview(request)

    catalog = method_recipes.catalog()
    task_record = next(item for item in catalog["tasks"] if item["key"] == task)
    assert system_type not in task_record["systems"]


def test_confirm_writes_sidecar_manifest_and_registers_without_submission(tmp_path):
    request, out = _request(tmp_path)
    api = Api(
        method_recipe_service=method_recipes.MethodRecipeService(),
        ledger_mod=cluster_ledger,
    )
    preview = api.method_recipe_preview(request)
    result = api.method_recipe_confirm(_confirmation(preview))

    assert result["ok"] is True
    assert result["state"] == "CREATED"
    assert result["submitted"] is False
    assert result["authorizes_submission"] is False
    assert result["scientifically_validated"] is False
    assert str(out) not in json.dumps(result)
    assert cluster_ledger.list_dirs() == [str(out.resolve())]

    sidecar = json.loads((out / method_recipes.SIDECAR_NAME).read_text(encoding="utf-8"))
    manifest = yaml.safe_load((out / "job.yaml").read_text(encoding="utf-8"))
    reference = manifest["inputs"]["method_recipe"]
    assert sidecar["scientific_status"] == "candidate"
    assert sidecar["authorizes_submission"] is False
    assert reference["sidecar"] == method_recipes.SIDECAR_NAME
    assert reference["sidecar_sha256"] == result["sidecar_sha256"]
    assert reference["recipe_semantic_sha256"] == sidecar["recipe_semantic_sha256"]
    assert manifest["state"] == "CREATED"


def test_optional_convergence_package_is_dry_run_and_never_claims_convergence(tmp_path):
    draft = _draft()
    draft["include_convergence_dry_run"] = True
    request, _out = _request(tmp_path, draft=draft)
    package = method_recipes.MethodRecipeService().preview(request)["recipe"][
        "convergence_dry_run"]

    assert package["scientifically_converged"] is False
    assert package["authorizes_submission"] is False
    assert {item["task"] for item in package["series"]} == {
        "conv_encut", "conv_kmesh", "conv_vacuum", "conv_thickness",
    }
