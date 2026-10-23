"""
SQLAlchemy ORM models for versioned document hierarchy and named selections.
"""

from datetime import datetime
import json
from typing import Any, Dict, List, Optional
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import backref, relationship

from app.database import Base


class DocumentVersion(Base):
    __tablename__ = "document_versions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_key = Column(String(100), index=True, nullable=False)
    version_number = Column(Integer, nullable=False)
    source_filename = Column(String(255), nullable=False)
    file_hash = Column(String(64), index=True, nullable=False)
    page_count = Column(Integer, default=0)
    title = Column(String(255), nullable=False)
    metadata_json = Column(Text, default="{}")
    created_at = Column(DateTime, default=datetime.utcnow)

    nodes = relationship(
        "DocumentNode",
        back_populates="version",
        cascade="all, delete-orphan",
        order_by="DocumentNode.sequence_order",
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "document_key": self.document_key,
            "version_number": self.version_number,
            "source_filename": self.source_filename,
            "file_hash": self.file_hash,
            "page_count": self.page_count,
            "title": self.title,
            "metadata": json.loads(self.metadata_json or "{}"),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "node_count": len(self.nodes) if self.nodes else 0,
        }


class DocumentNode(Base):
    __tablename__ = "document_nodes"

    # Unique physical primary key per version record
    id = Column(String(128), primary_key=True)
    version_id = Column(Integer, ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False, index=True)
    # Logical identity that remains stable across document versions
    logical_id = Column(String(128), index=True, nullable=False)
    parent_id = Column(String(128), ForeignKey("document_nodes.id"), nullable=True)
    parent_logical_id = Column(String(128), nullable=True)
    heading = Column(String(255), nullable=False)
    section_number = Column(String(50), nullable=True, index=True)
    level = Column(Integer, default=1)
    body_text = Column(Text, default="")
    structured_data = Column(Text, default="{}")  # JSON: tables, lists, figures
    content_hash = Column(String(64), index=True, nullable=False)
    page_numbers = Column(Text, default="[]")  # JSON list
    node_type = Column(String(50), default="section")
    sequence_order = Column(Integer, default=0)
    warnings = Column(Text, default="[]")  # JSON list

    version = relationship("DocumentVersion", back_populates="nodes")
    children = relationship(
        "DocumentNode",
        cascade="all",
        backref=backref("parent", remote_side=[id]),
        order_by="DocumentNode.sequence_order",
    )

    def to_dict(self, include_children: bool = False) -> Dict[str, Any]:
        res = {
            "node_id": self.id,
            "version_id": self.version_id,
            "logical_id": self.logical_id,
            "parent_id": self.parent_id,
            "parent_logical_id": self.parent_logical_id,
            "heading": self.heading,
            "section_number": self.section_number,
            "level": self.level,
            "body_text": self.body_text,
            "structured_data": json.loads(self.structured_data or "{}"),
            "content_hash": self.content_hash,
            "page_numbers": json.loads(self.page_numbers or "[]"),
            "node_type": self.node_type,
            "sequence_order": self.sequence_order,
            "warnings": json.loads(self.warnings or "[]"),
        }
        if include_children and self.children:
            res["children"] = [child.to_dict(include_children=True) for child in self.children]
        return res


class Selection(Base):
    __tablename__ = "selections"

    id = Column(String(64), primary_key=True)
    name = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    pinned_nodes = relationship(
        "SelectionNode",
        back_populates="selection",
        cascade="all, delete-orphan",
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "selection_id": self.id,
            "name": self.name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "pinned_nodes": [p.to_dict() for p in self.pinned_nodes],
        }


class SelectionNode(Base):
    __tablename__ = "selection_nodes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    selection_id = Column(String(64), ForeignKey("selections.id", ondelete="CASCADE"), nullable=False, index=True)
    node_id = Column(String(128), ForeignKey("document_nodes.id"), nullable=False, index=True)
    version_number = Column(Integer, nullable=False)
    logical_id = Column(String(128), nullable=False)
    content_hash = Column(String(64), nullable=False)
    heading = Column(String(255), nullable=False)
    source_text_snapshot = Column(Text, default="")

    selection = relationship("Selection", back_populates="pinned_nodes")
    node = relationship("DocumentNode")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "version_number": self.version_number,
            "logical_id": self.logical_id,
            "content_hash": self.content_hash,
            "heading": self.heading,
            "source_text_snapshot": self.source_text_snapshot,
        }
