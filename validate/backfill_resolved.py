"""
validate/backfill_resolved.py
-----------------------------
Script unico para retroactivamente resolver todos los mercados
que estan en active_markets con status='active' pero que ya cerraron.

Uso:
  python validate/backfill_resolved.py
"""

import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from data import storage
from data.rest_client import get_btc_5m_market_result


def main():
    storage.init_db()

    conn = storage.get_connection()
    rows = conn.execute(
        "SELECT slug, market_id FROM active_markets WHERE status='active' ORDER BY slug"
    ).fetchall()
    conn.close()

    print(f"Mercados activos a verificar: {len(rows)}")
    resolved_count = 0
    no_btc_count = 0

    for r in rows:
        slug = r["slug"]
        result = get_btc_5m_market_result(slug)
        if not result:
            print(f"  {slug} -> aun no resuelto o no encontrado")
            continue

        market_id = result["market_id"]

        # Extraer ts_interval del slug
        parts = slug.split("-")
        ts_interval = int(parts[-1]) if parts[-1].isdigit() else 0
        ts_open_ms = ts_interval * 1000
        ts_close_ms = (ts_interval + 300) * 1000

        btc_open = storage.get_btc_price_at(ts_open_ms, source="chainlink")
        if btc_open is None:
            btc_open = storage.get_btc_price_at(ts_open_ms, source="binance")

        btc_close = storage.get_btc_price_at(ts_close_ms, source="chainlink")
        if btc_close is None:
            btc_close = storage.get_btc_price_at(ts_close_ms, source="binance")

        storage.insert_resolved_market(
            market_id=market_id,
            asset_id_yes=result["asset_id_yes"],
            asset_id_no=result["asset_id_no"],
            winning_outcome=result["winning_outcome"],
            winning_asset=result.get("winning_asset_id", ""),
            question=result.get("question", ""),
            slug=result.get("slug", slug),
            btc_price_open=btc_open,
            btc_price_close=btc_close,
            ts_open=ts_open_ms if btc_open else None,
            ts_resolved=ts_close_ms
        )
        resolved_count += 1

        if btc_open and btc_close:
            d = "UP" if btc_close > btc_open else "DOWN"
            print(f"  {slug} -> {result['winning_outcome']} | "
                  f"BTC ${btc_open:,.2f} -> ${btc_close:,.2f} ({d})")
        else:
            no_btc_count += 1
            print(f"  {slug} -> {result['winning_outcome']} | no BTC prices in DB")

    print()
    stats = storage.get_db_stats()
    print(f"Resueltos insertados: {resolved_count}")
    print(f"Sin precios BTC: {no_btc_count}")
    print(f"Total resolved_markets en DB: {stats['resolved_markets']}")


if __name__ == "__main__":
    main()
