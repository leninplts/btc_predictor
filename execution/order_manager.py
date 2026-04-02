"""
execution/order_manager.py
--------------------------
Gestor de ordenes reales en Polymarket CLOB.

Ciclo de vida de una orden:
  1. place_order(decision) — recibe parametros de la Decision del engine
  2. Si es limit: submit limit GTC -> esperar fill (poll cada 2s, max 60s)
     - Si no se llena en 60s: cancelar -> resubmit como market FOK
  3. Si es market: submit FOK -> fill instantaneo o rechazo
  4. Re-consulta la orden para obtener datos reales de ejecucion
  5. Retorna OrderResult con fill_price, shares, fees reales

IMPORTANTE: Todas las llamadas al SDK de py_clob_client son sincronas.
Se envuelven en asyncio.to_thread() para no bloquear el event loop.

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


# ---------------------------------------------------------------------------
# Order Manager
# ---------------------------------------------------------------------------

class OrderManager:
    """
    Gestiona el envio y monitoreo de ordenes reales en Polymarket.
    Todas las llamadas al SDK se ejecutan via asyncio.to_thread()
    para no bloquear el event loop.
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
    # Helpers: ejecutar SDK calls sin bloquear asyncio
    # -----------------------------------------------------------------------

    async def _run_sync(self, func, *args, **kwargs):
        """Ejecuta una funcion sincrona del SDK en un thread separado."""
        return await asyncio.to_thread(func, *args, **kwargs)

    # -----------------------------------------------------------------------
    # Orden principal: recibe parametros, retorna OrderResult
    # -----------------------------------------------------------------------

    async def place_order(
        self,
        token_id: str,
        price: float,
        size: float,
        order_type: str,       # "limit" o "market"
        usdc_amount: float = 0.0,  # USDC real a gastar (para market orders)
    ) -> OrderResult:
        """
        Envia una orden a Polymarket.

        Args:
            token_id: ID del token YES/NO a comprar
            price: precio target por share (0.01-0.99)
            size: cantidad de shares
            order_type: "limit" o "market"
            usdc_amount: USDC total a gastar (usado en market orders)

        Si order_type="limit": envia limit GTC, espera fill hasta timeout,
        si no se llena cancela y reintenta como market FOK.

        Si order_type="market": envia market FOK directamente.
        """
        if not self.poly_client.is_ready():
            return self._error_result("Polymarket client no inicializado")

        if order_type == "limit":
            return await self._place_limit_with_fallback(
                token_id, price, size, usdc_amount
            )
        else:
            return await self._place_market_order(token_id, usdc_amount or (price * size))

    # -----------------------------------------------------------------------
    # Limit order con fallback a market
    # -----------------------------------------------------------------------

    async def _place_limit_with_fallback(
        self, token_id: str, price: float, size: float, usdc_amount: float
    ) -> OrderResult:
        """Envia limit order. Si no se llena en timeout, cancela y envia market."""

        # 1. Enviar limit order
        limit_result = await self._submit_limit_order(token_id, price, size)

        if not limit_result.success:
            logger.warning(f"Limit order fallo: {limit_result.error}. Intentando market...")
            return await self._place_market_order(token_id, usdc_amount or (price * size))

        order_id = limit_result.order_id
        self.heartbeat.activate()

        # 2. Esperar fill (poll)
        filled = await self._wait_for_fill(order_id, timeout_s=LIMIT_ORDER_TIMEOUT_S)

        if filled:
            self.heartbeat.deactivate()
            # Re-consultar para datos reales de ejecucion
            real_result = await self._fetch_fill_details(order_id, limit_result)
            logger.success(
                f"Limit order filled | id={order_id[:16]}... | "
                f"shares={real_result.shares_filled:.1f} @ ${real_result.fill_price:.4f}"
            )
            return real_result

        # 3. No se lleno — cancelar y resubmit como market
        logger.info(f"Limit order timeout ({LIMIT_ORDER_TIMEOUT_S}s). Cancelando...")
        await self._cancel_order(order_id)
        self.heartbeat.deactivate()

        market_result = await self._place_market_order(token_id, usdc_amount or (price * size))
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
            # NO redondear el precio a 2 decimales arbitrariamente.
            # El SDK valida internamente contra el tick_size del mercado.
            # Solo aseguramos que el precio este en rango valido.
            price = max(0.01, min(0.99, price))

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=round(size, 2),
                side=BUY,
            )

            signed_order = await self._run_sync(
                self.poly_client.clob.create_order, order_args
            )
            response = await self._run_sync(
                self.poly_client.clob.post_order, signed_order, OrderType.GTC
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
                f"{size:.1f} shares @ ${price:.4f} | token={token_id[:16]}..."
            )

            # Retornar datos preliminares; se actualizan en _fetch_fill_details
            return OrderResult(
                success=True,
                order_id=order_id,
                fill_price=price,           # preliminar, se actualiza con fill real
                shares_filled=size,          # preliminar
                usdc_spent=price * size,     # preliminar
                fee_paid=0.0,
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

    async def _place_market_order(self, token_id: str, usdc_amount: float) -> OrderResult:
        """Envia una market order FOK (Fill or Kill).

        Args:
            token_id: token a comprar
            usdc_amount: USDC total a gastar (viene de decision.usdc_amount)
        """
        try:
            amount = round(max(usdc_amount, 1.0), 2)

            market_args = MarketOrderArgs(
                token_id=token_id,
                amount=amount,
                side=BUY,
            )

            signed_order = await self._run_sync(
                self.poly_client.clob.create_market_order, market_args
            )
            response = await self._run_sync(
                self.poly_client.clob.post_order, signed_order, OrderType.FOK
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

            # Para market orders, intentar obtener detalles del fill
            result = OrderResult(
                success=True,
                order_id=order_id,
                fill_price=0.0,
                shares_filled=0.0,
                usdc_spent=amount,
                fee_paid=0.0,
                order_type="market",
                was_upgraded=False,
                error="",
                raw_response=response if isinstance(response, dict) else {"id": response},
            )

            # Intentar obtener detalles reales (FOK deberia estar filled inmediatamente)
            if order_id:
                await asyncio.sleep(1)  # breve espera para propagacion
                result = await self._fetch_fill_details(order_id, result)

            return result

        except Exception as e:
            logger.error(f"Error enviando market order: {e}")
            return self._error_result(str(e))

    # -----------------------------------------------------------------------
    # Fetch fill details: re-consulta la orden para datos reales
    # -----------------------------------------------------------------------

    async def _fetch_fill_details(
        self, order_id: str, preliminary: OrderResult
    ) -> OrderResult:
        """
        Re-consulta una orden para obtener datos reales de ejecucion.
        Si falla, retorna los datos preliminares sin cambios.
        """
        try:
            order = await self._run_sync(self.poly_client.clob.get_order, order_id)

            if isinstance(order, dict):
                size_matched = float(order.get("size_matched", 0))
                price = float(order.get("price", preliminary.fill_price))
                # Calcular USDC gastado basado en shares y precio reales
                if size_matched > 0:
                    preliminary.shares_filled = size_matched
                    preliminary.fill_price = price
                    preliminary.usdc_spent = size_matched * price

                # Intentar extraer fee si esta disponible
                fee = order.get("fee", order.get("fee_rate_bps", 0))
                if fee:
                    preliminary.fee_paid = float(fee)

                # Actualizar raw response
                preliminary.raw_response = order

        except Exception as e:
            logger.debug(f"No se pudieron obtener detalles del fill: {e}")

        return preliminary

    # -----------------------------------------------------------------------
    # Wait for fill (polling) — no bloquea event loop
    # -----------------------------------------------------------------------

    async def _wait_for_fill(self, order_id: str, timeout_s: int) -> bool:
        """
        Espera hasta que una orden se llene o expire el timeout.
        Retorna True si se lleno, False si timeout.
        """
        start = time.time()

        while (time.time() - start) < timeout_s:
            try:
                order = await self._run_sync(
                    self.poly_client.clob.get_order, order_id
                )

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
    # Cancel — no bloquea event loop
    # -----------------------------------------------------------------------

    async def _cancel_order(self, order_id: str) -> bool:
        """Cancela una orden especifica."""
        try:
            await self._run_sync(self.poly_client.clob.cancel, order_id)
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
            await self._run_sync(self.poly_client.clob.cancel_all)
            self.heartbeat.deactivate()
            logger.success("Todas las ordenes canceladas")
            return True
        except Exception as e:
            logger.error(f"Error cancelando todas las ordenes: {e}")
            return False

    # -----------------------------------------------------------------------
    # Consultas — no bloquean event loop
    # -----------------------------------------------------------------------

    async def get_open_orders(self) -> list:
        """Retorna lista de ordenes abiertas."""
        if not self.poly_client.is_ready():
            return []

        try:
            orders = await self._run_sync(
                self.poly_client.clob.get_orders, OpenOrderParams()
            )
            return orders if isinstance(orders, list) else []
        except Exception as e:
            logger.error(f"Error obteniendo ordenes abiertas: {e}")
            return []

    async def get_trades(self) -> list:
        """Retorna historial de trades ejecutados."""
        if not self.poly_client.is_ready():
            return []

        try:
            trades = await self._run_sync(self.poly_client.clob.get_trades)
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
