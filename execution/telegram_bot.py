"""
execution/telegram_bot.py
-------------------------
Bot de Telegram para monitoreo del bot de trading.

Funcionalidades:
  NOTIFICACIONES AUTOMATICAS (push al chat):
    - Nuevo mercado detectado + decision del modelo
    - Resultado de mercado resuelto + PnL
    - Errores criticos

  COMANDOS (el usuario envia al bot):
    /balance   — estado de la wallet demo
    /stats     — estadisticas completas
    /positions — posiciones abiertas
    /trades    — ultimos 5 trades cerrados
    /reset     — resetear wallet demo
    /help      — lista de comandos
"""

import os
import asyncio
from typing import Optional
from loguru import logger

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes


# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


# ---------------------------------------------------------------------------
# Telegram Notifier (solo envio de mensajes, sin polling)
# ---------------------------------------------------------------------------

class TelegramNotifier:
    """
    Envia notificaciones al chat de Telegram.
    Se usa desde main.py para push de eventos.
    """

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
        """Notifica que se detecto un nuevo mercado BTC 5-min."""
        msg = (
            f"<b>NUEVO MERCADO</b>\n"
            f"<code>{slug}</code>\n"
            f"{question}"
        )
        await self.send(msg)

    async def notify_decision(self, decision_dict: dict) -> None:
        """Notifica la decision del bot para el mercado actual."""
        d = decision_dict
        action = d.get("action", "SKIP")

        if action == "SKIP":
            msg = (
                f"<b>DECISION: SKIP</b>\n"
                f"Mercado: <code>{d.get('slug', '')}</code>\n"
                f"Confianza: {d.get('confidence', 0):.1%}\n"
                f"Regimen: {d.get('regime', '?')}\n"
                f"Razon: {d.get('signal_reason', '')}"
            )
        else:
            direction = "UP" if action == "BUY_YES" else "DOWN"
            msg = (
                f"<b>DECISION: {action}</b>\n"
                f"Mercado: <code>{d.get('slug', '')}</code>\n\n"
                f"Prediccion BTC: <b>{direction}</b>\n"
                f"P(UP): {d.get('prob_up', 0):.1%} | P(DOWN): {d.get('prob_down', 0):.1%}\n"
                f"Confianza: {d.get('confidence', 0):.1%}\n"
                f"Regimen: {d.get('regime', '?')}\n\n"
                f"Orden: {d.get('order_type', '')} @ ${d.get('target_price', 0):.3f}\n"
                f"Monto: ${d.get('usdc_amount', 0):.2f} ({d.get('n_shares', 0):.0f} shares)\n"
                f"Fee est: ${d.get('fee_estimated', 0):.4f}\n"
                f"{'[PAPER MODE]' if d.get('paper_mode') else '[LIVE]'}"
            )
        await self.send(msg)

    async def notify_resolution(self, trade_dict: dict, balance: dict) -> None:
        """Notifica el resultado de un trade cerrado."""
        t = trade_dict
        won = t.get("won", False)
        result = "WIN" if won else "LOSS"

        msg = (
            f"<b>RESULTADO: {result}</b>\n"
            f"Mercado: <code>{t.get('slug', '')}</code>\n"
            f"Apuesta: {t.get('action', '')} | Outcome: {t.get('outcome', '')}\n"
            f"PnL: <b>${t.get('pnl', 0):+.2f}</b> ({t.get('pnl_pct', 0):+.1f}%)\n\n"
            f"Capital: ${balance.get('equity_total', 0):.2f}\n"
            f"PnL total: ${balance.get('pnl_total', 0):+.2f} ({balance.get('pnl_total_pct', 0):+.1f}%)\n"
            f"Win rate: {balance.get('win_rate', 0):.1%} "
            f"({balance.get('wins', 0)}W/{balance.get('losses', 0)}L)\n"
            f"Racha: {balance.get('racha', '0')}"
        )
        await self.send(msg)

    async def notify_error(self, error_msg: str) -> None:
        """Notifica un error critico."""
        msg = f"<b>ERROR</b>\n<code>{error_msg[:500]}</code>"
        await self.send(msg)

    async def notify_startup(self, stats: dict) -> None:
        """Notifica que el bot arranco correctamente."""
        msg = (
            f"<b>BOT INICIADO</b>\n\n"
            f"Capital: ${stats.get('capital', 0):.2f} USDC\n"
            f"Modelo: {'Cargado' if stats.get('model_loaded') else 'NO cargado'}\n"
            f"Modo: {'PAPER' if stats.get('paper_mode') else 'LIVE'}\n"
            f"DB: {stats.get('db_backend', '?')}"
        )
        await self.send(msg)


# ---------------------------------------------------------------------------
# Telegram Command Handlers (para el polling de comandos)
# ---------------------------------------------------------------------------

# Referencia global a la wallet y engine — se setean desde main.py
_wallet_ref = None
_engine_ref = None
_notifier_ref = None


def set_refs(wallet, engine, notifier):
    """main.py llama esto para pasar las referencias."""
    global _wallet_ref, _engine_ref, _notifier_ref
    _wallet_ref = wallet
    _engine_ref = engine
    _notifier_ref = notifier


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando /balance — estado de la wallet."""
    if _wallet_ref is None:
        await update.message.reply_text("Wallet no inicializada")
        return

    b = _wallet_ref.get_balance()
    msg = (
        f"<b>BALANCE</b>\n\n"
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


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando /stats — estadisticas completas."""
    if _wallet_ref is None or _engine_ref is None:
        await update.message.reply_text("Bot no inicializado completamente")
        return

    b = _wallet_ref.get_balance()
    e = _engine_ref.get_stats()

    msg = (
        f"<b>ESTADISTICAS</b>\n\n"
        f"<b>Wallet:</b>\n"
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
        f"<b>Efectividad:</b>\n"
        f"  Win rate: {b['win_rate']:.1%}\n"
        f"  {b['wins']}W / {b['losses']}L\n"
        f"  Racha actual: {b['racha']}"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando /positions — posiciones abiertas."""
    if _wallet_ref is None:
        await update.message.reply_text("Wallet no inicializada")
        return

    positions = _wallet_ref.get_open_positions_summary()
    if not positions:
        await update.message.reply_text("Sin posiciones abiertas")
        return

    lines = ["<b>POSICIONES ABIERTAS</b>\n"]
    for p in positions:
        lines.append(
            f"  {p['action']} <code>{p['slug']}</code>\n"
            f"    {p['n_shares']} shares @ ${p['buy_price']:.3f} = ${p['usdc']:.2f}\n"
            f"    Conf: {p['confidence']:.1%}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_trades(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando /trades — ultimos 5 trades cerrados."""
    if _wallet_ref is None:
        await update.message.reply_text("Wallet no inicializada")
        return

    trades = _wallet_ref.get_recent_trades(n=5)
    if not trades:
        await update.message.reply_text("Sin trades cerrados aun")
        return

    lines = ["<b>ULTIMOS TRADES</b>\n"]
    for t in reversed(trades):
        icon = "W" if t['won'] else "L"
        lines.append(
            f"  [{icon}] {t['action']} | {t['outcome']} | "
            f"${t['pnl']:+.2f} ({t['pnl_pct']:+.1f}%) | "
            f"conf {t['confidence']:.1%}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando /reset — resetear wallet demo."""
    if _wallet_ref is None:
        await update.message.reply_text("Wallet no inicializada")
        return

    _wallet_ref.reset()
    if _engine_ref:
        _engine_ref.capital = _wallet_ref.capital
        _engine_ref.decisions.clear()

    await update.message.reply_text(
        f"Wallet reseteada a ${_wallet_ref.initial_capital:.2f} USDC\n"
        f"Historial de trades eliminado",
        parse_mode="HTML"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando /help — lista de comandos."""
    msg = (
        "<b>COMANDOS</b>\n\n"
        "/balance   — Capital, PnL, win rate\n"
        "/stats     — Estadisticas completas\n"
        "/positions — Posiciones abiertas\n"
        "/trades    — Ultimos 5 trades\n"
        "/reset     — Resetear wallet demo\n"
        "/help      — Este mensaje"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Iniciar polling de comandos
# ---------------------------------------------------------------------------

async def start_telegram_polling(token: str = "", chat_id: str = "") -> Optional[Application]:
    """
    Inicia el polling de comandos de Telegram en background.
    Retorna la Application para poder detenerla despues.
    """
    tok = token or TELEGRAM_TOKEN
    if not tok:
        logger.warning("Telegram polling no iniciado (falta TELEGRAM_TOKEN)")
        return None

    app = Application.builder().token(tok).build()

    # Registrar handlers
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("trades", cmd_trades))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))

    # Iniciar polling sin bloquear
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    logger.success("Telegram polling iniciado — comandos disponibles")
    return app
