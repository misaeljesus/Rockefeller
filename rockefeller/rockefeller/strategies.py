"""
Estrategias de Rockefeller — largo-solo (spot), alta selectividad.

Regla 80/20: pocas operaciones, de máxima calidad. Cada estrategia es un
checklist estricto; si falta UNA condición, no hay señal. El radar de
ballenas actúa después como confirmación/veto final.

S1 · VWAP Pullback   (principal — reversión al precio justo en tendencia)
S2 · VWAP+GSV Breakout (Larry Williams — ruptura de volatilidad con volumen)
S3 · Rebote en soporte / doble suelo (chartismo — zonas con ≥2 toques)
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from config import Settings
from . import patterns
from .indicators import greatest_swing_value


@dataclass
class Signal:
    symbol: str
    strategy: str
    entry: float
    stop: float
    target: float
    grade: str          # "A" | "A+"
    note: str

    @property
    def rr(self) -> float:
        risk = self.entry - self.stop
        return (self.target - self.entry) / risk if risk > 0 else 0.0


def _uptrend_ok(trend_df: pd.DataFrame, cfg: Settings) -> bool:
    """Filtro de tendencia en 4h: nunca comprar contra tendencia (chartismo).
    EMA20 > EMA50, precio sobre EMA50 y sobre el VWAP de sesión."""
    last = trend_df.iloc[-1]
    return bool(
        last["ema20"] > last["ema50"]
        and last["close"] > last["ema50"]
        and last["close"] > last["vwap"]
    )


# ────────────────────────── S1 · VWAP PULLBACK ──────────────────────────
def s1_vwap_pullback(symbol: str, entry_df: pd.DataFrame,
                     trend_df: pd.DataFrame, cfg: Settings) -> Signal | None:
    """
    En tendencia alcista (4h), el precio retrocede a la zona VWAP ↔ VWAP−1σ
    en 15m con RSI enfriado y vela de rechazo (martillo/envolvente).
    Es la operación institucional clásica: comprar el activo fuerte a su
    precio medio ponderado por volumen, no perseguirlo arriba.
    """
    if not _uptrend_ok(trend_df, cfg):
        return None
    last = entry_df.iloc[-1]
    v = cfg.vwap
    price, vwap_, atr_ = float(last["close"]), float(last["vwap"]), float(last["atr"])
    if atr_ <= 0 or vwap_ <= 0:
        return None

    # precio dentro de la zona de valor: entre VWAP−1σ y VWAP+0.35·ATR
    in_zone = (float(last["vwap_dn1"]) <= price
               <= vwap_ + v.s1_max_dist_to_vwap_atr * atr_)
    rsi_ok = v.s1_rsi_low <= float(last["rsi"]) <= v.s1_rsi_high
    candle = patterns.bullish_candle_signal(entry_df)
    if not (in_zone and rsi_ok and candle):
        return None

    stop = min(float(last["vwap_dn2"]), price - cfg.exit.stop_atr_mult * atr_)
    target = max(float(last["vwap_up2"]), price + cfg.exit.tp2_r_multiple * (price - stop))
    grade = "A+" if (candle == "bullish_engulfing" and float(last["vol_z"]) > 1.0) else "A"
    return Signal(symbol, "S1_VWAP_PULLBACK", price, stop, target, grade,
                  f"pullback a VWAP con {candle}, RSI {last['rsi']:.0f}")


# ─────────────────────── S2 · GSV VOLATILITY BREAKOUT ───────────────────
def s2_gsv_breakout(symbol: str, entry_df: pd.DataFrame,
                    trend_df: pd.DataFrame, cfg: Settings) -> Signal | None:
    """
    Adaptación del Greatest Swing Value de Larry Williams + validación
    chartista: ruptura de máximos de N velas, por encima de open+GSV,
    precio sobre VWAP y volumen z≥2 (sin volumen, la ruptura no vale).
    Prohibida en mercado lateral (ADX bajo).
    """
    last = entry_df.iloc[-1]
    if float(trend_df.iloc[-1]["adx"]) < cfg.regime.adx_min_trend:
        return None
    if not _uptrend_ok(trend_df, cfg):
        return None
    if float(last["close"]) <= float(last["vwap"]):
        return None

    bo = patterns.confirmed_breakout(entry_df, cfg.vwap.s2_breakout_lookback,
                                     cfg.vwap.s2_vol_zscore_min)
    if not bo:
        return None

    # filtro GSV: la vela debe superar open + GSV (fallo del swing vendedor)
    gsv = greatest_swing_value(trend_df, cfg.vwap.s2_gsv_lookback)
    trigger = float(trend_df.iloc[-1]["open"]) + cfg.vwap.s2_gsv_mult * gsv
    price = float(last["close"])
    if price < trigger:
        return None

    atr_ = float(last["atr"])
    stop = max(float(last["vwap"]), bo["breakout_level"] - cfg.exit.stop_atr_mult * atr_)
    if stop >= price:
        stop = price - cfg.exit.stop_atr_mult * atr_
    target = price + cfg.exit.tp2_r_multiple * (price - stop)
    grade = "A+" if bo["vol_z"] >= cfg.vwap.s2_vol_zscore_min + 1 else "A"
    return Signal(symbol, "S2_GSV_BREAKOUT", price, stop, target, grade,
                  f"ruptura {bo['breakout_level']:.6g} con vol_z {bo['vol_z']:.1f}, GSV ok")


# ─────────────────── S3 · SOPORTE / DOBLE SUELO ─────────────────────────
def s3_support_bounce(symbol: str, entry_df: pd.DataFrame,
                      trend_df: pd.DataFrame, cfg: Settings) -> Signal | None:
    """
    Chartismo puro: zona de soporte con ≥2 toques (ideal 3) o doble suelo
    confirmado sobre el neckline. Entrada con vela de rechazo; stop bajo la
    zona (margen anti-falsas); objetivo en la resistencia más cercana o la
    proyección de la figura. No exige tendencia 4h alcista, pero sí que el
    activo no esté en caída libre (precio ≥ EMA200 de 4h).
    """
    t_last = trend_df.iloc[-1]
    if float(t_last["close"]) < float(t_last["ema200"]):
        return None
    last = entry_df.iloc[-1]
    price, atr_ = float(last["close"]), float(last["atr"])

    # opción A: doble suelo confirmado
    db = patterns.double_bottom(entry_df, cfg.vwap.s3_zone_tolerance_pct)
    if db:
        stop = db["stop_ref"] * (1 - 0.001) - 0.5 * atr_
        return Signal(symbol, "S3_DOUBLE_BOTTOM", price, stop, db["target"], "A+",
                      "doble suelo confirmado sobre neckline")

    # opción B: rebote en zona de soporte con vela alcista
    zones = patterns.sr_zones(entry_df, cfg.vwap.s3_zone_tolerance_pct,
                              cfg.vwap.s3_min_touches)
    sup = patterns.nearest_support(zones, price)
    res = patterns.nearest_resistance(zones, price)
    candle = patterns.bullish_candle_signal(entry_df)
    if not (sup and candle and sup.contains(price, cfg.vwap.s3_zone_tolerance_pct * 2)):
        return None

    stop = sup.level * (1 - cfg.vwap.s3_zone_tolerance_pct / 100.0) - 0.25 * atr_
    target = res.level if res else price + cfg.exit.tp2_r_multiple * (price - stop)
    grade = "A+" if sup.touches >= 3 else "A"
    return Signal(symbol, "S3_SUPPORT_BOUNCE", price, stop, target, grade,
                  f"rebote en soporte {sup.level:.6g} ({sup.touches} toques) con {candle}")


ALL_STRATEGIES = (s1_vwap_pullback, s2_gsv_breakout, s3_support_bounce)

# ── WHITELIST OPERATIVA (backtest 120d, ago-2026) ──
# S1 demostró edge real (68% WR, +0.23R). S2 y S3, con los parámetros
# actuales, resultaron perdedoras en este régimen: quedan DESACTIVADAS
# hasta recalibrar y re-validar en backtest. No se opera lo que no
# demostró ganar — el dato mata la opinión.
ENABLED_STRATEGIES = ("S1_VWAP_PULLBACK",)


def scan_symbol(symbol: str, entry_df: pd.DataFrame, trend_df: pd.DataFrame,
                cfg: Settings) -> Signal | None:
    """Devuelve la mejor señal del símbolo (prioriza A+ y mayor R:R)."""
    signals = [s for fn in ALL_STRATEGIES
               if (s := fn(symbol, entry_df, trend_df, cfg)) is not None
               and s.strategy in ENABLED_STRATEGIES]
    if not signals:
        return None
    signals.sort(key=lambda s: (s.grade == "A+", s.rr), reverse=True)
    return signals[0]
