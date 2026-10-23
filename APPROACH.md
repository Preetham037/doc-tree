# Architectural Approach & Technical Decisions

Documenting the engineering design, edge-case resolutions, validation methodology, and decision log for the **CardioTrack CT-200 Technical Manual Document Tree & QA Traceability Engine**.

---

## 1. Parser and OCR Strategy

### Layout-Aware Inspection
Standard sequential PDF text extraction often scrambles tabular data and fails to associate tables with their parent sections. The parser (`app/parser.py`) implements a layout-aware extraction pipeline:
1. **Per-Page Element Collection**: For each page, tables (`page.find_tables()`), figure drawings, and text blocks (`page.get_text("dict")`) are gathered.
2. **Spatial Deduplication**: Text blocks whose bounding box centers fall inside table boundaries are excluded from the plain-text body stream to avoid raw string duplication.
3. **Vertical Reading-Order Sort**: All page elements (headings, text blocks, and tables) are sorted strictly by vertical coordinate `(y0, x0)`. This guarantees that tables located physically between section headings (e.g., the General Specifications table between Section 2.1 and Section 2.1.1.1) are attached to the immediately preceding heading.
4. **Cross-Page Flow & Sentence Continuation**: In multi-page technical manuals, sentences often break across page boundaries without terminal punctuation (e.g. `do not` at the bottom of Page 5 continuing to `use alcohol...` at the top of Page 6 in v2). The parser detects non-terminal line ends and joins cross-page text blocks with a space rather than paragraph breaks (`\n\n`), preventing artificial diffs across versions.

### Unicode Ligature Normalization
The CT-200 PDF fonts use typesetting ligatures such as `Cuﬀ` (`\ufb00`), `Speciﬁcations` (`\ufb01`), and `Shutoﬀ` (`\ufb00`), along with non-breaking hyphens (`\u2011`) and spaced decimal numbers (e.g. `1 .5 seconds`). The parser applies Unicode NFKD decomposition and explicit character mappings while preserving all technical numbers, units, and tolerances (`±3 mmHg`, `180 mmHg`, `30 mmHg`).

### OCR Fallback
PyMuPDF's built-in Tesseract OCR engine (`page.get_textpage_ocr(language='eng')`) is integrated as a fallback. When a page yields fewer than 20 characters of extractable text, OCR is automatically invoked. If OCR is unavailable or language data is missing, the parser emits an explicit document warning (`ocr_unavailable` or `low_text_page`) rather than silently dropping content. Page 7 of v2 (containing only 81 characters of text) triggers an explicit `low_text_page` warning.

---

## 2. Hierarchy Reconstruction & Observed Irregularities

### Irregularity 1: Missing Intermediate Parent (`2.1.1.1` without `2.1.1`)
- **Observed Behavior**: `2.1.1.1 Battery Life Under Typical Use` appears in the manual with four-level numbering, but no section `2.1.1` exists in the text.
- **Handling**: Rather than inventing a fictitious `2.1.1` parent node, the parser traverses upwards through the section number prefixes (`2.1.1` -> `2.1` -> `2`). It discovers `2.1 General Specifications` as the closest explicit ancestor, attaches `2.1.1.1` directly to `2.1`, sets its hierarchy level to `4`, and records an explicit warning:
  `missing_ancestor_parent: Section 2.1.1.1 has no explicit 2.1.1 parent; attached to closest ancestor 2.1`.

### Irregularity 2: Non-Sequential Section Order (`3.4` before `3.3`)
- **Observed Behavior**: In the document text stream, `3.4 Auto Shutoff` appears immediately after `3.2 Cuff Inflation Sequence`, preceding `3.3 Result Display and Classification`.
- **Handling**: The parser preserves the true physical source order (`sequence_order`: 3.4 is index 11, 3.3 is index 12) while associating both as children of `3. Device Operation`. It records an explicit warning on 3.3:
  `out_of_order_section: Section 3.3 appears after section 3.4 in document reading order`.

### Irregularity 3: Tables and Multi-Line Cells
- **Observed Behavior**: The General Specifications (p. 2) and Error Codes (p. 4) tables have cells spanning multiple lines with coordinate shifts (e.g., `Aborts measurement displays E1\n,`).
- **Handling**: `clean_table_cell` cleans dangling commas, normalizes spaced decimals, and formats each table into structured JSON `{"headers": [...], "rows": [...]}` attached directly to `2.1` and `4.2`.

### Irregularity 4: Page 4 v2 E3 Specification Discrepancy
- **Observed Behavior**: In manual v2 Page 4, Section 4.1 body text specifies emergency deflation *within 2 seconds*, whereas the Section 4.2 Error Code E3 table specifies auto-deflation *within 1.5 seconds*.
- **Handling**: The parser detects this conflict across the body text and table rows, emitting an explicit node warning on Section 4.1:
  `discrepancy_detected: Section 4.1 text specifies venting within 2 seconds, while Section 4.2 Error Code E3 table specifies auto-deflation within 1.5 seconds`.
  The system preserves both statements without silently reconciling them.

---

## 3. Data Model & Version Coexistence

### SQLite Relational Schema (`app/models.py`)
- `DocumentVersion`: Stores version number, document key (`ct200_manual`), source filename, file SHA-256 hash, page count, and title.
- `DocumentNode`: Represents an immutable physical node record.
  - `id`: Physical primary key (e.g. `node-v1-006-sec-2.1.1.1-...`), unique per version record.
  - `logical_id`: Stable logical identity across manual versions (e.g. `sec-2.1.1.1-2-1-1-1-battery-life-under-typical-use`).
  - `parent_id`: Foreign key pointing to physical parent in the same version.
  - `parent_logical_id`: Stable logical pointer to parent.
  - `body_text`, `structured_data` (JSON tables and lists), `content_hash` (SHA-256), `page_numbers`, `sequence_order`, and `warnings`.
- `Selection` & `SelectionNode`: Represents named requirement sets. Pins the exact `node_id`, `version_number`, `content_hash`, and a frozen `source_text_snapshot`.

### Version Coexistence & Idempotency
- Re-ingesting identical PDF bytes checks the file hash against existing versions; if already present, it returns the existing record without creating duplicate entries.
- Ingesting a new manual revision creates a distinct `version_number` (v1, v2). Physical node IDs are never overwritten or reused, ensuring historical versions remain fully queryable.

---

## 4. LLM QA Generation & Staleness Detection

### Generation Strategy
1. **Input**: A named selection of pinned nodes.
2. **Prompting**: Directs the LLM to act as a Medical Device QA Traceability Engineer, generating strictly 3 to 5 executable test-case ideas based solely on the provided requirement snapshots.
3. **Structured Validation**: Pydantic validates the response against `LLMGenerationOutput`:
   - 3 to 5 test cases required.
   - Non-empty execution steps.
   - Exact supporting quote matching source text.
4. **Bounded Retries**: If provider response fails schema validation or produces malformed JSON, the generator retries up to 3 times before returning a clean HTTP 500 error.

### NoSQL Persistence & Fallback
- Persists generated cases to **MongoDB** (`test_cases` collection).
- If MongoDB is unreachable, the storage manager automatically falls back to an atomic local JSON document store (`data/generated_cases_fallback.json`) and flags `storage_backend: "json_fallback"`.

### Staleness Evaluation
When retrieving generations (`GET /generations?selection_id=...`):
- For each source requirement in the generation, the engine checks the latest document version in SQLite using `logical_id`.
- If the content hash differs, the node is marked `status: "changed"` and an exact text diff is generated.
- The overall generation is flagged `is_stale: true`, identifying exactly which requirements were revised.

---

## 5. Decision Log

### Question 1: What is most likely to silently produce wrong results, and how will we catch it?
**Answer**:
The failure mode most likely to produce silent errors is **incorrect spatial text grouping during PDF extraction**—specifically when a table's text blocks bleed into the surrounding section body text or when cross-page sentences are split into separate fragments that alter content hashes.
- **How we catch it**:
  1. We compute bounding-box overlap between PyMuPDF tables and text blocks, discarding text blocks whose coordinates fall within table boundaries.
  2. We apply `join_body_blocks` to detect terminal punctuation before deciding whether to join text with a space or a paragraph break.
  3. We run explicit regression tests (`test_version_1_vs_version_2_specific_changes`) comparing v1 against v2, ensuring that 24 unmodified sections retain identical content hashes with zero false positive diffs.

### Question 2: Where did we choose simplicity over correctness, and what breaks first in production?
**Answer**:
We chose simplicity by using **deterministic string-slugged logical IDs derived from section numbers and headings** (e.g. `sec-2.1.1.1-battery-life-under-typical-use`) combined with SHA-256 content hashing.
- **What breaks first in production**:
  If a clinical author renumbers Section `3.2` to `3.5` while keeping the requirement identical, or renames the heading while keeping the section number, a purely string-based logical matcher may treat the section as an "added" and "removed" node pair rather than a single moved/renamed node. In production, a hybrid matcher using vector embeddings or bipartite graph matching on normalized body text would be required to track section identity across major restructuring.

### Question 3: Name one input we do not handle, and explain what the system does with it.
**Answer**:
We do not handle **embedded raster image flowcharts or circuit diagrams lacking textual vector elements**.
- **What the system does**:
  The parser extracts all text and vector line drawings, but image blocks (`type == 1`) without OCR-readable captions or annotations are logged under `figures: []` with their bounding box metadata. If an entire page contains only a scanned schematic with no text, the system attempts OCR fallback. If OCR cannot extract textual characters, it records an explicit `low_text_page` or `ocr_unavailable` extraction warning in the node and document metadata rather than hallucinating content or failing silently.

---

## 6. What Would Change for Production

1. **Distributed Task Queue**: PDF parsing and LLM generation should be executed asynchronously via Celery or Temporal with Redis/RabbitMQ.
2. **PostgreSQL with pgvector / Full-Text Search**: SQLite would be migrated to PostgreSQL, using `tsvector` for lexical search and `pgvector` for semantic requirement retrieval.
3. **Multi-Tenant Document Scoping**: Add organizational workspaces, access control policies, and audit logging for regulatory FDA 21 CFR Part 11 compliance.
4. **Interactive Discrepancy Reconciliation UI**: Provide QA teams with an interactive review screen to acknowledge, waive, or create Jira/ALM tickets for detected requirement staleness.
