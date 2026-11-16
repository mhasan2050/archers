"""Bounded, offline normalization of untrusted document bytes.

This module does not sandbox parser libraries. Callers must run it in an
appropriately isolated process before exposing it to user-facing ingestion.
"""

from __future__ import annotations

import csv
import io
import os
import re
import stat
import zipfile
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Literal

LocationKind = Literal["page", "slide", "cell", "line", "row", "block"]


@dataclass(frozen=True, slots=True)
class ParseLimits:
    max_file_bytes: int = 25 * 1024 * 1024
    max_pdf_pages: int = 1_000
    max_slides: int = 2_000
    max_sheets: int = 256
    max_rows: int = 100_000
    max_cells: int = 250_000
    max_blocks: int = 50_000
    max_text_chars: int = 5_000_000
    max_cell_chars: int = 100_000
    max_archive_entries: int = 10_000
    max_archive_member_bytes: int = 8 * 1024 * 1024
    max_archive_total_bytes: int = 64 * 1024 * 1024
    max_compression_ratio: int = 100

    def __post_init__(self) -> None:
        for limit in fields(self):
            if getattr(self, limit.name) <= 0:
                raise ValueError(f"{limit.name} must be positive")


@dataclass(frozen=True, slots=True)
class NormalizedLocation:
    kind: LocationKind
    page: int | None = None
    slide: int | None = None
    sheet: str | None = None
    cell_range: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    row_start: int | None = None
    row_end: int | None = None
    columns: tuple[str, ...] = ()
    block_id: str | None = None


@dataclass(frozen=True, slots=True)
class NormalizedBlock:
    text: str
    location: NormalizedLocation


@dataclass(frozen=True, slots=True)
class ParseWarning:
    code: str
    message: str
    location: NormalizedLocation | None = None


@dataclass(frozen=True, slots=True)
class NormalizedDocument:
    format: str
    blocks: tuple[NormalizedBlock, ...]
    warnings: tuple[ParseWarning, ...]
    parser_version: str = "normalization-v1"


class NormalizationError(Exception):
    """Base error with a stable, safe code; messages never include source data."""

    code = "NORMALIZATION_ERROR"

    def __init__(self) -> None:
        super().__init__(self.code)


class UnsupportedFormatError(NormalizationError):
    code = "UNSUPPORTED_FORMAT"


class InvalidFileError(NormalizationError):
    code = "INVALID_FILE"


class EncryptedDocumentError(NormalizationError):
    code = "ENCRYPTED_DOCUMENT"


class CorruptDocumentError(NormalizationError):
    code = "CORRUPT_DOCUMENT"


class ResourceLimitError(NormalizationError):
    code = "RESOURCE_LIMIT"


class ParserDependencyError(NormalizationError):
    code = "PARSER_UNAVAILABLE"


@dataclass(slots=True)
class _Builder:
    format: str
    limits: ParseLimits
    blocks: list[NormalizedBlock] = field(default_factory=list)
    warnings: list[ParseWarning] = field(default_factory=list)
    _chars: int = 0

    def add(
        self,
        text: str,
        location: NormalizedLocation,
    ) -> None:
        normalized = text.strip()
        if not normalized:
            return
        self._chars += len(normalized)
        if self._chars > self.limits.max_text_chars:
            raise ResourceLimitError
        if len(self.blocks) >= self.limits.max_blocks:
            raise ResourceLimitError
        self.blocks.append(NormalizedBlock(normalized, location))

    def warn(self, code: str, message: str, location: NormalizedLocation | None = None) -> None:
        self.warnings.append(ParseWarning(code, message, location))

    def finish(self) -> NormalizedDocument:
        return NormalizedDocument(self.format, tuple(self.blocks), tuple(self.warnings))


_SUPPORTED_FORMATS = frozenset({"text", "markdown", "html", "csv", "pdf", "docx", "pptx", "xlsx"})
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_KNOWN_BINARY_SIGNATURES = (b"%PDF-", *_ZIP_MAGIC, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
_BLOCK_TAGS = frozenset(
    {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote", "pre", "td", "th", "caption"}
)
_IGNORED_HTML_TAGS = ("script", "style", "noscript", "template", "iframe", "object", "embed")
_MEDIA_HTML_TAGS = ("img", "svg", "video", "audio", "canvas", "iframe", "object", "embed")
_OOXML_MAIN_PARTS = {
    "docx": "word/document.xml",
    "pptx": "ppt/presentation.xml",
    "xlsx": "xl/workbook.xml",
}
_MACRO_PART_RE = re.compile(r"(?:^|/)(?:vbaproject\.bin|vbadata\.xml)$", re.IGNORECASE)
_XML_DANGEROUS_DECLARATION_RE = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)


def normalize(
    path: Path,
    format: str,
    limits: ParseLimits = ParseLimits(),
) -> NormalizedDocument:
    """Normalize one local file without executing embedded code or fetching resources."""
    format_key = format.strip().lower()
    if format_key not in _SUPPORTED_FORMATS:
        raise UnsupportedFormatError
    source = _read_source(path, limits)
    builder = _Builder(format_key, limits)

    if format_key in {"text", "markdown"}:
        _reject_known_binary(source)
        text = _decode_text(source)
        _extract_lines(text, builder)
    elif format_key == "html":
        _reject_known_binary(source)
        text = _decode_text(source)
        _extract_html(text, builder)
    elif format_key == "csv":
        _reject_known_binary(source)
        text = _decode_text(source)
        _extract_csv(text, builder)
    elif format_key == "pdf":
        _extract_pdf(source, builder)
    else:
        _extract_ooxml(source, format_key, builder)
    return builder.finish()


def _read_source(path: Path, limits: ParseLimits) -> bytes:
    try:
        if path.is_symlink():
            raise InvalidFileError
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
    except NormalizationError:
        raise
    except (OSError, TypeError, ValueError):
        raise InvalidFileError from None
    try:
        with os.fdopen(descriptor, "rb") as source_file:
            metadata = os.fstat(source_file.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise InvalidFileError
            if metadata.st_size > limits.max_file_bytes:
                raise ResourceLimitError
            source = source_file.read(limits.max_file_bytes + 1)
        if len(source) > limits.max_file_bytes:
            raise ResourceLimitError
        return source
    except NormalizationError:
        raise
    except OSError:
        raise InvalidFileError from None


def _reject_known_binary(source: bytes) -> None:
    if source.startswith(_KNOWN_BINARY_SIGNATURES):
        raise InvalidFileError


def _decode_text(source: bytes) -> str:
    try:
        text = source.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        raise InvalidFileError from None
    if "\x00" in text:
        raise InvalidFileError
    invalid_controls = sum(1 for char in text if ord(char) < 32 and char not in "\t\n\r\f")
    if invalid_controls:
        raise InvalidFileError
    return text


def _extract_lines(text: str, builder: _Builder) -> None:
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        if not lines[index].strip():
            index += 1
            continue
        start = index + 1
        paragraph: list[str] = []
        while index < len(lines) and lines[index].strip():
            paragraph.append(lines[index])
            index += 1
        end = index
        builder.add(
            "\n".join(paragraph),
            NormalizedLocation(kind="line", line_start=start, line_end=end),
        )


def _extract_html(text: str, builder: _Builder) -> None:
    try:
        from bs4 import BeautifulSoup  # type: ignore[import-not-found]
    except ImportError:
        raise ParserDependencyError from None
    try:
        soup = BeautifulSoup(text, "html.parser")
        script_count = len(soup.find_all("script"))
        media_count = sum(len(soup.find_all(tag)) for tag in _MEDIA_HTML_TAGS)
        for tag_name in _IGNORED_HTML_TAGS:
            for tag in soup.find_all(tag_name):
                tag.decompose()
        block_number = 0
        for tag in soup.find_all(_BLOCK_TAGS):
            if any(parent.name in _BLOCK_TAGS for parent in tag.parents if parent.name):
                continue
            value = tag.get_text(" ", strip=True)
            if not value:
                continue
            block_number += 1
            builder.add(
                value,
                NormalizedLocation(kind="block", block_id=f"html-block-{block_number:06d}"),
            )
        if not builder.blocks:
            body = soup.body or soup
            value = body.get_text(" ", strip=True)
            if value:
                builder.add(
                    value,
                    NormalizedLocation(kind="block", block_id="html-block-000001"),
                )
        if script_count:
            builder.warn(
                "SCRIPT_CONTENT_IGNORED", "Embedded scripts were not executed or extracted."
            )
        if media_count:
            builder.warn(
                "EMBEDDED_MEDIA_NOT_EXTRACTED",
                "Embedded media and referenced resources were not loaded or interpreted.",
            )
    except ResourceLimitError:
        raise
    except Exception:
        raise CorruptDocumentError from None


def _extract_csv(text: str, builder: _Builder) -> None:
    try:
        sample = text[:8192]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(io.StringIO(text, newline=""), dialect=dialect, strict=True)
        iterator = iter(reader)
        try:
            first_row = next(iterator)
        except StopIteration:
            return
        first_end = reader.line_num
        width = len(first_row)
        if width == 0:
            raise CorruptDocumentError
        is_header = (
            all(value.strip() for value in first_row)
            and len({value.strip().casefold() for value in first_row}) == width
            and not all(_looks_numeric(value) for value in first_row)
        )
        columns = tuple(value.strip() for value in first_row) if is_header else ()
        total_cells = width
        if width > builder.limits.max_cells:
            raise ResourceLimitError
        if any(len(value) > builder.limits.max_cell_chars for value in first_row):
            raise ResourceLimitError
        previous_line = first_end
        if not is_header:
            _add_csv_row(first_row, 1, 1, first_end, columns, builder)
        for source_row, row in enumerate(iterator, start=2):
            if source_row > builder.limits.max_rows:
                raise ResourceLimitError
            line_end = reader.line_num
            line_start = previous_line + 1
            previous_line = line_end
            total_cells += len(row)
            if total_cells > builder.limits.max_cells:
                raise ResourceLimitError
            if len(row) != width:
                raise CorruptDocumentError
            if any(len(value) > builder.limits.max_cell_chars for value in row):
                raise ResourceLimitError
            _add_csv_row(row, source_row, line_start, line_end, columns, builder)
    except (csv.Error, UnicodeError):
        raise CorruptDocumentError from None


def _add_csv_row(
    row: list[str],
    source_row: int,
    line_start: int,
    line_end: int,
    columns: tuple[str, ...],
    builder: _Builder,
) -> None:
    parts = [
        f"{columns[index]}: {value}" if columns else value
        for index, value in enumerate(row)
        if value
    ]
    location = NormalizedLocation(
        kind="row",
        row_start=source_row,
        row_end=source_row,
        columns=columns,
    )
    builder.add(" | ".join(parts), location)
    if line_start != line_end:
        builder.warn(
            "MULTILINE_CSV_RECORD", "A CSV record spans multiple physical lines.", location
        )


def _looks_numeric(value: str) -> bool:
    try:
        float(value.strip())
    except ValueError:
        return False
    return True


def _extract_pdf(source: bytes, builder: _Builder) -> None:
    if not source.startswith(b"%PDF-"):
        raise InvalidFileError
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError:
        raise ParserDependencyError from None
    try:
        reader = PdfReader(io.BytesIO(source), strict=True)
        if reader.is_encrypted:
            raise EncryptedDocumentError
        page_count = len(reader.pages)
        if page_count > builder.limits.max_pdf_pages:
            raise ResourceLimitError
        for page_number, page in enumerate(reader.pages, start=1):
            location = NormalizedLocation(kind="page", page=page_number)
            extracted = page.extract_text()
            if extracted and extracted.strip():
                builder.add(extracted, location)
            else:
                builder.warn(
                    "OCR_NOT_PERFORMED",
                    "This page contains no extractable text; OCR is not implemented.",
                    location,
                )
            resources = page.get("/Resources")
            resource_dictionary = resources.get_object() if resources else None
            xobjects = resource_dictionary.get("/XObject") if resource_dictionary else None
            if xobjects:
                for image in xobjects.get_object().values():
                    if image.get_object().get("/Subtype") == "/Image":
                        builder.warn(
                            "EMBEDDED_IMAGES_NOT_OCR",
                            "Embedded page images were not interpreted or OCRed.",
                            location,
                        )
                        break
    except (EncryptedDocumentError, ResourceLimitError):
        raise
    except Exception:
        raise CorruptDocumentError from None


def _extract_ooxml(source: bytes, format_key: str, builder: _Builder) -> None:
    parts = _validate_ooxml(source, format_key, builder.limits)
    if format_key == "docx":
        _extract_docx(source, parts, builder)
    elif format_key == "pptx":
        _extract_pptx(source, parts, builder)
    else:
        _extract_xlsx(source, parts, builder)


def _validate_ooxml(
    source: bytes,
    format_key: str,
    limits: ParseLimits,
) -> frozenset[str]:
    if not source.startswith(_ZIP_MAGIC):
        raise InvalidFileError
    try:
        archive = zipfile.ZipFile(io.BytesIO(source))
        entries = archive.infolist()
        if len(entries) > limits.max_archive_entries:
            raise ResourceLimitError
        names: set[str] = set()
        declared_total_size = 0
        actual_total_size = 0
        package_types: bytes | None = None
        for entry in entries:
            name = entry.filename
            if (
                name.startswith(("/", "\\"))
                or "\\" in name
                or any(component == ".." for component in name.split("/"))
                or name in names
            ):
                raise InvalidFileError
            names.add(name)
            if entry.flag_bits & 0x1:
                raise EncryptedDocumentError
            if _MACRO_PART_RE.search(name) or name.lower().startswith(
                ("word/activex/", "ppt/activex/", "xl/activex/")
            ):
                raise UnsupportedFormatError
            if entry.file_size > limits.max_archive_member_bytes:
                raise ResourceLimitError
            declared_total_size += entry.file_size
            if declared_total_size > limits.max_archive_total_bytes:
                raise ResourceLimitError
            if entry.file_size and (
                entry.compress_size == 0
                or entry.file_size > entry.compress_size * limits.max_compression_ratio
            ):
                raise ResourceLimitError
            actual_total_size, member_content = _read_archive_member_limited(
                archive,
                entry,
                limits,
                actual_total_size,
            )
            if name == "[Content_Types].xml":
                package_types = member_content
        main_part = _OOXML_MAIN_PARTS[format_key]
        if "[Content_Types].xml" not in names or main_part not in names:
            raise CorruptDocumentError
        if package_types and b"macroenabled" in package_types.lower():
            raise UnsupportedFormatError
        return frozenset(names)
    except (EncryptedDocumentError, InvalidFileError, ResourceLimitError, UnsupportedFormatError):
        raise
    except CorruptDocumentError:
        raise
    except Exception:
        raise CorruptDocumentError from None
    finally:
        if "archive" in locals():
            archive.close()


def _read_archive_member_limited(
    archive: zipfile.ZipFile,
    entry: zipfile.ZipInfo,
    limits: ParseLimits,
    total_size: int,
) -> tuple[int, bytes | None]:
    is_xml = entry.filename.lower().endswith(".xml")
    is_content_types = entry.filename == "[Content_Types].xml"
    content_types = bytearray() if is_content_types else None
    member_size = 0
    tail = b""
    try:
        with archive.open(entry) as member:
            while chunk := member.read(64 * 1024):
                member_size += len(chunk)
                total_size += len(chunk)
                if (
                    member_size > limits.max_archive_member_bytes
                    or total_size > limits.max_archive_total_bytes
                ):
                    raise ResourceLimitError
                if entry.compress_size == 0 or member_size > (
                    entry.compress_size * limits.max_compression_ratio
                ):
                    raise ResourceLimitError
                if is_xml:
                    combined = tail + chunk
                    if _XML_DANGEROUS_DECLARATION_RE.search(combined):
                        raise UnsupportedFormatError
                    tail = combined[-32:]
                if content_types is not None:
                    content_types.extend(chunk)
        if member_size != entry.file_size:
            raise CorruptDocumentError
        return total_size, bytes(content_types) if content_types is not None else None
    except (UnsupportedFormatError, ResourceLimitError):
        raise
    except CorruptDocumentError:
        raise
    except Exception:
        raise CorruptDocumentError from None


def _extract_docx(source: bytes, parts: frozenset[str], builder: _Builder) -> None:
    try:
        from docx import Document  # type: ignore[import-not-found]
        from docx.oxml.table import CT_Tbl  # type: ignore[import-not-found]
        from docx.oxml.text.paragraph import CT_P  # type: ignore[import-not-found]
        from docx.table import Table  # type: ignore[import-not-found]
        from docx.text.paragraph import Paragraph  # type: ignore[import-not-found]
    except ImportError:
        raise ParserDependencyError from None
    try:
        document = Document(io.BytesIO(source))
        block_number = 0
        visited_blocks = 0
        table_rows = 0
        table_cells = 0
        for child in document.element.body.iterchildren():
            visited_blocks += 1
            if visited_blocks > builder.limits.max_blocks:
                raise ResourceLimitError
            if isinstance(child, CT_P):
                value = Paragraph(child, document).text
                if value.strip():
                    block_number += 1
                    builder.add(
                        value,
                        NormalizedLocation(kind="block", block_id=f"docx-block-{block_number:06d}"),
                    )
            elif isinstance(child, CT_Tbl):
                table = Table(child, document)
                for row_index, row in enumerate(table.rows, start=1):
                    table_rows += 1
                    table_cells += len(row.cells)
                    if (
                        table_rows > builder.limits.max_rows
                        or table_cells > builder.limits.max_cells
                    ):
                        raise ResourceLimitError
                    values = [cell.text.strip() for cell in row.cells]
                    if any(values):
                        block_number += 1
                        builder.add(
                            " | ".join(value for value in values if value),
                            NormalizedLocation(
                                kind="block",
                                block_id=f"docx-block-{block_number:06d}",
                                row_start=row_index,
                                row_end=row_index,
                            ),
                        )
        if any(name.startswith("word/media/") for name in parts):
            builder.warn(
                "EMBEDDED_MEDIA_NOT_EXTRACTED",
                "Embedded document media was not interpreted.",
            )
        if any(name.startswith("word/embeddings/") for name in parts):
            builder.warn(
                "EMBEDDED_OBJECTS_NOT_EXTRACTED",
                "Embedded document objects were not interpreted.",
            )
        if any(
            name.startswith(("word/header", "word/footer", "word/footnotes", "word/endnotes"))
            for name in parts
        ):
            builder.warn(
                "NON_BODY_CONTENT_OMITTED",
                "Headers, footers, footnotes, and endnotes were not included.",
            )
    except (ResourceLimitError, ParserDependencyError):
        raise
    except Exception:
        raise CorruptDocumentError from None


def _extract_pptx(source: bytes, parts: frozenset[str], builder: _Builder) -> None:
    try:
        from pptx import Presentation  # type: ignore[import-not-found]
    except ImportError:
        raise ParserDependencyError from None
    try:
        presentation = Presentation(io.BytesIO(source))
        if len(presentation.slides) > builder.limits.max_slides:
            raise ResourceLimitError
        shape_count = 0
        table_rows = 0
        table_cells = 0
        for slide_number, slide in enumerate(presentation.slides, start=1):
            for shape in slide.shapes:
                shape_count += 1
                if shape_count > builder.limits.max_blocks:
                    raise ResourceLimitError
                block_id = f"slide-{slide_number}-shape-{shape.shape_id}"
                location = NormalizedLocation(
                    kind="slide",
                    slide=slide_number,
                    block_id=block_id,
                )
                if shape.has_text_frame and shape.text.strip():
                    builder.add(shape.text, location)
                if shape.has_table:
                    for row_index, row in enumerate(shape.table.rows, start=1):
                        table_rows += 1
                        table_cells += len(row.cells)
                        if (
                            table_rows > builder.limits.max_rows
                            or table_cells > builder.limits.max_cells
                        ):
                            raise ResourceLimitError
                        values = [cell.text.strip() for cell in row.cells]
                        if any(values):
                            builder.add(
                                " | ".join(value for value in values if value),
                                NormalizedLocation(
                                    kind="slide",
                                    slide=slide_number,
                                    block_id=f"{block_id}-row-{row_index}",
                                    row_start=row_index,
                                    row_end=row_index,
                                ),
                            )
        coverage_parts = ("ppt/media/", "ppt/charts/", "ppt/embeddings/")
        if any(name.startswith(coverage_parts) for name in parts):
            builder.warn(
                "EMBEDDED_CONTENT_NOT_EXTRACTED",
                "Embedded media, charts, and objects were not interpreted.",
            )
        builder.warn(
            "SLIDE_NOTES_OMITTED",
            "Presenter notes are intentionally excluded by this parser's notes policy.",
        )
    except (ResourceLimitError, ParserDependencyError):
        raise
    except Exception:
        raise CorruptDocumentError from None


def _extract_xlsx(source: bytes, parts: frozenset[str], builder: _Builder) -> None:
    try:
        import openpyxl  # type: ignore[import-untyped]
    except ImportError:
        raise ParserDependencyError from None
    try:
        formulas = openpyxl.load_workbook(
            io.BytesIO(source),
            read_only=True,
            data_only=False,
            keep_links=False,
        )
        if len(formulas.worksheets) > builder.limits.max_sheets:
            raise ResourceLimitError
        total_cells = 0
        for formula_sheet in formulas.worksheets:
            formula_cells = 0
            row_count = 0
            for formula_row in formula_sheet.iter_rows():
                row_count += 1
                if row_count > builder.limits.max_rows:
                    raise ResourceLimitError
                for formula_cell in formula_row:
                    total_cells += 1
                    if total_cells > builder.limits.max_cells:
                        raise ResourceLimitError
                    if formula_cell.data_type == "f":
                        formula_cells += 1
                        continue
                    if formula_cell.value is None:
                        continue
                    value = str(formula_cell.value)
                    if len(value) > builder.limits.max_cell_chars:
                        raise ResourceLimitError
                    builder.add(
                        value,
                        NormalizedLocation(
                            kind="cell",
                            sheet=formula_sheet.title,
                            cell_range=formula_cell.coordinate,
                        ),
                    )
            if formula_cells:
                builder.warn(
                    "FORMULA_CELLS_OMITTED",
                    "Formula cells and cached results were omitted; no formulas were evaluated.",
                    NormalizedLocation(kind="cell", sheet=formula_sheet.title),
                )
        if any(name.startswith("xl/media/") or name.startswith("xl/charts/") for name in parts):
            builder.warn(
                "EMBEDDED_CONTENT_NOT_EXTRACTED",
                "Embedded spreadsheet media and charts were not interpreted.",
            )
        if any(name.startswith("xl/embeddings/") for name in parts):
            builder.warn(
                "EMBEDDED_OBJECTS_NOT_EXTRACTED",
                "Embedded spreadsheet objects were not interpreted.",
            )
        if any(name.startswith("xl/externalLinks/") for name in parts):
            builder.warn(
                "EXTERNAL_LINKS_OMITTED",
                "Workbook external links were not followed or interpreted.",
            )
    except (ResourceLimitError, ParserDependencyError):
        raise
    except Exception:
        raise CorruptDocumentError from None
    finally:
        if "formulas" in locals():
            formulas.close()
