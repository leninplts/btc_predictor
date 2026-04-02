"""
execution/order_manager.py
--------------------------
Gestor de ordenes reales en Polymarket CLOB.

Ciclo de vida de una orden:
  1. place_order(decision) — recibe Decision del engine
  2. Si es limit: submit limit GTC → esperar fill (poll cada 2s, max 60s)
     - Si no se llena en 60s: cancelar → resubmit como market FOK
  3. Si es market: submit FOK → fill instantaneo o rechazo
  4. Retorna OrderResult con fill_price, shares, fees reales

Funciones adicionales:
  - cancel_order(order_id)
  - cancel_all_orders()
  - get_open_orders()
  - get_fills()
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Optional
from loguru import logger

from py_clob_client.clob_types import (
    OrderArgs, MarketOrderArgs, OrderType, OpenOrderParams
)
from py_clob_client.order_builder.constants import BUY

from execution.clob_client import PolymarketClient
from execution.heartbeat import HeartbeatManager


# ---------------------------------------------------------------------------
# Tipos
# ---------------------------------------------------------------------------

@dataclass
class OrderResult:
    """Resultado de una orden enviada."""
    success: bool
    order_id: str
    fill_price: float         # precio real al que se ejecuto
    shares_filled: float      # shares realmente compradas
    usdc_spent: float         # USDC total gastado
    fee_paid: float           # fee real pagado
    order_type: str           # "limit" o "market"
    was_upgraded: bool        # True si limit->market fallback
    error: str                # "" si no hubo error
    raw_response: dict        # respuesta cruda del API


# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------

LIMIT_ORDER_TIMEOUT_S = 60     # segundos max para esperar fill de limit
FILL_POLL_INTERVAL_S = 2       # segundos entre checks de fill
MARKET_ORDER_SLIPPAGE = 0.02   # 2% slippage max para market orders


# ---------------------------------------------------------------------------
# Order Manager
# ---------------------------------------------------------------------------

class OrderManager:
    """
    Gestiona el envio y monitoreo de ordenes reales en Polymarket.
    """

    def __init__(
        self,
        poly_client: PolymarketClient,
        heartbeat: HeartbeatManager,
    ):
        self.poly_client = poly_client
        self.heartbeat = heartbeat
        self.last_order_id: Optional[str] = None

    # -----------------------------------------------------------------------
    # Orden principal: recibe Decision, retorna OrderResult
    # -----------------------------------------------------------------------

    async def place_order(
        self,
        token_id: str,
        price: float,
        size: float,
        order_type: str,   # "limit" o "market"
    ) -> OrderResult:
        """
        Envia una orden a Polymarket.

        Si order_type="limit": envia limit GTC, espera fill hasta timeout,
        si no se llena cancela y reintenta como market FOK.

        Si order_type="market": envia market FOK directamente.

        Retorna OrderResult.
        """
        if not self.poly_client.is_ready():
            return self._error_result("Polymarket client no inicializado")

        if order_type == "limit":
            return await self._place_limit_with_fallback(token_id, price, size)
        else:
            return await self._place_market_order(token_id, size)

    # -----------------------------------------------------------------------
    # Limit order con fallback a market
    # -----------------------------------------------------------------------

    async def _place_limit_with_fallback(
        self, token_id: str, price: float, size: float
    ) -> OrderResult:
        """Envia limit order. Si no se llena en LIMIT_ORDER_TIMEOUT_S, cancela y envia market."""
        
        # 1. Enviar limit order
        limit_result = await self._submit_limit_order(token_id, price, size)
        
        if not limit_result.success:
            logger.warning(f"Limit order fallo: {limit_result.error}. Intentando market order...")
            return await self._place_market_order(token_id, size)

        order_id = limit_result.order_id
        self.heartbeat.activate()

        # 2. Esperar fill (poll)
        filled = await self._wait_for_fill(order_id, timeout_s=LIMIT_ORDER_TIMEOUT_S)

        if filled:
            self.heartbeat.deactivate()
            logger.success(f"Limit order filled | id={order_id[:16]}...")
            return limit_result

        # 3. No se lleno — cancelar y resubmit como market
        logger.info(f"Limit order timeout ({LIMIT_ORDER_TIMEOUT_S}s). Cancelando y enviando market...")
        await self._cancel_order(order_id)
        self.heartbeat.deactivate()

        market_result = await self._place_market_order(token_id, size)
        market_result.was_upgraded = True
        return market_result

    # -----------------------------------------------------------------------
    # Submit limit order
    # -----------------------------------------------------------------------

    async def _submit_limit_order(
        self, token_id: str, price: float, size: float
    ) -> OrderResult:
        """Envia una limit order GTC al CLOB."""
        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=round(price, 2),    # Polymarket acepta 2 decimales
                size=round(size, 2),
                side=BUY,
            )

            signed_order = self.poly_client.clob.create_order(order_args)
            response = self.poly_client.clob.post_order(
                signed_order, orderType=OrderType.GTC
            )

            # Parsear respuesta
            order_id = ""
            success = False

            if isinstance(response, dict):
                order_id = response.get("orderID", response.get("id", ""))
                success = response.get("success", bool(order_id))
                if not success:
                    error_msg = response.get("errorMsg", response.get("error", str(response)))
                    return self._error_result(f"Limit order rechazada: {error_msg}")
            elif isinstance(response, str):
                order_id = response
                success = True

            self.last_order_id = order_id

            logger.info(
                f"Limit order enviada | id={order_id[:16]}... | "
                f"{size:.1f} shares @ ${price:.3f} | token={token_id[:16]}..."
            )

            return OrderResult(
                success=True,
                order_id=order_id,
                fill_price=price,
                shares_filled=size,
                usdc_spent=price * size,
                fee_paid=0.0,  # se actualiza cuando se confirma fill
                order_type="limit",
                was_upgraded=False,
                error="",
                raw_response=response if isinstance(response, dict) else {"id": response},
            )

        except Exception as e:
            logger.error(f"Error enviando limit order: {e}")
            return self._error_result(str(e))

    # -----------------------------------------------------------------------
    # Submit market order (FOK)
    # -----------------------------------------------------------------------

    async def _place_market_order(self, token_id: str, size: float) -> OrderResult:
        """Envia una market order FOK (Fill or Kill)."""
        try:
            # amount = USDC a gastar (no shares)
            # Para market order, usamos el amount como referencia
            amount = round(size * 0.55, 2)  # estimacion conservadora del costo

            market_args = MarketOrderArgs(
                token_id=token_id,
                amount=amount,
                side=BUY,
            )

            signed_order = self.poly_client.clob.create_market_order(market_args)
            response = self.poly_client.clob.post_order(
                signed_order, orderType=OrderType.FOK
            )

            order_id = ""
            success = False

            if isinstance(response, dict):
                order_id = response.get("orderID", response.get("id", ""))
                success = response.get("success", bool(order_id))
                if not success:
                    error_msg = response.get("errorMsg", response.get("error", str(response)))
                    return self._error_result(f"Market order rechazada: {error_msg}")
            elif isinstance(response, str):
                order_id = response
                success = True

            self.last_order_id = order_id

            logger.info(
                f"Market order (FOK) enviada | id={order_id[:16]}... | "
                f"amount=${amount:.2f} | token={token_id[:16]}..."
            )

            return OrderResult(
                success=True,
                order_id=order_id,
                fill_price=0.0,    # se conoce despues del fill
                shares_filled=0.0,  # se conoce despues del fill
                usdc_spent=amount,
                fee_paid=0.0,
                order_type="market",
                was_upgraded=False,
                error="",
                raw_response=response if isinstance(response, dict) else {"id": response},
            )

        except Exception as e:
            logger.error(f"Error enviando market order: {e}")
            return self._error_result(str(e))

    # -----------------------------------------------------------------------
    # Wait for fill (polling)
    # -----------------------------------------------------------------------

    async def _wait_for_fill(self, order_id: str, timeout_s: int) -> bool:
        """
        Espera hasta que una orden se llene o expire el timeout.
        Retorna True si se lleno, False si timeout.
        """
        start = time.time()

        while (time.time() - start) < timeout_s:
            try:
                order = self.poly_client.clob.get_order(order_id)

                if isinstance(order, dict):
                    status = order.get("status", "").lower()
                    size_matched = float(order.get("size_matched", 0))

                    if status in ("matched", "filled", "closed") or size_matched > 0:
                        return True

                    if status in ("cancelled", "expired", "rejected"):
                        logger.warning(f"Orden {order_id[:16]}... status={status}")
                        return False

            except Exception as e:
                logger.debug(f"Error polling orden {order_id[:16]}...: {e}")

            await asyncio.sleep(FILL_POLL_INTERVAL_S)

        return False

    # -----------------------------------------------------------------------
    # Cancel
    # -----------------------------------------------------------------------

    async def _cancel_order(self, order_id: str) -> bool:
        """Cancela una orden especifica."""
        try:
            self.poly_client.clob.cancel(order_id)
            logger.info(f"Orden cancelada: {order_id[:16]}...")
            return True
        except Exception as e:
            logger.error(f"Error cancelando orden {order_id[:16]}...: {e}")
            return False

    async def cancel_all_orders(self) -> bool:
        """Cancela TODAS las ordenes abiertas."""
        if not self.poly_client.is_ready():
            return False

        try:
            self.poly_client.clob.cancel_all()
            self.heartbeat.deactivate()
            logger.success("Todas las ordenes canceladas")
            return True
        except Exception as e:
            logger.error(f"Error cancelando todas las ordenes: {e}")
            return False

    # -----------------------------------------------------------------------
    # Consultas
    # -----------------------------------------------------------------------

    async def get_open_orders(self) -> list:
        """Retorna lista de ordenes abiertas."""
        if not self.poly_client.is_ready():
            return []

        try:
            orders = self.poly_client.clob.get_orders(OpenOrderParams())
            return orders if isinstance(orders, list) else []
        except Exception as e:
            logger.error(f"Error obteniendo ordenes abiertas: {e}")
            return []

    async def get_trades(self) -> list:
        """Retorna historial de trades ejecutados."""
        if not self.poly_client.is_ready():
            return []

        try:
            trades = self.poly_client.clob.get_trades()
            return trades if isinstance(trades, list) else []
        except Exception as e:
            logger.error(f"Error obteniendo trades: {e}")
            return []

    # -----------------------------------------------------------------------
    # Utilidades
    # -----------------------------------------------------------------------

    def _error_result(self, error: str) -> OrderResult:
        """Crea un OrderResult de error."""
        return OrderResult(
            success=False,
            order_id="",
            fill_price=0.0,
            shares_filled=0.0,
            usdc_spent=0.0,
            fee_paid=0.0,
            order_type="",
            was_upgraded=False,
            error=error,
            raw_response={},
        )
