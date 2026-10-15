"""Kenari web-search backend plugin — entry point.

Registers a :class:`KenariWebSearchProvider` so ``web_search`` / ``web_extract``
can be served by Kenari's ``/v1/web/search`` and ``/v1/web/fetch`` endpoints.
Relative import: a plugin is loaded with its own directory as the package root,
so the module name is install-specific and an absolute ``plugins.web.*`` import
would only resolve for a bundled install.
"""

from __future__ import annotations

from .provider import KenariWebSearchProvider


def register(ctx) -> None:
    """Hermes plugin entry point — called once at load time."""
    ctx.register_web_search_provider(KenariWebSearchProvider())
