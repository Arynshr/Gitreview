"""Re-export: events moved to `gitscribe.events` (shared by all services)."""

from gitscribe.events import Emitter, Event, emit

__all__ = ["Emitter", "Event", "emit"]
