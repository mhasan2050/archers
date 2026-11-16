from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from archers.ingestion.normalization import (
    CorruptDocumentError,
    EncryptedDocumentError,
    InvalidFileError,
    ParseLimits,
    ResourceLimitError,
    UnsupportedFormatError,
    normalize,
)

FIXTURE_PREFIX = ".normalization-fixture-"


def _fixture_path(name: str) -> Path:
    return Path(__file__).with_name(f"{FIXTURE_PREFIX}{name}")


def _normalize_bytes(
    name: str,
    data: bytes,
    format_key: str,
    *,
    limits: ParseLimits | None = None,
):
    path = _fixture_path(name)
    path.write_bytes(data)
    try:
        return normalize(path, format_key, limits=limits if limits is not None else ParseLimits())
    finally:
        path.unlink(missing_ok=True)


def _pdf_bytes(*, encrypted: bool = False) -> bytes:
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    stream = pypdf.generic.DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 100 Td (offline page text) Tj ET")
    page[pypdf.generic.NameObject("/Contents")] = writer._add_object(stream)
    font = pypdf.generic.DictionaryObject(
        {
            pypdf.generic.NameObject("/Type"): pypdf.generic.NameObject("/Font"),
            pypdf.generic.NameObject("/Subtype"): pypdf.generic.NameObject("/Type1"),
            pypdf.generic.NameObject("/BaseFont"): pypdf.generic.NameObject("/Helvetica"),
        }
    )
    resources = pypdf.generic.DictionaryObject(
        {
            pypdf.generic.NameObject("/Font"): pypdf.generic.DictionaryObject(
                {pypdf.generic.NameObject("/F1"): writer._add_object(font)}
            )
        }
    )
    page[pypdf.generic.NameObject("/Resources")] = resources
    if encrypted:
        writer.encrypt("fixture-password")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _blank_pdf_bytes() -> bytes:
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _docx_bytes() -> bytes:
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_heading("Fixture", level=1)
    document.add_paragraph("Document paragraph.")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Name"
    table.cell(0, 1).text = "Value"
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def _pptx_bytes() -> bytes:
    pptx = pytest.importorskip("pptx")
    presentation = pptx.Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    shape = slide.shapes.add_textbox(0, 0, 1000000, 300000)
    shape.text = "Slide fixture"
    output = io.BytesIO()
    presentation.save(output)
    return output.getvalue()


def _xlsx_bytes(*, with_formula: bool = False) -> bytes:
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet["A1"] = "Name"
    sheet["B1"] = "Value"
    sheet["A2"] = "fixture"
    sheet["B2"] = "=1+1" if with_formula else "two"
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_text_and_markdown_blocks_have_exact_line_locations() -> None:
    text = _normalize_bytes("text.txt", b"first line\n\nsecond line\n", "text")
    assert text.blocks[0].text == "first line"
    assert (text.blocks[0].location.line_start, text.blocks[0].location.line_end) == (1, 1)
    assert text.blocks[1].text == "second line"
    assert (text.blocks[1].location.line_start, text.blocks[1].location.line_end) == (3, 3)
    markdown_path = _fixture_path("markdown.md")
    markdown_path.write_text("# Title\n\nA paragraph.\n", encoding="utf-8")
    try:
        markdown = normalize(markdown_path, "markdown")
    finally:
        markdown_path.unlink(missing_ok=True)
    assert markdown.blocks[0].text == "# Title"
    assert markdown.blocks[0].location.line_start == 1
    assert markdown.blocks[1].location.line_start == 3


def test_html_is_static_and_scripts_are_not_extracted() -> None:
    pytest.importorskip("bs4")
    html = (
        b"<h1>Visible</h1><script>do_not_extract()</script>"
        b"<p>Text</p><img src='https://invalid.example/image.png'>"
    )
    document = _normalize_bytes("page.html", html, "html")
    assert "Visible" in " ".join(block.text for block in document.blocks)
    assert "Text" in " ".join(block.text for block in document.blocks)
    assert all("do_not_extract" not in block.text for block in document.blocks)
    assert any(issue.code == "EMBEDDED_MEDIA_NOT_EXTRACTED" for issue in document.warnings)


def test_csv_records_have_row_and_column_locations() -> None:
    document = _normalize_bytes(
        "rows.csv",
        b"name,value\nalpha,1\nbeta,2\n",
        "csv",
    )
    first_data = next(block for block in document.blocks if "alpha" in block.text)
    assert first_data.location.row_start == first_data.location.row_end == 2
    assert first_data.location.columns == ("name", "value")
    assert "name: alpha" in first_data.text


def test_pdf_page_location_and_no_ocr_warning() -> None:
    document = _normalize_bytes("page.pdf", _pdf_bytes(), "pdf")
    assert any("offline page text" in block.text for block in document.blocks)
    assert all(block.location.page == 1 for block in document.blocks)

    blank = _normalize_bytes("blank.pdf", _blank_pdf_bytes(), "pdf")
    assert any(issue.code == "OCR_NOT_PERFORMED" for issue in blank.warnings)


def test_encrypted_and_corrupt_pdfs_are_explicit_errors() -> None:
    with pytest.raises(EncryptedDocumentError):
        _normalize_bytes("encrypted.pdf", _pdf_bytes(encrypted=True), "pdf")
    with pytest.raises(CorruptDocumentError):
        _normalize_bytes("corrupt.pdf", b"%PDF-1.7\nnot a pdf", "pdf")


def test_docx_blocks_have_stable_block_locations() -> None:
    document = _normalize_bytes("document.docx", _docx_bytes(), "docx")
    assert any("Document paragraph." in block.text for block in document.blocks)
    assert all(block.location.block_id for block in document.blocks)
    assert any(block.location.kind == "block" for block in document.blocks)


def test_pptx_blocks_identify_slide_and_shape() -> None:
    document = _normalize_bytes("slides.pptx", _pptx_bytes(), "pptx")
    block = next(block for block in document.blocks if "Slide fixture" in block.text)
    assert block.location.slide == 1
    assert block.location.block_id


def test_xlsx_uses_cell_locations_and_does_not_evaluate_formulas() -> None:
    document = _normalize_bytes("sheet.xlsx", _xlsx_bytes(with_formula=True), "xlsx")
    assert any(block.location.cell_range == "A2" for block in document.blocks)
    assert all("=1+1" not in block.text for block in document.blocks)
    assert any(issue.code == "FORMULA_CELLS_OMITTED" for issue in document.warnings)


def test_format_signature_mismatch_and_unsupported_format_fail_closed() -> None:
    with pytest.raises(InvalidFileError):
        _normalize_bytes("not-pdf.pdf", b"plain text", "pdf")
    with pytest.raises(UnsupportedFormatError):
        _normalize_bytes("legacy.doc", b"legacy", "doc")


def test_input_size_and_zip_expansion_limits_are_enforced() -> None:
    with pytest.raises(ResourceLimitError):
        _normalize_bytes(
            "large.txt",
            b"12345",
            "text",
            limits=ParseLimits(max_file_bytes=4),
        )

    path = _fixture_path("expanded.docx")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", b"x" * 2048)
        archive.writestr("word/document.xml", b"x" * 2048)
    try:
        with pytest.raises(ResourceLimitError):
            normalize(
                path,
                "docx",
                limits=ParseLimits(max_archive_member_bytes=1024),
            )
    finally:
        path.unlink(missing_ok=True)


def test_symlink_inputs_are_rejected() -> None:
    target = _fixture_path("target.txt")
    link = _fixture_path("link.txt")
    target.write_text("safe", encoding="utf-8")
    try:
        link.symlink_to(target.name)
        with pytest.raises(InvalidFileError):
            normalize(link, "text")
    finally:
        link.unlink(missing_ok=True)
        target.unlink(missing_ok=True)


def test_csv_malformed_rows_are_reported_as_corrupt() -> None:
    with pytest.raises(CorruptDocumentError):
        _normalize_bytes("bad.csv", b"a,b\n1\n", "csv")


def test_html_and_text_bad_encoding_are_rejected() -> None:
    with pytest.raises(InvalidFileError):
        _normalize_bytes("invalid.txt", b"\xff\x00", "text")


def test_zip_signature_is_required_for_office_formats() -> None:
    with pytest.raises(InvalidFileError):
        _normalize_bytes("fake.xlsx", b"not a workbook", "xlsx")


def test_office_macro_parts_are_rejected() -> None:
    path = _fixture_path("macro.xlsx")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        archive.writestr("xl/workbook.xml", b"<workbook/>")
        archive.writestr("xl/vbaProject.bin", b"macro bytes")
    try:
        with pytest.raises(UnsupportedFormatError):
            normalize(path, "xlsx")
    finally:
        path.unlink(missing_ok=True)
