# Active ReactionNetwork authoring contract

`vcstudio.project.catalysis_authoring` provides the path-free bootstrap,
read-only preview and explicit confirmation core for the single active
`ReactionNetwork` binding. It does not modify domain envelopes and it never
chooses a network automatically.

## Snapshot adapter

The injected `CatalysisAuthoringSnapshotAuthority` must implement:

```python
authoring_bootstrap_snapshot() -> CatalysisAuthoringBootstrap
authoring_candidate_snapshot(network_id) -> CatalysisAuthoringCandidateSnapshot | None
```

Each call must acquire one projection-authority lock and one domain head
snapshot, then derive the binding CAS, domain CAS, network CAS and closure from
that single generation. An adapter must not enumerate IDs and then call
`network_head_cas()` or `build_snapshot()` separately for every candidate.

Bootstrap returns only opaque `NetworkHeadCAS` candidates, binding/domain CAS,
the explicit active ID/status, projection status, gap counts and closure hashes.
It excludes envelopes, payloads, formulas, composition, phase, charge, energies,
actors, credentials and filesystem locations.

The existing `CatalysisProjectionAuthority` is injected separately as the
`CatalysisBindingAuthority`; only its existing `bind_network(**cas)` mutation
seam is required. No adapter is implemented here because the current authority
does not yet expose a public single-lock candidate enumeration method.

## Stateless signed preview

The service uses a server-owned HMAC key rather than a browser-owned preview
store. The key must contain at least 256 bits and must be stable across server
processes that accept the same preview.

```python
service = CatalysisActiveNetworkAuthoringService(
    project_id=project_id,
    owner_id=server_owner_id,
    snapshot_authority=snapshot_adapter,
    binding_authority=projection_authority,
    preview_seal_key=server_secret_bytes,
)

bootstrap = service.bootstrap()
preview = service.preview(CatalysisActiveNetworkPreviewRequest.from_dict(body))
result = service.confirm(
    CatalysisActiveNetworkPreviewRequest.from_dict(body["request"]),
    CatalysisActiveNetworkConfirmation.from_dict(body["confirmation"]),
)
```

`vcstudio.catalysis-active-network-preview-request/v1` contains exactly:
`schema`, `network_id`, `intent_id`, `expected_binding`, and
`expected_network_head`. Both CAS values must be the full sealed snapshots from
bootstrap.

The preview seal binds the server-owned project/owner, request hash, target,
intent, expected CAS seals, action, current closure seal, projection status and
TTL. Preview never writes the binding and always returns `confirmed=false` and
`authorizes_execution=false`.

Confirmation repeats the original request and echoes only the request hash,
preview seal, issue/expiry times, and `confirmed=true`. The service re-reads the
current candidate closure, recomputes and verifies the HMAC, then calls
`bind_network`. The binding authority remains the final CAS authority.

The seal deliberately binds the request's base binding, rather than the latest
binding observed during confirmation. This permits an exact response-loss retry
to reach `bind_network` and return `replayed`. A different winner still returns
`stale_cas`, and reuse of an intent for another target returns `intent_reused`.

All conflicts include the latest available binding/network CAS and
`retry_automatically=false`. Browser/API code must adopt the returned CAS and
stop; it must never retry automatically.

## Test adapter construction

The focused test adapter in `tests/test_catalysis_authoring.py` demonstrates the
required production shape: under `CatalysisProjectionAuthority._locked()` it
reads `_read_binding_store()`, enters exactly one
`domain_store.locked_snapshot_heads()`, creates one in-memory head index, and
derives all candidate CAS/closures before releasing either lock. Those private
calls are test-only evidence of the protocol; production wiring should add an
equivalent public adapter later rather than importing private helpers.
