"""
Script temporal para diagnosticar si llega el evento market_resolved.
Escucha cruzando el cierre del mercado actual.
"""
import asyncio
import json
import time
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import websockets
from data.rest_client import (
    get_active_btc_5m_market, GAMMA_BASE, SESSION, _parse_token_ids
)


async def watch_resolution():
    now_ts  = int(time.time())
    rounded = (now_ts // 300) * 300
    secs_remaining = 300 - (now_ts - rounded)

    # Mercado actual
    m = get_active_btc_5m_market()
    if not m:
        print("No hay mercado activo")
        return

    # Mercado siguiente (para suscribirnos antes de que abra)
    next_slug = "btc-updown-5m-" + str(rounded + 300)
    r = SESSION.get(GAMMA_BASE + "/events", params={"slug": next_slug}).json()
    events = r if isinstance(r, list) else [r]
    next_ids = []
    for e in events:
        for mkt in e.get("markets", []):
            tids = _parse_token_ids(mkt.get("clobTokenIds"))
            if len(tids) >= 2:
                next_ids = tids
                break

    all_ids = [m["asset_id_yes"], m["asset_id_no"]] + next_ids

    print("Mercado actual :", m["question"])
    print("Slug           :", m["slug"])
    print("Cierre en      :", secs_remaining, "segundos")
    print("Asset IDs      :", len(all_ids), "(actual + siguiente)")
    print("Escuchando hasta 120s despues del cierre...")
    print()

    url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    sub = json.dumps({
        "assets_ids": all_ids,
        "type": "market",
        "custom_feature_enabled": True
    })

    event_log  = []
    deadline   = time.time() + secs_remaining + 120
    last_ping  = time.time()
    event_counts = {}

    async with websockets.connect(url, ping_interval=None, open_timeout=10) as ws:
        await ws.send(sub)
        print(f"[{time.strftime('%H:%M:%S')}] WebSocket conectado y suscrito.")

        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                if isinstance(raw, str) and raw.strip() in ("PONG", "pong"):
                    continue

                parsed = json.loads(raw)
                msgs = parsed if isinstance(parsed, list) else [parsed]

                for msg in msgs:
                    etype = msg.get("event_type", "")
                    event_counts[etype] = event_counts.get(etype, 0) + 1

                    # Imprimir TODOS los eventos que no sean ruido normal
                    if etype not in ("price_change", "last_trade_price", "best_bid_ask"):
                        ts_now = time.strftime("%H:%M:%S")
                        print(f"[{ts_now}] {etype}")
                        for k in ("question", "slug", "winning_outcome",
                                  "winning_asset_id", "market", "asset_id"):
                            if msg.get(k):
                                print(f"  {k}: {msg[k]}")
                        print()
                        event_log.append(etype)

            except asyncio.TimeoutError:
                remaining = deadline - time.time()
                ts_now = time.strftime("%H:%M:%S")
                print(f"[{ts_now}] esperando... ({remaining:.0f}s restantes) "
                      f"| eventos: {dict(list(event_counts.items())[:4])}")

            if time.time() - last_ping > 10:
                await ws.send("PING")
                last_ping = time.time()

    print()
    print("=" * 50)
    print("Resumen de eventos recibidos:")
    for k, v in sorted(event_counts.items()):
        print(f"  {k}: {v}")
    print()
    if "market_resolved" in event_log:
        print("RESULTADO: market_resolved SI llego")
    else:
        print("RESULTADO: market_resolved NO llego en este intervalo")


if __name__ == "__main__":
    asyncio.run(watch_resolution())
