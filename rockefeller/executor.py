"""
Ejecución y gestión de posiciones.

  · Entrada: orden LIMIT al precio de señal (paciencia > perseguir precio;
    reduce fees y slippage — "use limit orders instead of market orders").
  · Salida: TP1 a +1R vende 50% y sube el stop a breakeven ("scaling out");
    el resto corre con trailing ATR hasta TP2 o stop ("let winners run").
  · Time-stop: si en max_holding_hours no funcionó, se cierra (el capital
    debe estar en las mejores ideas, no atrapado).
  · Modo paper: simula fills con el precio de mercado, misma lógica.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field

from binance.client import Client
from binance.exceptions import BinanceAPIException

from config import Settings

log = logging.getLogger("rockefeller.exec")


@dataclass
class Position:
    symbol: str
    strategy: str
    qty: float
    entry: float
    stop: float
    target: float
    opened_ts: float = field(default_factory=time.time)
    tp1_done: bool = False
    highest: float = 0.0
    client_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    initial_stop: float = 0.0    # fijo al abrir — NUNCA se toca (a diferencia de .stop)
    qty_opened: float = 0.0      # qty TOTAL al abrir — fijo, para reportar el trade completo
    realized_quote: float = 0.0  # PnL ya embolsado en ventas parciales (TP1)
    close_attempts: int = 0      # intentos de cierre fallidos (v1.4)

    def __post_init__(self):
        if not self.initial_stop:
            self.initial_stop = self.stop
        if not self.qty_opened:
            self.qty_opened = self.qty

    @property
    def risk_per_unit(self) -> float:
        """Distancia de riesgo ACTUAL (stop puede haber subido por TP1/trailing).
        Solo sirve para decisiones de salida en curso, NUNCA para medir R."""
        return self.entry - self.stop

    @property
    def initial_risk_per_unit(self) -> float:
        """Distancia de riesgo ORIGINAL, fija — la base correcta para el
        múltiplo R y para el proxy de ATR del trailing."""
        return self.entry - self.initial_stop


class Executor:
    STATE_PATH = "positions.json"

    def __init__(self, client: Client, data, settings: Settings):
        self.client = client
        self.data = data
        self.s = settings
        self.paper = settings.run.mode == "paper"
        self.positions: dict[str, Position] = {}
        self.capital_cap = settings.run.starting_capital_usdt
        self.paper_equity = self.capital_cap    # equity simulada en modo paper
        self.closed_trades: list[dict] = []
        self._restore_state()

    # ─────────────── persistencia (fin de la amnesia) ───────────────
    def _save_state(self) -> None:
        try:
            state = {"paper_equity": self.paper_equity,
                     "positions": {s: vars(p) for s, p in self.positions.items()}}
            with open(self.STATE_PATH, "w") as f:
                json.dump(state, f, indent=2)
        except OSError as e:
            log.error("No se pudo persistir el estado: %s", e)

    def _restore_state(self) -> None:
        if not os.path.exists(self.STATE_PATH):
            return
        try:
            with open(self.STATE_PATH) as f:
                state = json.load(f)
            self.paper_equity = state.get("paper_equity", self.paper_equity)
            for sym, d in state.get("positions", {}).items():
                self.positions[sym] = Position(**d)
            if self.positions:
                log.info("Estado restaurado: %d posición(es) abiertas recuperadas "
                         "tras reinicio: %s", len(self.positions),
                         ", ".join(self.positions))
        except (OSError, json.JSONDecodeError, TypeError) as e:
            log.error("positions.json corrupto (%s) — revisa tu Spot manualmente", e)

    # ─────────────── reconciliación con el exchange (v1.4) ───────────────
    def reconcile(self) -> None:
        """Compara lo que el bot cree tener contra lo que el exchange dice.

        Detecta los dos fallos silenciosos que nos costaron caro:
          · cripto huérfana (el bot cerró la posición pero la venta falló),
          · fondos absorbidos por Binance Earn (suscripción automática) o
            bloqueados en otra orden, que hacen imposible vender.
        Solo INFORMA — nunca vende por su cuenta: esa decisión es del humano.
        """
        if self.paper:
            return
        try:
            prices = {t["symbol"]: float(t["price"])
                      for t in self.client.get_symbol_ticker()}
            balances = self.client.get_account()["balances"]
        except Exception as e:
            log.warning("Reconciliación omitida (%s)", e)
            return

        quote = self.s.run.quote_asset
        tracked = {self.data.base_asset(s) for s in self.positions}
        # BNB de reserva para comisiones: no es una posición huérfana. Solo se
        # ignora si es pequeño y no hay una posición abierta en BNB.
        fee_reserve = self.s.run.fee_asset_reserve_usdt
        for b in balances:
            asset, free, locked = b["asset"], float(b["free"]), float(b["locked"])
            total = free + locked
            if total <= 0 or asset == quote:
                continue
            value = total * prices.get(asset + quote, 0.0)
            if value < 1.0:            # polvo irrelevante
                continue
            if asset == "BNB" and asset not in tracked and value <= fee_reserve:
                log.info("Reserva de comisiones: %.8f BNB (~%.2f %s) — OK",
                         total, value, quote)
                continue
            # reserva de BNB para comisiones: no es una posición huérfana
            if (asset == "BNB" and asset not in tracked
                    and value <= self.s.run.bnb_fee_reserve_usdt):
                continue
            if asset in tracked:
                pos = self.positions[asset + quote]
                if free + 1e-12 < pos.qty * 0.99:
                    log.critical("RECONCILIACIÓN %s: el bot cree tener %.8f pero solo "
                                 "hay %.8f libres (%.8f bloqueados). ¿Suscripción "
                                 "automática de Binance Earn activa? Con los fondos "
                                 "fuera de Spot el stop NO puede ejecutarse.",
                                 asset, pos.qty, free, locked)
            else:
                log.critical("RECONCILIACIÓN: %.8f %s (~%.2f %s) en la cuenta SIN "
                             "posición asociada. Si el bot lo compró, quedó huérfano "
                             "y NO tiene stop. Revísalo y véndelo manualmente si no "
                             "es tuyo de antes.", total, asset, value, quote)

    # ─────────────────── equity y exposición ───────────────────
    def equity(self) -> float:
        if self.paper:
            unreal = sum((self._mark(p.symbol) - p.entry) * p.qty
                         for p in self.positions.values())
            return self.paper_equity + unreal
        acct = self.client.get_account()
        total = 0.0
        prices = {t["symbol"]: float(t["price"])
                  for t in self.client.get_symbol_ticker()}
        for b in acct["balances"]:
            amt = float(b["free"]) + float(b["locked"])
            if amt <= 0:
                continue
            asset = b["asset"]
            if asset == self.s.run.quote_asset:
                total += amt
            else:
                total += amt * prices.get(asset + self.s.run.quote_asset, 0.0)
        # el bot nunca dimensiona posiciones sobre más que el tope configurado,
        # aunque tu wallet Spot tenga más USDT que eso.
        return min(total, self.capital_cap)

    def exposure_quote(self) -> float:
        return sum(p.qty * p.entry for p in self.positions.values())

    def _mark(self, symbol: str) -> float:
        return float(self.client.get_symbol_ticker(symbol=symbol)["price"])

    # ─────────────────── apertura ───────────────────
    def open_long(self, symbol: str, strategy: str, quote_size: float,
                  entry: float, stop: float, target: float) -> Position | None:
        if symbol in self.positions:
            return None
        qty = self.data.round_qty(symbol, quote_size / entry)
        if qty * entry < self.data.min_notional(symbol):
            log.warning("%s: notional %.2f < mínimo del exchange", symbol, qty * entry)
            return None

        if self.paper:
            fill = self._mark(symbol)
        else:
            try:
                order = self.client.order_limit_buy(
                    symbol=symbol,
                    quantity=self.data.qty_to_str(symbol, qty),
                    price=self.data.price_to_str(symbol, entry),
                )
                # espera breve de fill; si no llena, cancela (no perseguimos)
                fill = self._await_fill(symbol, order["orderId"], timeout=90)
                if fill is None:
                    return None
                # v1.4: la cantidad que registramos es la REALMENTE ejecutada,
                # descontando la comisión si Binance la cobró en el activo base
                # (pasa cuando no hay BNB para pagar fees).
                try:
                    o = self.client.get_order(symbol=symbol, orderId=order["orderId"])
                    executed = float(o.get("executedQty", qty))
                    if executed > 0:
                        qty = self.data.round_qty(symbol, executed)
                except Exception:
                    pass
            except BinanceAPIException as e:
                log.error("%s: error al abrir — %s", symbol, e)
                return None

        pos = Position(symbol, strategy, qty, fill, stop, target, highest=fill)
        self.positions[symbol] = pos
        self._save_state()
        log.info("ABIERTA %s %s qty=%.6g @%.6g stop=%.6g tp=%.6g",
                 strategy, symbol, qty, fill, stop, target)
        return pos

    def _await_fill(self, symbol: str, order_id: int, timeout: int) -> float | None:
        t0 = time.time()
        while time.time() - t0 < timeout:
            o = self.client.get_order(symbol=symbol, orderId=order_id)
            if o["status"] == "FILLED":
                return float(o["price"]) if float(o["price"]) > 0 else float(o["cummulativeQuoteQty"]) / float(o["executedQty"])
            if o["status"] in ("CANCELED", "REJECTED", "EXPIRED"):
                return None
            time.sleep(3)
        try:
            self.client.cancel_order(symbol=symbol, orderId=order_id)
        except BinanceAPIException:
            pass
        log.info("%s: limit no llenada en %ds — señal descartada (disciplina)", symbol, timeout)
        return None

    # ─────────────────── gestión de salidas ───────────────────
    def manage(self, risk_manager) -> None:
        """Llamar en cada ciclo: stops, TP1 parcial, trailing, time-stop."""
        ex = self.s.exit
        for symbol in list(self.positions):
            p = self.positions[symbol]
            price = self._mark(symbol)
            p.highest = max(p.highest, price)

            # 1) STOP
            if price <= p.stop:
                self._close(p, price, "STOP", risk_manager)
                continue

            # 2) TP1: +1R → vende fracción, stop a breakeven
            if not p.tp1_done and price >= p.entry + ex.tp1_r_multiple * p.initial_risk_per_unit:
                sell_qty = self.data.round_qty(symbol, p.qty * ex.tp1_fraction)
                sold = self._sell(symbol, sell_qty, price) if sell_qty > 0 else 0.0
                if sold <= 0:
                    # v1.4 — si la venta no se ejecutó, NO se marca TP1 ni se
                    # reduce la cantidad: se reintentará el próximo ciclo.
                    log.warning("%s: TP1 no ejecutado (venta fallida). Se reintenta.",
                                symbol)
                    continue
                realized = (price - p.entry) * sold
                self._book(p, realized, partial=True)
                p.realized_quote += realized   # se suma al PnL total del trade
                p.qty -= sold
                p.tp1_done = True
                p.stop = p.entry * (1 + ex.breakeven_buffer_pct / 100)
                log.info("%s: TP1 (+1R, +%.4f USDT). Stop a breakeven, resto corre.",
                         symbol, realized)
                continue

            # 3) trailing tras TP1
            if p.tp1_done:
                atr_proxy = p.initial_risk_per_unit / ex.stop_atr_mult
                new_stop = p.highest - ex.trail_atr_mult * atr_proxy
                if new_stop > p.stop:
                    p.stop = new_stop

            # 4) TP2 final
            if price >= p.target:
                self._close(p, price, "TP2", risk_manager)
                continue

            # 5) time-stop
            if time.time() - p.opened_ts > ex.max_holding_hours * 3600:
                self._close(p, price, "TIME_STOP", risk_manager)
        if self.positions:
            self._save_state()   # persistir cambios de stop/qty (TP1, trailing)

    def _sell(self, symbol: str, qty: float, ref_price: float) -> float:
        """Vende y devuelve la cantidad REALMENTE vendida (0.0 si falló).

        v1.4 — antes esto devolvía None y el error se tragaba en silencio:
        la posición se marcaba como cerrada aunque la cripto siguiera en la
        cuenta. Ahora:
          · la cantidad se recorta al saldo REALMENTE libre (evita -2010),
          · se envía como string exacto vía Decimal (evita -1111),
          · si falla, se devuelve 0.0 y el llamador NO cierra la posición.
        """
        if self.paper:
            return qty

        base = self.data.base_asset(symbol)
        for intento in (1, 2, 3):
            free = self.data.free_balance(base)
            sell_qty = self.data.round_qty(symbol, min(qty, free))
            if sell_qty <= 0:
                log.error("%s: saldo libre insuficiente (%.8f %s disponible, "
                          "se querían vender %.8f). ¿Fondos en Earn o en otra "
                          "orden? La posición NO se cierra.", symbol, free, base, qty)
                return 0.0
            try:
                self.client.order_market_sell(
                    symbol=symbol, quantity=self.data.qty_to_str(symbol, sell_qty))
                if sell_qty < qty * 0.999:
                    log.warning("%s: vendidas %.8f de %.8f solicitadas (saldo real)",
                                symbol, sell_qty, qty)
                return sell_qty
            except BinanceAPIException as e:
                log.error("%s: error al vender (intento %d/3) — %s", symbol, intento, e)
                time.sleep(2)
        log.critical("%s: VENTA FALLIDA tras 3 intentos. La posición sigue ABIERTA "
                     "y se reintentará en el próximo ciclo. Revisa la cuenta.", symbol)
        return 0.0

    def _book(self, p: Position, realized_quote: float, partial: bool) -> None:
        if self.paper:
            self.paper_equity += realized_quote

    def _close(self, p: Position, price: float, reason: str, risk_manager) -> None:
        qty = self.data.round_qty(p.symbol, p.qty)
        sold = self._sell(p.symbol, qty, price) if qty > 0 else 0.0

        # v1.4 — LA REGLA DE ORO: si el exchange no confirmó la venta, la
        # posición NO se cierra. Nada de contabilidad ficticia.
        if qty > 0 and sold <= 0:
            p.close_attempts = getattr(p, "close_attempts", 0) + 1
            log.critical("%s: cierre [%s] ABORTADO — la venta no se ejecutó "
                         "(intento %d). Posición sigue viva y vigilada.",
                         p.symbol, reason, p.close_attempts)
            self._save_state()
            return

        if 0 < sold < qty * 0.999:      # venta parcial: queda remanente vivo
            realized_partial = (price - p.entry) * sold
            self._book(p, realized_partial, partial=True)
            p.realized_quote += realized_partial
            p.qty -= sold
            log.warning("%s: venta PARCIAL (%.8f de %.8f). Queda posición abierta.",
                        p.symbol, sold, qty)
            self._save_state()
            return

        final_leg = (price - p.entry) * p.qty
        self._book(p, final_leg, partial=False)
        total_realized = p.realized_quote + final_leg   # TP1 parcial + tramo final
        eq = max(self.equity(), 1e-9)
        pnl_pct = total_realized / eq * 100
        risk_manager.register_close(pnl_pct)
        risk_per_unit = max(p.initial_risk_per_unit, 1e-9)
        total_risk_quote = risk_per_unit * p.qty_opened
        trade = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": p.symbol, "strategy": p.strategy, "reason": reason,
            "entry": p.entry, "exit": price, "pnl_quote": total_realized,
            "pnl_r": total_realized / max(total_risk_quote, 1e-9),
            "pnl_pct_equity": pnl_pct, "held_h": (time.time() - p.opened_ts) / 3600,
        }
        self.closed_trades.append(trade)
        if getattr(self, "on_trade_closed", None):
            self.on_trade_closed(trade)     # journal + governor (bot los conecta)
        del self.positions[p.symbol]
        self._save_state()
        log.info("CERRADA %s [%s] %+.4f USDT total (%+.3f%% equity) — %s",
                 p.symbol, reason, total_realized, pnl_pct, risk_manager.summary())

    def emergency_flatten_losers(self, risk_manager) -> None:
        """Kill-switch: cierra posiciones en pérdida ante shock de mercado."""
        for symbol in list(self.positions):
            p = self.positions[symbol]
            price = self._mark(symbol)
            if price < p.entry:
                self._close(p, price, "KILL_SWITCH", risk_manager)
