"""
notifications/telegram.py
--------------------------
Notificaciones y comandos de Telegram para el bot BTC Predictor.

Dos funcionalidades:
  1. NOTIFICACIONES (push): el bot envia mensajes al chat cuando ocurren eventos
  2. COMANDOS (pull): el usuario envia /balance, /status, etc. y el bot responde

Arquitectura:
  - TelegramNotifier: clase singleton que encapsula el Bot de Telegram
  - Se inicializa con token + chat_id desde variables de entorno
  - Todos los metodos de notificacion son fire-and-forget (nunca crashean el bot)
  - AIORateLimiter maneja automaticamente los rate limits de Telegram
  - El command listener corre como tarea async en el event loop del bot principal

Variables de entorno:
  TELEGRAM_TOKEN         : token del bot (de @BotFather)
  TELEGRAM_CHAT_ID       : chat_id destino (tu chat privado o grupo)
  TELEGRAM_STATS_INTERVAL: intervalo en segundos para stats periodicos (default 300)
  TELEGRAM_NOTIFY_MARKET : true/false — notificar mercados detectados (default true)
  TELEGRAM_NOTIFY_SKIP   : true/false — notificar skips del modelo (default true)
"""

import os
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, TYPE_CHECKING
from loguru import logger

import telegram
from telegram import Bot, Update, BotCommand
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    AIORateLimiter,
)

if TYPE_CHECKING:
    from execution.paper_wallet import PaperWallet, ClosedTrade
    from strategy.engine import StrategyEngine
    from execution.safety import SafetyManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TZ_LIMA = timezone(timedelta(hours=-5))


def _esc(text: str) -> str:
    """Escapa caracteres especiales para HTML parse_mode."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _bool_env(key: str, default: bool = False) -> bool:
    """Lee una variable de entorno como bool."""
    val = os.environ.get(key, "").strip().lower()
    if val in ("true", "1", "yes"):
        return True
    if val in ("false", "0", "no"):
        return False
    return default


def _slug_period(slug: str) -> str:
    """
    Extrae el periodo de tiempo de un slug de mercado BTC 5-min.
    Formato slug: btc-updown-5m-TIMESTAMP
    Retorna: "HH:MM - HH:MM" en hora Lima, o "" si no se puede parsear.
    """
    try:
        ts = int(slug.rsplit("-", 1)[-1])
        dt_start = datetime.fromtimestamp(ts, tz=_TZ_LIMA)
        dt_end = datetime.fromtimestamp(ts + 300, tz=_TZ_LIMA)
        return f"{dt_start.strftime('%H:%M')} - {dt_end.strftime('%H:%M')}"
    except (ValueError, IndexError, OSError):
        return ""


def _slug_ref_number(slug: str) -> int:
    """
    Genera un numero de referencia corto a partir del slug.
    Usa los ultimos 4 digitos del timestamp para crear un # unico
    pero corto y facil de recordar.
    """
    try:
        ts = int(slug.rsplit("-", 1)[-1])
        return ts % 10000
    except (ValueError, IndexError):
        return 0


# ---------------------------------------------------------------------------
# Notifier
# ---------------------------------------------------------------------------

class TelegramNotifier:
    """
    Maneja notificaciones push y comandos pull via Telegram.

    Uso:
        notifier = TelegramNotifier()
        if notifier.is_enabled():
            await notifier.start()            # inicia command listener
            await notifier.bot_started(...)   # envia notificacion
            ...
            await notifier.stop()             # detiene command listener
    """

    def __init__(self):
        self._token = os.environ.get("TELEGRAM_TOKEN", "").strip()
        self._chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self._enabled = bool(self._token and self._chat_id)

        # Config de notificaciones
        self._stats_interval = int(os.environ.get("TELEGRAM_STATS_INTERVAL", "300"))
        self._notify_market = _bool_env("TELEGRAM_NOTIFY_MARKET", True)
        self._notify_skip = _bool_env("TELEGRAM_NOTIFY_SKIP", True)

        # Bot y Application (se inicializan en start())
        self._bot: Optional[Bot] = None
        self._app: Optional[Application] = None
        self._polling_task: Optional[asyncio.Task] = None
        self._stats_counter: int = 0

        # Referencias a componentes del bot (se setean desde main.py)
        self._wallet: Optional["PaperWallet"] = None
        self._engine: Optional["StrategyEngine"] = None
        self._safety: Optional["SafetyManager"] = None

        if self._enabled:
            logger.info(
                f"Telegram habilitado | chat_id={self._chat_id} "
                f"| stats cada {self._stats_interval}s "
                f"| mercados={'ON' if self._notify_market else 'OFF'} "
                f"| skips={'ON' if self._notify_skip else 'OFF'}"
            )
        else:
            if not self._token:
                logger.info("Telegram deshabilitado — TELEGRAM_TOKEN no configurado")
            elif not self._chat_id:
                logger.info("Telegram deshabilitado — TELEGRAM_CHAT_ID no configurado")

    # -----------------------------------------------------------------------
    # Setup
    # -----------------------------------------------------------------------

    def is_enabled(self) -> bool:
        return self._enabled

    def set_components(
        self,
        wallet: "PaperWallet",
        engine: "StrategyEngine",
        safety: Optional["SafetyManager"] = None,
    ) -> None:
        """Setea referencias a componentes del bot para los comandos."""
        self._wallet = wallet
        self._engine = engine
        self._safety = safety

    async def start(self) -> None:
        """Inicia el bot de Telegram (command listener + polling)."""
        if not self._enabled:
            return

        try:
            rate_limiter = AIORateLimiter(
                overall_max_rate=25,    # 25 msg/s (Telegram limit: 30)
                overall_time_period=1,
                max_retries=3,
            )

            self._app = (
                ApplicationBuilder()
                .token(self._token)
                .rate_limiter(rate_limiter)
                .build()
            )
            self._bot = self._app.bot

            # Registrar comandos
            self._register_commands()

            # Setear menu de comandos en Telegram
            await self._bot.set_my_commands([
                BotCommand("balance", "Estado de la wallet"),
                BotCommand("status", "Estado del bot"),
                BotCommand("trades", "Ultimos 5 trades"),
                BotCommand("open", "Posiciones abiertas"),
                BotCommand("stats", "Stats completos"),
                BotCommand("live", "Activar modo live"),
                BotCommand("paper", "Activar modo paper"),
                BotCommand("pause", "Pausar/reanudar live"),
            ])

            # Iniciar polling en background
            await self._app.initialize()
            await self._app.start()
            self._polling_task = asyncio.create_task(
                self._app.updater.start_polling(drop_pending_updates=True),
                name="telegram_polling",
            )

            logger.success("Telegram bot iniciado — escuchando comandos")

        except Exception as e:
            logger.error(f"Error iniciando Telegram bot: {e}")
            self._enabled = False

    async def stop(self) -> None:
        """Detiene el bot de Telegram."""
        if not self._enabled or not self._app:
            return

        try:
            if self._app.updater and self._app.updater.running:
                await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            logger.info("Telegram bot detenido")
        except Exception as e:
            logger.debug(f"Error deteniendo Telegram: {e}")

    # -----------------------------------------------------------------------
    # Envio base (fire-and-forget)
    # -----------------------------------------------------------------------

    async def _send(self, text: str) -> None:
        """Envia un mensaje al chat configurado. Nunca lanza excepciones."""
        if not self._enabled or not self._bot:
            return

        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except telegram.error.RetryAfter as e:
            logger.warning(f"Telegram rate limit — retry en {e.retry_after}s")
            await asyncio.sleep(e.retry_after)
            try:
                await self._bot.send_message(
                    chat_id=self._chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"Telegram send error: {e}")

    # -----------------------------------------------------------------------
    # NOTIFICACIONES — Ciclo de vida
    # -----------------------------------------------------------------------

    async def bot_started(self, config: dict) -> None:
        """Notifica que el bot arranco."""
        mode = config.get("mode", "?")
        capital = config.get("capital", 0)
        model = config.get("model", "?")
        db = config.get("db", "?")
        poly = config.get("poly", "off")

        await self._send(
            f"<b>BOT INICIADO</b>\n"
            f"Capital: ${capital:.2f} | Modo: {_esc(mode)}\n"
            f"Modelo: {_esc(model)}\n"
            f"DB: {_esc(db)} | Polymarket: {_esc(poly)}"
        )

    async def bot_stopped(self, reason: str = "normal") -> None:
        """Notifica que el bot se detuvo."""
        await self._send(f"<b>BOT DETENIDO</b>\nRazon: {_esc(reason)}")

    async def mode_changed(self, new_mode: str) -> None:
        """Notifica cambio de modo."""
        await self._send(f"Modo cambiado a <b>{_esc(new_mode)}</b>")

    async def daily_reset(self, yesterday_pnl: float, new_date: str) -> None:
        """Notifica reset diario."""
        await self._send(
            f"Nuevo dia: <b>{_esc(new_date)}</b>\n"
            f"PnL ayer: ${yesterday_pnl:+.2f}"
        )

    # -----------------------------------------------------------------------
    # NOTIFICACIONES — Trading
    # -----------------------------------------------------------------------

    async def position_opened(self, data: dict) -> None:
        """Notifica posicion abierta en paper wallet."""
        action = data.get("action", "?")
        slug = data.get("slug", "?")
        n_shares = data.get("n_shares", 0)
        fill_price = data.get("fill_price", 0)
        slippage = data.get("slippage", 0)
        fee = data.get("fee", 0)
        total_cost = data.get("total_cost", 0)
        confidence = data.get("confidence", 0)
        capital = data.get("capital", 0)
        btc_price = data.get("btc_price", 0)
        prob_up = data.get("prob_up", 0)
        prob_down = data.get("prob_down", 0)
        regime = data.get("regime", "")
        was_retry = data.get("was_retry", False)

        ref = _slug_ref_number(slug)
        period = _slug_period(slug)
        period_str = f" ({period})" if period else ""
        slip_str = f" (slippage: ${slippage:+.4f})" if slippage != 0 else ""
        btc_str = f"\nBTC: ${btc_price:,.2f}" if btc_price else ""
        retry_str = " [RETRY +$0.02]" if was_retry else ""
        regime_str = f" | Regime: {_esc(regime)}" if regime else ""

        # Direccion predicha
        if prob_up >= prob_down:
            pred_str = f"UP {prob_up:.1%} / DOWN {prob_down:.1%}"
        else:
            pred_str = f"DOWN {prob_down:.1%} / UP {prob_up:.1%}"

        await self._send(
            f"<b>PAPER OPEN</b> — {_esc(action)} <b>#{ref}</b>{period_str}{retry_str}\n"
            f"Prediccion: {pred_str}{regime_str}\n"
            f"{n_shares:.1f} shares @ ${fill_price:.4f}{slip_str}\n"
            f"Fee: ${fee:.4f} | Costo: ${total_cost:.2f}\n"
            f"Capital: ${capital:.2f}{btc_str}"
        )

    async def position_closed(self, data: dict) -> None:
        """Notifica posicion cerrada (WIN o LOSS)."""
        won = data.get("won", False)
        action = data.get("action", "?")
        slug = data.get("slug", "?")
        outcome = data.get("outcome", "?")
        pnl = data.get("pnl", 0)
        pnl_pct = data.get("pnl_pct", 0)
        capital = data.get("capital", 0)
        win_rate = data.get("win_rate", 0)
        wins = data.get("wins", 0)
        losses = data.get("losses", 0)
        btc_open = data.get("btc_open", 0)
        btc_close = data.get("btc_close", 0)
        direction = data.get("direction", "")

        ref = _slug_ref_number(slug)
        period = _slug_period(slug)
        period_str = f" ({period})" if period else ""

        if won:
            icon = "\u2705"   # check verde
            tag = "WIN"
        else:
            icon = "\u274c"   # X roja
            tag = "LOSS"

        btc_str = ""
        if btc_open and btc_close:
            btc_str = f"\nBTC: ${btc_open:,.2f} \u2192 ${btc_close:,.2f} ({_esc(direction)})"

        await self._send(
            f"{icon} <b>{tag}</b> — {_esc(action)} <b>#{ref}</b>{period_str}\n"
            f"Outcome: {_esc(outcome)}{btc_str}\n"
            f"PnL: <b>${pnl:+.2f}</b> ({pnl_pct:+.1f}%)\n"
            f"Capital: ${capital:.2f} | WR: {win_rate:.0%} ({wins}W/{losses}L)"
        )

    async def paper_skip_no_fill(self, action: str, slug: str, reason: str) -> None:
        """Notifica que una orden paper no se lleno."""
        ref = _slug_ref_number(slug)
        period = _slug_period(slug)
        period_str = f" ({period})" if period else ""

        await self._send(
            f"PAPER SKIP (no fill) <b>#{ref}</b>{period_str}\n"
            f"{_esc(action)}\n"
            f"{_esc(reason)}"
        )

    async def live_order_sent(self, action: str, slug: str, price: float, shares: float) -> None:
        """Notifica envio de orden real."""
        await self._send(
            f"<b>LIVE ORDER</b> — {_esc(action)}\n"
            f"{_esc(slug)}\n"
            f"{shares:.1f} shares @ ${price:.4f}"
        )

    async def live_order_filled(self, order_id: str, shares: float, fill_price: float) -> None:
        """Notifica fill de orden real."""
        await self._send(
            f"<b>LIVE FILLED</b>\n"
            f"ID: {_esc(order_id[:20])}...\n"
            f"{shares:.1f} shares @ ${fill_price:.4f}"
        )

    async def live_order_failed(self, reason: str) -> None:
        """Notifica fallo de orden real."""
        await self._send(
            f"<b>LIVE ORDER FALLIDA</b>\n"
            f"{_esc(reason)}"
        )

    # -----------------------------------------------------------------------
    # NOTIFICACIONES — Mercado
    # -----------------------------------------------------------------------

    async def market_detected(
        self, question: str, slug: str, btc_price: float = 0,
    ) -> None:
        """Notifica nuevo mercado detectado (configurable)."""
        if not self._notify_market:
            return

        ref = _slug_ref_number(slug)
        period = _slug_period(slug)
        period_str = f"\nPeriodo: <b>{period}</b>" if period else ""
        btc_str = f"\nBTC: ${btc_price:,.2f}" if btc_price else ""

        await self._send(
            f"==========\nNuevo mercado <b>#{ref}</b>{period_str}{btc_str}\n"
            f"{_esc(question)}"
        )

    async def market_resolved(
        self, outcome: str, slug: str,
        btc_open: float, btc_close: float, direction: str,
    ) -> None:
        """Notifica mercado resuelto."""
        ref = _slug_ref_number(slug)
        period = _slug_period(slug)
        period_str = f" ({period})" if period else ""

        dir_icon = "\u2b06\ufe0f" if direction == "UP" else "\u2b07\ufe0f" if direction == "DOWN" else ""

        await self._send(
            f"RESUELTO <b>#{ref}</b>{period_str} [{_esc(outcome)}] {dir_icon}\n"
            f"BTC ${btc_open:,.2f} \u2192 ${btc_close:,.2f} ({_esc(direction)})"
        )

    async def model_skip(
        self, slug: str, confidence: float, regime: str, reason: str,
    ) -> None:
        """Notifica skip del modelo (configurable)."""
        if not self._notify_skip:
            return

        ref = _slug_ref_number(slug)
        period = _slug_period(slug)
        period_str = f" ({period})" if period else ""

        await self._send(
            f"SKIP <b>#{ref}</b>{period_str}\n"
            f"Confianza: {confidence:.3f} | Regime: {_esc(regime)}\n"
            f"{_esc(reason)}"
        )

    # -----------------------------------------------------------------------
    # NOTIFICACIONES — Seguridad
    # -----------------------------------------------------------------------

    async def daily_loss_triggered(
        self, daily_pnl: float, daily_pnl_pct: float, limit_pct: float,
    ) -> None:
        """Notifica activacion del daily loss limit."""
        await self._send(
            f"<b>ALERTA: DAILY LOSS LIMIT</b>\n"
            f"PnL hoy: ${daily_pnl:+.2f} ({daily_pnl_pct:+.1f}%)\n"
            f"Limite: -{limit_pct:.1f}%\n"
            f"Bot pausado automaticamente"
        )

    async def circuit_breaker_reset(self) -> None:
        """Notifica reseteo del circuit breaker."""
        await self._send("Circuit breaker reseteado — trading reactivado")

    # -----------------------------------------------------------------------
    # NOTIFICACIONES — Stats periodicos
    # -----------------------------------------------------------------------

    async def periodic_stats(self, stats: dict) -> None:
        """
        Notifica stats periodicos. Se llama cada STATS_INTERVAL (60s) desde main,
        pero solo envia a Telegram cada TELEGRAM_STATS_INTERVAL.
        """
        self._stats_counter += 1
        calls_per_tg = max(1, self._stats_interval // 60)
        if self._stats_counter % calls_per_tg != 0:
            return

        mode = stats.get("mode", "?")
        btc = stats.get("btc_price", 0)
        equity = stats.get("equity", 0)
        pnl = stats.get("pnl", 0)
        pnl_pct = stats.get("pnl_pct", 0)
        win_rate = stats.get("win_rate", 0)
        wins = stats.get("wins", 0)
        losses = stats.get("losses", 0)
        open_pos = stats.get("open_positions", 0)

        now = datetime.now(_TZ_LIMA).strftime("%H:%M")
        btc_str = f"${btc:,.2f}" if btc else "N/A"

        await self._send(
            f"<b>STATS</b> [{now} Lima] [{_esc(mode)}]\n"
            f"BTC: {btc_str}\n"
            f"Equity: ${equity:.2f} | PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%)\n"
            f"WR: {win_rate:.0%} ({wins}W/{losses}L) | Open: {open_pos}"
        )

    # -----------------------------------------------------------------------
    # NOTIFICACIONES — Conectividad
    # -----------------------------------------------------------------------

    async def heartbeat_failing(self, consecutive_failures: int) -> None:
        """Notifica fallos de heartbeat."""
        await self._send(
            f"<b>ALERTA: HEARTBEAT</b>\n"
            f"{consecutive_failures} fallos consecutivos\n"
            f"Las limit orders pueden haber sido canceladas por Polymarket"
        )

    async def websocket_disconnected(self, ws_type: str, reason: str) -> None:
        """Notifica desconexion de WebSocket."""
        await self._send(
            f"WS desconectado: <b>{_esc(ws_type)}</b>\n"
            f"{_esc(reason)}\n"
            f"Reconectando..."
        )

    # -----------------------------------------------------------------------
    # COMANDOS — Handlers
    # -----------------------------------------------------------------------

    def _register_commands(self) -> None:
        """Registra todos los command handlers."""
        if not self._app:
            return

        self._app.add_handler(CommandHandler("balance", self._cmd_balance))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("trades", self._cmd_trades))
        self._app.add_handler(CommandHandler("open", self._cmd_open))
        self._app.add_handler(CommandHandler("stats", self._cmd_stats))
        self._app.add_handler(CommandHandler("live", self._cmd_live))
        self._app.add_handler(CommandHandler("paper", self._cmd_paper))
        self._app.add_handler(CommandHandler("pause", self._cmd_pause))

    def _is_authorized(self, update: Update) -> bool:
        """Verifica que el mensaje viene del chat autorizado."""
        return str(update.effective_chat.id) == self._chat_id

    async def _cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Comando /balance — estado de la wallet."""
        if not self._is_authorized(update):
            return

        if not self._wallet:
            await update.message.reply_text("Wallet no inicializada")
            return

        b = self._wallet.get_balance()
        text = (
            f"<b>BALANCE</b>\n"
            f"Capital libre: ${b['capital_libre']:.2f}\n"
            f"Capital invertido: ${b['capital_invertido']:.2f}\n"
            f"Equity total: <b>${b['equity_total']:.2f}</b>\n"
            f"\n"
            f"PnL: ${b['pnl_total']:+.2f} ({b['pnl_total_pct']:+.1f}%)\n"
            f"Fees pagados: ${b['fees_total']:.4f}\n"
            f"\n"
            f"Trades: {b['trades_totales']} ({b['wins']}W / {b['losses']}L)\n"
            f"Win rate: {b['win_rate']:.1%}\n"
            f"Racha: {b['racha']}\n"
            f"Mejor trade: ${b['mejor_trade_pnl']:+.2f}\n"
            f"Peor trade: ${b['peor_trade_pnl']:+.2f}\n"
            f"\n"
            f"Posiciones abiertas: {b['posiciones_abiertas']}"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Comando /status — estado del bot."""
        if not self._is_authorized(update):
            return

        mode = self._engine.get_mode_str() if self._engine else "?"
        model = "cargado" if self._engine and self._engine.predictor.is_loaded() else "NO"
        capital = self._engine.capital if self._engine else 0

        safety_str = "N/A"
        if self._safety:
            s = self._safety.get_daily_stats()
            safety_str = (
                f"PnL hoy: ${s['daily_pnl']:+.2f} ({s['daily_pnl_pct']:+.1f}%)\n"
                f"Trades hoy: {s['daily_trades']} ({s['daily_wins']}W/{s['daily_losses']}L)\n"
                f"Circuit breaker: {'ACTIVO' if s['circuit_breaker'] else 'OFF'}"
            )

        text = (
            f"<b>STATUS</b>\n"
            f"Modo: <b>{_esc(mode)}</b>\n"
            f"Modelo: {model}\n"
            f"Capital engine: ${capital:.2f}\n"
            f"\n"
            f"Safety:\n{safety_str}"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _cmd_trades(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Comando /trades — ultimos 5 trades cerrados."""
        if not self._is_authorized(update):
            return

        if not self._wallet:
            await update.message.reply_text("Wallet no inicializada")
            return

        trades = self._wallet.get_recent_trades(5)
        if not trades:
            await update.message.reply_text("No hay trades cerrados todavia")
            return

        lines = ["<b>ULTIMOS TRADES</b>\n"]
        for t in reversed(trades):
            tag = "W" if t["won"] else "L"
            lines.append(
                f"[{tag}] {_esc(t['slug'][:30])}\n"
                f"    {t['action']} | PnL: ${t['pnl']:+.2f} ({t['pnl_pct']:+.1f}%) "
                f"| conf: {t['confidence']:.3f}"
            )

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def _cmd_open(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Comando /open — posiciones abiertas."""
        if not self._is_authorized(update):
            return

        if not self._wallet:
            await update.message.reply_text("Wallet no inicializada")
            return

        positions = self._wallet.get_open_positions_summary()
        if not positions:
            await update.message.reply_text("No hay posiciones abiertas")
            return

        lines = ["<b>POSICIONES ABIERTAS</b>\n"]
        for p in positions:
            lines.append(
                f"{_esc(p['action'])} {_esc(p['slug'][:30])}\n"
                f"    {p['n_shares']:.1f} shares @ ${p['buy_price']:.4f} "
                f"| ${p['usdc']:.2f} | conf: {p['confidence']:.3f}"
            )

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def _cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Comando /stats — stats completos bajo demanda."""
        if not self._is_authorized(update):
            return

        if not self._wallet or not self._engine:
            await update.message.reply_text("Bot no completamente inicializado")
            return

        b = self._wallet.get_balance()
        mode = self._engine.get_mode_str()

        safety_str = ""
        if self._safety:
            s = self._safety.get_daily_stats()
            safety_str = (
                f"\n<b>Safety</b>\n"
                f"PnL hoy: ${s['daily_pnl']:+.2f} ({s['daily_pnl_pct']:+.1f}%)\n"
                f"Trades hoy: {s['daily_trades']} | CB: {'ACTIVO' if s['circuit_breaker'] else 'off'}"
            )

        pending = self._wallet.get_pending_payouts()
        pending_str = ""
        if pending["count"] > 0:
            pending_str = f"\nPayouts pendientes: {pending['count']} (~${pending['total']:.2f})"

        text = (
            f"<b>STATS COMPLETOS</b>\n"
            f"Modo: {_esc(mode)}\n\n"
            f"<b>Wallet</b>\n"
            f"Capital libre: ${b['capital_libre']:.2f}\n"
            f"Invertido: ${b['capital_invertido']:.2f}\n"
            f"Equity: <b>${b['equity_total']:.2f}</b>\n"
            f"PnL: ${b['pnl_total']:+.2f} ({b['pnl_total_pct']:+.1f}%)\n"
            f"Fees: ${b['fees_total']:.4f}\n\n"
            f"<b>Trading</b>\n"
            f"Trades: {b['trades_totales']} ({b['wins']}W / {b['losses']}L)\n"
            f"Win rate: {b['win_rate']:.1%}\n"
            f"Racha: {b['racha']}\n"
            f"Mejor: ${b['mejor_trade_pnl']:+.2f} | Peor: ${b['peor_trade_pnl']:+.2f}\n"
            f"Abiertas: {b['posiciones_abiertas']}"
            f"{pending_str}"
            f"{safety_str}"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _cmd_live(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Comando /live — activar modo live."""
        if not self._is_authorized(update):
            return

        if not self._engine:
            await update.message.reply_text("Engine no inicializado")
            return

        self._engine.set_live_mode()
        if self._safety and self._safety.is_circuit_breaker_active():
            self._safety.reset_circuit_breaker()
            await update.message.reply_text(
                "<b>LIVE TRADING ACTIVADO</b>\n"
                "Circuit breaker reseteado",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text(
                "<b>LIVE TRADING ACTIVADO</b>",
                parse_mode=ParseMode.HTML,
            )

    async def _cmd_paper(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Comando /paper — activar modo paper."""
        if not self._is_authorized(update):
            return

        if not self._engine:
            await update.message.reply_text("Engine no inicializado")
            return

        self._engine.set_paper_mode()
        await update.message.reply_text(
            "<b>PAPER TRADING ACTIVADO</b>",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Comando /pause — pausar/reanudar live."""
        if not self._is_authorized(update):
            return

        if not self._engine:
            await update.message.reply_text("Engine no inicializado")
            return

        if self._engine.paper_mode:
            await update.message.reply_text("Estas en modo PAPER — /pause solo aplica a LIVE")
            return

        if self._engine.live_paused:
            self._engine.resume_live()
            await update.message.reply_text(
                "<b>LIVE REANUDADO</b>\nSe abriran nuevas posiciones reales",
                parse_mode=ParseMode.HTML,
            )
        else:
            self._engine.pause_live()
            await update.message.reply_text(
                "<b>LIVE PAUSADO</b>\nNo se abriran nuevas posiciones reales\n"
                "Las abiertas se resuelven normalmente",
                parse_mode=ParseMode.HTML,
            )
