from __future__ import annotations

import copy
import hashlib
import json
import threading
import zipfile
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_report_service import _Host, _file_digest, _request
from vcstudio.project import reproducibility_archive as archive_mod
from vcstudio.project.report_service import ReportService
from vcstudio.project.reproducibility_archive import (
    ArchiveAttachment,
    ArchiveConfirmations,
    build_archive_plan,
    export_archive,
    verify_archive_bytes,
    verify_archive_file,
)


class _AssetHost(_Host):
    def _report_workbench_render_build(self, build, out_dir, *, stem, revision):
        result = super()._report_workbench_render_build(
            build, out_dir, stem=stem, revision=revision,
        )
        manifest_path = Path(result["manifest"])
        asset = manifest_path.parent / "assets" / "chart-asset.png"
        asset.parent.mkdir(exist_ok=True)
        asset.write_bytes(b"\x89PNG\r\n\x1a\nvcstudio-test-chart")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["assets"] = [{
            "path": "assets/chart-asset.png",
            "sha256": _file_digest(asset),
            "size": asset.stat().st_size,
            "media_type": "image/png",
        }]
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return result


def _published_revision(tmp_path: Path, *, assets: bool = True):
    host = (_AssetHost if assets else _Host)(tmp_path)
    service = ReportService(host, temp_root=tmp_path / "previews")
    preview = service.preview(
        "project.yaml", _request(host.project_id, operation_id="archive-plan"),
    )
    published = service.publish(
        "project.yaml", str(tmp_path / "reports"), preview["preview_id"],
        preview["preview_token"], public=False,
    )
    assert published["ok"] is True
    return host, service, published["revision"]["revision_id"], published


def _attachment(
    name: str,
    data: bytes,
    *,
    role: str = "research_ledger",
    license_id: str = "CC-BY-4.0",
    attribution: str = "Authoritative notebook export",
    redistributable: bool = True,
    third_party: bool = False,
) -> ArchiveAttachment:
    return ArchiveAttachment(
        archive_path=name,
        logical_role=role,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        license_id=license_id,
        attribution=attribution,
        redistributable=redistributable,
        third_party=third_party,
    )


class _Provider:
    provider_id = "test-ledger"

    def __init__(self, *attachments: ArchiveAttachment):
        self.attachments = attachments

    def frozen_attachments(self, _bundle):
        return self.attachments


def test_dry_run_lists_authoritative_members_roles_rights_risks_and_exclusions(tmp_path):
    _host, service, revision_id, _published = _published_revision(tmp_path)

    plan = build_archive_plan(service, "project.yaml", revision_id)
    public = plan.public_summary()
    decisions = public["decisions"]
    included = {row["archive_path"]: row for row in decisions if row["decision"] == "include"}

    assert public["status"] == "dry_run_ready"
    assert public["revision"]["revision_id"] == revision_id
    assert public["archive"]["version"] == 1
    assert public["denominator"]["decisions"] == len(decisions)
    assert public["denominator"]["included"] > 0
    assert public["denominator"]["excluded"] >= 7
    assert {
        "contracts/report-spec.json",
        "contracts/report-snapshot.json",
        "contracts/validation-result.json",
        "model/report-model.json",
        "artifacts/reports/report.html",
        "artifacts/figures/chart-asset.png",
        "inputs/input-manifest.json",
        "inputs/potcar-identities.json",
        "methods/methods.json",
        "environment/environment.json",
        "environment/parsers.json",
        "provenance/evidence-graph.json",
        "metadata/archive-manifest.json",
        "README.md",
        "CITATION.cff",
        "SHA256SUMS",
    }.issubset(included)
    for row in decisions:
        assert {
            "archive_path", "logical_role", "size", "sha256", "license",
            "license_status", "attribution", "sensitive_risk", "authority",
            "decision", "exclusion_reason",
        }.issubset(row)
    reasons = {row["exclusion_reason"] for row in decisions if row["decision"] == "exclude"}
    assert {
        "raw_potcar_forbidden", "secrets_forbidden", "absolute_paths_forbidden",
        "cache_not_authoritative", "temporary_files_not_authoritative",
        "mutable_live_files_forbidden", "license_or_attribution_required",
    }.issubset(reasons)
    encoded = json.dumps(public, ensure_ascii=False)
    assert str(tmp_path) not in encoded
    assert r"C:\private\project.yaml" not in encoded
    assert public["boundaries"] == {
        "local_only": True,
        "uploaded": False,
        "doi_requested": False,
        "doi_assigned": False,
        "scientific_gate_reused": True,
    }


def test_archive_is_byte_deterministic_bound_and_self_verifying(tmp_path):
    _host, service, revision_id, _published = _published_revision(tmp_path)

    first = build_archive_plan(service, "project.yaml", revision_id)
    second = build_archive_plan(service, "project.yaml", revision_id)

    assert first._archive_bytes == second._archive_bytes
    assert first.plan_sha256 == second.plan_sha256
    assert first.archive_sha256 == second.archive_sha256
    assert revision_id in first.archive_name
    assert first.source_manifest_sha256[:12] in first.archive_name
    assert first.plan_sha256[:12] in first.archive_name
    checked = verify_archive_bytes(first._archive_bytes, expected_sha256=first.archive_sha256)
    assert checked["ok"] is True
    assert checked["checksums"] == "pass"
    with zipfile.ZipFile(BytesIO(first._archive_bytes)) as archive:
        infos = archive.infolist()
        assert [info.filename for info in infos] == sorted(info.filename for info in infos)
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in infos)
        assert all(info.compress_type == zipfile.ZIP_STORED for info in infos)
        assert all((info.external_attr >> 16) & 0o170000 == 0o100000 for info in infos)
        manifest = json.loads(archive.read("metadata/archive-manifest.json"))
        binding = manifest["source_binding"]
        assert binding["revision_id"] == revision_id
        assert binding["source_manifest_sha256"] == first.source_manifest_sha256
        assert manifest["boundaries"]["uploaded"] is False
        assert manifest["boundaries"]["doi_requested"] is False
        sums = archive.read("SHA256SUMS").decode("ascii").splitlines()
        assert len(sums) == len(infos) - 1
        assert str(tmp_path).encode() not in first._archive_bytes


@pytest.mark.parametrize("unsafe", [
    "../escape.txt", "safe/../../escape.txt", r"C:\private\x.txt",
    r"\\server\share\x.txt", "/etc/passwd", "with\\backslash.txt",
    "CON.txt", "name\x00.txt",
])
def test_provider_zip_slip_and_nonportable_names_are_excluded(tmp_path, unsafe):
    _host, service, revision_id, _published = _published_revision(tmp_path, assets=False)
    provider = _Provider(_attachment(unsafe, b"safe payload"))

    plan = build_archive_plan(
        service, "project.yaml", revision_id, attachment_providers=[provider],
    )

    rejected = [
        row for row in plan.decisions
        if row.get("provider_id") == provider.provider_id and row["decision"] == "exclude"
    ]
    assert rejected
    assert rejected[0]["exclusion_reason"] in {
        "attachment_contract_invalid", "unsafe_archive_path",
    }
    with zipfile.ZipFile(BytesIO(plan._archive_bytes)) as archive:
        assert unsafe not in archive.namelist()


def test_provider_secret_path_potcar_and_missing_third_party_license_fail_closed(tmp_path):
    _host, service, revision_id, _published = _published_revision(tmp_path, assets=False)
    provider = _Provider(
        _attachment("attachments/safe-ledger.json", b'{"rows":1}'),
        _attachment("attachments/secret.txt", b"Bearer top-secret-token-value"),
        _attachment("attachments/path.txt", rb"C:\private\ledger.json"),
        _attachment("attachments/POTCAR", b"parameters from PSCTR are:\nEnd of Dataset"),
        _attachment(
            "attachments/unlicensed.csv", b"third party rows",
            license_id="NOASSERTION", attribution="", redistributable=False,
            third_party=True,
        ),
    )

    plan = build_archive_plan(
        service, "project.yaml", revision_id, attachment_providers=[provider],
    )
    provider_rows = [row for row in plan.decisions if row.get("provider_id") == "test-ledger"]
    by_path = {row.get("archive_path"): row for row in provider_rows}

    assert by_path["attachments/safe-ledger.json"]["decision"] == "include"
    assert by_path["attachments/secret.txt"]["exclusion_reason"] == "secret_literal"
    assert by_path["attachments/path.txt"]["exclusion_reason"] == "absolute_path"
    assert set(by_path["attachments/POTCAR"]["exclusion_reason"].split(";")) == {
        "potcar_raw_content", "potcar_raw_filename",
    }
    assert by_path["attachments/unlicensed.csv"]["exclusion_reason"] == (
        "third_party_license_or_attribution_missing"
    )
    for forbidden in (
        b"top-secret-token-value", rb"C:\private\ledger.json",
        b"parameters from PSCTR are:", b"End of Dataset", b"third party rows",
    ):
        assert forbidden not in plan._archive_bytes


def test_potcar_projection_contains_only_element_irreversible_identity_and_notice(
    tmp_path, monkeypatch,
):
    _host, service, revision_id, _published = _published_revision(tmp_path, assets=False)
    frozen = archive_mod.load_frozen_revision(service, "project.yaml", revision_id)
    snapshot = copy.deepcopy(frozen.snapshot)
    snapshot.setdefault("payload", {})["potcar_provenance"] = [{
        "element": "Fe",
        "variant": "Fe_pv",
        "titel": "PAW_PBE Fe_pv 06Sep2000",
        "potcar_content": "parameters from PSCTR are:\nEnd of Dataset",
    }]
    augmented = replace(frozen, snapshot=snapshot)
    monkeypatch.setattr(archive_mod, "load_frozen_revision", lambda *_args, **_kwargs: augmented)
    monkeypatch.setattr(archive_mod, "_recapture_frozen_revision", lambda bundle: bundle)
    monkeypatch.setattr(archive_mod, "evidence_graph", lambda *_args, **_kwargs: {
        "schema": "vcstudio.report-evidence-graph/v1", "ok": True,
        "status": "ready", "nodes": [], "edges": [], "missing_links": [],
    })

    plan = build_archive_plan(service, "project.yaml", revision_id)
    with zipfile.ZipFile(BytesIO(plan._archive_bytes)) as archive:
        records = json.loads(archive.read("inputs/potcar-identities.json"))["records"]

    assert records and records[0]["element"] == "Fe"
    assert len(records[0]["identity_sha256"]) == 64
    assert set(records[0]) == {"element", "identity_sha256", "license_notice"}
    assert b"Fe_pv" not in plan._archive_bytes
    assert b"PAW_PBE Fe_pv" not in plan._archive_bytes
    assert b"parameters from PSCTR are:" not in plan._archive_bytes


def test_path_backed_provider_rejects_symlink(tmp_path):
    _host, service, revision_id, _published = _published_revision(tmp_path, assets=False)
    source = tmp_path / "ledger.json"
    source.write_bytes(b'{"rows":1}')
    link = tmp_path / "ledger-link.json"
    try:
        link.symlink_to(source)
    except (OSError, NotImplementedError):
        pytest.skip("filesystem symbolic links are unavailable")
    attachment = replace(
        _attachment("attachments/ledger.json", source.read_bytes()),
        data=None,
        source_path=link,
    )

    plan = build_archive_plan(
        service, "project.yaml", revision_id,
        attachment_providers=[_Provider(attachment)],
    )

    row = next(item for item in plan.decisions if item.get("provider_id") == "test-ledger")
    assert row["decision"] == "exclude"
    assert row["exclusion_reason"] == "symbolic_link_or_toctou_rejected"


def test_bound_file_reader_detects_change_during_open_file_capture(tmp_path, monkeypatch):
    source = tmp_path / "bound.bin"
    payload = b"bound bytes"
    source.write_bytes(payload)
    real_fstat = archive_mod.os.fstat
    calls = []

    def changed_fstat(descriptor):
        current = real_fstat(descriptor)
        calls.append(current)
        if len(calls) == 2:
            return SimpleNamespace(
                st_dev=current.st_dev,
                st_ino=current.st_ino,
                st_mode=current.st_mode,
                st_size=current.st_size,
                st_mtime_ns=current.st_mtime_ns + 1,
            )
        return current

    monkeypatch.setattr(archive_mod.os, "fstat", changed_fstat)

    with pytest.raises(RuntimeError, match="changed during read"):
        archive_mod._safe_read_bound_file(
            source, expected_sha256=hashlib.sha256(payload).hexdigest(),
            expected_size=len(payload),
        )


def test_stale_report_after_dry_run_cannot_export_and_writes_no_archive(tmp_path):
    _host, service, revision_id, published = _published_revision(tmp_path, assets=False)
    plan = build_archive_plan(service, "project.yaml", revision_id)
    Path(published["files"]["html"]).write_text("tampered", encoding="utf-8")
    destination = tmp_path / "archives"
    destination.mkdir()

    with pytest.raises(RuntimeError, match="changed|hash|audit"):
        export_archive(
            service, "project.yaml", revision_id, destination,
            expected_plan_sha256=plan.plan_sha256,
        )

    assert not list(destination.glob("*.zip"))
    assert not list(destination.glob("*.tmp-*"))


def test_export_is_atomic_verified_and_never_overwrites(tmp_path):
    _host, service, revision_id, _published = _published_revision(tmp_path)
    plan = build_archive_plan(service, "project.yaml", revision_id)
    destination = tmp_path / "archives"
    destination.mkdir()

    first = export_archive(
        service, "project.yaml", revision_id, destination,
        expected_plan_sha256=plan.plan_sha256,
    )
    target = destination / first["file"]["name"]
    original = target.read_bytes()

    assert first["ok"] is True
    assert first["status"] == "verified_local_archive"
    assert first["verification"]["ok"] is True
    assert first["boundaries"]["published"] is False
    assert first["boundaries"]["uploaded"] is False
    assert first["boundaries"]["doi_requested"] is False
    assert verify_archive_file(target, expected_sha256=plan.archive_sha256)["ok"] is True
    with pytest.raises(FileExistsError):
        export_archive(
            service, "project.yaml", revision_id, destination,
            expected_plan_sha256=plan.plan_sha256,
        )
    assert target.read_bytes() == original
    assert not list(destination.glob("*.tmp-*"))


def test_concurrent_export_has_one_winner_and_no_partial_final_file(tmp_path):
    _host, service, revision_id, _published = _published_revision(tmp_path, assets=False)
    plan = build_archive_plan(service, "project.yaml", revision_id)
    destination = tmp_path / "archives"
    destination.mkdir()
    barrier = threading.Barrier(2)
    results = []

    def worker():
        barrier.wait()
        try:
            result = export_archive(
                service, "project.yaml", revision_id, destination,
                expected_plan_sha256=plan.plan_sha256,
            )
            results.append(("ok", result))
        except Exception as exc:  # noqa: BLE001 - deliberate concurrency outcome capture
            results.append(("error", exc))

    threads = [threading.Thread(target=worker) for _index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 2
    assert sum(kind == "ok" for kind, _value in results) == 1
    error = next(value for kind, value in results if kind == "error")
    assert isinstance(error, FileExistsError)
    archives = list(destination.glob("*.zip"))
    assert len(archives) == 1
    assert verify_archive_file(archives[0], expected_sha256=plan.archive_sha256)["ok"] is True
    assert not list(destination.glob("*.tmp-*"))


def test_publish_failure_leaves_no_final_or_temporary_archive(tmp_path, monkeypatch):
    _host, service, revision_id, _published = _published_revision(tmp_path, assets=False)
    plan = build_archive_plan(service, "project.yaml", revision_id)
    destination = tmp_path / "archives"
    destination.mkdir()
    monkeypatch.setattr(archive_mod.os, "link", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        OSError("simulated atomic publish failure")
    ))

    with pytest.raises(OSError, match="atomic publish failure"):
        export_archive(
            service, "project.yaml", revision_id, destination,
            expected_plan_sha256=plan.plan_sha256,
        )

    assert not list(destination.glob("*.zip"))
    assert not list(destination.glob("*.tmp-*"))


def test_failed_rollback_writes_path_free_recovery_record(tmp_path, monkeypatch):
    _host, service, revision_id, _published = _published_revision(tmp_path, assets=False)
    plan = build_archive_plan(service, "project.yaml", revision_id)
    destination = tmp_path / "archives"
    destination.mkdir()
    real_verify = archive_mod.verify_archive_file
    verification_calls = []

    def fail_final_verify(path, *, expected_sha256=None):
        verification_calls.append(Path(path).name)
        if len(verification_calls) == 1:
            return real_verify(path, expected_sha256=expected_sha256)
        return {"ok": False, "error": "simulated final verification failure"}

    real_unlink = Path.unlink

    def fail_final_unlink(self, *args, **kwargs):
        if self.name == plan.archive_name:
            raise OSError("simulated cleanup failure")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(archive_mod, "verify_archive_file", fail_final_verify)
    monkeypatch.setattr(Path, "unlink", fail_final_unlink)

    with pytest.raises(RuntimeError, match="final verification failure"):
        export_archive(
            service, "project.yaml", revision_id, destination,
            expected_plan_sha256=plan.plan_sha256,
        )

    recovery_files = list(destination.glob("*.recovery.json"))
    assert len(recovery_files) == 1
    recovery = json.loads(recovery_files[0].read_text(encoding="utf-8"))
    encoded = json.dumps(recovery, ensure_ascii=False)
    assert recovery["archive_name"] == plan.archive_name
    assert recovery["schema"] == "vcstudio.vcs-archive-recovery/v1"
    assert str(tmp_path) not in encoded


def test_symlink_destination_is_rejected_before_any_archive_write(tmp_path):
    _host, service, revision_id, _published = _published_revision(tmp_path, assets=False)
    plan = build_archive_plan(service, "project.yaml", revision_id)
    real_destination = tmp_path / "real-archives"
    real_destination.mkdir()
    linked_destination = tmp_path / "linked-archives"
    try:
        linked_destination.symlink_to(real_destination, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("filesystem directory symbolic links are unavailable")

    with pytest.raises(ValueError, match="non-symlink directory"):
        export_archive(
            service, "project.yaml", revision_id, linked_destination,
            expected_plan_sha256=plan.plan_sha256,
        )

    assert not list(real_destination.iterdir())


def test_archive_verifier_detects_tamper_and_extra_members(tmp_path):
    _host, service, revision_id, _published = _published_revision(tmp_path, assets=False)
    plan = build_archive_plan(service, "project.yaml", revision_id)
    tampered = BytesIO()
    with zipfile.ZipFile(BytesIO(plan._archive_bytes)) as source, zipfile.ZipFile(
        tampered, "w", compression=zipfile.ZIP_STORED,
    ) as target:
        for info in source.infolist():
            target.writestr(info, source.read(info.filename))
        extra = zipfile.ZipInfo("unexpected.txt", date_time=(1980, 1, 1, 0, 0, 0))
        extra.compress_type = zipfile.ZIP_STORED
        extra.create_system = 3
        extra.external_attr = 0o100644 << 16
        target.writestr(extra, b"not declared")

    result = verify_archive_bytes(tampered.getvalue())

    assert result["ok"] is False
    assert result["status"] == "tampered_or_invalid"
    assert result["checksums"] == "fail"


def test_confirmation_tokens_expire_are_single_use_and_hold_no_public_path(tmp_path):
    now = [100.0]
    confirmations = ArchiveConfirmations(
        clock=lambda: now[0], ttl_seconds=10, max_items=2,
    )
    token = confirmations.register({"private_destination": str(tmp_path)})

    record = confirmations.consume(token)

    assert record["private_destination"] == str(tmp_path)
    with pytest.raises(ValueError, match="invalid, expired, or already used"):
        confirmations.consume(token)
    expired = confirmations.register({"private_destination": str(tmp_path)})
    now[0] = 111.0
    with pytest.raises(ValueError, match="invalid, expired, or already used") as caught:
        confirmations.consume(expired)
    assert str(tmp_path) not in str(caught.value)
