"""
strategy/signal.py
------------------
Generador de senales de trading.

Recibe la prediccion del modelo + estado del mercado y decide:
  - BUY_YES  : comprar share YES (apuesta a que BTC sube)
  - BUY_NO   : comprar share NO  (apuesta a que BTC baja)
  - SKIP     : no operar este intervalo

Estrategia hibrida:
  - Usa limit orders para mejorar el precio de entrada
  - El precio limite se calcula como midpoint - offset (intenta comprar
    mas barato que el precio actual del mercado)
  - Si la confianza es muy alta, usa market order (urgencia)
"""

from dataclasses import dataclass
from typing import Optional
from loguru import logger

from strategy.regime_filter import RegimeState


# ---------------------------------------------------------------------------
# Tipos
# ---------------------------------------------------------------------------

@dataclass
class Signal:
    """Senal de trading producida por el generador."""
    action: str             # "BUY_YES" | "BUY_NO" | "SKIP"
    token_id: str           # asset_id del token a comprar ("" si SKIP)
    order_type: str         # "limit" | "market"
    target_price: float     # precio al que poner la limit order (0 si market/skip)
    confidence: float       # confianza del modelo (0.5 a 1.0)
    prob_up: float          # P(BTC sube)
    prob_down: float        # P(BTC baja)
    reason: str             # explicacion legible de la decision


# ---------------------------------------------------------------------------
# Configuracion por defecto
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    # Umbrales de confianza
    "min_confidence":       0.55,   # minimo para operar
    "high_confidence":      0.70,   # umbral para market order en vez de limit
    "neutral_zone_low":     0.45,   # por debajo de esto: NO seguro
    "neutral_zone_high":    0.55,   # por encima de esto: YES seguro

    # Limit order pricing
    "limit_offset":         0.01,   # comprar N centavos mas barato que midpoint
    "limit_offset_high_conf": 0.005, # offset menor si confianza alta (mas agresivo)

    # Spread maximo para operar
    "max_spread":           0.10,   # no operar si spread > 10 centavos

    # Regimen
    "skip_low_vol":         True,   # no operar en regimen de baja volatilidad
    "reduce_in_choppy":     True,   # reducir tamano en regimen choppy
}


# ---------------------------------------------------------------------------
# Generador de senales
# ---------------------------------------------------------------------------

class SignalGenerator:
    """
    Genera senales de trading basadas en la prediccion del modelo,
    el estado del order book, y el regimen de mercado.
    """

    def __init__(self, config: Optional[dict] = None):
        self.config = {**DEFAULT_CONFIG, **(config or {})}

    def generate(
        self,
        prob_up: float,
        prob_down: float,
        confidence: float,
        asset_id_yes: str,
        asset_id_no: str,
        ob_midpoint: float = 0.50,
        ob_spread: float = 0.04,
        regime: Optional[RegimeState] = None,
    ) -> Signal:
        """
        Genera una senal de trading.

        Parametros:
          prob_up       : P(BTC sube) del modelo
          prob_down     : P(BTC baja) del modelo
          confidence    : max(prob_up, prob_down)
          asset_id_yes  : token ID del share YES
          asset_id_no   : token ID del share NO
          ob_midpoint   : precio midpoint actual del order book
          ob_spread     : spread actual (best_ask - best_bid)
          regime        : estado del regimen de volatilidad (opcional)
        """
        cfg = self.config

        # ----- Check 1: Confianza minima -----
        if confidence < cfg["min_confidence"]:
            return Signal(
                action="SKIP", token_id="", order_type="",
                target_price=0.0, confidence=confidence,
                prob_up=prob_up, prob_down=prob_down,
                reason=f"Confianza {confidence:.3f} < minimo {cfg['min_confidence']}"
            )

        # ----- Check 2: Spread maximo -----
        if ob_spread > cfg["max_spread"]:
            return Signal(
                action="SKIP", token_id="", order_type="",
                target_price=0.0, confidence=confidence,
                prob_up=prob_up, prob_down=prob_down,
                reason=f"Spread {ob_spread:.3f} > maximo {cfg['max_spread']}"
            )

        # ----- Check 3: Regimen de mercado -----
        if regime is not None:
            if cfg["skip_low_vol"] and regime.regime == "low_vol":
                return Signal(
                    action="SKIP", token_id="", order_type="",
                    target_price=0.0, confidence=confidence,
                    prob_up=prob_up, prob_down=prob_down,
                    reason=f"Regimen low_vol (ATR={regime.atr:.6f}) — saltando"
                )

        # ----- Determinar direccion -----
        if prob_up > prob_down:
            action = "BUY_YES"
            token_id = asset_id_yes
            buy_side_price = ob_midpoint  # el share YES esta cerca de midpoint
        else:
            action = "BUY_NO"
            token_id = asset_id_no
            buy_side_price = 1.0 - ob_midpoint  # el share NO es el complemento

        # ----- Tipo de orden -----
        # Siempre limit order: los mercados BTC 5-min de Polymarket tienen
        # poca liquidez y las market orders (FOK) fallan con "no match".
        # Con alta confianza usamos un offset menor (mas agresivo, mas probable fill).
        if confidence >= cfg["high_confidence"]:
            offset = cfg["limit_offset_high_conf"]
            reason_price = f"limit agresivo @ offset={offset} (alta confianza)"
        else:
            offset = cfg["limit_offset"]
            reason_price = f"limit @ offset={offset}"

        target_price = max(0.01, min(0.99, buy_side_price - offset))
        order_type = "limit"
        reason_price += f" | precio={target_price:.4f} (midpoint={buy_side_price:.3f})"

        # ----- Ajuste por regimen choppy -----
        regime_note = ""
        if regime is not None and cfg["reduce_in_choppy"] and regime.regime == "choppy":
            regime_note = " | CHOPPY: sizing reducido"

        reason = (
            f"{action} prob_up={prob_up:.3f} prob_down={prob_down:.3f} "
            f"conf={confidence:.3f} | {reason_price}{regime_note}"
        )

        return Signal(
            action=action,
            token_id=token_id,
            order_type=order_type,
            target_price=round(target_price, 4),
            confidence=confidence,
            prob_up=prob_up,
            prob_down=prob_down,
            reason=reason
        )
