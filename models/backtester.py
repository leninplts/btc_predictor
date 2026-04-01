"""
models/backtester.py
--------------------
Simulacion historica del bot sobre mercados resueltos.

Itera cronologicamente sobre resolved_markets:
  1. Genera features para cada mercado
  2. El modelo predice P(up)
  3. Simula la decision de trading (comprar YES, NO, o SKIP)
  4. Aplica fees reales de Polymarket
  5. Calcula PnL, win rate, drawdown, Sharpe

Uso:
  python -m models.backtester
  o
  from models.backtester import run_backtest
  report = run_backtest(model_path="models/xgb_btc5m_XXX.pkl")
"""

import os
import sys
import math
import numpy as np
import pandas as pd
from loguru import logger
from typing import Optional

from features.builder import build_training_dataset, ALL_FEATURE_COLS
from models.predictor import Predictor


# ---------------------------------------------------------------------------
# Fee de Polymarket
# ---------------------------------------------------------------------------

def polymarket_fee(price: float) -> float:
    """
    Calcula el fee de Polymarket para un share a un precio dado.
    El fee es maximo cuando price = 0.50 (~1.56%) y se reduce hacia los extremos.

    Formula aproximada basada en la tabla oficial:
      fee_rate = 2 * price * (1 - price) * 0.0312
    """
    if price <= 0 or price >= 1:
        return 0.0
    return 2.0 * price * (1.0 - price) * 0.0312


# ---------------------------------------------------------------------------
# Backtester
# ---------------------------------------------------------------------------

def run_backtest(
    model_path: Optional[str] = None,
    initial_capital: float = 1000.0,
    risk_per_trade: float = 0.02,
    min_confidence: float = 0.55,
    min_markets: int = 30,
) -> dict:
    """
    Corre el backtest completo sobre datos historicos.

    Parametros:
      model_path      : ruta al .pkl del modelo (None = el mas reciente)
      initial_capital  : capital inicial en USDC
      risk_per_trade   : fraccion del capital a arriesgar por trade (0.02 = 2%)
      min_confidence   : confianza minima para operar
      min_markets      : minimo de mercados para construir dataset

    Retorna dict con metricas completas del backtest.
    """
    logger.info("=" * 60)
    logger.info("  BACKTEST")
    logger.info("=" * 60)

    # 1. Construir dataset
    result = build_training_dataset(min_markets=min_markets)
    if result is None:
        logger.error("No hay suficientes datos para backtest")
        return {"error": "insufficient_data"}

    X, y = result

    # 2. Cargar modelo
    predictor = Predictor(model_path=model_path, min_confidence=min_confidence)
    if not predictor.is_loaded():
        logger.error("No hay modelo entrenado. Entrena primero con models/trainer.py")
        return {"error": "no_model"}

    # 3. Simular operaciones
    capital = initial_capital
    peak_capital = initial_capital
    trades = []

    for i in range(len(X)):
        features = X.iloc[[i]]
        actual = int(y.iloc[i])

        # Predecir
        pred = predictor.predict(features)

        if not pred["should_trade"]:
            continue

        # Decidir lado: comprar YES si predice UP, NO si predice DOWN
        if pred["direction"] == "UP":
            # Comprar YES share a ~0.50 (simplificado)
            buy_price = 0.50   # asumimos midpoint
            win = actual == 1  # YES gano
        else:
            # Comprar NO share a ~0.50
            buy_price = 0.50
            win = actual == 0  # NO gano

        # Calcular size de la operacion
        trade_size_usdc = capital * risk_per_trade
        n_shares = trade_size_usdc / buy_price

        # Fee
        fee_per_share = polymarket_fee(buy_price)
        total_fee = fee_per_share * n_shares

        # PnL
        if win:
            # Share paga $1.00
            pnl = n_shares * (1.0 - buy_price) - total_fee
        else:
            # Share paga $0.00
            pnl = -n_shares * buy_price - total_fee

        capital += pnl
        peak_capital = max(peak_capital, capital)

        trades.append({
            "index":      i,
            "market_id":  X.index[i],
            "direction":  pred["direction"],
            "confidence": pred["confidence"],
            "actual":     "UP" if actual == 1 else "DOWN",
            "win":        win,
            "buy_price":  buy_price,
            "n_shares":   n_shares,
            "fee":        total_fee,
            "pnl":        pnl,
            "capital":    capital,
        })

    # 4. Calcular metricas
    if not trades:
        logger.warning("El backtest no genero trades (confidence demasiado alta?)")
        return {"error": "no_trades", "n_markets": len(X)}

    trades_df = pd.DataFrame(trades)
    n_trades   = len(trades_df)
    n_wins     = trades_df["win"].sum()
    n_losses   = n_trades - n_wins
    win_rate   = n_wins / n_trades
    total_pnl  = trades_df["pnl"].sum()
    total_fees = trades_df["fee"].sum()
    avg_pnl    = trades_df["pnl"].mean()

    # Max drawdown
    running_max = trades_df["capital"].cummax()
    drawdown = (trades_df["capital"] - running_max) / running_max
    max_drawdown = drawdown.min()

    # Sharpe ratio (anualizado, asumiendo ~12 trades/hora * 24h)
    returns = trades_df["pnl"] / trades_df["capital"].shift(1).fillna(initial_capital)
    sharpe = (returns.mean() / returns.std() * math.sqrt(12 * 24 * 365)
              if returns.std() > 0 else 0.0)

    # Profit factor
    gross_profit = trades_df.loc[trades_df["pnl"] > 0, "pnl"].sum()
    gross_loss   = abs(trades_df.loc[trades_df["pnl"] < 0, "pnl"].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    report = {
        "initial_capital": initial_capital,
        "final_capital":   capital,
        "total_pnl":       total_pnl,
        "total_pnl_pct":   (total_pnl / initial_capital) * 100,
        "total_fees":      total_fees,
        "n_markets":       len(X),
        "n_trades":        n_trades,
        "n_skipped":       len(X) - n_trades,
        "n_wins":          int(n_wins),
        "n_losses":        int(n_losses),
        "win_rate":        win_rate,
        "avg_pnl":         avg_pnl,
        "max_drawdown":    max_drawdown,
        "sharpe_ratio":    sharpe,
        "profit_factor":   profit_factor,
        "risk_per_trade":  risk_per_trade,
        "min_confidence":  min_confidence,
    }

    # 5. Imprimir reporte
    logger.info("-" * 60)
    logger.info(f"  Capital:  ${initial_capital:,.2f} -> ${capital:,.2f} "
                f"({report['total_pnl_pct']:+.2f}%)")
    logger.info(f"  PnL:     ${total_pnl:+,.2f} (fees: ${total_fees:,.2f})")
    logger.info(f"  Trades:  {n_trades} ({n_wins}W / {n_losses}L) | "
                f"win rate: {win_rate:.2%}")
    logger.info(f"  Skipped: {report['n_skipped']} mercados (low confidence)")
    logger.info(f"  Max DD:  {max_drawdown:.2%}")
    logger.info(f"  Sharpe:  {sharpe:.3f}")
    logger.info(f"  PF:      {profit_factor:.2f}")
    logger.info("-" * 60)

    if win_rate >= 0.53:
        logger.success(f"WIN RATE {win_rate:.2%} >= 53% — estrategia potencialmente rentable")
    else:
        logger.warning(f"WIN RATE {win_rate:.2%} < 53% — no es rentable con fees actuales")

    return report


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

    from data.storage import init_db
    init_db()

    report = run_backtest(
        min_confidence=0.55,
        risk_per_trade=0.02,
        min_markets=30,
    )

    if "error" in report:
        print(f"Error: {report['error']}")
