"""
FastAPI application exposing browse, search, change comparison, selections,
and QA generation endpoints.
"""

from contextlib import asynccontextmanager
import difflib
import json
import os
import shutil
import tempfile
from typing import Any, Dict, List, Optional
import uuid

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, or_

from app.database import Base, engine, get_db, init_db
from app.models import DocumentVersion, DocumentNode, Selection, SelectionNode
from app.ingestion import ingest_pdf
from app.schemas import (
    VersionResponse,
    NodeResponse,
    SectionSummary,
    SearchResult,
    NodeChangeResponse,
    SelectionCreate,
    SelectionResponse,
    PinnedNodeSummary,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize SQLite database schema
    init_db()
    yield


app = FastAPI(
    title="CardioTrack CT-200 Document Tree & QA Traceability API",
    description="Backend API for hierarchical manual parsing, versioning, change comparison, named selections, and QA generation.",
    version="1.0.0",
    lifespan=lifespan,
)


def resolve_version(
    db: Session,
    version_param: Optional[int] = None,
    document_key: str = "ct200_manual",
) -> DocumentVersion:
    """Helper to resolve the target document version (defaulting to latest)."""
    if version_param is not None:
        v = (
            db.query(DocumentVersion)
            .filter_by(document_key=document_key, version_number=version_param)
            .first()
        )
        if not v:
            raise HTTPException(
                status_code=404,
                detail=f"Document version {version_param} not found for '{document_key}'",
            )
        return v

    latest = (
        db.query(DocumentVersion)
        .filter_by(document_key=document_key)
        .order_by(DocumentVersion.version_number.desc())
        .first()
    )
    if not latest:
        raise HTTPException(
            status_code=404,
            detail=f"No versions have been ingested yet for '{document_key}'",
        )
    return latest


@app.post("/ingest", summary="Ingest a CT-200 manual PDF into the versioned tree")
async def ingest_manual(
    file: Optional[UploadFile] = File(None),
    file_path: Optional[str] = Query(None, description="Local file path to ingest (optional)"),
    document_key: str = Query("ct200_manual", description="Document key"),
    db: Session = Depends(get_db),
):
    if file:
        temp_dir = tempfile.mkdtemp()
        temp_path = os.path.join(temp_dir, file.filename or "upload.pdf")
        with open(temp_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        try:
            doc_version, is_new = ingest_pdf(temp_path, file.filename or "upload.pdf", db, document_key)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
    elif file_path:
        if not os.path.isfile(file_path):
            raise HTTPException(status_code=400, detail=f"File not found: {file_path}")
        filename = os.path.basename(file_path)
        doc_version, is_new = ingest_pdf(file_path, filename, db, document_key)
    else:
        # Default to data/ct200_manual.pdf if no input provided
        default_path = os.path.join("data", "ct200_manual.pdf")
        if not os.path.isfile(default_path):
            raise HTTPException(status_code=400, detail="No PDF file provided and default data/ct200_manual.pdf not found")
        doc_version, is_new = ingest_pdf(default_path, "ct200_manual.pdf", db, document_key)

    return {
        "status": "success",
        "is_new_version": is_new,
        "version": doc_version.to_dict(),
    }


@app.get("/versions", response_model=List[VersionResponse], summary="List all ingested document versions")
def list_versions(
    document_key: str = Query("ct200_manual", description="Document key"),
    db: Session = Depends(get_db),
):
    versions = (
        db.query(DocumentVersion)
        .filter_by(document_key=document_key)
        .order_by(DocumentVersion.version_number.asc())
        .all()
    )
    return [v.to_dict() for v in versions]


@app.get("/sections", response_model=List[SectionSummary], summary="Browse hierarchical sections for a version")
def list_sections(
    version: Optional[int] = Query(None, description="Version number (defaults to latest)"),
    document_key: str = Query("ct200_manual", description="Document key"),
    db: Session = Depends(get_db),
):
    doc_ver = resolve_version(db, version, document_key)
    nodes = (
        db.query(DocumentNode)
        .filter_by(version_id=doc_ver.id)
        .order_by(DocumentNode.sequence_order.asc())
        .all()
    )
    summaries = []
    for n in nodes:
        structured = json.loads(n.structured_data or "{}")
        has_tables = bool(structured.get("tables"))
        warnings = json.loads(n.warnings or "[]")
        summaries.append(
            SectionSummary(
                node_id=n.id,
                logical_id=n.logical_id,
                heading=n.heading,
                section_number=n.section_number,
                level=n.level,
                parent_logical_id=n.parent_logical_id,
                page_numbers=json.loads(n.page_numbers or "[]"),
                content_hash=n.content_hash,
                sequence_order=n.sequence_order,
                has_tables=has_tables,
                warning_count=len(warnings),
            )
        )
    return summaries


@app.get("/nodes/{node_id}", response_model=NodeResponse, summary="Retrieve a detailed node with tables, children, and warnings")
def get_node(
    node_id: str,
    version: Optional[int] = Query(None, description="Version number (defaults to latest)"),
    document_key: str = Query("ct200_manual", description="Document key"),
    db: Session = Depends(get_db),
):
    # Attempt 1: Lookup directly by physical primary key
    node = db.query(DocumentNode).filter_by(id=node_id).first()
    if node:
        resp = node.to_dict(include_children=True)
        resp["version_number"] = node.version.version_number
        return resp

    # Attempt 2: Lookup by logical_id or section_number within the target version
    target_ver = resolve_version(db, version, document_key)
    node = (
        db.query(DocumentNode)
        .filter(
            DocumentNode.version_id == target_ver.id,
            or_(
                DocumentNode.logical_id == node_id,
                DocumentNode.section_number == node_id,
            ),
        )
        .first()
    )
    if not node:
        raise HTTPException(
            status_code=404,
            detail=f"Node '{node_id}' not found in version {target_ver.version_number}",
        )

    resp = node.to_dict(include_children=True)
    resp["version_number"] = target_ver.version_number
    return resp


@app.get("/search", response_model=List[SearchResult], summary="Search headings and body text within a document version")
def search_document(
    q: str = Query(..., min_length=1, description="Search query term"),
    version: Optional[int] = Query(None, description="Version number (defaults to latest)"),
    document_key: str = Query("ct200_manual", description="Document key"),
    db: Session = Depends(get_db),
):
    target_ver = resolve_version(db, version, document_key)
    query_str = f"%{q.strip().lower()}%"

    nodes = (
        db.query(DocumentNode)
        .filter(
            DocumentNode.version_id == target_ver.id,
            or_(
                func.lower(DocumentNode.heading).like(query_str),
                func.lower(DocumentNode.body_text).like(query_str),
            ),
        )
        .order_by(DocumentNode.sequence_order.asc())
        .all()
    )

    results = []
    q_lower = q.strip().lower()
    for n in nodes:
        in_heading = q_lower in n.heading.lower()
        in_body = q_lower in n.body_text.lower()
        if in_heading and in_body:
            match_field = "both"
        elif in_heading:
            match_field = "heading"
        else:
            match_field = "body"

        # Generate snippet around match
        snippet = n.heading
        if in_body:
            idx = n.body_text.lower().find(q_lower)
            start = max(0, idx - 40)
            end = min(len(n.body_text), idx + len(q) + 40)
            snippet = f"...{n.body_text[start:end]}..."

        results.append(
            SearchResult(
                node_id=n.id,
                version_number=target_ver.version_number,
                logical_id=n.logical_id,
                heading=n.heading,
                section_number=n.section_number,
                match_field=match_field,
                snippet=snippet,
                content_hash=n.content_hash,
                page_numbers=json.loads(n.page_numbers or "[]"),
            )
        )
    return results


@app.get("/nodes/{node_id}/changes", response_model=NodeChangeResponse, summary="Compare a specific node across two versions")
def compare_node_change(
    node_id: str,
    from_version: int = Query(1, description="Source version number"),
    to_version: int = Query(2, description="Target version number"),
    document_key: str = Query("ct200_manual", description="Document key"),
    db: Session = Depends(get_db),
):
    v_from = resolve_version(db, from_version, document_key)
    v_to = resolve_version(db, to_version, document_key)

    # Resolve logical_id from physical ID, logical_id, or section_number
    logical_id = node_id
    lookup_node = (
        db.query(DocumentNode)
        .filter(
            or_(
                DocumentNode.id == node_id,
                DocumentNode.logical_id == node_id,
                DocumentNode.section_number == node_id,
            )
        )
        .first()
    )
    if lookup_node:
        logical_id = lookup_node.logical_id

    # Lookup node in from_version and to_version
    node_from = (
        db.query(DocumentNode)
        .filter_by(version_id=v_from.id, logical_id=logical_id)
        .first()
    )
    node_to = (
        db.query(DocumentNode)
        .filter_by(version_id=v_to.id, logical_id=logical_id)
        .first()
    )

    if not node_from and not node_to:
        raise HTTPException(
            status_code=404,
            detail=f"Node '{node_id}' does not exist in version {from_version} or version {to_version}",
        )

    if node_from and not node_to:
        return NodeChangeResponse(
            from_version=from_version,
            to_version=to_version,
            logical_id=logical_id,
            heading=node_from.heading,
            status="removed",
            from_node_id=node_from.id,
            to_node_id=None,
            from_content_hash=node_from.content_hash,
            to_content_hash=None,
            text_diff=None,
            notes=f"Node was removed in version {to_version}",
        )

    if not node_from and node_to:
        return NodeChangeResponse(
            from_version=from_version,
            to_version=to_version,
            logical_id=logical_id,
            heading=node_to.heading,
            status="added",
            from_node_id=None,
            to_node_id=node_to.id,
            from_content_hash=None,
            to_content_hash=node_to.content_hash,
            text_diff=None,
            notes=f"Node was newly added in version {to_version}",
        )

    # Present in both versions
    if node_from.content_hash == node_to.content_hash:
        return NodeChangeResponse(
            from_version=from_version,
            to_version=to_version,
            logical_id=logical_id,
            heading=node_to.heading,
            status="unchanged",
            from_node_id=node_from.id,
            to_node_id=node_to.id,
            from_content_hash=node_from.content_hash,
            to_content_hash=node_to.content_hash,
            text_diff=None,
            notes="Content hashes are identical",
        )

    # Compute concise text diff
    lines_from = node_from.body_text.splitlines(keepends=True)
    lines_to = node_to.body_text.splitlines(keepends=True)
    diff = "".join(
        difflib.unified_diff(
            lines_from,
            lines_to,
            fromfile=f"v{from_version}",
            tofile=f"v{to_version}",
            n=2,
        )
    )

    # If body text was identical but structured tables changed
    if not diff.strip() and node_from.structured_data != node_to.structured_data:
        diff = f"Structured table data changed between v{from_version} and v{to_version}."

    return NodeChangeResponse(
        from_version=from_version,
        to_version=to_version,
        logical_id=logical_id,
        heading=node_to.heading,
        status="changed",
        from_node_id=node_from.id,
        to_node_id=node_to.id,
        from_content_hash=node_from.content_hash,
        to_content_hash=node_to.content_hash,
        text_diff=diff.strip() or "Content hash changed",
        notes="Content was modified between versions",
    )


@app.get("/changes", response_model=List[NodeChangeResponse], summary="Full cross-version diff report across all nodes")
def full_version_changes(
    from_version: int = Query(1, description="Source version number"),
    to_version: int = Query(2, description="Target version number"),
    document_key: str = Query("ct200_manual", description="Document key"),
    db: Session = Depends(get_db),
):
    v_from = resolve_version(db, from_version, document_key)
    v_to = resolve_version(db, to_version, document_key)

    nodes_from = {n.logical_id: n for n in v_from.nodes}
    nodes_to = {n.logical_id: n for n in v_to.nodes}

    all_logical_ids = list(dict.fromkeys(list(nodes_from.keys()) + list(nodes_to.keys())))
    changes: List[NodeChangeResponse] = []

    for lid in all_logical_ids:
        nf = nodes_from.get(lid)
        nt = nodes_to.get(lid)

        if nf and not nt:
            changes.append(
                NodeChangeResponse(
                    from_version=from_version,
                    to_version=to_version,
                    logical_id=lid,
                    heading=nf.heading,
                    status="removed",
                    from_node_id=nf.id,
                    from_content_hash=nf.content_hash,
                )
            )
        elif not nf and nt:
            changes.append(
                NodeChangeResponse(
                    from_version=from_version,
                    to_version=to_version,
                    logical_id=lid,
                    heading=nt.heading,
                    status="added",
                    to_node_id=nt.id,
                    to_content_hash=nt.content_hash,
                )
            )
        elif nf.content_hash == nt.content_hash:
            changes.append(
                NodeChangeResponse(
                    from_version=from_version,
                    to_version=to_version,
                    logical_id=lid,
                    heading=nt.heading,
                    status="unchanged",
                    from_node_id=nf.id,
                    to_node_id=nt.id,
                    from_content_hash=nf.content_hash,
                    to_content_hash=nt.content_hash,
                )
            )
        else:
            diff = "".join(
                difflib.unified_diff(
                    nf.body_text.splitlines(keepends=True),
                    nt.body_text.splitlines(keepends=True),
                    fromfile=f"v{from_version}",
                    tofile=f"v{to_version}",
                    n=2,
                )
            )
            if not diff.strip() and nf.structured_data != nt.structured_data:
                diff = f"Structured table data modified between v{from_version} and v{to_version}."

            changes.append(
                NodeChangeResponse(
                    from_version=from_version,
                    to_version=to_version,
                    logical_id=lid,
                    heading=nt.heading,
                    status="changed",
                    from_node_id=nf.id,
                    to_node_id=nt.id,
                    from_content_hash=nf.content_hash,
                    to_content_hash=nt.content_hash,
                    text_diff=diff.strip() or "Modified",
                )
            )

    return changes


# --- SELECTION APIS ---


@app.post("/selections", response_model=SelectionResponse, summary="Create a named selection pinned to exact historical node/version records")
def create_selection(
    payload: SelectionCreate,
    db: Session = Depends(get_db),
):
    # Verify every node ID exists. Reject invalid node IDs with a clear error; do not silently substitute.
    pinned_records: List[SelectionNode] = []
    sel_id = f"sel-{uuid.uuid4().hex[:8]}"
    selection = Selection(id=sel_id, name=payload.name)
    db.add(selection)
    db.flush()

    for nid in payload.node_ids:
        # Lookup physical node
        node = db.query(DocumentNode).filter_by(id=nid).first()
        if not node:
            # Check if user provided logical_id or section_number
            node = db.query(DocumentNode).filter(
                or_(DocumentNode.logical_id == nid, DocumentNode.section_number == nid)
            ).order_by(DocumentNode.sequence_order.asc()).first()

        if not node:
            db.rollback()
            raise HTTPException(
                status_code=400,
                detail=f"Invalid node_id '{nid}'. Node does not exist. Silent substitution is rejected.",
            )

        pinned = SelectionNode(
            selection_id=sel_id,
            node_id=node.id,
            version_number=node.version.version_number,
            logical_id=node.logical_id,
            content_hash=node.content_hash,
            heading=node.heading,
            source_text_snapshot=node.body_text,
        )
        pinned_records.append(pinned)

    db.add_all(pinned_records)
    db.commit()
    db.refresh(selection)

    return SelectionResponse(
        selection_id=selection.id,
        name=selection.name,
        created_at=selection.created_at.isoformat() if selection.created_at else None,
        pinned_nodes=[
            PinnedNodeSummary(
                node_id=p.node_id,
                version_number=p.version_number,
                logical_id=p.logical_id,
                heading=p.heading,
                content_hash=p.content_hash,
                source_text_snapshot=p.source_text_snapshot,
            )
            for p in pinned_records
        ],
    )


@app.get("/selections/{selection_id}", response_model=SelectionResponse, summary="Retrieve a named selection with its pinned historical nodes")
def get_selection(
    selection_id: str,
    db: Session = Depends(get_db),
):
    selection = db.query(Selection).filter_by(id=selection_id).first()
    if not selection:
        raise HTTPException(status_code=404, detail=f"Selection '{selection_id}' not found")

    return SelectionResponse(
        selection_id=selection.id,
        name=selection.name,
        created_at=selection.created_at.isoformat() if selection.created_at else None,
        pinned_nodes=[
            PinnedNodeSummary(
                node_id=p.node_id,
                version_number=p.version_number,
                logical_id=p.logical_id,
                heading=p.heading,
                content_hash=p.content_hash,
                source_text_snapshot=p.source_text_snapshot,
            )
            for p in selection.pinned_nodes
        ],
    )


# --- LLM QA GENERATION & STALENESS APIS ---

from pydantic import BaseModel as PyBaseModel


class GenerateRequest(PyBaseModel):
    selection_id: str
    force_regenerate: bool = False


from app.generation import (
    GenerationRetrievalResponse,
    QAGeneratorService,
    evaluate_staleness,
    storage,
)


@app.post("/generate", summary="Generate 3-5 validated QA test cases for a named selection")
def generate_qa_test_cases(
    payload: GenerateRequest,
    mock_mode: bool = Query(False, description="Use deterministic mock LLM provider for local testing"),
    db: Session = Depends(get_db),
):
    selection = db.query(Selection).filter_by(id=payload.selection_id).first()
    if not selection:
        raise HTTPException(
            status_code=404,
            detail=f"Selection '{payload.selection_id}' not found",
        )

    if not selection.pinned_nodes:
        raise HTTPException(
            status_code=400,
            detail=f"Selection '{payload.selection_id}' has no pinned nodes",
        )

    generator = QAGeneratorService(mock_mode=mock_mode)
    try:
        record = generator.generate_for_selection(
            selection=selection,
            db=db,
            force_regenerate=payload.force_regenerate,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"QA Generation failed: {str(e)}")

    # Return generation enriched with initial staleness status
    return evaluate_staleness(record, db)


@app.get(
    "/generations",
    response_model=List[GenerationRetrievalResponse],
    summary="Retrieve generated test cases with real-time staleness evaluation against latest document version",
)
def list_generations(
    selection_id: Optional[str] = Query(None, description="Filter by selection ID"),
    node_id: Optional[str] = Query(None, description="Filter by source node ID"),
    generation_id: Optional[str] = Query(None, description="Filter by generation ID"),
    db: Session = Depends(get_db),
):
    records = storage.find_generations(
        selection_id=selection_id,
        node_id=node_id,
        generation_id=generation_id,
    )

    results: List[GenerationRetrievalResponse] = []
    for rec in records:
        try:
            report = evaluate_staleness(rec, db)
            results.append(report)
        except Exception as e:
            # If document versions not available
            continue

    return results


@app.get(
    "/generations/{generation_id}",
    response_model=GenerationRetrievalResponse,
    summary="Retrieve a specific generation and evaluate its staleness against latest version",
)
def get_generation_by_id(
    generation_id: str,
    db: Session = Depends(get_db),
):
    records = storage.find_generations(generation_id=generation_id)
    if not records:
        raise HTTPException(status_code=404, detail=f"Generation '{generation_id}' not found")

    return evaluate_staleness(records[0], db)

