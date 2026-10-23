# CardioTrack CT-200 Document Tree & QA Traceability API

Backend API for the fictional **CardioTrack CT-200 Home Blood Pressure Monitor** technical and user manual.

This system ingests multi-page PDF technical manuals, parses them into structured hierarchical trees (reconstructing sections, subsections, paragraphs, numbered lists, and tables), maintains version co-existence across manual revisions (v1 and v2), supports cross-version change diffing, pins exact historical requirements into named selections, generates validated QA test cases via LLM, and evaluates requirement staleness upon manual updates.

---

## Architecture Overview

- **PDF Ingestion & Hierarchy Reconstruction**: PyMuPDF (`pymupdf`) with layout-aware spatial sorting, ligature normalization, table detection, OCR fallback, and irregularity handling.
- **Relational Version Store**: SQLite + SQLAlchemy ORM for immutable `DocumentVersion`, `DocumentNode`, `Selection`, and `SelectionNode` records.
- **QA Persistence Store**: MongoDB (via `pymongo`) for generated QA cases, with a documented local JSON fallback if MongoDB is unreachable.
- **LLM Test Case Generation**: Pydantic schema validation enforcing 3–5 executable test cases, exact quote matching, and bounded retries.
- **Staleness Engine**: Evaluates content hashes and logical identities of pinned requirements against the latest document version at retrieval time.

---

## 1. Prerequisites and Setup

- macOS, Linux, or Windows with **Python 3.10+** (Python 3.13.7 verified).
- Git.
- Optional: MongoDB community edition (or uses local JSON document fallback).

### Virtual Environment Setup & Activation

From the repository root:

```bash
# 1. Create a clean virtual environment
python3 -m venv .venv

# 2. Activate the virtual environment
# macOS / Linux:
source .venv/bin/activate
# Windows:
# .venv\Scripts\activate

# 3. Verify active interpreter points to .venv
which python
python --version
```

### Dependency Installation

```bash
pip install -r requirements.txt
```

---

## 2. Environment Variables

Copy the example environment configuration:

```bash
cp .env.example .env
```

Available configuration keys in `.env`:

| Variable | Description | Default |
|---|---|---|
| `DATABASE_URL` | SQLite database URI | `sqlite:///./doc_tree.db` |
| `MONGODB_URI` | MongoDB connection string | `mongodb://localhost:27017` |
| `MONGODB_DB_NAME` | MongoDB database name | `doc_tree_qa` |
| `GROQ_API_KEY` | Groq API key for LLM generation | `""` (empty enables mock mode) |
| `GROQ_MODEL` | LLM model identifier | `llama-3.3-70b-versatile` |

---

## 3. MongoDB Setup (Optional)

If MongoDB is installed via Homebrew on macOS:
```bash
brew services start mongodb/brew/mongodb-community
```
> **Note**: If MongoDB is not running or unreachable, the system automatically falls back to storing generated test cases in `data/generated_cases_fallback.json` and flags `storage_backend: "json_fallback"` in the response.

---

## 4. Running the API

Start the FastAPI application with Uvicorn:

```bash
uvicorn app.main:app --reload --port 8000
```

The interactive Swagger API documentation will be available at:
[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

---

## 5. Running Automated Tests

Run the full automated test suite (18 tests covering parser irregularities, API endpoints, schema validation, and end-to-end staleness):

```bash
pytest -v
```

Run specific test modules:
```bash
# Parser regression & irregularity tests
pytest -v tests/test_parser.py

# Browse, search, and selection pinning API tests
pytest -v tests/test_api.py

# QA generation, validation, and staleness tests
pytest -v tests/test_generation.py
```

---

## 6. End-to-End Workflow & Example cURL Requests

### Step 1: Ingest Version 1 of the Manual
```bash
curl -X POST "http://127.0.0.1:8000/ingest?file_path=data/ct200_manual.pdf"
```

### Step 2: List Ingested Document Versions
```bash
curl -X GET "http://127.0.0.1:8000/versions"
```

### Step 3: Browse Sections for Version 1
```bash
curl -X GET "http://127.0.0.1:8000/sections?version=1"
```

### Step 4: Search for Requirements (e.g., Battery)
```bash
curl -X GET "http://127.0.0.1:8000/search?q=battery&version=1"
```

### Step 5: Retrieve Detailed Node with Tables
```bash
# Retrieve Section 2.1 (contains General Specifications table)
curl -X GET "http://127.0.0.1:8000/nodes/2.1?version=1"

# Retrieve Section 4.2 (contains Error Codes table)
curl -X GET "http://127.0.0.1:8000/nodes/4.2?version=1"
```

### Step 6: Create a Named Selection Pinned to Historical Version 1
Create a selection pinning `2.1.1.1` (Battery Life) and `1.1` (Intended Use):
```bash
curl -X POST "http://127.0.0.1:8000/selections" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Power & Indication Subsystem",
    "node_ids": ["node-v1-006-sec-2.1.1.1-2-1-1-1-battery-life-under-typical-use", "node-v1-002-sec-1.1-1-1-intended-use"]
  }'
```
*Note the returned `selection_id` (e.g. `sel-a1b2c3d4`).*

### Step 7: Generate QA Test Cases Against Version 1
```bash
curl -X POST "http://127.0.0.1:8000/generate?mock_mode=true" \
  -H "Content-Type: application/json" \
  -d '{
    "selection_id": "sel-a1b2c3d4"
  }'
```
*Initially, the response shows `"is_stale": false` and all source requirements are marked `"status": "current"`.*

### Step 8: Ingest Version 2 of the Manual
```bash
curl -X POST "http://127.0.0.1:8000/ingest?file_path=data/ct200_manual_v2.pdf"
```

### Step 9: Inspect Cross-Version Changes Between v1 and v2
```bash
# Check changes across the entire manual
curl -X GET "http://127.0.0.1:8000/changes?from_version=1&to_version=2"

# Inspect changes to Section 2.1.1.1 specifically
curl -X GET "http://127.0.0.1:8000/nodes/2.1.1.1/changes?from_version=1&to_version=2"
```

### Step 10: Inspect Generation Staleness
Retrieve the original generation created in Step 7:
```bash
curl -X GET "http://127.0.0.1:8000/generations?selection_id=sel-a1b2c3d4"
```
*The response now returns:*
- `"is_stale": true`
- Detailed breakdown showing:
  - `Section 1.1 Intended Use`: `"status": "current"` (no changes)
  - `Section 2.1.1.1 Battery Life`: `"status": "changed"` with a text diff highlighting 300 -> 250 cycles and 15% -> 10% threshold.

---

## 7. Known Limitations

1. **Section Renumbering Without Heading Text Continuity**: If a future manual version completely moves and renumbers a section while simultaneously renaming its title, the logical ID matcher flags it as `removed` and `added` rather than `changed`.
2. **Sub-table Layout Heuristics**: Multi-line table cells are flattened to single clean strings; internal column borders that are drawn without bounding lines depend on PyMuPDF's heuristic table boundary detection.
3. **No Clinical Semantic Analysis in Staleness**: Staleness triggers on SHA-256 hash discrepancies of normalized requirement text and structured content. A hash detects textual differences, not clinical severity or regulatory significance.
