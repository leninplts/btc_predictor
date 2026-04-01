"""Test funcional completo de la Fase 3 — Strategy Engine."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("DATABASE_URL", None)

import pandas as pd
from data.storage import init_db, get_connection

init_db()

print("=" * 60)
print("  TEST FUNCIONAL — FASE 3: STRATEGY")
print("=" * 60)
print()

# 1. Regime Detector
print("--- 1. REGIME DETECTOR ---")
from strategy.regime_filter import RegimeDetector
rd = RegimeDetector()

r1 = rd.detect(atr=0.008, bb_width=0.01, recent_momentums=[0.1, 0.05, 0.12, 0.08])
print(f"  High vol:  {r1.regime:10s} | {r1.reason}")

r2 = rd.detect(atr=0.001, bb_width=0.002, recent_momentums=[0.01, -0.01, 0.005])
print(f"  Low vol:   {r2.regime:10s} | {r2.reason}")

r3 = rd.detect(atr=0.003, bb_width=0.005, recent_momentums=[0.1, -0.08, 0.05, -0.12])
print(f"  Choppy:    {r3.regime:10s} | {r3.reason}")

r4 = rd.detect(atr=0.004, bb_width=0.006, recent_momentums=[0.1, 0.12, 0.08, 0.15])
print(f"  Trending:  {r4.regime:10s} | {r4.reason}")
print()

# 2. Signal Generator
print("--- 2. SIGNAL GENERATOR ---")
from strategy.signal import SignalGenerator
sg = SignalGenerator()

s1 = sg.generate(prob_up=0.65, prob_down=0.35, confidence=0.65,
                  asset_id_yes="YES123", asset_id_no="NO456",
                  ob_midpoint=0.52, ob_spread=0.04, regime=r1)
print(f"  Conf 0.65: {s1.action:10s} | {s1.order_type:6s} @ {s1.target_price:.3f}")

s2 = sg.generate(prob_up=0.52, prob_down=0.48, confidence=0.52,
                  asset_id_yes="YES123", asset_id_no="NO456",
                  ob_midpoint=0.50, ob_spread=0.04, regime=r1)
print(f"  Conf 0.52: {s2.action:10s} | {s2.reason}")

s3 = sg.generate(prob_up=0.80, prob_down=0.20, confidence=0.80,
                  asset_id_yes="YES123", asset_id_no="NO456",
                  ob_midpoint=0.55, ob_spread=0.02, regime=r1)
print(f"  Conf 0.80: {s3.action:10s} | {s3.order_type:6s} @ {s3.target_price:.3f}")

s4 = sg.generate(prob_up=0.65, prob_down=0.35, confidence=0.65,
                  asset_id_yes="YES123", asset_id_no="NO456",
                  ob_midpoint=0.50, ob_spread=0.04, regime=r2)
print(f"  Low vol:   {s4.action:10s} | {s4.reason}")
print()

# 3. Position Sizer
print("--- 3. POSITION SIZER ---")
from strategy.sizing import PositionSizer
ps = PositionSizer()

p1 = ps.calculate(capital=1000, prob_win=0.65, buy_price=0.50)
print(f"  Normal:    ${p1.usdc_amount:.2f} | {p1.n_shares:.1f} shares | {p1.reason}")

p2 = ps.calculate(capital=1000, prob_win=0.55, buy_price=0.48, is_choppy=True)
print(f"  Choppy:    ${p2.usdc_amount:.2f} | {p2.n_shares:.1f} shares | {p2.reason}")

p3 = ps.calculate(capital=1000, prob_win=0.50, buy_price=0.50)
print(f"  50/50:     ${p3.usdc_amount:.2f} | {p3.reason}")
print()

# 4. Full Engine with real data
print("--- 4. STRATEGY ENGINE (datos reales de DB) ---")
from strategy.engine import StrategyEngine

engine = StrategyEngine(capital=1000.0, paper_mode=True, min_confidence=0.55)

conn = get_connection()

btc_rows = conn.execute(
    "SELECT ts, price FROM btc_prices WHERE source='chainlink' ORDER BY ts DESC LIMIT 500"
).fetchall()
btc_ticks = pd.DataFrame([dict(r) for r in btc_rows]) if btc_rows else None

snap = conn.execute(
    "SELECT bids, asks FROM orderbook_snapshots ORDER BY ts DESC LIMIT 1"
).fetchone()

trades_rows = conn.execute(
    "SELECT ts, price, size, side FROM last_trades ORDER BY ts DESC LIMIT 50"
).fetchall()
trades_df = pd.DataFrame([dict(r) for r in trades_rows]) if trades_rows else None

outcomes_rows = conn.execute(
    "SELECT winning_outcome FROM resolved_markets ORDER BY ts_resolved DESC LIMIT 10"
).fetchall()
outcomes = [r[0] for r in outcomes_rows][::-1] if outcomes_rows else []

conn.close()

bids = json.loads(snap[0]) if snap else []
asks = json.loads(snap[1]) if snap else []

decision = engine.decide(
    market_id="0xTEST",
    slug="btc-updown-5m-test",
    asset_id_yes="YES_TOKEN",
    asset_id_no="NO_TOKEN",
    btc_ticks=btc_ticks,
    latest_snapshot_bids=bids,
    latest_snapshot_asks=asks,
    recent_trades=trades_df,
    share_price_yes=0.52,
    share_price_yes_prev=0.50,
    recent_outcomes=outcomes,
)

print()
print("=== DECISION COMPLETA ===")
d = decision.to_dict()
for k in ["action", "prob_up", "prob_down", "confidence", "model_loaded",
           "regime", "order_type", "target_price", "usdc_amount", "n_shares",
           "kelly_raw", "fee_estimated", "paper_mode"]:
    v = d[k]
    if isinstance(v, float):
        print(f"  {k:20s} {v:.6f}")
    else:
        print(f"  {k:20s} {v}")

print()
print(f"  signal_reason: {d['signal_reason']}")
print(f"  sizing_reason: {d['sizing_reason']}")
print(f"  regime_reason: {d['regime_reason']}")

print()
stats = engine.get_stats()
print("=== ENGINE STATS ===")
for k, v in stats.items():
    print(f"  {k:20s} {v}")

print()
print("FASE 3: TEST COMPLETO [OK]")
