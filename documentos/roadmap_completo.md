# Bot Cripto Polymarket — Roadmap Completo
> Documento maestro de referencia. Actualizar con cada avance.
> Ultima actualizacion: 31 de marzo de 2026

---

## CONTEXTO DEL PROYECTO

### Que estamos construyendo
Un bot automatizado que opera en los mercados **BTC Up/Down de 5 minutos** de Polymarket.
Cada 5 minutos se abre un mercado nuevo: "Bitcoin sube o baja en los proximos 5 minutos?"
El bot predice la direccion usando ML y ejecuta ordenes asimetricas (estrategia hibrida).

### Decisiones tomadas
- **Estrategia**: Hibrida (Tipo C) — ordenes asimetricas segun prediccion
- **Mercado objetivo**: BTC 5 minutos (12 ciclos/hora, mas datos y oportunidades)
- **Modelo ML**: XGBoost/LightGBM como baseline
- **Lenguaje**: Python (py_clob_client SDK)
- **Infraestructura**: VPS Oracle (4 OCPU, 32GB RAM) + Dokploy + Docker
- **Base de datos**: PostgreSQL (produccion) / SQLite (desarrollo local)
- **Deploy**: docker-compose desde GitHub via Dokploy

### Datos clave de Polymarket BTC 5-min
- Slug: `btc-updown-5m-<unix_timestamp>` (timestamp multiplo de 300)
- Outcomes: "Up" (YES) / "Down" (NO)
- Resolucion: oracle Chainlink BTC/USD
- Fee maximo: ~1.56% en precio 0.50 (se reduce en extremos)
- Para ser rentable: necesitamos win rate > 53%
- La resolucion llega ~2 min despues del cierre via REST (no confiable por WS)

### Estructura del proyecto
```
bot_cripto/
├── main.py                 ← Orquestador principal
├── Dockerfile              ← Imagen Docker del bot
├── docker-compose.yml      ← Bot + PostgreSQL
├── requirements.txt
├── .env.example
├── .gitignore
│
├── data/                   ← FASE 1: Pipeline de datos
│   ├── storage.py          ← Dual PostgreSQL/SQLite
│   ├── rest_client.py      ← Market discovery + REST endpoints
│   └── websocket_client.py ← RTDS + Market Channel WebSockets
│
├── features/               ← FASE 2: Feature engineering
│   ├── technical.py        ← Indicadores tecnicos sobre precio BTC
│   ├── orderbook.py        ← Features del order book de Polymarket
│   ├── market_features.py  ← Features del share y mercado
│   └── builder.py          ← Orquestador: genera feature matrix completa
│
├── models/                 ← FASE 2: Modelo ML
│   ├── trainer.py          ← Entrenamiento XGBoost
│   ├── predictor.py        ← Inferencia en tiempo real
│   └── backtester.py       ← Evaluacion historica + simulacion PnL
│
├── strategy/               ← FASE 3: Logica de trading
│   ├── signal.py           ← Genera senales BUY YES / BUY NO / SKIP
│   ├── sizing.py           ← Kelly Criterion fraccional
│   └── regime_filter.py    ← Filtro de volatilidad/regimen
│
├── execution/              ← FASE 4: Ejecucion de ordenes
│   ├── order_manager.py    ← Place/cancel orders via CLOB API
│   ├── heartbeat.py        ← Mantener sesion activa (PING cada 20s)
│   └── position_tracker.py ← Estado del portafolio y PnL
│
├── validate/               ← Tests y validacion
│   ├── check_static.py     ← Nivel 1: sin red (11 tests)
│   ├── check_rest.py       ← Nivel 2: REST API (11 tests)
│   ├── check_websockets.py ← Nivel 3: WebSocket 30s en vivo (10 checks)
│   ├── check_resolved.py   ← Integridad de resolved_markets
│   └── backfill_resolved.py← Rellenar resoluciones retroactivamente
│
├── documentos/
│   ├── polymarket_guia_completa.md
│   └── roadmap_completo.md  ← ESTE ARCHIVO
│
└── logs/
```

---

## FASE 1 — PIPELINE DE DATOS
> Estado: COMPLETADA

### Que hace
Recolecta y persiste todos los datos necesarios para entrenar el modelo y operar.

### Componentes

**4 tareas en paralelo en main.py:**

| Tarea | Intervalo | Funcion |
|---|---|---|
| WebSocket RTDS | Tiempo real | Precio BTC de Binance y Chainlink |
| WebSocket Market | Tiempo real | Order book, trades, price changes |
| Market Discovery | Cada 60s | Busca mercado BTC 5-min activo via REST |
| Resolved Poller | Cada 60s | Detecta mercados cerrados y guarda resultado |

**6 tablas en la DB:**

| Tabla | Datos | Uso |
|---|---|---|
| btc_prices | Precio BTC cada tick (Binance + Chainlink) | Features tecnicos |
| orderbook_snapshots | Book completo con bids/asks JSON | Features de order book |
| price_changes | Cambios nivel a nivel del book | Order flow analysis |
| last_trades | Cada trade ejecutado (precio, size, side) | Volumen y flujo |
| resolved_markets | Resultado de cada mercado 5-min (ground truth) | Target variable del ML |
| active_markets | Mercados activos descubiertos | Control de estado |

### Bugs encontrados y corregidos
1. **RTDS filter rechazado**: la API de Polymarket rechaza `filters: "btcusdt"` (string plano). Fix: suscribirse sin filtro, filtrar en Python.
2. **market_resolved nunca capturado**: el WS emite la resolucion ~2 min despues del cierre, pero el bot ya se desuscribio al cambiar de mercado. Fix: poller REST cada 60s.
3. **clobTokenIds como string JSON**: la Gamma API devuelve `"[\"123\",\"456\"]"` (string) en vez de lista. Fix: `_parse_token_ids()` con `json.loads()`.

### Endpoints utilizados

| API | Endpoint | Uso |
|---|---|---|
| Gamma REST | `GET /events?slug=btc-updown-5m-<ts>` | Market discovery |
| CLOB REST | `GET /prices-history` | Historico del share |
| CLOB REST | `GET /book?token_id=<id>` | Order book snapshot |
| CLOB REST | `GET /midpoint?token_id=<id>` | Precio midpoint |
| WS RTDS | `wss://ws-live-data.polymarket.com` | Precios BTC real-time |
| WS Market | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | Order book stream |

---

## FASE 2 — FEATURE ENGINEERING + MODELO ML
> Estado: EN PROGRESO

### Objetivo
Construir un modelo XGBoost que prediga P(BTC_up) para cada intervalo de 5 min.
El modelo debe superar win rate 53% para ser rentable despues de fees.

### 2.1 Features Tecnicos (features/technical.py)

Calculados sobre la serie de precios BTC (de btc_prices en la DB).

| Feature | Descripcion | Rango | Intuicion |
|---|---|---|---|
| rsi_3 | RSI de 3 periodos (agresivo) | 0-100 | Sobrecompra/sobreventa a corto plazo |
| rsi_5 | RSI de 5 periodos | 0-100 | Mas suave que rsi_3 |
| bb_position | Posicion dentro de Bollinger Bands | 0-1 | 0=banda inferior, 1=banda superior |
| bb_width | Ancho de las bandas (volatilidad) | >0 | Mayor ancho = mas volatilidad |
| ema_cross_3_8 | EMA(3) - EMA(8) normalizado | -1 a 1 | Positivo = tendencia alcista |
| ema_cross_5_13 | EMA(5) - EMA(13) normalizado | -1 a 1 | Tendencia de medio plazo |
| momentum_1 | Retorno del ultimo periodo (5 min) | % | Inercia inmediata |
| momentum_3 | Retorno de los ultimos 3 periodos | % | Inercia de 15 min |
| momentum_6 | Retorno de los ultimos 6 periodos | % | Inercia de 30 min |
| volatility_6 | Desviacion estandar de retornos (6 per) | >0 | Volatilidad realizada 30 min |
| volatility_12 | Desviacion estandar de retornos (12 per) | >0 | Volatilidad realizada 1 hora |
| vwap_diff | (precio_actual - VWAP) / VWAP | % | Desviacion del valor justo |
| atr_6 | Average True Range (6 periodos) | >0 | Rango tipico de movimiento |
| price_vs_high_12 | Precio actual vs max de 1 hora | 0-1 | Posicion en rango reciente |
| price_vs_low_12 | Precio actual vs min de 1 hora | 0-1 | Posicion en rango reciente |

**Periodo base**: 5 minutos (cada periodo = 1 mercado BTC 5-min).
**Datos de entrada**: OHLC resampleado a 5 min desde btc_prices (fuente Chainlink).

### 2.2 Features del Order Book (features/orderbook.py)

Calculados del order book de Polymarket (de orderbook_snapshots y price_changes).

| Feature | Descripcion | Rango | Intuicion |
|---|---|---|---|
| ob_imbalance | (bid_vol - ask_vol) / (bid_vol + ask_vol) | -1 a 1 | Presion compradora vs vendedora |
| ob_depth_bid_3 | Volumen total en 3 mejores niveles bid | >0 | Soporte inmediato |
| ob_depth_ask_3 | Volumen total en 3 mejores niveles ask | >0 | Resistencia inmediata |
| ob_spread | best_ask - best_bid | 0-1 | Liquidez (menor spread = mas liquido) |
| ob_midpoint | (best_bid + best_ask) / 2 | 0-1 | Probabilidad implicita del mercado |
| trade_flow_net | Volumen BUY - volumen SELL (ultimos N trades) | cualquier valor | Presion neta de trading |
| trade_count | Numero de trades en el ultimo periodo | >=0 | Actividad del mercado |
| trade_avg_size | Tamano promedio de trades recientes | >0 | Retail vs institucional |
| trade_vwap | VWAP de trades recientes del share | 0-1 | Precio promedio ponderado |

### 2.3 Features del Mercado/Share (features/market_features.py)

Calculados sobre el propio mercado de Polymarket y variables temporales.

| Feature | Descripcion | Rango | Intuicion |
|---|---|---|---|
| share_price_yes | Precio actual del share YES | 0-1 | Lo que el mercado cree |
| share_price_change | Cambio del precio del share en ultimo min | % | Momentum del share |
| hour_sin | sin(2*pi*hora/24) | -1 a 1 | Estacionalidad ciclica (hora) |
| hour_cos | cos(2*pi*hora/24) | -1 a 1 | Estacionalidad ciclica (hora) |
| dow_sin | sin(2*pi*dia/7) | -1 a 1 | Estacionalidad ciclica (dia semana) |
| dow_cos | cos(2*pi*dia/7) | -1 a 1 | Estacionalidad ciclica (dia semana) |
| streak_up | Mercados consecutivos resueltos como UP | >=0 | Momentum de mercado |
| streak_down | Mercados consecutivos resueltos como DOWN | >=0 | Momentum de mercado |
| prev_result | Resultado del mercado anterior (1=UP, 0=DOWN) | 0 o 1 | Autocorrelacion serial |

### 2.4 Feature Builder (features/builder.py)

Orquesta los 3 modulos anteriores para generar una matriz de features lista para el modelo.

Dos modos de operacion:
1. **Batch (entrenamiento)**: lee toda la DB, genera DataFrame con 1 fila por mercado resuelto
2. **Real-time (inferencia)**: dado el estado actual, genera 1 vector de features

### 2.5 Modelo (models/trainer.py)

| Aspecto | Decision |
|---|---|
| Algoritmo | XGBoost (baseline) + LightGBM (comparacion) |
| Target | Binario: 1 = BTC subio (Yes), 0 = BTC bajo (No) |
| Split | Temporal (70% train, 15% val, 15% test — en orden cronologico, NO aleatorio) |
| Metricas | Accuracy, Precision, Recall, F1, ROC AUC, Brier Score |
| Umbral de exito | Accuracy > 53% en test set (minimo para rentabilidad) |
| Hiperparametros | Grid search en: max_depth, learning_rate, n_estimators, subsample |
| Output | Modelo serializado en .pkl + reporte de metricas |
| Feature importance | SHAP values para interpretar que features importan |

### 2.6 Predictor (models/predictor.py)

Modulo de inferencia que usa el modelo entrenado:
- Carga el .pkl al iniciar
- Recibe vector de features del builder
- Devuelve: `{prob_up: float, prob_down: float, confidence: float, should_trade: bool}`
- `should_trade = True` si `max(prob_up, prob_down) > min_confidence`

### 2.7 Backtester (models/backtester.py)

Simulacion historica end-to-end:
- Itera sobre resolved_markets en orden cronologico
- Para cada mercado: genera features → modelo predice → simula orden
- Calcula: PnL total, win rate, max drawdown, Sharpe ratio, num trades
- Aplica fees reales de Polymarket (variable segun precio del share)
- Genera reporte detallado

### Datos necesarios para Fase 2
- Minimo: ~1,000 mercados resueltos con precios BTC (~3.5 dias de bot corriendo)
- Recomendado: ~4,000+ (~2 semanas)
- Podemos empezar a escribir todo el codigo ahora y entrenar cuando haya datos

---

## FASE 3 — ESTRATEGIA HIBRIDA
> Estado: PENDIENTE (depende de Fase 2)

### Objetivo
Convertir la probabilidad del modelo en decisiones de trading optimas.

### 3.1 Generador de Senales (strategy/signal.py)

```
INPUT: prob_up, prob_down, confidence, market_state
OUTPUT: Signal(action, side, target_price, urgency)

Reglas:
  prob_up > 0.58       → BUY YES  (limit order a precio_yes - 0.01)
  prob_down > 0.58     → BUY NO   (limit order a precio_no - 0.01)
  0.45 < prob < 0.55   → SKIP     (no operar, incertidumbre alta)
  confidence < 0.55    → SKIP     (modelo no esta seguro)
  regime = low_vol     → SKIP o reducir tamano

Aspecto "hibrido":
  - No solo compra market orders (paga spread completo)
  - Pone limit orders asimetricas segun su prediccion
  - Si cree que sube: bid agresivo en YES, ask pasivo en NO
  - Gana spread cuando acierta, pierde menos cuando falla
```

### 3.2 Sizing (strategy/sizing.py)

Kelly Criterion fraccional:
```
f* = (p * b - q) / b
  p = probabilidad de ganar (del modelo)
  q = 1 - p
  b = odds netos (ej: a precio 0.48, b = 0.52/0.48 = 1.083)

Fraccion Kelly: usar 25-50% del Kelly completo
  → reduce volatilidad del portafolio significativamente
  → protege contra errores del modelo
```

Controles:
- Maximo % del capital por operacion: configurable (default 2%)
- Stop-loss implicito: el share puede ir a $0 (perdida maxima = inversion)
- No apalancamiento

### 3.3 Filtro de Regimen (strategy/regime_filter.py)

Detecta el regimen de volatilidad del mercado:
- **Alta volatilidad**: ATR alto, BB ancho → mas oportunidades, operar normal
- **Baja volatilidad**: ATR bajo, precio estancado → reducir tamano o no operar
- **Tendencia fuerte**: momentum consistente → seguir la tendencia
- **Choppy**: momentum cambia de signo frecuentemente → reducir exposicion

---

## FASE 4 — EJECUCION EN VIVO
> Estado: PENDIENTE (depende de Fase 3)

### Objetivo
Conectar el bot a Polymarket para operar con fondos reales.

### 4.1 Order Manager (execution/order_manager.py)

Interfaz con Polymarket CLOB API:
```
Funciones:
  place_limit_order(side, token_id, price, size)
  place_market_order(side, token_id, size)
  cancel_order(order_id)
  cancel_all_orders()
  get_open_orders()
  get_fills(market_id)

Autenticacion:
  - API Key + Secret + Passphrase
  - Firma de ordenes con wallet privada (EIP-712)
  - SDK: py_clob_client

Endpoints:
  POST /order      → crear orden
  DELETE /order     → cancelar orden
  GET /orders       → listar ordenes abiertas
  GET /trades       → historial de fills
```

### 4.2 Heartbeat (execution/heartbeat.py)

Mantener conexion activa:
- Enviar PING al CLOB API cada 20 segundos (Session Heartbeat)
- Si no se envia → las ordenes limit se cancelan automaticamente
- Debe correr como task asyncio en paralelo

### 4.3 Position Tracker (execution/position_tracker.py)

Estado del portafolio en tiempo real:
- USDC disponible (no comprometido en ordenes)
- Shares YES y NO en cada mercado activo
- PnL realizado (trades cerrados)
- PnL no realizado (posiciones abiertas valoradas a mercado)
- Historial de operaciones

### 4.4 Modos de operacion

| Modo | Descripcion | Riesgo |
|---|---|---|
| Paper Trading | Simula ordenes sin enviarlas. Registra como si fueran reales. | 0 |
| Live (conservador) | Opera con 1-2% del capital. Kelly fraccional al 25%. | Bajo |
| Live (moderado) | Opera con 3-5% del capital. Kelly fraccional al 50%. | Medio |

### 4.5 Credenciales necesarias

| Variable | Donde obtenerla |
|---|---|
| POLY_API_KEY | polymarket.com -> Settings -> API Keys |
| POLY_API_SECRET | Se genera junto con la API Key |
| POLY_API_PASSPHRASE | Se genera junto con la API Key |
| POLY_PRIVATE_KEY | Private key de tu wallet Polygon (MetaMask export) |

### 4.6 Flujo completo en vivo

```
Cada 5 minutos (nuevo mercado BTC):
  1. Discovery detecta nuevo mercado activo
  2. WebSocket se suscribe al nuevo mercado
  3. Feature builder genera vector de features
  4. Predictor infiere P(up)
  5. Signal generator decide: BUY YES, BUY NO, o SKIP
  6. Si opera: Sizing calcula cuanto (Kelly fraccional)
  7. Order manager envia la orden limit/market
  8. Monitor de fills espera ejecucion
  9. Al resolverse el mercado: shares ganadores = $1, perdedores = $0
  10. Position tracker actualiza PnL
  11. Repeat
```

---

## INFRAESTRUCTURA

### Deploy con Dokploy

```
VPS Oracle (4 OCPU, 32GB RAM, Ubuntu)
├── Dokploy
│   └── docker-compose (desde GitHub)
│       ├── postgres:16-alpine
│       │   ├── Volume persistente (pgdata)
│       │   └── Puerto 5432 expuesto (para conectar desde PC)
│       └── bot (python main.py)
│           ├── DATABASE_URL → postgres interno
│           └── Variables de entorno via Dokploy
```

### Conexion remota a la DB

Desde tu PC con DBeaver/pgAdmin:
```
Host: <IP_VPS_Oracle>
Port: 5432
Database: bot_cripto
User: bot
Password: <la configurada en .env>
```

### Variables de entorno

```env
# PostgreSQL
DATABASE_URL=postgresql://bot:password@postgres:5432/bot_cripto
POSTGRES_DB=bot_cripto
POSTGRES_USER=bot
POSTGRES_PASSWORD=<segura>
POSTGRES_PORT=5432

# Polymarket (solo Fase 4)
POLY_API_KEY=
POLY_API_SECRET=
POLY_API_PASSPHRASE=
POLY_PRIVATE_KEY=

# Bot
BOT_RISK_FRACTION=0.01
BOT_MIN_CONFIDENCE=0.58
BOT_PAPER_MODE=true
```

---

## METRICAS DE EXITO

| Metrica | Minimo viable | Objetivo |
|---|---|---|
| Win rate del modelo | >53% | >57% |
| ROC AUC | >0.55 | >0.62 |
| PnL neto (backtesting) | >0 despues de fees | >10% mensual |
| Max drawdown | <20% | <10% |
| Sharpe ratio | >0.5 | >1.5 |
| Uptime del bot | >95% | >99% |

---

## RIESGOS Y MITIGACIONES

| Riesgo | Impacto | Mitigacion |
|---|---|---|
| Modelo no supera 53% win rate | No es rentable | Iterar features, probar otros modelos, o no operar |
| API de Polymarket cambia | Bot deja de funcionar | Monitoreo de errores + alertas |
| Latencia de internet en VPS | Ordenes no se ejecutan a tiempo | Oracle Cloud tiene buen peering, limit orders toleran delay |
| Mercados BTC 5-min desaparecen | No hay donde operar | Pivotar a BTC 15-min o otro mercado |
| Overfitting del modelo | Win rate alto en train, bajo en produccion | Split temporal estricto, validacion walk-forward |
| Falta de liquidez en el mercado | Ordenes no se llenan | Monitorear spread, no operar si spread > umbral |

---

*Documento de referencia interna. Actualizar con cada avance.*
