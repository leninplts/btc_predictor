"""
strategy/sizing.py
------------------
Gestion del tamano de posicion usando Kelly Criterion fraccional.

Kelly Criterion:
  f* = (p * b - q) / b
  donde:
    p = probabilidad de ganar (del modelo)
    q = 1 - p
    b = ratio de ganancia neto (payout / riesgo)

En Polymarket:
  - Si compras YES a $0.48 y ganas: recibes $1.00 → ganancia = $0.52
  - Si pierdes: recibes $0.00 → perdida = $0.48
  - b = 0.52 / 0.48 = 1.083

Kelly fraccional:
  Usar solo una fraccion (25-50%) del Kelly completo para reducir
  volatilidad y proteger contra errores del modelo.

Controles de riesgo:
  - Maximo % del capital por operacion (hard cap)
  - Minimo de capital (no operar si queda muy poco)
  - Ajuste por regimen choppy (reduce tamano)
"""

from dataclasses import dataclass
from typing import Optional
from loguru import logger

from models.backtester import polymarket_fee


# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------

DEFAULT_SIZING_CONFIG = {
    "kelly_fraction":     0.35,    # usar 35% del Kelly completo
    "max_risk_per_trade": 0.05,    # maximo 5% del capital por trade
    "min_risk_per_trade": 0.005,   # minimo 0.5% (para que valga la pena)
    "min_capital":        10.0,    # no operar si capital < $10 USDC
    "min_shares":         5.0,     # Polymarket requiere minimo 5 shares por orden
    "choppy_multiplier":  0.5,     # reducir tamano 50% en regimen choppy
}


# ---------------------------------------------------------------------------
# Tipos
# ---------------------------------------------------------------------------

@dataclass
class PositionSize:
    """Resultado del calculo de sizing."""
    usdc_amount: float       # monto en USDC a invertir
    n_shares: float          # cantidad de shares a comprar
    kelly_raw: float         # Kelly completo (antes de fraccion)
    kelly_fraction: float    # Kelly fraccional aplicado
    risk_pct: float          # % del capital que se esta arriesgando
    fee_estimated: float     # fee estimado de Polymarket
    reason: str              # explicacion


# ---------------------------------------------------------------------------
# Calculador de sizing
# ---------------------------------------------------------------------------

class PositionSizer:
    """
    Calcula cuantos shares comprar para cada operacion.
    """

    def __init__(self, config: Optional[dict] = None):
        self.config = {**DEFAULT_SIZING_CONFIG, **(config or {})}

    def calculate(
        self,
        capital: float,
        prob_win: float,
        buy_price: float,
        is_choppy: bool = False
    ) -> PositionSize:
        """
        Calcula el tamano de la posicion.

        Parametros:
          capital   : USDC disponible
          prob_win  : probabilidad de ganar (del modelo, 0.5 a 1.0)
          buy_price : precio al que vamos a comprar el share (0.01 a 0.99)
          is_choppy : True si el regimen es choppy (reduce tamano)

        Retorna PositionSize con el monto a invertir.
        """
        cfg = self.config

        # ----- Check: capital minimo -----
        if capital < cfg["min_capital"]:
            return PositionSize(
                usdc_amount=0, n_shares=0, kelly_raw=0, kelly_fraction=0,
                risk_pct=0, fee_estimated=0,
                reason=f"Capital ${capital:.2f} < minimo ${cfg['min_capital']:.2f}"
            )

        # ----- Check: precio valido -----
        if buy_price <= 0.01 or buy_price >= 0.99:
            return PositionSize(
                usdc_amount=0, n_shares=0, kelly_raw=0, kelly_fraction=0,
                risk_pct=0, fee_estimated=0,
                reason=f"Precio {buy_price:.3f} fuera de rango operativo [0.01, 0.99]"
            )

        # ----- Kelly Criterion -----
        # b = ganancia neta si gano / perdida si pierdo
        # Si compro a buy_price y gano: recibo $1.00, ganancia = 1.0 - buy_price
        # Si pierdo: pierdo buy_price
        b = (1.0 - buy_price) / buy_price

        p = prob_win
        q = 1.0 - p

        kelly_raw = (p * b - q) / b if b > 0 else 0.0

        # Kelly puede ser negativo si la apuesta no tiene valor esperado positivo
        if kelly_raw <= 0:
            return PositionSize(
                usdc_amount=0, n_shares=0, kelly_raw=kelly_raw, kelly_fraction=0,
                risk_pct=0, fee_estimated=0,
                reason=f"Kelly negativo ({kelly_raw:.4f}) — apuesta sin valor esperado positivo"
            )

        # ----- Fraccion Kelly -----
        kelly_frac = kelly_raw * cfg["kelly_fraction"]

        # ----- Ajuste por regimen choppy -----
        if is_choppy:
            kelly_frac *= cfg["choppy_multiplier"]

        # ----- Limitar al rango permitido -----
        risk_pct = max(cfg["min_risk_per_trade"], min(kelly_frac, cfg["max_risk_per_trade"]))

        # ----- Calcular monto total disponible para esta operacion -----
        usdc_budget = capital * risk_pct

        # ----- Fee estimado -----
        fee_per_share = polymarket_fee(buy_price)

        # Calcular cuantas shares podemos comprar con el budget
        # budget = n_shares * buy_price + n_shares * fee_per_share
        # budget = n_shares * (buy_price + fee_per_share)
        cost_per_share = buy_price + fee_per_share
        if cost_per_share <= 0:
            return PositionSize(
                usdc_amount=0, n_shares=0, kelly_raw=kelly_raw, kelly_fraction=kelly_frac,
                risk_pct=risk_pct, fee_estimated=0,
                reason="Costo por share invalido"
            )

        n_shares_net = usdc_budget / cost_per_share

        # ----- Floor: minimo de shares (Polymarket requiere >= 5) -----
        min_shares = cfg["min_shares"]
        adjusted = False

        if n_shares_net < min_shares:
            n_shares_net = min_shares
            adjusted = True

        # Calcular costos finales
        shares_cost = n_shares_net * buy_price     # costo puro de shares
        fee_total = fee_per_share * n_shares_net    # fee aparte
        total_cost = shares_cost + fee_total        # total debitado del capital

        # Verificar que el capital alcance
        if total_cost > capital:
            return PositionSize(
                usdc_amount=0, n_shares=0, kelly_raw=kelly_raw,
                kelly_fraction=kelly_frac, risk_pct=risk_pct,
                fee_estimated=fee_total,
                reason=(
                    f"Minimo {min_shares:.0f} shares requiere "
                    f"${total_cost:.2f} pero capital es ${capital:.2f}"
                )
            )

        risk_pct = total_cost / capital

        reason_parts = [
            f"Kelly raw={kelly_raw:.4f}",
            f"frac={kelly_frac:.4f}",
            f"risk={risk_pct:.3%} of ${capital:.2f}",
            f"shares=${shares_cost:.2f} + fee=${fee_total:.4f} = ${total_cost:.2f}",
            f"{n_shares_net:.1f} shares @ {buy_price:.3f}",
        ]
        if adjusted:
            reason_parts.append(f"AJUSTADO min {min_shares:.0f} shares")
        if is_choppy:
            reason_parts.append("CHOPPY x0.5")

        return PositionSize(
            usdc_amount=round(shares_cost, 4),     # solo costo de shares (sin fee)
            n_shares=round(n_shares_net, 2),
            kelly_raw=round(kelly_raw, 6),
            kelly_fraction=round(kelly_frac, 6),
            risk_pct=round(risk_pct, 6),
            fee_estimated=round(fee_total, 6),
            reason=" | ".join(reason_parts)
        )
