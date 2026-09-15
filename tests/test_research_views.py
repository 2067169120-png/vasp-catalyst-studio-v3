from __future__ import annotations

import json

import pytest

from vcstudio.project import research_views


def _view(view_id="surface-screen", **overrides):
    value = {
        "id": view_id,
        "name": "Surface screen",
        "filters": {
            "elements": ["Pt", "S"],
            "adsorbate": "Li2S8",
            "states": ["DONE"],
            "method_compatible": True,
            "energy_min_eV": -5.0,
        },
        "sort": {"key": "energy_eV", "direction": "asc"},
        "axes": {"x": "energy_eV", "y": "barrier_eV"},
    }
    value.update(overrides)
    return value


def test_missing_authority_is_stable_in_process_and_first_save_uses_cas(tmp_path):
    path = tmp_path / "research-views.json"
    store = research_views.ResearchViewStore(
        path, authority_id="0123456789abcdef0123456789abcdef")

    first = store.read()
    second = store.read()

    assert first == second
    assert first["authority_id"] == "0123456789abcdef0123456789abcdef"
    assert first["revision"] == 0
    assert first["views"] == []
    assert not path.exists()

    saved = store.save(
        _view(), authority_id=first["authority_id"], expected_revision=0)
    assert saved["ok"] is True
    assert saved["revision"] == 1
    assert saved["view"] == _view()
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "authority_id": first["authority_id"],
        "revision": 1,
        "schema": research_views.SCHEMA,
        "views": [_view()],
    }


def test_authority_id_and_revision_both_participate_in_compare_and_swap(tmp_path):
    store = research_views.ResearchViewStore(
        tmp_path / "views.json", authority_id="a" * 32)
    winner = store.save(
        _view("winner"), authority_id="a" * 32, expected_revision=0)

    wrong_authority = store.save(
        _view("loser-a"), authority_id="b" * 32, expected_revision=1)
    stale_revision = store.save(
        _view("loser-b"), authority_id="a" * 32, expected_revision=0)

    for result in (wrong_authority, stale_revision):
        assert result["ok"] is False
        assert result["conflict"] is True
        assert result["authority_id"] == winner["authority_id"]
        assert result["revision"] == 1
        assert [view["id"] for view in result["views"]] == ["winner"]


@pytest.mark.parametrize("view,match", [
    (_view(name=r"C:\private\view.json"), "path"),
    (_view(name="token=top-secret"), "credential"),
    (_view(filters={"notes": "arbitrary正文"}), "unknown fields"),
    (_view(filters={"project_ids": [r"C:\private\project.yaml"]}), "path"),
    (_view(filters={"energy_min_eV": float("nan")}), "finite"),
    (_view(sort={"key": "unknown", "direction": "asc"}), "unsupported"),
    (_view(axes={"x": "client_result", "y": "energy_eV"}), "unsupported"),
])
def test_saved_view_contract_rejects_paths_credentials_body_and_scientific_payloads(
        tmp_path, view, match):
    store = research_views.ResearchViewStore(
        tmp_path / "views.json", authority_id="c" * 32)

    with pytest.raises(research_views.ResearchViewError, match=match):
        store.save(view, authority_id="c" * 32, expected_revision=0)

    assert not store.path.exists()


def test_delete_is_cas_guarded_and_corrupt_authority_fails_closed(tmp_path):
    path = tmp_path / "views.json"
    store = research_views.ResearchViewStore(path, authority_id="d" * 32)
    saved = store.save(_view(), authority_id="d" * 32, expected_revision=0)
    stale = store.delete(
        "surface-screen", authority_id="d" * 32, expected_revision=0)
    assert stale["conflict"] is True
    deleted = store.delete(
        "surface-screen", authority_id="d" * 32,
        expected_revision=saved["revision"])
    assert deleted["ok"] is True
    assert deleted["views"] == []

    path.write_text('{"schema":', encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(research_views.ResearchViewError, match="cannot read"):
        store.read()
    with pytest.raises(research_views.ResearchViewError, match="cannot read"):
        store.save(_view(), authority_id="d" * 32, expected_revision=2)
    assert path.read_bytes() == before
