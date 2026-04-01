"""
strategy/engine.py
------------------
Motor de estrategia: orquesta el ciclo completo de decision.

Cada vez que se detecta un nuevo mercado BTC 5-min, el engine:
  1. Genera features en tiempo real (builder.build_realtime_features)
  2. Predice P(up) con el modelo (predictor.predict)
  3. Detecta el regimen de mercado (regime_filter.detect)
  4. Genera la senal de trading (signal.generate)
  5. Calcula el tamano de posicion (sizing.calculate)
  6. Devuelve una Decision completa lista para ejecutar

En modo paper trading, no se envian ordenes reales sino que se
registra la decision para trackear PnL simulado.
"""

import time
import json
import pandas as pd
from dataclasses import dataclass, asdict
from typing import Optional
from loguru import logger

from features.builder import build_realtime_features
from models.predictor import Predictor
from strategy.signal import SignalGenerator, Signal
from strategy.sizing import PositionSizer, PositionSize
from strategy.regime_filter import RegimeDetector, RegimeState
from data import storage


# ---------------------------------------------------------------------------
# Tipos
# ---------------------------------------------------------------------------

@dataclass
class Decision:
    """Resultado completo de un ciclo de decision."""
    timestamp_ms: int
    market_id: str
    slug: str

    # Prediccion
    prob_up: float
    prob_down: float
    confidence: float
    model_loaded: bool

    # Regimen
    regime: str
    regime_reason: str

    # Senal
    action: str             # BUY_YES | BUY_NO | SKIP
    token_id: str
    order_type: str         # limit | market | ""
    target_price: float
    signal_reason: str

    # Sizing
    usdc_amount: float
    n_shares: float
    kelly_raw: float
    fee_estimated: float
    sizing_reason: str

    # Meta
    paper_mode: bool

    def to_dict(self) -> dict:
        return asdict(self)

    def to_log(self) -> str:
        """Formato legible para logging."""
        if self.action == "SKIP":
            return (
                f"SKIP | {self.slug} | conf={self.confidence:.3f} "
                f"| regime={self.regime} | {self.signal_reason}"
            )
        return (
            f"{self.action} | {self.slug} | "
            f"prob_up={self.prob_up:.3f} conf={self.confidence:.3f} | "
            f"regime={self.regime} | "
            f"{self.order_type} @ {self.target_price:.3f} | "
            f"${self.usdc_amount:.2f} ({self.n_shares:.1f} shares) | "
            f"fee ~${self.fee_estimated:.4f}"
            f"{' [PAPER]' if self.paper_mode else ''}"
        )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class StrategyEngine:
    """
    Motor principal de decision.
    Se instancia una vez al iniciar el bot y se llama cada nuevo mercado.
    """

    def __init__(
        self,
        model_path: Optional[str] = None,
        capital: float = 1000.0,
        paper_mode: bool = True,
        min_confidence: float = 0.55,
        max_risk_per_trade: float = 0.05,
        kelly_fraction: float = 0.35,
    ):
        self.capital = capital
        self.paper_mode = paper_mode

        # Sub-componentes
        self.predictor = Predictor(
            model_path=model_path,
            min_confidence=min_confidence
        )
        self.signal_gen = SignalGenerator(config={
            "min_confidence": min_confidence,
        })
        self.sizer = PositionSizer(config={
            "kelly_fraction": kelly_fraction,
            "max_risk_per_trade": max_risk_per_trade,
        })
        self.regime_detector = RegimeDetector()

        # Historial de decisiones (para tracking)
        self.decisions: list[Decision] = []

        logger.info(
            f"StrategyEngine inicializado | "
            f"capital=${capital:.2f} | paper={paper_mode} | "
            f"min_conf={min_confidence} | kelly_frac={kelly_fraction} | "
            f"model={'loaded' if self.predictor.is_loaded() else 'NOT loaded'}"
        )

    def decide(
        self,
        market_id: str,
        slug: str,
        asset_id_yes: str,
        asset_id_no: str,
        btc_ticks: Optional[pd.DataFrame],
        latest_snapshot_bids: list,
        latest_snapshot_asks: list,
        recent_trades: Optional[pd.DataFrame],
        share_price_yes: float,
        share_price_yes_prev: Optional[float],
        recent_outcomes: list[str],
    ) -> Decision:
        """
        Ejecuta un ciclo completo de decision para un mercado nuevo.

        Retorna Decision con toda la informacion (accion, sizing, etc).
        """
        ts_now = int(time.time() * 1000)

        # 1. Features en tiempo real
        features_df = build_realtime_features(
            btc_ticks=btc_ticks,
            latest_snapshot_bids=latest_snapshot_bids,
            latest_snapshot_asks=latest_snapshot_asks,
            recent_trades=recent_trades,
            share_price_yes=share_price_yes,
            share_price_yes_prev=share_price_yes_prev,
            recent_outcomes=recent_outcomes,
            ts_now_ms=ts_now
        )

        # 2. Prediccion
        prediction = self.predictor.predict(features_df)

        # 3. Regimen
        features_dict = features_df.iloc[0].to_dict() if not features_df.empty else {}
        regime = self.regime_detector.detect_from_features(features_dict)

        # 4. Senal
        ob_midpoint = features_dict.get("ob_midpoint", 0.5)
        ob_spread = features_dict.get("ob_spread", 0.04)

        signal = self.signal_gen.generate(
            prob_up=prediction["prob_up"],
            prob_down=prediction["prob_down"],
            confidence=prediction["confidence"],
            asset_id_yes=asset_id_yes,
            asset_id_no=asset_id_no,
            ob_midpoint=ob_midpoint,
            ob_spread=ob_spread,
            regime=regime,
        )

        # 5. Sizing (solo si no es SKIP)
        if signal.action != "SKIP":
            is_choppy = regime.regime == "choppy"
            sizing = self.sizer.calculate(
                capital=self.capital,
                prob_win=prediction["confidence"],
                buy_price=signal.target_price,
                is_choppy=is_choppy,
            )
        else:
            sizing = PositionSize(
                usdc_amount=0, n_shares=0, kelly_raw=0, kelly_fraction=0,
                risk_pct=0, fee_estimated=0, reason="SKIP — no sizing needed"
            )

        # 6. Construir Decision
        decision = Decision(
            timestamp_ms=ts_now,
            market_id=market_id,
            slug=slug,
            prob_up=prediction["prob_up"],
            prob_down=prediction["prob_down"],
            confidence=prediction["confidence"],
            model_loaded=prediction["model_loaded"],
            regime=regime.regime,
            regime_reason=regime.reason,
            action=signal.action,
            token_id=signal.token_id,
            order_type=signal.order_type,
            target_price=signal.target_price,
            signal_reason=signal.reason,
            usdc_amount=sizing.usdc_amount,
            n_shares=sizing.n_shares,
            kelly_raw=sizing.kelly_raw,
            fee_estimated=sizing.fee_estimated,
            sizing_reason=sizing.reason,
            paper_mode=self.paper_mode,
        )

        # 7. Log
        if decision.action == "SKIP":
            logger.info(f"DECISION: {decision.to_log()}")
        else:
            logger.success(f"DECISION: {decision.to_log()}")

        # 8. Registrar
        self.decisions.append(decision)

        return decision

    def update_capital(self, pnl: float) -> None:
        """Actualiza el capital despues de que un mercado se resuelve."""
        old = self.capital
        self.capital += pnl
        logger.info(f"Capital actualizado: ${old:.2f} -> ${self.capital:.2f} (PnL: ${pnl:+.2f})")

    def get_stats(self) -> dict:
        """Estadisticas del engine."""
        total = len(self.decisions)
        trades = [d for d in self.decisions if d.action != "SKIP"]
        skips = total - len(trades)

        return {
            "total_decisions": total,
            "total_trades":    len(trades),
            "total_skips":     skips,
            "current_capital": self.capital,
            "paper_mode":      self.paper_mode,
            "model_loaded":    self.predictor.is_loaded(),
        }
