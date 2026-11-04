"""Vercel entrypoint: exposes the FastAPI `app`."""

from ariadne.api import app

__all__ = ["app"]
