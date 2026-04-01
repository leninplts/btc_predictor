"""
execution/paper_wallet.py
-------------------------
Wallet demo para paper trading.

Simula una cuenta real de trading:
  - Capital en USDC
  - Posiciones abiertas (shares YES/NO por mercado)
  - Historial de trades (entradas, salidas, PnL)
  - Balance, ganancias/perdidas acumuladas
  - Porcentaje de efectividad (win rate)
  - Posibilidad de resetear

Cuando un mercado se resuelve, la wallet cierra la posicion
automaticamente y actualiza el PnL.

Persiste el estado en la DB para sobrevivir reinicios del bot.
"""

import json
import time
from dataclasses import dataclass, asdict, field
from typing import Optional
from datetime import datetime, timezone
from loguru import logger

from models.backtester import polymarket_fee
from data import storage


# ---------------------------------------------------------------------------
# Tipos
# ---------------------------------------------------------------------------

@dataclass
class OpenPosition:
    """Posicion abierta en un mercado."""
    market_id: str
    slug: str
    action: str           # "BUY_YES" | "BUY_NO"
    token_id: str
    buy_price: float      # precio al que compramos
    n_shares: float       # cantidad de shares
    usdc_invested: float  # USDC total invertido (incluyendo fee)
    fee_paid: float
    timestamp_ms: int
    prob_up: float        # prediccion del modelo al momento de entrar
    confidence: float


@dataclass
class ClosedTrade:
    """Trade cerrado (posicion resuelta)."""
    market_id: str
    slug: str
    action: str
    buy_price: float
    n_shares: float
    usdc_invested: float
    fee_paid: float
    winning_outcome: str   # "Yes" | "No"
    won: bool
    pnl: float             # ganancia o perdida neta
    pnl_pct: float         # PnL como % de la inversion
    open_ts: int
    close_ts: int
    prob_up: float
    confidence: float


# ---------------------------------------------------------------------------
# Paper Wallet
# ---------------------------------------------------------------------------

class PaperWallet:
    """
    Wallet demo que trackea todo el estado de paper trading.
    """

    def __init__(self, initial_capital: float = 1000.0):
        self.initial_capital = initial_capital
        self.capital = initial_capital         # USDC libre
        self.open_positions: dict[str, OpenPosition] = {}  # market_id -> position
        self.closed_trades: list[ClosedTrade] = []
        self.total_fees_paid = 0.0

        logger.info(f"PaperWallet inicializada: ${initial_capital:.2f} USDC")

    def open_position(
        self,
        market_id: str,
        slug: str,
        action: str,
        token_id: str,
        buy_price: float,
        usdc_amount: float,
        n_shares: float,
        fee: float,
        prob_up: float,
        confidence: float,
    ) -> bool:
        """
        Abre una posicion en un mercado.
        Retorna True si se pudo abrir, False si no hay capital suficiente.
        """
        total_cost = usdc_amount
        if total_cost > self.capital:
            logger.warning(
                f"Capital insuficiente: necesita ${total_cost:.2f} "
                f"pero solo hay ${self.capital:.2f}"
            )
            return False

        if market_id in self.open_positions:
            logger.warning(f"Ya hay posicion abierta en {slug}")
            return False

        self.capital -= total_cost
        self.total_fees_paid += fee

        pos = OpenPosition(
            market_id=market_id,
            slug=slug,
            action=action,
            token_id=token_id,
            buy_price=buy_price,
            n_shares=n_shares,
            usdc_invested=usdc_amount,
            fee_paid=fee,
            timestamp_ms=int(time.time() * 1000),
            prob_up=prob_up,
            confidence=confidence,
        )
        self.open_positions[market_id] = pos

        logger.info(
            f"PAPER OPEN | {action} {slug} | "
            f"{n_shares:.1f} shares @ {buy_price:.3f} | "
            f"${usdc_amount:.2f} | fee ${fee:.4f} | "
            f"capital restante: ${self.capital:.2f}"
        )
        return True

    def resolve_position(
        self,
        market_id: str,
        winning_outcome: str,
    ) -> Optional[ClosedTrade]:
        """
        Resuelve una posicion abierta cuando el mercado cierra.

        winning_outcome: "Yes" o "No"
        Retorna ClosedTrade o None si no habia posicion.
        """
        pos = self.open_positions.pop(market_id, None)
        if pos is None:
            return None

        # Determinar si ganamos
        won = (
            (pos.action == "BUY_YES" and winning_outcome == "Yes") or
            (pos.action == "BUY_NO" and winning_outcome == "No")
        )

        # PnL
        if won:
            # Recibimos $1.00 por share
            payout = pos.n_shares * 1.0
            pnl = payout - pos.usdc_invested
        else:
            # Recibimos $0.00 por share
            pnl = -pos.usdc_invested

        pnl_pct = (pnl / pos.usdc_invested * 100) if pos.usdc_invested > 0 else 0.0

        # Actualizar capital
        if won:
            self.capital += pos.usdc_invested + pnl  # devolver inversion + ganancia
        # Si perdemos, ya se descontaron los USDC al abrir

        close_ts = int(time.time() * 1000)

        trade = ClosedTrade(
            market_id=market_id,
            slug=pos.slug,
            action=pos.action,
            buy_price=pos.buy_price,
            n_shares=pos.n_shares,
            usdc_invested=pos.usdc_invested,
            fee_paid=pos.fee_paid,
            winning_outcome=winning_outcome,
            won=won,
            pnl=pnl,
            pnl_pct=pnl_pct,
            open_ts=pos.timestamp_ms,
            close_ts=close_ts,
            prob_up=pos.prob_up,
            confidence=pos.confidence,
        )
        self.closed_trades.append(trade)

        result_emoji = "WIN" if won else "LOSS"
        logger.info(
            f"PAPER {result_emoji} | {pos.action} {pos.slug} | "
            f"outcome={winning_outcome} | PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%) | "
            f"capital: ${self.capital:.2f}"
        )

        return trade

    def get_balance(self) -> dict:
        """Devuelve el estado completo de la wallet."""
        total_invested = sum(p.usdc_invested for p in self.open_positions.values())
        total_equity = self.capital + total_invested  # aproximacion

        n_trades = len(self.closed_trades)
        n_wins = sum(1 for t in self.closed_trades if t.won)
        n_losses = n_trades - n_wins
        win_rate = n_wins / n_trades if n_trades > 0 else 0.0

        total_pnl = sum(t.pnl for t in self.closed_trades)
        total_pnl_pct = (total_pnl / self.initial_capital * 100) if self.initial_capital > 0 else 0.0

        # Mejor y peor trade
        if self.closed_trades:
            best_trade = max(self.closed_trades, key=lambda t: t.pnl)
            worst_trade = min(self.closed_trades, key=lambda t: t.pnl)
        else:
            best_trade = None
            worst_trade = None

        # Racha actual
        streak = 0
        streak_type = ""
        for t in reversed(self.closed_trades):
            if streak == 0:
                streak_type = "W" if t.won else "L"
                streak = 1
            elif (t.won and streak_type == "W") or (not t.won and streak_type == "L"):
                streak += 1
            else:
                break

        return {
            "capital_libre":       round(self.capital, 2),
            "capital_invertido":   round(total_invested, 2),
            "equity_total":        round(total_equity, 2),
            "capital_inicial":     self.initial_capital,
            "pnl_total":           round(total_pnl, 2),
            "pnl_total_pct":       round(total_pnl_pct, 2),
            "fees_total":          round(self.total_fees_paid, 4),
            "trades_totales":      n_trades,
            "wins":                n_wins,
            "losses":              n_losses,
            "win_rate":            round(win_rate, 4),
            "posiciones_abiertas": len(self.open_positions),
            "racha":               f"{streak}{streak_type}" if streak > 0 else "0",
            "mejor_trade_pnl":     round(best_trade.pnl, 2) if best_trade else 0,
            "peor_trade_pnl":      round(worst_trade.pnl, 2) if worst_trade else 0,
        }

    def get_open_positions_summary(self) -> list[dict]:
        """Lista resumida de posiciones abiertas."""
        result = []
        for pos in self.open_positions.values():
            result.append({
                "slug": pos.slug,
                "action": pos.action,
                "buy_price": pos.buy_price,
                "n_shares": round(pos.n_shares, 1),
                "usdc": round(pos.usdc_invested, 2),
                "confidence": round(pos.confidence, 3),
            })
        return result

    def get_recent_trades(self, n: int = 5) -> list[dict]:
        """Ultimos N trades cerrados."""
        result = []
        for t in self.closed_trades[-n:]:
            result.append({
                "slug": t.slug,
                "action": t.action,
                "won": t.won,
                "pnl": round(t.pnl, 2),
                "pnl_pct": round(t.pnl_pct, 1),
                "outcome": t.winning_outcome,
                "confidence": round(t.confidence, 3),
            })
        return result

    def reset(self, new_capital: Optional[float] = None) -> None:
        """Resetea la wallet al estado inicial."""
        cap = new_capital if new_capital is not None else self.initial_capital
        self.initial_capital = cap
        self.capital = cap
        self.open_positions.clear()
        self.closed_trades.clear()
        self.total_fees_paid = 0.0
        logger.info(f"PaperWallet reseteada: ${cap:.2f} USDC")
