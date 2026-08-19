"""Multi-source academic paper search."""

from .base import PaperSearchRequest, PaperSourceConnector, SourceSearchResult
from .service import (
    DEFAULT_SEARCH_LIMITS,
    SUPPORTED_SOURCES,
    PaperSourceSearchLimits,
    parse_source_names,
    search_paper_sources,
)

__all__ = (
    "PaperSearchRequest",
    "PaperSourceConnector",
    "SourceSearchResult",
    "SUPPORTED_SOURCES",
    "PaperSourceSearchLimits",
    "DEFAULT_SEARCH_LIMITS",
    "parse_source_names",
    "search_paper_sources",
)
