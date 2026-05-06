"""Abstract base class for event collectors."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from models import Event

log = logging.getLogger(__name__)


class BaseCollector(ABC):
    """Interface every collector must implement."""

    name: str = "base"

    @abstractmethod
    def collect(self) -> list[Event]:
        """Scrape/fetch events and return normalized Event objects."""
        ...

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}>"
