"""
execution/safety.py
-------------------
Mecanismos de seguridad para proteger el capital en modo live.

Funcionalidades:
  - Daily loss limit: si se pierde X% del capital en un dia, fuerza paper mode
  - Tracking de PnL diario
  - Reset automatico a medianoche UTC

Cuando se activa el daily loss limit:
  1. Cancela todas las ordenes abiertas
  2. Cambia el bot a paper mode
  3. Notifica por Telegram
  4. Solo el usuario puede reactivar live con /live
"""

import os
from datetime import datetime, timezone
from loguru import logger


# ---------------------------------------------------------------------------
# Safety Manager
# ---------------------------------------------------------------------------

class SafetyManager:
    """
    Monitorea el PnL diario y aplica limites de seguridad.
    """

    def __init__(
        self,
        daily_loss_limit_pct: float = 10.0,
        initial_capital: float = 1000.0,
    ):
        """
        Args:
            daily_loss_limit_pct: porcentaje maximo de perdida diaria (ej: 10 = 10%)
            initial_capital: capital de referencia para calcular el %
        """
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.reference_capital = initial_capital

        # Tracking diario
        self._daily_pnl: float = 0.0
        self._daily_trades: int = 0
        self._daily_wins: int = 0
        self._daily_losses: int = 0
        self._current_day: str = self._today_str()
        self._circuit_breaker_active: bool = False

        logger.info(
            f"SafetyManager | daily loss limit: {daily_loss_limit_pct}% "
            f"| ref capital: ${initial_capital:.2f}"
        )

    # -----------------------------------------------------------------------
    # Registro de trades
    # -----------------------------------------------------------------------

    def record_trade(self, pnl: float, won: bool) -> dict:
        """
        Registra un trade completado y verifica el daily loss limit.

        Retorna dict con:
          - limit_triggered: bool
          - daily_pnl: float
          - daily_pnl_pct: float
          - message: str (si se activo el limit)
        """
        self._check_day_reset()

        self._daily_pnl += pnl
        self._daily_trades += 1
        if won:
            self._daily_wins += 1
        else:
            self._daily_losses += 1

        daily_pnl_pct = (self._daily_pnl / self.reference_capital * 100) \
            if self.reference_capital > 0 else 0.0

        result = {
            "limit_triggered": False,
            "daily_pnl": round(self._daily_pnl, 2),
            "daily_pnl_pct": round(daily_pnl_pct, 2),
            "daily_trades": self._daily_trades,
            "daily_wins": self._daily_wins,
            "daily_losses": self._daily_losses,
            "message": "",
        }

        # Verificar si se activo el daily loss limit
        if daily_pnl_pct <= -self.daily_loss_limit_pct:
            self._circuit_breaker_active = True
            result["limit_triggered"] = True
            result["message"] = (
                f"DAILY LOSS LIMIT ACTIVADO\n"
                f"PnL hoy: ${self._daily_pnl:+.2f} ({daily_pnl_pct:+.1f}%)\n"
                f"Limite: -{self.daily_loss_limit_pct}%\n"
                f"El bot pasara a PAPER MODE automaticamente.\n"
                f"Usa /live para reactivar manualmente."
            )
            logger.error(f"DAILY LOSS LIMIT TRIGGERED | PnL: ${self._daily_pnl:+.2f} ({daily_pnl_pct:+.1f}%)")

        return result

    # -----------------------------------------------------------------------
    # Consultas
    # -----------------------------------------------------------------------

    def is_circuit_breaker_active(self) -> bool:
        """True si el circuit breaker esta activo (daily loss excedido)."""
        self._check_day_reset()
        return self._circuit_breaker_active

    def reset_circuit_breaker(self) -> None:
        """Resetea el circuit breaker manualmente (al activar /live)."""
        self._circuit_breaker_active = False
        logger.info("Circuit breaker reseteado manualmente")

    def update_reference_capital(self, capital: float) -> None:
        """Actualiza el capital de referencia (al inicio de cada dia)."""
        self.reference_capital = capital

    def get_daily_stats(self) -> dict:
        """Retorna estadisticas del dia actual."""
        self._check_day_reset()
        daily_pnl_pct = (self._daily_pnl / self.reference_capital * 100) \
            if self.reference_capital > 0 else 0.0

        return {
            "daily_pnl": round(self._daily_pnl, 2),
            "daily_pnl_pct": round(daily_pnl_pct, 2),
            "daily_trades": self._daily_trades,
            "daily_wins": self._daily_wins,
            "daily_losses": self._daily_losses,
            "circuit_breaker": self._circuit_breaker_active,
            "loss_limit_pct": self.daily_loss_limit_pct,
        }

    # -----------------------------------------------------------------------
    # Internos
    # -----------------------------------------------------------------------

    def _check_day_reset(self) -> None:
        """Resetea contadores si cambio el dia (UTC)."""
        today = self._today_str()
        if today != self._current_day:
            logger.info(
                f"Nuevo dia ({today}) — reseteando daily stats | "
                f"PnL ayer: ${self._daily_pnl:+.2f}"
            )
            self._daily_pnl = 0.0
            self._daily_trades = 0
            self._daily_wins = 0
            self._daily_losses = 0
            self._current_day = today
            self._circuit_breaker_active = False

    @staticmethod
    def _today_str() -> str:
        """Fecha actual en UTC como string YYYY-MM-DD."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
