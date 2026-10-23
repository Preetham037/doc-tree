"""
Pydantic response and request schemas for document tree, browsing, search, and selections.
"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class VersionResponse(BaseModel):
    id: int
    document_key: str
    version_number: int
    source_filename: str
    file_hash: str
    page_count: int
    title: str
    node_count: int
    created_at: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class NodeResponse(BaseModel):
    node_id: str
    version_id: int
    version_number: Optional[int] = None
    logical_id: str
    parent_id: Optional[str] = None
    parent_logical_id: Optional[str] = None
    heading: str
    section_number: Optional[str] = None
    level: int
    body_text: str
    structured_data: Dict[str, Any] = Field(default_factory=dict)
    content_hash: str
    page_numbers: List[int] = Field(default_factory=list)
    node_type: str = "section"
    sequence_order: int
    warnings: List[str] = Field(default_factory=list)
    children: Optional[List["NodeResponse"]] = Field(default_factory=list)


class SectionSummary(BaseModel):
    node_id: str
    logical_id: str
    heading: str
    section_number: Optional[str] = None
    level: int
    parent_logical_id: Optional[str] = None
    page_numbers: List[int] = Field(default_factory=list)
    content_hash: str
    sequence_order: int
    has_tables: bool = False
    warning_count: int = 0


class SearchResult(BaseModel):
    node_id: str
    version_number: int
    logical_id: str
    heading: str
    section_number: Optional[str] = None
    match_field: str  # "heading", "body", "both"
    snippet: str
    content_hash: str
    page_numbers: List[int] = Field(default_factory=list)


class NodeChangeResponse(BaseModel):
    from_version: int
    to_version: int
    logical_id: str
    heading: str
    status: str  # "unchanged", "changed", "added", "removed", "ambiguous"
    from_node_id: Optional[str] = None
    to_node_id: Optional[str] = None
    from_content_hash: Optional[str] = None
    to_content_hash: Optional[str] = None
    text_diff: Optional[str] = None
    notes: Optional[str] = None


class SelectionCreate(BaseModel):
    name: str = Field(..., min_length=1, description="Descriptive name for the selection")
    node_ids: List[str] = Field(..., min_length=1, description="List of physical node IDs to pin")


class PinnedNodeSummary(BaseModel):
    node_id: str
    version_number: int
    logical_id: str
    heading: str
    content_hash: str
    source_text_snapshot: str


class SelectionResponse(BaseModel):
    selection_id: str
    name: str
    created_at: Optional[str] = None
    pinned_nodes: List[PinnedNodeSummary]
