"""
main.py
-------
Orquestador principal del bot de trading BTC en Polymarket.

Flujo completo:
  1. Inicializa DB, verifica APIs, conecta Telegram
  2. Lanza WebSockets (RTDS + Market Channel)
  3. Cada nuevo mercado BTC 5-min:
     a. Genera features en tiempo real
     b. Modelo predice P(up)
     c. Estrategia decide: BUY_YES, BUY_NO o SKIP
     d. Paper wallet abre posicion
     e. Telegram notifica la decision
  4. Cada mercado resuelto:
     a. Paper wallet cierra posicion y calcula PnL
     b. Telegram notifica el resultado
  5. Estadisticas cada 60 segundos

Uso:
  python main.py

Para detener: Ctrl+C
"""

import asyncio
import json
import signal
import sys
import os
import time
from datetime import datetime, timezone, timedelta

# Cargar .env antes de cualquier import que lea variables de entorno
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import pandas as pd
from loguru import logger

from data import storage
from data import rest_client as rest
from data.websocket_client import BotState, run_pipeline
from strategy.engine import StrategyEngine
from execution.paper_wallet import PaperWallet
from execution.telegram_bot import (
    TelegramNotifier, set_refs, start_telegram_polling
)


# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------

_TZ_LIMA = timezone(timedelta(hours=-5))

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "8619893862:AAFeqiKfE__P-3Eu9Sfw8cz-Ys5WgF8cwKU")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "1435277461")

INITIAL_CAPITAL     = float(os.environ.get("BOT_INITIAL_CAPITAL", "1000"))
MIN_CONFIDENCE      = float(os.environ.get("BOT_MIN_CONFIDENCE", "0.55"))
MAX_RISK_PER_TRADE  = float(os.environ.get("BOT_MAX_RISK", "0.05"))
KELLY_FRACTION      = float(os.environ.get("BOT_KELLY_FRACTION", "0.35"))

MARKET_CHECK_INTERVAL  = 60
RESOLVED_POLL_INTERVAL = 60
STATS_INTERVAL         = 60


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logger.remove()
logger.add(
    sys.stdout, level="INFO",
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    colorize=True
)
logger.add(
    os.path.join(LOG_DIR, "bot_{time:YYYY-MM-DD}.log"),
    level="DEBUG", rotation="00:00", retention="14 days",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{line} | {message}"
)


# ---------------------------------------------------------------------------
# Componentes globales (se inicializan en main())
# ---------------------------------------------------------------------------

notifier: TelegramNotifier = None
wallet: PaperWallet = None
engine: StrategyEngine = None
_last_decision_market: str = ""  # para no decidir dos veces el mismo mercado


# ---------------------------------------------------------------------------
# Tarea: discovery + decision de trading
# ---------------------------------------------------------------------------

async def market_discovery_and_trade_loop(state: BotState) -> None:
    """
    Cada 60s busca si hay un nuevo mercado BTC 5-min.
    Cuando detecta uno nuevo:
      1. Actualiza el estado
      2. Ejecuta el ciclo de decision del strategy engine
      3. Si la decision es operar, abre posicion en paper wallet
      4. Notifica por Telegram
    """
    global _last_decision_market

    logger.info("Discovery + Trade loop iniciado")

    while state.running:
        try:
            market = rest.get_active_btc_5m_market()

            if market:
                market_id = market["market_id"]
                yes_id    = market["asset_id_yes"]
                no_id     = market["asset_id_no"]
                slug      = market.get("slug", "")
                question  = market.get("question", "")

                if market_id != state.active_market_id:
                    # Nuevo mercado detectado
                    storage.upsert_active_market(
                        market_id=market_id,
                        asset_id_yes=yes_id,
                        asset_id_no=no_id,
                        question=question,
                        slug=slug,
                        description=market.get("description", "")
                    )
                    state.update_market(market_id, yes_id, no_id, slug=slug)
                    logger.success(f"Nuevo mercado: {question} | {slug}")

                    # --- Ciclo de decision ---
                    if market_id != _last_decision_market and engine is not None:
                        _last_decision_market = market_id
                        await _run_trading_decision(state, market)

        except Exception as e:
            logger.error(f"Error en discovery/trade loop: {e}", exc_info=True)
            if notifier:
                await notifier.notify_error(f"Discovery loop: {e}")

        await asyncio.sleep(MARKET_CHECK_INTERVAL)


async def _run_trading_decision(state: BotState, market: dict) -> None:
    """Ejecuta el ciclo completo de decision para un mercado nuevo."""
    market_id = market["market_id"]
    slug      = market.get("slug", "")
    yes_id    = market["asset_id_yes"]
    no_id     = market["asset_id_no"]
    question  = market.get("question", "")

    # Notificar nuevo mercado
    if notifier:
        await notifier.notify_new_market(slug, question)

    # Obtener datos para features
    conn = storage.get_connection()
    try:
        # Ticks BTC recientes (ultima hora)
        cutoff_ms = int(time.time() * 1000) - 3600_000
        btc_rows = storage._fetchall(
            conn.cursor(),
            f"SELECT ts, price FROM btc_prices WHERE source='chainlink' AND ts > {storage.PH} ORDER BY ts ASC",
            (cutoff_ms,)
        )
        btc_ticks = pd.DataFrame(btc_rows) if btc_rows else None

        # Ultimo snapshot del order book
        snap_row = storage._fetchone(
            conn.cursor(),
            "SELECT bids, asks FROM orderbook_snapshots ORDER BY ts DESC LIMIT 1"
        )
        bids = json.loads(snap_row["bids"]) if snap_row else []
        asks = json.loads(snap_row["asks"]) if snap_row else []

        # Trades recientes
        trade_rows = storage._fetchall(
            conn.cursor(),
            "SELECT ts, price, size, side FROM last_trades ORDER BY ts DESC LIMIT 100"
        )
        trades_df = pd.DataFrame(trade_rows) if trade_rows else None

        # Outcomes recientes
        outcome_rows = storage._fetchall(
            conn.cursor(),
            "SELECT winning_outcome FROM resolved_markets ORDER BY ts_resolved DESC LIMIT 15"
        )
        recent_outcomes = [r["winning_outcome"] for r in outcome_rows][::-1]

        # Share price: usar ob_midpoint del ultimo snapshot
        if bids and asks:
            best_bid = float(bids[0].get("price", 0.5)) if bids else 0.5
            best_ask = float(asks[0].get("price", 0.5)) if asks else 0.5
            share_price = (best_bid + best_ask) / 2
        else:
            share_price = 0.5

    finally:
        conn.close()

    # Ejecutar decision
    decision = engine.decide(
        market_id=market_id,
        slug=slug,
        asset_id_yes=yes_id,
        asset_id_no=no_id,
        btc_ticks=btc_ticks,
        latest_snapshot_bids=bids,
        latest_snapshot_asks=asks,
        recent_trades=trades_df,
        share_price_yes=share_price,
        share_price_yes_prev=None,
        recent_outcomes=recent_outcomes,
    )

    # Notificar decision por Telegram
    if notifier:
        await notifier.notify_decision(decision.to_dict())

    # Si decide operar, abrir posicion en paper wallet
    if decision.action != "SKIP" and decision.usdc_amount > 0:
        wallet.open_position(
            market_id=market_id,
            slug=slug,
            action=decision.action,
            token_id=decision.token_id,
            buy_price=decision.target_price,
            usdc_amount=decision.usdc_amount,
            n_shares=decision.n_shares,
            fee=decision.fee_estimated,
            prob_up=decision.prob_up,
            confidence=decision.confidence,
        )


# ---------------------------------------------------------------------------
# Tarea: poller de mercados resueltos + cierre de posiciones
# ---------------------------------------------------------------------------

async def resolved_markets_poller(state: BotState) -> None:
    """
    Cada minuto detecta mercados resueltos via REST.
    Si hay posicion abierta en ese mercado, la cierra y notifica.
    """
    logger.info("Resolved poller iniciado")
    await asyncio.sleep(30)

    already_resolved: set[str] = set()
    conn = storage.get_connection()
    try:
        rows = storage._fetchall(conn.cursor(), "SELECT market_id FROM resolved_markets")
        already_resolved = {row["market_id"] for row in rows}
    finally:
        conn.close()

    while state.running:
        try:
            resolved_list = rest.get_recent_resolved_btc_5m_markets(lookback_intervals=12)

            for r in resolved_list:
                market_id = r["market_id"]
                if market_id in already_resolved:
                    continue

                ts_interval = r.get("ts_interval_start", 0)
                ts_open_ms  = ts_interval * 1000
                ts_close_ms = (ts_interval + 300) * 1000

                btc_open = state.get_open_price(market_id)
                if btc_open is None:
                    btc_open = storage.get_btc_price_at(ts_open_ms, source="chainlink")
                if btc_open is None:
                    btc_open = storage.get_btc_price_at(ts_open_ms, source="binance")

                btc_close = storage.get_btc_price_at(ts_close_ms, source="chainlink")
                if btc_close is None:
                    btc_close = storage.get_btc_price_at(ts_close_ms, source="binance")
                if btc_close is None:
                    btc_close = state.last_btc_price_chainlink or state.last_btc_price_binance

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

                direction = ""
                if btc_open and btc_close:
                    direction = "UP" if btc_close > btc_open else "DOWN"

                logger.success(
                    f"RESUELTO [{r['winning_outcome']}] {r.get('slug','')} "
                    f"| BTC ${btc_open or 0:,.2f} -> ${btc_close or 0:,.2f} ({direction})"
                )

                # --- Cerrar posicion en paper wallet ---
                trade = wallet.resolve_position(market_id, r["winning_outcome"])
                if trade:
                    engine.update_capital(trade.pnl)
                    balance = wallet.get_balance()
                    if notifier:
                        await notifier.notify_resolution(
                            {
                                "slug": trade.slug,
                                "action": trade.action,
                                "won": trade.won,
                                "pnl": trade.pnl,
                                "pnl_pct": trade.pnl_pct,
                                "outcome": trade.winning_outcome,
                            },
                            balance
                        )

        except Exception as e:
            logger.error(f"Error en resolved poller: {e}", exc_info=True)

        await asyncio.sleep(RESOLVED_POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Tarea: estadisticas periodicas
# ---------------------------------------------------------------------------

async def stats_loop(state: BotState) -> None:
    """Cada minuto imprime stats de DB + wallet."""
    while state.running:
        await asyncio.sleep(STATS_INTERVAL)
        try:
            db_stats = storage.get_db_stats()
            balance = wallet.get_balance() if wallet else {}

            price_str = f"${state.last_btc_price_binance:,.2f}" \
                        if state.last_btc_price_binance else "N/A"

            logger.info(
                f"--- STATS [{datetime.now(_TZ_LIMA).strftime('%H:%M:%S Lima')}] ---\n"
                f"  BTC: {price_str}\n"
                f"  DB: prices={db_stats.get('btc_prices',0):,} | "
                f"ob={db_stats.get('orderbook_snapshots',0):,} | "
                f"trades={db_stats.get('last_trades',0):,} | "
                f"resolved={db_stats.get('resolved_markets',0):,}\n"
                f"  Wallet: ${balance.get('equity_total', 0):.2f} | "
                f"PnL: ${balance.get('pnl_total', 0):+.2f} ({balance.get('pnl_total_pct', 0):+.1f}%) | "
                f"WR: {balance.get('win_rate', 0):.0%} "
                f"({balance.get('wins', 0)}W/{balance.get('losses', 0)}L) | "
                f"Open: {balance.get('posiciones_abiertas', 0)}"
            )
        except Exception as e:
            logger.error(f"Error en stats loop: {e}")


# ---------------------------------------------------------------------------
# Arranque principal
# ---------------------------------------------------------------------------

async def main() -> None:
    global notifier, wallet, engine

    logger.info("=" * 60)
    logger.info("  BOT CRIPTO — Polymarket BTC 5-min Predictor")
    logger.info("=" * 60)

    # 1. Inicializar DB
    storage.init_db()

    # 2. Verificar APIs
    logger.info("Verificando conectividad...")
    clob_ok  = rest.ping_clob()
    gamma_ok = rest.ping_gamma()
    logger.info(f"CLOB: {'OK' if clob_ok else 'FAIL'} | Gamma: {'OK' if gamma_ok else 'FAIL'}")

    # 3. Inicializar componentes
    wallet = PaperWallet(initial_capital=INITIAL_CAPITAL)
    engine = StrategyEngine(
        capital=INITIAL_CAPITAL,
        paper_mode=True,
        min_confidence=MIN_CONFIDENCE,
        max_risk_per_trade=MAX_RISK_PER_TRADE,
        kelly_fraction=KELLY_FRACTION,
    )
    notifier = TelegramNotifier(token=TELEGRAM_TOKEN, chat_id=TELEGRAM_CHAT_ID)

    # Pasar referencias al modulo de telegram para los comandos
    set_refs(wallet, engine, notifier)

    # 4. Estado compartido
    state = BotState()

    # 5. Primer discovery
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
            market["market_id"], market["asset_id_yes"],
            market["asset_id_no"], slug=market.get("slug", "")
        )

    # 6. Iniciar Telegram polling (comandos)
    telegram_app = await start_telegram_polling(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)

    # 7. Notificar startup
    collect_str = "ACTIVA" if storage.DATA_COLLECTION_ENABLED else "DESACTIVADA"
    logger.info(f"Recoleccion de datos: {collect_str}")

    if notifier:
        await notifier.notify_startup({
            "capital": INITIAL_CAPITAL,
            "model_loaded": engine.predictor.is_loaded(),
            "paper_mode": True,
            "db_backend": "PostgreSQL" if storage.USE_POSTGRES else "SQLite",
            "data_collection": storage.DATA_COLLECTION_ENABLED,
        })

    # 8. Lanzar tareas
    tasks = [
        asyncio.create_task(run_pipeline(state),                    name="websockets"),
        asyncio.create_task(market_discovery_and_trade_loop(state), name="discovery_trade"),
        asyncio.create_task(resolved_markets_poller(state),         name="resolved_poller"),
        asyncio.create_task(stats_loop(state),                      name="stats"),
    ]

    logger.success("Bot corriendo. Ctrl+C para detener.")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        state.stop()
        for task in tasks:
            task.cancel()
        if telegram_app:
            await telegram_app.updater.stop()
            await telegram_app.stop()
            await telegram_app.shutdown()
        logger.info("Bot detenido correctamente")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _handle_sigint(loop: asyncio.AbstractEventLoop) -> None:
    logger.info("Ctrl+C recibido, deteniendo...")
    for task in asyncio.all_tasks(loop):
        task.cancel()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGINT,  lambda: _handle_sigint(loop))
        loop.add_signal_handler(signal.SIGTERM, lambda: _handle_sigint(loop))

    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Detenido por el usuario")
    finally:
        loop.close()
