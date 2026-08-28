"""Abstract base class for event collectors."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from models import Event

log = logging.getLogger(__name__)


class BaseCollector(ABC):
    """Interface every collector must implement."""

    name: str = "base"

    # La pagina (o l'endpoint) da cui questo collector prende i dati.
    # Serve a health/diagnose per riscaricare cio' che il collector legge
    # quando un campo smette di riempirsi: gli URL vivono in tre posti diversi
    # — costanti di modulo, config.py, o inline dentro collect() — e senza un
    # accesso uniforme la diagnosi non saprebbe cosa andare a guardare.
    # Punta alla stessa costante che collect() usa: non e' una seconda verita'.
    probe_url: str = ""

    @abstractmethod
    def collect(self) -> list[Event]:
        """Scrape/fetch events and return normalized Event objects."""
        ...

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}>"
