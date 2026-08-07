# ROCKEFELLER 🏛️
### Bot de trading spot cuantitativo para Binance — VWAP · Chartismo · Order Flow

**v1.4.0** · Python · Binance Spot · solo largos · top-40 USDT

Rockefeller opera con la disciplina de un desk institucional: pocas operaciones,
alta selectividad, riesgo definido antes de entrar y control anti-avaricia
estructural. Diseñado a partir de *Long-Term Secrets to Short-Term Trading*
(Larry Williams), *Manual de Chartismo* (Ferran Gallofré), la síntesis de 150+
libros de inversión, y conceptos de order flow (CVD, bid-ask depth imbalance).

> ⚠️ **Software educativo, no asesoría financiera.** Operar cripto implica riesgo
> de pérdida total. Empieza en modo paper, sigue en testnet, y en live usa solo
> capital que puedas permitirte perder.

---

## ⚠️ ANTES DE NADA — Seguridad

**NUNCA subas el archivo `.env` a GitHub.** Contiene tus claves API de Binance.
El `.gitignore` de este repo ya lo excluye, pero verifícalo con `git status`
antes de cada commit.

Al crear las claves API en Binance:
- ✅ Activar solo *Enable Reading* y *Enable Spot & Margin Trading*
- ❌ **Nunca** activar retiros (*Withdrawals*)
- ✅ Restringir la clave a la IP del servidor

---

## Arquitectura

```
main.py                    → punto de entrada
config.py                  → TODOS los parámetros (riesgo, estrategias, salidas)
backtest.py                → validación histórica con fees y slippage
rockefeller/
  bot.py                   → orquestador (ciclo de decisión)
  data_engine.py           → universo top-40, velas 1h/4h, order book, snapshot REST
  indicators.py            → VWAP sesión + bandas σ, ATR, RSI, EMA, ADX, vol z-score, GSV
  patterns.py              → zonas S/R, doble suelo, velas, rupturas validadas
  strategies.py            → S1 VWAP Pullback · S2 GSV Breakout · S3 Soporte/Doble suelo
  whale_radar.py           → CVD, trades gigantes, imbalance del libro (confirmación/veto)
  event_sentinel.py        → blackouts macro, regímenes por amplitud, shock por símbolo
  calendar_sync.py         → calendario económico automático (feed ForexFactory)
  adaptive.py              → journal persistente + Strategy Governor (autoauditoría)
  risk_manager.py          → sizing, límites diarios, lockouts, cooldowns, kill-switch
  executor.py              → LIMIT entries, TP1 parcial + breakeven, trailing, time-stop
```

## Las tres estrategias

| | Lógica | Estado |
|---|---|---|
| **S1 · VWAP Pullback** | Tendencia alcista 4h + retroceso a la zona VWAP↔VWAP−1σ en 1h + RSI enfriado + martillo/envolvente | ✅ **ACTIVA** |
| **S2 · GSV Breakout** | Ruptura de máximos de 20 velas con volumen z≥2, sobre VWAP, superando `open + Greatest Swing Value` (L. Williams) | ⛔ desactivada |
| **S3 · Soporte / Doble suelo** | Zona de soporte con ≥2 toques + vela de rechazo, o doble suelo confirmado sobre neckline | ⛔ desactivada |

**Por qué solo S1:** el backtest de 120 días (ago-2026) mostró que S2 y S3, con
los parámetros actuales, eran perdedoras en este régimen (mean_R de −0.17 a
−0.42), mientras S1 daba **68.9% win-rate y +0.147R por trade**. La whitelist
está en `strategies.py → ENABLED_STRATEGIES`. *El dato mata la opinión* — para
reactivarlas hay que recalibrarlas y re-validarlas en backtest.

Toda señal pasa después **dos aduanas**:
1. **Radar de ballenas** (snapshot REST de aggTrades + order book) — veto si el
   CVD cae con grandes ventas o si hay muro vendedor. *Volumen incierto ⇒ no hay trade.*
2. **Gestor de riesgo** — el checklist completo de abajo.

## Filtros de contexto

| Módulo | Qué hace |
|---|---|
| **Calendario automático** | Descarga el feed semanal de ForexFactory cada 6h; blackout alrededor de FOMC/CPI/NFP (4h antes / 3h después para los mayores). Los eventos manuales de `events.json` se preservan al sincronizar. |
| **Régimen por amplitud** | Mide el % del universo sobre su VWAP. `RISK_OFF` (≤25%): cero entradas. `CHOPPY` (25-55%): sin rupturas. `TRENDING` (≥55% + BTC en tendencia): todo permitido. |
| **Shock por símbolo** | Caída >3×ATR en 1h en un token concreto ⇒ vetado ese ciclo (hack, delist, unlock). |
| **Strategy Governor** | Si la expectancy móvil de una estrategia (últimas 12 ops) cae bajo −0.10R, se suspende 7 días en *shadow mode*: sigue generando señales en el log, pero sin dinero. |

## Plan de gestión de riesgo

| Regla | Valor |
|---|---|
| Riesgo por operación | 1% del equity (fixed fractional: `equity × riesgo% / distancia_al_stop`) |
| Techo duro por operación | 2% (micro-cuentas: si el mínimo de Binance lo excede, **no se opera**) |
| R:R mínimo | 1:1.8 |
| Objetivo diario | +1% → modo conservador (solo A+ a media carga) |
| Techo anti-avaricia | +2% → lockout hasta el día siguiente |
| Pérdida máx. diaria | −1.5% → lockout hasta el día siguiente |
| 3 pérdidas seguidas | cooldown 4h |
| Drawdown > 5% / > 10% | tamaño × 0.5 / solo paper |
| Exposición | 1 posición a la vez (preset micro) · 65% máx por símbolo |
| Shock BTC −3% / −4.5% en 1h | sin nuevas entradas / kill-switch cierra perdedoras |

**Salidas:** stop 1.5×ATR (o estructura), TP1 a +1R vende 50% y stop a breakeven,
resto con trailing 2×ATR hasta TP2 (+2.2R). Time-stop 72h.

## Despliegue

```bash
pip install -r requirements.txt
cp .env.example .env        # añade tus claves (sin retiros, IP restringida)

# 1) Backtest — si el profit factor < 1.3 tras costes, NO desplegar
python3 backtest.py BTCUSDT ETHUSDT SOLUSDT BNBUSDT --days 120

# 2) Paper trading (por defecto): mode="paper" en config.py
python3 main.py

# 3) Testnet: mode="testnet" (claves de https://testnet.binance.vision)

# 4) Live: mode="live" + capital en starting_capital_usdt
```

### Producción con systemd (recomendado)

```ini
# /etc/systemd/system/rockefeller.service
[Unit]
Description=Rockefeller trading bot
After=network-online.target

[Service]
WorkingDirectory=/root/rockefeller
ExecStart=/usr/bin/python3 main.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now rockefeller
systemctl status rockefeller        # ¿está vivo?
tail -50 rockefeller.log            # qué está viendo
cat trades.csv                      # historial de operaciones
```

## Resultados del backtest (S1, 120 días, ago-2026)

| Métrica | Valor |
|---|---|
| Operaciones | 45 (≈0.38/día) |
| Win-rate | 68.9% |
| Profit factor | 1.40 |
| Expectancy | +0.147 R por trade |
| Retorno | +6.59% (riesgo 1%/trade) |
| Max drawdown | 5.55% |

## Expectativas honestas

El objetivo del 1% diario funciona como **techo con lockout**, no como promesa:
un 1% diario compuesto son ~3.700% anuales, y ningún fondo del mundo sostiene
eso. Un estudio Monte Carlo de 15.000 trayectorias dio 0.00% de probabilidad de
sostenerlo un solo mes. Lo alcanzable es **+0.5% a +4% mensual según el
régimen**, con meses rojos incluidos y drawdowns del 7-20%.

**Habrá días —a veces varios seguidos— con cero operaciones.** Eso es la
selectividad funcionando, no un fallo. Y 45 operaciones de backtest es evidencia
prometedora, no prueba definitiva: el veredicto real lo dan las primeras 30-40
operaciones en vivo, y si el win-rate converge hacia el del backtest.

## Operación en vivo — requisitos que NO son opcionales

1. **Mantén saldo de BNB en Spot (3-5 USDT) y activa "Pagar comisiones con BNB".**
   Sin BNB, Binance cobra la comisión *en el activo comprado*: compras 60.7 ADA
   y recibes 60.64 — al intentar vender los 60.7 el exchange responde
   `-2010 insufficient balance` y la venta falla.
2. **Desactiva la suscripción automática de Binance Earn (Simple Earn → Auto-Subscribe).**
   Si Earn absorbe el token recién comprado, sale de Spot y **el stop-loss no
   puede ejecutarse**: la posición queda sin protección real.
3. **Revisa el log buscando `RECONCILIACIÓN`** en tu rutina semanal. El bot avisa
   ahí de cualquier cripto huérfana o de fondos fuera de Spot.

```bash
grep -iE "RECONCILIACIÓN|VENTA FALLIDA|ABORTADO" rockefeller.log
```

## Changelog

- **1.4.0** — **Integridad de ejecución.** Redondeos con `Decimal` (adiós `-1111
  too much precision`); la cantidad a vender se recorta al saldo *realmente libre*
  (adiós `-2010 insufficient balance`); **una venta fallida ya nunca cierra la
  posición** ni registra un trade fantasma — se reintenta y la posición sigue
  vigilada; ventas parciales dejan el remanente vivo con su stop; la qty registrada
  al abrir es la *realmente ejecutada*; reconciliación con el exchange al arrancar
  y cada hora (detecta cripto huérfana y fondos secuestrados por Earn).
- **1.3.2** — PnL total del trade (TP1 + tramo final) en `trades.csv` y en el lockout diario
- **1.3.1** — Fix: múltiplo R corrupto cuando el trailing cruza la entrada (`initial_stop` fijo)
- **1.3.0** — Radar de ballenas por REST bajo demanda; eliminados los websockets (fugas de memoria → OOM kills)
- **1.2.x** — Event Sentinel, calendario automático, Governor, persistencia de posiciones
- **1.1** — Temporalidad 1h (fricción de 0.125R → 0.06R), R:R mínimo 1.8
- **1.0** — Release inicial: 3 estrategias, VWAP, radar de ballenas, gestión de riesgo
