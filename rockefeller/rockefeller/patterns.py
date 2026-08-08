"""
Detección de patrones chartistas (Manual de Chartismo, F. Gallofré +
Encyclopedia of Chart Patterns, Bulkowski).

Reglas del manual codificadas aquí:
  - Soportes/resistencias son ZONAS, no niveles exactos → tolerancia %.
  - A más toques, más fuerte el nivel (mín. 2, ideal 3).
  - Tras la ruptura, los roles se invierten (resistencia → soporte).
  - Margen anti-rupturas falsas: exigir cierre más allá de la zona.
  - El volumen debe confirmar la ruptura; sin volumen = probable fallo.
  - Nunca operar contra la tendencia; en lateral, no operar rupturas.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Zone:
    level: float
    touches: int
    kind: str  # "support" | "resistance"

    def contains(self, price: float, tol_pct: float) -> bool:
        return abs(price - self.level) / self.level * 100.0 <= tol_pct


# ─────────────────────── Pivotes y zonas S/R ───────────────────────
def _pivots(df: pd.DataFrame, left: int = 3, right: int = 3):
    """Pivotes fractales (swing highs/lows)."""
    highs, lows = df["high"].values, df["low"].values
    n = len(df)
    ph, pl = [], []
    for i in range(left, n - right):
        if highs[i] == max(highs[i - left:i + right + 1]):
            ph.append((i, highs[i]))
        if lows[i] == min(lows[i - left:i + right + 1]):
            pl.append((i, lows[i]))
    return ph, pl


def sr_zones(df: pd.DataFrame, tol_pct: float = 0.4, min_touches: int = 2,
             max_zones: int = 8) -> list[Zone]:
    """Agrupa pivotes en zonas por proximidad (clustering 1D simple)."""
    ph, pl = _pivots(df)
    last = float(df["close"].iloc[-1])
    zones: list[Zone] = []

    for pts, _kind in ((pl, "support"), (ph, "resistance")):
        levels = sorted(p for _, p in pts)
        cluster: list[float] = []
        for lv in levels:
            if cluster and (lv - cluster[0]) / cluster[0] * 100.0 > tol_pct:
                if len(cluster) >= min_touches:
                    m = float(np.mean(cluster))
                    zones.append(Zone(m, len(cluster),
                                      "support" if m < last else "resistance"))
                cluster = []
            cluster.append(lv)
        if len(cluster) >= min_touches:
            m = float(np.mean(cluster))
            zones.append(Zone(m, len(cluster),
                              "support" if m < last else "resistance"))

    # las más cercanas al precio actual son las operables
    zones.sort(key=lambda z: abs(z.level - last))
    return zones[:max_zones]


def nearest_support(zones: list[Zone], price: float) -> Zone | None:
    sup = [z for z in zones if z.kind == "support" and z.level < price]
    return max(sup, key=lambda z: z.level) if sup else None


def nearest_resistance(zones: list[Zone], price: float) -> Zone | None:
    res = [z for z in zones if z.kind == "resistance" and z.level > price]
    return min(res, key=lambda z: z.level) if res else None


# ─────────────────────── Patrones de velas ────────────────────────
def bullish_candle_signal(df: pd.DataFrame) -> str | None:
    """Martillo o envolvente alcista en la última vela cerrada.
    (Nison: funcionan mejor EN soporte y con confirmación de volumen —
    esa condición la impone la estrategia, no este detector)."""
    if len(df) < 3:
        return None
    prev, cur = df.iloc[-2], df.iloc[-1]
    body = abs(cur["close"] - cur["open"])
    rng = cur["high"] - cur["low"]
    if rng <= 0:
        return None
    lower_wick = min(cur["open"], cur["close"]) - cur["low"]
    upper_wick = cur["high"] - max(cur["open"], cur["close"])

    # Martillo: mecha inferior ≥ 2x cuerpo, mecha superior pequeña
    if body > 0 and lower_wick >= 2 * body and upper_wick <= 0.5 * body:
        return "hammer"
    # Envolvente alcista
    if (prev["close"] < prev["open"] and cur["close"] > cur["open"]
            and cur["close"] >= prev["open"] and cur["open"] <= prev["close"]):
        return "bullish_engulfing"
    return None


# ─────────────────────── Doble suelo ──────────────────────────────
def double_bottom(df: pd.DataFrame, tol_pct: float = 0.5,
                  lookback: int = 60) -> dict | None:
    """
    Doble suelo (figura de cambio de tendencia del manual):
    dos mínimos a la misma altura (± tol) separados por un pico intermedio;
    se confirma al romper el pico (neckline). Objetivo = altura de la figura.
    """
    d = df.tail(lookback)
    _, pl = _pivots(d)
    if len(pl) < 2:
        return None
    (i1, low1), (i2, low2) = pl[-2], pl[-1]
    if i2 - i1 < 5:
        return None
    if abs(low1 - low2) / low1 * 100.0 > tol_pct:
        return None
    between = d.iloc[i1:i2 + 1]
    neckline = float(between["high"].max())
    last_close = float(d["close"].iloc[-1])
    height = neckline - min(low1, low2)
    if height <= 0:
        return None
    # confirmación: cierre por encima del neckline (margen anti-falsas)
    if last_close > neckline * 1.001:
        return {
            "neckline": neckline,
            "target": neckline + height,       # proyección de la figura
            "stop_ref": float(min(low1, low2)),
            "pattern": "double_bottom",
        }
    return None


# ─────────────────── Validación de rupturas ───────────────────────
def confirmed_breakout(df: pd.DataFrame, lookback: int = 20,
                       vol_z_min: float = 2.0) -> dict | None:
    """
    Ruptura de máximos de `lookback` velas con CIERRE por encima
    (margen anti-falsas) y volumen z-score ≥ umbral.
    Chartismo: "ruptura con volumen bajo = probable fallo".
    """
    if len(df) < lookback + 2:
        return None
    window = df.iloc[-(lookback + 1):-1]
    hi = float(window["high"].max())
    cur = df.iloc[-1]
    if float(cur["close"]) > hi * 1.001 and float(cur.get("vol_z", 0.0)) >= vol_z_min:
        return {"breakout_level": hi, "vol_z": float(cur["vol_z"])}
    return None
