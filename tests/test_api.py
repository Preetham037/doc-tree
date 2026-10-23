"""
Tests for FastAPI browse, search, change comparison, and selection pinning APIs.
"""

import os
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from app.ingestion import ingest_pdf


@pytest.fixture(scope="module")
def test_client():
    # Use isolated SQLite test database
    test_db_url = "sqlite:///./test_doc_tree.db"
    engine = create_engine(test_db_url, connect_args={"check_same_thread": False})
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    # Ingest v1 and v2 into test database
    db = TestingSessionLocal()
    v1_path = os.path.join("data", "ct200_manual.pdf")
    v2_path = os.path.join("data", "ct200_manual_v2.pdf")
    ingest_pdf(v1_path, "ct200_manual.pdf", db)
    ingest_pdf(v2_path, "ct200_manual_v2.pdf", db)
    db.close()

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    yield client

    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)
    if os.path.exists("./test_doc_tree.db"):
        os.remove("./test_doc_tree.db")


class TestBrowseAndSearchAPIs:
    def test_list_versions(self, test_client):
        response = test_client.get("/versions")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["version_number"] == 1
        assert data[0]["node_count"] == 28
        assert data[1]["version_number"] == 2
        assert data[1]["node_count"] == 29

    def test_list_sections_default_and_specific(self, test_client):
        # Default should return latest (v2, 29 sections)
        resp_latest = test_client.get("/sections")
        assert resp_latest.status_code == 200
        assert len(resp_latest.json()) == 29

        # Version 1 should return 28 sections
        resp_v1 = test_client.get("/sections?version=1")
        assert resp_v1.status_code == 200
        assert len(resp_v1.json()) == 28

    def test_get_node_details(self, test_client):
        # Lookup by section number in v1
        resp = test_client.get("/nodes/2.1?version=1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["section_number"] == "2.1"
        assert data["version_number"] == 1
        assert len(data["structured_data"]["tables"]) == 1
        assert data["structured_data"]["tables"][0]["headers"] == ["Parameter", "Value"]

        # Lookup invalid node returns 404
        resp_404 = test_client.get("/nodes/non-existent-node-id")
        assert resp_404.status_code == 404

    def test_search_document(self, test_client):
        # Search for 'battery' in v1
        resp = test_client.get("/search?q=battery&version=1")
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) > 0
        section_nums = [r["section_number"] for r in results]
        assert "2.1.1.1" in section_nums
        assert "3.4" in section_nums  # Auto shutoff to conserve battery

    def test_node_change_comparison(self, test_client):
        # Battery life changed
        resp = test_client.get("/nodes/2.1.1.1/changes?from_version=1&to_version=2")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "changed"
        assert "300 measurement cycles" in data["text_diff"]
        assert "250 measurement cycles" in data["text_diff"]

        # Section 5.3 was added in v2
        resp_53 = test_client.get("/nodes/5.3/changes?from_version=1&to_version=2")
        assert resp_53.status_code == 200
        assert resp_53.json()["status"] == "added"

        # Section 1.1 unchanged
        resp_11 = test_client.get("/nodes/1.1/changes?from_version=1&to_version=2")
        assert resp_11.status_code == 200
        assert resp_11.json()["status"] == "unchanged"


class TestSelectionsAndPinning:
    def test_create_and_pin_selection(self, test_client):
        # Get v1 node for battery life
        v1_battery = test_client.get("/nodes/2.1.1.1?version=1").json()
        v1_cuff = test_client.get("/nodes/3.2?version=1").json()

        # Create selection pinned to v1 nodes
        payload = {
            "name": "Power & Inflation Subsystem",
            "node_ids": [v1_battery["node_id"], v1_cuff["node_id"]],
        }
        resp = test_client.post("/selections", json=payload)
        assert resp.status_code == 200
        sel = resp.json()
        sel_id = sel["selection_id"]
        assert len(sel["pinned_nodes"]) == 2

        # Verify pinned nodes retain v1 version number and v1 content hashes
        for p in sel["pinned_nodes"]:
            assert p["version_number"] == 1
            if p["logical_id"] == v1_battery["logical_id"]:
                assert p["content_hash"] == v1_battery["content_hash"]
                assert "300 measurement cycles" in p["source_text_snapshot"]

        # Retrieve selection through GET /selections/{id}
        get_resp = test_client.get(f"/selections/{sel_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["name"] == "Power & Inflation Subsystem"

    def test_reject_invalid_node_id_in_selection(self, test_client):
        payload = {
            "name": "Invalid Selection",
            "node_ids": ["non_existent_node_12345"],
        }
        resp = test_client.post("/selections", json=payload)
        assert resp.status_code == 400
        assert "Invalid node_id" in resp.json()["detail"]
