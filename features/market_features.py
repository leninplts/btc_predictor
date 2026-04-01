"""
features/market_features.py
---------------------------
Features del propio mercado de Polymarket y variables temporales.

Incluye:
  - Precio del share YES/NO y su cambio reciente
  - Estacionalidad ciclica (hora del dia, dia de la semana)
  - Streak de resultados consecutivos (UP/DOWN)
  - Resultado del mercado anterior (autocorrelacion serial)
"""

import math
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Estacionalidad ciclica
# ---------------------------------------------------------------------------

def time_features_from_timestamp(ts_ms: int) -> dict:
    """
    Genera features ciclicos de hora y dia de la semana.
    Usa sin/cos para que las 23:55 y 00:00 esten cerca.

    Input:  timestamp en milisegundos UTC
    Output: dict con hour_sin, hour_cos, dow_sin, dow_cos
    """
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    hour = dt.hour + dt.minute / 60.0   # hora decimal
    dow = dt.weekday()                   # 0=lunes, 6=domingo

    return {
        "hour_sin": math.sin(2 * math.pi * hour / 24),
        "hour_cos": math.cos(2 * math.pi * hour / 24),
        "dow_sin":  math.sin(2 * math.pi * dow / 7),
        "dow_cos":  math.cos(2 * math.pi * dow / 7),
    }


# ---------------------------------------------------------------------------
# Share price features
# ---------------------------------------------------------------------------

def share_price_features(
    share_price_yes: Optional[float],
    share_price_prev: Optional[float] = None
) -> dict:
    """
    Features basados en el precio actual del share.

    share_price_yes: precio actual del share YES (0.0 a 1.0)
    share_price_prev: precio del share YES en el minuto anterior
    """
    price = share_price_yes if share_price_yes is not None else 0.5

    change = 0.0
    if share_price_prev is not None and share_price_prev > 0:
        change = (price - share_price_prev) / share_price_prev * 100

    return {
        "share_price_yes":    price,
        "share_price_change": change,
    }


# ---------------------------------------------------------------------------
# Streak y resultado anterior
# ---------------------------------------------------------------------------

def streak_features(resolved_outcomes: list[str]) -> dict:
    """
    Calcula streak de resultados consecutivos y resultado anterior.

    Input: lista de outcomes ordenados cronologicamente, ej: ["Yes", "No", "Yes", "Yes"]
           el ultimo elemento es el mas reciente.

    Output: dict con streak_up, streak_down, prev_result
    """
    if not resolved_outcomes:
        return {
            "streak_up":   0,
            "streak_down": 0,
            "prev_result": 0.5,  # neutral si no hay data
        }

    # Resultado anterior (el mas reciente)
    prev = resolved_outcomes[-1]
    prev_result = 1.0 if prev == "Yes" else 0.0

    # Streak: contar consecutivos desde el final
    streak_up = 0
    streak_down = 0

    for outcome in reversed(resolved_outcomes):
        if outcome == "Yes":
            if streak_down > 0:
                break
            streak_up += 1
        else:
            if streak_up > 0:
                break
            streak_down += 1

    return {
        "streak_up":   streak_up,
        "streak_down": streak_down,
        "prev_result": prev_result,
    }


# ---------------------------------------------------------------------------
# Batch: features por intervalo para entrenamiento
# ---------------------------------------------------------------------------

def compute_market_features_batch(
    resolved_df: pd.DataFrame,
    share_prices: Optional[dict] = None
) -> pd.DataFrame:
    """
    Calcula features de mercado para cada mercado resuelto (batch mode).

    Parametros:
      resolved_df  : DataFrame de resolved_markets, ordenado por ts_resolved ASC
                     Columnas: market_id, winning_outcome, ts_resolved, slug
      share_prices : dict opcional {market_id: (price_yes, price_yes_prev)}

    Devuelve DataFrame con market_id como indice y features de mercado como columnas.
    """
    resolved_df = resolved_df.sort_values("ts_resolved").reset_index(drop=True)
    records = []

    outcomes_history: list[str] = []

    for i, row in resolved_df.iterrows():
        market_id  = row["market_id"]
        ts_resolved = row["ts_resolved"]

        # Time features
        ts_for_time = ts_resolved - 300_000  # usamos el inicio del intervalo
        time_feats = time_features_from_timestamp(ts_for_time)

        # Share price features
        if share_prices and market_id in share_prices:
            sp_yes, sp_prev = share_prices[market_id]
            sp_feats = share_price_features(sp_yes, sp_prev)
        else:
            sp_feats = share_price_features(None, None)

        # Streak features (basado en los mercados ANTERIORES, no incluir el actual)
        streak_feats = streak_features(outcomes_history)

        # Registrar
        rec = {"market_id": market_id}
        rec.update(time_feats)
        rec.update(sp_feats)
        rec.update(streak_feats)
        records.append(rec)

        # Agregar el outcome actual al historial (para el siguiente)
        outcomes_history.append(row["winning_outcome"])

    if not records:
        return pd.DataFrame()

    return pd.DataFrame(records).set_index("market_id")
