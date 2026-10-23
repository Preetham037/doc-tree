# CardioTrack CT-200 Document Tree & QA Traceability Engine

A backend API built for the **CardioTrack CT-200 Home Blood Pressure Monitor** manual. 

In medical device engineering, manuals and firmware specifications change constantly. If firmware v1.4 tweaks the cuff deflation rate or battery threshold, QA teams need to know exactly which historical test cases just became invalid—without having to re-read the entire manual by hand.

This project solves that problem. It parses PDF manuals into structured requirement trees, tracks changes across document versions (v1 $\to$ v2) without wiping historical records, lets you pin requirements into custom test suites, generates validated QA test ideas using an LLM, and automatically flags test cases as **stale** when underlying specifications change.

---

## What's Under the Hood

- **Layout-Aware PDF Parser (`app/parser.py`)**: Uses PyMuPDF to extract text, headings, and tables in visual reading order. It normalizes typography (fixing font ligatures like `Cuﬀ` $\to$ `Cuff` and spaced decimals like `1 .5`), stitches cross-page sentences together, extracts multi-line tables, and falls back to OCR on scanned/empty pages.
- **Versioned Document Tree (`app/models.py`, `app/ingestion.py`)**: SQLite + SQLAlchemy ORM. Keeps immutable physical nodes (`node-v1-...`, `node-v2-...`) and connects them across revisions using stable logical IDs (`sec-2.1.1.1-...`). Ingestion is idempotent: re-uploading the exact same PDF won't create duplicate versions.
- **Pinned Selections**: QA engineers can create named requirement sets (e.g., "Battery & Inflation Subsystem"). The selection permanently snapshots the exact source text, version number, and content hash.
- **LLM Test Generation (`app/generation.py`)**: Prompts an LLM (Groq / Llama 3.3 or local mock) to generate 3 to 5 realistic, executable QA test cases. Pydantic validates the response schema with a 3-attempt bounded retry loop.
- **Dual Persistence (MongoDB + JSON Fallback)**: Test cases are saved to MongoDB when available. If MongoDB isn't running locally, it seamlessly falls back to an atomic local file (`data/generated_cases_fallback.json`) without crashing.
- **Staleness Engine**: When you query saved test cases, it compares the original pinned requirement hashes against the latest manual revision in SQLite. If a requirement changed, it flags the generation as `is_stale: true` and shows a unified text diff of what changed.

---

## Quickstart

### 1. Prerequisites
- Python 3.10+ (tested on Python 3.13.7 on macOS)
- Git
- Optional: Local MongoDB (if you want native NoSQL storage instead of the JSON fallback)

### 2. Setup the Virtual Environment

```bash
# Clone and enter the project folder
cd doc-tree

# Create a clean virtual environment
python3 -m venv .venv

# Activate it
# macOS / Linux:
source .venv/bin/activate
# Windows:
# .venv\Scripts\activate

# Confirm you're using the venv's python
which python
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Environment Configuration

Copy the sample `.env.example` file to `.env`:

```bash
cp .env.example .env
```

Here is what's inside `.env`:

```env
# Database
DATABASE_URL=sqlite:///./doc_tree.db

# MongoDB (falls back to local JSON file if not running)
MONGODB_URI=mongodb://localhost:27017
MONGODB_DB_NAME=doc_tree_qa

# LLM Provider (Optional - tests and local runs use mock mode by default if unset)
GROQ_API_KEY=
GROQ_MODEL=llama-3.3-70b-versatile
```

> **Tip**: You don't need a paid API key to run tests or test the API locally! If `GROQ_API_KEY` is blank, the app runs in deterministic mock mode. If you have a Groq key, just paste it after `GROQ_API_KEY=` to enable live LLM generation.

### 5. (Optional) Start MongoDB

If you have MongoDB installed (e.g. via Homebrew):
```bash
brew services start mongodb-community
```
If you don't run MongoDB, don't worry—the system automatically logs a note and writes generated cases to `data/generated_cases_fallback.json`.

---

## Running the API

Fire up the FastAPI server with Uvicorn:

```bash
uvicorn app.main:app --reload --port 8000
```

Once running, head over to the interactive Swagger UI at:
**[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)**

---

## Running the Automated Tests

The test suite covers PDF extraction quirks, edge-case hierarchies, REST endpoints, schema validation, and the full v1 $\to$ v2 staleness lifecycle.

To run all 18 tests:
```bash
pytest -v
```

Or run individual suites:
```bash
# Parser edge cases (missing parents, ligature fixes, table cleaning)
pytest -v tests/test_parser.py

# REST API endpoints (browsing, search, selection pinning)
pytest -v tests/test_api.py

# LLM schema validation, retries, and v1 -> v2 staleness detection
pytest -v tests/test_generation.py
```

---

## Step-by-Step Walkthrough (with cURL examples)

Here is a full demonstration of ingesting v1, generating QA test cases, upgrading to v2, and observing requirement staleness in action.

### Step 1: Ingest Version 1
Ingest the original CT-200 manual:
```bash
curl -X POST "http://127.0.0.1:8000/ingest?file_path=data/ct200_manual.pdf"
```

### Step 2: Check Available Versions
```bash
curl -X GET "http://127.0.0.1:8000/versions"
```
*You'll see Version 1 with 28 parsed nodes.*

### Step 3: Browse the Section Hierarchy
```bash
curl -X GET "http://127.0.0.1:8000/sections?version=1"
```

### Step 4: Search for a Specific Requirement
Search for mentions of battery behavior:
```bash
curl -X GET "http://127.0.0.1:8000/search?q=battery&version=1"
```

### Step 5: Inspect a Node with Structured Tables
Fetch Section 2.1 (General Specifications table) or Section 4.2 (Error Codes table):
```bash
curl -X GET "http://127.0.0.1:8000/nodes/2.1?version=1"
curl -X GET "http://127.0.0.1:8000/nodes/4.2?version=1"
```

### Step 6: Create a Named Selection Pinned to v1
Let's select two requirements:
1. `1.1 Intended Use` (which will **not** change in v2)
2. `2.1.1.1 Battery Life` (which **will** change in v2 from 300 cycles / 15% threshold down to 250 cycles / 10% threshold)

```bash
curl -X POST "http://127.0.0.1:8000/selections" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Power and Clinical Scope Suite",
    "node_ids": [
      "node-v1-006-sec-2.1.1.1-2-1-1-1-battery-life-under-typical-use",
      "node-v1-002-sec-1.1-1-1-intended-use"
    ]
  }'
```
*Take note of the returned `selection_id` (e.g. `sel-a1b2c3d4`).*

### Step 7: Generate QA Test Cases Against v1
Run test generation (using `mock_mode=true` if you haven't set a Groq API key):
```bash
curl -X POST "http://127.0.0.1:8000/generate?mock_mode=true" \
  -H "Content-Type: application/json" \
  -d '{
    "selection_id": "sel-a1b2c3d4"
  }'
```
*At this point, `"is_stale": false` because v1 is still the latest version.*

### Step 8: Ingest Version 2 of the Manual
Now ingest the revised manual:
```bash
curl -X POST "http://127.0.0.1:8000/ingest?file_path=data/ct200_manual_v2.pdf"
```
*Version 2 is created with 29 nodes. Version 1 is still preserved intact in the database.*

### Step 9: Compare Changes Across Versions
Review the diff across the entire manual, or check Section 2.1.1.1 specifically:
```bash
# Full diff summary
curl -X GET "http://127.0.0.1:8000/changes?from_version=1&to_version=2"

# Inspect Section 2.1.1.1 diff
curl -X GET "http://127.0.0.1:8000/nodes/2.1.1.1/changes?from_version=1&to_version=2"
```

### Step 10: Check Generation Staleness
Now fetch the original test generation from Step 7:
```bash
curl -X GET "http://127.0.0.1:8000/generations?selection_id=sel-a1b2c3d4"
```

Notice what happens:
- `"is_stale": true`
- `Section 1.1 Intended Use` is marked `"status": "current"` with no diff (it didn't change).
- `Section 2.1.1.1 Battery Life` is marked `"status": "changed"` with a clear text diff showing the cycle count reduction (300 $\to$ 250) and low-battery alert threshold change (15% $\to$ 10%).

---

## Notable Quirks in the CT-200 PDFs (and how we handle them)

1. **Missing Parent Section (`2.1.1.1`)**: The manual has a section numbered `2.1.1.1 Battery Life Under Typical Use`, but there is no `2.1.1` section heading anywhere in the document. Instead of inventing a fake parent, the parser attaches it directly to `2.1 General Specifications` at depth 4 and logs an explicit warning (`missing_ancestor_parent`).
2. **Out-of-Order Section (`3.4` before `3.3`)**: Section `3.4 Auto Shutoff` physically appears before `3.3 Result Display` in the text stream. We preserve the true physical reading order in `sequence_order` and attach an `out_of_order_section` warning.
3. **Internal Discrepancy on Page 4 of v2**: In v2, Section 4.1 text says the overpressure valve opens within **2 seconds**, while the Error Code E3 table on the same page specifies **1.5 seconds**. The parser flags this conflict (`discrepancy_detected`) without silently editing or guessing which number is right.
4. **Cross-Page Sentences**: In v2, Section 6.1 splits `do not` across page 5 and `use alcohol...` on page 6. The parser detects that the line didn't end with terminal punctuation and joins them seamlessly, avoiding artificial diffs between versions.

---

## Known Limitations

- **Heuristic Table Line Splitting**: Multi-line table cells are normalized to single strings. If a future PDF contains complex nested sub-tables without gridlines, PyMuPDF's heuristic table border detection may require custom bounding-box tuning.
- **Section Renumbering**: Matching across versions relies on section numbers and normalized slugified titles. If a future revision completely renumbers a section and changes its title simultaneously, the engine will flag it as an added/removed pair rather than a rename.
- **Syntactic vs. Clinical Staleness**: Staleness triggers on SHA-256 hash mismatches of the normalized text and table data. A hash detects that a specification changed, but doesn't judge whether the change is clinically significant or just a typo fix.
