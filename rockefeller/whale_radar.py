"""
Radar de ballenas — microestructura de mercado.

Conceptos implementados (Market Profile / order flow, y las herramientas
tipo OpenMarket-Kiyotaka y Velo que inspiran este módulo):

  1. Trades agresivos gigantes (aggTrades): tamaño absoluto en USD y
     z-score frente a la distribución reciente del propio símbolo.
  2. CVD (Cumulative Volume Delta): compras taker − ventas taker.
     Pendiente positiva ⇒ los agresores compran ⇒ confirma longs.
  3. Bid-Ask Depth Imbalance: profundidad del libro a ±X% del mid.
     Muro de asks dominante ⇒ presión vendedora ⇒ VETO a la entrada.

El radar nunca genera señales por sí solo: CONFIRMA o VETA señales
de las estrategias. Volumen incierto ⇒ no hay operación.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

import numpy as np


@dataclass
class WhaleVerdict:
    ok_to_buy: bool
    reason: str
    cvd_slope: float = 0.0
    ob_ratio: float = 1.0       # bids/asks (>1 = soporte comprador)
    big_buys: int = 0
    big_sells: int = 0


class WhaleRadar:
    """Mantiene una ventana móvil de aggTrades por símbolo y evalúa flujo."""

    def __init__(self, window_sec: int = 300, big_trade_usd: float = 150_000.0,
                 big_trade_z: float = 3.0, ob_depth_pct: float = 0.5,
                 ob_imbalance_veto: float = 0.75):
        self.window_sec = window_sec
        self.big_trade_usd = big_trade_usd
        self.big_trade_z = big_trade_z
        self.ob_depth_pct = ob_depth_pct
        self.ob_imbalance_veto = ob_imbalance_veto
        # symbol -> deque[(ts, signed_usd, usd)]
        self._trades: dict[str, deque] = {}

    # ---------- ingesta ----------
    def on_agg_trade(self, symbol: str, price: float, qty: float,
                     is_buyer_maker: bool, ts: float | None = None) -> None:
        """is_buyer_maker=True ⇒ el AGRESOR fue vendedor (taker sell)."""
        ts = ts or time.time()
        usd = price * qty
        signed = -usd if is_buyer_maker else usd
        dq = self._trades.setdefault(symbol, deque(maxlen=20_000))
        dq.append((ts, signed, usd))
        self._evict(symbol, ts)

    def _evict(self, symbol: str, now: float) -> None:
        dq = self._trades.get(symbol)
        if not dq:
            return
        cutoff = now - self.window_sec
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    # ---------- métricas ----------
    def cvd_slope(self, symbol: str) -> float:
        """Pendiente del CVD en la ventana (USD netos por segundo)."""
        dq = self._trades.get(symbol)
        if not dq or len(dq) < 10:
            return 0.0
        ts = np.array([t for t, _, _ in dq])
        cvd = np.cumsum([s for _, s, _ in dq])
        t = ts - ts[0]
        if t[-1] <= 0:
            return 0.0
        slope = np.polyfit(t, cvd, 1)[0]
        return float(slope)

    def big_trade_counts(self, symbol: str) -> tuple[int, int]:
        dq = self._trades.get(symbol)
        if not dq:
            return 0, 0
        sizes = np.array([u for _, _, u in dq])
        if len(sizes) < 20:
            thr = self.big_trade_usd
        else:
            thr = max(self.big_trade_usd,
                      float(sizes.mean() + self.big_trade_z * sizes.std()))
        buys = sum(1 for _, s, u in dq if u >= thr and s > 0)
        sells = sum(1 for _, s, u in dq if u >= thr and s < 0)
        return buys, sells

    @staticmethod
    def orderbook_ratio(bids: list, asks: list, mid: float,
                        depth_pct: float) -> float:
        """Σ notional de bids / Σ notional de asks dentro de ±depth_pct del mid."""
        lo, hi = mid * (1 - depth_pct / 100), mid * (1 + depth_pct / 100)
        b = sum(float(p) * float(q) for p, q in bids if float(p) >= lo)
        a = sum(float(p) * float(q) for p, q in asks if float(p) <= hi)
        if a <= 0:
            return 10.0
        return b / a

    # ---------- veredicto ----------
    def assess_long(self, symbol: str, bids: list | None = None,
                    asks: list | None = None, mid: float | None = None) -> WhaleVerdict:
        slope = self.cvd_slope(symbol)
        buys, sells = self.big_trade_counts(symbol)
        ob = 1.0
        if bids and asks and mid:
            ob = self.orderbook_ratio(bids, asks, mid, self.ob_depth_pct)

        # VETO 1: muro de venta en el libro
        if ob < self.ob_imbalance_veto:
            return WhaleVerdict(False, f"veto: presión vendedora en libro (b/a={ob:.2f})",
                                slope, ob, buys, sells)
        # VETO 2: ballenas descargando (grandes ventas dominan y CVD cae)
        if sells > buys and slope < 0:
            return WhaleVerdict(False, "veto: distribución de ballenas (CVD↓, big sells)",
                                slope, ob, buys, sells)
        # Confirmación positiva
        if slope > 0 or buys > sells:
            return WhaleVerdict(True, "flujo comprador confirmado", slope, ob, buys, sells)
        # Neutral: se permite, la estrategia ya exige sus propias confirmaciones
        return WhaleVerdict(True, "flujo neutral", slope, ob, buys, sells)
