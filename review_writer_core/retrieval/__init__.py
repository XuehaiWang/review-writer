"""Deterministic, rebuildable retrieval primitives."""

from .chunker import CHUNKER_VERSION, DocumentChunk, build_document_chunks

__all__ = ["CHUNKER_VERSION", "DocumentChunk", "build_document_chunks"]
