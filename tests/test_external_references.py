from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from vcstudio.gui_web.api import Api
from vcstudio.project.external_references import (
    AdapterResult,
    BoundProjectEntity,
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
        secrets_store=secrets or FakeSecrets({
            "materials_project": "a" * 32,
            "catalysis_hub": "c" * 32,
        }),
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
    assert enabled["schema"] == "vcstudio.external-reference-network/v1"
    assert enabled["ok"] is True
    assert enabled["network_enabled"] is True
    assert enabled["network_state"] == "enabled"
    assert enabled["draining_requests"] == 0
    assert enabled["persisted"] is False
    assert enabled["scope"] == "current_process"


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


def test_transport_sets_stable_application_user_agent():
    captured = []

    class Response:
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            return b"{}"

    def open_request(request, **_kwargs):
        captured.append(request)
        return Response()

    transport = BoundedHttpTransport(
        opener=SimpleNamespace(open=open_request), timeout_seconds=2)
    response = transport.request(OutboundRequest(
        "GET", "https://example.com/v1/structures",
        {"User-Agent": "browser-controlled-value"}))

    assert response.status == 200
    assert captured[0].get_header("User-agent").startswith(
        "VASP-Catalyst-Studio/4 ExternalReferenceGateway/1")
    assert "browser-controlled-value" not in captured[0].get_header("User-agent")


def test_network_disable_closes_epoch_before_new_requests_and_drains_inflight(
        tmp_path):
    started = threading.Event()
    release = threading.Event()

    class BlockingTransport(FakeTransport):
        def request(self, outbound):
            self.requests.append(outbound)
            started.set()
            assert release.wait(2)
            return _response(_optimade_payload())

    transport = BlockingTransport()
    gateway = _gateway(tmp_path, transport)
    outcome = {}

    worker = threading.Thread(
        target=lambda: outcome.setdefault(
            "first", gateway.search("optimade", {"formula": "Si"})))
    worker.start()
    assert started.wait(1)

    disabled = gateway.set_network_enabled(False)
    second = gateway.search("optimade", {"formula": "Si"})
    draining_catalog = gateway.catalog()
    release.set()
    worker.join(timeout=2)

    assert disabled["network_state"] == "draining"
    assert disabled["draining_requests"] == 1
    assert draining_catalog["network_state"] == "draining"
    assert second["error"]["code"] == "network_disabled"
    assert outcome["first"]["error"]["code"] == "network_session_changed"
    assert len(transport.requests) == 1
    assert not list((tmp_path / "cache").glob("external-*.*"))


def test_materials_project_fixed_fields_keyring_and_cache_provenance(tmp_path):
    api_key = "b" * 32
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
    provider_bytes = json.dumps(
        _mp_payload(), ensure_ascii=False).encode("utf-8")
    assert raw != provider_bytes
    assert manifest["raw_response_sha256"] == hashlib.sha256(
        provider_bytes).hexdigest()
    assert manifest["cached_response_sha256"] == hashlib.sha256(raw).hexdigest()
    assert manifest["cache_representation"] == "credential-scrubbed-json"
    assert b"server-secret" not in raw and b"private" not in raw
    assert manifest["query"] == {"formula": "Si", "page": 1, "limit": 5}
    assert manifest["provider_version"] == "2025.09.25"
    assert manifest["endpoint_identity"] == "materials-project:materials-summary"
    assert manifest["retrieved_at"].endswith("Z")
    assert manifest["license"]["id"] == "provider-terms"
    assert manifest["attribution"].startswith("Materials Project")
    assert manifest["method_metadata"][0]["status"] == "provider_reported"
    assert api_key not in _encoded(manifest)


def test_provider_credential_echo_is_not_cached_or_returned(tmp_path):
    api_key = "d" * 32
    payload = _mp_payload()
    payload["debug_echo"] = api_key
    gateway = _gateway(
        tmp_path, FakeTransport(_response(payload)),
        secrets=FakeSecrets({"materials_project": api_key}))

    result = gateway.search("materials_project", {"formula": "Si"})

    assert result["error"]["code"] == "credential_echo_detected"
    assert api_key not in _encoded(result)
    assert not list((tmp_path / "cache").glob("external-*.*"))


@pytest.mark.parametrize("echo_location", ["escaped_json", "response_header"])
def test_credential_echo_detection_decodes_json_and_checks_headers(
        tmp_path, echo_location):
    api_key = "b" * 32
    payload = _mp_payload()
    headers = {}
    if echo_location == "escaped_json":
        raw = json.dumps(payload, ensure_ascii=True).encode("utf-8")[:-1]
        escaped = "".join(f"\\u{ord(character):04x}" for character in api_key)
        response = _response(raw + f',"echo":"{escaped}"}}'.encode("ascii"))
    else:
        headers["x-api-version"] = api_key
        response = _response(payload, headers=headers)
    gateway = _gateway(
        tmp_path, FakeTransport(response),
        secrets=FakeSecrets({"materials_project": api_key}))

    result = gateway.search("materials_project", {"formula": "Si"})

    assert result["error"]["code"] == "credential_echo_detected"
    assert api_key not in _encoded(result)
    assert not list((tmp_path / "cache").glob("external-*.*"))


def test_credential_echo_in_adapter_dto_or_manifest_is_rejected(tmp_path):
    api_key = "b" * 32
    gateway = _gateway(
        tmp_path, FakeTransport(_response(_mp_payload())),
        secrets=FakeSecrets({"materials_project": api_key}))
    normal = gateway._adapters["materials-project-rest"]

    class LeakingAdapter:
        def build_request(self, spec, query, key):
            return normal.build_request(spec, query, key)

        def parse_response(self, _spec, _query, _response):
            return AdapterResult(
                items=({"item_id": "materials_project:fixture",
                        "title": api_key, "citation": {"dois": []},
                        "method": {}},),
                provider_version="fixture", total_count=1,
                more_available=False,
                response_metadata={"version": api_key})

    gateway._adapters["materials-project-rest"] = LeakingAdapter()

    result = gateway.search("materials_project", {"formula": "Si"})

    assert result["error"]["code"] == "credential_echo_detected"
    assert api_key not in _encoded(result)
    assert not list((tmp_path / "cache").glob("external-*.*"))


@pytest.mark.parametrize(("provider", "bad_key"), [
    ("materials_project", "g" * 32),
    ("materials_project", "a" * 31),
    ("catalysis_hub", "with spaces" * 4),
    ("catalysis_hub", "x" * 129),
])
def test_provider_api_keys_have_strict_provider_specific_shapes(
        tmp_path, provider, bad_key):
    transport = FakeTransport()
    gateway = _gateway(
        tmp_path, transport, secrets=FakeSecrets({provider: bad_key}))

    stored = gateway.store_api_key(provider, bad_key)
    searched = gateway.search(
        provider, {"formula": "Si"} if provider == "materials_project"
        else {"reactants": "COstar"})

    assert stored["error"]["code"] == "invalid_credential"
    assert searched["error"]["code"] == "credential_required"
    assert transport.requests == []


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
        secrets = FakeSecrets({"materials_project": "a" * 32})
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
    provider = next(item for item in gateway.catalog()["providers"]
                    if item["id"] == "catalysis_hub")
    assert provider["requires_api_key"] is True
    assert provider["credential_available"] is True
    outbound = transport.requests[0]
    body = json.loads(outbound.body.decode("utf-8"))
    assert outbound.headers["X-API-Key"] == "c" * 32
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

    missing_key = _gateway(
        tmp_path / "missing", FakeTransport(),
        secrets=FakeSecrets({"materials_project": "a" * 32}))
    unavailable = missing_key.search("catalysis_hub", {"reactants": "COstar"})
    assert unavailable["error"]["code"] == "credential_required"


@pytest.mark.parametrize("mutation", ["total_type", "page_info", "publication_type"])
def test_catalysis_hub_authenticated_response_schema_is_strict(
        tmp_path, mutation):
    payload = _catalysis_payload()
    connection = payload["data"]["reactions"]
    if mutation == "total_type":
        connection["totalCount"] = "1"
        expected = "schema_drift"
    elif mutation == "page_info":
        connection["pageInfo"]["hasNextPage"] = 0
        expected = "schema_drift"
    else:
        connection["edges"][0]["node"]["publication"]["authors"] = ["unsafe"]
        expected = "partial_data"
    gateway = _gateway(tmp_path, FakeTransport(_response(payload)))

    result = gateway.search("catalysis_hub", {"reactants": "COstar"})

    assert result["status"] == "unavailable"
    assert result["error"]["code"] == expected
    assert result["items"] == []


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
    with BoundProjectEntity.open(_project_manifest(project)) as entity:
        prepared = gateway.prepare_candidate_import(
            token, item_id, project_id="project-" + "a" * 32,
            project_entity_sha256=entity.identity_sha256)
    imported = _import_prepared(gateway, project, prepared["preview_token"])
    replayed = _import_prepared(gateway, project, prepared["preview_token"])

    assert prepared["status"] == "preview"
    assert prepared["formula"] == "Si"
    assert prepared["site_count"] == 1
    assert len(prepared["content_sha256"]) == 64
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


def _project_manifest(project: Path) -> Path:
    project.mkdir(parents=True, exist_ok=True)
    manifest = project / "project.yaml"
    if not manifest.exists():
        manifest.write_text("schema: vcstudio.project/v1\n", encoding="utf-8")
    return manifest


def _prepared_import(gateway, project: Path, *,
                     project_id="project-" + "a" * 32):
    search = gateway.search("materials_project", {"formula": "Si"})
    with BoundProjectEntity.open(_project_manifest(project)) as entity:
        preview = gateway.prepare_candidate_import(
            search["result_token"], search["items"][0]["item_id"],
            project_id=project_id,
            project_entity_sha256=entity.identity_sha256)
    return search, preview


def _import_prepared(gateway, project: Path, preview_token: str, *,
                     project_id="project-" + "a" * 32):
    with BoundProjectEntity.open(_project_manifest(project)) as entity:
        return gateway.import_candidate(
            preview_token, project_entity=entity,
            project_id=project_id, confirmed=True)


def test_import_preflight_rejects_candidate_directory_file_without_consuming_token(
        tmp_path):
    gateway = _gateway(tmp_path / "gateway", FakeTransport(_response(_mp_payload())))
    project = tmp_path / "project"
    _search, preview = _prepared_import(gateway, project)
    state = project / ".vcstudio"
    state.mkdir(parents=True)
    blocked = state / "external_reference_candidates"
    blocked.write_text("not-a-directory", encoding="utf-8")

    failed = _import_prepared(gateway, project, preview["preview_token"])
    blocked.unlink()
    retried = _import_prepared(gateway, project, preview["preview_token"])

    assert failed["error"]["code"] == "candidate_import_failed"
    assert retried["ok"] is True
    assert len(list(
        (state / "external_reference_candidates").glob("*.json"))) == 1


def test_import_manifest_same_content_replacement_is_rejected_without_consuming_token(
        tmp_path):
    gateway = _gateway(tmp_path / "gateway", FakeTransport(_response(_mp_payload())))
    project = tmp_path / "project"
    _search, preview = _prepared_import(gateway, project)
    manifest = project / "project.yaml"
    original = project / "project.original.yaml"
    manifest.replace(original)
    manifest.write_bytes(original.read_bytes())

    failed = _import_prepared(gateway, project, preview["preview_token"])
    manifest.unlink()
    original.replace(manifest)
    retried = _import_prepared(gateway, project, preview["preview_token"])

    assert failed["error"]["code"] == "identity_mismatch"
    assert retried["ok"] is True
    candidates = list(
        (project / ".vcstudio" / "external_reference_candidates").glob("*.json"))
    assert len(candidates) == 1


def test_candidate_import_lock_is_cross_process(tmp_path):
    from vcstudio.project.external_references import _project_import_lock

    project = tmp_path / "project"
    project.mkdir()
    ready = tmp_path / "ready"
    script = (
        "import sys,time; from pathlib import Path; "
        "from vcstudio.project.external_references import _project_import_lock; "
        "root=Path(sys.argv[1]); ready=Path(sys.argv[2]); "
        "guard=_project_import_lock(root,timeout_seconds=2); guard.__enter__(); "
        "ready.write_text('ready',encoding='utf-8'); time.sleep(0.8); "
        "guard.__exit__(None,None,None)")
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(project.resolve()), str(ready.resolve())],
        cwd=Path(__file__).resolve().parents[1])
    try:
        deadline = time.monotonic() + 2
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        with pytest.raises(ExternalReferenceError) as busy:
            with _project_import_lock(project.resolve(), timeout_seconds=0.1):
                pass
        assert busy.value.code == "candidate_import_busy"
    finally:
        process.wait(timeout=3)
    assert process.returncode == 0


def test_import_preview_token_is_bound_to_project_and_structure_content(tmp_path):
    gateway = _gateway(tmp_path / "gateway", FakeTransport(_response(_mp_payload())))
    project = tmp_path / "project"
    search, preview = _prepared_import(gateway, project)
    wrong_project = tmp_path / "wrong"
    _project_manifest(wrong_project)

    rejected = _import_prepared(
        gateway, wrong_project, preview["preview_token"],
        project_id="project-" + "b" * 32)

    assert rejected["error"]["code"] == "invalid_token"
    assert not (wrong_project / ".vcstudio").exists()
    internal = gateway._tokens.resolve(search["result_token"])
    internal.items[0]["_structure_payload"]["sites"][0]["abc"] = [0.1, 0, 0]
    changed = _import_prepared(gateway, project, preview["preview_token"])
    assert changed["error"]["code"] == "structure_changed"
    assert not (project / ".vcstudio").exists()


def test_candidate_claim_rejects_sensitive_structure_payload(tmp_path):
    payload = _mp_payload()
    payload["data"][0]["structure"]["path"] = "C:\\private\\POSCAR"
    gateway = _gateway(tmp_path, FakeTransport(_response(payload)))
    result = gateway.search("materials_project", {"formula": "Si"})

    assert result["status"] == "unavailable"
    assert result["error"]["code"] == "partial_data"


@pytest.mark.parametrize("provider", ["materials_project", "optimade"])
def test_structure_site_composition_cross_validates_provider_formula(
        tmp_path, provider):
    if provider == "materials_project":
        payload = _mp_payload()
        payload["data"][0]["formula_pretty"] = "SiO2"
        secrets = FakeSecrets({"materials_project": "a" * 32})
    else:
        payload = _optimade_payload()
        payload["data"][0]["attributes"]["chemical_formula_reduced"] = "SiO2"
        secrets = FakeSecrets()
    gateway = _gateway(
        tmp_path, FakeTransport(_response(payload)), secrets=secrets)

    result = gateway.search(provider, {"formula": "SiO2"})

    assert result["status"] == "unavailable"
    assert result["error"]["code"] == "partial_data"
    assert result["result_token"] is None


def test_structure_formula_is_reduced_from_sites_and_content_bound(tmp_path):
    payload = _mp_payload()
    second_site = json.loads(json.dumps(payload["data"][0]["structure"]["sites"][0]))
    second_site["abc"] = [0.5, 0.5, 0.5]
    second_site["xyz"] = [2.5, 2.5, 2.5]
    payload["data"][0]["structure"]["sites"].append(second_site)
    gateway = _gateway(tmp_path, FakeTransport(_response(payload)))
    project = tmp_path / "project"

    search = gateway.search("materials_project", {"formula": "Si"})
    with BoundProjectEntity.open(_project_manifest(project)) as entity:
        preview = gateway.prepare_candidate_import(
            search["result_token"], search["items"][0]["item_id"],
            project_id="project-" + "a" * 32,
            project_entity_sha256=entity.identity_sha256)

    assert search["items"][0]["formula"] == "Si"
    assert preview["formula"] == "Si"
    assert preview["site_count"] == 2
    record = gateway._import_previews.resolve(
        preview["preview_token"], project_id="project-" + "a" * 32)
    assert record.content_sha256 == preview["content_sha256"]


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


@pytest.mark.parametrize("kind", ["deep", "oversize"])
def test_cache_manifest_bounded_read_isolates_deep_and_oversize_json(
        tmp_path, kind):
    root = tmp_path / "cache"
    root.mkdir()
    entry_id = "external-" + "d" * 32
    (root / f"{entry_id}.raw").write_bytes(b"{}")
    if kind == "deep":
        payload = b'{"nested":' + b"[" * 1500 + b"0" + b"]" * 1500 + b"}"
    else:
        payload = b'{"padding":"' + b"x" * (600 * 1024) + b'"}'
    (root / f"{entry_id}.json").write_bytes(payload)

    cache = ExternalReferenceCache(root, clock=lambda: 1_700_000_000.0)

    assert cache.cleanup()["retained"] == 0
    assert not list(root.glob("external-*.*"))


def test_cache_manifest_memory_error_is_isolated_and_removed(tmp_path, monkeypatch):
    root = tmp_path / "cache"
    root.mkdir()
    entry_id = "external-" + "e" * 32
    (root / f"{entry_id}.raw").write_bytes(b"{}")
    (root / f"{entry_id}.json").write_bytes(b"{}")
    real_loads = json.loads

    def exhausted(_payload):
        raise MemoryError("fixture")

    monkeypatch.setattr(
        __import__("vcstudio.project.external_references", fromlist=["json"]).json,
        "loads", exhausted)
    ExternalReferenceCache(root, clock=lambda: 1_700_000_000.0)
    monkeypatch.setattr(
        __import__("vcstudio.project.external_references", fromlist=["json"]).json,
        "loads", real_loads)

    assert not list(root.glob("external-*.*"))


def test_cache_cleanup_rejects_raw_size_or_hash_mismatch(tmp_path):
    root = tmp_path / "cache"
    cache = ExternalReferenceCache(root, clock=lambda: 1_700_000_000.0)
    manifest = cache.write(b"original", {
        "provider": "fixture", "provider_version": "1",
        "endpoint_identity": "fixture:1", "query": {"id": 1},
        "license": {}, "attribution": "fixture", "dois": [],
        "method_metadata": [],
    })
    raw_path = root / f"{manifest['entry_id']}.raw"
    raw_path.write_bytes(b"tampered")

    cleaned = cache.cleanup()

    assert cleaned["orphan_removed"] == 1
    assert not list(root.glob("external-*.*"))


def test_cache_cleanup_parses_each_valid_manifest_once(tmp_path, monkeypatch):
    root = tmp_path / "cache"
    cache = ExternalReferenceCache(root, clock=lambda: 1_700_000_000.0)
    cache.write(b"{}", {
        "provider": "fixture", "provider_version": "1",
        "endpoint_identity": "fixture:1", "query": {"id": 1},
        "license": {}, "attribution": "fixture", "dois": [],
        "method_metadata": [],
    })
    module_json = __import__(
        "vcstudio.project.external_references", fromlist=["json"]).json
    real_loads = module_json.loads
    calls = []

    def counted(payload):
        calls.append(len(payload))
        return real_loads(payload)

    monkeypatch.setattr(module_json, "loads", counted)
    cleaned = cache.cleanup()

    assert cleaned["retained"] == 1
    assert len(calls) == 1


def test_cache_manifest_symlink_or_reparse_is_removed_without_following(tmp_path):
    root = tmp_path / "cache"
    target = tmp_path / "outside"
    root.mkdir()
    target.mkdir()
    sentinel = target / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    entry_id = "external-" + "f" * 32
    link = root / f"{entry_id}.json"
    raw = root / f"{entry_id}.raw"
    raw.write_bytes(b"{}")
    try:
        if os.name == "nt":
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                check=False, capture_output=True, text=True)
            if created.returncode != 0:
                pytest.skip("Windows junction creation is unavailable")
        else:
            link.symlink_to(target, target_is_directory=True)
        ExternalReferenceCache(root, clock=lambda: 1_700_000_000.0)
        assert sentinel.read_text(encoding="utf-8") == "keep"
        assert not link.exists()
        assert not raw.exists()
    finally:
        try:
            link.rmdir() if link.is_dir() else link.unlink(missing_ok=True)
        except OSError:
            pass


def test_operation_failures_keep_operation_specific_schemas(tmp_path):
    gateway = _gateway(tmp_path, FakeTransport())

    assert gateway.compare("bad", ["x"])["schema"].endswith("comparison/v1")
    with BoundProjectEntity.open(_project_manifest(tmp_path)) as entity:
        assert gateway.import_candidate(
            "bad", project_entity=entity, project_id="project-" + "a" * 32,
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
    assert list(inspect.signature(
        Api.external_reference_import_preview).parameters) == [
            "self", "project_id", "result_token", "item_id"]
    assert list(inspect.signature(Api.external_reference_import).parameters) == [
        "self", "project_id", "preview_token", "confirmation"]

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
            "name": (
                f"local {root / 'private' / 'OUTCAR'} "
                "ssh://alice:hunter2@example.invalid/private"
            ),
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
    preview = api.external_reference_import_preview(
        project_id, search["result_token"], search["items"][0]["item_id"])
    imported = api.external_reference_import(
        project_id, preview["preview_token"],
        {"confirmed": True, "scope": "candidate_provenance"})

    assert compared["ok"] is True
    assert compared["aggregate"] is None
    assert compared["local_items"][0]["value"] == -1.2
    assert str(root) not in _encoded(compared)
    assert "local-secret" not in _encoded(compared)
    assert "hunter2" not in _encoded(compared)
    assert rejected["ok"] is False
    assert str(root) not in _encoded(rejected)
    assert preview["ok"] is True
    assert str(root) not in _encoded(preview)
    assert imported["ok"] is True
    assert str(root) not in _encoded(imported)


def test_external_and_structure_lazy_singletons_are_thread_safe_and_local_fallback_works(
        tmp_path, monkeypatch):
    import vcstudio.project.external_references as external_module

    created = []
    sentinel = object()

    def build_gateway():
        created.append(object())
        time.sleep(0.02)
        return sentinel

    monkeypatch.setattr(external_module, "ExternalReferenceGateway", build_gateway)
    api = Api()
    with ThreadPoolExecutor(max_workers=8) as pool:
        gateways = list(pool.map(lambda _index: api._external_references(), range(8)))
    assert gateways == [sentinel] * 8 and len(created) == 1

    sessions = []
    structure_sentinel = object()

    def build_session(*, gateway):
        sessions.append(gateway)
        time.sleep(0.02)
        return structure_sentinel

    api = Api(
        structure_sources_mod=SimpleNamespace(StructureSourceSession=build_session),
        external_structure_gateway=object())
    with ThreadPoolExecutor(max_workers=8) as pool:
        resolved = list(pool.map(lambda _index: api._structure_sources(), range(8)))
    assert resolved == [structure_sentinel] * 8 and len(sessions) == 1

    local_only = Api(external_reference_gateway=object()).structure_source_capabilities()
    assert local_only["ok"] is True
    assert [item["provider"] for item in local_only["providers"]] == ["local"]


@pytest.mark.skipif(os.name != "nt", reason="Windows file-ID regression")
def test_api_import_windows_bound_root_blocks_same_uuid_rename_swap(
        tmp_path, monkeypatch):
    root = tmp_path / "project-a"
    replacement = tmp_path / "project-b"
    holding = tmp_path / "project-hold"
    locator = _project_manifest(root)
    _project_manifest(replacement).write_bytes(locator.read_bytes())
    project = {
        "name": "fixture", "project_uuid": "a" * 32,
        "members": {"clean_slab": None, "gas_ref": None, "configs": []},
    }
    adsorption = SimpleNamespace(
        list_projects=lambda: [str(locator)],
        load_project=lambda path: project if Path(path) == locator else None,
    )
    gateway = _gateway(
        tmp_path / "gateway", FakeTransport(_response(_mp_payload())))
    api = Api(
        adsorption_mod=adsorption,
        manifest_mod=SimpleNamespace(load_manifest=lambda _path: None),
        external_reference_gateway=gateway)
    project_id = api._workspace_project_id(str(locator), project)
    search = api.external_reference_search("materials_project", {"formula": "Si"})
    preview = api.external_reference_import_preview(
        project_id, search["result_token"], search["items"][0]["item_id"])
    original_stage = BoundProjectEntity.stage_payload
    stage_ready = threading.Event()
    attack_done = threading.Event()
    attack = {"attempted": False, "swapped": False, "error": None}

    def paused_stage(self, name, payload):
        original_stage(self, name, payload)
        stage_ready.set()
        assert attack_done.wait(2)

    def rename_swap():
        assert stage_ready.wait(2)
        attack["attempted"] = True
        try:
            root.replace(holding)
            replacement.replace(root)
            attack["swapped"] = True
        except OSError as exc:
            attack["error"] = exc
        finally:
            attack_done.set()

    monkeypatch.setattr(BoundProjectEntity, "stage_payload", paused_stage)
    attacker = threading.Thread(target=rename_swap)
    attacker.start()

    imported = api.external_reference_import(
        project_id, preview["preview_token"],
        {"confirmed": True, "scope": "candidate_provenance"})
    attacker.join(timeout=3)

    assert attack["attempted"] is True
    assert attack["swapped"] is False
    assert isinstance(attack["error"], OSError)
    assert imported["ok"] is True
    assert len(list((
        root / ".vcstudio" / "external_reference_candidates").glob("*.json"))) == 1
    assert not (replacement / ".vcstudio").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-relative regression")
def test_posix_bound_root_detects_rename_swap_and_rolls_back(tmp_path, monkeypatch):
    gateway = _gateway(
        tmp_path / "gateway", FakeTransport(_response(_mp_payload())))
    root = tmp_path / "project-a"
    replacement = tmp_path / "project-b"
    holding = tmp_path / "project-hold"
    _project_manifest(root)
    _project_manifest(replacement).write_bytes(
        (root / "project.yaml").read_bytes())
    _search, preview = _prepared_import(gateway, root)
    original_stage = BoundProjectEntity.stage_payload

    def swap_after_stage(self, name, payload):
        original_stage(self, name, payload)
        root.replace(holding)
        replacement.replace(root)

    monkeypatch.setattr(BoundProjectEntity, "stage_payload", swap_after_stage)
    try:
        with BoundProjectEntity.open(root / "project.yaml") as entity:
            result = gateway.import_candidate(
                preview["preview_token"], project_entity=entity,
                project_id="project-" + "a" * 32, confirmed=True)
        assert result["error"]["code"] == "identity_mismatch"
        assert not (root / ".vcstudio").exists()
        assert not (holding / ".vcstudio").exists()
    finally:
        if root.exists() and holding.exists():
            root.replace(replacement)
            holding.replace(root)


@pytest.mark.skipif(os.name == "nt", reason="POSIX no-follow regression")
def test_posix_project_root_symlink_is_rejected(tmp_path):
    target = tmp_path / "project"
    _project_manifest(target)
    link = tmp_path / "project-link"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(ExternalReferenceError) as rejected:
        BoundProjectEntity.open(link / "project.yaml")

    assert rejected.value.code == "project_unavailable"


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point regression")
def test_windows_project_root_junction_is_rejected(tmp_path):
    target = tmp_path / "project"
    _project_manifest(target)
    junction = tmp_path / "project-junction"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        check=False, capture_output=True, text=True)
    if created.returncode != 0:
        pytest.skip("Windows junction creation is unavailable")
    try:
        with pytest.raises(ExternalReferenceError) as rejected:
            BoundProjectEntity.open(junction / "project.yaml")
        assert rejected.value.code == "project_unavailable"
    finally:
        junction.rmdir()


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
        "token", "source_id", "formula", "raw_structure_sha256",
        "canonical_structure_sha256", "poscar",
    }
    assert preview["token"] == results[0]["token"]
    assert preview["source_id"] == results[0]["source_id"]
    assert preview["raw_structure_sha256"] == hashlib.sha256(
        preview["poscar"].encode("utf-8")).hexdigest()
    from vcstudio.generate.structure_sources import parse_structure_content
    assert preview["canonical_structure_sha256"] == parse_structure_content(
        preview["poscar"], "poscar").structure_sha256
    assert "\nSi\n1\nDirect\n" in preview["poscar"]
    assert str(tmp_path) not in _encoded(results + [preview])
    assert "cache" not in _encoded(results + [preview]).lower()


def test_api_auto_wires_external_gateway_through_structure_preview_and_confirm(tmp_path):
    gateway = _gateway(tmp_path, FakeTransport(_response(_mp_payload())))
    api = Api(external_reference_gateway=gateway)

    capabilities = api.structure_source_capabilities()
    searched = api.structure_source_search("materials_project", "mp-149")
    source = searched["results"][0]
    previewed = api.structure_source_preview(source["token"])
    confirmed = api.structure_source_confirm(source["token"])

    assert capabilities["ok"] is True
    assert {item["provider"] for item in capabilities["providers"]} >= {
        "local", "materials_project", "optimade",
    }
    assert searched["ok"] is True and len(searched["results"]) == 1
    assert previewed["ok"] is True
    assert previewed["preview"]["provenance"]["computed_structure_sha256"]
    assert confirmed["ok"] is True and confirmed["confirmed"] is True
    assert str(tmp_path) not in _encoded(
        [capabilities, searched, previewed, confirmed])


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


@pytest.mark.skipif(
    os.environ.get("VCSTUDIO_EXTERNAL_REFERENCE_LIVE_SMOKE") != "1",
    reason="set VCSTUDIO_EXTERNAL_REFERENCE_LIVE_SMOKE=1 for real endpoint smoke",
)
@pytest.mark.parametrize(("provider", "filters"), [
    ("materials_project", {"formula": "Si", "limit": 1}),
    ("optimade", {"formula": "Si", "limit": 1}),
])
def test_conditional_real_provider_endpoint_smoke(tmp_path, provider, filters):
    """Operational smoke only: a network failure is a failure, never a science pass."""
    from vcstudio.shared import secrets

    if provider == "materials_project":
        key = secrets.get_external_reference_api_key(provider)
        assert key is not None, (
            "live Materials Project smoke requires its API key in the system keyring")
    gateway = ExternalReferenceGateway(
        transport=BoundedHttpTransport(timeout_seconds=10),
        cache=ExternalReferenceCache(tmp_path / provider),
        secrets_store=secrets, network_enabled=True)

    result = gateway.search(provider, filters)

    assert result["ok"] is True, result.get("error")
    assert result["provider"] == provider
    assert result["items"]
