"""
Tests for LLM QA test case generation, Pydantic structured output validation,
retry behavior, NoSQL persistence, and end-to-end staleness evaluation.
"""

import os
import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from app.ingestion import ingest_pdf
from app.generation import (
    QATestCase,
    LLMGenerationOutput,
    QAGeneratorService,
    storage,
    JSON_FALLBACK_FILE,
)


class TestLLMSchemaValidation:
    def test_valid_test_case_passes_validation(self):
        case = QATestCase(
            title="Verify battery life under typical use",
            preconditions=["Four fresh AA alkaline batteries installed"],
            test_steps=["Run simulated 3 measurements/day protocol", "Record total measurement cycles"],
            expected_result="Device operates for approximately 300 cycles before requiring replacement",
            exact_supporting_quote="four AA alkaline batteries provide approximately 300 measurement cycles",
            risk_or_priority="Medium",
            source_node_id="node-v1-006-sec-2.1.1.1",
            source_logical_id="sec-2.1.1.1-battery-life",
            source_version=1,
            source_content_hash="abc123hash",
        )
        assert case.title == "Verify battery life under typical use"

    def test_rejects_empty_test_steps(self):
        with pytest.raises(ValidationError):
            QATestCase(
                title="Invalid test case with no steps",
                preconditions=["Ready"],
                test_steps=[],  # Invalid: empty steps
                expected_result="Should fail",
                exact_supporting_quote="quote",
                risk_or_priority="Low",
                source_node_id="nid",
                source_logical_id="lid",
                source_version=1,
                source_content_hash="hash",
            )

    def test_rejects_case_count_outside_3_to_5(self):
        # Fewer than 3 cases
        case1 = QATestCase(
            title="Test Case 1",
            preconditions=["Setup condition"],
            test_steps=["Step 1"],
            expected_result="Device behaves correctly",
            exact_supporting_quote="supporting quote",
            risk_or_priority="Low",
            source_node_id="nid",
            source_logical_id="lid",
            source_version=1,
            source_content_hash="hash",
        )
        case2 = case1.model_copy(update={"title": "Test Case 2"})
        with pytest.raises(ValidationError) as exc:
            LLMGenerationOutput(test_cases=[case1, case2])
        assert "between 3 and 5 test cases" in str(exc.value)


@pytest.fixture(scope="module")
def workflow_client():
    test_db_url = "sqlite:///./test_workflow.db"
    engine = create_engine(test_db_url, connect_args={"check_same_thread": False})
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    # Clean up test fallback JSON file if present
    if os.path.exists(JSON_FALLBACK_FILE):
        try:
            os.remove(JSON_FALLBACK_FILE)
        except Exception:
            pass

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    yield client, TestingSessionLocal

    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)
    if os.path.exists("./test_workflow.db"):
        os.remove("./test_workflow.db")


class TestEndToEndVersioningAndStalenessWorkflow:
    def test_complete_v1_to_v2_generation_and_staleness_workflow(self, workflow_client):
        client, SessionLocal = workflow_client
        db = SessionLocal()

        # Step 1: Ingest v1 only initially
        v1_path = os.path.join("data", "ct200_manual.pdf")
        v1, is_new = ingest_pdf(v1_path, "ct200_manual.pdf", db)
        assert v1.version_number == 1
        assert is_new is True

        # Verify only 1 version exists right now
        vers_resp = client.get("/versions").json()
        assert len(vers_resp) == 1

        # Step 2: Create a named selection on v1
        # Include:
        # - Section 1.1 Intended Use (will remain unchanged in v2)
        # - Section 2.1.1.1 Battery Life (will change in v2)
        node_11 = client.get("/nodes/1.1?version=1").json()
        node_2111 = client.get("/nodes/2.1.1.1?version=1").json()

        sel_payload = {
            "name": "Safety and Battery Requirements",
            "node_ids": [node_11["node_id"], node_2111["node_id"]],
        }
        sel_resp = client.post("/selections", json=sel_payload)
        assert sel_resp.status_code == 200
        sel_id = sel_resp.json()["selection_id"]

        # Step 3: Generate QA test cases against v1 selection using mock mode
        gen_resp = client.post(f"/generate?mock_mode=true", json={"selection_id": sel_id})
        assert gen_resp.status_code == 200
        gen_data = gen_resp.json()
        gen_id = gen_data["generation_id"]

        # Check generation properties
        assert len(gen_data["test_cases"]) >= 3
        assert len(gen_data["test_cases"]) <= 5
        # Since only v1 is ingested so far, generation should NOT be stale!
        assert gen_data["is_stale"] is False
        assert gen_data["stale_reason"] is None
        for sn in gen_data["source_nodes_staleness"]:
            assert sn["status"] == "current"

        # Check idempotency: second generation call without force_regenerate returns existing record
        gen_dup = client.post(f"/generate?mock_mode=true", json={"selection_id": sel_id})
        assert gen_dup.json()["generation_id"] == gen_id

        # Step 4: Now ingest Version 2 into the database!
        v2_path = os.path.join("data", "ct200_manual_v2.pdf")
        v2, is_new_v2 = ingest_pdf(v2_path, "ct200_manual_v2.pdf", db)
        assert v2.version_number == 2
        assert is_new_v2 is True

        # Verify v1 and v2 coexist in database
        vers_resp_after = client.get("/versions").json()
        assert len(vers_resp_after) == 2

        # Step 5: Retrieve original selection and verify version pinning
        pinned_sel = client.get(f"/selections/{sel_id}").json()
        for p in pinned_sel["pinned_nodes"]:
            assert p["version_number"] == 1
            if p["logical_id"] == node_2111["logical_id"]:
                assert "300 measurement cycles" in p["source_text_snapshot"]

        # Step 6: Retrieve generation and evaluate staleness
        # The retrieval must detect that 2.1.1.1 changed in v2 while 1.1 remained current!
        retrieval_resp = client.get(f"/generations?selection_id={sel_id}")
        assert retrieval_resp.status_code == 200
        reports = retrieval_resp.json()
        assert len(reports) >= 1
        report = reports[0]

        # Overall generation MUST BE FLAGGED AS STALE!
        assert report["is_stale"] is True
        assert report["stale_reason"] is not None
        assert "modified in v2" in report["stale_reason"] or "hash difference" in report["stale_reason"]

        # Check individual node staleness:
        node_status_map = {sn["logical_id"]: sn for sn in report["source_nodes_staleness"]}
        
        # Section 1.1 Intended Use remained unchanged
        sn_11 = node_status_map[node_11["logical_id"]]
        assert sn_11["status"] == "current"
        assert sn_11["diff_summary"] is None

        # Section 2.1.1.1 Battery Life was changed in v2
        sn_battery = node_status_map[node_2111["logical_id"]]
        assert sn_battery["status"] == "changed"
        assert sn_battery["original_version"] == 1
        assert sn_battery["current_version"] == 2
        assert sn_battery["diff_summary"] is not None
        assert "300" in sn_battery["diff_summary"] or "250" in sn_battery["diff_summary"]

        db.close()
