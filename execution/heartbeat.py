"""
execution/heartbeat.py
----------------------
Mantiene la sesion activa con el CLOB de Polymarket.

Polymarket requiere heartbeats periodicos para mantener las limit orders
vivas. Si no recibe un heartbeat en ~10 segundos, cancela automaticamente
todas las ordenes abiertas de esa sesion (Dead Man's Switch).

Se ejecuta como tarea asyncio en paralelo con el resto del bot.
"""

import asyncio
import uuid
from loguru import logger
from typing import Optional

from execution.clob_client import PolymarketClient


# ---------------------------------------------------------------------------
# Heartbeat Worker
# ---------------------------------------------------------------------------

class HeartbeatManager:
    """
    Envia heartbeats periodicos al CLOB de Polymarket.
    """

    def __init__(self, poly_client: PolymarketClient, interval: float = 5.0):
        """
        Args:
            poly_client: Cliente Polymarket inicializado
            interval: Segundos entre heartbeats (default 5, max seguro ~8)
        """
        self.poly_client = poly_client
        self.interval = interval
        self.session_id: str = str(uuid.uuid4())[:16]
        self.running: bool = False
        self.active: bool = False  # True cuando hay ordenes limit activas
        self._consecutive_failures: int = 0
        self._max_failures: int = 5

    async def start(self) -> None:
        """Inicia el loop de heartbeat. Corre indefinidamente."""
        self.running = True
        logger.info(f"Heartbeat manager iniciado | session={self.session_id} | interval={self.interval}s")

        while self.running:
            try:
                if self.active and self.poly_client.is_ready():
                    # Ejecutar en thread separado para no bloquear el event loop
                    await asyncio.to_thread(
                        self.poly_client.clob.post_heartbeat, self.session_id
                    )
                    self._consecutive_failures = 0
                    logger.debug(f"Heartbeat enviado | session={self.session_id}")
                
                await asyncio.sleep(self.interval)

            except Exception as e:
                self._consecutive_failures += 1
                logger.warning(
                    f"Heartbeat fallo ({self._consecutive_failures}/{self._max_failures}): {e}"
                )

                if self._consecutive_failures >= self._max_failures:
                    logger.error(
                        f"Heartbeat: {self._max_failures} fallos consecutivos. "
                        "Las limit orders pueden haber sido canceladas por Polymarket."
                    )
                    self._consecutive_failures = 0

                await asyncio.sleep(self.interval)

        logger.info("Heartbeat manager detenido")

    def activate(self) -> None:
        """Activa el envio de heartbeats (cuando hay limit orders abiertas)."""
        if not self.active:
            self.active = True
            logger.debug("Heartbeat activado — limit orders activas")

    def deactivate(self) -> None:
        """Desactiva el envio de heartbeats (sin limit orders)."""
        if self.active:
            self.active = False
            logger.debug("Heartbeat desactivado — sin limit orders")

    def stop(self) -> None:
        """Detiene el loop de heartbeat."""
        self.running = False
        self.active = False
