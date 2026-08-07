"""
ROCKEFELLER — Configuración central.
Todos los parámetros de riesgo, estrategia y ejecución viven aquí.
Filosofía (Larry Williams / síntesis de 150+ libros):
  1) Preservar capital.  2) Retornos consistentes.  3) Retornos excepcionales.
  Nunca invertir ese orden.
"""
from dataclasses import dataclass, field


# ──────────────────────────────────────────────────────────────
#  MODO DE OPERACIÓN
# ──────────────────────────────────────────────────────────────
@dataclass
class RunConfig:
    mode: str = "paper"          # "paper" | "testnet" | "live"
    quote_asset: str = "USDT"
    loop_interval_sec: int = 60       # ciclo de decisión (1h no exige más)
    universe_refresh_min: int = 60    # refresco del top-40
    log_level: str = "INFO"
    # ── CAPITAL QUE EL BOT PUEDE USAR ──
    # Paper: saldo simulado inicial. Live/testnet: TOPE de seguridad —
    # aunque tengas más USDT en tu wallet Spot, el bot nunca calculará
    # tamaños de posición sobre más que este número. Para cambiarlo:
    # edita este valor y reinicia el bot (Ctrl+C y volver a lanzar).
    starting_capital_usdt: float = 20.0


# ──────────────────────────────────────────────────────────────
#  UNIVERSO — Top 40 de Binance Spot
# ──────────────────────────────────────────────────────────────
@dataclass
class UniverseConfig:
    top_n: int = 40
    min_quote_volume_24h: float = 20_000_000.0   # liquidez mínima (USDT)
    exclude_bases: tuple = (
        # stablecoins y wrapped: no tienen edge direccional
        "USDC", "FDUSD", "TUSD", "DAI", "BUSD", "EUR", "USDP", "PYUSD",
        "WBTC", "WBETH", "USD1",
    )
    exclude_suffixes: tuple = ("UP", "DOWN", "BULL", "BEAR")  # tokens apalancados


# ──────────────────────────────────────────────────────────────
#  GESTIÓN DE RIESGO — el corazón del bot
#  (Fixed Fractional + fórmula de Larry Williams adaptada:
#   tamaño = equity * riesgo% / distancia_al_stop)
# ──────────────────────────────────────────────────────────────
@dataclass
class RiskConfig:
    # ── PRESET MICRO-CAPITAL (cuentas < $100) ──
    # Binance exige ~$10 mínimo por orden. Con $20, los topes normales de
    # diversificación (15%/símbolo, 40%/cartera) quedan por debajo de ese
    # mínimo y el bot nunca operaría. Este preset abre UNA posición a la
    # vez de ~$10-13. Cuando el capital supere ~$500, vuelve al preset
    # profesional comentado a la derecha de cada línea.
    risk_per_trade_pct: float = 1.0            # profesional: 0.75
    max_open_positions: int = 1                # profesional: 4
    max_portfolio_exposure_pct: float = 65.0   # profesional: 40.0
    max_position_notional_pct: float = 65.0    # profesional: 15.0
    # Con capital micro y stops amplios de 1h, el tamaño ideal puede caer
    # bajo el mínimo de ~$10 de Binance. Se permite subir al mínimo SOLO si
    # el riesgo implícito no supera este techo duro. Si lo supera → no trade.
    micro_min_notional_usdt: float = 10.5
    micro_risk_ceiling_pct: float = 2.0
    # Objetivo diario. Al alcanzarlo el bot entra en modo conservador.
    daily_target_pct: float = 1.0
    # Techo duro anti-avaricia: alcanzado esto, no se abren más entradas hoy.
    daily_hard_cap_pct: float = 2.0
    # Pérdida máxima diaria: se apaga hasta el próximo día UTC.
    daily_max_loss_pct: float = 1.5
    # R:R mínimo exigido a cada señal (1:1.5 con win-rate alto es sostenible;
    # se prefieren señales 1:2+).
    # R:R mínimo exigido. v1.1: subido a 1.8 — con la nueva estructura de
    # salidas solo pasan señales cuyo objetivo paga de sobra la fricción.
    min_risk_reward: float = 1.8
    max_correlated_alts: int = 3               # alts abiertas simultáneas (corr≈1 con BTC)
    # Racha de pérdidas → enfriamiento (recuperación metódica, no revenge trading)
    consecutive_loss_limit: int = 3
    cooldown_hours_after_streak: float = 4.0
    # Throttle por drawdown (reduce tamaño, "stop digging")
    drawdown_half_size_pct: float = 5.0    # DD>5%  → tamaño x0.5
    drawdown_paper_mode_pct: float = 10.0  # DD>10% → solo paper
    # Coste round-trip: 0.15% asume descuento BNB (mantén algo de BNB en
    # Spot y activa "Pagar fees con BNB" en Binance — es la palanca de
    # rentabilidad más barata que existe). Sin BNB, sube esto a 0.20.
    roundtrip_cost_pct: float = 0.15
    cost_edge_multiple: float = 3.0
    # Kill-switch de mercado: shock en BTC ⇒ no nuevas entradas
    btc_crash_1h_pct: float = -3.0
    btc_kill_switch_1h_pct: float = -4.5   # además cierra posiciones perdedoras


# ──────────────────────────────────────────────────────────────
#  ESTRATEGIAS
# ──────────────────────────────────────────────────────────────
@dataclass
class VWAPConfig:
    """VWAP de sesión (ancla diaria UTC) + bandas de desviación estándar."""
    band_k1: float = 1.0
    band_k2: float = 2.0
    # S1 — Reversión al VWAP (estrategia principal, alta tasa de acierto)
    s1_rsi_low: float = 32.0
    s1_rsi_high: float = 52.0
    s1_max_dist_to_vwap_atr: float = 0.35   # entrada solo pegada al VWAP/banda-1
    # S2 — Ruptura con volumen (adaptación GSV de Larry Williams)
    s2_vol_zscore_min: float = 2.0
    s2_breakout_lookback: int = 20
    s2_gsv_lookback: int = 4                # LW: 1–4 días producen el mejor valor
    s2_gsv_mult: float = 1.0
    # S3 — Rebote en soporte / doble suelo (chartismo)
    s3_zone_tolerance_pct: float = 0.4      # S/R son zonas, no niveles exactos
    s3_min_touches: int = 2                 # 2 toques mínimo, 3 ideal


@dataclass
class RegimeConfig:
    """Filtro multi-timeframe: 4h para tendencia, 1h para entrada.
    v1.1 QUANT EDGE: se abandonó 15m. En velas de 1h los stops
    estructurales son ~2-3x más amplios, así que las comisiones pasan de
    ~0.125R a ~0.05-0.06R por operación — desplaza la frontera de
    rentabilidad ~4 puntos de win-rate a nuestro favor. Menos ruido,
    menos operaciones, mejor calidad: el intercambio correcto."""
    trend_tf: str = "4h"
    entry_tf: str = "1h"
    ema_fast: int = 20
    ema_slow: int = 50
    ema_long: int = 200
    adx_min_trend: float = 18.0   # ADX bajo ⇒ lateral ⇒ solo S1/S3, nunca S2


@dataclass
class WhaleConfig:
    """Radar de ballenas: aggTrades + order book (conceptos CVD /
    bid-ask depth imbalance de OpenMarket-Kiyotaka)."""
    window_sec: int = 300
    big_trade_usd: float = 150_000.0      # trade individual "ballena"
    big_trade_zscore: float = 3.0
    cvd_confirm_slope: float = 0.0        # CVD ascendente confirma longs
    ob_depth_pct: float = 0.5             # profundidad ±0.5% del mid
    ob_imbalance_veto: float = 0.75       # asks/bids > 1/0.75 ⇒ presión vendedora, veto


@dataclass
class ExitConfig:
    stop_atr_mult: float = 1.5
    tp1_r_multiple: float = 1.0    # TP1 = +1R → vende 50% y stop a breakeven
    tp1_fraction: float = 0.5
    tp2_r_multiple: float = 2.2    # v1.1: más recorrido al runner en 1h
    trail_atr_mult: float = 2.0    # trailing tras TP1 (deja correr al ganador)
    max_holding_hours: float = 72.0  # v1.1: swings de 1h necesitan 2-3 días
    breakeven_buffer_pct: float = 0.05


@dataclass
class Settings:
    run: RunConfig = field(default_factory=RunConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    vwap: VWAPConfig = field(default_factory=VWAPConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    whale: WhaleConfig = field(default_factory=WhaleConfig)
    exit: ExitConfig = field(default_factory=ExitConfig)


SETTINGS = Settings()
