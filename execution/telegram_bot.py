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
from datetime import datetime, timezone, timedelta
from typing import Optional
from loguru import logger

# Zona horaria UTC-5 (Lima, Bogota, etc.)
_TZ_UTC5 = timezone(timedelta(hours=-5))

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

    async def send(self, message: str, parse_mode: str = "HTML", retries: int = 2) -> bool:
        """Envia un mensaje al chat con reintentos. Retorna True si exito."""
        if not self.enabled or not self.bot:
            return False
        for attempt in range(1, retries + 1):
            try:
                await self.bot.send_message(
                    chat_id=self.chat_id,
                    text=message,
                    parse_mode=parse_mode,
                    read_timeout=15,
                    write_timeout=15,
                    connect_timeout=10,
                )
                return True
            except Exception as e:
                logger.warning(
                    f"Telegram send intento {attempt}/{retries} fallo: {e}"
                )
                if attempt < retries:
                    await asyncio.sleep(2 * attempt)
        logger.error(f"Telegram send fallo despues de {retries} intentos")
        return False

    # --- Helpers ---

    @staticmethod
    def _now_utc5() -> str:
        """Retorna la hora actual en UTC-5 formateada."""
        return datetime.now(_TZ_UTC5).strftime("%H:%M:%S")

    # --- Mensajes pre-formateados ---

    async def notify_new_market(self, slug: str, question: str = "",
                                btc_price: float = 0.0) -> None:
        """Notifica que se detecto un nuevo mercado BTC 5-min."""
        hora = self._now_utc5()
        btc_str = f"BTC: <b>${btc_price:,.2f}</b>\n" if btc_price else ""
        msg = (
            f"📢 <b>NUEVO MERCADO DETECTADO</b>\n"
            f"🕐 {hora} (UTC-5)\n\n"
            f"{btc_str}"
            f"🎯 <code>{slug}</code>\n"
            f"{question}"
        )
        await self.send(msg)

    async def notify_decision(self, decision_dict: dict, mode: str = "PAPER",
                              btc_price: float = 0.0) -> None:
        d = decision_dict
        action = d.get("action", "SKIP")
        mode_icon = "🟢" if mode == "LIVE" else "🟡"
        btc_str = f"📍 BTC al decidir: <b>${btc_price:,.2f}</b>\n" if btc_price else ""

        hora = self._now_utc5()

        if action == "SKIP":
            reason = (d.get("signal_reason", "")
                      .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            msg = (
                f"⛔ <b>NO ENTRA</b> {mode_icon} {mode}\n"
                f"🕐 {hora} (UTC-5)\n\n"
                f"Mercado: <code>{d.get('slug', '')}</code>\n"
                f"{btc_str}"
                f"Confianza: {d.get('confidence', 0):.1%}\n"
                f"Regimen: {d.get('regime', '?')}\n"
                f"Razon: {reason}"
            )
        else:
            direction = "📈 UP" if action == "BUY_YES" else "📉 DOWN"
            msg = (
                f"✅ <b>ENTRADA</b> {mode_icon} {mode}\n"
                f"🕐 {hora} (UTC-5)\n\n"
                f"Mercado: <code>{d.get('slug', '')}</code>\n"
                f"{btc_str}"
                f"Prediccion: <b>{direction}</b>\n"
                f"Accion: <b>{action}</b>\n\n"
                f"📊 P(UP): {d.get('prob_up', 0):.1%} | P(DOWN): {d.get('prob_down', 0):.1%}\n"
                f"Confianza: <b>{d.get('confidence', 0):.1%}</b>\n"
                f"Regimen: {d.get('regime', '?')}\n\n"
                f"💰 Precio entrada: <b>${d.get('target_price', 0):.4f}</b>\n"
                f"Tipo orden: {d.get('order_type', '')}\n"
                f"Monto: <b>${d.get('usdc_amount', 0):.2f}</b> ({d.get('n_shares', 0):.1f} shares)\n"
                f"Fee est: ${d.get('fee_estimated', 0):.4f}"
            )
        await self.send(msg)

    async def notify_order_sent(self, order_result: dict) -> None:
        """Notifica que se envio una orden real a Polymarket."""
        hora = self._now_utc5()
        btc_price = order_result.get("btc_price", 0)
        btc_str = f"📍 BTC al ejecutar: <b>${btc_price:,.2f}</b>\n" if btc_price else ""

        if order_result.get("success"):
            upgraded = " (reintento)" if order_result.get("was_upgraded") else ""
            msg = (
                f"🚀 <b>ORDEN EJECUTADA</b>{upgraded}\n"
                f"🕐 {hora} (UTC-5)\n\n"
                f"{btc_str}"
                f"Tipo: {order_result.get('order_type', '?')}\n"
                f"Shares: <b>{order_result.get('shares_filled', 0):.1f}</b>\n"
                f"Precio: <b>${order_result.get('fill_price', 0):.4f}</b>\n"
                f"USDC gastado: <b>${order_result.get('usdc_spent', 0):.2f}</b>\n"
                f"ID: <code>{order_result.get('order_id', '')[:20]}...</code>"
            )
        else:
            msg = (
                f"❌ <b>ORDEN FALLIDA</b>\n\n"
                f"{btc_str}"
                f"Error: {order_result.get('error', 'desconocido')}"
            )
        await self.send(msg)

    async def notify_market_resolved(self, slug: str, winning_outcome: str,
                                     btc_open: float = 0, btc_close: float = 0,
                                     had_position: bool = False) -> None:
        """Notifica que un mercado se resolvio (siempre, tenga o no posicion)."""
        if had_position:
            return  # La notificacion de WIN/LOSS ya incluye la resolucion

        hora = self._now_utc5()
        btc_line = ""
        if btc_open and btc_close:
            btc_dir = "📈" if btc_close > btc_open else "📉"
            btc_change = btc_close - btc_open
            btc_line = (
                f"{btc_dir} BTC: ${btc_open:,.2f} → ${btc_close:,.2f} "
                f"({btc_change:+,.2f})\n"
            )

        outcome_icon = "🟢" if winning_outcome == "Yes" else "🔴"
        msg = (
            f"🏁 <b>MERCADO CERRADO</b>\n"
            f"🕐 {hora} (UTC-5)\n\n"
            f"<code>{slug}</code>\n"
            f"Resultado: {outcome_icon} <b>{winning_outcome}</b>\n"
            f"{btc_line}"
            f"Sin posicion abierta"
        )
        await self.send(msg)

    async def notify_resolution(self, trade_dict: dict, balance: dict) -> None:
        """Notifica resolucion de un mercado con resultado del trade."""
        hora = self._now_utc5()
        t = trade_dict
        won = t.get("won", False)

        if won:
            icon = "🏆"
            result = "GANASTE"
        else:
            icon = "🔴"
            result = "PERDISTE"

        btc_open = t.get("btc_open", 0)
        btc_close = t.get("btc_close", 0)
        btc_line = ""
        if btc_open and btc_close:
            btc_dir = "📈" if btc_close > btc_open else "📉"
            btc_change = btc_close - btc_open
            btc_line = (
                f"\n{btc_dir} BTC: ${btc_open:,.2f} → ${btc_close:,.2f} "
                f"({btc_change:+,.2f})\n"
            )

        msg = (
            f"{icon} <b>{result}</b>\n"
            f"🕐 {hora} (UTC-5)\n\n"
            f"Mercado: <code>{t.get('slug', '')}</code>\n"
            f"Apuesta: {t.get('action', '')} | Resultado: {t.get('outcome', '')}"
            f"{btc_line}\n"
            f"💵 PnL: <b>${t.get('pnl', 0):+.2f}</b> ({t.get('pnl_pct', 0):+.1f}%)\n\n"
            f"💼 <b>Estado de cuenta (demo)</b>\n"
            f"Capital: ${balance.get('equity_total', 0):.2f}\n"
            f"PnL total: ${balance.get('pnl_total', 0):+.2f} ({balance.get('pnl_total_pct', 0):+.1f}%)\n"
            f"Win rate: {balance.get('win_rate', 0):.1%} "
            f"({balance.get('wins', 0)}W/{balance.get('losses', 0)}L)\n"
            f"Racha: {balance.get('racha', '0')}"
        )
        await self.send(msg)

    async def notify_safety_triggered(self, message: str) -> None:
        """Notifica que se activo un mecanismo de seguridad."""
        msg = f"🚨 <b>ALERTA DE SEGURIDAD</b>\n\n{message}"
        await self.send(msg)

    async def notify_mode_change(self, new_mode: str, reason: str = "") -> None:
        """Notifica cambio de modo."""
        icon = "🟢" if new_mode == "LIVE" else "🟡"
        msg = f"{icon} <b>MODO: {new_mode}</b>"
        if reason:
            msg += f"\n{reason}"
        await self.send(msg)

    async def notify_error(self, error_msg: str) -> None:
        msg = f"⚠️ <b>ERROR</b>\n<code>{error_msg[:500]}</code>"
        await self.send(msg)

    async def notify_startup(self, stats: dict) -> None:
        data_status = "ACTIVA" if stats.get("data_collection") else "DESACTIVADA"
        poly_icon = "✅" if stats.get("poly_ready") else "❌"
        model_icon = "✅" if stats.get("model_loaded") else "❌"
        msg = (
            f"🤖 <b>BOT INICIADO</b>\n\n"
            f"Capital demo: <b>${stats.get('capital', 0):.2f}</b> USDC\n"
            f"Modelo: {model_icon} {'Cargado' if stats.get('model_loaded') else 'NO cargado'}\n"
            f"Modo: <b>{stats.get('mode', 'PAPER')}</b>\n"
            f"DB: {stats.get('db_backend', '?')}\n"
            f"Data collection: {data_status}\n"
            f"Polymarket live: {poly_icon} {'Listo' if stats.get('poly_ready') else 'No disponible'}"
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


async def _safe_reply(
    update: Update, text: str, parse_mode: str = "HTML", retries: int = 2
) -> bool:
    """reply_text protegido contra TimedOut con reintentos."""
    for attempt in range(1, retries + 1):
        try:
            await update.message.reply_text(
                text,
                parse_mode=parse_mode,
                read_timeout=15,
                write_timeout=15,
                connect_timeout=10,
            )
            return True
        except Exception as e:
            logger.warning(f"reply_text intento {attempt}/{retries} fallo: {e}")
            if attempt < retries:
                await asyncio.sleep(2 * attempt)
    logger.error(f"reply_text fallo despues de {retries} intentos")
    return False


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
        # Ejecutar en thread para no bloquear el event loop (SDK sincrono)
        usdc = await asyncio.to_thread(_poly_client_ref.get_usdc_balance)
        mode = _engine_ref.get_mode_str() if _engine_ref else "?"
        daily = _safety_ref.get_daily_stats() if _safety_ref else {}

        # Payouts pendientes de redeem
        pending_line = ""
        if _wallet_ref:
            pending = _wallet_ref.get_pending_payouts()
            if pending["count"] > 0:
                s = "s" if pending["count"] > 1 else ""
                pending_line = (
                    f"\n⏳ <b>Payout pendiente:</b> ~${pending['total']:.2f} "
                    f"({pending['count']} mercado{s} ganado{s} en proceso de redeem)\n"
                    f"Balance estimado real: <b>${usdc + pending['total']:.2f}</b>\n"
                )

        msg = (
            f"💰 <b>BALANCE REAL (Polymarket)</b>\n\n"
            f"USDC disponible: <b>${usdc:.2f}</b>\n"
            f"Modo: {mode}\n"
            f"{pending_line}\n"
            f"<b>Hoy:</b>\n"
            f"PnL: ${daily.get('daily_pnl', 0):+.2f} ({daily.get('daily_pnl_pct', 0):+.1f}%)\n"
            f"Trades: {daily.get('daily_trades', 0)} "
            f"({daily.get('daily_wins', 0)}W/{daily.get('daily_losses', 0)}L)"
        )
    else:
        msg = (
            "💰 <b>BALANCE REAL</b>\n\n"
            "Polymarket client no disponible.\n"
            "Configura POLY_PRIVATE_KEY y POLY_FUNDER_ADDRESS."
        )
    await _safe_reply(update, msg)


# --- /demobalance --- Balance del paper wallet
async def cmd_demobalance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Balance de la wallet demo (paper trading)."""
    if not _is_authorized(update):
        return
    if _wallet_ref is None:
        await _safe_reply(update, "Wallet no inicializada")
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
    await _safe_reply(update, msg)


# --- /stats ---
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    if _wallet_ref is None or _engine_ref is None:
        await _safe_reply(update, "Bot no inicializado completamente")
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
    await _safe_reply(update, msg)


# --- /positions ---
async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    if _wallet_ref is None:
        await _safe_reply(update, "Wallet no inicializada")
        return

    positions = _wallet_ref.get_open_positions_summary()
    if not positions:
        await _safe_reply(update, "Sin posiciones abiertas (demo)")
        return

    lines = ["<b>POSICIONES ABIERTAS (Demo)</b>\n"]
    for p in positions:
        lines.append(
            f"  {p['action']} <code>{p['slug']}</code>\n"
            f"    {p['n_shares']} shares @ ${p['buy_price']:.3f} = ${p['usdc']:.2f}\n"
            f"    Conf: {p['confidence']:.1%}"
        )
    await _safe_reply(update, "\n".join(lines))


# --- /trades ---
async def cmd_trades(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    if _wallet_ref is None:
        await _safe_reply(update, "Wallet no inicializada")
        return

    trades = _wallet_ref.get_recent_trades(n=5)
    if not trades:
        await _safe_reply(update, "Sin trades cerrados aun")
        return

    lines = ["<b>ULTIMOS TRADES (Demo)</b>\n"]
    for t in reversed(trades):
        icon = "W" if t['won'] else "L"
        lines.append(
            f"  [{icon}] {t['action']} | {t['outcome']} | "
            f"${t['pnl']:+.2f} ({t['pnl_pct']:+.1f}%) | "
            f"conf {t['confidence']:.1%}"
        )
    await _safe_reply(update, "\n".join(lines))


# --- /mode ---
async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra el modo actual."""
    if not _is_authorized(update):
        return
    if _engine_ref is None:
        await _safe_reply(update, "Engine no inicializado")
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
    await _safe_reply(update, msg)


# --- /live --- Activar trading real (con confirmacion)
async def cmd_live(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    if _engine_ref is None:
        await _safe_reply(update, "Engine no inicializado")
        return

    if not _poly_client_ref or not _poly_client_ref.is_ready():
        await _safe_reply(update,
            "Polymarket client no disponible.\n"
            "Configura POLY_PRIVATE_KEY y POLY_FUNDER_ADDRESS."
        )
        return

    if not _engine_ref.paper_mode and not _engine_ref.live_paused:
        await _safe_reply(update, "Ya estas en modo LIVE")
        return

    # Pedir confirmacion
    usdc = await asyncio.to_thread(_poly_client_ref.get_usdc_balance)
    chat_id = str(update.effective_chat.id)
    _pending_confirmation[chat_id] = "live"

    msg = (
        f"<b>CONFIRMAR ACTIVACION LIVE</b>\n\n"
        f"Vas a activar TRADING REAL con fondos reales.\n"
        f"USDC disponible: <b>${usdc:.2f}</b>\n\n"
        f"Escribe <b>CONFIRMAR</b> para activar.\n"
        f"Cualquier otro mensaje cancela."
    )
    await _safe_reply(update, msg)


# --- /paper --- Volver a paper mode (con confirmacion)
async def cmd_paper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    if _engine_ref is None:
        await _safe_reply(update, "Engine no inicializado")
        return

    if _engine_ref.paper_mode:
        await _safe_reply(update, "Ya estas en modo PAPER")
        return

    chat_id = str(update.effective_chat.id)
    _pending_confirmation[chat_id] = "paper"

    msg = (
        f"<b>CONFIRMAR CAMBIO A PAPER</b>\n\n"
        f"Se cancelaran las ordenes abiertas y se pasara a modo paper.\n\n"
        f"Escribe <b>CONFIRMAR</b> para cambiar.\n"
        f"Cualquier otro mensaje cancela."
    )
    await _safe_reply(update, msg)


# --- /pauselive --- Pausar nuevas entradas
async def cmd_pauselive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    if _engine_ref is None:
        await _safe_reply(update, "Engine no inicializado")
        return

    msg = _engine_ref.pause_live()
    await _safe_reply(update, msg, parse_mode=None)


# --- /stop --- EMERGENCY STOP
async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Emergency stop: cancela ordenes + paper mode. Sin confirmacion."""
    if not _is_authorized(update):
        return
    if _engine_ref is None:
        await _safe_reply(update, "Engine no inicializado")
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
    await _safe_reply(update, msg)

    if _notifier_ref:
        await _notifier_ref.notify_mode_change("PAPER", "Emergency stop activado por usuario")


# --- /reset ---
async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    if _wallet_ref is None:
        await _safe_reply(update, "Wallet no inicializada")
        return

    _wallet_ref.reset()
    if _engine_ref:
        _engine_ref.capital = _wallet_ref.capital
        _engine_ref.decisions.clear()

    await _safe_reply(update,
        f"Demo wallet reseteada a ${_wallet_ref.initial_capital:.2f} USDC\n"
        f"Historial de trades demo eliminado"
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
    await _safe_reply(update, msg)


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
        await _safe_reply(update, "Accion cancelada.", parse_mode=None)
        return

    if pending == "live" and _engine_ref:
        msg = _engine_ref.set_live_mode()
        if _safety_ref:
            _safety_ref.reset_circuit_breaker()
        await _safe_reply(update, f"<b>{msg}</b>")
        if _notifier_ref:
            await _notifier_ref.notify_mode_change("LIVE", "Activado por usuario")

    elif pending == "paper" and _engine_ref:
        # Cancelar ordenes primero
        if _order_manager_ref:
            await _order_manager_ref.cancel_all_orders()
        msg = _engine_ref.set_paper_mode()
        await _safe_reply(update, f"<b>{msg}</b>")
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

    app = (
        Application.builder()
        .token(tok)
        .read_timeout(15)
        .write_timeout(15)
        .connect_timeout(10)
        .pool_timeout(10)
        .build()
    )

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
