"""
Módulo adaptativo — el bot que se audita a sí mismo.

Cierra la segunda debilidad: la rigidez. Un quant profesional no cree en
su sistema por fe; lo mide en ventana móvil y desconecta lo que dejó de
funcionar ANTES de que el drawdown lo obligue.

  · TradeJournal: cada operación cerrada se persiste en trades.csv
    (sobrevive reinicios). Es la fuente de verdad para auditoría y para
    el governor. "Keep a trading journal" es unánime en la literatura.

  · StrategyGovernor: por estrategia, calcula la expectancy (en R) de sus
    últimas N operaciones. Si cae bajo el umbral, la estrategia queda
    SUSPENDIDA una semana: sigue generando señales en el log (shadow
    mode) pero no opera dinero. Si el mercado vuelve a favorecerla, la
    re-auditoría la reactiva. Nunca suspende TODO a la vez sin motivo:
    cada estrategia se juzga por su propio historial.
"""
from __future__ import annotations

import csv
import logging
import os
import time
from collections import defaultdict, deque

log = logging.getLogger("rockefeller.adaptive")

FIELDS = ["ts", "symbol", "strategy", "reason", "entry", "exit",
          "pnl_quote", "pnl_r", "pnl_pct_equity", "held_h"]


class TradeJournal:
    def __init__(self, path: str = "trades.csv"):
        self.path = path
        if not os.path.exists(path):
            with open(path, "w", newline="") as f:
                csv.DictWriter(f, FIELDS).writeheader()

    def record(self, row: dict) -> None:
        clean = {k: row.get(k, "") for k in FIELDS}
        with open(self.path, "a", newline="") as f:
            csv.DictWriter(f, FIELDS).writerow(clean)

    def load_recent(self, per_strategy: int = 40) -> dict[str, deque]:
        hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=per_strategy))
        try:
            with open(self.path) as f:
                for row in csv.DictReader(f):
                    try:
                        hist[row["strategy"]].append(float(row["pnl_r"]))
                    except (KeyError, ValueError):
                        continue
        except OSError:
            pass
        return hist


class StrategyGovernor:
    def __init__(self, journal: TradeJournal, window: int = 12,
                 min_trades: int = 8, expectancy_floor_r: float = -0.10,
                 suspension_days: float = 7.0):
        self.journal = journal
        self.window = window
        self.min_trades = min_trades
        self.floor = expectancy_floor_r
        self.suspension_sec = suspension_days * 86400
        self.suspended_until: dict[str, float] = {}
        self.history = journal.load_recent()

    def record_close(self, strategy: str, pnl_r: float) -> None:
        self.history.setdefault(strategy, deque(maxlen=40)).append(pnl_r)
        self._audit(strategy)

    def _audit(self, strategy: str) -> None:
        h = list(self.history.get(strategy, []))[-self.window:]
        if len(h) < self.min_trades:
            return
        expectancy = sum(h) / len(h)
        if expectancy < self.floor and not self.is_suspended(strategy):
            self.suspended_until[strategy] = time.time() + self.suspension_sec
            log.warning("GOVERNOR: %s suspendida %d días — expectancy %.2fR "
                        "en últimas %d ops (umbral %.2fR). Pasa a shadow mode.",
                        strategy, int(self.suspension_sec / 86400), expectancy,
                        len(h), self.floor)

    def is_suspended(self, strategy: str) -> bool:
        until = self.suspended_until.get(strategy, 0.0)
        if until and time.time() >= until:
            del self.suspended_until[strategy]
            log.info("GOVERNOR: %s reactivada tras suspensión — a prueba", strategy)
            return False
        return bool(until)

    def status(self) -> str:
        parts = []
        for strat, h in self.history.items():
            recent = list(h)[-self.window:]
            e = sum(recent) / len(recent) if recent else 0.0
            flag = "⛔" if self.is_suspended(strat) else "✓"
            parts.append(f"{flag}{strat}:{e:+.2f}R({len(recent)})")
        return " | ".join(parts) if parts else "sin historial aún"
