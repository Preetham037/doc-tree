"""
Regression tests for CT-200 PDF Document Parser.
Validates extraction of hierarchy, tables, unicode ligatures, structural irregularities,
and version differences between v1 and v2.
"""

import os
import pytest
from app.parser import CT200Parser, normalize_text, clean_table_cell


@pytest.fixture(scope="module")
def parsed_docs():
    parser = CT200Parser()
    v1_path = os.path.join("data", "ct200_manual.pdf")
    v2_path = os.path.join("data", "ct200_manual_v2.pdf")
    doc_v1 = parser.parse(v1_path, document_key="ct200_v1")
    doc_v2 = parser.parse(v2_path, document_key="ct200_v2")
    return {"v1": doc_v1, "v2": doc_v2}


class TestParserRegression:
    def test_unicode_ligature_normalization(self):
        """Test Unicode ligature normalization without changing technical values."""
        raw = "Cuﬀ Speciﬁcations Shutoﬀ ±3 mmHg 1 .5 seconds"
        normalized = normalize_text(raw)
        assert "Cuff" in normalized
        assert "Specifications" in normalized
        assert "Shutoff" in normalized
        assert "±3 mmHg" in normalized
        assert "1.5 seconds" in normalized

    def test_missing_intermediate_parent_2_1_1_1(self, parsed_docs):
        """
        Structural Irregularity 1: Section 2.1.1.1 appears without a visible 2.1.1 parent.
        Must attach to ancestor 2.1 and record an explicit warning instead of inventing a fake parent.
        """
        doc = parsed_docs["v1"]
        node_2111 = next((n for n in doc.nodes if n.section_number == "2.1.1.1"), None)
        assert node_2111 is not None, "Section 2.1.1.1 not found"
        assert node_2111.level == 4
        assert node_2111.parent_logical_id == "sec-2.1-2-1-general-specifications"
        assert any("missing_ancestor_parent" in w for w in node_2111.warnings)

    def test_non_sequential_section_order_3_4_before_3_3(self, parsed_docs):
        """
        Structural Irregularity 2: Section 3.4 Auto Shutoff appears before
        Section 3.3 Result Display and Classification in source reading order.
        """
        doc = parsed_docs["v1"]
        node_34 = next((n for n in doc.nodes if n.section_number == "3.4"), None)
        node_33 = next((n for n in doc.nodes if n.section_number == "3.3"), None)
        assert node_34 is not None and node_33 is not None
        assert node_34.sequence_order < node_33.sequence_order
        assert any("out_of_order_section" in w for w in node_33.warnings)

    def test_table_extraction_and_multiline_cells(self, parsed_docs):
        """
        Structural Irregularity 3: Tables are parsed as structured rows and columns
        with multi-line cells properly cleaned.
        """
        doc_v1 = parsed_docs["v1"]
        # Section 2.1 General Specifications table
        node_21 = next(n for n in doc_v1.nodes if n.section_number == "2.1")
        assert len(node_21.tables) == 1
        spec_table = node_21.tables[0]
        assert spec_table.headers == ["Parameter", "Value"]
        assert len(spec_table.rows) == 7
        assert spec_table.rows[0] == ["Measurement method", "Oscillometric"]

        # Section 4.2 Error Codes table in v1
        node_42 = next(n for n in doc_v1.nodes if n.section_number == "4.2")
        assert len(node_42.tables) == 1
        err_table = node_42.tables[0]
        assert err_table.headers == ["Code", "Meaning", "Device Behavior"]
        assert len(err_table.rows) == 5

        # Check multi-line cell formatting
        e1_row = next(r for r in err_table.rows if r[0] == "E1")
        assert "leak detected" in e1_row[1]
        assert "Aborts measurement" in e1_row[2]

    def test_version_1_vs_version_2_specific_changes(self, parsed_docs):
        """
        Validates all required version changes between v1 and v2:
        - Battery life: ~300 to ~250 measurement cycles
        - Low battery icon threshold: 15% to 10%
        - Cuff inflation increments: 40 mmHg to 30 mmHg
        - E3 deflation in Error Codes table: 2 seconds to 1.5 seconds
        - E6 Bluetooth sync failure added
        - Section 5.3 Data Export added
        """
        v1 = parsed_docs["v1"]
        v2 = parsed_docs["v2"]

        # 1. Battery life & threshold in 2.1.1.1
        node_battery_v1 = next(n for n in v1.nodes if n.section_number == "2.1.1.1")
        node_battery_v2 = next(n for n in v2.nodes if n.section_number == "2.1.1.1")
        assert "300 measurement cycles" in node_battery_v1.body_text
        assert "15%" in node_battery_v1.body_text
        assert "250 measurement cycles" in node_battery_v2.body_text
        assert "10%" in node_battery_v2.body_text

        # 2. Cuff inflation in 3.2
        node_cuff_v1 = next(n for n in v1.nodes if n.section_number == "3.2")
        node_cuff_v2 = next(n for n in v2.nodes if n.section_number == "3.2")
        assert "40 mmHg increments" in node_cuff_v1.body_text
        assert "30 mmHg increments" in node_cuff_v2.body_text

        # 3. E3 auto-deflation in 4.2 table
        tbl_v1 = next(n for n in v1.nodes if n.section_number == "4.2").tables[0]
        tbl_v2 = next(n for n in v2.nodes if n.section_number == "4.2").tables[0]
        e3_v1 = next(r for r in tbl_v1.rows if r[0] == "E3")
        e3_v2 = next(r for r in tbl_v2.rows if r[0] == "E3")
        assert "2 seconds" in e3_v1[2]
        assert "1.5 seconds" in e3_v2[2]

        # 4. Error code E6 added in v2
        assert not any(r[0] == "E6" for r in tbl_v1.rows)
        e6_v2 = next((r for r in tbl_v2.rows if r[0] == "E6"), None)
        assert e6_v2 is not None
        assert "Bluetooth sync failure" in e6_v2[1]

        # 5. Section 5.3 Data Export added in v2
        assert not any(n.section_number == "5.3" for n in v1.nodes)
        node_53_v2 = next((n for n in v2.nodes if n.section_number == "5.3"), None)
        assert node_53_v2 is not None
        assert "CSV" in node_53_v2.body_text

    def test_page_4_v2_e3_discrepancy_reported(self, parsed_docs):
        """
        Validates that the discrepancy on Page 4 of v2 (Section 4.1 text says 2 seconds,
        while Section 4.2 Error Code table says 1.5 seconds) is detected and reported.
        """
        v2 = parsed_docs["v2"]
        node_41_v2 = next(n for n in v2.nodes if n.section_number == "4.1")
        assert any("discrepancy_detected" in w for w in node_41_v2.warnings)

    def test_low_text_page_warning_on_v2_page_7(self, parsed_docs):
        """Page 7 of v2 has minimal text and should trigger a low_text_page document warning."""
        v2 = parsed_docs["v2"]
        assert any("low_text_page" in w for w in v2.warnings)
