"""
Document ingestion pipeline and version management service.
Handles idempotent re-ingestion, version increments, and physical node storage.
"""

import json
from typing import Optional, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models import DocumentVersion, DocumentNode
from app.parser import CT200Parser, ParsedDocument


def ingest_pdf(
    pdf_path: str,
    filename: str,
    db: Session,
    document_key: str = "ct200_manual",
) -> Tuple[DocumentVersion, bool]:
    """
    Ingests a PDF document into the versioned database.
    
    If the exact same file bytes have already been ingested for this document key,
    it returns the existing DocumentVersion without modifying or duplicating records (idempotent).
    Otherwise, increments the version number and creates immutable DocumentVersion and DocumentNode records.
    """
    parser = CT200Parser()
    parsed_doc: ParsedDocument = parser.parse(pdf_path, document_key=document_key)

    # Check for existing version with identical file hash
    existing = (
        db.query(DocumentVersion)
        .filter_by(document_key=document_key, file_hash=parsed_doc.file_hash)
        .first()
    )
    if existing:
        return existing, False

    # Determine next version number
    max_ver = (
        db.query(func.max(DocumentVersion.version_number))
        .filter_by(document_key=document_key)
        .scalar()
    )
    next_version_num = (max_ver or 0) + 1

    # Create new DocumentVersion
    doc_version = DocumentVersion(
        document_key=document_key,
        version_number=next_version_num,
        source_filename=filename,
        file_hash=parsed_doc.file_hash,
        page_count=parsed_doc.page_count,
        title=parsed_doc.title,
        metadata_json=json.dumps({"warnings": parsed_doc.warnings}),
    )
    db.add(doc_version)
    db.flush()

    # Map logical_id to physical node ID for this version
    logical_to_physical = {}
    for idx, pn in enumerate(parsed_doc.nodes):
        phys_id = f"node-v{next_version_num}-{idx:03d}-{pn.logical_id}"
        logical_to_physical[pn.logical_id] = phys_id

    # Create and persist immutable DocumentNode records
    node_records = []
    for idx, pn in enumerate(parsed_doc.nodes):
        phys_id = logical_to_physical[pn.logical_id]
        parent_phys_id = (
            logical_to_physical.get(pn.parent_logical_id)
            if pn.parent_logical_id
            else None
        )
        structured = {
            "tables": [t.to_dict() for t in pn.tables],
            "numbered_lists": pn.numbered_lists,
            "figures": pn.figures,
        }
        node_record = DocumentNode(
            id=phys_id,
            version_id=doc_version.id,
            logical_id=pn.logical_id,
            parent_id=parent_phys_id,
            parent_logical_id=pn.parent_logical_id,
            heading=pn.heading,
            section_number=pn.section_number,
            level=pn.level,
            body_text=pn.body_text,
            structured_data=json.dumps(structured),
            content_hash=pn.content_hash,
            page_numbers=json.dumps(pn.page_numbers),
            sequence_order=pn.sequence_order,
            warnings=json.dumps(pn.warnings),
        )
        node_records.append(node_record)

    db.add_all(node_records)
    db.commit()
    db.refresh(doc_version)

    return doc_version, True
