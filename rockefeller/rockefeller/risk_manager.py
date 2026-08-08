"""
Gestor de Riesgo — la pieza que separa a un quant de un apostador.

Principios codificados:
  · Larry Williams: "lo que te mata es la pérdida grande".
    tamaño = (equity × riesgo%) / distancia_al_stop  (fixed fractional,
    donde la 'pérdida máxima' de su fórmula ES el stop definido a priori).
  · Ley de supervivencia: 1º preservar capital, 2º consistencia, 3º
    retornos excepcionales.
  · Anti-avaricia estructural: objetivo diario 1% → modo conservador;
    techo duro 2% → lockout. El bot no puede "querer más".
  · Recuperación metódica: 3 pérdidas seguidas → cooldown; DD>5% →
    mitad de tamaño; DD>10% → solo paper ("stop digging").
  · Edge neto: la señal debe pagar ≥3× el coste round-trip (fees+slippage).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config import RiskConfig


@dataclass
class DayState:
    date_key: str = ""
    realized_pnl_pct: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    consecutive_losses: int = 0
    locked_out: bool = False
    conservative: bool = False
    cooldown_until: float = 0.0


@dataclass
class RiskDecision:
    allowed: bool
    reason: str
    quote_size: float = 0.0     # notional USDT a comprar
    size_multiplier: float = 1.0


class RiskManager:
    def __init__(self, cfg: RiskConfig, initial_equity: float):
        self.cfg = cfg
        self.initial_equity = initial_equity
        self.peak_equity = initial_equity
        self.day = DayState(self._today())

    # ────────────────── utilidades de estado ──────────────────
    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _roll_day(self) -> None:
        today = self._today()
        if self.day.date_key != today:
            self.day = DayState(today)

    def update_equity_peak(self, equity: float) -> None:
        self.peak_equity = max(self.peak_equity, equity)

    def drawdown_pct(self, equity: float) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return (self.peak_equity - equity) / self.peak_equity * 100.0

    # ────────────────── registro de resultados ──────────────────
    def register_close(self, pnl_pct_of_equity: float) -> None:
        self._roll_day()
        d = self.day
        d.realized_pnl_pct += pnl_pct_of_equity
        d.trades += 1
        if pnl_pct_of_equity > 0:
            d.wins += 1
            d.consecutive_losses = 0
        else:
            d.losses += 1
            d.consecutive_losses += 1
            if d.consecutive_losses >= self.cfg.consecutive_loss_limit:
                d.cooldown_until = time.time() + self.cfg.cooldown_hours_after_streak * 3600
        # objetivo / límites diarios
        if d.realized_pnl_pct >= self.cfg.daily_hard_cap_pct:
            d.locked_out = True
        elif d.realized_pnl_pct >= self.cfg.daily_target_pct:
            d.conservative = True
        if d.realized_pnl_pct <= -self.cfg.daily_max_loss_pct:
            d.locked_out = True

    # ────────────────── autorización de entradas ──────────────────
    def authorize_entry(self, *, equity: float, entry: float, stop: float,
                        target: float, open_positions: int,
                        open_exposure_quote: float, btc_change_1h_pct: float,
                        signal_grade: str = "A") -> RiskDecision:
        """
        Checklist de entrada (síntesis de 150+ libros: entrar solo si TODOS
        los criterios se cumplen). Devuelve el notional autorizado.
        """
        self._roll_day()
        d = self.day
        now = time.time()

        # 1) Estado del día
        if d.locked_out:
            if d.realized_pnl_pct > 0:
                return RiskDecision(False, "lockout anti-avaricia: techo diario alcanzado")
            return RiskDecision(False, "lockout: pérdida máxima diaria alcanzada")
        if now < d.cooldown_until:
            mins = int((d.cooldown_until - now) / 60)
            return RiskDecision(False, f"cooldown por racha de pérdidas ({mins} min restantes)")

        # 2) Shock de mercado (proxy de noticias: BTC arrastra a todo el top-40)
        if btc_change_1h_pct <= self.cfg.btc_crash_1h_pct:
            return RiskDecision(False, f"shock BTC {btc_change_1h_pct:.1f}%/1h: sin nuevas entradas")

        # 3) Exposición
        if open_positions >= self.cfg.max_open_positions:
            return RiskDecision(False, "máximo de posiciones abiertas")
        if open_exposure_quote >= equity * self.cfg.max_portfolio_exposure_pct / 100:
            return RiskDecision(False, "exposición máxima de cartera alcanzada")

        # 4) Geometría de la operación
        if not (stop < entry < target):
            return RiskDecision(False, "geometría inválida (stop/entry/target)")
        stop_dist_pct = (entry - stop) / entry * 100.0
        reward_pct = (target - entry) / entry * 100.0
        rr = reward_pct / stop_dist_pct if stop_dist_pct > 0 else 0.0
        if rr < self.cfg.min_risk_reward:
            return RiskDecision(False, f"R:R {rr:.2f} < mínimo {self.cfg.min_risk_reward}")

        # 5) Edge neto sobre costes (fees + slippage)
        if reward_pct < self.cfg.roundtrip_cost_pct * self.cfg.cost_edge_multiple:
            return RiskDecision(False, "edge esperado no cubre 3× costes de transacción")

        # 6) Tamaño — fixed fractional (Williams: equity·riesgo% / pérdida máxima)
        mult = 1.0
        dd = self.drawdown_pct(equity)
        if dd >= self.cfg.drawdown_paper_mode_pct:
            return RiskDecision(False, f"drawdown {dd:.1f}% ⇒ modo paper obligatorio")
        if dd >= self.cfg.drawdown_half_size_pct:
            mult *= 0.5
        if d.conservative:
            mult *= 0.5          # tras lograr el 1%: solo setups A+ a media carga
            if signal_grade != "A+":
                return RiskDecision(False, "modo conservador: solo setups A+ tras objetivo diario")

        risk_quote = equity * (self.cfg.risk_per_trade_pct / 100.0) * mult
        notional = risk_quote / (stop_dist_pct / 100.0)

        # topes por símbolo y por cartera
        notional = min(
            notional,
            equity * self.cfg.max_position_notional_pct / 100.0,
            equity * self.cfg.max_portfolio_exposure_pct / 100.0 - open_exposure_quote,
        )
        floor = getattr(self.cfg, "micro_min_notional_usdt", 10.0)
        if notional < floor:
            # cuenta micro: subir al mínimo del exchange solo si el riesgo
            # implícito respeta el techo duro; si no, disciplina: no trade.
            implied_risk_pct = floor * (stop_dist_pct / 100.0) / equity * 100.0
            ceiling = getattr(self.cfg, "micro_risk_ceiling_pct", 2.0) * mult
            if implied_risk_pct <= ceiling and floor <= equity * self.cfg.max_position_notional_pct / 100.0:
                notional = floor
                return RiskDecision(True,
                                    f"OK micro-bump (R:R {rr:.2f}, riesgo {implied_risk_pct:.2f}%)",
                                    quote_size=round(notional, 2), size_multiplier=mult)
            return RiskDecision(False,
                                f"notional mínimo implicaría riesgo {implied_risk_pct:.2f}% > techo {ceiling:.1f}%")

        return RiskDecision(True, f"OK (R:R {rr:.2f}, riesgo {self.cfg.risk_per_trade_pct * mult:.2f}%)",
                            quote_size=round(notional, 2), size_multiplier=mult)

    # ────────────────── estado imprimible ──────────────────
    def summary(self) -> str:
        d = self.day
        return (f"[{d.date_key}] PnL {d.realized_pnl_pct:+.2f}% | "
                f"trades {d.trades} (W{d.wins}/L{d.losses}) | "
                f"{'LOCKED' if d.locked_out else 'conservador' if d.conservative else 'activo'}")
