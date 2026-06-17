"""Bublik e2e fixture toolkit (the ``bublik-e2e`` CLI).

External fixture providers passed via ``--fixture`` should subclass
``BaseFixture`` re-exported here::

    from core import BaseFixture
"""

from __future__ import annotations

from core.common import CliError
from core.fixture_api import BaseFixture, FixtureProvider

__all__ = ["BaseFixture", "FixtureProvider", "CliError"]
