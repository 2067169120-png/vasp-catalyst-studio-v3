# Research Notebook + Human Review Ledger contract

## Purpose

Research Notebook is a project-local, local-first record of research reasoning and
human review activity. It covers:

- notes categorized as problem, hypothesis, observation, interpretation,
  limitation, or next step;
- explicit decisions;
- local human-review records with reviewer role, decision, requested changes,
  and optional typed signature attribution;
- opaque links to a project, job, frozen report source, or report revision; and
- project-local attachment bytes with public metadata and hashes.

It is not an authentication service, electronic-signature service, multi-user
server, report validator, or replacement for the campaign `accepted` gate.

## Storage and append contract

The project journal is stored under:

```text
<project-root>/.vcstudio/research-notebook/ledger.jsonl
<project-root>/.vcstudio/research-notebook/attachments/<sha256>.bin
```

Each JSONL record contains a monotonically increasing `revision`,
`previous_digest`, and `record_digest`. The digest is SHA-256 over canonical JSON
excluding only `record_digest`. Writers:

1. acquire a stable project-scoped OS file lock;
2. read and verify the complete existing chain;
3. compare the caller's `expected_revision` with the current revision;
4. write attachment bytes to content-addressed project storage when present;
5. append one complete UTF-8 JSON line with one `O_APPEND` write; and
6. `fsync` the journal descriptor before returning.

A malformed line, partial final line, revision gap, project mismatch, previous
digest mismatch, or record digest mismatch makes the journal `tampered` and
blocks further writes. The reader does not skip a damaged row and pretend that
the remaining history is current.

An edit appends a new record with `supersedes=<active record id>`. It does not
rewrite the earlier body. A deletion appends a tombstone with
`tombstones=<active record id>`. Historical records remain in the journal and
the public DTO marks active versus superseded/tombstoned records explicitly.

## Actor and review boundary

A normal local entry is stamped by the server as:

```text
actor_type=human
entry_method=local-explicit
identity_assurance=self-asserted-local
```

An AI proposal, when used, is stamped as:

```text
actor_type=ai
entry_method=ai-proposal
identity_assurance=declared-software-agent
```

The caller cannot submit `actor_type`, `reviewer_type`, `made_by`, assurance, or
signature-verification fields. A human-review record requires a separate local
human attestation and cannot be created as an AI proposal. These controls prevent
the application from silently relabeling its own proposal as human review, but
they are not authentication: a local person supplies their own actor ID, display
name, role, and optional typed attribution.

`signature_attribution` is descriptive text only. Every public review record
states `cryptographic_signature=false`. The MVP does not claim legal or
cryptographic identity.

## Evidence links and read-time revalidation

The journal stores only opaque IDs and the evidence digest observed when a link
was created. The browser never supplies an authority-bearing local path.

| Link | Binding and revalidation |
|---|---|
| project | Registered opaque project identity and current server identity fingerprint |
| job | Opaque job ID and canonical current `job.yaml` digest |
| report revision | Authoritative history revision reloaded through the frozen revision validator; manifest digest |
| source | Source ID plus exact report revision ID; frozen source record and, when available, current source-file hash |

Each read resolves the link again. A matching digest is `current`, a different
digest is `stale`, and an unavailable/ambiguous/tampered target is `missing`.
The route returned to the browser is a semantic workspace route, never a
filesystem locator.

## Browser and Resume Center boundary

The Project and Publish surfaces render note/review bodies with DOM `textContent`.
The MVP does not render Markdown or inject record fields with `innerHTML`.

Unsaved body text and attachment bytes remain in the current form/server-side
selection and are never written into workspace `localStorage`. Resume Center may
store only this marker:

```json
{
  "schema": "vcstudio.safe-draft-ref/v1",
  "kind": "research-notebook",
  "project_id": "<opaque project id>"
}
```

The marker can return the user to the owning project surface. It cannot restore
an unsaved body, and it does not become a project fact.

## Report and archive boundary

Publish can display notebook records and copy a stable material reference of the
form `notebook:<record_id>@<record-digest-prefix>`. This allows a writer to cite
or discuss notebook material without turning it into an automatically supported
scientific claim.

The existing report path remains authoritative:

```text
ReportSpec → ReportSnapshot → ValidationResult → ReportModel → revision/manifest
```

Research Notebook never constructs, mutates, or upgrades `ValidationResult`;
never changes `claims`, `final_allowed`, `scientific_qualification`, or campaign
`accepted`; and never converts an approved notebook review into
`human_scientific_reviewed`.

SI capsule export adds:

```text
research-notebook/ledger.json
research-notebook/limitations.json
```

The ledger snapshot binds its head digest, journal revision, integrity status,
and selected report revision. It is recursively redacted. Shared capsules include
attachment names, sizes, media types, and hashes, but not project-local attachment
bytes. The limitations member always states the self-attributed actor, no
cryptographic signature, unchanged report gate, and local attachment boundaries.

## Acceptance limits

Passing unit, concurrency, Fake DOM, injection, redaction, lint, or packaging
checks proves only the software contract exercised by those checks. It does not
authenticate a reviewer, prove that a scientific review actually occurred,
validate a scientific claim, exercise a real multi-user workflow, or establish
release readiness.
