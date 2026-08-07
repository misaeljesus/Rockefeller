"""
Indicadores cuantitativos. Todo vectorizado con numpy/pandas.
El VWAP de sesión (ancla diaria UTC) es la herramienta principal:
es la referencia de "precio justo" que usan los desks institucionales
para medir la calidad de sus ejecuciones — operar a favor del lado
correcto del VWAP es operar con las instituciones, no contra ellas.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ────────────────────────────── VWAP ──────────────────────────────
def session_vwap(df: pd.DataFrame, k1: float = 1.0, k2: float = 2.0) -> pd.DataFrame:
    """
    VWAP anclado al inicio de cada día UTC + bandas de desviación estándar
    ponderadas por volumen. df requiere index datetime UTC y columnas
    high, low, close, volume.
    Devuelve df con: vwap, vwap_up1, vwap_dn1, vwap_up2, vwap_dn2, vwap_dist_pct
    """
    out = df.copy()
    tp = (out["high"] + out["low"] + out["close"]) / 3.0
    day = out.index.floor("D")

    pv = tp * out["volume"]
    cum_pv = pv.groupby(day).cumsum()
    cum_v = out["volume"].groupby(day).cumsum().replace(0, np.nan)
    vwap = cum_pv / cum_v

    # varianza ponderada por volumen acumulada por sesión
    cum_pv2 = (tp * tp * out["volume"]).groupby(day).cumsum()
    var = (cum_pv2 / cum_v) - vwap**2
    sd = np.sqrt(var.clip(lower=0))

    out["vwap"] = vwap
    out["vwap_up1"] = vwap + k1 * sd
    out["vwap_dn1"] = vwap - k1 * sd
    out["vwap_up2"] = vwap + k2 * sd
    out["vwap_dn2"] = vwap - k2 * sd
    out["vwap_dist_pct"] = (out["close"] - vwap) / vwap * 100.0
    return out


def rolling_vwap(df: pd.DataFrame, window: int = 96) -> pd.Series:
    """VWAP móvil (p. ej. 96 velas de 15m = 24h) para contexto continuo."""
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = (tp * df["volume"]).rolling(window).sum()
    v = df["volume"].rolling(window).sum().replace(0, np.nan)
    return pv / v


# ────────────────────────── Volatilidad ───────────────────────────
def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def greatest_swing_value(df: pd.DataFrame, lookback: int = 4) -> float:
    """
    GSV de Larry Williams adaptado a cripto (mercado 24/7):
    media del swing comprador (high - open) de los últimos días *bajistas*.
    LW: "los últimos 1 a 4 días producen el mejor valor". La ruptura de
    open + GSV tras un cierre bajista es un fallo del swing vendedor
    ⇒ breakout de volatilidad con lógica, no arbitrario.
    """
    d = df.tail(lookback + 12)
    down = d[d["close"] < d["open"]]
    if len(down) == 0:
        return float((d["high"] - d["open"]).tail(lookback).mean())
    return float((down["high"] - down["open"]).tail(lookback).mean())


# ─────────────────────────── Momentum ─────────────────────────────
def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def ema(close: pd.Series, period: int) -> pd.Series:
    return close.ewm(span=period, adjust=False).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ADX de Wilder — mide fuerza de tendencia (no dirección).
    ADX < ~18 ⇒ lateral: el chartismo aconseja no operar rupturas ahí."""
    h, l, c = df["high"], df["low"], df["close"]
    up = h.diff()
    dn = -l.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr_w = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr_w
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr_w
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False).mean().fillna(0.0)


# ──────────────────────────── Volumen ─────────────────────────────
def volume_zscore(volume: pd.Series, window: int = 50) -> pd.Series:
    """Z-score del volumen. Anna Coulling / chartismo: el volumen valida
    el movimiento; una ruptura sin volumen es una ruptura falsa."""
    mean = volume.rolling(window).mean()
    std = volume.rolling(window).std().replace(0, np.nan)
    return ((volume - mean) / std).fillna(0.0)


def enrich(df: pd.DataFrame, k1: float, k2: float) -> pd.DataFrame:
    """Pipeline completo de indicadores sobre un dataframe OHLCV."""
    out = session_vwap(df, k1, k2)
    out["atr"] = atr(out)
    out["rsi"] = rsi(out["close"])
    out["ema20"] = ema(out["close"], 20)
    out["ema50"] = ema(out["close"], 50)
    out["ema200"] = ema(out["close"], 200)
    out["adx"] = adx(out)
    out["vol_z"] = volume_zscore(out["volume"])
    return out
