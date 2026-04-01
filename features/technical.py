"""
features/technical.py
---------------------
Indicadores tecnicos calculados sobre la serie de precios BTC.

Entrada: DataFrame con columnas (ts, price) de btc_prices.
         Se resamplea a OHLC de 5 minutos para alinear con los mercados.

Salida:  DataFrame con una fila por intervalo de 5 minutos y columnas
         de indicadores tecnicos.

Todos los indicadores usan ventanas cortas (3-12 periodos de 5 min = 15-60 min)
porque el timeframe de decision es de 5 minutos.
"""

import numpy as np
import pandas as pd
from typing import Optional


# ---------------------------------------------------------------------------
# OHLC resampling
# ---------------------------------------------------------------------------

def resample_to_ohlc(df: pd.DataFrame, freq_seconds: int = 300) -> pd.DataFrame:
    """
    Convierte ticks de precio BTC a OHLC por intervalos de freq_seconds.

    Input:  DataFrame con columnas 'ts' (unix ms) y 'price'.
    Output: DataFrame indexado por timestamp de inicio del intervalo con
            columnas: open, high, low, close, volume (count de ticks).
    """
    df = df.copy()
    df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("datetime").sort_index()

    freq = f"{freq_seconds}s"
    ohlc = df["price"].resample(freq).agg(
        open="first",
        high="max",
        low="min",
        close="last"
    )
    ohlc["volume"] = df["price"].resample(freq).count()

    # Eliminar periodos sin datos
    ohlc = ohlc.dropna(subset=["close"])

    return ohlc


# ---------------------------------------------------------------------------
# Indicadores individuales
# ---------------------------------------------------------------------------

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)

    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def bollinger_bands(series: pd.Series, period: int = 20, std_dev: float = 2.0):
    """
    Bollinger Bands.
    Devuelve (bb_position, bb_width):
      bb_position: 0 = en banda inferior, 1 = en banda superior
      bb_width: ancho relativo de las bandas (volatilidad)
    """
    sma = series.rolling(window=period, min_periods=period).mean()
    std = series.rolling(window=period, min_periods=period).std()

    upper = sma + std_dev * std
    lower = sma - std_dev * std

    bb_position = (series - lower) / (upper - lower).replace(0, np.nan)
    bb_width = (upper - lower) / sma.replace(0, np.nan)

    return bb_position, bb_width


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def ema_cross(series: pd.Series, fast: int, slow: int) -> pd.Series:
    """
    Diferencia normalizada entre EMA rapida y lenta.
    Positivo = tendencia alcista, negativo = bajista.
    """
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    # Normalizar por el precio actual para que sea comparable
    return (ema_fast - ema_slow) / series.replace(0, np.nan)


def momentum(series: pd.Series, period: int) -> pd.Series:
    """Retorno porcentual en los ultimos N periodos."""
    return series.pct_change(periods=period) * 100


def volatility(series: pd.Series, period: int) -> pd.Series:
    """Desviacion estandar de retornos (volatilidad realizada)."""
    returns = series.pct_change()
    return returns.rolling(window=period, min_periods=max(2, period // 2)).std() * 100


def vwap_diff(ohlc: pd.DataFrame) -> pd.Series:
    """
    Diferencia porcentual entre precio actual y VWAP rolling (12 periodos).
    VWAP simplificado: media de close ponderada por volume.
    """
    typical_price = (ohlc["high"] + ohlc["low"] + ohlc["close"]) / 3
    vol = ohlc["volume"].replace(0, 1)  # evitar division por 0
    cum_tp_vol = (typical_price * vol).rolling(12, min_periods=1).sum()
    cum_vol = vol.rolling(12, min_periods=1).sum()
    vwap_val = cum_tp_vol / cum_vol.replace(0, np.nan)
    return (ohlc["close"] - vwap_val) / vwap_val.replace(0, np.nan) * 100


def atr(ohlc: pd.DataFrame, period: int = 6) -> pd.Series:
    """Average True Range (ATR)."""
    high = ohlc["high"]
    low = ohlc["low"]
    close_prev = ohlc["close"].shift(1)

    tr1 = high - low
    tr2 = (high - close_prev).abs()
    tr3 = (low - close_prev).abs()

    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return true_range.rolling(window=period, min_periods=max(1, period // 2)).mean()


def price_position(series: pd.Series, period: int = 12):
    """
    Posicion del precio actual dentro del rango high-low de los ultimos N periodos.
    vs_high: 1 = en el maximo, 0 = lejos del maximo
    vs_low:  1 = en el minimo, 0 = lejos del minimo
    """
    rolling_high = series.rolling(window=period, min_periods=1).max()
    rolling_low  = series.rolling(window=period, min_periods=1).min()
    rng = (rolling_high - rolling_low).replace(0, np.nan)

    vs_high = (series - rolling_low) / rng   # 1 = en el high
    vs_low  = (rolling_high - series) / rng  # 1 = en el low

    return vs_high, vs_low


# ---------------------------------------------------------------------------
# Funcion principal: calcular todos los indicadores
# ---------------------------------------------------------------------------

def compute_technical_features(ohlc: pd.DataFrame) -> pd.DataFrame:
    """
    Dado un DataFrame OHLC de 5 minutos, calcula todos los indicadores tecnicos.

    Input:  DataFrame con columnas: open, high, low, close, volume
    Output: DataFrame con los mismos indices + columnas de features
    """
    close = ohlc["close"]
    result = pd.DataFrame(index=ohlc.index)

    # RSI
    result["rsi_3"]  = rsi(close, period=3)
    result["rsi_5"]  = rsi(close, period=5)

    # Bollinger Bands (periodo 10 para ser responsive en 5-min bars)
    result["bb_position"], result["bb_width"] = bollinger_bands(close, period=10, std_dev=2.0)

    # EMA crosses
    result["ema_cross_3_8"]  = ema_cross(close, fast=3, slow=8)
    result["ema_cross_5_13"] = ema_cross(close, fast=5, slow=13)

    # Momentum
    result["momentum_1"] = momentum(close, period=1)
    result["momentum_3"] = momentum(close, period=3)
    result["momentum_6"] = momentum(close, period=6)

    # Volatilidad
    result["volatility_6"]  = volatility(close, period=6)
    result["volatility_12"] = volatility(close, period=12)

    # VWAP
    result["vwap_diff"] = vwap_diff(ohlc)

    # ATR
    result["atr_6"] = atr(ohlc, period=6)

    # Posicion en rango
    result["price_vs_high_12"], result["price_vs_low_12"] = price_position(close, period=12)

    return result


def compute_from_raw_ticks(ticks_df: pd.DataFrame) -> pd.DataFrame:
    """
    Shortcut: recibe ticks crudos (ts, price) y devuelve features tecnicos.
    """
    ohlc = resample_to_ohlc(ticks_df, freq_seconds=300)
    features = compute_technical_features(ohlc)
    return features
