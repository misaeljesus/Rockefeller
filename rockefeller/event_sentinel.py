"""
Event Sentinel — los "ojos de contexto" de Rockefeller.

Cierra la mayor debilidad del bot v1.1: la ceguera a noticias y régimen.
Tres capas, de lo programado a lo reactivo:

  1. CALENDARIO DE BLACKOUTS (events.json, editable por el usuario):
     ventanas alrededor de eventos macro (FOMC, CPI, NFP...) en las que
     NO se abren posiciones nuevas. Un quant no predice la noticia:
     simplemente no apuesta dentro del ruido. El archivo se recarga en
     caliente — puedes añadir eventos sin reiniciar el bot.

  2. SHOCK POR SÍMBOLO: antes solo BTC actuaba como proxy de pánico.
     Ahora cada símbolo con caída anómala (>k×ATR en 1h) queda vetado
     individualmente — las malas noticias de un token concreto (hack,
     delist, unlock) suelen verse primero en su propio precio.

  3. AMPLITUD DE MERCADO (breadth): % del top-40 cotizando sobre su VWAP
     de sesión. Con la tendencia de BTC clasifica el régimen:
        TRENDING  → todo permitido
        CHOPPY    → solo reversión (S1) y soportes (S3); rupturas fuera
        RISK_OFF  → nada nuevo; solo gestionar lo abierto
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger("rockefeller.sentinel")

DEFAULT_EVENTS = [
    # Plantilla — EDITA/AÑADE fechas reales en events.json.
    # Fuentes: calendario de la Fed, BLS (CPI/NFP), vencimientos de opciones.
    {"name": "FOMC (ejemplo — actualizar fecha)", "utc": "2026-09-16T18:00:00",
     "blackout_hours_before": 4, "blackout_hours_after": 3},
    {"name": "CPI EEUU (ejemplo — actualizar fecha)", "utc": "2026-08-12T12:30:00",
     "blackout_hours_before": 2, "blackout_hours_after": 2},
]


@dataclass
class RegimeVerdict:
    regime: str            # "TRENDING" | "CHOPPY" | "RISK_OFF"
    allow_new_entries: bool
    allowed_strategies: tuple
    reason: str


class EventSentinel:
    def __init__(self, events_path: str = "events.json",
                 shock_atr_mult: float = 3.0,
                 breadth_risk_off: float = 0.25,
                 breadth_trending: float = 0.55):
        self.events_path = events_path
        self.shock_atr_mult = shock_atr_mult
        self.breadth_risk_off = breadth_risk_off
        self.breadth_trending = breadth_trending
        self._events_mtime = 0.0
        self._events: list[dict] = []
        self._ensure_events_file()

    # ─────────── capa 1: calendario de blackouts ───────────
    def _ensure_events_file(self) -> None:
        if not os.path.exists(self.events_path):
            with open(self.events_path, "w") as f:
                json.dump(DEFAULT_EVENTS, f, indent=2, ensure_ascii=False)
            log.info("Creado %s con plantilla — edítalo con fechas reales",
                     self.events_path)

    def _load_events(self) -> list[dict]:
        try:
            mtime = os.path.getmtime(self.events_path)
            if mtime != self._events_mtime:
                with open(self.events_path) as f:
                    self._events = json.load(f)
                self._events_mtime = mtime
                log.info("Calendario recargado: %d eventos", len(self._events))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("events.json ilegible (%s); sin blackouts programados", e)
        return self._events

    def active_blackout(self) -> str | None:
        now = datetime.now(timezone.utc)
        for ev in self._load_events():
            try:
                t = datetime.fromisoformat(ev["utc"]).replace(tzinfo=timezone.utc)
                pre = ev.get("blackout_hours_before", 2) * 3600
                post = ev.get("blackout_hours_after", 2) * 3600
                if (t.timestamp() - pre) <= now.timestamp() <= (t.timestamp() + post):
                    return ev["name"]
            except (KeyError, ValueError):
                continue
        return None

    # ─────────── capa 2: shock por símbolo ───────────
    def symbol_shock(self, entry_df) -> bool:
        """Caída de la última hora > k×ATR ⇒ algo pasa con ESTE token."""
        if len(entry_df) < 2:
            return False
        last = entry_df.iloc[-1]
        atr = float(last.get("atr", 0.0))
        if atr <= 0:
            return False
        drop = float(entry_df["close"].iloc[-2]) - float(last["low"])
        return drop > self.shock_atr_mult * atr

    # ─────────── capa 3: régimen por amplitud ───────────
    def classify_regime(self, breadth_above_vwap: float,
                        btc_trend_up: bool, btc_adx: float) -> RegimeVerdict:
        b = breadth_above_vwap
        if b <= self.breadth_risk_off:
            return RegimeVerdict("RISK_OFF", False, (),
                                 f"amplitud {b:.0%}: el mercado entero vende")
        if b >= self.breadth_trending and btc_trend_up and btc_adx >= 18:
            return RegimeVerdict("TRENDING", True,
                                 ("S1_VWAP_PULLBACK", "S2_GSV_BREAKOUT",
                                  "S3_DOUBLE_BOTTOM", "S3_SUPPORT_BOUNCE"),
                                 f"amplitud {b:.0%} + BTC en tendencia")
        return RegimeVerdict("CHOPPY", True,
                             ("S1_VWAP_PULLBACK", "S3_DOUBLE_BOTTOM",
                              "S3_SUPPORT_BOUNCE"),
                             f"amplitud {b:.0%}: lateral — rupturas desactivadas")
