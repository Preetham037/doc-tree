"""
LLM QA test-case generation with Pydantic validation, bounded retries,
NoSQL (MongoDB) persistence with documented JSON fallback, and staleness detection.
"""

from datetime import datetime, timezone
import difflib
import json
import logging
import os
from typing import Any, Dict, List, Optional
import uuid

import httpx
from pydantic import BaseModel, Field, field_validator
import pymongo
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
from sqlalchemy.orm import Session

from app.models import DocumentNode, DocumentVersion, Selection

logger = logging.getLogger("app.generation")

# MongoDB configuration
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "doc_tree_qa")
JSON_FALLBACK_FILE = os.path.join("data", "generated_cases_fallback.json")

# LLM provider configuration (Groq default or OpenAI compatible)
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"


class QATestCase(BaseModel):
    title: str = Field(..., min_length=5, description="Clear, descriptive title of the test case")
    preconditions: List[str] = Field(..., min_length=1, description="List of required setup conditions")
    test_steps: List[str] = Field(..., min_length=1, description="Sequential, executable verification steps")
    expected_result: str = Field(..., min_length=5, description="Expected device behavior or output")
    exact_supporting_quote: str = Field(..., min_length=5, description="Verbatim quote from the manual requirement")
    risk_or_priority: str = Field(..., description="High, Medium, Low, or clinical risk category")
    source_node_id: str = Field(..., description="Physical node ID of the source requirement")
    source_logical_id: str = Field(..., description="Logical ID of the source requirement")
    source_version: int = Field(..., description="Version number of the manual used")
    source_content_hash: str = Field(..., description="Content hash of the source requirement")

    @field_validator("test_steps")
    @classmethod
    def validate_steps(cls, v: List[str]) -> List[str]:
        if not v or len(v) == 0:
            raise ValueError("Test case must contain at least one executable test step.")
        return v


class LLMGenerationOutput(BaseModel):
    test_cases: List[QATestCase] = Field(..., description="List of generated test cases")

    @field_validator("test_cases")
    @classmethod
    def validate_case_count(cls, v: List[QATestCase]) -> List[QATestCase]:
        if len(v) < 3 or len(v) > 5:
            raise ValueError(f"Generation must contain between 3 and 5 test cases; received {len(v)}.")
        return v


class NodeStalenessDetail(BaseModel):
    logical_id: str
    heading: str
    original_node_id: str
    original_version: int
    original_content_hash: str
    current_node_id: Optional[str] = None
    current_version: int
    current_content_hash: Optional[str] = None
    status: str  # "current", "changed", "removed", "ambiguous"
    diff_summary: Optional[str] = None


class GenerationRetrievalResponse(BaseModel):
    generation_id: str
    selection_id: str
    selection_name: str
    storage_backend: str  # "mongodb" or "json_fallback"
    created_at: str
    is_stale: bool
    stale_reason: Optional[str] = None
    test_cases: List[Dict[str, Any]]
    source_nodes_staleness: List[NodeStalenessDetail]


class NoSQLStorage:
    """
    Persists and retrieves generated QA cases. Uses MongoDB when available;
    otherwise falls back to a local JSON document store with explicit documentation.
    """

    def __init__(self):
        self.client: Optional[pymongo.MongoClient] = None
        self.db = None
        self._init_connection()

    def _init_connection(self):
        try:
            self.client = pymongo.MongoClient(MONGODB_URI, serverSelectionTimeoutMS=1000)
            # Force server check
            self.client.admin.command("ping")
            self.db = self.client[MONGODB_DB_NAME]
            logger.info(f"Connected to MongoDB at {MONGODB_URI}")
        except Exception as e:
            logger.warning(
                f"MongoDB unavailable ({e}). Using local JSON document store fallback: {JSON_FALLBACK_FILE}"
            )
            self.client = None
            self.db = None

    @property
    def backend_type(self) -> str:
        return "mongodb" if self.db is not None else "json_fallback"

    def save_generation(self, record: Dict[str, Any]) -> str:
        record["storage_backend"] = self.backend_type
        record["created_at"] = record.get("created_at") or datetime.now(timezone.utc).isoformat()

        if self.db is not None:
            try:
                self.db.test_cases.insert_one(record)
                return record["generation_id"]
            except Exception as e:
                logger.error(f"MongoDB write failed ({e}); switching to JSON fallback")
                self.db = None

        # JSON fallback
        os.makedirs(os.path.dirname(JSON_FALLBACK_FILE) or ".", exist_ok=True)
        records = []
        if os.path.exists(JSON_FALLBACK_FILE):
            try:
                with open(JSON_FALLBACK_FILE, "r") as f:
                    records = json.load(f)
            except Exception:
                records = []

        records.append(record)
        with open(JSON_FALLBACK_FILE, "w") as f:
            json.dump(records, f, indent=2, default=str)

        return record["generation_id"]

    def find_generation_by_selection(self, selection_id: str) -> Optional[Dict[str, Any]]:
        if self.db is not None:
            try:
                doc = self.db.test_cases.find_one({"selection_id": selection_id}, {"_id": 0})
                if doc:
                    return doc
            except Exception:
                pass

        # Check JSON fallback
        if os.path.exists(JSON_FALLBACK_FILE):
            try:
                with open(JSON_FALLBACK_FILE, "r") as f:
                    records = json.load(f)
                    for r in reversed(records):
                        if r.get("selection_id") == selection_id:
                            return r
            except Exception:
                pass
        return None

    def find_generations(
        self,
        selection_id: Optional[str] = None,
        node_id: Optional[str] = None,
        generation_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        results = []
        if self.db is not None:
            try:
                query: Dict[str, Any] = {}
                if generation_id:
                    query["generation_id"] = generation_id
                if selection_id:
                    query["selection_id"] = selection_id
                if node_id:
                    query["source_nodes.node_id"] = node_id

                cursor = self.db.test_cases.find(query, {"_id": 0})
                results = list(cursor)
                if results:
                    return results
            except Exception:
                pass

        # Check JSON fallback
        if os.path.exists(JSON_FALLBACK_FILE):
            try:
                with open(JSON_FALLBACK_FILE, "r") as f:
                    records = json.load(f)
                    for r in records:
                        if generation_id and r.get("generation_id") != generation_id:
                            continue
                        if selection_id and r.get("selection_id") != selection_id:
                            continue
                        if node_id and not any(sn.get("node_id") == node_id for sn in r.get("source_nodes", [])):
                            continue
                        results.append(r)
            except Exception:
                pass

        return results


storage = NoSQLStorage()


class QAGeneratorService:
    """
    Generates structured, validated QA test-case ideas with bounded retries.
    """

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None, mock_mode: bool = False):
        self.api_key = api_key or GROQ_API_KEY
        self.model = model or GROQ_MODEL
        self.mock_mode = mock_mode

    def generate_for_selection(
        self,
        selection: Selection,
        db: Session,
        force_regenerate: bool = False,
        max_retries: int = 3,
    ) -> Dict[str, Any]:
        """
        Generates 3 to 5 test cases from a named selection of pinned nodes.
        Preserves idempotency if source hashes match unless force_regenerate is True.
        """
        # Build source node snapshots
        source_nodes = []
        for p in selection.pinned_nodes:
            source_nodes.append({
                "node_id": p.node_id,
                "version_number": p.version_number,
                "logical_id": p.logical_id,
                "heading": p.heading,
                "content_hash": p.content_hash,
                "source_text_snapshot": p.source_text_snapshot,
            })

        source_hash_composite = "-".join(sorted(sn["content_hash"] for sn in source_nodes))

        # Check idempotency
        if not force_regenerate:
            existing = storage.find_generation_by_selection(selection.id)
            if existing:
                existing_comp = "-".join(sorted(sn["content_hash"] for sn in existing.get("source_nodes", [])))
                if existing_comp == source_hash_composite:
                    logger.info(f"Returning cached generation for selection {selection.id}")
                    return existing

        # Execute generation with bounded retry loop
        prompt = self._build_prompt(selection.name, source_nodes)
        generation_output = self._call_llm_with_retries(prompt, source_nodes, max_retries=max_retries)

        generation_id = f"gen-{uuid.uuid4().hex[:10]}"
        record = {
            "generation_id": generation_id,
            "selection_id": selection.id,
            "selection_name": selection.name,
            "source_nodes": source_nodes,
            "test_cases": [tc.model_dump() for tc in generation_output.test_cases],
            "model": self.model,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        storage.save_generation(record)
        return record

    def _build_prompt(self, selection_name: str, source_nodes: List[Dict[str, Any]]) -> str:
        prompt_parts = [
            f"You are a Senior Medical Device QA Traceability Engineer generating test cases for the CardioTrack CT-200 Monitor.",
            f"Selection Name: {selection_name}",
            f"Generate exactly 3 to 5 executable QA test-case ideas based SOLELY on the following manual requirements.",
            f"Do NOT invent thresholds, tolerances, or behaviors not supported by the text.",
            f"If an instruction or specification is ambiguous, make the ambiguity explicit in the test idea.",
            f"\n--- SOURCE REQUIREMENTS ---",
        ]

        for sn in source_nodes:
            prompt_parts.append(
                f"\nRequirement [Node ID: {sn['node_id']} | Logical ID: {sn['logical_id']} | Version: {sn['version_number']} | Hash: {sn['content_hash']}]"
                f"\nHeading: {sn['heading']}"
                f"\nText:\n{sn['source_text_snapshot']}"
            )

        prompt_parts.append(
            "\nOutput must be valid JSON adhering STRICTLY to this structure:\n"
            "{\n"
            '  "test_cases": [\n'
            "    {\n"
            '      "title": "Title of test case",\n'
            '      "preconditions": ["List of preconditions"],\n'
            '      "test_steps": ["Step 1", "Step 2"],\n'
            '      "expected_result": "Exact expected result",\n'
            '      "exact_supporting_quote": "Exact verbatim quote from the text",\n'
            '      "risk_or_priority": "High / Medium / Low",\n'
            '      "source_node_id": "Node ID from above",\n'
            '      "source_logical_id": "Logical ID from above",\n'
            '      "source_version": 1,\n'
            '      "source_content_hash": "Hash from above"\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "The list of test_cases must contain at least 3 and at most 5 items."
        )

        return "\n".join(prompt_parts)

    def _call_llm_with_retries(
        self, prompt: str, source_nodes: List[Dict[str, Any]], max_retries: int = 3
    ) -> LLMGenerationOutput:
        # Mock mode for automated unit tests without external API dependencies
        if self.mock_mode or not self.api_key:
            return self._generate_mock_output(source_nodes)

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                }
                payload = {
                    "model": self.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are a QA engineer. Respond ONLY with valid JSON conforming to the requested schema.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.2,
                    "response_format": {"type": "json_object"},
                }
                response = httpx.post(GROQ_API_URL, headers=headers, json=payload, timeout=45.0)
                if response.status_code != 200:
                    raise ValueError(f"Provider returned error {response.status_code}: {response.text}")

                res_json = response.json()
                content = res_json["choices"][0]["message"]["content"]
                validated = LLMGenerationOutput.model_validate_json(content)
                return validated
            except Exception as e:
                logger.warning(f"LLM generation attempt {attempt} failed: {e}")
                last_error = e

        raise RuntimeError(f"LLM generation failed after {max_retries} attempts. Last error: {last_error}")

    def _generate_mock_output(self, source_nodes: List[Dict[str, Any]]) -> LLMGenerationOutput:
        """Generates deterministic, compliant test cases for unit testing."""
        cases: List[QATestCase] = []
        for i, sn in enumerate(source_nodes[:3]):
            quote = sn["source_text_snapshot"][:80].strip() or sn["heading"]
            cases.append(
                QATestCase(
                    title=f"Verify {sn['heading']} functionality",
                    preconditions=["Device powered on with fresh batteries", "Arm cuff connected"],
                    test_steps=[
                        f"Initiate standard operating procedure for {sn['heading']}",
                        "Observe device display and log sensor outputs",
                    ],
                    expected_result=f"Device behaves in accordance with {sn['heading']} specification",
                    exact_supporting_quote=quote,
                    risk_or_priority="High" if "Safety" in sn["heading"] or "Alarm" in sn["heading"] else "Medium",
                    source_node_id=sn["node_id"],
                    source_logical_id=sn["logical_id"],
                    source_version=sn["version_number"],
                    source_content_hash=sn["content_hash"],
                )
            )

        # Ensure between 3 and 5 cases
        while len(cases) < 3:
            sn = source_nodes[0]
            idx = len(cases) + 1
            cases.append(
                QATestCase(
                    title=f"Edge case test {idx} for {sn['heading']}",
                    preconditions=["System in test calibration mode"],
                    test_steps=["Trigger boundary condition", "Verify fail-safe behavior"],
                    expected_result="Fail-safe protocol triggers without patient hazard",
                    exact_supporting_quote=sn["source_text_snapshot"][:60].strip() or sn["heading"],
                    risk_or_priority="High",
                    source_node_id=sn["node_id"],
                    source_logical_id=sn["logical_id"],
                    source_version=sn["version_number"],
                    source_content_hash=sn["content_hash"],
                )
            )

        return LLMGenerationOutput(test_cases=cases[:4])


def evaluate_staleness(
    generation_record: Dict[str, Any],
    db: Session,
    document_key: str = "ct200_manual",
) -> GenerationRetrievalResponse:
    """
    Evaluates staleness of a generation record by comparing original source node
    hashes with the corresponding node in the latest document version.
    """
    # Lookup latest document version
    latest_ver = (
        db.query(DocumentVersion)
        .filter_by(document_key=document_key)
        .order_by(DocumentVersion.version_number.desc())
        .first()
    )
    if not latest_ver:
        raise ValueError(f"No document version found for '{document_key}'")

    latest_nodes_by_logical = {
        n.logical_id: n
        for n in db.query(DocumentNode).filter_by(version_id=latest_ver.id).all()
    }

    staleness_details: List[NodeStalenessDetail] = []
    is_any_stale = False
    stale_reasons = []

    for sn in generation_record.get("source_nodes", []):
        lid = sn["logical_id"]
        orig_hash = sn["content_hash"]
        curr_node = latest_nodes_by_logical.get(lid)

        if not curr_node:
            is_any_stale = True
            stale_reasons.append(f"Section '{sn['heading']}' was removed in v{latest_ver.version_number}")
            staleness_details.append(
                NodeStalenessDetail(
                    logical_id=lid,
                    heading=sn["heading"],
                    original_node_id=sn["node_id"],
                    original_version=sn["version_number"],
                    original_content_hash=orig_hash,
                    current_node_id=None,
                    current_version=latest_ver.version_number,
                    current_content_hash=None,
                    status="removed",
                    diff_summary="Node removed in current document version",
                )
            )
        elif curr_node.content_hash == orig_hash:
            staleness_details.append(
                NodeStalenessDetail(
                    logical_id=lid,
                    heading=curr_node.heading,
                    original_node_id=sn["node_id"],
                    original_version=sn["version_number"],
                    original_content_hash=orig_hash,
                    current_node_id=curr_node.id,
                    current_version=latest_ver.version_number,
                    current_content_hash=curr_node.content_hash,
                    status="current",
                    diff_summary=None,
                )
            )
        else:
            is_any_stale = True
            stale_reasons.append(
                f"Section '{curr_node.heading}' modified in v{latest_ver.version_number} (hash difference)"
            )
            # Lightweight diff
            diff = "".join(
                difflib.unified_diff(
                    sn.get("source_text_snapshot", "").splitlines(keepends=True),
                    curr_node.body_text.splitlines(keepends=True),
                    fromfile=f"v{sn['version_number']}",
                    tofile=f"v{latest_ver.version_number}",
                    n=1,
                )
            ).strip()

            staleness_details.append(
                NodeStalenessDetail(
                    logical_id=lid,
                    heading=curr_node.heading,
                    original_node_id=sn["node_id"],
                    original_version=sn["version_number"],
                    original_content_hash=orig_hash,
                    current_node_id=curr_node.id,
                    current_version=latest_ver.version_number,
                    current_content_hash=curr_node.content_hash,
                    status="changed",
                    diff_summary=diff or "Content hash changed",
                )
            )

    stale_summary = "; ".join(stale_reasons) if is_any_stale else "All source requirements are current"

    return GenerationRetrievalResponse(
        generation_id=generation_record["generation_id"],
        selection_id=generation_record["selection_id"],
        selection_name=generation_record["selection_name"],
        storage_backend=generation_record.get("storage_backend", storage.backend_type),
        created_at=generation_record.get("created_at", ""),
        is_stale=is_any_stale,
        stale_reason=stale_summary if is_any_stale else None,
        test_cases=generation_record.get("test_cases", []),
        source_nodes_staleness=staleness_details,
    )
