"""
Motor de datos — Binance Spot (REST + WebSockets vía python-binance).

  · Universo: top-40 pares USDT por volumen 24h, excluyendo stablecoins
    y tokens apalancados (sin edge direccional).
  · Velas 15m (entrada) y 4h (tendencia) cacheadas y refrescadas.
  · aggTrades en streaming alimentan al WhaleRadar.
  · Snapshot del order book bajo demanda para el imbalance check.
"""
from __future__ import annotations

import logging
import math
import time

import pandas as pd
from binance.client import Client

from config import Settings

log = logging.getLogger("rockefeller.data")

KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume",
              "close_time", "qav", "trades", "tbb", "tbq", "ignore"]


def to_df(raw: list) -> pd.DataFrame:
    df = pd.DataFrame(raw, columns=KLINE_COLS)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    return df.set_index("open_time")[["open", "high", "low", "close", "volume"]]


class DataEngine:
    def __init__(self, client: Client, settings: Settings, whale_radar=None):
        self.client = client
        self.s = settings
        self.whale = whale_radar
        self.universe: list[str] = []
        self._universe_ts = 0.0
        self._filters: dict[str, dict] = {}

    # ─────────────────── universo top-40 ───────────────────
    def refresh_universe(self, force: bool = False) -> list[str]:
        now = time.time()
        if (not force and self.universe
                and now - self._universe_ts < self.s.run.universe_refresh_min * 60):
            return self.universe

        u = self.s.universe
        tickers = self.client.get_ticker()  # 24h stats de todos los pares
        rows = []
        for t in tickers:
            sym = t["symbol"]
            if not sym.endswith(self.s.run.quote_asset):
                continue
            base = sym[: -len(self.s.run.quote_asset)]
            if base in u.exclude_bases:
                continue
            if any(base.endswith(sfx) for sfx in u.exclude_suffixes):
                continue
            qv = float(t["quoteVolume"])
            if qv < u.min_quote_volume_24h:
                continue
            rows.append((sym, qv))

        rows.sort(key=lambda r: r[1], reverse=True)
        new_universe = [sym for sym, _ in rows[: u.top_n]]
        if new_universe:
            self.universe = new_universe
            self._universe_ts = now
            self._load_filters()
            log.info("Universo actualizado (%d símbolos): %s ...",
                     len(self.universe), ", ".join(self.universe[:8]))
        return self.universe

    def _load_filters(self) -> None:
        info = self.client.get_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] in self.universe:
                f = {flt["filterType"]: flt for flt in s["filters"]}
                self._filters[s["symbol"]] = {
                    "step": float(f["LOT_SIZE"]["stepSize"]),
                    "tick": float(f["PRICE_FILTER"]["tickSize"]),
                    "min_notional": float(f.get("NOTIONAL", f.get("MIN_NOTIONAL", {"minNotional": 10})).get("minNotional", 10)),
                }

    # ─────────────────── redondeos de exchange ───────────────────
    def round_qty(self, symbol: str, qty: float) -> float:
        step = self._filters.get(symbol, {}).get("step", 1e-6)
        return math.floor(qty / step) * step

    def round_price(self, symbol: str, price: float) -> float:
        tick = self._filters.get(symbol, {}).get("tick", 1e-6)
        return math.floor(price / tick) * tick

    def min_notional(self, symbol: str) -> float:
        return self._filters.get(symbol, {}).get("min_notional", 10.0)

    # ─────────────────── velas ───────────────────
    def klines(self, symbol: str, interval: str, limit: int = 300) -> pd.DataFrame:
        raw = self.client.get_klines(symbol=symbol, interval=interval, limit=limit)
        return to_df(raw)

    def btc_change_1h(self) -> float:
        df = self.klines("BTC" + self.s.run.quote_asset, "5m", limit=13)
        if len(df) < 13:
            return 0.0
        return float((df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100)

    # ─────────────────── order book ───────────────────
    def orderbook(self, symbol: str, limit: int = 100) -> tuple[list, list, float]:
        ob = self.client.get_order_book(symbol=symbol, limit=limit)
        bids, asks = ob["bids"], ob["asks"]
        mid = (float(bids[0][0]) + float(asks[0][0])) / 2 if bids and asks else 0.0
        return bids, asks, mid

    # ─────────── radar de ballenas vía REST (v1.3, sin websockets) ───────────
    # Los websockets de python-binance filtran memoria en reconexiones y
    # provocaban OOM-kills en el VPS. Solución de fondo: snapshot REST de
    # los últimos aggTrades SOLO cuando hay una señal que evaluar. Sin
    # conexiones persistentes ⇒ sin fugas ⇒ sin muertes.
    def load_whale_snapshot(self, symbol: str, limit: int = 1000) -> bool:
        """Carga en el WhaleRadar los últimos `limit` aggTrades del símbolo."""
        if self.whale is None:
            return False
        try:
            trades = self.client.get_aggregate_trades(symbol=symbol, limit=limit)
        except Exception as e:
            log.debug("%s: snapshot de aggTrades no disponible (%s)", symbol, e)
            return False
        dq = self.whale._trades.get(symbol)
        if dq is not None:
            dq.clear()                      # snapshot fresco, sin datos viejos
        for t in trades:
            try:
                self.whale.on_agg_trade(
                    symbol=symbol, price=float(t["p"]), qty=float(t["q"]),
                    is_buyer_maker=bool(t["m"]), ts=t["T"] / 1000.0,
                )
            except (KeyError, ValueError):
                continue
        return True
