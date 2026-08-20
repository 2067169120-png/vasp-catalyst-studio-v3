from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from vcstudio.generate.structure_sources import (
    StructureSourceChangedError,
    StructureSourceError,
    StructureSourceSession,
    StructureSourceTokenError,
    StructureSourceValidationError,
    parse_structure_content,
)


POSCAR = """C:\\private\\sample\\POSCAR
1.0
3.0 0.0 0.0
0.0 3.0 0.0
0.0 0.0 8.0
Cu O
1 1
Direct
0.0 0.0 0.25
0.5 0.5 0.75
"""

CIF = """data_offline_fixture
_space_group_IT_number 1
_cell_length_a 3.0
_cell_length_b 4.0
_cell_length_c 5.0
_cell_angle_alpha 90
_cell_angle_beta 90
_cell_angle_gamma 90
loop_
_atom_site_label
_atom_site_type_symbol
_atom_site_fract_x
_atom_site_fract_y
_atom_site_fract_z
_atom_site_occupancy
Si1 Si 0.0 0.0 0.0 1
O1 O 0.25 0.25 0.25 1.0
O2 O 0.75 0.75 0.75 1
"""


def _assert_path_free(payload, forbidden: str) -> None:
    wire = repr(payload)
    assert forbidden not in wire
    assert "\\private\\" not in wire


def test_local_poscar_result_is_minimal_and_preview_is_canonical(tmp_path: Path):
    source = tmp_path / "POSCAR"
    source.write_text(POSCAR, encoding="utf-8")
    session = StructureSourceSession()

    results = session.select_local(source)

    assert len(results) == 1
    result = results[0]
    assert set(result) == {
        "token", "source_id", "formula", "license", "citation",
        "method", "raw_structure_sha256", "declared_formula",
        "declared_raw_structure_sha256", "computed_formula",
        "computed_structure_sha256",
    }
    assert result["formula"] == "CuO"
    assert result["declared_formula"] == "CuO"
    assert result["computed_formula"] == "CuO"
    assert result["method"] == {
        "provider": "local",
        "format": "poscar",
        "parser": "vcstudio.structure-source/v1",
        "network": False,
    }
    _assert_path_free(result, str(tmp_path))

    preview = session.preview(result["token"])
    assert preview["structure"]["poscar"].startswith("vcstudio structure source\n1.0\n")
    assert preview["structure"]["elements"] == ["Cu", "O"]
    assert preview["structure"]["counts"] == [1, 1]
    assert preview["view"]["natoms"] == 2
    assert preview["raw_structure_sha256"] == result["raw_structure_sha256"]
    assert len(preview["structure_sha256"]) == 64
    assert preview["computed_structure_sha256"] == result["computed_structure_sha256"]
    assert preview["provenance"]["declared_raw_structure_sha256"] == result[
        "raw_structure_sha256"]
    _assert_path_free(preview, str(tmp_path))


def test_restricted_cif_parser_builds_deterministic_poscar(tmp_path: Path):
    parsed_a = parse_structure_content(CIF, "cif")
    parsed_b = parse_structure_content(CIF.replace("3.0", "3.000", 1), "cif")

    assert parsed_a.formula == "SiO2"
    assert parsed_a.canonical_poscar == parsed_b.canonical_poscar
    assert parsed_a.cell == ((3.0, 0.0, 0.0), (0.0, 4.0, 0.0), (0.0, 0.0, 5.0))

    source = tmp_path / "fixture.cif"
    source.write_text(CIF, encoding="utf-8")
    session = StructureSourceSession()
    result = session.select_local(source)[0]
    preview = session.preview(result["token"])

    assert result["method"]["format"] == "cif"
    assert preview["structure"]["format"] == "poscar"
    assert preview["formula"] == "SiO2"
    assert preview["structure"]["cartesian_coords"][1] == [0.75, 1.0, 1.25]


@pytest.mark.parametrize(
    "content, message",
    [
        (CIF.replace("O1 O 0.25 0.25 0.25 1.0", "O1 O 0.25 0.25 0.25 0.5"),
         "partial occupancy"),
        (CIF.replace("_atom_site_fract_z\n", ""), "fractional coordinate"),
        (CIF.replace("_cell_angle_gamma 90", "_cell_angle_gamma 180"), "cell angles"),
    ],
)
def test_restricted_cif_parser_fails_closed(content: str, message: str):
    with pytest.raises(StructureSourceValidationError, match=message):
        parse_structure_content(content, "cif")


def test_restricted_cif_parser_rejects_unexpanded_non_p1_symmetry():
    non_p1 = CIF.replace(
        "_space_group_IT_number 1",
        "_space_group_IT_number 225",
    )

    with pytest.raises(StructureSourceValidationError, match="non-P1 symmetry"):
        parse_structure_content(non_p1, "cif")


@pytest.mark.parametrize(
    "content",
    [
        CIF.replace("_space_group_IT_number 1\n", ""),
        CIF.replace(
            "_space_group_IT_number 1",
            "_space_group_symop_operation_xyz 'x,y,z'",
        ),
    ],
)
def test_restricted_cif_parser_does_not_assume_p1_from_missing_or_identity_symop(
    content: str,
):
    with pytest.raises(StructureSourceValidationError, match="space group is unknown"):
        parse_structure_content(content, "cif")


def test_restricted_cif_parser_rejects_duplicate_fractional_coordinate_columns():
    duplicate_x = CIF.replace(
        "_atom_site_fract_x\n",
        "_atom_site_fract_x\n_atom_site_fract_x\n",
    )

    with pytest.raises(StructureSourceValidationError, match="columns must be unique"):
        parse_structure_content(duplicate_x, "cif")

    scalar_and_loop_x = CIF.replace(
        "_cell_length_a 3.0",
        "_atom_site_fract_x 0.125\n_cell_length_a 3.0",
    )
    with pytest.raises(StructureSourceValidationError, match="exactly once"):
        parse_structure_content(scalar_and_loop_x, "cif")


def test_local_source_hash_change_fails_closed_at_preview_and_confirm(tmp_path: Path):
    source = tmp_path / "POSCAR"
    source.write_text(POSCAR, encoding="utf-8")
    session = StructureSourceSession()
    token = session.select_local(source)[0]["token"]

    source.write_text(POSCAR.replace("0.5 0.5 0.75", "0.4 0.5 0.75"), encoding="utf-8")

    with pytest.raises(StructureSourceChangedError, match="changed"):
        session.preview(token)
    with pytest.raises(StructureSourceChangedError, match="changed"):
        session.confirm(token)


def test_confirmation_is_idempotent_until_private_resolution_and_consumes_once(tmp_path: Path):
    source = tmp_path / "POSCAR"
    source.write_text(POSCAR, encoding="utf-8")
    session = StructureSourceSession()
    result = session.select_local(source)[0]

    first = session.confirm(result["token"])
    second = session.confirm(result["token"])

    assert first == second
    assert first["confirmed"] is True
    assert first["source"] == result
    _assert_path_free(first, str(tmp_path))
    private = session.resolve_confirmed(first["confirmation_token"])
    assert private["raw_source"] == private["poscar"]
    assert r"C:\private\sample\POSCAR" not in private["raw_source"]
    assert private["poscar"].startswith("vcstudio structure source")
    assert private["provenance"]["provider"] == "local"
    assert "path" not in private
    with pytest.raises(StructureSourceTokenError, match="invalid, expired, or consumed"):
        session.resolve_confirmed(first["confirmation_token"])
    with pytest.raises(StructureSourceTokenError, match="invalid or expired"):
        session.preview(result["token"])


class _FakeGateway:
    def __init__(self) -> None:
        self.fail = False

    def capabilities(self):
        if self.fail:
            raise OSError("offline")
        return [{
            "provider": "fixture-db",
            "label": {"zh": "离线夹具库", "en": "Offline fixture database"},
            "modes": ["search", "preview"],
            "formats": ["poscar"],
            "network": True,
            "enabled": True,
        }]

    def search(self, provider, query):
        if self.fail:
            raise OSError("network unavailable")
        assert provider == "fixture-db"
        assert query == "CuO"
        return [{
            "token": "gateway.opaque-token-0001",
            "source_id": "fixture-42",
            "formula": "CuO",
            "license": {"name": "CC0-1.0", "url": "https://example.invalid/license"},
            "citation": {"text": "Offline fixture record"},
            "method": {
                "provider": "fixture-db", "retrieval": "offline-fixture", "network": False,
            },
            "raw_structure_sha256": "a" * 64,
        }]

    def preview(self, token):
        if self.fail:
            raise OSError("network unavailable")
        assert token == "gateway.opaque-token-0001"
        path_free_poscar = POSCAR.replace("C:\\private\\sample\\POSCAR", "gateway fixture")
        canonical_digest = parse_structure_content(
            path_free_poscar, "poscar").structure_sha256
        return {
            "token": token,
            "source_id": "fixture-42",
            "formula": "CuO",
            "raw_structure_sha256": "a" * 64,
            "canonical_structure_sha256": canonical_digest,
            "poscar": path_free_poscar,
        }


def test_fake_gateway_token_is_preserved_and_response_is_validated():
    session = StructureSourceSession(gateway=_FakeGateway())

    capabilities = session.capabilities()
    assert [item["provider"] for item in capabilities] == ["local", "fixture-db"]
    result = session.search("fixture-db", "CuO")[0]

    assert result["token"] == "gateway.opaque-token-0001"
    assert set(result) == {
        "token", "source_id", "formula", "license", "citation",
        "method", "raw_structure_sha256", "declared_formula",
        "declared_raw_structure_sha256", "computed_formula",
        "computed_structure_sha256",
    }
    assert result["computed_formula"] is None
    assert result["computed_structure_sha256"] is None
    preview = session.preview(result["token"])
    assert preview["formula"] == "CuO"
    assert preview["declared_formula"] == "CuO"
    assert preview["computed_formula"] == "CuO"
    assert preview["computed_structure_sha256"] == preview["structure_sha256"]
    assert "\\private\\" not in preview["structure"]["poscar"]

    confirmation = session.confirm(result["token"])
    private = session.resolve_confirmed(confirmation["confirmation_token"])
    assert private["provenance"]["database_id"] == "fixture-42"
    assert private["provenance"]["query"] == "CuO"
    assert private["provenance"]["declared_raw_structure_sha256"] == "a" * 64
    assert private["provenance"]["computed_structure_sha256"] == private[
        "structure_sha256"]
    assert private["raw_source"] == private["poscar"]


@pytest.mark.parametrize("mutation, message", [
    ("missing-canonical-hash", "canonical structure SHA-256 is required"),
    ("wrong-canonical-hash", "does not match server computation"),
    ("content-formula-mismatch", "computed composition"),
])
def test_gateway_preview_requires_server_verifiable_content_identity(mutation, message):
    gateway = _FakeGateway()
    original_search = gateway.search
    original_preview = gateway.preview

    if mutation == "content-formula-mismatch":
        def mismatched_search(provider, query):
            result = deepcopy(original_search(provider, query)[0])
            result["formula"] = "FeO"
            return [result]

        gateway.search = mismatched_search

    def adversarial_preview(token):
        result = deepcopy(original_preview(token))
        if mutation == "missing-canonical-hash":
            result.pop("canonical_structure_sha256")
        elif mutation == "wrong-canonical-hash":
            result["canonical_structure_sha256"] = "b" * 64
        elif mutation == "content-formula-mismatch":
            result["formula"] = "FeO"
        return result

    gateway.preview = adversarial_preview
    session = StructureSourceSession(gateway=gateway)
    result = session.search("fixture-db", "CuO")[0]

    with pytest.raises(StructureSourceValidationError, match=message):
        session.preview(result["token"])


@pytest.mark.parametrize(
    "field, malicious",
    [
        ("license", {"name": "terms copied from /home/alice/private/license.txt"}),
        ("license", {"name": "terms copied from /workspace"}),
        ("citation", {"text": r"citation mirror at \\server\share\paper.txt"}),
        ("citation", {"text": "cached as file:///etc/passwd"}),
        ("method", {"provider": "fixture-db", "nested": {
            "note": r"source cache C:\private\record.cif"}}),
        ("method", {"provider": "fixture-db", "nested": {
            "note": r"prefixC:\private\record.cif"}}),
        ("method", {"provider": "fixture-db", "nested": [
            "endpoint https://alice:secret@example.invalid/api"]}),
        ("method", {"provider": "fixture-db", "nested": {
            "note": "Authorization Bearer abcdefghijklmnop"}}),
        ("method", {"provider": "fixture-db", "nested": {
            "note": "password is abcdefghijklmnop"}}),
    ],
)
def test_gateway_search_recursively_rejects_embedded_paths_and_credentials(
    field, malicious,
):
    gateway = _FakeGateway()
    original = gateway.search

    def poisoned(provider, query):
        result = deepcopy(original(provider, query)[0])
        result[field] = malicious
        return [result]

    gateway.search = poisoned
    session = StructureSourceSession(gateway=gateway)

    with pytest.raises(
        StructureSourceValidationError,
        match="filesystem path|credential",
    ):
        session.search("fixture-db", "CuO")


def test_gateway_malformed_or_path_bearing_dto_is_rejected():
    gateway = _FakeGateway()
    original = gateway.search

    def malicious(provider, query):
        result = deepcopy(original(provider, query)[0])
        result["path"] = "C:\\private\\secret.cif"
        return [result]

    gateway.search = malicious
    session = StructureSourceSession(gateway=gateway)

    with pytest.raises(StructureSourceValidationError, match="unexpected fields"):
        session.search("fixture-db", "CuO")


def test_gateway_failure_does_not_break_local_provider(tmp_path: Path):
    gateway = _FakeGateway()
    gateway.fail = True
    session = StructureSourceSession(gateway=gateway)

    assert [item["provider"] for item in session.capabilities()] == ["local"]
    with pytest.raises(StructureSourceError, match="unavailable"):
        session.search("fixture-db", "CuO")

    source = tmp_path / "POSCAR"
    source.write_text(POSCAR, encoding="utf-8")
    local = session.select_local(source)[0]
    assert session.preview(local["token"])["formula"] == "CuO"


def test_source_and_confirmation_tokens_are_ttl_and_capacity_bounded(tmp_path: Path):
    now = [100.0]
    session = StructureSourceSession(
        ttl_seconds=10.0,
        max_tokens=1,
        clock=lambda: now[0],
    )
    first_path = tmp_path / "first.POSCAR"
    second_path = tmp_path / "second.POSCAR"
    first_path.write_text(POSCAR, encoding="utf-8")
    second_path.write_text(POSCAR.replace("Cu O", "Ni O"), encoding="utf-8")

    first = session.select_local(first_path)[0]
    second = session.select_local(second_path)[0]
    with pytest.raises(StructureSourceTokenError, match="invalid or expired"):
        session.preview(first["token"])

    confirmation = session.confirm(second["token"])
    now[0] += 11.0
    with pytest.raises(StructureSourceTokenError, match="invalid, expired, or consumed"):
        session.resolve_confirmed(confirmation["confirmation_token"])
