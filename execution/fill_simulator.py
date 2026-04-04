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

        Parametros:
          action       : "BUY_YES" o "BUY_NO"
          target_price : precio target de la limit order
          n_shares     : shares a comprar
          bids         : bids del order book (raw, como vienen de la DB/WS)
          asks         : asks del order book (raw, como vienen de la DB/WS)

        En Polymarket:
          - BUY_YES: compramos del lado ASK del book de YES
            Tu limit order dice "quiero comprar YES shares a maximo $target_price"
            Se matchea contra asks que tengan price <= target_price

          - BUY_NO: compramos del lado ASK del book de NO
            El book de NO es el inverso: comprar NO a $0.48 equivale a
            que alguien venda YES a $0.52 (1 - 0.48)
            Para simplificar, usamos el lado BID del book de YES como proxy:
            los bids de YES son "gente que quiere comprar YES" = "gente que
            esta dispuesta a venderte NO"

        Returns: SimulatedFill
        """
        cfg = self.config

        # Parsear book
        parsed_bids = _parse_book_side(bids)
        parsed_asks = _parse_book_side(asks)

        # Ordenar: bids desc (mejor primero), asks asc (mejor primero)
        parsed_bids.sort(key=lambda x: x[0], reverse=True)
        parsed_asks.sort(key=lambda x: x[0])

        # --- Validar que hay order book ---
        if not parsed_bids and not parsed_asks:
            return SimulatedFill(
                filled=False, fill_price=0, shares_filled=0,
                shares_requested=n_shares, fill_ratio=0, fee=0, slippage=0,
                reason="Order book vacio — no hay liquidez"
            )

        # --- Determinar que lado del book consumir ---
        if action == "BUY_YES":
            # Compramos YES: consumimos los asks (vendedores de YES)
            # Solo matcheamos asks con price <= target_price
            available = [(p, s) for p, s in parsed_asks if p <= target_price]
            if not available:
                # Intentar con asks cercanos al target (dentro de 2 centavos)
                close_asks = [(p, s) for p, s in parsed_asks if p <= target_price + 0.02]
                if close_asks:
                    return SimulatedFill(
                        filled=False, fill_price=0, shares_filled=0,
                        shares_requested=n_shares, fill_ratio=0, fee=0, slippage=0,
                        reason=f"No hay asks <= ${target_price:.4f}. "
                               f"Best ask: ${close_asks[0][0]:.4f} "
                               f"(+${close_asks[0][0] - target_price:.4f})"
                    )
                return SimulatedFill(
                    filled=False, fill_price=0, shares_filled=0,
                    shares_requested=n_shares, fill_ratio=0, fee=0, slippage=0,
                    reason=f"No hay asks disponibles cerca de ${target_price:.4f}"
                )
        else:
            # BUY_NO: compramos NO shares
            # El book de YES no tiene asks de NO directamente.
            # Proxy: los bids de YES son gente que quiere comprar YES,
            # lo que implica que estan dispuestos a "venderte" NO al complemento.
            # Precio de NO = 1 - precio del bid de YES
            # Filtramos bids de YES donde (1 - bid_price) <= target_price
            # Es decir, bid_price >= (1 - target_price)
            min_bid = 1.0 - target_price
            available = [
                (1.0 - p, s) for p, s in parsed_bids if p >= min_bid
            ]
            available.sort(key=lambda x: x[0])  # menor precio NO primero
            if not available:
                return SimulatedFill(
                    filled=False, fill_price=0, shares_filled=0,
                    shares_requested=n_shares, fill_ratio=0, fee=0, slippage=0,
                    reason=f"No hay liquidez para NO a ${target_price:.4f} "
                           f"(necesita bids YES >= ${min_bid:.4f})"
                )

        # --- Simular fill consumiendo niveles del book ---
        shares_remaining = n_shares
        total_cost = 0.0
        shares_filled = 0.0

        for price, size in available:
            if shares_remaining <= 0:
                break

            fill_at_level = min(shares_remaining, size)
            total_cost += fill_at_level * price
            shares_filled += fill_at_level
            shares_remaining -= fill_at_level

        # --- Evaluar resultado ---
        fill_ratio = shares_filled / n_shares if n_shares > 0 else 0.0

        # No fill si no se alcanzo el minimo
        if fill_ratio < cfg["min_fill_ratio"]:
            return SimulatedFill(
                filled=False, fill_price=0, shares_filled=0,
                shares_requested=n_shares, fill_ratio=fill_ratio, fee=0, slippage=0,
                reason=f"Liquidez insuficiente: solo se llenaria {fill_ratio:.0%} "
                       f"({shares_filled:.1f}/{n_shares:.1f} shares)"
            )

        # Precio promedio ponderado
        vwap = total_cost / shares_filled if shares_filled > 0 else target_price

        # Agregar slippage por latencia
        vwap += cfg["latency_slippage"]
        vwap = min(0.99, max(0.01, vwap))

        # Verificar price impact
        slippage = vwap - target_price
        price_impact_pct = abs(slippage) / target_price if target_price > 0 else 0

        if price_impact_pct > cfg["max_price_impact_pct"]:
            return SimulatedFill(
                filled=False, fill_price=vwap, shares_filled=0,
                shares_requested=n_shares, fill_ratio=0, fee=0,
                slippage=slippage,
                reason=f"Price impact excesivo: {price_impact_pct:.1%} "
                       f"(target=${target_price:.4f}, fill=${vwap:.4f}, "
                       f"max={cfg['max_price_impact_pct']:.0%})"
            )

        # --- Fill exitoso ---
        # Si no se permiten partial fills, usar shares completas
        if not cfg["allow_partial_fills"] and shares_filled < n_shares:
            # Aun asi aceptamos si el ratio es >= min_fill_ratio
            # pero ajustamos shares al total (asumimos que el resto
            # se llenaria en los segundos siguientes a precio similar)
            shares_filled = n_shares
            total_cost = n_shares * vwap

        # Fee sobre el fill real
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
            reason=f"Fill simulado: {shares_filled:.1f} shares @ ${vwap:.4f} "
                   f"(target=${target_price:.4f}, slippage=${slippage:+.4f}, "
                   f"fee=${total_fee:.4f})"
        )
