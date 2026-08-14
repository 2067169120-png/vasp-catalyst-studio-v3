from __future__ import annotations

from vcstudio.gui_web.api import Api


def test_catalog_and_preview_api_are_read_only_and_path_free():
    api = Api()
    catalog = api.research_recipe_catalog()
    assert catalog["ok"] is True
    assert catalog["recipe_count"] == 5
    assert catalog["read_only"] is True
    assert catalog["authorizes_execution"] is False

    result = api.research_recipe_preview(
        "adsorption_energy", "1.0.0",
        {"overrides": {"reference_stoichiometry": 0.5}, "evidence": {}},
    )
    assert result["ok"] is True
    assert result["preview"]["status"] == "blocked"
    assert result["preview"]["parameter_sources"]["reference_stoichiometry"] == (
        "user_override"
    )
    assert result["preview"]["authorizes_execution"] is False
    assert "operation_token" not in str(result)


def test_api_rejects_nested_browser_paths_and_secrets_without_echo():
    api = Api()
    requests = (
        {"evidence": {"initial_state": {
            "ref_type": "structure_record", "opaque_id": "evidence-1",
            "origin": "observed", "revision_id": None,
            "nested": {"project_path": r"C:\\private\\job.yaml"},
        }}},
        {"overrides": {"password": "needle-secret"}},
    )
    for request in requests:
        result = api.research_recipe_preview("neb_path", "1.0.0", request)
        assert result["ok"] is False
        assert result["preview"] is None
        assert result["error_code"] == "research_recipe_preview_invalid"
        assert r"C:\\private" not in str(result)
        assert "needle-secret" not in str(result)


class _FakeRecipes:
    def catalog(self):
        raise RuntimeError(r"password=hunter2 C:\\private\\catalog.json")

    def preview(self, *_args):
        raise RuntimeError(r"token=needle /srv/private/job")


def test_api_failures_are_constant_and_recursively_safe():
    api = Api(research_recipes_mod=_FakeRecipes())
    catalog = api.research_recipe_catalog()
    preview = api.research_recipe_preview("recipe", "1.0.0", {})
    assert catalog["error_code"] == "research_recipe_catalog_unavailable"
    assert preview["error_code"] == "research_recipe_preview_invalid"
    rendered = str([catalog, preview])
    for secret in ("hunter2", "needle", "private\\catalog", "/srv/private"):
        assert secret not in rendered


class _LeakyRecipes:
    def catalog(self):
        return {
            "schema": "vcstudio.research-recipe-catalog/v1",
            "recipes": [{
                "recipe_id": "safe-recipe",
                "github_api_key": "needle-key",
                "note": "see(/srv/private/catalog.json)",
            }],
            "recipe_count": 1,
        }

    def preview(self, *_args):
        return {
            "schema": "vcstudio.workflow-preview/v1",
            "recipe_id": "safe-recipe",
            "nested": {"ssh_private_key": "needle-private"},
            "note": r"see(C:\\private\\job.yaml)",
        }


def test_success_payloads_are_recursively_redacted_not_just_failures():
    api = Api(research_recipes_mod=_LeakyRecipes())
    catalog = api.research_recipe_catalog()
    preview = api.research_recipe_preview("safe-recipe", "1.0.0", {})
    assert catalog["ok"] is True and preview["ok"] is True
    rendered = str([catalog, preview])
    for secret in ("needle-key", "needle-private", "/srv/private", r"C:\\private"):
        assert secret not in rendered
    assert "redacted-sensitive" in rendered
