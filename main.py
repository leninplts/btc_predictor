"""
main.py
-------
Orquestador principal del bot de trading BTC en Polymarket.

Flujo completo:
  1. Inicializa DB, verifica APIs
  2. Inicializa Polymarket CLOB client (si hay credenciales)
  3. Lanza WebSockets (RTDS + Market Channel)
  4. Lanza Heartbeat manager (para limit orders en modo live)
  5. Cada nuevo mercado BTC 5-min:
     a. Genera features en tiempo real
     b. Modelo predice P(up)
     c. Estrategia decide: BUY_YES, BUY_NO o SKIP
     d. Paper wallet abre posicion (SIEMPRE, tracking)
     e. Si modo LIVE: envia orden real via order_manager
  6. Cada mercado resuelto:
     a. Paper wallet cierra posicion y calcula PnL
     b. Safety manager verifica daily loss limit
  7. Estadisticas cada 60 segundos

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
from execution.clob_client import PolymarketClient
from execution.order_manager import OrderManager
from execution.heartbeat import HeartbeatManager
from execution.safety import SafetyManager
from execution.fill_simulator import FillSimulator


# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------

_TZ_LIMA = timezone(timedelta(hours=-5))

INITIAL_CAPITAL     = float(os.environ.get("BOT_INITIAL_CAPITAL", "1000"))
MIN_CONFIDENCE      = float(os.environ.get("BOT_MIN_CONFIDENCE", "0.55"))
MAX_RISK_PER_TRADE  = float(os.environ.get("BOT_MAX_RISK", "0.05"))
KELLY_FRACTION      = float(os.environ.get("BOT_KELLY_FRACTION", "0.35"))
DAILY_LOSS_LIMIT    = float(os.environ.get("BOT_DAILY_LOSS_LIMIT_PCT", "10"))

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

wallet: PaperWallet = None
engine: StrategyEngine = None
poly_client: PolymarketClient = None
order_manager: OrderManager = None
heartbeat: HeartbeatManager = None
safety: SafetyManager = None
fill_sim: FillSimulator = None
_last_decision_market: str = ""


# ---------------------------------------------------------------------------
# Tarea: discovery + decision de trading
# ---------------------------------------------------------------------------

async def market_discovery_and_trade_loop(state: BotState) -> None:
    """
    Cada 60s busca si hay un nuevo mercado BTC 5-min.
    Cuando detecta uno nuevo:
      1. Actualiza el estado
      2. Ejecuta el ciclo de decision del strategy engine
      3. Paper wallet abre posicion (SIEMPRE)
      4. Si modo LIVE: envia orden real
    """
    global _last_decision_market

    logger.info("Discovery + Trade loop iniciado")

    while state.running:
        try:
            market = await asyncio.to_thread(rest.get_active_btc_5m_market)

            if market:
                market_id = market["market_id"]
                yes_id    = market["asset_id_yes"]
                no_id     = market["asset_id_no"]
                slug      = market.get("slug", "")
                question  = market.get("question", "")

                if market_id != state.active_market_id:
                    await asyncio.to_thread(
                        lambda: storage.upsert_active_market(
                            market_id=market_id,
                            asset_id_yes=yes_id,
                            asset_id_no=no_id,
                            question=question,
                            slug=slug,
                            description=market.get("description", ""),
                        )
                    )
                    state.update_market(market_id, yes_id, no_id, slug=slug)
                    logger.success(f"Nuevo mercado: {question} | {slug}")

                    if market_id != _last_decision_market and engine is not None:
                        _last_decision_market = market_id
                        await _run_trading_decision(state, market)

        except Exception as e:
            logger.error(f"Error en discovery/trade loop: {e}", exc_info=True)

        await asyncio.sleep(MARKET_CHECK_INTERVAL)


def _fetch_trading_data_sync() -> dict:
    """
    Obtiene datos para features desde la DB (sincrono).
    Se ejecuta en un thread separado para no bloquear el event loop.
    """
    conn = storage.get_connection()
    try:
        cutoff_ms = int(time.time() * 1000) - 3600_000
        btc_rows = storage._fetchall(
            conn.cursor(),
            f"SELECT ts, price FROM btc_prices WHERE source='chainlink' AND ts > {storage.PH} ORDER BY ts ASC",
            (cutoff_ms,)
        )
        btc_ticks = pd.DataFrame(btc_rows) if btc_rows else None

        snap_row = storage._fetchone(
            conn.cursor(),
            "SELECT bids, asks FROM orderbook_snapshots ORDER BY ts DESC LIMIT 1"
        )
        bids = json.loads(snap_row["bids"]) if snap_row else []
        asks = json.loads(snap_row["asks"]) if snap_row else []

        trade_rows = storage._fetchall(
            conn.cursor(),
            "SELECT ts, price, size, side FROM last_trades ORDER BY ts DESC LIMIT 100"
        )
        trades_df = pd.DataFrame(trade_rows) if trade_rows else None

        outcome_rows = storage._fetchall(
            conn.cursor(),
            "SELECT winning_outcome FROM resolved_markets ORDER BY ts_resolved DESC LIMIT 15"
        )
        recent_outcomes = [r["winning_outcome"] for r in outcome_rows][::-1]

        if bids and asks:
            best_bid = float(bids[0].get("price", 0.5)) if bids else 0.5
            best_ask = float(asks[0].get("price", 0.5)) if asks else 0.5
            share_price = (best_bid + best_ask) / 2
        else:
            share_price = 0.5

        return {
            "btc_ticks": btc_ticks,
            "bids": bids,
            "asks": asks,
            "trades_df": trades_df,
            "recent_outcomes": recent_outcomes,
            "share_price": share_price,
        }
    finally:
        conn.close()


async def _run_trading_decision(state: BotState, market: dict) -> None:
    """Ejecuta el ciclo completo de decision para un mercado nuevo."""
    market_id = market["market_id"]
    slug      = market.get("slug", "")
    yes_id    = market["asset_id_yes"]
    no_id     = market["asset_id_no"]

    try:
        # Obtener datos para features (en thread separado, no bloquea event loop)
        data = await asyncio.to_thread(_fetch_trading_data_sync)

        # Ejecutar decision
        decision = engine.decide(
            market_id=market_id,
            slug=slug,
            asset_id_yes=yes_id,
            asset_id_no=no_id,
            btc_ticks=data["btc_ticks"],
            latest_snapshot_bids=data["bids"],
            latest_snapshot_asks=data["asks"],
            recent_trades=data["trades_df"],
            share_price_yes=data["share_price"],
            share_price_yes_prev=None,
            recent_outcomes=data["recent_outcomes"],
        )

        # --- PAPER WALLET: siempre (tracking) con fill simulado ---
        if decision.action != "SKIP" and decision.usdc_amount > 0:
            # Simular fill contra el order book real
            sim = fill_sim.simulate_buy(
                action=decision.action,
                target_price=decision.target_price,
                n_shares=decision.n_shares,
                bids=data["bids"],
                asks=data["asks"],
            )

            if sim.filled:
                # Usar datos del fill simulado (precio real, fee real)
                wallet.open_position(
                    market_id=market_id,
                    slug=slug,
                    action=decision.action,
                    token_id=decision.token_id,
                    buy_price=sim.fill_price,
                    usdc_amount=sim.shares_filled * sim.fill_price,
                    n_shares=sim.shares_filled,
                    fee=sim.fee,
                    prob_up=decision.prob_up,
                    confidence=decision.confidence,
                    simulated_fill=True,
                    slippage=sim.slippage,
                    target_price=decision.target_price,
                )
            else:
                logger.info(
                    f"PAPER SKIP (no fill) | {decision.action} {slug} | "
                    f"{sim.reason}"
                )

        # --- LIVE ORDER: solo si modo live activo y no pausado ---
        if decision.action != "SKIP" and decision.usdc_amount > 0:
            if engine.should_execute_live() and order_manager:
                if safety and safety.is_circuit_breaker_active():
                    logger.warning("Circuit breaker activo — no se envia orden real")
                else:
                    logger.info(f"LIVE: enviando orden real | {decision.action} {slug}")
                    order_result = await order_manager.place_order(
                        token_id=decision.token_id,
                        price=decision.target_price,
                        size=decision.n_shares,
                        order_type=decision.order_type,
                        usdc_amount=decision.usdc_amount,
                    )

    except Exception as e:
        logger.error(f"Error en trading decision para {slug}: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Tarea: poller de mercados resueltos + cierre de posiciones
# ---------------------------------------------------------------------------

async def resolved_markets_poller(state: BotState) -> None:
    """
    Cada minuto detecta mercados resueltos via REST.
    Si hay posicion abierta en ese mercado, la cierra.
    """
    logger.info("Resolved poller iniciado")
    await asyncio.sleep(30)

    def _load_already_resolved() -> set:
        conn = storage.get_connection()
        try:
            rows = storage._fetchall(conn.cursor(), "SELECT market_id FROM resolved_markets")
            return {row["market_id"] for row in rows}
        finally:
            conn.close()

    already_resolved: set[str] = await asyncio.to_thread(_load_already_resolved)

    while state.running:
        try:
            resolved_list = await asyncio.to_thread(
                rest.get_recent_resolved_btc_5m_markets, 12
            )

            for r in resolved_list:
                try:
                    market_id = r["market_id"]
                    if market_id in already_resolved:
                        continue

                    ts_interval = r.get("ts_interval_start", 0)
                    ts_open_ms  = ts_interval * 1000
                    ts_close_ms = (ts_interval + 300) * 1000

                    btc_open = state.get_open_price(market_id)
                    if btc_open is None:
                        btc_open = await asyncio.to_thread(
                            storage.get_btc_price_at, ts_open_ms, "chainlink"
                        )
                    if btc_open is None:
                        btc_open = await asyncio.to_thread(
                            storage.get_btc_price_at, ts_open_ms, "binance"
                        )

                    btc_close = await asyncio.to_thread(
                        storage.get_btc_price_at, ts_close_ms, "chainlink"
                    )
                    if btc_close is None:
                        btc_close = await asyncio.to_thread(
                            storage.get_btc_price_at, ts_close_ms, "binance"
                        )
                    if btc_close is None:
                        btc_close = state.last_btc_price_chainlink or state.last_btc_price_binance

                    await asyncio.to_thread(
                        lambda: storage.insert_resolved_market(
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
                            ts_resolved=ts_close_ms,
                        )
                    )
                    already_resolved.add(market_id)

                    direction = ""
                    if btc_open and btc_close:
                        direction = "UP" if btc_close > btc_open else "DOWN"

                    logger.success(
                        f"RESUELTO [{r['winning_outcome']}] {r.get('slug','')} "
                        f"| BTC ${btc_open or 0:,.2f} -> ${btc_close or 0:,.2f} ({direction})"
                    )

                    # --- Cerrar posicion en paper wallet (SIEMPRE) ---
                    trade = wallet.resolve_position(market_id, r["winning_outcome"])
                    if trade:
                        engine.update_capital(trade.pnl)

                        # --- Safety check (solo si hubo trade Y modo live) ---
                        if safety and not engine.paper_mode:
                            safety_result = safety.record_trade(trade.pnl, trade.won)
                            if safety_result["limit_triggered"]:
                                engine.set_paper_mode()
                                if order_manager:
                                    await order_manager.cancel_all_orders()

                except Exception as e:
                    slug = r.get("slug", r.get("market_id", "?"))
                    logger.error(f"Error procesando mercado resuelto {slug}: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Error en resolved poller: {e}", exc_info=True)

        await asyncio.sleep(RESOLVED_POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Tarea: estadisticas periodicas
# ---------------------------------------------------------------------------

async def stats_loop(state: BotState) -> None:
    """Cada minuto imprime stats de DB + wallet + limpieza de payouts."""
    _last_balance_check: float = 0.0

    while state.running:
        await asyncio.sleep(STATS_INTERVAL)
        try:
            db_stats = await asyncio.to_thread(storage.get_db_stats)
            balance = wallet.get_balance() if wallet else {}

            price_str = f"${state.last_btc_price_binance:,.2f}" \
                        if state.last_btc_price_binance else "N/A"

            mode = engine.get_mode_str() if engine else "?"

            # Verificar si payouts pendientes ya se procesaron
            pending_str = ""
            if wallet and poly_client and poly_client.is_ready():
                pending = wallet.get_pending_payouts()
                if pending["count"] > 0:
                    current_usdc = await asyncio.to_thread(poly_client.get_usdc_balance)
                    if current_usdc > _last_balance_check and _last_balance_check > 0:
                        diff = current_usdc - _last_balance_check
                        if diff >= pending["total"] * 0.8:
                            wallet.clear_all_pending_payouts()
                            logger.info(
                                f"Payouts pendientes acreditados: "
                                f"balance subio ${diff:+.2f} a ${current_usdc:.2f}"
                            )
                    _last_balance_check = current_usdc
                    pending = wallet.get_pending_payouts()
                    if pending["count"] > 0:
                        pending_str = f" | Payout pendiente: ~${pending['total']:.2f}"

            logger.info(
                f"--- STATS [{datetime.now(_TZ_LIMA).strftime('%H:%M:%S Lima')}] [{mode}] ---\n"
                f"  BTC: {price_str}\n"
                f"  DB: prices={db_stats.get('btc_prices',0):,} | "
                f"ob={db_stats.get('orderbook_snapshots',0):,} | "
                f"trades={db_stats.get('last_trades',0):,} | "
                f"resolved={db_stats.get('resolved_markets',0):,}\n"
                f"  Demo: ${balance.get('equity_total', 0):.2f} | "
                f"PnL: ${balance.get('pnl_total', 0):+.2f} ({balance.get('pnl_total_pct', 0):+.1f}%) | "
                f"WR: {balance.get('win_rate', 0):.0%} "
                f"({balance.get('wins', 0)}W/{balance.get('losses', 0)}L) | "
                f"Open: {balance.get('posiciones_abiertas', 0)}{pending_str}"
            )
        except Exception as e:
            logger.error(f"Error en stats loop: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Arranque principal
# ---------------------------------------------------------------------------

async def main() -> None:
    global wallet, engine, poly_client, order_manager, heartbeat, safety, fill_sim

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

    # 3. Inicializar componentes base
    wallet = PaperWallet(initial_capital=INITIAL_CAPITAL)
    engine = StrategyEngine(
        capital=INITIAL_CAPITAL,
        paper_mode=True,
        min_confidence=MIN_CONFIDENCE,
        max_risk_per_trade=MAX_RISK_PER_TRADE,
        kelly_fraction=KELLY_FRACTION,
    )
    safety = SafetyManager(
        daily_loss_limit_pct=DAILY_LOSS_LIMIT,
        initial_capital=INITIAL_CAPITAL,
    )
    fill_sim = FillSimulator()

    # 4. Inicializar Polymarket CLOB client (opcional — solo si hay credenciales)
    poly_client = PolymarketClient()
    poly_ready = poly_client.initialize()

    if poly_ready:
        poly_client.ensure_allowances()
        heartbeat = HeartbeatManager(poly_client)
        order_manager = OrderManager(poly_client, heartbeat)

        usdc = poly_client.get_usdc_balance()
        logger.success(f"Polymarket LIVE disponible | USDC: ${usdc:.2f}")

        if usdc > 0:
            safety.update_reference_capital(usdc)
    else:
        logger.info("Polymarket LIVE no disponible (sin credenciales). Solo paper mode.")

    # 5. Estado compartido
    state = BotState()

    # 6. Primer discovery
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

    # 7. Notificar startup
    collect_str = "ACTIVA" if storage.DATA_COLLECTION_ENABLED else "DESACTIVADA"
    logger.info(f"Recoleccion de datos: {collect_str}")
    logger.info(
        f"Config: capital=${INITIAL_CAPITAL} | model={'loaded' if engine.predictor.is_loaded() else 'NO'} | "
        f"mode={engine.get_mode_str()} | db={'PostgreSQL' if storage.USE_POSTGRES else 'SQLite'} | "
        f"poly={'ready' if poly_ready else 'off'}"
    )

    # 8. Lanzar tareas
    tasks = [
        asyncio.create_task(run_pipeline(state),                    name="websockets"),
        asyncio.create_task(market_discovery_and_trade_loop(state), name="discovery_trade"),
        asyncio.create_task(resolved_markets_poller(state),         name="resolved_poller"),
        asyncio.create_task(stats_loop(state),                      name="stats"),
    ]

    if heartbeat:
        tasks.append(asyncio.create_task(heartbeat.start(), name="heartbeat"))

    logger.success("Bot corriendo. Ctrl+C para detener.")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        state.stop()
        if heartbeat:
            heartbeat.stop()
        if order_manager:
            await order_manager.cancel_all_orders()
        for task in tasks:
            task.cancel()
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
