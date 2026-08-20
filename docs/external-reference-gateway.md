# External Reference Gateway boundary

The External Reference Gateway is a read-only reference-data sidecar.  It is
not a second project database, a calculation engine, or a publication
authority.

## Stable core protocol

Internal consumers such as a Structure Source Hub depend only on
`vcstudio.project.external_references.ExternalReferenceProtocol`:

- `catalog()` returns provider capabilities and policy metadata;
- `search(provider_id, filters)` accepts one registered provider ID and that
  provider's bounded filter object;
- `claim_structure_candidate(result_token, item_id, confirmed=True)` consumes
  one opaque token and returns an `ExternalStructureCandidate` for an explicit
  candidate-only import.

The browser import bridge is deliberately two-step.  It first returns a
short-lived `external-import-preview.*` token bound server-side to the opaque
project ID, result token, item ID, canonical site-derived formula and canonical
structure-content SHA-256.  Only that preview token can be confirmed for a
filesystem commit; the browser cannot substitute any of the bound identities.

`StructureSourceGatewayAdapter` implements Structure Source Hub's separately
frozen structural Protocol without importing or editing that module:

- `capabilities()` returns exact path-free capability DTOs for Materials
  Project and OPTIMADE;
- `search(provider, query)` accepts only a formula, comma/hyphen-separated
  element set, or Materials Project ID and returns one opaque token per result;
- `preview(token)` returns the exact identity fields plus a deterministic POSCAR.

Ordered, fully occupied MP MSON and OPTIMADE structures are converted directly.
Disorder, partial occupancy, ambiguous species, non-finite or mismatched
lattice/site arrays, URL/path/query-language text, expired tokens and capacity
eviction all fail closed.  Structure Source Hub remains the owner of its later
preview/confirm/consume transaction; this adapter owns only remote retrieval,
attribution and the opaque preview handoff.

The protocol does not accept URLs, filesystem paths, arbitrary response fields,
headers, GraphQL documents, timeouts, or cache destinations.  Endpoint
selection, request fields, pagination, timeout, response byte limit, cache and
credentials are server-owned.

## Providers and network policy

The built-in registry contains narrow adapters for:

- Materials Project `/materials/summary/`;
- a server-pinned generic OPTIMADE `/v1/structures` endpoint;
- Catalysis-Hub's reactions GraphQL endpoint using one fixed query document and
  validated variables.

Networking is disabled when the process starts.  Enabling it requires the
exact session confirmation `enable-external-reference-network`; the setting is
not persisted.  Each outbound operation registers an epoch lease while the
network-state lock is held.  Disabling advances the epoch before returning,
rejects every new lease, reports any existing operations as `draining`, and
prevents stale operations from caching or issuing tokens.  Redirects are
rejected, only credential-free HTTPS endpoints
can enter the server registry, response bodies are limited to 2 MiB, page size
is limited to 25, page number is limited to 4, and the timeout is capped at 10
seconds.  The caller-facing request has a wall-clock deadline; at most two
timed-out transport workers may remain in flight, so a slow-trickle provider
cannot block the local workflow or create unbounded request threads.  Provider
responses that exceed the requested row count, repeat an identity, or return a
malformed structure fail as `unavailable`.  The transport supplies the stable
application `User-Agent` itself.  Opt-in MP/OPTIMADE real-endpoint smoke tests
run only when `VCSTUDIO_EXTERNAL_REFERENCE_LIVE_SMOKE=1`; when enabled, network
or schema failure fails the smoke rather than being counted as a scientific
pass.

Materials Project and Catalysis-Hub API keys are independent provider entries
stored only under the dedicated
`vcstudio-external-reference` OS-keyring service.  Keys are never read from an
environment variable, YAML/JSON config, project data, result DTO, or cache
manifest.  Both adapters send `X-API-Key`; key values must satisfy their
provider-specific ASCII shape.  Credential echoes are checked in response
headers, raw bytes, every decoded JSON string, parsed DTO metadata and the
prospective cache manifest.

## Cache and result identities

The private cache stores a bounded response snapshot beside a manifest
containing the provider response SHA-256 and size, normalized query and query
hash, provider/adapter version,
endpoint identity, retrieval/expiry time, license, attribution, DOI list and
method metadata.  It has TTL, entry-count and byte-capacity cleanup.  No cache
path is returned to the browser.  For credentialed requests, provider bytes are
never stored verbatim: JSON is decoded, sensitive/path-like fields are scrubbed
and the result is canonicalized.  The manifest separately records the source
response hash and the cached-snapshot hash.

Successful searches return a process-local opaque result token.  Tokens expire
after 15 minutes by default.  A token can be used for read-only side-by-side
comparison, but can authorize at most one structure-candidate claim/import;
replay and expired tokens fail closed.  The token store is independently bound
to 64 records and 16 MiB by default and evicts oldest entries first.

Candidate persistence reserves rather than consumes the result token, performs
path/type/symlink and opaque-identity preflight, takes a cooperating
cross-process project lock, stages and fsyncs the sidecar, rechecks project
identity/content/path CAS, then atomically replaces the target.  Only a
successful commit changes the token to `committed`.  Any failure releases the
reservation, removes staged/committed artifacts and removes directories created
by the failed attempt when they are empty.

## Evidence and publication policy

Every external item and property carries this policy:

```json
{
  "role": "external_reference",
  "method_compatibility": "not_assessed",
  "aggregate_eligible": false,
  "validation_result_eligible": false,
  "final_claim_eligible": false,
  "promotion_status": "candidate_only"
}
```

Comparison is side-by-side only and returns no aggregate.  Explicit structure
import writes a separate candidate-provenance sidecar under the selected local
project; it does not add an accepted project member, create a calculation,
change a gate, construct a `ValidationResult`, or alter a report claim.

Network failure, rate limiting, schema drift and partial data return structured
`status=unavailable` results.  They are isolated from local analysis, cluster,
pipeline and report workflows.

## Reference Browser UI adapter

The bilingual `#/projects/<opaque-id>/analysis/references` page consumes only
the gateway's public bridge methods.  Its provider-specific form maps visible
controls to fixed filter names; it has no control for URLs, GraphQL text,
response fields, headers, timeouts or paths.  Results show method, property,
license, attribution and DOI metadata, and provide explicit candidate import
and side-by-side comparison actions.  Credential labels follow the selected
provider.  Import retrieves and validates the project/content-bound structure
preview before presenting confirmation, and generation checks enforce
last-wins behavior across project/provider changes.

The page uses native labelled form controls and buttons, live status/error
regions, visible keyboard focus, local table scrolling and single-column
reflow below 720 px.  Browser QA at 1280 px and 480 px confirmed no
document-level horizontal overflow.  HTTP-only browser fixtures cannot prove
the native pywebview bridge; the bridge contracts are therefore also covered
by offline Python and Node tests.

## License boundary

The repository contains protocol clients and synthetic test fixtures only.  It
does not package external database records, pretrained model weights or copied
GPL client code.  Runtime records retain provider attribution, license/citation
links, DOI metadata when supplied, retrieval time and raw-response identity.
Users remain responsible for the provider's current terms and for citing the
database version and property-specific methods used in their work.
