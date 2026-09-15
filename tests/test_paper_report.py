"""Unified paper report renderer tests (offline, no Office dependency)."""
from __future__ import annotations

import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

import pytest

from vcstudio.project import paper_report


def _png(path: Path, color=(37, 99, 235)) -> Path:
    from PIL import Image

    image = Image.new("RGB", (640, 360), color)
    image.save(path)
    return path


def _model(tmp_path: Path) -> dict:
    figure = _png(tmp_path / "step plot.png")
    return {
        "title": "Adsorption Screening Report",
        "subtitle": "A reproducible multi-catalyst comparison",
        "kicker": "Computational Catalysis",
        "metadata": {
            "Project": "Li-S catalyst screen",
            "Date": "2026-07-24",
            "Status": "Validated data",
        },
        "executive_summary": (
            "The shared report model keeps numerical statements aligned across all formats."
        ),
        "key_findings": [
            {"title": "Balanced binding", "text": "Candidate A merits further calculations."},
            "Near ties below 0.2 eV should not be over-ranked.",
        ],
        "candidate_evaluations": [
            {
                "candidate": "Candidate A",
                "status": "Advance",
                "evidence": "Moderate long-chain binding",
            },
            {
                "candidate": "Candidate B",
                "status": "Conditional",
                "evidence": "Check Li2S decomposition",
            },
        ],
        "adsorption_table": {
            "title": "Stable adsorption configurations",
            "caption": "Negative values indicate exothermic adsorption.",
            "columns": [
                {"key": "species", "label": "Species"},
                {"key": "energy", "label": "Eads (eV)", "align": "right"},
                {"key": "decision", "label": "Screening decision"},
            ],
            "rows": [
                {"species": "Li2S8", "energy": -1.04, "decision": "Advance"},
                {"species": "Li2S", "energy": -2.81, "decision": "Check kinetics"},
            ],
        },
        "comparison_table": [
            {"catalyst": "Candidate A", "UL (V)": 1.55, "PDS": "Li2S2 -> Li2S"},
            {"catalyst": "Candidate B", "UL (V)": 1.31, "PDS": "Li2S4 -> Li2S2"},
        ],
        "figures": [
            {
                "path": figure,
                "title": "Free-energy pathways",
                "caption": "All candidate paths use the same reaction-state order.",
                "alt": "Overlaid free-energy pathways",
            }
        ],
        "methods": [
            {
                "title": "Electronic structure",
                "text": "Method evidence is read from each managed calculation.",
            }
        ],
        "limitations": ["Solvation and activation barriers are not included."],
        "recommendations": [
            "Run Li2S decomposition NEB for Candidate A.",
            "Retain the raw method-consistency evidence.",
        ],
    }


def test_render_all_formats_from_one_model_and_write_manifest(tmp_path):
    model = _model(tmp_path)
    result = paper_report.render_report_bundle(model, tmp_path / "bundle", stem="screening")

    assert result["schema"] == paper_report.BUNDLE_SCHEMA
    assert set(result["files"]) == {"html", "docx", "pdf", "manifest"}
    assert all(path.is_file() and path.stat().st_size > 100 for path in result["files"].values())
    assert len(result["assets"]) == 1 and result["assets"][0].parent.name == "assets"

    manifest = json.loads(result["manifest"].read_text(encoding="utf-8"))
    assert manifest["schema"] == paper_report.BUNDLE_SCHEMA
    assert manifest["model_schema"] == paper_report.MODEL_SCHEMA
    assert manifest["model_sha256"] == result["model_sha256"]
    assert set(manifest["files"]) == {"html", "docx", "pdf"}
    for record in manifest["files"].values():
        path = result["manifest"].parent / record["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]
        assert path.stat().st_size == record["size"]


def test_assets_are_hash_staged_and_never_use_cross_drive_relpath(monkeypatch, tmp_path):
    model = _model(tmp_path)
    duplicate = tmp_path / "different-name.png"
    duplicate.write_bytes(Path(model["figures"][0]["path"]).read_bytes())
    model["figures"].append({"path": duplicate, "title": "Same pixels"})

    def forbidden_relpath(*_args, **_kwargs):
        raise AssertionError("os.path.relpath must not be used for report assets")

    monkeypatch.setattr(paper_report.os.path, "relpath", forbidden_relpath)
    result = paper_report.render_report_bundle(
        model,
        tmp_path / "other-volume-output",
        formats=("html",),
    )
    assert len(result["assets"]) == 1  # identical bytes are deduplicated
    html = result["files"]["html"].read_text(encoding="utf-8")
    assert html.count("assets/") == 2
    assert str(tmp_path) not in html
    assert model["figures"][0]["path"].is_file()  # sources are never moved or rewritten


def test_html_escapes_content_and_uses_paper_tables(tmp_path):
    model = _model(tmp_path)
    model["title"] = "<script>alert(1)</script>"
    result = paper_report.render_report_bundle(
        model,
        tmp_path / "html-only",
        formats=("html",),
    )
    text = result["files"]["html"].read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert "@page" in text and "size: A4 portrait" in text
    assert "table class='three-line'" in text
    assert "Figure 1. Free-energy pathways" in text


def test_docx_is_a4_with_header_footer_page_field_and_three_line_table(tmp_path):
    from docx import Document
    from docx.oxml.ns import qn
    from docx.shared import Mm

    result = paper_report.render_report_bundle(
        _model(tmp_path),
        tmp_path / "docx-only",
        formats=("docx",),
    )
    path = result["files"]["docx"]
    doc = Document(path)
    section = doc.sections[0]
    assert abs(section.page_width - Mm(210)) < 1000  # OOXML rounds to whole twips.
    assert abs(section.page_height - Mm(297)) < 1000
    assert "COMPUTATIONAL CATALYSIS" in section.header.paragraphs[0].text
    with zipfile.ZipFile(path) as archive:
        footer_xml = archive.read("word/footer1.xml").decode("utf-8")
        document_xml = archive.read("word/document.xml").decode("utf-8")
    assert "PAGE" in footer_xml
    assert "w:numPr" in document_xml  # findings/recommendations use real Word numbering.
    assert doc.tables
    borders = doc.tables[0]._tbl.tblPr.find(qn("w:tblBorders"))
    assert borders.find(qn("w:top")).get(qn("w:val")) == "single"
    assert borders.find(qn("w:bottom")).get(qn("w:val")) == "single"
    assert borders.find(qn("w:insideV")).get(qn("w:val")) == "nil"
    all_text = "\n".join(paragraph.text for paragraph in doc.paragraphs)
    assert "Adsorption Screening Report" in all_text
    assert "Table 1. Candidate evaluation" in all_text
    assert "Figure 1. Free-energy pathways" in all_text


def test_pdf_is_a4_multipage_and_contains_shared_model_text(tmp_path):
    from pypdf import PdfReader

    result = paper_report.render_report_bundle(
        _model(tmp_path),
        tmp_path / "pdf-only",
        formats=("pdf",),
    )
    reader = PdfReader(result["files"]["pdf"])
    assert len(reader.pages) >= 2
    page = reader.pages[0]
    width = float(page.mediabox.width)
    height = float(page.mediabox.height)
    assert width == pytest.approx(595.28, abs=0.5)
    assert height == pytest.approx(841.89, abs=0.5)
    text = "\n".join(item.extract_text() or "" for item in reader.pages)
    assert "Adsorption Screening Report" in text
    assert "Candidate A" in text
    assert "Figure 1." in text
    assert "Page 2" in text


def test_model_hash_tracks_figure_content_not_source_location(tmp_path):
    first_model = _model(tmp_path)
    first = paper_report.render_report_bundle(
        first_model,
        tmp_path / "first",
        formats=("html",),
    )
    moved = tmp_path / "moved" / "renamed.png"
    moved.parent.mkdir()
    moved.write_bytes(Path(first_model["figures"][0]["path"]).read_bytes())
    second_model = _model(tmp_path)
    second_model["figures"][0]["path"] = moved
    second = paper_report.render_report_bundle(
        second_model,
        tmp_path / "second",
        formats=("html",),
    )
    assert first["model_sha256"] == second["model_sha256"]

    _png(moved, color=(220, 38, 38))
    changed = paper_report.render_report_bundle(
        second_model,
        tmp_path / "changed",
        formats=("html",),
    )
    assert changed["model_sha256"] != second["model_sha256"]


def test_missing_requested_dependency_has_clear_error(monkeypatch, tmp_path):
    real_import = paper_report.importlib.import_module

    def missing_docx(name, *args, **kwargs):
        if name == "docx":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(paper_report.importlib, "import_module", missing_docx)
    with pytest.raises(paper_report.ReportDependencyError, match="python-docx"):
        paper_report.render_report_bundle(
            _model(tmp_path),
            tmp_path / "missing",
            formats=("docx",),
        )


def test_zh_locale_localizes_sections_captions_and_page_furniture(tmp_path):
    from docx import Document
    from pypdf import PdfReader

    model = _model(tmp_path)
    model.update(
        locale="zh-CN",
        title="锂硫电池吸附能筛选报告",
        subtitle="热力学筛选与后续计算建议",
        kicker="第一性原理计算报告",
        executive_summary="吸附能需要满足抓得住、转得动和脱得掉。",
    )
    result = paper_report.render_report_bundle(
        model,
        tmp_path / "zh-bundle",
        stem="中文报告",
    )

    html = result["files"]["html"].read_text(encoding="utf-8")
    for heading in (
        "执行摘要",
        "核心结论",
        "候选评价",
        "吸附能结果",
        "多项目比较",
        "图表",
        "计算方法",
        "局限性",
        "后续建议",
    ):
        assert heading in html
    assert "表 1  Candidate evaluation" in html
    assert "图 1  Free-energy pathways" in html
    assert '"第 " counter(page) " 页"' in html

    doc = Document(result["files"]["docx"])
    doc_text = "\n".join(paragraph.text for paragraph in doc.paragraphs)
    assert "1. 执行摘要" in doc_text
    assert "表 1  Candidate evaluation" in doc_text
    assert "图 1  Free-energy pathways" in doc_text
    with zipfile.ZipFile(result["files"]["docx"]) as archive:
        footer_xml = archive.read("word/footer1.xml").decode("utf-8")
    assert "第 " in footer_xml and " 页" in footer_xml and "PAGE" in footer_xml

    reader = PdfReader(result["files"]["pdf"])
    pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
    compact_pdf_text = re.sub(r"\s+", " ", pdf_text)
    assert "锂硫电池吸附能筛选报告" in pdf_text
    assert "01 执行摘要" in compact_pdf_text
    assert "表 1 Candidate evaluation" in compact_pdf_text
    assert "图 1 Free-energy pathways" in compact_pdf_text
    assert "第 2 页" in compact_pdf_text


def test_table_columns_accept_plain_string_keys(tmp_path):
    model = _model(tmp_path)
    model["comparison_table"] = {
        "title": "Direct string columns",
        "columns": ["candidate", "score"],
        "rows": [
            {"candidate": "A", "score": 0.91},
            {"candidate": "B", "score": 0.86},
        ],
    }
    result = paper_report.render_report_bundle(
        model,
        tmp_path / "string-columns",
        formats=("html",),
    )
    html = result["files"]["html"].read_text(encoding="utf-8")
    assert "<th class=''>candidate</th>" in html
    assert "<th class='numeric'>score</th>" in html
    assert "<td class='numeric'>0.91</td>" in html


def test_pdf_font_root_supports_frozen_bundle_layout(monkeypatch, tmp_path):
    frozen = tmp_path / "frozen"
    # packaging/build_exe.py installs the complete Web asset tree here.
    fonts = frozen / "vcstudio_assets" / "fonts"
    fonts.mkdir(parents=True)
    for name in ("noto-sans-sc-400.ttf", "noto-sans-sc-700.ttf"):
        (fonts / name).write_bytes(b"frozen-font-fixture")
    monkeypatch.setattr(sys, "_MEIPASS", str(frozen), raising=False)
    assert paper_report._pdf_font_root() == fonts


def test_source_pdf_fonts_are_preconverted_true_type():
    root = paper_report._pdf_font_root()
    for name in (
        "noto-sans-sc-400.ttf",
        "noto-sans-sc-700.ttf",
        "dejavu-sans-400.ttf",
        "dejavu-sans-700.ttf",
        "dejavu-sans-400-italic.ttf",
        "dejavu-sans-700-italic.ttf",
    ):
        data = (root / name).read_bytes()
        assert data[:4] in {b"\x00\x01\x00\x00", b"OTTO"}
        assert len(data) > 500_000


def test_bundled_latin_fonts_cover_scientific_text_without_matplotlib(monkeypatch, tmp_path):
    import reportlab
    from reportlab.pdfbase.ttfonts import TTFont

    real_import = paper_report.importlib.import_module

    def no_matplotlib(name, *args, **kwargs):
        if name == "matplotlib":
            raise AssertionError("standalone PDF fonts must not import matplotlib")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(paper_report.importlib, "import_module", no_matplotlib)
    files = paper_report._latin_pdf_font_files(reportlab)
    regular = TTFont("StandaloneScientificCoverage", str(files["PaperSans"]))
    for character in "ΔηLi₂S₈→⁺⁻":
        assert ord(character) in regular.face.charToGlyph

    model = _model(tmp_path)
    model["title"] = "Li₂S₈ → Li₂S · ΔG · η"
    result = paper_report.render_report_bundle(
        model,
        tmp_path / "standalone-fonts",
        formats=("pdf",),
    )
    assert result["files"]["pdf"].stat().st_size > 1_000


def test_font_notice_records_licenses_and_current_hashes():
    root = paper_report._pdf_font_root()
    notice = (root / "NOTICE.md").read_text(encoding="utf-8")
    assert (root / "OFL-1.1.txt").is_file()
    assert (root / "LICENSE-DEJAVU.txt").is_file()
    for name in (
        "noto-sans-sc-400.ttf",
        "noto-sans-sc-700.ttf",
        "dejavu-sans-400.ttf",
        "dejavu-sans-700.ttf",
        "dejavu-sans-400-italic.ttf",
        "dejavu-sans-700-italic.ttf",
    ):
        digest = hashlib.sha256((root / name).read_bytes()).hexdigest()
        assert f"`{name}`" in notice
        assert f"`{digest}`" in notice


def test_publish_failure_rolls_back_all_formats_and_manifest(monkeypatch, tmp_path):
    output = tmp_path / "transactional"
    old = paper_report.render_report_bundle(
        _model(tmp_path),
        output,
        formats=("html", "docx", "pdf"),
    )
    before = {
        kind: path.read_bytes()
        for kind, path in old["files"].items()
    }

    replacement = _model(tmp_path)
    replacement["title"] = "A replacement that must not be published partially"
    real_replace = paper_report.os.replace

    def fail_while_docx_is_open(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            source_path.suffix == ".docx"
            and destination_path.suffix == ".docx"
            and destination_path.parent == output
        ):
            raise PermissionError("simulated Word file lock")
        return real_replace(source, destination)

    monkeypatch.setattr(paper_report.os, "replace", fail_while_docx_is_open)
    with pytest.raises(PermissionError, match="Word file lock"):
        paper_report.render_report_bundle(
            replacement,
            output,
            formats=("html", "docx", "pdf"),
        )

    for kind, expected in before.items():
        assert old["files"][kind].read_bytes() == expected


@pytest.mark.parametrize("stem", ("../escape", r"C:\escape", "", ".."))
def test_unsafe_stem_is_rejected(tmp_path, stem):
    with pytest.raises(ValueError, match="safe filename"):
        paper_report.render_report_bundle(
            _model(tmp_path),
            tmp_path / "bundle",
            stem=stem,
            formats=("html",),
        )
