"""
Calendar Sync — calendario económico automático.

Descarga el feed JSON semanal de ForexFactory (gratuito, sin API key,
el calendario más usado por sistemas de trading) y reescribe events.json
con los eventos de alto impacto en USD: FOMC, CPI, NFP, PCE, decisiones
de tipos... Todo lo que mueve a cripto vía el dólar.

Diseño fail-safe (mentalidad quant: el sistema degrada, nunca rompe):
  · Si el feed no responde, se conserva el último events.json válido.
  · Las entradas MANUALES del usuario se preservan siempre (todo lo que
    no tenga "source": "auto" se respeta al fusionar). Puedes seguir
    añadiendo eventos propios: unlocks de tokens, hard forks, etc.
  · Ventanas de blackout escaladas por importancia: FOMC/tipos 4h antes
    y 3h después; resto de alto impacto 2h/2h.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger("rockefeller.calendar")

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
# palabras que amplían la ventana de blackout (los movedores de mercado)
MAJOR_KEYWORDS = ("fomc", "federal funds", "interest rate", "rate decision",
                  "press conference", "powell")


class CalendarSync:
    def __init__(self, events_path: str = "events.json",
                 refresh_hours: float = 6.0,
                 currencies: tuple = ("USD",),
                 impacts: tuple = ("High",),
                 timeout: int = 15):
        self.events_path = events_path
        self.refresh_sec = refresh_hours * 3600
        self.currencies = set(currencies)
        self.impacts = set(impacts)
        self.timeout = timeout
        self._last_sync = 0.0

    # ─────────────── ciclo público ───────────────
    def maybe_sync(self) -> None:
        """Llamar cada ciclo del bot: solo actúa cada refresh_hours."""
        if time.time() - self._last_sync < self.refresh_sec:
            return
        self._last_sync = time.time()
        try:
            auto_events = self._fetch()
        except Exception as e:
            log.warning("Calendario: feed no disponible (%s). "
                        "Se conserva el events.json anterior.", e)
            return
        merged = self._merge_with_manual(auto_events)
        try:
            with open(self.events_path, "w") as f:
                json.dump(merged, f, indent=2, ensure_ascii=False)
            log.info("Calendario sincronizado: %d eventos auto + %d manuales",
                     len(auto_events), len(merged) - len(auto_events))
        except OSError as e:
            log.error("Calendario: no se pudo escribir events.json (%s)", e)

    # ─────────────── descarga y parseo ───────────────
    def _fetch(self) -> list[dict]:
        resp = requests.get(FEED_URL, timeout=self.timeout,
                            headers={"User-Agent": "rockefeller-bot/1.2"})
        resp.raise_for_status()
        return self.parse_feed(resp.json())

    def parse_feed(self, raw: list[dict]) -> list[dict]:
        """Convierte el formato ForexFactory al formato de events.json."""
        out = []
        for ev in raw:
            try:
                if ev.get("country") not in self.currencies:
                    continue
                if ev.get("impact") not in self.impacts:
                    continue
                # fechas tipo "2026-08-12T08:30:00-04:00" → UTC naive ISO
                dt = datetime.fromisoformat(ev["date"]).astimezone(timezone.utc)
                title = str(ev.get("title", "evento"))
                is_major = any(k in title.lower() for k in MAJOR_KEYWORDS)
                out.append({
                    "name": f"{ev.get('country', '')} {title}",
                    "utc": dt.strftime("%Y-%m-%dT%H:%M:%S"),
                    "blackout_hours_before": 4 if is_major else 2,
                    "blackout_hours_after": 3 if is_major else 2,
                    "source": "auto",
                })
            except (KeyError, ValueError, TypeError):
                continue
        return out

    # ─────────────── fusión con eventos manuales ───────────────
    def _merge_with_manual(self, auto_events: list[dict]) -> list[dict]:
        manual: list[dict] = []
        if os.path.exists(self.events_path):
            try:
                with open(self.events_path) as f:
                    for ev in json.load(f):
                        if ev.get("source") != "auto":
                            manual.append(ev)
            except (OSError, json.JSONDecodeError):
                pass
        # descartar manuales ya pasados hace más de 2 días (limpieza)
        cutoff = datetime.now(timezone.utc).timestamp() - 2 * 86400
        keep = []
        for ev in manual:
            try:
                t = datetime.fromisoformat(ev["utc"]).replace(
                    tzinfo=timezone.utc).timestamp()
                if t >= cutoff:
                    keep.append(ev)
            except (KeyError, ValueError):
                keep.append(ev)   # si no se puede parsear, no se borra
        return keep + auto_events
