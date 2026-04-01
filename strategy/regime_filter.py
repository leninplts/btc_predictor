"""
strategy/regime_filter.py
-------------------------
Detector de regimen de mercado basado en volatilidad y momentum.

Clasifica el estado actual del mercado en uno de 4 regimenes:

  high_vol  : ATR alto, BB ancho → mercado activo, buenas oportunidades
  low_vol   : ATR bajo, BB estrecho → mercado dormido, poco edge
  trending  : momentum consistente en una direccion → seguir tendencia
  choppy    : momentum oscila, sin direccion clara → reducir exposicion

El regimen se usa en:
  - signal.py    : para decidir si operar (SKIP en low_vol)
  - sizing.py    : para reducir tamano en choppy
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional
from loguru import logger


# ---------------------------------------------------------------------------
# Tipos
# ---------------------------------------------------------------------------

@dataclass
class RegimeState:
    """Estado del regimen actual del mercado."""
    regime: str           # "high_vol" | "low_vol" | "trending" | "choppy"
    atr: float            # ATR actual
    bb_width: float       # ancho de Bollinger Bands actual
    momentum_sign: int    # 1 = ultimos momentums positivos, -1 = negativos, 0 = mixto
    confidence: float     # 0.0 a 1.0 — que tan seguro estamos del regimen
    reason: str           # explicacion legible


# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------

DEFAULT_REGIME_CONFIG = {
    # Percentiles relativos al historico para clasificar ATR
    "atr_low_threshold":   0.0015,   # ATR < esto = low_vol (0.15% del precio)
    "atr_high_threshold":  0.005,    # ATR > esto = high_vol (0.5% del precio)

    # Bollinger Band width
    "bb_narrow_threshold": 0.003,    # BB width < esto = mercado comprimido
    "bb_wide_threshold":   0.008,    # BB width > esto = mercado expandido

    # Momentum consistency (ultimos N periodos)
    "momentum_lookback":   4,        # cuantos periodos revisar
    "momentum_consistency": 0.75,    # % de periodos con mismo signo para "trending"
}


# ---------------------------------------------------------------------------
# Detector de regimen
# ---------------------------------------------------------------------------

class RegimeDetector:
    """
    Clasifica el regimen de mercado actual basado en features tecnicos.
    """

    def __init__(self, config: Optional[dict] = None):
        self.config = {**DEFAULT_REGIME_CONFIG, **(config or {})}

    def detect(
        self,
        atr: float = 0.0,
        bb_width: float = 0.0,
        recent_momentums: Optional[list[float]] = None,
    ) -> RegimeState:
        """
        Detecta el regimen actual.

        Parametros:
          atr              : Average True Range actual (normalizado por precio)
          bb_width         : ancho de Bollinger Bands actual (normalizado)
          recent_momentums : lista de retornos de los ultimos N periodos
                             ej: [0.05, -0.02, 0.03, 0.01]
        """
        cfg = self.config

        if recent_momentums is None:
            recent_momentums = []

        # --- Analizar momentum ---
        n_mom = len(recent_momentums)
        if n_mom >= 2:
            positive = sum(1 for m in recent_momentums if m > 0)
            negative = sum(1 for m in recent_momentums if m < 0)
            consistency = max(positive, negative) / n_mom

            if positive > negative:
                momentum_sign = 1
            elif negative > positive:
                momentum_sign = -1
            else:
                momentum_sign = 0
        else:
            consistency = 0.0
            momentum_sign = 0

        # --- Clasificar regimen ---
        is_low_atr  = atr < cfg["atr_low_threshold"] and atr > 0
        is_high_atr = atr >= cfg["atr_high_threshold"]
        is_narrow_bb = bb_width < cfg["bb_narrow_threshold"] and bb_width > 0
        is_wide_bb   = bb_width >= cfg["bb_wide_threshold"]
        is_trending  = consistency >= cfg["momentum_consistency"] and n_mom >= 2

        # Prioridad de clasificacion:
        # 1. low_vol si ATR y BB son bajos
        if is_low_atr and is_narrow_bb:
            return RegimeState(
                regime="low_vol",
                atr=atr, bb_width=bb_width,
                momentum_sign=momentum_sign,
                confidence=0.8,
                reason=f"ATR={atr:.6f} < {cfg['atr_low_threshold']} "
                       f"& BB_width={bb_width:.6f} < {cfg['bb_narrow_threshold']}"
            )

        # 2. trending si momentum es consistente
        if is_trending and not is_low_atr:
            direction = "UP" if momentum_sign == 1 else "DOWN"
            return RegimeState(
                regime="trending",
                atr=atr, bb_width=bb_width,
                momentum_sign=momentum_sign,
                confidence=min(0.9, consistency),
                reason=f"Trending {direction} (consistency={consistency:.0%}, "
                       f"ATR={atr:.6f})"
            )

        # 3. high_vol si ATR o BB son altos
        if is_high_atr or is_wide_bb:
            return RegimeState(
                regime="high_vol",
                atr=atr, bb_width=bb_width,
                momentum_sign=momentum_sign,
                confidence=0.7,
                reason=f"High volatility (ATR={atr:.6f}, BB_width={bb_width:.6f})"
            )

        # 4. choppy: momentum mixto sin tendencia clara
        if n_mom >= 2 and not is_trending and not is_low_atr:
            return RegimeState(
                regime="choppy",
                atr=atr, bb_width=bb_width,
                momentum_sign=momentum_sign,
                confidence=0.6,
                reason=f"Choppy (consistency={consistency:.0%}, no clear direction)"
            )

        # 5. Default: regimen normal (tratar como high_vol para operar)
        return RegimeState(
            regime="high_vol",
            atr=atr, bb_width=bb_width,
            momentum_sign=momentum_sign,
            confidence=0.5,
            reason=f"Default regime (ATR={atr:.6f}, BB={bb_width:.6f}, "
                   f"insufficient data for classification)"
        )

    def detect_from_features(self, features: dict) -> RegimeState:
        """
        Shortcut: detecta regimen a partir del dict de features del builder.
        """
        atr = features.get("atr_6", 0.0)
        bb_width = features.get("bb_width", 0.0)

        # Reconstruir momentums recientes
        recent_momentums = []
        for key in ["momentum_1", "momentum_3", "momentum_6"]:
            val = features.get(key, 0.0)
            if val != 0.0:
                recent_momentums.append(val)

        return self.detect(
            atr=atr,
            bb_width=bb_width,
            recent_momentums=recent_momentums
        )
