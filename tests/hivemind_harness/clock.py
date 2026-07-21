# -*- coding: utf-8 -*-
"""
Horloge logique déterministe pour le harnais d'injection de fautes (issue #11).

Aucune horloge murale, aucun ``time.sleep`` : le temps n'avance QUE par appel
explicite à ``tick``. Cela garantit que les scénarios de fenêtre de rejeu et
d'expiration de lease sont 100 % reproductibles.

L'horloge expose un ``callable`` ``now()`` qui retourne un ``datetime`` UTC,
compatible avec le seam ``clock=`` de ``HivemindPeerChannel`` (peer.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


class DeterministicClock:
    """
    Temps logique avançant uniquement sur ``tick(delta)``.

    Usage::

        clock = DeterministicClock()
        channel = HivemindPeerChannel(..., clock=clock.now)
        clock.tick(seconds=600)  # fait expirer une lease / fenêtre de rejeu

    L'instance elle-même est appelable (``clock()`` == ``clock.now()``) pour
    rester un drop-in du ``Callable[[], datetime]`` attendu par le channel.
    """

    def __init__(self, start: datetime | None = None) -> None:
        if start is None:
            start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        self._now = start.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._now

    def __call__(self) -> datetime:
        return self._now

    def iso(self) -> str:
        return self._now.isoformat()

    def tick(
        self,
        *,
        seconds: int = 0,
        minutes: int = 0,
        hours: int = 0,
        days: int = 0,
    ) -> datetime:
        """Avance le temps logique. Refuse de reculer."""
        delta = timedelta(seconds=seconds, minutes=minutes, hours=hours, days=days)
        if delta < timedelta(0):
            raise ValueError("DeterministicClock.tick ne peut pas reculer le temps")
        self._now = self._now + delta
        return self._now
