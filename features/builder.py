"""
features/builder.py
-------------------
Orquestador de features: combina technical, orderbook y market_features
en una unica matriz lista para XGBoost.

Dos modos:
  1. build_training_dataset() — batch desde DB, para entrenamiento
  2. build_realtime_features() — un vector para inferencia en vivo
"""

import pandas as pd
import numpy as np
from loguru import logger
from typing import Optional

from data import storage
from features.technical import resample_to_ohlc, compute_technical_features
from features.orderbook import (
    compute_orderbook_features_batch,
    compute_snapshot_features,
    compute_trade_features
)
from features.market_features import (
    compute_market_features_batch,
    time_features_from_timestamp,
    share_price_features,
    streak_features
)


# ---------------------------------------------------------------------------
# Columnas que produce el builder (para referencia y validacion)
# ---------------------------------------------------------------------------

TECHNICAL_COLS = [
    "rsi_3", "rsi_5", "bb_position", "bb_width",
    "ema_cross_3_8", "ema_cross_5_13",
    "momentum_1", "momentum_3", "momentum_6",
    "volatility_6", "volatility_12",
    "vwap_diff", "atr_6",
    "price_vs_high_12", "price_vs_low_12",
]

ORDERBOOK_COLS = [
    "ob_imbalance", "ob_depth_bid_3", "ob_depth_ask_3",
    "ob_spread", "ob_midpoint", "ob_total_bid", "ob_total_ask",
    "trade_flow_net", "trade_count", "trade_avg_size", "trade_vwap",
]

MARKET_COLS = [
    "share_price_yes", "share_price_change",
    "hour_sin", "hour_cos", "dow_sin", "dow_cos",
    "streak_up", "streak_down", "prev_result",
]

ALL_FEATURE_COLS = TECHNICAL_COLS + ORDERBOOK_COLS + MARKET_COLS


# ---------------------------------------------------------------------------
# Batch mode: generar dataset completo para entrenamiento
# ---------------------------------------------------------------------------

def build_training_dataset(min_markets: int = 50) -> Optional[tuple[pd.DataFrame, pd.Series]]:
    """
    Construye el dataset completo de entrenamiento desde la DB.

    Lee resolved_markets + btc_prices + orderbook_snapshots + last_trades,
    genera features por cada mercado resuelto y devuelve (X, y).

    Returns:
      X : DataFrame con ALL_FEATURE_COLS, indexado por market_id
      y : Series binaria (1 = UP/Yes, 0 = DOWN/No)

      o None si no hay suficientes datos.
    """
    conn = storage.get_connection()
    try:
        # 1. Cargar resolved_markets
        resolved_rows = storage._fetchall(
            conn.cursor(),
            "SELECT * FROM resolved_markets WHERE winning_outcome IS NOT NULL "
            "ORDER BY ts_resolved ASC"
        )
    finally:
        conn.close()

    if len(resolved_rows) < min_markets:
        logger.warning(
            f"Solo hay {len(resolved_rows)} mercados resueltos "
            f"(minimo {min_markets}). No se puede construir dataset."
        )
        return None

    resolved_df = pd.DataFrame(resolved_rows)
    logger.info(f"Construyendo dataset con {len(resolved_df)} mercados resueltos")

    # 2. Cargar btc_prices (solo Chainlink, es el oracle de resolucion)
    conn = storage.get_connection()
    try:
        btc_rows = storage._fetchall(
            conn.cursor(),
            f"SELECT ts, price FROM btc_prices WHERE source={storage.PH} ORDER BY ts ASC",
            ("chainlink",)
        )
        if len(btc_rows) < 100:
            # Fallback a Binance si Chainlink tiene pocos datos
            btc_rows = storage._fetchall(
                conn.cursor(),
                f"SELECT ts, price FROM btc_prices WHERE source={storage.PH} ORDER BY ts ASC",
                ("binance",)
            )
    finally:
        conn.close()

    btc_df = pd.DataFrame(btc_rows)
    logger.info(f"Ticks de precio BTC: {len(btc_df):,}")

    # 3. OHLC y features tecnicos
    if btc_df.empty:
        logger.error("No hay datos de precio BTC — no se pueden calcular features tecnicos")
        return None

    ohlc = resample_to_ohlc(btc_df, freq_seconds=300)
    tech_features = compute_technical_features(ohlc)
    logger.info(f"OHLC periodos: {len(ohlc)} | Features tecnicos: {len(tech_features)}")

    # 4. Cargar orderbook snapshots y trades
    conn = storage.get_connection()
    try:
        snap_rows = storage._fetchall(
            conn.cursor(),
            "SELECT ts, asset_id, market_id, bids, asks FROM orderbook_snapshots ORDER BY ts"
        )
        trade_rows = storage._fetchall(
            conn.cursor(),
            "SELECT ts, asset_id, market_id, price, size, side FROM last_trades ORDER BY ts"
        )
    finally:
        conn.close()

    snap_df = pd.DataFrame(snap_rows) if snap_rows else pd.DataFrame(
        columns=["ts", "asset_id", "market_id", "bids", "asks"]
    )
    trade_df = pd.DataFrame(trade_rows) if trade_rows else pd.DataFrame(
        columns=["ts", "asset_id", "market_id", "price", "size", "side"]
    )
    logger.info(f"Snapshots: {len(snap_df):,} | Trades: {len(trade_df):,}")

    # 5. Preparar intervalos para features de OB
    market_intervals = []
    for _, row in resolved_df.iterrows():
        slug = row.get("slug", "")
        parts = slug.split("-") if slug else []
        if len(parts) >= 4 and parts[-1].isdigit():
            ts_start = int(parts[-1])
        elif row.get("ts_open"):
            ts_start = int(row["ts_open"]) // 1000
        else:
            ts_start = (int(row["ts_resolved"]) - 300_000) // 1000

        market_intervals.append({
            "market_id":    row["market_id"],
            "ts_open":      ts_start * 1000,
            "ts_close":     (ts_start + 300) * 1000,
            "asset_id_yes": row.get("asset_id_yes", ""),
        })

    # 6. Features de order book (batch)
    ob_features = compute_orderbook_features_batch(snap_df, trade_df, market_intervals)
    logger.info(f"OB features generados: {len(ob_features)} filas")

    # 7. Features de mercado (batch)
    mkt_features = compute_market_features_batch(resolved_df)
    logger.info(f"Market features generados: {len(mkt_features)} filas")

    # 8. Features tecnicos: mapear cada mercado al periodo OHLC mas cercano
    tech_by_market = _map_technical_to_markets(tech_features, market_intervals)
    logger.info(f"Tech features mapeados: {len(tech_by_market)} filas")

    # 9. Combinar todo en un solo DataFrame
    X = _merge_all_features(
        resolved_df, tech_by_market, ob_features, mkt_features
    )

    # 10. Target variable: 1 = Yes/UP, 0 = No/DOWN
    y_map = resolved_df.set_index("market_id")["winning_outcome"]
    y = y_map.map({"Yes": 1, "No": 0}).reindex(X.index)

    # Eliminar filas con target NaN
    valid_mask = y.notna()
    X = X.loc[valid_mask]
    y = y.loc[valid_mask].astype(int)

    # Rellenar NaN en features con 0 (seguro para tree models)
    X = X.fillna(0.0)

    logger.success(
        f"Dataset listo: {len(X)} samples x {len(X.columns)} features | "
        f"UP: {y.sum()} ({y.mean()*100:.1f}%) | DOWN: {len(y) - y.sum()} ({(1-y.mean())*100:.1f}%)"
    )

    return X, y


def _map_technical_to_markets(
    tech_features: pd.DataFrame,
    market_intervals: list[dict]
) -> pd.DataFrame:
    """
    Para cada mercado, encuentra el periodo OHLC correspondiente
    y extrae los features tecnicos de ese momento.
    """
    records = []
    tech_timestamps = tech_features.index.values

    for mi in market_intervals:
        market_id = mi["market_id"]
        ts_open_ms = mi["ts_open"]
        target_time = pd.Timestamp(ts_open_ms, unit="ms", tz="UTC")

        # Buscar el periodo OHLC mas cercano antes del inicio del mercado
        mask = tech_features.index <= target_time
        if mask.any():
            closest_idx = tech_features.index[mask][-1]
            row_data = tech_features.loc[closest_idx].to_dict()
        else:
            # No hay datos tecnicos antes de este mercado
            row_data = {col: np.nan for col in TECHNICAL_COLS}

        row_data["market_id"] = market_id
        records.append(row_data)

    if not records:
        return pd.DataFrame()

    return pd.DataFrame(records).set_index("market_id")


def _merge_all_features(
    resolved_df: pd.DataFrame,
    tech_df: pd.DataFrame,
    ob_df: pd.DataFrame,
    mkt_df: pd.DataFrame
) -> pd.DataFrame:
    """
    Combina los 3 DataFrames de features en uno solo, alineado por market_id.
    """
    market_ids = resolved_df["market_id"].values

    X = pd.DataFrame(index=market_ids)
    X.index.name = "market_id"

    # Merge tech features
    for col in TECHNICAL_COLS:
        if col in tech_df.columns:
            X[col] = tech_df[col].reindex(X.index).values
        else:
            X[col] = np.nan

    # Merge OB features
    for col in ORDERBOOK_COLS:
        if col in ob_df.columns:
            X[col] = ob_df[col].reindex(X.index).values
        else:
            X[col] = np.nan

    # Merge market features
    for col in MARKET_COLS:
        if col in mkt_df.columns:
            X[col] = mkt_df[col].reindex(X.index).values
        else:
            X[col] = np.nan

    return X


# ---------------------------------------------------------------------------
# Real-time mode: un vector de features para inferencia
# ---------------------------------------------------------------------------

def build_realtime_features(
    btc_ticks: pd.DataFrame,
    latest_snapshot_bids: list,
    latest_snapshot_asks: list,
    recent_trades: pd.DataFrame,
    share_price_yes: float,
    share_price_yes_prev: Optional[float],
    recent_outcomes: list[str],
    ts_now_ms: int
) -> pd.DataFrame:
    """
    Genera un unico vector de features para inferencia en tiempo real.

    Parametros:
      btc_ticks           : DataFrame con (ts, price) de los ultimos 60+ min
      latest_snapshot_bids: bids del ultimo snapshot del order book
      latest_snapshot_asks: asks del ultimo snapshot
      recent_trades       : DataFrame de trades recientes (ts, price, size, side)
      share_price_yes     : precio actual del share YES
      share_price_yes_prev: precio del share YES ~1 min antes
      recent_outcomes     : ultimos N resultados ["Yes", "No", "Yes", ...]
      ts_now_ms           : timestamp actual en ms

    Devuelve: DataFrame de 1 fila con ALL_FEATURE_COLS
    """
    # Features tecnicos
    if btc_ticks is not None and len(btc_ticks) > 10:
        ohlc = resample_to_ohlc(btc_ticks, freq_seconds=300)
        tech = compute_technical_features(ohlc)
        if not tech.empty:
            tech_row = tech.iloc[-1].to_dict()
        else:
            tech_row = {col: 0.0 for col in TECHNICAL_COLS}
    else:
        tech_row = {col: 0.0 for col in TECHNICAL_COLS}

    # Features de order book
    ob_row = compute_snapshot_features(latest_snapshot_bids, latest_snapshot_asks)
    trade_row = compute_trade_features(recent_trades)

    # Features de mercado
    time_row = time_features_from_timestamp(ts_now_ms)
    share_row = share_price_features(share_price_yes, share_price_yes_prev)
    streak_row = streak_features(recent_outcomes)

    # Combinar todo
    all_feats = {}
    all_feats.update(tech_row)
    all_feats.update(ob_row)
    all_feats.update(trade_row)
    all_feats.update(time_row)
    all_feats.update(share_row)
    all_feats.update(streak_row)

    # Asegurar que tenemos todas las columnas en el orden correcto
    row = {col: all_feats.get(col, 0.0) for col in ALL_FEATURE_COLS}

    return pd.DataFrame([row], columns=ALL_FEATURE_COLS)
