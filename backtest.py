"""
Backtester de Rockefeller — valida el edge antes de arriesgar un dólar.

"Backtest all strategies before live trading" es unánime en los 150+ libros.
Simula la lógica de señales + salidas (stop / TP1 parcial / TP2 / time-stop)
sobre velas 15m con contexto 4h, con fees y slippage incluidos.

Uso:
    python backtest.py BTCUSDT ETHUSDT SOLUSDT --days 120

Métricas: win-rate, profit factor (objetivo >1.5–2), expectancy, max DD,
retorno por día. Si el edge no aparece aquí, NO se despliega.
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd
from binance.client import Client

from config import SETTINGS
from rockefeller.data_engine import to_df
from rockefeller.indicators import enrich
from rockefeller.strategies import scan_symbol

FEE_PCT = 0.075 / 100      # taker con BNB; ajusta a tu tier
SLIP_PCT = 0.03 / 100


def fetch(client: Client, symbol: str, interval: str, days: int) -> pd.DataFrame:
    raw = client.get_historical_klines(symbol, interval, f"{days} day ago UTC")
    return to_df(raw)


def simulate(symbol: str, e15: pd.DataFrame, e4h: pd.DataFrame) -> list[dict]:
    s, ex = SETTINGS, SETTINGS.exit
    bars_per_hour = {"5m": 12, "15m": 4, "30m": 2, "1h": 1, "2h": 0.5}[s.regime.entry_tf]
    trades: list[dict] = []
    i = 220                      # warm-up de indicadores
    n = len(e15)
    while i < n - 1:
        window15 = e15.iloc[: i + 1]
        t_now = e15.index[i]
        window4h = e4h[e4h.index <= t_now]
        if len(window4h) < 210:
            i += 1
            continue

        sig = scan_symbol(symbol, window15, window4h, s)
        if sig is None:
            i += 1
            continue

        entry = sig.entry * (1 + SLIP_PCT)
        stop, target = sig.stop, sig.target
        risk = entry - stop
        if risk <= 0:
            i += 1
            continue

        tp1 = entry + ex.tp1_r_multiple * risk
        tp1_done, highest = False, entry
        pnl_r = 0.0
        max_bars = int(ex.max_holding_hours * bars_per_hour)
        j = i + 1
        while j < min(i + 1 + max_bars, n):
            bar = e15.iloc[j]
            highest = max(highest, bar["high"])
            # trailing tras TP1
            if tp1_done:
                atr_proxy = risk / ex.stop_atr_mult
                stop = max(stop, highest - ex.trail_atr_mult * atr_proxy)
            # ¿toca stop? (intra-vela: asumimos peor caso primero)
            if bar["low"] <= stop:
                fill = stop * (1 - SLIP_PCT)
                frac = (1 - ex.tp1_fraction) if tp1_done else 1.0
                pnl_r += frac * (fill - entry) / risk
                break
            if not tp1_done and bar["high"] >= tp1:
                fill = tp1 * (1 - SLIP_PCT)
                pnl_r += ex.tp1_fraction * (fill - entry) / risk
                tp1_done = True
                stop = entry * (1 + ex.breakeven_buffer_pct / 100)
            if bar["high"] >= target:
                fill = target * (1 - SLIP_PCT)
                frac = (1 - ex.tp1_fraction) if tp1_done else 1.0
                pnl_r += frac * (fill - entry) / risk
                break
            j += 1
        else:
            # time-stop al cierre
            fill = e15.iloc[min(j, n - 1)]["close"] * (1 - SLIP_PCT)
            frac = (1 - ex.tp1_fraction) if tp1_done else 1.0
            pnl_r += frac * (fill - entry) / risk

        # fees round-trip en unidades R
        pnl_r -= (2 * FEE_PCT) * entry / risk
        trades.append({"symbol": symbol, "strategy": sig.strategy,
                       "time": str(t_now), "pnl_r": pnl_r})
        i = j + 1                 # sin solapamiento por símbolo
    return trades


def report(trades: list[dict], days: int, risk_pct: float) -> None:
    if not trades:
        print("Sin operaciones. Ajusta parámetros o amplía el periodo.")
        return
    df = pd.DataFrame(trades)
    r = df["pnl_r"].values
    wins, losses = r[r > 0], r[r <= 0]
    wr = len(wins) / len(r) * 100
    pf = wins.sum() / abs(losses.sum()) if len(losses) and losses.sum() != 0 else float("inf")
    expectancy = r.mean()
    eq = (1 + r * risk_pct / 100).cumprod()
    dd = float((1 - eq / np.maximum.accumulate(eq)).max() * 100)
    daily = (eq[-1] ** (1 / days) - 1) * 100

    print("\n══════════ ROCKEFELLER · BACKTEST ══════════")
    print(f"Operaciones:      {len(r)}   (≈{len(r)/days:.2f}/día)")
    print(f"Win-rate:         {wr:.1f}%")
    print(f"Profit factor:    {pf:.2f}   (objetivo > 1.5)")
    print(f"Expectancy:       {expectancy:+.3f} R por trade")
    print(f"Retorno total:    {(eq[-1]-1)*100:+.2f}%  con riesgo {risk_pct}%/trade")
    print(f"Retorno diario:   {daily:+.3f}%/día (compuesto)")
    print(f"Max drawdown:     {dd:.2f}%")
    print("\nPor estrategia:")
    print(df.groupby("strategy")["pnl_r"]
            .agg(trades="count", winrate=lambda x: (x > 0).mean() * 100,
                 mean_R="mean").round(3).to_string())
    print("═════════════════════════════════════════════")
    if pf < 1.3:
        print("⚠ Edge insuficiente tras costes: NO desplegar. Revisar parámetros.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="+")
    ap.add_argument("--days", type=int, default=90)
    args = ap.parse_args()

    client = Client()  # datos públicos: no requiere claves
    all_trades: list[dict] = []
    for sym in args.symbols:
        print(f"Descargando {sym} ...")
        e15 = enrich(fetch(client, sym, SETTINGS.regime.entry_tf, args.days),
                     SETTINGS.vwap.band_k1, SETTINGS.vwap.band_k2)
        e4h = enrich(fetch(client, sym, "4h", args.days + 60),
                     SETTINGS.vwap.band_k1, SETTINGS.vwap.band_k2)
        all_trades += simulate(sym, e15, e4h)
    report(all_trades, args.days, SETTINGS.risk.risk_per_trade_pct)


if __name__ == "__main__":
    main()
