"""Quick check of resolved_markets data integrity."""
import sys, os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from data import storage

def main():
    conn = storage.get_connection()
    stats = storage.get_db_stats()

    print("=== DB STATS ===")
    for k, v in stats.items():
        print(f"  {k}: {v:,}")

    # Direction distribution
    rows = conn.execute(
        "SELECT direction, COUNT(*) as c FROM resolved_markets "
        "WHERE direction IS NOT NULL GROUP BY direction"
    ).fetchall()
    print("\n=== DIRECTION DISTRIBUTION ===")
    for r in rows:
        print(f"  {r['direction']}: {r['c']}")

    null_c = conn.execute(
        "SELECT COUNT(*) as c FROM resolved_markets WHERE direction IS NULL"
    ).fetchone()["c"]
    print(f"  NULL (no BTC prices): {null_c}")

    # Consistency: Yes should = UP, No should = DOWN
    yes_up = conn.execute(
        "SELECT COUNT(*) as c FROM resolved_markets "
        "WHERE winning_outcome='Yes' AND direction='UP'"
    ).fetchone()["c"]

    no_down = conn.execute(
        "SELECT COUNT(*) as c FROM resolved_markets "
        "WHERE winning_outcome='No' AND direction='DOWN'"
    ).fetchone()["c"]

    total_dir = conn.execute(
        "SELECT COUNT(*) as c FROM resolved_markets WHERE direction IS NOT NULL"
    ).fetchone()["c"]

    consistent = yes_up + no_down == total_dir
    print(f"\n=== CONSISTENCY CHECK ===")
    print(f"  Yes+UP:   {yes_up}")
    print(f"  No+DOWN:  {no_down}")
    print(f"  Total:    {total_dir}")
    print(f"  All match: {consistent}")

    if not consistent:
        mismatches = conn.execute(
            "SELECT slug, winning_outcome, direction FROM resolved_markets "
            "WHERE direction IS NOT NULL "
            "AND NOT (winning_outcome='Yes' AND direction='UP') "
            "AND NOT (winning_outcome='No' AND direction='DOWN')"
        ).fetchall()
        print("\n  MISMATCHES:")
        for m in mismatches:
            print(f"    {m['slug']}: outcome={m['winning_outcome']} dir={m['direction']}")

    # Sample of recent resolved
    recent = conn.execute(
        "SELECT slug, winning_outcome, direction, btc_price_open, btc_price_close "
        "FROM resolved_markets ORDER BY ts_resolved DESC LIMIT 5"
    ).fetchall()
    print("\n=== RECENT RESOLVED ===")
    for r in recent:
        if r["btc_price_open"] and r["btc_price_close"]:
            print(f"  {r['slug']} | {r['winning_outcome']} ({r['direction']}) "
                  f"| ${r['btc_price_open']:,.2f} -> ${r['btc_price_close']:,.2f}")
        else:
            print(f"  {r['slug']} | {r['winning_outcome']} | no BTC prices")

    conn.close()

if __name__ == "__main__":
    main()
