"""
websocket_client.py
-------------------
Dos conexiones WebSocket corriendo en paralelo con asyncio:

  1. RTDS (Real-Time Data Socket)
     wss://ws-live-data.polymarket.com
     - Fuente Binance  : precio BTC/USDT en tiempo real
     - Fuente Chainlink: precio BTC/USD (mismo oracle que usa Polymarket para resolver)

  2. Market Channel
     wss://ws-subscriptions-clob.polymarket.com/ws/market
     - book            : snapshot completo del order book al suscribirse o al haber un trade
     - price_change    : cambios nivel a nivel (stream continuo)
     - best_bid_ask    : mejor bid/ask en tiempo real
     - last_trade_price: precio y size de cada trade ejecutado
     - market_resolved : aviso cuando el mercado cierra (para capturar resultado)
     - new_market      : aviso cuando un nuevo mercado BTC 5-min abre

Cada mensaje recibido se persiste en SQLite via storage.py.
Los asset_ids que se monitorearan se pasan desde el exterior (main.py).
"""

import asyncio
import json
import time
from typing import Optional
from loguru import logger

import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from data import storage


# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------

RTDS_URL   = "wss://ws-live-data.polymarket.com"
MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

PING_INTERVAL_RTDS   = 5    # segundos - el servidor exige pong en <10s
PING_INTERVAL_MARKET = 10   # segundos - send PING, esperar PONG
RECONNECT_DELAY      = 3    # segundos entre reintentos de conexion


# ---------------------------------------------------------------------------
# Estado compartido (thread-safe via asyncio, un solo hilo)
# ---------------------------------------------------------------------------

class BotState:
    """
    Estado global del pipeline de datos.
    main.py actualiza asset_ids cuando detecta un nuevo mercado activo.
    """
    def __init__(self):
        self.asset_ids: list[str] = []          # [yes_token_id, no_token_id]
        self.active_market_id: Optional[str] = None
        self.running: bool = True
        self.last_btc_price_binance: Optional[float] = None
        self.last_btc_price_chainlink: Optional[float] = None
        self.last_btc_ts: Optional[int] = None

    def update_market(self, market_id: str, yes_id: str, no_id: str) -> None:
        self.active_market_id = market_id
        self.asset_ids = [yes_id, no_id]
        logger.info(f"Estado actualizado — mercado activo: {market_id[:20]}...")

    def stop(self) -> None:
        self.running = False


# ---------------------------------------------------------------------------
# RTDS WebSocket — Precio BTC en tiempo real
# ---------------------------------------------------------------------------

async def rtds_worker(state: BotState) -> None:
    """
    Mantiene conexion con el RTDS de Polymarket.
    Se suscribe a precios BTC de Binance y Chainlink.
    Guarda cada tick en la DB y actualiza state.last_btc_price_*.
    Se reconecta automaticamente ante desconexiones.
    """
    subscribe_msg = json.dumps({
        "action": "subscribe",
        "subscriptions": [
            # Binance: sin filtro — filtramos por simbolo en el handler
            # (la API rechaza filtros en formato string plano)
            {
                "topic": "crypto_prices",
                "type": "update"
            },
            # Chainlink: filtro en formato JSON valido
            {
                "topic": "crypto_prices_chainlink",
                "type": "*"
            }
        ]
    })

    while state.running:
        try:
            logger.info("RTDS: conectando...")
            async with websockets.connect(
                RTDS_URL,
                ping_interval=None,       # manejamos PING manualmente
                max_size=2**20,
                open_timeout=15,
            ) as ws:
                await ws.send(subscribe_msg)
                logger.success("RTDS: conectado y suscrito a BTC (Binance + Chainlink)")

                last_ping = time.time()

                async for raw in ws:
                    if not state.running:
                        break

                    # Heartbeat manual cada PING_INTERVAL_RTDS segundos
                    now = time.time()
                    if now - last_ping >= PING_INTERVAL_RTDS:
                        await ws.send("PING")
                        last_ping = now

                    # Ignorar respuestas de PING/PONG en texto plano
                    if isinstance(raw, str) and raw.strip() in ("PONG", "pong"):
                        continue

                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    await _handle_rtds_message(msg, state)

        except (ConnectionClosedError, ConnectionClosedOK) as e:
            logger.warning(f"RTDS: conexion cerrada ({e}). Reconectando en {RECONNECT_DELAY}s...")
        except Exception as e:
            logger.error(f"RTDS: error inesperado: {e}. Reconectando en {RECONNECT_DELAY}s...")

        if state.running:
            await asyncio.sleep(RECONNECT_DELAY)

    logger.info("RTDS: worker detenido")


async def _handle_rtds_message(msg: dict, state: BotState) -> None:
    """Procesa un mensaje del RTDS y persiste en DB."""
    topic = msg.get("topic", "")
    ts_recv_ms = int(time.time() * 1000)

    # --- Precio BTC de Binance ---
    if topic == "crypto_prices":
        payload = msg.get("payload", {})
        symbol  = payload.get("symbol", "")
        price   = payload.get("value")
        ts      = payload.get("timestamp", ts_recv_ms)

        if symbol == "btcusdt" and price is not None:
            state.last_btc_price_binance = float(price)
            state.last_btc_ts = ts
            storage.insert_btc_price(
                source="binance",
                symbol=symbol,
                price=float(price),
                ts=int(ts)
            )
            logger.debug(f"BTC Binance: ${price:,.2f}")

    # --- Precio BTC de Chainlink ---
    elif topic == "crypto_prices_chainlink":
        payload = msg.get("payload", {})
        symbol  = payload.get("symbol", "")
        price   = payload.get("value")
        ts      = payload.get("timestamp", ts_recv_ms)

        if "btc" in symbol.lower() and price is not None:
            state.last_btc_price_chainlink = float(price)
            storage.insert_btc_price(
                source="chainlink",
                symbol=symbol,
                price=float(price),
                ts=int(ts)
            )
            logger.debug(f"BTC Chainlink: ${price:,.2f}")


# ---------------------------------------------------------------------------
# Market Channel WebSocket — Order book de Polymarket
# ---------------------------------------------------------------------------

async def market_worker(state: BotState) -> None:
    """
    Mantiene conexion con el Market Channel de Polymarket.
    Se suscribe dinamicamente a los asset_ids del mercado BTC 5-min activo.
    Reconecta y re-suscribe cuando cambia el mercado activo.
    """
    while state.running:
        # Esperar a que main.py haya descubierto un mercado activo
        if not state.asset_ids:
            logger.info("Market WS: esperando asset_ids del mercado activo...")
            await asyncio.sleep(2)
            continue

        try:
            logger.info(f"Market WS: conectando para {len(state.asset_ids)} assets...")
            async with websockets.connect(
                MARKET_URL,
                ping_interval=None,
                max_size=2**20,
                open_timeout=15,
            ) as ws:

                current_asset_ids = list(state.asset_ids)
                await _subscribe_market(ws, current_asset_ids)
                logger.success(f"Market WS: suscrito a {current_asset_ids[0][:16]}... y NO token")

                last_ping = time.time()

                async for raw in ws:
                    if not state.running:
                        break

                    # Heartbeat manual
                    now = time.time()
                    if now - last_ping >= PING_INTERVAL_MARKET:
                        await ws.send("PING")
                        last_ping = now

                    if isinstance(raw, str) and raw.strip() == "PONG":
                        continue

                    try:
                        parsed = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    # El Market Channel puede enviar lista o dict
                    msgs = parsed if isinstance(parsed, list) else [parsed]
                    for msg in msgs:
                        await _handle_market_message(msg, state)

                    # Si los asset_ids cambiaron (nuevo mercado), reconectar
                    if state.asset_ids != current_asset_ids:
                        logger.info("Market WS: asset_ids cambiaron, reconectando...")
                        break

        except (ConnectionClosedError, ConnectionClosedOK) as e:
            logger.warning(f"Market WS: conexion cerrada ({e}). Reconectando en {RECONNECT_DELAY}s...")
        except Exception as e:
            logger.error(f"Market WS: error inesperado: {e}. Reconectando en {RECONNECT_DELAY}s...")

        if state.running:
            await asyncio.sleep(RECONNECT_DELAY)

    logger.info("Market WS: worker detenido")


async def _subscribe_market(ws, asset_ids: list[str]) -> None:
    """Envia el mensaje de suscripcion al Market Channel."""
    msg = json.dumps({
        "assets_ids":             asset_ids,
        "type":                   "market",
        "custom_feature_enabled": True   # habilita best_bid_ask, new_market, market_resolved
    })
    await ws.send(msg)


async def _handle_market_message(msg: dict, state: BotState) -> None:
    """
    Despacha cada tipo de evento del Market Channel al handler correspondiente.
    Tipos: book | price_change | best_bid_ask | last_trade_price |
           tick_size_change | new_market | market_resolved
    """
    event_type = msg.get("event_type", "")

    if event_type == "book":
        _handle_book(msg)

    elif event_type == "price_change":
        _handle_price_change(msg)

    elif event_type == "best_bid_ask":
        _handle_best_bid_ask(msg, state)

    elif event_type == "last_trade_price":
        _handle_last_trade(msg)

    elif event_type == "market_resolved":
        _handle_market_resolved(msg, state)

    elif event_type == "new_market":
        _handle_new_market(msg, state)

    elif event_type == "tick_size_change":
        logger.debug(f"Tick size cambiado: {msg.get('asset_id','')[:12]}... "
                     f"{msg.get('old_tick_size')} -> {msg.get('new_tick_size')}")


def _handle_book(msg: dict) -> None:
    """Snapshot completo del order book."""
    asset_id  = msg.get("asset_id", "")
    market_id = msg.get("market", "")
    bids      = msg.get("bids", [])
    asks      = msg.get("asks", [])
    ts_raw    = msg.get("timestamp", 0)
    hash_val  = msg.get("hash")

    try:
        ts = int(ts_raw)
    except (ValueError, TypeError):
        ts = int(time.time() * 1000)

    storage.insert_orderbook_snapshot(
        ts=ts,
        asset_id=asset_id,
        market_id=market_id,
        bids=bids,
        asks=asks,
        hash_val=hash_val
    )

    best_bid = float(bids[0]["price"]) if bids else None
    best_ask = float(asks[0]["price"]) if asks else None
    logger.info(
        f"BOOK snapshot | {len(bids)} bids / {len(asks)} asks | "
        f"bid={best_bid} ask={best_ask} | {asset_id[:12]}..."
    )


def _handle_price_change(msg: dict) -> None:
    """Cambios nivel a nivel del order book (stream de liquidity)."""
    market_id   = msg.get("market", "")
    ts_raw      = msg.get("timestamp", 0)
    changes     = msg.get("price_changes", [])

    try:
        ts = int(ts_raw)
    except (ValueError, TypeError):
        ts = int(time.time() * 1000)

    for change in changes:
        asset_id = change.get("asset_id", "")
        try:
            price    = float(change.get("price", 0))
            size     = float(change.get("size", 0))
            side     = change.get("side", "")
            best_bid = float(change.get("best_bid", 0)) if change.get("best_bid") else None
            best_ask = float(change.get("best_ask", 0)) if change.get("best_ask") else None
            hash_val = change.get("hash")

            storage.insert_price_change(
                ts=ts,
                asset_id=asset_id,
                market_id=market_id,
                price=price,
                size=size,
                side=side,
                best_bid=best_bid,
                best_ask=best_ask,
                hash_val=hash_val
            )
        except Exception as e:
            logger.warning(f"Error procesando price_change: {e} | data={change}")

    if changes:
        logger.debug(f"PRICE_CHANGE: {len(changes)} niveles actualizados | market={market_id[:12]}...")


def _handle_best_bid_ask(msg: dict, state: BotState) -> None:
    """Best bid/ask actualizado — util para ver el spread en tiempo real."""
    asset_id = msg.get("asset_id", "")
    best_bid = msg.get("best_bid")
    best_ask = msg.get("best_ask")
    spread   = msg.get("spread")
    ts_raw   = msg.get("timestamp", 0)

    try:
        ts = int(ts_raw)
    except (ValueError, TypeError):
        ts = int(time.time() * 1000)

    # Guardamos como price_change de size=0 para tener el dato historico
    if best_bid is not None:
        storage.insert_price_change(
            ts=ts,
            asset_id=asset_id,
            market_id=state.active_market_id or "",
            price=float(best_bid),
            size=0.0,
            side="BUY",
            best_bid=float(best_bid),
            best_ask=float(best_ask) if best_ask else None
        )

    logger.debug(
        f"BEST_BID_ASK | bid={best_bid} ask={best_ask} spread={spread} | "
        f"{asset_id[:12]}..."
    )


def _handle_last_trade(msg: dict) -> None:
    """Trade ejecutado — precio y size de la ultima operacion."""
    asset_id     = msg.get("asset_id", "")
    market_id    = msg.get("market", "")
    price_raw    = msg.get("price")
    size_raw     = msg.get("size")
    side         = msg.get("side", "")
    fee_rate_bps = msg.get("fee_rate_bps")
    ts_raw       = msg.get("timestamp", 0)

    try:
        ts    = int(ts_raw)
        price = float(price_raw)
        size  = float(size_raw)
    except (ValueError, TypeError) as e:
        logger.warning(f"Error parseando last_trade_price: {e} | msg={msg}")
        return

    storage.insert_last_trade(
        ts=ts,
        asset_id=asset_id,
        market_id=market_id,
        price=price,
        size=size,
        side=side,
        fee_rate_bps=fee_rate_bps
    )

    logger.info(f"TRADE | {side} {size:.2f} shares @ {price:.3f} | {asset_id[:12]}...")


def _handle_market_resolved(msg: dict, state: BotState) -> None:
    """
    El mercado fue resuelto. Guardamos el resultado (YES/NO) para ground truth.
    Comparamos con el ultimo precio BTC conocido para calcular UP/DOWN.
    """
    market_id       = msg.get("market", "")
    winning_outcome = msg.get("winning_outcome", "")
    winning_asset   = msg.get("winning_asset_id", "")
    question        = msg.get("question", "")
    slug            = msg.get("slug", "")
    ts_raw          = msg.get("timestamp", 0)

    asset_ids = msg.get("assets_ids", [])
    yes_id = asset_ids[0] if len(asset_ids) > 0 else ""
    no_id  = asset_ids[1] if len(asset_ids) > 1 else ""

    try:
        ts_resolved = int(ts_raw)
    except (ValueError, TypeError):
        ts_resolved = int(time.time() * 1000)

    # Precio BTC al cierre (mejor aproximacion disponible)
    btc_close = state.last_btc_price_chainlink or state.last_btc_price_binance

    storage.insert_resolved_market(
        market_id=market_id,
        asset_id_yes=yes_id,
        asset_id_no=no_id,
        winning_outcome=winning_outcome,
        winning_asset=winning_asset,
        question=question,
        slug=slug,
        btc_price_open=None,      # se puede enriquecer despues desde la DB
        btc_price_close=btc_close,
        ts_resolved=ts_resolved
    )

    logger.success(
        f"MERCADO RESUELTO | resultado={winning_outcome} | "
        f"BTC_close=${btc_close:,.2f} | {slug}"
    )


def _handle_new_market(msg: dict, state: BotState) -> None:
    """
    Nuevo mercado BTC 5-min abierto.
    Actualizamos el estado para que market_worker se re-suscriba.
    """
    market_id = msg.get("market", "")
    slug      = msg.get("slug", "")
    question  = msg.get("question", "")
    desc      = msg.get("description", "")
    asset_ids = msg.get("assets_ids", [])

    if len(asset_ids) < 2:
        logger.warning(f"new_market sin 2 asset_ids: {msg}")
        return

    yes_id = asset_ids[0]
    no_id  = asset_ids[1]

    # Solo actualizamos si es un mercado BTC 5-min
    if "btc" in slug.lower() or "bitcoin" in question.lower():
        storage.upsert_active_market(
            market_id=market_id,
            asset_id_yes=yes_id,
            asset_id_no=no_id,
            question=question,
            slug=slug,
            description=desc
        )
        state.update_market(market_id, yes_id, no_id)
        logger.success(f"NUEVO MERCADO detectado via WS: {slug}")
    else:
        logger.debug(f"new_market ignorado (no es BTC 5-min): {slug}")


# ---------------------------------------------------------------------------
# Punto de entrada: lanzar ambos workers en paralelo
# ---------------------------------------------------------------------------

async def run_pipeline(state: BotState) -> None:
    """
    Lanza RTDS + Market Channel en paralelo.
    Se detiene cuando state.running = False.
    """
    logger.info("Iniciando pipeline de datos WebSocket...")
    await asyncio.gather(
        rtds_worker(state),
        market_worker(state),
        return_exceptions=True
    )
    logger.info("Pipeline detenido")
