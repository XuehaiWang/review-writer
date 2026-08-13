"""Production-facing API shell for Review Writer."""

from .app import create_app

__all__ = ["create_app"]
