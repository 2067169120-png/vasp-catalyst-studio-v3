"""Path-minimised pywebview bridge contracts for project lifecycle operations."""
from __future__ import annotations

import json
import time
import types
from pathlib import Path

import yaml

from vcstudio.gui_web.api import Api
from vcstudio.project.project_lifecycle import ProjectLifecycleService


def _write_project(root: Path, project_uuid: str, name: str = "study") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    member = root / "job"
    member.mkdir()
    (member / "job.yaml").write_text(
        yaml.safe_dump({"job_id": f"job-{name}", "state": "CREATED"},
                       sort_keys=False), encoding="utf-8")
    project = {
        "schema": 1,
        "name": name,
        "root": str(root),
        "project_uuid": project_uuid,
        "members": {"clean_slab": str(member), "gas_ref": None, "configs": []},
    }
    path = root / "project.yaml"
    path.write_text(yaml.safe_dump(project, sort_keys=False), encoding="utf-8")
    return path


def _adsorption(registry: Path):
    def listing():
        if not registry.exists():
            return []
        return json.loads(registry.read_text(encoding="utf-8"))["projects"]

    def load(path):
        candidate = Path(path)
        if candidate.is_dir():
            candidate /= "project.yaml"
        if not candidate.is_file():
            return None
        value = yaml.safe_load(candidate.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None

    return types.SimpleNamespace(
        default_registry_path=lambda: registry,
        list_projects=listing,
        load_project=load,
    )


def _api(registry: Path, selected: Path) -> Api:
    service = ProjectLifecycleService(registry)
    manifest = types.SimpleNamespace(load_manifest=lambda _path: {"state": "CREATED"})
    return Api(
        adsorption_mod=_adsorption(registry),
        manifest_mod=manifest,
        project_lifecycle_service=service,
        dialog_fn=lambda kind: str(selected) if kind == "dir" else None,
    )


def test_adopt_bridge_uses_server_selection_defaults_to_remint_and_never_returns_path(
        tmp_path):
    source = _write_project(tmp_path / "copied", "a" * 32)
    registry = tmp_path / "projects.json"
    registry.write_text('{"projects": []}', encoding="utf-8")
    api = _api(registry, source.parent)

    selected = api.proj_lifecycle_select("adopt_source")
    preflight = api.proj_lifecycle_preflight(
        "adopt", selection_token=selected["selection_token"]
    )

    assert selected["ok"] and preflight["ready"]
    assert preflight["identity_mode"] == "remint"
    assert preflight["jobs"]["count"] == 1
    public_json = json.dumps({"selected": selected, "preflight": preflight})
    assert str(tmp_path) not in public_json
    assert "project_path" not in public_json

    applied = api.proj_lifecycle_apply(preflight["operation_token"])

    assert applied["ok"] and applied["project"]["project_id"].startswith("project-")
    assert applied["project"]["project_id"] != f"project-{'a' * 32}"
    assert "project_uuid" not in applied["project"]
    assert applied["jobs_ledger_updated"] and applied["jobs_updated"] == 1
    assert "path" not in applied and "project_path" not in applied
    assert api.proj_lifecycle_apply(preflight["operation_token"])["error_code"] == (
        "operation_token_expired"
    )


def test_clone_bridge_requires_canonical_registry_id_and_opaque_destination_selection(
        tmp_path):
    source = _write_project(tmp_path / "source", "b" * 32)
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"projects": [str(source)]}), encoding="utf-8")
    api = _api(registry, tmp_path)
    selected = api.proj_lifecycle_select("clone_destination")

    rejected = api.proj_lifecycle_preflight(
        "clone", project_id=str(source),
        selection_token=selected["selection_token"], target_name="clone",
    )
    assert not rejected["ok"] and rejected["operation_token"] is None
    assert str(tmp_path) not in json.dumps(rejected)

    preflight = api.proj_lifecycle_preflight(
        "clone", project_id=f"project-{'b' * 32}",
        selection_token=selected["selection_token"], target_name="clone",
    )
    assert preflight["ready"] and preflight["impact"]["project_uuid_reminted"]
    assert str(tmp_path) not in json.dumps(preflight)

    applied = api.proj_lifecycle_apply(preflight["operation_token"])
    assert applied["ok"] and (tmp_path / "clone" / "project.yaml").is_file()
    assert applied["project"]["project_id"] != f"project-{'b' * 32}"
    assert "project_uuid" not in applied["project"]


def test_move_bridge_preflight_is_dry_run_and_returns_path_redacted_stale_failure(
        tmp_path):
    source = _write_project(tmp_path / "source", "c" * 32)
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"projects": [str(source)]}), encoding="utf-8")
    api = _api(registry, tmp_path)
    selected = api.proj_lifecycle_select("move_destination")
    preflight = api.proj_lifecycle_preflight(
        "move", project_id=f"project-{'c' * 32}",
        selection_token=selected["selection_token"], target_name="moved",
    )

    assert preflight["ready"] and source.is_file() and not (tmp_path / "moved").exists()
    source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    applied = api.proj_lifecycle_apply(preflight["operation_token"])

    assert not applied["ok"] and applied["error_code"] == "preflight_stale"
    assert str(tmp_path) not in json.dumps(applied)
    assert source.is_file() and not (tmp_path / "moved").exists()


def test_move_bridge_marks_recreated_source_rollback_for_manual_recovery(
        tmp_path, monkeypatch):
    source = _write_project(tmp_path / "source", "e" * 32)
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"projects": [str(source)]}), encoding="utf-8")
    api = _api(registry, tmp_path)
    selected = api.proj_lifecycle_select("move_destination")
    preflight = api.proj_lifecycle_preflight(
        "move", project_id=f"project-{'e' * 32}",
        selection_token=selected["selection_token"], target_name="moved",
    )

    def recreate_source_then_fail(_payload, _entries):
        source.parent.mkdir()
        raise OSError("registry unavailable at a private local path")

    monkeypatch.setattr(
        api._project_lifecycle_service, "_write_registry", recreate_source_then_fail
    )

    applied = api.proj_lifecycle_apply(preflight["operation_token"])

    assert not applied["ok"]
    assert applied["error_code"] == "partial_rollback"
    assert applied["requires_manual_recovery"] is True
    assert str(tmp_path) not in json.dumps(applied)
    assert (tmp_path / "moved" / "project.yaml").is_file()
    assert api._project_lifecycle_service.journal_path.is_file()


def test_duplicate_canonical_project_id_is_rejected_before_lifecycle_service(tmp_path):
    first = _write_project(tmp_path / "first", "d" * 32, "first")
    second = _write_project(tmp_path / "second", "d" * 32, "second")
    registry = tmp_path / "projects.json"
    registry.write_text(
        json.dumps({"projects": [str(first), str(second)]}), encoding="utf-8"
    )
    api = _api(registry, tmp_path)
    selected = api.proj_lifecycle_select("move_destination")

    preflight = api.proj_lifecycle_preflight(
        "move", project_id=f"project-{'d' * 32}",
        selection_token=selected["selection_token"], target_name="moved",
    )

    assert not preflight["ok"] and preflight["operation_token"] is None
    assert "ambiguous" in preflight["error"].lower()
    assert first.is_file() and second.is_file()


def test_lifecycle_opaque_token_stores_are_bounded_and_evict_oldest(tmp_path):
    registry = tmp_path / "projects.json"
    registry.write_text('{"projects": []}', encoding="utf-8")
    api = _api(registry, tmp_path)
    now = time.monotonic()
    for index in range(70):
        api._project_lifecycle_selections[f"selection-{index}"] = {
            "created_at": now + index / 1000,
        }
        api._project_lifecycle_plans[f"plan-{index}"] = {
            "created_at": now + index / 1000,
        }

    api._project_lifecycle_prune()

    assert len(api._project_lifecycle_selections) == 64
    assert len(api._project_lifecycle_plans) == 64
    assert "selection-0" not in api._project_lifecycle_selections
    assert "selection-69" in api._project_lifecycle_selections
    assert "plan-0" not in api._project_lifecycle_plans
    assert "plan-69" in api._project_lifecycle_plans
