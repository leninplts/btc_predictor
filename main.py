"""
main.py
-------
Orquestador principal del pipeline de datos — Fase 1.

Flujo:
  1. Inicializa la base de datos SQLite
  2. Verifica conectividad con las APIs de Polymarket
  3. Descubre el mercado BTC 5-min activo via REST
  4. Lanza los workers WebSocket (RTDS + Market Channel) en paralelo
  5. Cada 5 minutos re-verifica si hay un nuevo mercado BTC 5-min activo
  6. Cada 60 segundos imprime estadisticas de cuantos registros se han guardado

Uso:
  python main.py

Para detener: Ctrl+C
"""

import asyncio
import signal
import sys
import os
from datetime import datetime, timezone, timedelta

_TZ_LIMA = timezone(timedelta(hours=-5))

from loguru import logger

from data import storage
from data import rest_client as rest
from data.websocket_client import BotState, run_pipeline


# ---------------------------------------------------------------------------
# Configuracion de logging
# ---------------------------------------------------------------------------

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logger.remove()  # quitar handler default

# Consola: nivel INFO, formato limpio
logger.add(
    sys.stdout,
    level="INFO",
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    colorize=True
)

# Archivo: nivel DEBUG, rotacion diaria
logger.add(
    os.path.join(LOG_DIR, "pipeline_{time:YYYY-MM-DD}.log"),
    level="DEBUG",
    rotation="00:00",      # nuevo archivo cada dia
    retention="14 days",   # conservar 2 semanas
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{line} | {message}"
)


# ---------------------------------------------------------------------------
# Tarea: discovery periodico del mercado activo
# ---------------------------------------------------------------------------

MARKET_CHECK_INTERVAL = 60   # segundos entre checks de mercado activo

async def market_discovery_loop(state: BotState) -> None:
    """
    Cada MARKET_CHECK_INTERVAL segundos consulta la REST API para ver si
    hay un mercado BTC 5-min activo y actualiza el estado del bot.
    Esto cubre el caso donde el WebSocket new_market no se recibe a tiempo.
    """
    logger.info("Discovery loop iniciado")

    while state.running:
        try:
            market = rest.get_active_btc_5m_market()

            if market:
                market_id = market["market_id"]
                yes_id    = market["asset_id_yes"]
                no_id     = market["asset_id_no"]

                # Solo actualizar si es un mercado distinto al actual
                if market_id != state.active_market_id:
                    storage.upsert_active_market(
                        market_id=market_id,
                        asset_id_yes=yes_id,
                        asset_id_no=no_id,
                        question=market.get("question", ""),
                        slug=market.get("slug", ""),
                        description=market.get("description", "")
                    )
                    state.update_market(market_id, yes_id, no_id,
                                        slug=market.get("slug", ""))
                    logger.success(
                        f"Mercado activo: {market.get('question', '')} "
                        f"| slug={market.get('slug', '')}"
                    )
                else:
                    logger.debug(f"Mercado sin cambios: {market.get('slug', '')}")
            else:
                # Fallback: buscar por texto
                logger.debug("Intentando busqueda por texto...")
                markets = rest.search_btc_markets("Bitcoin Up or Down")
                for m in markets:
                    if m.get("active") and m["market_id"] != state.active_market_id:
                        storage.upsert_active_market(
                            market_id=m["market_id"],
                            asset_id_yes=m["asset_id_yes"],
                            asset_id_no=m["asset_id_no"],
                            question=m.get("question", ""),
                            slug=m.get("slug", "")
                        )
                        state.update_market(m["market_id"], m["asset_id_yes"],
                                            m["asset_id_no"], slug=m.get("slug", ""))
                        break

        except Exception as e:
            logger.error(f"Error en discovery loop: {e}")

        await asyncio.sleep(MARKET_CHECK_INTERVAL)


# ---------------------------------------------------------------------------
# Tarea: poller de mercados resueltos (ground truth)
# ---------------------------------------------------------------------------

RESOLVED_POLL_INTERVAL = 60   # segundos entre polls (cada minuto)

async def resolved_markets_poller(state: BotState) -> None:
    """
    Cada minuto consulta la Gamma API para detectar mercados BTC 5-min
    que se hayan cerrado recientemente, y los persiste en resolved_markets.

    El WebSocket market_resolved llega ~2 min despues del cierre y ademas
    nos desuscribimos del mercado viejo al cambiar de intervalo, asi que
    NUNCA lo capturamos en tiempo real. Este poller es el mecanismo confiable.

    Para cada mercado resuelto:
      - winning_outcome: "Yes" (UP) o "No" (DOWN)
      - btc_price_open : precio BTC al inicio del intervalo (de DB o state)
      - btc_price_close: precio BTC al final del intervalo (de DB o state)
      - direction      : UP o DOWN (calculado automaticamente)
    """
    logger.info("Poller de mercados resueltos iniciado")

    # Esperar 30s para que los WS conecten y tengamos precios BTC
    await asyncio.sleep(30)

    # Set de market_ids ya procesados (para no reintentar cada ciclo)
    already_resolved: set[str] = set()

    # Pre-cargar los que ya estan en la DB
    conn = storage.get_connection()
    try:
        cur = conn.cursor()
        rows = storage._fetchall(cur, "SELECT market_id FROM resolved_markets")
        already_resolved = {row["market_id"] for row in rows}
        if already_resolved:
            logger.info(f"Poller: {len(already_resolved)} mercados ya resueltos en DB")
    finally:
        conn.close()

    while state.running:
        try:
            resolved_list = rest.get_recent_resolved_btc_5m_markets(lookback_intervals=12)
            new_count = 0

            for r in resolved_list:
                market_id = r["market_id"]

                # Saltar si ya lo procesamos
                if market_id in already_resolved:
                    continue

                ts_interval = r.get("ts_interval_start", 0)
                ts_open_ms  = ts_interval * 1000
                ts_close_ms = (ts_interval + 300) * 1000

                # Formato legible del intervalo (hora Lima UTC-5)
                dt_open  = datetime.fromtimestamp(ts_interval, tz=_TZ_LIMA)
                dt_close = datetime.fromtimestamp(ts_interval + 300, tz=_TZ_LIMA)
                intervalo_str = (f"{dt_open.strftime('%Y-%m-%d %H:%M')} -> "
                                 f"{dt_close.strftime('%H:%M')} Lima")

                # Precio BTC al inicio: primero memoria, luego DB
                btc_open = state.get_open_price(market_id)
                if btc_open is None:
                    btc_open = storage.get_btc_price_at(ts_open_ms, source="chainlink")
                if btc_open is None:
                    btc_open = storage.get_btc_price_at(ts_open_ms, source="binance")

                # Precio BTC al cierre: primero DB (mas preciso), luego state
                btc_close = storage.get_btc_price_at(ts_close_ms, source="chainlink")
                if btc_close is None:
                    btc_close = storage.get_btc_price_at(ts_close_ms, source="binance")
                if btc_close is None:
                    btc_close = (state.last_btc_price_chainlink
                                 or state.last_btc_price_binance)

                storage.insert_resolved_market(
                    market_id=market_id,
                    asset_id_yes=r["asset_id_yes"],
                    asset_id_no=r["asset_id_no"],
                    winning_outcome=r["winning_outcome"],
                    winning_asset=r.get("winning_asset_id", ""),
                    question=r.get("question", ""),
                    slug=r.get("slug", ""),
                    btc_price_open=btc_open,
                    btc_price_close=btc_close,
                    ts_open=ts_open_ms if btc_open else None,
                    ts_resolved=ts_close_ms
                )

                already_resolved.add(market_id)
                new_count += 1

                # Calcular direccion para el log
                if btc_open and btc_close:
                    direction = "UP" if btc_close > btc_open else "DOWN"
                    logger.success(
                        f"RESUELTO [{r['winning_outcome']}] {intervalo_str} "
                        f"| BTC ${btc_open:,.2f} -> ${btc_close:,.2f} ({direction})"
                    )
                else:
                    logger.success(
                        f"RESUELTO [{r['winning_outcome']}] {intervalo_str} "
                        f"| BTC precios no disponibles en DB"
                    )

            if new_count > 0:
                logger.info(f"Poller: {new_count} nuevos mercados resueltos registrados")

        except Exception as e:
            logger.error(f"Error en resolved poller: {e}", exc_info=True)

        await asyncio.sleep(RESOLVED_POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Tarea: estadisticas periodicas
# ---------------------------------------------------------------------------

STATS_INTERVAL = 60   # segundos entre impresion de estadisticas

async def stats_loop(state: BotState) -> None:
    """Cada minuto imprime cuantos registros se han guardado."""
    while state.running:
        await asyncio.sleep(STATS_INTERVAL)
        try:
            stats = storage.get_db_stats()
            btc   = storage.get_latest_btc_price("binance")
            btc_c = storage.get_latest_btc_price("chainlink")

            price_str = f"${state.last_btc_price_binance:,.2f}" \
                        if state.last_btc_price_binance else "N/A"
            chainlink_str = f"${state.last_btc_price_chainlink:,.2f}" \
                            if state.last_btc_price_chainlink else "N/A"

            logger.info(
                f"--- STATS [{datetime.now(_TZ_LIMA).strftime('%H:%M:%S Lima')}] ---\n"
                f"  BTC Binance   : {price_str}\n"
                f"  BTC Chainlink : {chainlink_str}\n"
                f"  btc_prices    : {stats.get('btc_prices', 0):,} registros\n"
                f"  orderbook_snap: {stats.get('orderbook_snapshots', 0):,} registros\n"
                f"  price_changes : {stats.get('price_changes', 0):,} registros\n"
                f"  last_trades   : {stats.get('last_trades', 0):,} registros\n"
                f"  resolved      : {stats.get('resolved_markets', 0):,} mercados\n"
                f"  active_markets: {stats.get('active_markets', 0):,} mercados"
            )
        except Exception as e:
            logger.error(f"Error en stats loop: {e}")


# ---------------------------------------------------------------------------
# Arranque principal
# ---------------------------------------------------------------------------

async def main() -> None:
    logger.info("=" * 60)
    logger.info("  BOT CRIPTO — Pipeline de Datos — Fase 1")
    logger.info("=" * 60)

    # 1. Inicializar DB
    storage.init_db()

    # 2. Verificar conectividad
    logger.info("Verificando conectividad con Polymarket...")
    clob_ok  = rest.ping_clob()
    gamma_ok = rest.ping_gamma()

    if not clob_ok:
        logger.warning("CLOB API no responde — datos de order book pueden fallar")
    else:
        logger.success("CLOB API: OK")

    if not gamma_ok:
        logger.warning("Gamma API no responde — market discovery puede fallar")
    else:
        logger.success("Gamma API: OK")

    # 3. Estado compartido
    state = BotState()

    # 4. Primer discovery sincrono para no esperar el primer tick del loop
    logger.info("Buscando mercado BTC 5-min activo...")
    market = rest.get_active_btc_5m_market()
    if market:
        storage.upsert_active_market(
            market_id=market["market_id"],
            asset_id_yes=market["asset_id_yes"],
            asset_id_no=market["asset_id_no"],
            question=market.get("question", ""),
            slug=market.get("slug", ""),
            description=market.get("description", "")
        )
        state.update_market(
            market["market_id"],
            market["asset_id_yes"],
            market["asset_id_no"],
            slug=market.get("slug", "")
        )
    else:
        logger.warning(
            "No se encontro mercado BTC 5-min activo ahora mismo.\n"
            "El pipeline esperara hasta que haya uno disponible."
        )

    # 5. Lanzar todas las tareas en paralelo
    tasks = [
        asyncio.create_task(run_pipeline(state),              name="websockets"),
        asyncio.create_task(market_discovery_loop(state),     name="discovery"),
        asyncio.create_task(resolved_markets_poller(state),   name="resolved_poller"),
        asyncio.create_task(stats_loop(state),                name="stats"),
    ]

    logger.success("Pipeline corriendo. Ctrl+C para detener.")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        state.stop()
        for task in tasks:
            task.cancel()
        logger.info("Pipeline detenido correctamente")
        final_stats = storage.get_db_stats()
        logger.info(f"Registros totales guardados: {final_stats}")


# ---------------------------------------------------------------------------
# Entry point con manejo de Ctrl+C
# ---------------------------------------------------------------------------

def _handle_sigint(loop: asyncio.AbstractEventLoop) -> None:
    logger.info("Ctrl+C recibido, deteniendo...")
    for task in asyncio.all_tasks(loop):
        task.cancel()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Manejo de Ctrl+C en Windows (no soporta add_signal_handler)
    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGINT,  lambda: _handle_sigint(loop))
        loop.add_signal_handler(signal.SIGTERM, lambda: _handle_sigint(loop))

    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Detenido por el usuario")
    finally:
        loop.close()
