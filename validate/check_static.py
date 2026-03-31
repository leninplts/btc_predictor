"""
validate/check_static.py
------------------------
Nivel 1 — Validacion estatica: sin red, sin credenciales.

Verifica que el codigo del proyecto es correcto en aislamiento:
  - Imports sin errores
  - DB se inicializa con las tablas y columnas esperadas
  - Logica de parseo y calculos internos es correcta
  - Funciones de insercion y consulta funcionan end-to-end

Usa una DB SQLite en archivo temporal para no tocar pipeline.db.

Uso:
  python validate/check_static.py
"""

import sys
import os
import json
import sqlite3
import tempfile
import traceback
from typing import Callable

# Asegurar que el root del proyecto esta en el path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ---------------------------------------------------------------------------
# Utilidades de reporte
# ---------------------------------------------------------------------------

RESULTS: list[tuple[str, str, str]] = []   # (status, nombre, mensaje)

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def _ok(name: str, msg: str = "") -> None:
    RESULTS.append(("OK", name, msg))
    print(f"  {GREEN}[OK]  {RESET} {name}" + (f" — {msg}" if msg else ""))

def _fail(name: str, msg: str) -> None:
    RESULTS.append(("FAIL", name, msg))
    print(f"  {RED}[FAIL]{RESET} {name} — {msg}")

def _run(name: str, fn: Callable) -> None:
    """Ejecuta un test y captura excepciones."""
    try:
        fn()
    except AssertionError as e:
        _fail(name, str(e) or "assertion fallida")
    except Exception as e:
        _fail(name, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Fixture: DB temporal
# ---------------------------------------------------------------------------

def _make_temp_db() -> tuple[str, Callable]:
    """
    Crea un archivo DB temporal.
    Devuelve (path, patch_fn) donde patch_fn parchea storage.DB_PATH
    apuntando al archivo temporal en lugar de pipeline.db.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return tmp.name


def _patch_db_path(tmp_path: str):
    """Reemplaza storage.DB_PATH con tmp_path."""
    import data.storage as storage
    storage.DB_PATH = tmp_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_imports():
    """Todos los modulos del proyecto importan sin error."""
    modules = [
        "data.storage",
        "data.rest_client",
        "data.websocket_client",
    ]
    for mod in modules:
        __import__(mod)
    # Verificar que los simbolos clave existen
    from data.storage import (
        init_db, get_connection, insert_btc_price,
        insert_orderbook_snapshot, insert_price_change,
        insert_last_trade, upsert_active_market,
        insert_resolved_market, get_latest_btc_price,
        get_active_markets, get_db_stats
    )
    from data.rest_client import (
        get_active_btc_5m_market, get_order_book,
        get_midpoint, get_spread, get_share_price_history,
        ping_clob, ping_gamma, _parse_token_ids
    )
    from data.websocket_client import BotState, run_pipeline
    _ok("test_imports", "todos los modulos y simbolos clave presentes")


def test_db_tables():
    """init_db() crea las 6 tablas con las columnas correctas."""
    tmp = _make_temp_db()
    _patch_db_path(tmp)

    import data.storage as storage
    storage.init_db()

    conn = sqlite3.connect(tmp)
    cursor = conn.cursor()

    expected_tables = {
        "btc_prices":          {"id", "ts", "ts_recv", "source", "symbol", "price"},
        "orderbook_snapshots": {"id", "ts", "ts_recv", "asset_id", "market_id", "bids", "asks", "hash"},
        "price_changes":       {"id", "ts", "ts_recv", "asset_id", "market_id", "price", "size", "side", "best_bid", "best_ask", "hash"},
        "last_trades":         {"id", "ts", "ts_recv", "asset_id", "market_id", "price", "size", "side", "fee_rate_bps"},
        "resolved_markets":    {"id", "market_id", "asset_id_yes", "asset_id_no", "question", "slug",
                                "winning_outcome", "winning_asset", "btc_price_open", "btc_price_close",
                                "direction", "ts_open", "ts_resolved", "ts_recv"},
        "active_markets":      {"id", "market_id", "asset_id_yes", "asset_id_no", "question", "slug",
                                "description", "status", "ts_discovered", "ts_updated"},
    }

    for table, expected_cols in expected_tables.items():
        cursor.execute(f"PRAGMA table_info({table})")
        rows = cursor.fetchall()
        assert rows, f"Tabla '{table}' no existe"
        actual_cols = {row[1] for row in rows}
        missing = expected_cols - actual_cols
        assert not missing, f"Tabla '{table}' le faltan columnas: {missing}"

    conn.close()
    os.unlink(tmp)
    _ok("test_db_tables", "6 tablas con columnas correctas")


def test_parse_token_ids():
    """_parse_token_ids maneja lista, string JSON, invalido y None."""
    from data.rest_client import _parse_token_ids

    # Lista Python directa
    result = _parse_token_ids(["123", "456"])
    assert result == ["123", "456"], f"Lista directa fallo: {result}"

    # String JSON (como devuelve la Gamma API)
    raw_json = '["111111", "222222"]'
    result = _parse_token_ids(raw_json)
    assert result == ["111111", "222222"], f"String JSON fallo: {result}"

    # String invalido → lista vacia
    result = _parse_token_ids("no-es-json")
    assert result == [], f"String invalido deberia devolver []: {result}"

    # None → lista vacia
    result = _parse_token_ids(None)
    assert result == [], f"None deberia devolver []: {result}"

    # Lista vacia
    result = _parse_token_ids([])
    assert result == [], f"Lista vacia fallo: {result}"

    _ok("test_parse_token_ids", "5 casos cubiertos (lista, json, invalido, None, vacio)")


def test_botstate():
    """BotState inicializa correctamente y sus metodos funcionan."""
    from data.websocket_client import BotState

    state = BotState()

    # Estado inicial
    assert state.asset_ids == [], f"asset_ids deberia ser [] al inicio: {state.asset_ids}"
    assert state.active_market_id is None, "active_market_id deberia ser None al inicio"
    assert state.running is True, "running deberia ser True al inicio"
    assert state.last_btc_price_binance is None
    assert state.last_btc_price_chainlink is None

    # update_market
    state.update_market("0xABCD", "yes_token_001", "no_token_002")
    assert state.active_market_id == "0xABCD"
    assert state.asset_ids == ["yes_token_001", "no_token_002"]

    # stop
    state.stop()
    assert state.running is False, "running deberia ser False tras stop()"

    _ok("test_botstate", "init, update_market y stop verificados")


def test_db_insert_btc_price():
    """insert_btc_price guarda y get_latest_btc_price recupera el valor."""
    tmp = _make_temp_db()
    _patch_db_path(tmp)

    import data.storage as storage
    storage.init_db()

    storage.insert_btc_price(source="binance", symbol="btcusdt", price=82500.50, ts=1700000000000)
    storage.insert_btc_price(source="binance", symbol="btcusdt", price=82600.00, ts=1700000001000)
    storage.insert_btc_price(source="chainlink", symbol="btc/usd", price=82590.25, ts=1700000002000)

    latest_binance = storage.get_latest_btc_price("binance")
    assert latest_binance is not None, "get_latest_btc_price(binance) devolvio None"
    assert latest_binance["price"] == 82600.00, f"Precio incorrecto: {latest_binance['price']}"
    assert latest_binance["source"] == "binance"

    latest_chainlink = storage.get_latest_btc_price("chainlink")
    assert latest_chainlink is not None, "get_latest_btc_price(chainlink) devolvio None"
    assert latest_chainlink["price"] == 82590.25

    conn = sqlite3.connect(tmp)
    count = conn.execute("SELECT COUNT(*) FROM btc_prices").fetchone()[0]
    conn.close()
    assert count == 3, f"Esperaba 3 registros en btc_prices, hay {count}"

    os.unlink(tmp)
    _ok("test_db_insert_btc_price", "insert + get_latest para binance y chainlink")


def test_db_insert_orderbook():
    """insert_orderbook_snapshot guarda bids/asks como JSON y se puede recuperar."""
    tmp = _make_temp_db()
    _patch_db_path(tmp)

    import data.storage as storage
    storage.init_db()

    bids = [{"price": "0.48", "size": "100"}, {"price": "0.47", "size": "200"}]
    asks = [{"price": "0.52", "size": "150"}, {"price": "0.53", "size": "300"}]

    storage.insert_orderbook_snapshot(
        ts=1700000000000,
        asset_id="yes_token_abc",
        market_id="0xcondition123",
        bids=bids,
        asks=asks,
        hash_val="0xhash001"
    )

    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM orderbook_snapshots LIMIT 1").fetchone()
    assert row is not None, "No se encontro el snapshot"
    assert row["asset_id"] == "yes_token_abc"
    assert row["market_id"] == "0xcondition123"
    assert row["hash"] == "0xhash001"

    # Verificar que bids/asks son JSON valido y coinciden
    bids_recovered = json.loads(row["bids"])
    asks_recovered = json.loads(row["asks"])
    assert bids_recovered == bids, f"bids no coinciden: {bids_recovered}"
    assert asks_recovered == asks, f"asks no coinciden: {asks_recovered}"
    assert len(bids_recovered) == 2
    assert len(asks_recovered) == 2

    conn.close()
    os.unlink(tmp)
    _ok("test_db_insert_orderbook", "bids/asks serializados y deserializados correctamente")


def test_db_insert_trade():
    """insert_last_trade guarda un trade y se puede consultar."""
    tmp = _make_temp_db()
    _patch_db_path(tmp)

    import data.storage as storage
    storage.init_db()

    storage.insert_last_trade(
        ts=1700000000500,
        asset_id="yes_token_xyz",
        market_id="0xconditionXYZ",
        price=0.54,
        size=50.5,
        side="BUY",
        fee_rate_bps="0"
    )

    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM last_trades LIMIT 1").fetchone()
    assert row is not None, "No se encontro el trade"
    assert abs(row["price"] - 0.54) < 1e-9, f"Precio incorrecto: {row['price']}"
    assert abs(row["size"] - 50.5) < 1e-9, f"Size incorrecto: {row['size']}"
    assert row["side"] == "BUY"
    assert row["fee_rate_bps"] == "0"

    conn.close()
    os.unlink(tmp)
    _ok("test_db_insert_trade", "trade guardado con precio, size, side y fee correctos")


def test_db_resolved_direction():
    """insert_resolved_market calcula UP/DOWN correctamente segun precios."""
    tmp = _make_temp_db()
    _patch_db_path(tmp)

    import data.storage as storage
    storage.init_db()

    # Caso UP: precio cierre > precio apertura
    storage.insert_resolved_market(
        market_id="0xUP001",
        asset_id_yes="yes_001",
        asset_id_no="no_001",
        winning_outcome="Yes",
        winning_asset="yes_001",
        btc_price_open=80000.0,
        btc_price_close=82000.0,
        ts_resolved=1700000300000
    )

    # Caso DOWN: precio cierre < precio apertura
    storage.insert_resolved_market(
        market_id="0xDOWN001",
        asset_id_yes="yes_002",
        asset_id_no="no_002",
        winning_outcome="No",
        winning_asset="no_002",
        btc_price_open=82000.0,
        btc_price_close=81500.0,
        ts_resolved=1700000600000
    )

    # Caso sin precios: direction debe ser NULL
    storage.insert_resolved_market(
        market_id="0xNULL001",
        asset_id_yes="yes_003",
        asset_id_no="no_003",
        winning_outcome="Yes",
        winning_asset="yes_003",
        btc_price_open=None,
        btc_price_close=None,
        ts_resolved=1700000900000
    )

    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row

    row_up = conn.execute(
        "SELECT direction FROM resolved_markets WHERE market_id=?", ("0xUP001",)
    ).fetchone()
    assert row_up["direction"] == "UP", f"Esperaba UP, got: {row_up['direction']}"

    row_down = conn.execute(
        "SELECT direction FROM resolved_markets WHERE market_id=?", ("0xDOWN001",)
    ).fetchone()
    assert row_down["direction"] == "DOWN", f"Esperaba DOWN, got: {row_down['direction']}"

    row_null = conn.execute(
        "SELECT direction FROM resolved_markets WHERE market_id=?", ("0xNULL001",)
    ).fetchone()
    assert row_null["direction"] is None, f"Esperaba NULL, got: {row_null['direction']}"

    conn.close()
    os.unlink(tmp)
    _ok("test_db_resolved_direction", "UP, DOWN y NULL calculados correctamente")


def test_db_upsert_market():
    """upsert_active_market no duplica y actualiza en segundo llamado."""
    tmp = _make_temp_db()
    _patch_db_path(tmp)

    import data.storage as storage
    storage.init_db()

    storage.upsert_active_market(
        market_id="0xMARKET001",
        asset_id_yes="yes_111",
        asset_id_no="no_111",
        question="Will BTC go up?",
        slug="btc-updown-5m-12345"
    )
    storage.upsert_active_market(
        market_id="0xMARKET001",
        asset_id_yes="yes_111",
        asset_id_no="no_111",
        question="Will BTC go up? (updated)",
        slug="btc-updown-5m-12345"
    )

    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row

    count = conn.execute(
        "SELECT COUNT(*) as c FROM active_markets WHERE market_id=?", ("0xMARKET001",)
    ).fetchone()["c"]
    assert count == 1, f"Upsert duplico el registro: count={count}"

    row = conn.execute(
        "SELECT question FROM active_markets WHERE market_id=?", ("0xMARKET001",)
    ).fetchone()
    assert "updated" in row["question"], f"Upsert no actualizo question: {row['question']}"

    conn.close()
    os.unlink(tmp)
    _ok("test_db_upsert_market", "segundo upsert actualiza sin duplicar")


def test_db_stats():
    """get_db_stats devuelve dict con las 6 tablas esperadas."""
    tmp = _make_temp_db()
    _patch_db_path(tmp)

    import data.storage as storage
    storage.init_db()

    stats = storage.get_db_stats()

    expected_keys = {
        "btc_prices", "orderbook_snapshots", "price_changes",
        "last_trades", "resolved_markets", "active_markets"
    }
    missing = expected_keys - set(stats.keys())
    assert not missing, f"get_db_stats() no tiene estas tablas: {missing}"

    for key, val in stats.items():
        assert isinstance(val, int), f"stats[{key}] deberia ser int, es {type(val)}"
        assert val == 0, f"stats[{key}] deberia ser 0 en DB vacia, es {val}"

    os.unlink(tmp)
    _ok("test_db_stats", "6 tablas presentes, todas con count=0 en DB vacia")


def test_db_insert_price_change():
    """insert_price_change guarda correctamente con best_bid/ask opcionales."""
    tmp = _make_temp_db()
    _patch_db_path(tmp)

    import data.storage as storage
    storage.init_db()

    # Con best_bid/ask
    storage.insert_price_change(
        ts=1700000000000,
        asset_id="yes_token_abc",
        market_id="0xcondition123",
        price=0.50,
        size=100.0,
        side="BUY",
        best_bid=0.49,
        best_ask=0.51
    )
    # Sin best_bid/ask (None)
    storage.insert_price_change(
        ts=1700000001000,
        asset_id="yes_token_abc",
        market_id="0xcondition123",
        price=0.49,
        size=0.0,   # nivel eliminado
        side="SELL"
    )

    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM price_changes ORDER BY ts").fetchall()
    assert len(rows) == 2, f"Esperaba 2 price_changes, hay {len(rows)}"

    assert abs(rows[0]["best_bid"] - 0.49) < 1e-9
    assert abs(rows[0]["best_ask"] - 0.51) < 1e-9
    assert rows[1]["best_bid"] is None
    assert rows[1]["size"] == 0.0

    conn.close()
    os.unlink(tmp)
    _ok("test_db_insert_price_change", "con y sin best_bid/ask, size=0 para nivel eliminado")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

ALL_TESTS = [
    ("test_imports",               test_imports),
    ("test_db_tables",             test_db_tables),
    ("test_parse_token_ids",       test_parse_token_ids),
    ("test_botstate",              test_botstate),
    ("test_db_insert_btc_price",   test_db_insert_btc_price),
    ("test_db_insert_orderbook",   test_db_insert_orderbook),
    ("test_db_insert_trade",       test_db_insert_trade),
    ("test_db_resolved_direction", test_db_resolved_direction),
    ("test_db_upsert_market",      test_db_upsert_market),
    ("test_db_stats",              test_db_stats),
    ("test_db_insert_price_change",test_db_insert_price_change),
]


def main():
    print()
    print(f"{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  VALIDACION FASE 1 — Nivel 1: Estatica (sin red){RESET}")
    print(f"{BOLD}{'='*60}{RESET}")
    print()

    for name, fn in ALL_TESTS:
        _run(name, fn)

    ok_count   = sum(1 for s, _, _ in RESULTS if s == "OK")
    fail_count = sum(1 for s, _, _ in RESULTS if s == "FAIL")

    print()
    print(f"{BOLD}{'-'*60}{RESET}")
    print(f"  RESULTADO: {GREEN}{ok_count} OK{RESET} | {RED}{fail_count} FAIL{RESET}")

    if fail_count == 0:
        print(f"  {GREEN}{BOLD}Nivel 1: PASS [OK]{RESET}")
    else:
        print(f"  {RED}{BOLD}Nivel 1: FAIL [FAIL]{RESET}")
        print()
        print("  Tests fallidos:")
        for status, name, msg in RESULTS:
            if status == "FAIL":
                print(f"    - {name}: {msg}")

    print(f"{BOLD}{'='*60}{RESET}")
    print()

    sys.exit(0 if fail_count == 0 else 1)


if __name__ == "__main__":
    main()
