"""
execution/telegram_bot.py
-------------------------
Bot de Telegram para monitoreo y control del bot de trading.

NOTIFICACIONES AUTOMATICAS (push al chat):
  - Nuevo mercado detectado + decision del modelo
  - Resultado de mercado resuelto + PnL
  - Ordenes reales enviadas y fills (modo live)
  - Daily loss limit activado
  - Errores criticos

COMANDOS (el usuario envia al bot):
  /balance     — balance real de Polymarket (modo live)
  /demobalance — balance del paper wallet (simulado)
  /stats       — estadisticas completas del engine
  /positions   — posiciones abiertas (paper)
  /trades      — ultimos 5 trades cerrados (paper)
  /mode        — muestra modo actual (PAPER / LIVE / LIVE PAUSADO)
  /live        — activar trading real (con confirmacion)
  /paper       — volver a modo paper (con confirmacion)
  /pauselive   — pausar nuevas entradas live (posiciones abiertas siguen)
  /stop        — EMERGENCY STOP: cancela todo y pasa a paper
  /reset       — resetear paper wallet
  /help        — lista de comandos
"""

import os
import asyncio
from typing import Optional
from loguru import logger

from telegram import Bot, BotCommand, Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)


# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


# ---------------------------------------------------------------------------
# Telegram Notifier (solo envio de mensajes, sin polling)
# ---------------------------------------------------------------------------

class TelegramNotifier:
    """Envia notificaciones al chat de Telegram."""

    def __init__(self, token: str = "", chat_id: str = ""):
        self.token = token or TELEGRAM_TOKEN
        self.chat_id = chat_id or TELEGRAM_CHAT_ID
        self.bot: Optional[Bot] = None
        self.enabled = bool(self.token and self.chat_id)

        if self.enabled:
            self.bot = Bot(token=self.token)
            logger.info(f"TelegramNotifier habilitado | chat_id={self.chat_id}")
        else:
            logger.warning("TelegramNotifier deshabilitado (falta TELEGRAM_TOKEN o TELEGRAM_CHAT_ID)")

    async def send(self, message: str, parse_mode: str = "HTML") -> bool:
        """Envia un mensaje al chat. Retorna True si exito."""
        if not self.enabled or not self.bot:
            return False
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode=parse_mode,
            )
            return True
        except Exception as e:
            logger.error(f"Error enviando mensaje Telegram: {e}")
            return False

    # --- Mensajes pre-formateados ---

    async def notify_new_market(self, slug: str, question: str = "") -> None:
        msg = (
            f"<b>NUEVO MERCADO</b>\n"
            f"<code>{slug}</code>\n"
            f"{question}"
        )
        await self.send(msg)

    async def notify_decision(self, decision_dict: dict, mode: str = "PAPER") -> None:
        d = decision_dict
        action = d.get("action", "SKIP")
        mode_tag = f"[{mode}]"

        if action == "SKIP":
            msg = (
                f"<b>DECISION: SKIP</b> {mode_tag}\n"
                f"Mercado: <code>{d.get('slug', '')}</code>\n"
                f"Confianza: {d.get('confidence', 0):.1%}\n"
                f"Regimen: {d.get('regime', '?')}\n"
                f"Razon: {d.get('signal_reason', '')}"
            )
        else:
            direction = "UP" if action == "BUY_YES" else "DOWN"
            msg = (
                f"<b>DECISION: {action}</b> {mode_tag}\n"
                f"Mercado: <code>{d.get('slug', '')}</code>\n\n"
                f"Prediccion BTC: <b>{direction}</b>\n"
                f"P(UP): {d.get('prob_up', 0):.1%} | P(DOWN): {d.get('prob_down', 0):.1%}\n"
                f"Confianza: {d.get('confidence', 0):.1%}\n"
                f"Regimen: {d.get('regime', '?')}\n\n"
                f"Orden: {d.get('order_type', '')} @ ${d.get('target_price', 0):.3f}\n"
                f"Monto: ${d.get('usdc_amount', 0):.2f} ({d.get('n_shares', 0):.0f} shares)\n"
                f"Fee est: ${d.get('fee_estimated', 0):.4f}"
            )
        await self.send(msg)

    async def notify_order_sent(self, order_result: dict) -> None:
        """Notifica que se envio una orden real."""
        if order_result.get("success"):
            upgraded = " (limit->market fallback)" if order_result.get("was_upgraded") else ""
            msg = (
                f"<b>ORDEN ENVIADA</b>{upgraded}\n"
                f"Tipo: {order_result.get('order_type', '?')}\n"
                f"ID: <code>{order_result.get('order_id', '')[:20]}...</code>\n"
                f"Shares: {order_result.get('shares_filled', 0):.1f}\n"
                f"USDC: ${order_result.get('usdc_spent', 0):.2f}"
            )
        else:
            msg = (
                f"<b>ORDEN FALLIDA</b>\n"
                f"Error: {order_result.get('error', 'desconocido')}"
            )
        await self.send(msg)

    async def notify_resolution(self, trade_dict: dict, balance: dict) -> None:
        t = trade_dict
        won = t.get("won", False)
        result = "WIN" if won else "LOSS"

        msg = (
            f"<b>RESULTADO: {result}</b>\n"
            f"Mercado: <code>{t.get('slug', '')}</code>\n"
            f"Apuesta: {t.get('action', '')} | Outcome: {t.get('outcome', '')}\n"
            f"PnL: <b>${t.get('pnl', 0):+.2f}</b> ({t.get('pnl_pct', 0):+.1f}%)\n\n"
            f"Capital demo: ${balance.get('equity_total', 0):.2f}\n"
            f"PnL total: ${balance.get('pnl_total', 0):+.2f} ({balance.get('pnl_total_pct', 0):+.1f}%)\n"
            f"Win rate: {balance.get('win_rate', 0):.1%} "
            f"({balance.get('wins', 0)}W/{balance.get('losses', 0)}L)\n"
            f"Racha: {balance.get('racha', '0')}"
        )
        await self.send(msg)

    async def notify_safety_triggered(self, message: str) -> None:
        """Notifica que se activo un mecanismo de seguridad."""
        msg = f"<b>SEGURIDAD</b>\n{message}"
        await self.send(msg)

    async def notify_mode_change(self, new_mode: str, reason: str = "") -> None:
        """Notifica cambio de modo."""
        msg = f"<b>MODO: {new_mode}</b>"
        if reason:
            msg += f"\n{reason}"
        await self.send(msg)

    async def notify_error(self, error_msg: str) -> None:
        msg = f"<b>ERROR</b>\n<code>{error_msg[:500]}</code>"
        await self.send(msg)

    async def notify_startup(self, stats: dict) -> None:
        data_status = "ACTIVA" if stats.get("data_collection") else "DESACTIVADA"
        msg = (
            f"<b>BOT INICIADO</b>\n\n"
            f"Capital demo: ${stats.get('capital', 0):.2f} USDC\n"
            f"Modelo: {'Cargado' if stats.get('model_loaded') else 'NO cargado'}\n"
            f"Modo: {stats.get('mode', 'PAPER')}\n"
            f"DB: {stats.get('db_backend', '?')}\n"
            f"Data collection: {data_status}\n"
            f"Polymarket live: {'Listo' if stats.get('poly_ready') else 'No disponible'}"
        )
        await self.send(msg)


# ---------------------------------------------------------------------------
# Telegram Command Handlers
# ---------------------------------------------------------------------------

# Referencias globales — se setean desde main.py via set_refs()
_wallet_ref = None
_engine_ref = None
_notifier_ref = None
_poly_client_ref = None
_order_manager_ref = None
_safety_ref = None

# Estado de confirmacion pendiente
_pending_confirmation: dict = {}


def _is_authorized(update: Update) -> bool:
    """Verifica que el mensaje venga del chat autorizado."""
    if not TELEGRAM_CHAT_ID:
        return True  # si no hay chat_id configurado, permitir todo
    return str(update.effective_chat.id) == TELEGRAM_CHAT_ID


def set_refs(wallet, engine, notifier, poly_client=None, order_manager=None, safety=None):
    """main.py llama esto para pasar las referencias."""
    global _wallet_ref, _engine_ref, _notifier_ref
    global _poly_client_ref, _order_manager_ref, _safety_ref
    _wallet_ref = wallet
    _engine_ref = engine
    _notifier_ref = notifier
    _poly_client_ref = poly_client
    _order_manager_ref = order_manager
    _safety_ref = safety


# --- /balance --- Balance real de Polymarket
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Balance real de Polymarket (USDC en la cuenta)."""
    if not _is_authorized(update):
        return
    if _poly_client_ref and _poly_client_ref.is_ready():
        usdc = _poly_client_ref.get_usdc_balance()
        mode = _engine_ref.get_mode_str() if _engine_ref else "?"
        daily = _safety_ref.get_daily_stats() if _safety_ref else {}

        msg = (
            f"<b>BALANCE REAL (Polymarket)</b>\n\n"
            f"USDC disponible: <b>${usdc:.2f}</b>\n"
            f"Modo: {mode}\n\n"
            f"<b>Hoy:</b>\n"
            f"PnL: ${daily.get('daily_pnl', 0):+.2f} ({daily.get('daily_pnl_pct', 0):+.1f}%)\n"
            f"Trades: {daily.get('daily_trades', 0)} "
            f"({daily.get('daily_wins', 0)}W/{daily.get('daily_losses', 0)}L)"
        )
    else:
        msg = (
            "<b>BALANCE REAL</b>\n\n"
            "Polymarket client no disponible.\n"
            "Configura POLY_PRIVATE_KEY y POLY_FUNDER_ADDRESS."
        )
    await update.message.reply_text(msg, parse_mode="HTML")


# --- /demobalance --- Balance del paper wallet
async def cmd_demobalance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Balance de la wallet demo (paper trading)."""
    if not _is_authorized(update):
        return
    if _wallet_ref is None:
        await update.message.reply_text("Wallet no inicializada")
        return

    b = _wallet_ref.get_balance()
    msg = (
        f"<b>BALANCE DEMO (Paper)</b>\n\n"
        f"Capital libre:    ${b['capital_libre']:.2f}\n"
        f"Capital invertido: ${b['capital_invertido']:.2f}\n"
        f"Equity total:     <b>${b['equity_total']:.2f}</b>\n\n"
        f"PnL total: <b>${b['pnl_total']:+.2f}</b> ({b['pnl_total_pct']:+.1f}%)\n"
        f"Fees pagados: ${b['fees_total']:.4f}\n\n"
        f"Trades: {b['trades_totales']} ({b['wins']}W / {b['losses']}L)\n"
        f"Win rate: <b>{b['win_rate']:.1%}</b>\n"
        f"Racha: {b['racha']}\n"
        f"Posiciones abiertas: {b['posiciones_abiertas']}"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


# --- /stats ---
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    if _wallet_ref is None or _engine_ref is None:
        await update.message.reply_text("Bot no inicializado completamente")
        return

    b = _wallet_ref.get_balance()
    e = _engine_ref.get_stats()
    daily = _safety_ref.get_daily_stats() if _safety_ref else {}

    msg = (
        f"<b>ESTADISTICAS</b>\n\n"
        f"<b>Modo:</b> {e.get('mode', '?')}\n\n"
        f"<b>Demo wallet:</b>\n"
        f"  Capital inicial: ${b['capital_inicial']:.2f}\n"
        f"  Equity actual:   ${b['equity_total']:.2f}\n"
        f"  PnL: ${b['pnl_total']:+.2f} ({b['pnl_total_pct']:+.1f}%)\n"
        f"  Mejor trade: ${b['mejor_trade_pnl']:+.2f}\n"
        f"  Peor trade:  ${b['peor_trade_pnl']:+.2f}\n\n"
        f"<b>Engine:</b>\n"
        f"  Decisiones: {e['total_decisions']}\n"
        f"  Trades: {e['total_trades']}\n"
        f"  Skips: {e['total_skips']}\n"
        f"  Modelo: {'Cargado' if e['model_loaded'] else 'NO'}\n\n"
        f"<b>Hoy (live):</b>\n"
        f"  PnL: ${daily.get('daily_pnl', 0):+.2f} ({daily.get('daily_pnl_pct', 0):+.1f}%)\n"
        f"  Trades: {daily.get('daily_trades', 0)}\n"
        f"  Loss limit: {'-' + str(daily.get('loss_limit_pct', 0)) + '%' if daily else 'N/A'}\n\n"
        f"<b>Efectividad demo:</b>\n"
        f"  Win rate: {b['win_rate']:.1%}\n"
        f"  {b['wins']}W / {b['losses']}L\n"
        f"  Racha actual: {b['racha']}"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


# --- /positions ---
async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    if _wallet_ref is None:
        await update.message.reply_text("Wallet no inicializada")
        return

    positions = _wallet_ref.get_open_positions_summary()
    if not positions:
        await update.message.reply_text("Sin posiciones abiertas (demo)")
        return

    lines = ["<b>POSICIONES ABIERTAS (Demo)</b>\n"]
    for p in positions:
        lines.append(
            f"  {p['action']} <code>{p['slug']}</code>\n"
            f"    {p['n_shares']} shares @ ${p['buy_price']:.3f} = ${p['usdc']:.2f}\n"
            f"    Conf: {p['confidence']:.1%}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# --- /trades ---
async def cmd_trades(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    if _wallet_ref is None:
        await update.message.reply_text("Wallet no inicializada")
        return

    trades = _wallet_ref.get_recent_trades(n=5)
    if not trades:
        await update.message.reply_text("Sin trades cerrados aun")
        return

    lines = ["<b>ULTIMOS TRADES (Demo)</b>\n"]
    for t in reversed(trades):
        icon = "W" if t['won'] else "L"
        lines.append(
            f"  [{icon}] {t['action']} | {t['outcome']} | "
            f"${t['pnl']:+.2f} ({t['pnl_pct']:+.1f}%) | "
            f"conf {t['confidence']:.1%}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# --- /mode ---
async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra el modo actual."""
    if not _is_authorized(update):
        return
    if _engine_ref is None:
        await update.message.reply_text("Engine no inicializado")
        return

    mode = _engine_ref.get_mode_str()
    poly_ready = _poly_client_ref.is_ready() if _poly_client_ref else False

    msg = (
        f"<b>MODO ACTUAL: {mode}</b>\n\n"
        f"Polymarket client: {'Listo' if poly_ready else 'No disponible'}\n"
        f"Paper wallet: Siempre activa\n"
    )
    if _safety_ref:
        daily = _safety_ref.get_daily_stats()
        msg += (
            f"\nDaily loss limit: -{daily['loss_limit_pct']}%\n"
            f"Circuit breaker: {'ACTIVO' if daily['circuit_breaker'] else 'OK'}"
        )
    await update.message.reply_text(msg, parse_mode="HTML")


# --- /live --- Activar trading real (con confirmacion)
async def cmd_live(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    if _engine_ref is None:
        await update.message.reply_text("Engine no inicializado")
        return

    if not _poly_client_ref or not _poly_client_ref.is_ready():
        await update.message.reply_text(
            "Polymarket client no disponible.\n"
            "Configura POLY_PRIVATE_KEY y POLY_FUNDER_ADDRESS."
        )
        return

    if not _engine_ref.paper_mode and not _engine_ref.live_paused:
        await update.message.reply_text("Ya estas en modo LIVE")
        return

    # Pedir confirmacion
    usdc = _poly_client_ref.get_usdc_balance()
    chat_id = str(update.effective_chat.id)
    _pending_confirmation[chat_id] = "live"

    msg = (
        f"<b>CONFIRMAR ACTIVACION LIVE</b>\n\n"
        f"Vas a activar TRADING REAL con fondos reales.\n"
        f"USDC disponible: <b>${usdc:.2f}</b>\n\n"
        f"Escribe <b>CONFIRMAR</b> para activar.\n"
        f"Cualquier otro mensaje cancela."
    )
    await update.message.reply_text(msg, parse_mode="HTML")


# --- /paper --- Volver a paper mode (con confirmacion)
async def cmd_paper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    if _engine_ref is None:
        await update.message.reply_text("Engine no inicializado")
        return

    if _engine_ref.paper_mode:
        await update.message.reply_text("Ya estas en modo PAPER")
        return

    chat_id = str(update.effective_chat.id)
    _pending_confirmation[chat_id] = "paper"

    msg = (
        f"<b>CONFIRMAR CAMBIO A PAPER</b>\n\n"
        f"Se cancelaran las ordenes abiertas y se pasara a modo paper.\n\n"
        f"Escribe <b>CONFIRMAR</b> para cambiar.\n"
        f"Cualquier otro mensaje cancela."
    )
    await update.message.reply_text(msg, parse_mode="HTML")


# --- /pauselive --- Pausar nuevas entradas
async def cmd_pauselive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    if _engine_ref is None:
        await update.message.reply_text("Engine no inicializado")
        return

    msg = _engine_ref.pause_live()
    await update.message.reply_text(msg)


# --- /stop --- EMERGENCY STOP
async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Emergency stop: cancela ordenes + paper mode. Sin confirmacion."""
    if not _is_authorized(update):
        return
    if _engine_ref is None:
        await update.message.reply_text("Engine no inicializado")
        return

    # 1. Cancelar todas las ordenes
    cancelled = False
    if _order_manager_ref:
        cancelled = await _order_manager_ref.cancel_all_orders()

    # 2. Forzar paper mode
    _engine_ref.set_paper_mode()

    msg = (
        f"<b>EMERGENCY STOP</b>\n\n"
        f"Ordenes canceladas: {'Si' if cancelled else 'N/A'}\n"
        f"Modo: PAPER\n\n"
        f"El bot seguira corriendo en modo paper.\n"
        f"Usa /live para reactivar trading real."
    )
    await update.message.reply_text(msg, parse_mode="HTML")

    if _notifier_ref:
        await _notifier_ref.notify_mode_change("PAPER", "Emergency stop activado por usuario")


# --- /reset ---
async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    if _wallet_ref is None:
        await update.message.reply_text("Wallet no inicializada")
        return

    _wallet_ref.reset()
    if _engine_ref:
        _engine_ref.capital = _wallet_ref.capital
        _engine_ref.decisions.clear()

    await update.message.reply_text(
        f"Demo wallet reseteada a ${_wallet_ref.initial_capital:.2f} USDC\n"
        f"Historial de trades demo eliminado",
        parse_mode="HTML"
    )


# --- /help ---
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    mode = _engine_ref.get_mode_str() if _engine_ref else "?"
    msg = (
        f"<b>COMANDOS</b> (Modo: {mode})\n\n"
        "<b>Consultas:</b>\n"
        "/balance     — Balance real (Polymarket)\n"
        "/demobalance — Balance demo (paper wallet)\n"
        "/stats       — Estadisticas completas\n"
        "/positions   — Posiciones abiertas demo\n"
        "/trades      — Ultimos 5 trades demo\n"
        "/mode        — Modo actual\n\n"
        "<b>Control:</b>\n"
        "/live        — Activar trading real\n"
        "/paper       — Volver a modo paper\n"
        "/pauselive   — Pausar nuevas entradas live\n"
        "/stop        — EMERGENCY STOP\n"
        "/reset       — Resetear demo wallet\n"
        "/help        — Este mensaje"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Handler de confirmacion (texto libre despues de /live o /paper)
# ---------------------------------------------------------------------------

async def _handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Procesa respuestas de confirmacion para /live y /paper."""
    if not _is_authorized(update):
        return
    chat_id = str(update.effective_chat.id)
    pending = _pending_confirmation.pop(chat_id, None)

    if pending is None:
        return  # No hay confirmacion pendiente, ignorar

    text = (update.message.text or "").strip().upper()

    if text != "CONFIRMAR":
        await update.message.reply_text("Accion cancelada.")
        return

    if pending == "live" and _engine_ref:
        msg = _engine_ref.set_live_mode()
        if _safety_ref:
            _safety_ref.reset_circuit_breaker()
        await update.message.reply_text(f"<b>{msg}</b>", parse_mode="HTML")
        if _notifier_ref:
            await _notifier_ref.notify_mode_change("LIVE", "Activado por usuario")

    elif pending == "paper" and _engine_ref:
        # Cancelar ordenes primero
        if _order_manager_ref:
            await _order_manager_ref.cancel_all_orders()
        msg = _engine_ref.set_paper_mode()
        await update.message.reply_text(f"<b>{msg}</b>", parse_mode="HTML")
        if _notifier_ref:
            await _notifier_ref.notify_mode_change("PAPER", "Cambiado por usuario")


# ---------------------------------------------------------------------------
# Iniciar polling de comandos
# ---------------------------------------------------------------------------

async def start_telegram_polling(token: str = "", chat_id: str = "") -> Optional[Application]:
    """Inicia el polling de comandos de Telegram en background."""
    tok = token or TELEGRAM_TOKEN
    if not tok:
        logger.warning("Telegram polling no iniciado (falta TELEGRAM_TOKEN)")
        return None

    app = Application.builder().token(tok).build()

    # Registrar command handlers
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("demobalance", cmd_demobalance))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("trades", cmd_trades))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("live", cmd_live))
    app.add_handler(CommandHandler("paper", cmd_paper))
    app.add_handler(CommandHandler("pauselive", cmd_pauselive))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))

    # Handler de texto libre para confirmaciones
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_confirmation))

    # Iniciar polling
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    # Registrar comandos en el menu de Telegram
    try:
        await app.bot.set_my_commands([
            BotCommand("balance",     "Balance real (Polymarket)"),
            BotCommand("demobalance", "Balance demo (paper wallet)"),
            BotCommand("stats",       "Estadisticas completas"),
            BotCommand("positions",   "Posiciones abiertas demo"),
            BotCommand("trades",      "Ultimos 5 trades demo"),
            BotCommand("mode",        "Modo actual"),
            BotCommand("live",        "Activar trading real"),
            BotCommand("paper",       "Volver a modo paper"),
            BotCommand("pauselive",   "Pausar nuevas entradas live"),
            BotCommand("stop",        "EMERGENCY STOP"),
            BotCommand("reset",       "Resetear demo wallet"),
            BotCommand("help",        "Lista de comandos"),
        ])
        logger.debug("Comandos de Telegram registrados en el menu")
    except Exception as e:
        logger.warning(f"No se pudieron registrar comandos en Telegram: {e}")

    logger.success("Telegram polling iniciado — comandos disponibles")
    return app
