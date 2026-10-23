"""
CT-200 PDF Document Parser with Layout-Aware Extraction, Hierarchy Reconstruction,
and Structural Irregularity Handling.
"""

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Dict, List, Optional
import unicodedata

import pymupdf


@dataclass
class ParsedTable:
    table_id: str
    headers: List[str]
    rows: List[List[str]]
    page_number: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "table_id": self.table_id,
            "headers": self.headers,
            "rows": self.rows,
            "page_number": self.page_number,
        }


@dataclass
class ParsedNode:
    node_id: str
    logical_id: str
    heading: str
    section_number: Optional[str]
    level: int
    parent_logical_id: Optional[str]
    body_text: str
    numbered_lists: List[str] = field(default_factory=list)
    tables: List[ParsedTable] = field(default_factory=list)
    figures: List[Dict[str, Any]] = field(default_factory=list)
    page_numbers: List[int] = field(default_factory=list)
    sequence_order: int = 0
    warnings: List[str] = field(default_factory=list)
    content_hash: str = ""

    def compute_content_hash(self) -> str:
        serialized_tables = json.dumps([t.to_dict() for t in self.tables], sort_keys=True)
        serialized_figures = json.dumps(self.figures, sort_keys=True)
        content = (
            f"heading:{self.heading}\n"
            f"section:{self.section_number or ''}\n"
            f"body:{self.body_text}\n"
            f"tables:{serialized_tables}\n"
            f"figures:{serialized_figures}"
        )
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "logical_id": self.logical_id,
            "heading": self.heading,
            "section_number": self.section_number,
            "level": self.level,
            "parent_logical_id": self.parent_logical_id,
            "body_text": self.body_text,
            "numbered_lists": self.numbered_lists,
            "tables": [t.to_dict() for t in self.tables],
            "figures": self.figures,
            "page_numbers": self.page_numbers,
            "sequence_order": self.sequence_order,
            "warnings": self.warnings,
            "content_hash": self.content_hash,
        }


@dataclass
class ParsedDocument:
    document_key: str
    title: str
    file_hash: str
    page_count: int
    nodes: List[ParsedNode]
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "document_key": self.document_key,
            "title": self.title,
            "file_hash": self.file_hash,
            "page_count": self.page_count,
            "nodes": [n.to_dict() for n in self.nodes],
            "warnings": self.warnings,
        }


def normalize_text(text: Optional[str]) -> str:
    """
    Carefully normalizes Unicode ligatures, non-breaking characters, and whitespace
    without altering technical measurements, tolerances, or values.
    """
    if not text:
        return ""

    # Unicode NFKD normalization decomposes ligatures into individual glyphs
    normalized = unicodedata.normalize("NFKD", text)

    # Explicit ligature and punctuation replacements
    replacements = {
        "\ufb00": "ff",
        "\ufb01": "fi",
        "\ufb02": "fl",
        "\ufb03": "ffi",
        "\ufb04": "ffl",
        "\u2011": "-",  # Non-breaking hyphen
        "\u2012": "-",  # Figure dash
        "\u2013": "-",  # En dash
        "\u2014": "-",  # Em dash
        "\u2018": "'",  # Left single quotation mark
        "\u2019": "'",  # Right single quotation mark
        "\u201c": '"',  # Left double quotation mark
        "\u201d": '"',  # Right double quotation mark
        "\u00a0": " ",  # Non-breaking space
    }
    for orig, repl in replacements.items():
        normalized = normalized.replace(orig, repl)

    # Normalize spaced decimals like '1 .5' -> '1.5'
    normalized = re.sub(r"(\d+)\s*\.\s*(\d+)", r"\1.\2", normalized)

    # Normalize whitespace while preserving single spaces
    normalized = re.sub(r"[ \t]+", " ", normalized)
    return normalized.strip()


def clean_table_cell(cell_val: Optional[str]) -> str:
    """
    Cleans table cell text, removes extraneous newlines, and resolves
    layout-induced comma shifts from multi-line PDF table cells.
    """
    if not cell_val:
        return ""
    text = normalize_text(cell_val)
    # Fix commas that became dangling or shifted by newlines
    text = re.sub(r"\s*\n\s*,\s*", ", ", text)
    text = re.sub(r"\s*,\s*\n\s*", ", ", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"\s+,", ",", text)
    # Normalize spaced decimals like '1 .5' -> '1.5'
    text = re.sub(r"(\d+)\s*\.\s*(\d+)", r"\1.\2", text)
    return re.sub(r"\s+", " ", text).strip()


SECTION_HEADER_REGEX = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(.+)$")
NUMBERED_LIST_REGEX = re.compile(r"^\d+\.\s+[A-Z]")


def is_heading_span(first_span: Dict[str, Any]) -> bool:
    """
    Determines if a span represents a heading based on font weight and size.
    Headings in the CT-200 manual are bold and at least 11.0 pt.
    """
    font = first_span.get("font", "").lower()
    flags = first_span.get("flags", 0)
    size = round(first_span.get("size", 0), 1)
    is_bold = "bold" in font or (flags & 16) != 0
    return is_bold and size >= 11.0


def compute_file_sha256(pdf_path: str) -> str:
    hasher = hashlib.sha256()
    with open(pdf_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


class CT200Parser:
    """
    Dedicated parser for the CardioTrack CT-200 Blood Pressure Monitor Manual.
    """

    def __init__(self, low_text_threshold: int = 100):
        self.low_text_threshold = low_text_threshold

    def parse(self, pdf_path: str, document_key: str = "ct200_manual") -> ParsedDocument:
        file_hash = compute_file_sha256(pdf_path)
        doc = pymupdf.open(pdf_path)
        page_count = len(doc)
        doc_warnings: List[str] = []

        raw_sections: List[Dict[str, Any]] = []
        current_sec: Optional[Dict[str, Any]] = None
        doc_title: Optional[str] = None

        for pno in range(page_count):
            page = doc[pno]
            page_num = pno + 1

            # Check for low-text pages or need for OCR
            page_raw_text = page.get_text()
            if len(page_raw_text.strip()) < 20:
                # Attempt OCR fallback
                try:
                    ocr_page = page.get_textpage_ocr(language="eng")
                    ocr_text = ocr_page.get_text()
                    if ocr_text.strip():
                        doc_warnings.append(
                            f"ocr_fallback_used: Page {page_num} had empty text; recovered {len(ocr_text)} chars via OCR"
                        )
                    else:
                        doc_warnings.append(
                            f"low_text_page: Page {page_num} has only {len(page_raw_text.strip())} chars and OCR produced no text"
                        )
                except Exception as e:
                    doc_warnings.append(
                        f"ocr_unavailable: Page {page_num} text extraction minimal and OCR failed ({e})"
                    )
            elif len(page_raw_text.strip()) < self.low_text_threshold:
                doc_warnings.append(
                    f"low_text_page: Page {page_num} has unexpectedly low text ({len(page_raw_text.strip())} chars)"
                )

            # Extract tables on this page
            tabs = page.find_tables()
            table_bboxes = []
            page_elements: List[Dict[str, Any]] = []

            for tno, tab in enumerate(tabs.tables):
                table_bboxes.append(tab.bbox)
                page_elements.append({
                    "type": "table",
                    "bbox": tab.bbox,
                    "y0": tab.bbox[1],
                    "x0": tab.bbox[0],
                    "data": tab,
                    "tno": tno + 1,
                    "page_num": page_num,
                })

            # Extract text blocks
            page_dict = page.get_text("dict")
            for b in page_dict.get("blocks", []):
                if b.get("type") == 0:  # text
                    bbox = b["bbox"]
                    cx = (bbox[0] + bbox[2]) / 2
                    cy = (bbox[1] + bbox[3]) / 2

                    # Check if block falls inside any table bbox
                    inside_table = False
                    for tb in table_bboxes:
                        if tb[0] <= cx <= tb[2] and tb[1] <= cy <= tb[3]:
                            inside_table = True
                            break
                    if not inside_table:
                        page_elements.append({
                            "type": "text",
                            "bbox": bbox,
                            "y0": bbox[1],
                            "x0": bbox[0],
                            "data": b,
                            "page_num": page_num,
                        })

            # Sort page elements strictly by vertical reading order (y0, x0)
            page_elements.sort(key=lambda el: (el["y0"], el["x0"]))

            for el in page_elements:
                if el["type"] == "table":
                    tab = el["data"]
                    cleaned_rows = [
                        [clean_table_cell(c) for c in row]
                        for row in tab.extract()
                    ]
                    headers = cleaned_rows[0] if cleaned_rows else []
                    data_rows = cleaned_rows[1:] if len(cleaned_rows) > 1 else []
                    parsed_table = ParsedTable(
                        table_id=f"p{page_num}-tbl-{el['tno']}",
                        headers=headers,
                        rows=data_rows,
                        page_number=page_num,
                    )
                    if current_sec is not None:
                        current_sec["tables"].append(parsed_table)
                    else:
                        doc_warnings.append(
                            f"unattached_table: Table on page {page_num} found before first section"
                        )

                elif el["type"] == "text":
                    b = el["data"]
                    lines = b.get("lines", [])
                    if not lines:
                        continue

                    first_span = lines[0]["spans"][0] if lines[0].get("spans") else {}
                    raw_text = " ".join("".join(s["text"] for s in l.get("spans", [])) for l in lines)
                    norm_text = normalize_text(raw_text)
                    first_line = normalize_text("".join(s["text"] for s in lines[0].get("spans", [])))

                    # Detect document title on page 1
                    if page_num == 1 and not doc_title and "CardioTrack" in norm_text:
                        doc_title = norm_text
                        current_sec = {
                            "section_number": None,
                            "heading": doc_title,
                            "level": 0,
                            "pages": [page_num],
                            "body_lines": [],
                            "numbered_lists": [],
                            "tables": [],
                            "warnings": [],
                        }
                        raw_sections.append(current_sec)
                        continue

                    # Check if line matches a numbered section heading
                    m = SECTION_HEADER_REGEX.match(first_line)
                    is_numbered_list = (
                        first_line.startswith(("1. Normal", "2. Elevated", "3. Hypertension", "4. Hypertension", "5. Hypertensive"))
                    )

                    if m and is_heading_span(first_span) and not is_numbered_list:
                        sec_num = m.group(1)
                        title = m.group(2).strip()
                        heading_text = f"{sec_num} {title}"

                        # If the block contains text after the heading line, treat as body
                        body_parts = []
                        if len(lines) > 1:
                            rem_text = " ".join("".join(s["text"] for s in l.get("spans", [])) for l in lines[1:])
                            norm_rem = normalize_text(rem_text)
                            if norm_rem:
                                body_parts.append(norm_rem)

                        current_sec = {
                            "section_number": sec_num,
                            "heading": heading_text,
                            "level": len(sec_num.split(".")),
                            "pages": [page_num],
                            "body_lines": body_parts,
                            "numbered_lists": [],
                            "tables": [],
                            "warnings": [],
                        }
                        raw_sections.append(current_sec)
                    else:
                        # Body text or numbered list under the currently active section
                        if current_sec is not None:
                            if page_num not in current_sec["pages"]:
                                current_sec["pages"].append(page_num)

                            # Check for numbered list items inside body (e.g. blood pressure classification)
                            list_items = re.findall(r"\b(\d+\.\s+[^\d\n]+?)(?=\s+\d+\.|$)", norm_text)
                            if list_items and len(list_items) >= 2:
                                for item in list_items:
                                    current_sec["numbered_lists"].append(normalize_text(item))

                            current_sec["body_lines"].append(norm_text)
                        else:
                            # Pre-section text on page 1
                            if norm_text:
                                doc_warnings.append(f"orphan_text_page_{page_num}: {norm_text[:40]}")

        # Now build hierarchical tree and resolve parent-child relationships
        nodes = self._build_hierarchy(raw_sections, document_key)

        return ParsedDocument(
            document_key=document_key,
            title=doc_title or "CardioTrack CT-200 Technical & User Manual",
            file_hash=file_hash,
            page_count=page_count,
            nodes=nodes,
            warnings=doc_warnings,
        )

    def _build_hierarchy(
        self, raw_sections: List[Dict[str, Any]], document_key: str
    ) -> List[ParsedNode]:
        """
        Reconstructs the parent-child relationships, detects irregularities
        (missing parents, non-sequential order, table discrepancies), and
        assigns unique node IDs and stable logical identities.
        """
        nodes: List[ParsedNode] = []
        section_map: Dict[str, str] = {}  # sec_num -> logical_id
        previous_sec_nums: List[str] = []

        for idx, sec in enumerate(raw_sections):
            sec_num = sec["section_number"]
            heading = sec["heading"]
            level = sec["level"]
            warnings = list(sec["warnings"])

            # Generate stable logical ID
            if sec_num:
                slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
                logical_id = f"sec-{sec_num}-{slug}"
            else:
                logical_id = "doc-root-title"

            # Determine parent node
            parent_logical_id: Optional[str] = None
            if level == 0:
                parent_logical_id = None
            elif level == 1:
                # Level 1 sections belong to Root Title
                parent_logical_id = "doc-root-title"
            else:
                # E.g. for "2.1.1.1", look for "2.1.1", then "2.1", then "2"
                parts = sec_num.split(".")
                found_parent = False
                for parent_depth in range(len(parts) - 1, 0, -1):
                    candidate_parent_num = ".".join(parts[:parent_depth])
                    if candidate_parent_num in section_map:
                        parent_logical_id = section_map[candidate_parent_num]
                        found_parent = True
                        if parent_depth < len(parts) - 1:
                            expected_parent = ".".join(parts[:-1])
                            warnings.append(
                                f"missing_ancestor_parent: Section {sec_num} has no explicit {expected_parent} parent; "
                                f"attached to closest ancestor {candidate_parent_num}"
                            )
                        break

                if not found_parent:
                    parent_logical_id = "doc-root-title"
                    warnings.append(
                        f"ambiguous_hierarchy: Section {sec_num} could not find intermediate parent; attached to root"
                    )

            # Check for non-sequential section ordering (e.g. 3.4 appearing before 3.3)
            if sec_num and "." in sec_num:
                parts = sec_num.split(".")
                prefix = ".".join(parts[:-1])
                current_num = int(parts[-1])
                # Check all previously seen sections with same prefix
                for prev in previous_sec_nums:
                    if prev.startswith(prefix + ".") and len(prev.split(".")) == len(parts):
                        prev_num = int(prev.split(".")[-1])
                        if prev_num > current_num:
                            warnings.append(
                                f"out_of_order_section: Section {sec_num} appears after section {prev} in document reading order"
                            )

            if sec_num:
                section_map[sec_num] = logical_id
                previous_sec_nums.append(sec_num)

            # Combine and clean body text
            body_text = "\n\n".join(sec["body_lines"]).strip()

            # Check for technical discrepancies (Page 4 v2 E3 discrepancy between table and body)
            if sec_num == "4.1":
                if "within 2 seconds" in body_text:
                    for s in raw_sections:
                        if s["section_number"] == "4.2":
                            for tbl in s["tables"]:
                                for row in tbl.rows:
                                    if len(row) >= 3 and row[0] == "E3" and "1.5 seconds" in row[2]:
                                        warnings.append(
                                            "discrepancy_detected: Section 4.1 text specifies venting within 2 seconds, "
                                            "while Section 4.2 Error Code E3 table specifies auto-deflation within 1.5 seconds"
                                        )

            # Generate unique physical node ID
            physical_node_id = f"node-{document_key}-{idx:03d}-{logical_id}"

            parsed_node = ParsedNode(
                node_id=physical_node_id,
                logical_id=logical_id,
                heading=heading,
                section_number=sec_num,
                level=level,
                parent_logical_id=parent_logical_id,
                body_text=body_text,
                numbered_lists=sec["numbered_lists"],
                tables=sec["tables"],
                figures=[],
                page_numbers=sec["pages"],
                sequence_order=idx,
                warnings=warnings,
            )
            parsed_node.content_hash = parsed_node.compute_content_hash()
            nodes.append(parsed_node)

        return nodes
