"""
Orquestador de Rockefeller — el ciclo de decisión del quant.

Cada ciclo:
  1. Gestiona posiciones abiertas (stops, TP1/TP2, trailing, time-stop).
  2. Chequea el régimen de mercado (shock BTC ⇒ defensa / kill-switch).
  3. Escanea el top-40 buscando señales A/A+ de las 3 estrategias.
  4. Confirma cada señal con el radar de ballenas (CVD + order book).
  5. Pide autorización y tamaño al gestor de riesgo (checklist completo).
  6. Ejecuta con orden LIMIT. Registra todo.

"Take every trade that meets your criteria, without reservation or
hesitation" (Douglas) — y ninguno que no los cumpla (anti-avaricia).
"""
from __future__ import annotations

import logging
import time

from binance.client import Client

from config import Settings
from .adaptive import StrategyGovernor, TradeJournal
from .calendar_sync import CalendarSync
from .data_engine import DataEngine
from .event_sentinel import EventSentinel
from .executor import Executor
from .indicators import enrich
from .risk_manager import RiskManager
from .strategies import scan_symbol
from .whale_radar import WhaleRadar

log = logging.getLogger("rockefeller.bot")


class Rockefeller:
    def __init__(self, api_key: str, api_secret: str, settings: Settings):
        self.s = settings
        testnet = settings.run.mode == "testnet"
        self.client = Client(api_key, api_secret, testnet=testnet)
        self.whale = WhaleRadar(
            window_sec=settings.whale.window_sec,
            big_trade_usd=settings.whale.big_trade_usd,
            big_trade_z=settings.whale.big_trade_zscore,
            ob_depth_pct=settings.whale.ob_depth_pct,
            ob_imbalance_veto=settings.whale.ob_imbalance_veto,
        )
        self.data = DataEngine(self.client, settings, self.whale)
        self.executor = Executor(self.client, self.data, settings)
        self.risk = RiskManager(settings.risk, initial_equity=self.executor.equity()
                                if settings.run.mode != "paper" else settings.run.starting_capital_usdt)
        # v1.2 — contexto y adaptabilidad
        self.sentinel = EventSentinel()
        self.calendar = CalendarSync()          # calendario económico automático
        self.journal = TradeJournal()
        self.governor = StrategyGovernor(self.journal)
        self.executor.on_trade_closed = self._on_trade_closed
        self._api_key, self._api_secret, self._testnet = api_key, api_secret, testnet

    def _on_trade_closed(self, trade: dict) -> None:
        self.journal.record(trade)
        self.governor.record_close(trade["strategy"], trade["pnl_r"])
        log.info("GOVERNOR estado: %s", self.governor.status())

    # ─────────────────── ciclo principal ───────────────────
    def run(self) -> None:
        log.info("Rockefeller iniciado en modo %s", self.s.run.mode.upper())
        self.data.refresh_universe(force=True)

        while True:
            try:
                self._cycle()
            except KeyboardInterrupt:
                log.info("Parada manual. %s", self.risk.summary())
                break
            except Exception as e:
                log.exception("Error en ciclo: %s — pausa defensiva 60s", e)
                time.sleep(60)
            time.sleep(self.s.run.loop_interval_sec)

    def _cycle(self) -> None:
        # 1) gestionar lo abierto SIEMPRE primero (defensa antes que ataque)
        self.executor.manage(self.risk)

        equity = self.executor.equity()
        self.risk.update_equity_peak(equity)

        # 2) régimen de mercado
        btc_1h = self.data.btc_change_1h()
        if btc_1h <= self.s.risk.btc_kill_switch_1h_pct:
            log.warning("KILL-SWITCH: BTC %.2f%%/1h — cerrando perdedoras", btc_1h)
            self.executor.emergency_flatten_losers(self.risk)
            return
        if btc_1h <= self.s.risk.btc_crash_1h_pct:
            log.info("Shock BTC %.2f%%/1h — modo defensivo, sin escaneo", btc_1h)
            return

        # 3) sincronizar calendario económico (cada 6h) y chequear blackouts
        self.calendar.maybe_sync()
        blackout = self.sentinel.active_blackout()
        if blackout:
            log.info("BLACKOUT activo: %s — solo gestión de posiciones", blackout)
            return

        # 4) si el día está bloqueado, no gastamos API en escanear
        if self.risk.day.locked_out:
            return

        # 5) PRIMER PASE — recolectar datos y medir amplitud del mercado
        universe = self.data.refresh_universe()
        frames: dict[str, tuple] = {}
        above_vwap = 0
        for symbol in universe:
            try:
                entry_df = enrich(self.data.klines(symbol, self.s.regime.entry_tf, 300),
                                  self.s.vwap.band_k1, self.s.vwap.band_k2)
                trend_df = enrich(self.data.klines(symbol, self.s.regime.trend_tf, 300),
                                  self.s.vwap.band_k1, self.s.vwap.band_k2)
                frames[symbol] = (entry_df, trend_df)
                if float(entry_df["close"].iloc[-1]) > float(entry_df["vwap"].iloc[-1]):
                    above_vwap += 1
            except Exception as e:
                log.debug("%s: sin datos (%s)", symbol, e)

        if not frames:
            return
        breadth = above_vwap / len(frames)
        btc_sym = "BTC" + self.s.run.quote_asset
        btc_trend = frames.get(btc_sym, (None, None))[1]
        btc_up = bool(btc_trend is not None
                      and btc_trend["ema20"].iloc[-1] > btc_trend["ema50"].iloc[-1]
                      and btc_trend["close"].iloc[-1] > btc_trend["ema50"].iloc[-1])
        btc_adx = float(btc_trend["adx"].iloc[-1]) if btc_trend is not None else 0.0

        regime = self.sentinel.classify_regime(breadth, btc_up, btc_adx)
        log.info("RÉGIMEN %s — %s | governor: %s",
                 regime.regime, regime.reason, self.governor.status())
        if not regime.allow_new_entries:
            return

        # 6) SEGUNDO PASE — señales con todos los filtros de contexto
        for symbol, (entry_df, trend_df) in frames.items():
            if symbol in self.executor.positions:
                continue
            if self.sentinel.symbol_shock(entry_df):
                log.info("%s: shock propio detectado — vetado este ciclo", symbol)
                continue

            sig = scan_symbol(symbol, entry_df, trend_df, self.s)
            if sig is None:
                continue
            if sig.strategy not in regime.allowed_strategies:
                log.info("%s %s fuera de régimen %s (shadow)", symbol,
                         sig.strategy, regime.regime)
                continue
            if self.governor.is_suspended(sig.strategy):
                log.info("%s %s suspendida por governor (shadow) — señal: %s",
                         symbol, sig.strategy, sig.note)
                continue

            # 7) radar de ballenas: snapshot REST + confirmación / veto
            self.data.load_whale_snapshot(symbol)
            try:
                bids, asks, mid = self.data.orderbook(symbol)
            except Exception:
                bids = asks = None
                mid = sig.entry
            verdict = self.whale.assess_long(symbol, bids, asks, mid)
            if not verdict.ok_to_buy:
                log.info("%s %s descartada — %s", symbol, sig.strategy, verdict.reason)
                continue

            # 8) gestor de riesgo: autorización + tamaño
            decision = self.risk.authorize_entry(
                equity=equity, entry=sig.entry, stop=sig.stop, target=sig.target,
                open_positions=len(self.executor.positions),
                open_exposure_quote=self.executor.exposure_quote(),
                btc_change_1h_pct=btc_1h, signal_grade=sig.grade,
            )
            if not decision.allowed:
                log.info("%s %s no autorizada — %s", symbol, sig.strategy, decision.reason)
                continue

            log.info("SEÑAL %s %s [%s] R:R=%.2f — %s | ballenas: %s | régimen %s",
                     sig.strategy, symbol, sig.grade, sig.rr, sig.note,
                     verdict.reason, regime.regime)
            self.executor.open_long(symbol, sig.strategy, decision.quote_size,
                                    sig.entry, sig.stop, sig.target)
