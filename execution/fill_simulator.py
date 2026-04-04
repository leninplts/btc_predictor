"""
execution/fill_simulator.py
---------------------------
Simulador de fill realista para paper trading.

En vez de asumir que la orden se llena al precio target (ideal),
consulta el order book real y simula:
  1. Si hay suficiente liquidez para llenar la orden
  2. A que precio real se ejecutaria (price impact)
  3. Si la orden se llenaria o no (fill probability)

Esto hace que el paper wallet refleje condiciones reales de mercado:
  - No fill si no hay liquidez
  - Precio peor si la orden es grande vs la liquidez disponible
  - Slippage basado en el spread real

Formato del order book:
  bids = [{"price": 0.52, "size": 100}, {"price": 0.51, "size": 200}, ...]
  asks = [{"price": 0.53, "size": 150}, {"price": 0.54, "size": 300}, ...]
"""

import json
from dataclasses import dataclass
from typing import Optional
from loguru import logger


# ---------------------------------------------------------------------------
# Tipos
# ---------------------------------------------------------------------------

@dataclass
class SimulatedFill:
    """Resultado de una simulacion de fill."""
    filled: bool              # True si la orden se lleno (parcial o total)
    fill_price: float         # precio promedio ponderado de ejecucion
    shares_filled: float      # shares que se llenaron
    shares_requested: float   # shares que se pidieron originalmente
    fill_ratio: float         # shares_filled / shares_requested (0.0 a 1.0)
    fee: float                # fee calculado sobre el fill real
    slippage: float           # diferencia entre target_price y fill_price
    reason: str               # explicacion legible
    was_retry: bool = False   # True si se lleno en el retry (+$0.02)


# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    # Minimo de liquidez disponible para considerar que la orden se llena
    # Si el order book tiene menos de este % de las shares pedidas, no fill
    "min_fill_ratio":           0.50,   # al menos llenar 50% de la orden

    # Slippage extra (simula latencia entre decision y ejecucion)
    # Se suma al price impact del order book
    "latency_slippage":         0.002,  # 0.2 centavos por latencia

    # Spread maximo para intentar fill
    "max_spread_for_fill":      0.10,   # no intentar si spread > 10 centavos

    # Si el price impact supera este %, se rechaza el fill
    "max_price_impact_pct":     0.05,   # 5% maximo de price impact

    # Usar partial fills (True) o solo fills completos (False)
    "allow_partial_fills":      False,  # Polymarket no soporta partial en limit

    # Retry: si el primer intento falla, reintentar con precio mas agresivo
    # Igual que order_manager real: +$0.02 por retry
    "retry_on_fail":            True,   # habilitar retry (como la cuenta real)
    "retry_price_bump":         0.02,   # +2 centavos en el retry
}


# ---------------------------------------------------------------------------
# Parser de order book
# ---------------------------------------------------------------------------

def _parse_book_side(raw) -> list[tuple[float, float]]:
    """
    Parsea un lado del book (bids o asks) a lista de (price, size).
    Acepta: JSON string, lista de dicts [{price, size}], o lista de tuples.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return []
    if not isinstance(raw, list):
        return []

    result = []
    for entry in raw:
        try:
            if isinstance(entry, (list, tuple)):
                p, s = float(entry[0]), float(entry[1])
            elif isinstance(entry, dict):
                p = float(entry.get("price", entry.get("p", 0)))
                s = float(entry.get("size", entry.get("s", 0)))
            else:
                continue
            if p > 0 and s > 0:
                result.append((p, s))
        except (ValueError, TypeError, IndexError):
            continue
    return result


# ---------------------------------------------------------------------------
# Fee de Polymarket (misma formula que backtester)
# ---------------------------------------------------------------------------

def _polymarket_fee(price: float) -> float:
    """Fee por share a un precio dado."""
    if price <= 0 or price >= 1:
        return 0.0
    return 2.0 * price * (1.0 - price) * 0.0312


# ---------------------------------------------------------------------------
# Simulador principal
# ---------------------------------------------------------------------------

class FillSimulator:
    """
    Simula el fill de una orden contra el order book real.

    Replica el comportamiento del OrderManager real:
      1. Intenta fill al target_price
      2. Si falla y retry_on_fail=True, reintenta a target_price + retry_price_bump
      3. Si ambos fallan, reporta no-fill
    """

    def __init__(self, config: Optional[dict] = None):
        self.config = {**DEFAULT_CONFIG, **(config or {})}

    def simulate_buy(
        self,
        action: str,
        target_price: float,
        n_shares: float,
        bids: list,
        asks: list,
    ) -> SimulatedFill:
        """
        Simula una orden de compra contra el order book real.
        Si el primer intento falla, reintenta con precio +$0.02 (como la cuenta real).

        Parametros:
          action       : "BUY_YES" o "BUY_NO"
          target_price : precio target de la limit order
          n_shares     : shares a comprar
          bids         : bids del order book (raw, como vienen de la DB/WS)
          asks         : asks del order book (raw, como vienen de la DB/WS)

        Returns: SimulatedFill
        """
        cfg = self.config

        # Parsear book una sola vez
        parsed_bids = _parse_book_side(bids)
        parsed_asks = _parse_book_side(asks)
        parsed_bids.sort(key=lambda x: x[0], reverse=True)
        parsed_asks.sort(key=lambda x: x[0])

        # --- Validar que hay order book ---
        if not parsed_bids and not parsed_asks:
            return SimulatedFill(
                filled=False, fill_price=0, shares_filled=0,
                shares_requested=n_shares, fill_ratio=0, fee=0, slippage=0,
                reason="Order book vacio — no hay liquidez"
            )

        # --- Intento 1: al target_price ---
        result = self._try_fill(
            action, target_price, n_shares, parsed_bids, parsed_asks,
        )
        if result.filled:
            return result

        # --- Intento 2 (retry): al target_price + bump ---
        if cfg["retry_on_fail"]:
            retry_price = min(0.99, target_price + cfg["retry_price_bump"])
            retry_result = self._try_fill(
                action, retry_price, n_shares, parsed_bids, parsed_asks,
            )
            if retry_result.filled:
                # Marcar que fue retry y calcular slippage vs precio ORIGINAL
                retry_result.was_retry = True
                retry_result.slippage = round(
                    retry_result.fill_price - target_price, 6
                )
                retry_result.reason = (
                    f"Fill en retry: {retry_result.shares_filled:.1f} shares "
                    f"@ ${retry_result.fill_price:.4f} "
                    f"(original=${target_price:.4f}, retry=${retry_price:.4f}, "
                    f"slippage=${retry_result.slippage:+.4f}, "
                    f"fee=${retry_result.fee:.4f})"
                )
                return retry_result

            # Ambos intentos fallaron — reportar con detalle
            return SimulatedFill(
                filled=False, fill_price=0, shares_filled=0,
                shares_requested=n_shares, fill_ratio=0, fee=0, slippage=0,
                reason=f"No fill en 2 intentos "
                       f"(${target_price:.4f}, ${retry_price:.4f}). "
                       f"1er: {result.reason} | 2do: {retry_result.reason}"
            )

        return result

    # -------------------------------------------------------------------
    # Intento individual de fill contra el book
    # -------------------------------------------------------------------

    def _try_fill(
        self,
        action: str,
        price: float,
        n_shares: float,
        parsed_bids: list[tuple[float, float]],
        parsed_asks: list[tuple[float, float]],
    ) -> SimulatedFill:
        """
        Un intento individual de fill al precio dado.

        En Polymarket:
          - BUY_YES: consumimos los asks (vendedores de YES)
            Matcheamos asks con price <= nuestro precio target
          - BUY_NO: usamos bids de YES como proxy
            Los bids de YES = gente dispuesta a venderte NO
            Precio NO = 1 - bid_price_YES

        Returns: SimulatedFill
        """
        cfg = self.config

        # --- Determinar que lado del book consumir ---
        if action == "BUY_YES":
            available = [(p, s) for p, s in parsed_asks if p <= price]
            if not available:
                best_ask = parsed_asks[0][0] if parsed_asks else None
                if best_ask is not None:
                    return SimulatedFill(
                        filled=False, fill_price=0, shares_filled=0,
                        shares_requested=n_shares, fill_ratio=0, fee=0,
                        slippage=0,
                        reason=f"No hay asks <= ${price:.4f} "
                               f"(best ask: ${best_ask:.4f})"
                    )
                return SimulatedFill(
                    filled=False, fill_price=0, shares_filled=0,
                    shares_requested=n_shares, fill_ratio=0, fee=0, slippage=0,
                    reason=f"No hay asks disponibles"
                )
        else:
            min_bid = 1.0 - price
            available = [
                (1.0 - p, s) for p, s in parsed_bids if p >= min_bid
            ]
            available.sort(key=lambda x: x[0])
            if not available:
                return SimulatedFill(
                    filled=False, fill_price=0, shares_filled=0,
                    shares_requested=n_shares, fill_ratio=0, fee=0, slippage=0,
                    reason=f"No hay liquidez NO a ${price:.4f} "
                           f"(necesita bids YES >= ${min_bid:.4f})"
                )

        # --- Simular fill consumiendo niveles del book ---
        shares_remaining = n_shares
        total_cost = 0.0
        shares_filled = 0.0

        for level_price, size in available:
            if shares_remaining <= 0:
                break
            fill_at_level = min(shares_remaining, size)
            total_cost += fill_at_level * level_price
            shares_filled += fill_at_level
            shares_remaining -= fill_at_level

        # --- Evaluar resultado ---
        fill_ratio = shares_filled / n_shares if n_shares > 0 else 0.0

        if fill_ratio < cfg["min_fill_ratio"]:
            return SimulatedFill(
                filled=False, fill_price=0, shares_filled=0,
                shares_requested=n_shares, fill_ratio=fill_ratio, fee=0,
                slippage=0,
                reason=f"Liquidez insuficiente: {fill_ratio:.0%} "
                       f"({shares_filled:.1f}/{n_shares:.1f} shares)"
            )

        # Precio promedio ponderado + slippage latencia
        vwap = total_cost / shares_filled if shares_filled > 0 else price
        vwap += cfg["latency_slippage"]
        vwap = min(0.99, max(0.01, vwap))

        # Verificar price impact
        slippage = vwap - price
        price_impact_pct = abs(slippage) / price if price > 0 else 0

        if price_impact_pct > cfg["max_price_impact_pct"]:
            return SimulatedFill(
                filled=False, fill_price=vwap, shares_filled=0,
                shares_requested=n_shares, fill_ratio=0, fee=0,
                slippage=slippage,
                reason=f"Price impact: {price_impact_pct:.1%} "
                       f"(${price:.4f}→${vwap:.4f}, max={cfg['max_price_impact_pct']:.0%})"
            )

        # --- Fill exitoso ---
        if not cfg["allow_partial_fills"] and shares_filled < n_shares:
            shares_filled = n_shares
            total_cost = n_shares * vwap

        fee_per_share = _polymarket_fee(vwap)
        total_fee = fee_per_share * shares_filled

        return SimulatedFill(
            filled=True,
            fill_price=round(vwap, 6),
            shares_filled=round(shares_filled, 2),
            shares_requested=n_shares,
            fill_ratio=round(fill_ratio, 4),
            fee=round(total_fee, 6),
            slippage=round(slippage, 6),
            reason=f"Fill: {shares_filled:.1f} shares @ ${vwap:.4f} "
                   f"(target=${price:.4f}, slippage=${slippage:+.4f}, "
                   f"fee=${total_fee:.4f})",
            was_retry=False,
        )
