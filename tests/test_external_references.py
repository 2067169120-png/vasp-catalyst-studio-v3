from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from vcstudio.gui_web.api import Api
from vcstudio.project.external_references import (
    BoundedHttpTransport,
    CatalysisHubAdapter,
    ExternalReferenceCache,
    ExternalReferenceError,
    ExternalReferenceGateway,
    ExternalReferenceProtocol,
    OutboundRequest,
    ProviderRegistry,
    ProviderSpec,
    StructureSourceGatewayAdapter,
    TransportFailure,
    TransportResponse,
)


class FakeSecrets:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def set_external_reference_api_key(self, provider, api_key):
        self.values[str(provider)] = str(api_key)
        return True

    def get_external_reference_api_key(self, provider):
        return self.values.get(str(provider))

    def delete_external_reference_api_key(self, provider):
        self.values.pop(str(provider), None)


class FakeTransport:
    timeout_seconds = 8.0
    max_response_bytes = 2 * 1024 * 1024

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, outbound):
        self.requests.append(outbound)
        if not self.responses:
            raise AssertionError("unexpected transport call")
        return self.responses.pop(0)


def _response(payload, *, status=200, headers=None):
    body = payload if isinstance(payload, bytes) else json.dumps(
        payload, ensure_ascii=False).encode("utf-8")
    return TransportResponse(
        status=status, headers=dict(headers or {}), body=body)


def _mp_payload(material_id="mp-149"):
    return {
        "data": [{
            "material_id": material_id,
            "formula_pretty": "Si",
            "chemsys": "Si",
            "elements": ["Si"],
            "nelements": 1,
            "density": 2.33,
            "volume": 40.1,
            "energy_above_hull": 0.0,
            "formation_energy_per_atom": -0.1,
            "band_gap": 0.6,
            "is_stable": True,
            "structure": {
                "@module": "pymatgen.core.structure",
                "@class": "Structure",
                "charge": 0.0,
                "lattice": {
                    "@module": "pymatgen.core.lattice", "@class": "Lattice",
                    "matrix": [[5, 0, 0], [0, 5, 0], [0, 0, 5]],
                },
                "sites": [{
                    "species": [{"element": "Si", "occu": 1}],
                    "abc": [0, 0, 0], "xyz": [0, 0, 0], "label": "Si",
                    "properties": {},
                }],
            },
            "origins": [{"name": "structure", "task_id": "mp-149"}],
            # Unknown fields are never projected, even when hostile.
            "debug": "C:\\private\\cache token=server-secret",
        }],
        "meta": {"db_version": "2025.09.25", "total_doc": 1},
    }


def _optimade_payload():
    return {
        "data": [{
            "type": "structures",
            "id": "opt-1",
            "attributes": {
                "chemical_formula_reduced": "Si",
                "chemical_formula_hill": "Si",
                "elements": ["Si"],
                "nelements": 1,
                "nsites": 1,
                "lattice_vectors": [[5, 0, 0], [0, 5, 0], [0, 0, 5]],
                "cartesian_site_positions": [[0, 0, 0]],
                "species_at_sites": ["Si"],
                "species": [{"name": "Si", "chemical_symbols": ["Si"],
                             "concentration": [1.0]}],
                "dimension_types": [1, 1, 1],
            },
        }],
        "meta": {
            "api_version": "1.2.0",
            "data_available": 1,
            "more_data_available": False,
            "provider": {"name": "Fixture Provider", "prefix": "fx"},
        },
        "links": {"next": None},
    }


def _catalysis_payload():
    return {
        "data": {
            "reactions": {
                "totalCount": 1,
                "pageInfo": {"hasNextPage": False, "endCursor": "opaque"},
                "edges": [{
                    "node": {
                        "id": "42",
                        "chemicalComposition": "Pt-C-O",
                        "surfaceComposition": "Pt",
                        "facet": "111",
                        "reactants": "COstar+Ostar",
                        "products": "CO2gas",
                        "reactionEnergy": -0.8,
                        "activationEnergy": 0.5,
                        "dftCode": "VASP",
                        "dftFunctional": "PBE",
                        "pubId": "fixture-publication",
                        "publication": {
                            "title": "Fixture catalysis paper",
                            "authors": "A. Researcher",
                            "year": 2024,
                            "doi": "10.1234/example.42",
                            "publisher": "Fixture Press",
                        },
                    },
                }],
            },
        },
    }


def _gateway(tmp_path, transport, *, clock=lambda: 1_700_000_000.0,
             network_enabled=True, secrets=None, token_ttl_seconds=900,
             cache_ttl_seconds=3600, max_entries=32,
             token_max_records=64, token_capacity_bytes=16 * 1024 * 1024):
    cache = ExternalReferenceCache(
        tmp_path / "cache", ttl_seconds=cache_ttl_seconds,
        max_entries=max_entries, clock=clock)
    return ExternalReferenceGateway(
        transport=transport, cache=cache,
        secrets_store=secrets or FakeSecrets({"materials_project": "k" * 32}),
        network_enabled=network_enabled, token_ttl_seconds=token_ttl_seconds,
        token_max_records=token_max_records,
        token_capacity_bytes=token_capacity_bytes,
        clock=clock)


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def test_gateway_implements_narrow_protocol_and_defaults_to_offline(tmp_path):
    gateway = _gateway(tmp_path, FakeTransport(), network_enabled=False)

    assert isinstance(gateway, ExternalReferenceProtocol)
    catalog = gateway.catalog()
    assert catalog["network_enabled"] is False
    assert catalog["network_default"] == "disabled"
    assert {item["id"] for item in catalog["providers"]} == {
        "materials_project", "optimade", "catalysis_hub"}
    assert all(item["status"] == "network_disabled"
               for item in catalog["providers"])
    assert "api.materialsproject.org" not in _encoded(catalog)
    unavailable = gateway.search("optimade", {"formula": "Si"})
    assert unavailable["status"] == "unavailable"
    assert unavailable["error"]["code"] == "network_disabled"

    denied = gateway.set_network_enabled(True, "wrong-confirmation")
    enabled = gateway.set_network_enabled(
        True, "enable-external-reference-network")
    assert denied["error"]["code"] == "network_confirmation_required"
    assert enabled == {
        "schema": "vcstudio.external-reference-network/v1",
        "ok": True, "network_enabled": True,
        "persisted": False, "scope": "current_process",
    }


@pytest.mark.parametrize("bad_filters", [
    {"url": "https://example.com"},
    {"endpoint": "https://example.com"},
    {"path": "C:\\private\\raw.json"},
    {"graphql": "{ systems { Cifdata } }"},
    {"fields": ["*", "secret"]},
    {"timeout": 999},
])
def test_browser_query_rejects_url_graphql_path_fields_and_limits(
        tmp_path, bad_filters):
    transport = FakeTransport()
    gateway = _gateway(tmp_path, transport)

    result = gateway.search("optimade", bad_filters)

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_query"
    assert transport.requests == []
    assert "C:\\private" not in _encoded(result)


def test_provider_registry_and_transport_reject_ssrf_endpoints():
    base = ProviderRegistry().get("optimade")
    for endpoint in (
            "http://example.com/v1/structures",
            "https://127.0.0.1/v1/structures",
            "https://[::1]/v1/structures",
            "https://localhost/v1/structures",
            "https://user:pass@example.com/v1/structures",
            "https://example.com/v1/../private"):
        spec = ProviderSpec(**{**base.__dict__, "endpoint": endpoint})
        with pytest.raises(ValueError):
            ProviderRegistry([spec])

    transport = BoundedHttpTransport(opener=SimpleNamespace())
    with pytest.raises(ValueError):
        transport.request(SimpleNamespace(
            method="GET", url="https://127.0.0.1/private",
            headers={}, body=None))


def test_bounded_transport_rejects_oversized_response_without_partial_parse():
    class Response:
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size):
            return b"x" * size

    opener = SimpleNamespace(open=lambda *_args, **_kwargs: Response())
    transport = BoundedHttpTransport(
        opener=opener, timeout_seconds=100, max_response_bytes=1024)

    with pytest.raises(TransportFailure) as oversized:
        transport.request(OutboundRequest(
            "GET", "https://example.com/v1/structures"))

    assert oversized.value.code == "response_too_large"
    assert transport.timeout_seconds == 10.0


def test_bounded_transport_enforces_wall_clock_deadline():
    class Response:
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            time.sleep(1.25)
            return b"{}"

    opener = SimpleNamespace(open=lambda *_args, **_kwargs: Response())
    transport = BoundedHttpTransport(opener=opener, timeout_seconds=1)
    started = time.monotonic()

    with pytest.raises(TransportFailure) as timed_out:
        transport.request(OutboundRequest(
            "GET", "https://example.com/v1/structures"))

    assert timed_out.value.code == "request_timeout"
    assert time.monotonic() - started < 1.2


def test_materials_project_fixed_fields_keyring_and_cache_provenance(tmp_path):
    api_key = "mp-key-" + "x" * 32
    secrets = FakeSecrets({"materials_project": api_key})
    transport = FakeTransport(_response(_mp_payload()))
    gateway = _gateway(tmp_path, transport, secrets=secrets)

    result = gateway.search("materials_project", {
        "formula": "Si", "page": 1, "limit": 5,
    })

    assert result["ok"] is True
    assert result["result_token"].startswith("external-reference.")
    assert "cache" not in _encoded(result).lower()
    assert "C:\\private" not in _encoded(result)
    assert "server-secret" not in _encoded(result)
    assert api_key not in _encoded(result)
    request = transport.requests[0]
    params = parse_qs(urlsplit(request.url).query)
    assert request.headers["X-API-KEY"] == api_key
    assert api_key not in request.url
    assert params["_limit"] == ["5"]
    assert params["_fields"][0].split(",") == list(
        __import__("vcstudio.project.external_references", fromlist=[
            "MaterialsProjectAdapter"]).MaterialsProjectAdapter._FIELDS)

    manifests = list((tmp_path / "cache").glob("external-*.json"))
    raw_files = list((tmp_path / "cache").glob("external-*.raw"))
    assert len(manifests) == len(raw_files) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    raw = raw_files[0].read_bytes()
    assert manifest["raw_response_sha256"] == hashlib.sha256(raw).hexdigest()
    assert manifest["query"] == {"formula": "Si", "page": 1, "limit": 5}
    assert manifest["provider_version"] == "2025.09.25"
    assert manifest["endpoint_identity"] == "materials-project:materials-summary"
    assert manifest["retrieved_at"].endswith("Z")
    assert manifest["license"]["id"] == "provider-terms"
    assert manifest["attribution"].startswith("Materials Project")
    assert manifest["method_metadata"][0]["status"] == "provider_reported"
    assert api_key not in _encoded(manifest)


def test_provider_credential_echo_is_not_cached_or_returned(tmp_path):
    api_key = "mp-key-" + "z" * 32
    payload = _mp_payload()
    payload["debug_echo"] = api_key
    gateway = _gateway(
        tmp_path, FakeTransport(_response(payload)),
        secrets=FakeSecrets({"materials_project": api_key}))

    result = gateway.search("materials_project", {"formula": "Si"})

    assert result["error"]["code"] == "credential_echo_detected"
    assert api_key not in _encoded(result)
    assert not list((tmp_path / "cache").glob("external-*.*"))


def test_optimade_filter_is_generated_and_response_fields_are_fixed(tmp_path):
    transport = FakeTransport(_response(_optimade_payload()))
    gateway = _gateway(tmp_path, transport)

    result = gateway.search("optimade", {
        "formula": "Si", "elements": ["Si"],
        "nelements_min": 1, "nelements_max": 2, "limit": 3,
    })

    assert result["ok"] is True
    request = transport.requests[0]
    params = parse_qs(urlsplit(request.url).query)
    assert params["filter"] == [
        'chemical_formula_reduced = "Si" AND elements HAS "Si" '
        'AND nelements >= 1 AND nelements <= 2']
    assert params["page_limit"] == ["3"]
    assert params["page_offset"] == ["0"]
    assert "response_fields" in params
    item = result["items"][0]
    assert item["structure_available"] is True
    assert item["evidence_policy"]["validation_result_eligible"] is False

    rejected = gateway.search("optimade", {
        "formula": 'Si" OR elements HAS "U', "limit": 3})
    assert rejected["error"]["code"] == "invalid_query"
    assert len(transport.requests) == 1


@pytest.mark.parametrize(("provider", "filters"), [
    ("materials_project", {"formula": True}),
    ("materials_project", {"formula": 123}),
    ("materials_project", {"formula": "Si", "page": 1.5}),
    ("materials_project", {"formula": "Si", "limit": "2"}),
    ("optimade", {"elements": ["Si", 8]}),
    ("catalysis_hub", {"reactants": 123}),
])
def test_query_contract_rejects_implicit_type_coercion(tmp_path, provider, filters):
    transport = FakeTransport()
    result = _gateway(tmp_path, transport).search(provider, filters)

    assert result["error"]["code"] == "invalid_query"
    assert transport.requests == []


@pytest.mark.parametrize("provider", ["materials_project", "optimade"])
def test_provider_cannot_exceed_requested_page_cardinality(tmp_path, provider):
    if provider == "materials_project":
        payload = _mp_payload("mp-1")
        second = json.loads(json.dumps(payload["data"][0]))
        second["material_id"] = "mp-2"
        payload["data"].append(second)
        secrets = FakeSecrets({"materials_project": "k" * 32})
        filters = {"formula": "Si", "limit": 1}
    else:
        payload = _optimade_payload()
        second = json.loads(json.dumps(payload["data"][0]))
        second["id"] = "opt-2"
        payload["data"].append(second)
        secrets = FakeSecrets()
        filters = {"formula": "Si", "limit": 1}
    gateway = _gateway(
        tmp_path, FakeTransport(_response(payload)), secrets=secrets)

    result = gateway.search(provider, filters)

    assert result["status"] == "unavailable"
    assert result["error"]["code"] == "partial_data"
    assert result["items"] == []


def test_catalysis_hub_uses_fixed_graphql_variables_and_preserves_attribution(tmp_path):
    transport = FakeTransport(_response(_catalysis_payload()))
    gateway = _gateway(tmp_path, transport)

    result = gateway.search("catalysis_hub", {
        "reactants": "COstar+Ostar", "products": "CO2gas", "limit": 4,
    })

    assert result["ok"] is True
    outbound = transport.requests[0]
    body = json.loads(outbound.body.decode("utf-8"))
    assert body["query"] == CatalysisHubAdapter._QUERY
    assert "COstar+Ostar" not in body["query"]
    assert body["variables"]["reactants"] == "COstar+Ostar"
    item = result["items"][0]
    assert item["publication"]["doi"] == "10.1234/example.42"
    assert item["citation"]["dois"] == ["10.1234/example.42"]
    assert item["license"]["id"] == "CC-BY-4.0"
    assert item["method"]["status"] == "provider_reported"
    assert item["properties"][0]["aggregate_eligible"] is False

    injection = gateway.search("catalysis_hub", {
        "reactants": 'CO") { systems { Cifdata } } #', "limit": 4})
    assert injection["ok"] is False
    assert injection["error"]["code"] == "invalid_query"
    assert len(transport.requests) == 1


@pytest.mark.parametrize(("payload", "code"), [
    ({"unexpected": []}, "schema_drift"),
    ({"data": [{"material_id": "mp-1"}], "meta": {}}, "partial_data"),
])
def test_schema_drift_and_partial_data_are_structured_unavailable_and_cached(
        tmp_path, payload, code):
    transport = FakeTransport(_response(payload))
    gateway = _gateway(tmp_path, transport)

    result = gateway.search("materials_project", {"formula": "Si"})

    assert result["ok"] is False
    assert result["status"] == "unavailable"
    assert result["error"]["code"] == code
    assert result["result_token"] is None
    manifest_path = next((tmp_path / "cache").glob("external-*.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "unavailable"
    assert manifest["error_code"] == code


def test_network_failure_and_rate_limit_do_not_break_local_gateway_state(tmp_path):
    transport = FakeTransport(_response({}, status=429), _response(_optimade_payload()))
    gateway = _gateway(tmp_path, transport)

    limited = gateway.search("optimade", {"formula": "Si"})
    recovered = gateway.search("optimade", {"formula": "Si"})

    assert limited["status"] == "unavailable"
    assert limited["error"] == {
        "code": "rate_limited",
        "message": "external provider rate limit reached",
        "retryable": True,
    }
    assert recovered["ok"] is True


def test_structure_claim_requires_confirmation_and_token_is_single_use(tmp_path):
    transport = FakeTransport(_response(_mp_payload()))
    gateway = _gateway(tmp_path, transport)
    result = gateway.search("materials_project", {"formula": "Si"})
    token = result["result_token"]
    item_id = result["items"][0]["item_id"]

    with pytest.raises(ExternalReferenceError) as not_confirmed:
        gateway.claim_structure_candidate(token, item_id, confirmed=False)
    assert not_confirmed.value.code == "confirmation_required"

    project = tmp_path / "project"
    project.mkdir()
    imported = gateway.import_candidate(
        token, item_id, project_root=project,
        project_id="project-" + "a" * 32, confirmed=True)
    replayed = gateway.import_candidate(
        token, item_id, project_root=project,
        project_id="project-" + "a" * 32, confirmed=True)

    assert imported["ok"] is True
    assert imported["status"] == "candidate_only"
    assert "path" not in _encoded(imported).lower()
    candidate_files = list(
        (project / ".vcstudio" / "external_reference_candidates").glob("*.json"))
    assert len(candidate_files) == 1
    candidate = json.loads(candidate_files[0].read_text(encoding="utf-8"))
    assert candidate["structure"]["format"] == "materials-project-structure-json"
    assert candidate["provenance"]["raw_response_sha256"]
    assert candidate["evidence_policy"] == {
        "role": "external_reference",
        "method_compatibility": "not_assessed",
        "aggregate_eligible": False,
        "validation_result_eligible": False,
        "final_claim_eligible": False,
        "promotion_status": "candidate_only",
    }
    assert replayed["error"]["code"] == "token_replayed"


def test_candidate_claim_rejects_sensitive_structure_payload(tmp_path):
    payload = _mp_payload()
    payload["data"][0]["structure"]["path"] = "C:\\private\\POSCAR"
    gateway = _gateway(tmp_path, FakeTransport(_response(payload)))
    result = gateway.search("materials_project", {"formula": "Si"})

    assert result["status"] == "unavailable"
    assert result["error"]["code"] == "partial_data"


def test_result_token_expiry_and_cache_ttl_capacity_cleanup(tmp_path):
    now = [1_700_000_000.0]

    def clock():
        return now[0]
    transport = FakeTransport(_response(_mp_payload("mp-1")))
    gateway = _gateway(
        tmp_path, transport, clock=clock,
        token_ttl_seconds=30, cache_ttl_seconds=60, max_entries=1)
    result = gateway.search("materials_project", {"material_ids": ["mp-1"]})
    now[0] += 31

    expired = gateway.compare(result["result_token"], [result["items"][0]["item_id"]])
    assert expired["error"]["code"] == "expired_token"

    cache = gateway.cache
    cache.write(b"{}", {
        "provider": "fixture", "provider_version": "1",
        "endpoint_identity": "fixture:1", "query": {"id": 2},
        "license": {}, "attribution": "fixture", "dois": [],
        "method_metadata": [],
    })
    assert len(list((tmp_path / "cache").glob("external-*.json"))) == 1
    now[0] += 61
    cleaned = cache.cleanup()
    assert cleaned["expired_removed"] == 1
    assert not list((tmp_path / "cache").glob("external-*.*"))


def test_result_token_store_evicts_oldest_at_record_capacity(tmp_path):
    transport = FakeTransport(
        _response(_mp_payload("mp-1")), _response(_mp_payload("mp-2")))
    gateway = _gateway(
        tmp_path, transport, token_max_records=1,
        token_capacity_bytes=2 * 1024 * 1024)
    first = gateway.search("materials_project", {"material_ids": ["mp-1"]})
    second = gateway.search("materials_project", {"material_ids": ["mp-2"]})

    evicted = gateway.compare(
        first["result_token"], [first["items"][0]["item_id"]])
    retained = gateway.compare(
        second["result_token"], [second["items"][0]["item_id"]])

    assert evicted["error"]["code"] == "invalid_token"
    assert retained["ok"] is True


def test_cache_startup_removes_orphan_and_malformed_entries(tmp_path):
    root = tmp_path / "cache"
    root.mkdir()
    orphan_id = "external-" + "a" * 32
    malformed_id = "external-" + "b" * 32
    (root / f"{orphan_id}.raw").write_bytes(b"orphan")
    (root / f"{malformed_id}.raw").write_bytes(b"raw")
    (root / f"{malformed_id}.json").write_text("{broken", encoding="utf-8")

    ExternalReferenceCache(root, clock=lambda: 1_700_000_000.0)

    assert not list(root.glob("external-*.*"))


def test_operation_failures_keep_operation_specific_schemas(tmp_path):
    gateway = _gateway(tmp_path, FakeTransport())

    assert gateway.compare("bad", ["x"])["schema"].endswith("comparison/v1")
    assert gateway.import_candidate(
        "bad", "x", project_root=tmp_path, project_id="project-x",
        confirmed=True)["schema"].endswith("candidate/v1")
    assert gateway.store_api_key(
        "optimade", "x" * 32)["schema"].endswith("credential/v1")


def test_optimade_typed_fields_and_structure_shape_fail_closed(tmp_path):
    payload = _optimade_payload()
    payload["data"][0]["attributes"]["nsites"] = "C:\\private\\job.yaml"
    gateway = _gateway(tmp_path, FakeTransport(_response(payload)))

    leaked = gateway.search("optimade", {"formula": "Si"})

    assert leaked["status"] == "unavailable"
    assert leaked["error"]["code"] == "partial_data"
    assert "C:\\private" not in _encoded(leaked)

    malformed = _optimade_payload()
    malformed["data"][0]["attributes"]["lattice_vectors"] = []
    malformed_gateway = _gateway(
        tmp_path / "malformed", FakeTransport(_response(malformed)))
    result = malformed_gateway.search("optimade", {"formula": "Si"})
    assert result["error"]["code"] == "partial_data"


def test_compare_is_side_by_side_only_and_never_builds_external_aggregate(tmp_path):
    transport = FakeTransport(_response(_catalysis_payload()))
    gateway = _gateway(tmp_path, transport)
    search = gateway.search("catalysis_hub", {"reactants": "COstar"})

    compared = gateway.compare(
        search["result_token"], [search["items"][0]["item_id"]])

    assert compared["status"] == "side_by_side_only"
    assert compared["aggregate"] is None
    assert compared["evidence_policy"]["aggregate_eligible"] is False
    assert compared["evidence_policy"]["validation_result_eligible"] is False
    assert compared["evidence_policy"]["final_claim_eligible"] is False


def test_api_bridge_is_narrow_opaque_and_project_bound(tmp_path):
    assert list(inspect.signature(Api.external_reference_search).parameters) == [
        "self", "provider", "filters"]

    root = tmp_path / "project"
    root.mkdir()
    locator = str(root / "project.yaml")
    Path(locator).write_text("schema: vcstudio.project/v1\n", encoding="utf-8")
    project = {
        "name": "fixture", "project_uuid": "a" * 32, "root": str(root),
        "members": {"clean_slab": None, "gas_ref": None, "configs": []},
    }
    summary = {
        "rows": [{
            "name": f"local {root / 'private' / 'OUTCAR'}",
            "species": "CO token=local-secret", "state": "DONE",
            "delta_e": -1.2, "reference_valid": True,
            "method_check": {"status": "verified"},
            "source_job": str(root / "private" / "job.yaml"),
        }],
    }
    adsorption = SimpleNamespace(
        list_projects=lambda: [locator],
        load_project=lambda path: project if str(Path(path).resolve()) == str(
            Path(locator).resolve()) else None,
        delta_e_rows=lambda _project: summary,
    )
    gateway = _gateway(tmp_path / "gateway", FakeTransport(_response(_mp_payload())))
    api = Api(
        adsorption_mod=adsorption,
        manifest_mod=SimpleNamespace(load_manifest=lambda _path: None),
        external_reference_gateway=gateway)
    project_id = api._workspace_project_id(locator, project)
    search = api.external_reference_search("materials_project", {"formula": "Si"})

    compared = api.external_reference_compare(
        project_id, search["result_token"], [search["items"][0]["item_id"]])
    rejected = api.external_reference_compare(
        locator, search["result_token"], [search["items"][0]["item_id"]])
    imported = api.external_reference_import(
        project_id, search["result_token"], search["items"][0]["item_id"],
        {"confirmed": True, "scope": "candidate_provenance"})

    assert compared["ok"] is True
    assert compared["aggregate"] is None
    assert compared["local_items"][0]["value"] == -1.2
    assert str(root) not in _encoded(compared)
    assert rejected["ok"] is False
    assert str(root) not in _encoded(rejected)
    assert imported["ok"] is True
    assert str(root) not in _encoded(imported)


def test_external_reference_api_key_uses_dedicated_keyring_namespace(monkeypatch):
    from vcstudio.shared import secrets

    calls = []
    backend = SimpleNamespace(
        set_password=lambda service, name, value: calls.append(
            ("set", service, name, value)),
        get_password=lambda service, name: calls.append(
            ("get", service, name)) or "fixture-key",
        delete_password=lambda service, name: calls.append(
            ("delete", service, name)),
    )
    monkeypatch.setattr(secrets, "keyring", backend)

    assert secrets.set_external_reference_api_key(
        "materials_project", "fixture-key") is True
    assert secrets.get_external_reference_api_key("materials_project") == "fixture-key"
    secrets.delete_external_reference_api_key("materials_project")

    assert calls == [
        ("set", "vcstudio-external-reference", "materials_project", "fixture-key"),
        ("get", "vcstudio-external-reference", "materials_project"),
        ("delete", "vcstudio-external-reference", "materials_project"),
    ]


def test_structure_source_adapter_matches_frozen_path_free_protocol(tmp_path):
    gateway = _gateway(tmp_path, FakeTransport(_response(_mp_payload())))
    adapter = StructureSourceGatewayAdapter(gateway)

    capabilities = adapter.capabilities()
    results = adapter.search("materials_project", "mp-149")
    preview = adapter.preview(results[0]["token"])

    assert {item["provider"] for item in capabilities} == {
        "materials_project", "optimade"}
    assert all(set(item) == {
        "provider", "label", "modes", "formats", "network", "enabled",
    } for item in capabilities)
    assert set(results[0]) == {
        "token", "source_id", "formula", "license", "citation", "method",
        "raw_structure_sha256",
    }
    assert results[0]["token"].startswith("external-structure.")
    assert results[0]["source_id"].startswith("materials_project:")
    assert results[0]["method"] == {
        "provider": "materials_project",
        "endpoint_identity": "materials-project:materials-summary",
        "provider_version": "2025.09.25",
        "format": "poscar",
        "evidence_role": "external_reference",
    }
    assert set(preview) == {
        "token", "source_id", "formula", "raw_structure_sha256", "poscar",
    }
    assert preview["token"] == results[0]["token"]
    assert preview["source_id"] == results[0]["source_id"]
    assert preview["raw_structure_sha256"] == hashlib.sha256(
        preview["poscar"].encode("utf-8")).hexdigest()
    assert "\nSi\n1\nDirect\n" in preview["poscar"]
    assert str(tmp_path) not in _encoded(results + [preview])
    assert "cache" not in _encoded(results + [preview]).lower()


def test_structure_source_adapter_converts_optimade_cartesian_structure(tmp_path):
    gateway = _gateway(tmp_path, FakeTransport(_response(_optimade_payload())))
    adapter = StructureSourceGatewayAdapter(gateway)

    result = adapter.search("optimade", "Si")[0]
    preview = adapter.preview(result["token"])

    assert result["method"]["provider"] == "optimade"
    assert result["license"]["name"] == "provider-terms"
    assert "\nSi\n1\nCartesian\n" in preview["poscar"]
    assert preview["formula"] == "Si"


@pytest.mark.parametrize("query", [
    "https://example.com/structure",
    "C:\\private\\POSCAR",
    '{ systems { Cifdata } }',
    'Si" OR elements HAS "U',
])
def test_structure_source_adapter_rejects_url_path_and_query_languages(
        tmp_path, query):
    transport = FakeTransport()
    adapter = StructureSourceGatewayAdapter(_gateway(tmp_path, transport))

    with pytest.raises(ExternalReferenceError) as blocked:
        adapter.search("materials_project", query)

    assert blocked.value.code == "invalid_query"
    assert transport.requests == []


def test_structure_source_adapter_tokens_expire_and_evict_oldest(tmp_path):
    now = [1_700_000_000.0]

    def clock():
        return now[0]

    gateway = _gateway(
        tmp_path, FakeTransport(
            _response(_mp_payload("mp-1")), _response(_mp_payload("mp-2"))),
        clock=clock)
    adapter = StructureSourceGatewayAdapter(
        gateway, ttl_seconds=30, max_records=1, clock=clock)
    first = adapter.search("materials_project", "mp-1")[0]
    second = adapter.search("materials_project", "mp-2")[0]

    with pytest.raises(ExternalReferenceError) as evicted:
        adapter.preview(first["token"])
    assert evicted.value.code == "expired_token"
    assert adapter.preview(second["token"])["source_id"] == second["source_id"]
    now[0] += 31
    with pytest.raises(ExternalReferenceError) as expired:
        adapter.preview(second["token"])
    assert expired.value.code == "expired_token"
