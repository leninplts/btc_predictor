"""
features/orderbook.py
---------------------
Features extraidos del order book de Polymarket.

Dos fuentes de datos:
  - orderbook_snapshots : book completo con JSON de bids/asks
  - last_trades         : cada trade ejecutado (precio, size, side)

Para batch (entrenamiento): se recibe DataFrame con datos de la DB.
Para real-time (inferencia): se recibe el snapshot mas reciente.
"""

import json
import numpy as np
import pandas as pd
from typing import Optional


# ---------------------------------------------------------------------------
# Parsing de snapshots
# ---------------------------------------------------------------------------

def _parse_book_side(raw: str | list) -> list[tuple[float, float]]:
    """
    Parsea un lado del book (bids o asks) a lista de (price, size).
    Viene como string JSON o lista de dicts [{price, size}].
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
            p = float(entry.get("price", entry.get("p", 0)))
            s = float(entry.get("size", entry.get("s", 0)))
            result.append((p, s))
        except (ValueError, TypeError, AttributeError):
            continue
    return result


# ---------------------------------------------------------------------------
# Features de un solo snapshot
# ---------------------------------------------------------------------------

def compute_snapshot_features(
    bids_raw: str | list,
    asks_raw: str | list,
    top_n: int = 3
) -> dict:
    """
    Calcula features de un snapshot del order book.

    Parametros:
      bids_raw : JSON string o lista de bids [{price, size}, ...]
      asks_raw : JSON string o lista de asks [{price, size}, ...]
      top_n    : cuantos niveles superiores usar para depth

    Devuelve dict con:
      ob_imbalance, ob_depth_bid_3, ob_depth_ask_3,
      ob_spread, ob_midpoint, ob_total_bid, ob_total_ask
    """
    bids = _parse_book_side(bids_raw)
    asks = _parse_book_side(asks_raw)

    # Ordenar: bids de mayor a menor precio, asks de menor a mayor
    bids.sort(key=lambda x: x[0], reverse=True)
    asks.sort(key=lambda x: x[0])

    total_bid_vol = sum(s for _, s in bids) if bids else 0.0
    total_ask_vol = sum(s for _, s in asks) if asks else 0.0

    # Imbalance total
    total = total_bid_vol + total_ask_vol
    ob_imbalance = (total_bid_vol - total_ask_vol) / total if total > 0 else 0.0

    # Profundidad en top N niveles
    depth_bid_n = sum(s for _, s in bids[:top_n]) if bids else 0.0
    depth_ask_n = sum(s for _, s in asks[:top_n]) if asks else 0.0

    # Spread y midpoint
    best_bid = bids[0][0] if bids else 0.0
    best_ask = asks[0][0] if asks else 1.0
    ob_spread = best_ask - best_bid
    ob_midpoint = (best_bid + best_ask) / 2

    return {
        "ob_imbalance":    ob_imbalance,
        "ob_depth_bid_3":  depth_bid_n,
        "ob_depth_ask_3":  depth_ask_n,
        "ob_spread":       ob_spread,
        "ob_midpoint":     ob_midpoint,
        "ob_total_bid":    total_bid_vol,
        "ob_total_ask":    total_ask_vol,
    }


# ---------------------------------------------------------------------------
# Features de trades
# ---------------------------------------------------------------------------

def compute_trade_features(trades_df: pd.DataFrame) -> dict:
    """
    Calcula features a partir de trades recientes.

    Input: DataFrame con columnas: ts, price, size, side
           (filtrado a trades del mercado/asset actual)

    Devuelve dict con:
      trade_flow_net, trade_count, trade_avg_size, trade_vwap
    """
    if trades_df is None or trades_df.empty:
        return {
            "trade_flow_net":  0.0,
            "trade_count":     0,
            "trade_avg_size":  0.0,
            "trade_vwap":      0.5,  # neutral
        }

    # Flujo neto: volumen BUY - volumen SELL
    buy_vol = trades_df.loc[trades_df["side"] == "BUY", "size"].sum()
    sell_vol = trades_df.loc[trades_df["side"] == "SELL", "size"].sum()
    trade_flow_net = buy_vol - sell_vol

    # Conteo
    trade_count = len(trades_df)

    # Tamano promedio
    trade_avg_size = trades_df["size"].mean() if trade_count > 0 else 0.0

    # VWAP de trades
    total_notional = (trades_df["price"] * trades_df["size"]).sum()
    total_volume = trades_df["size"].sum()
    trade_vwap = total_notional / total_volume if total_volume > 0 else 0.5

    return {
        "trade_flow_net":  trade_flow_net,
        "trade_count":     trade_count,
        "trade_avg_size":  trade_avg_size,
        "trade_vwap":      trade_vwap,
    }


# ---------------------------------------------------------------------------
# Batch: features por intervalo para entrenamiento
# ---------------------------------------------------------------------------

def compute_orderbook_features_batch(
    snapshots_df: pd.DataFrame,
    trades_df: pd.DataFrame,
    market_intervals: list[dict]
) -> pd.DataFrame:
    """
    Calcula features de order book para cada mercado resuelto (batch mode).

    Parametros:
      snapshots_df    : DataFrame de orderbook_snapshots (ts, asset_id, market_id, bids, asks)
      trades_df       : DataFrame de last_trades (ts, asset_id, market_id, price, size, side)
      market_intervals: lista de dicts con {market_id, ts_open, ts_close, asset_id_yes}

    Devuelve DataFrame con market_id como indice y features de OB como columnas.
    """
    records = []

    for mi in market_intervals:
        market_id = mi["market_id"]
        ts_open   = mi["ts_open"]
        ts_close  = mi["ts_close"]
        yes_id    = mi.get("asset_id_yes", "")

        # Snapshot mas reciente del YES token antes del cierre
        mask_snap = (
            (snapshots_df["ts"] >= ts_open) &
            (snapshots_df["ts"] <= ts_close) &
            (snapshots_df["asset_id"] == yes_id)
        )
        relevant_snaps = snapshots_df.loc[mask_snap].sort_values("ts", ascending=False)

        if not relevant_snaps.empty:
            latest = relevant_snaps.iloc[0]
            ob_feats = compute_snapshot_features(latest["bids"], latest["asks"])
        else:
            ob_feats = compute_snapshot_features([], [])

        # Trades del YES token en este intervalo
        mask_trades = (
            (trades_df["ts"] >= ts_open) &
            (trades_df["ts"] <= ts_close) &
            (trades_df["asset_id"] == yes_id)
        )
        relevant_trades = trades_df.loc[mask_trades]
        trade_feats = compute_trade_features(relevant_trades)

        row = {"market_id": market_id}
        row.update(ob_feats)
        row.update(trade_feats)
        records.append(row)

    if not records:
        return pd.DataFrame()

    return pd.DataFrame(records).set_index("market_id")
