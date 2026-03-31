"""
storage.py
----------
Capa de persistencia para el pipeline de datos.
Soporta PostgreSQL (produccion/VPS) y SQLite (desarrollo local).

Se selecciona automaticamente segun la variable de entorno DATABASE_URL:
  - Si DATABASE_URL existe -> PostgreSQL
  - Si no -> SQLite local en data/pipeline.db

Tablas:
  - btc_prices          : precios BTC en tiempo real (Binance + Chainlink)
  - orderbook_snapshots : snapshots completos del order book de Polymarket
  - price_changes       : cambios nivel a nivel del order book (tick data)
  - last_trades         : ultimo precio de cada trade ejecutado en Polymarket
  - resolved_markets    : mercados BTC 5-min resueltos con su resultado
  - active_markets      : registro de mercados activos descubiertos
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional
from loguru import logger


# ---------------------------------------------------------------------------
# Deteccion automatica del backend
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES = DATABASE_URL.startswith("postgres")

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras

# Path para SQLite (solo usado si USE_POSTGRES es False)
SQLITE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "pipeline.db")


# ---------------------------------------------------------------------------
# Placeholder: PostgreSQL usa %s, SQLite usa ?
# ---------------------------------------------------------------------------

PH = "%s" if USE_POSTGRES else "?"


# ---------------------------------------------------------------------------
# Conexion
# ---------------------------------------------------------------------------

def get_connection():
    """Abre una conexion a la DB activa (PostgreSQL o SQLite)."""
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        return conn
    else:
        os.makedirs(os.path.dirname(SQLITE_PATH), exist_ok=True)
        conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn


def _fetchone(cur, query: str, params: tuple = ()) -> Optional[dict]:
    """Ejecuta query y devuelve un dict o None."""
    cur.execute(query, params)
    row = cur.fetchone()
    if row is None:
        return None
    if USE_POSTGRES:
        cols = [desc[0] for desc in cur.description]
        return dict(zip(cols, row))
    return dict(row)


def _fetchall(cur, query: str, params: tuple = ()) -> list[dict]:
    """Ejecuta query y devuelve lista de dicts."""
    cur.execute(query, params)
    rows = cur.fetchall()
    if USE_POSTGRES:
        cols = [desc[0] for desc in cur.description]
        return [dict(zip(cols, row)) for row in rows]
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Inicializacion de tablas
# ---------------------------------------------------------------------------

_PG_SERIAL = "SERIAL" if USE_POSTGRES else "INTEGER"
_PG_AUTOINCREMENT = "" if USE_POSTGRES else "AUTOINCREMENT"
_PG_PK = f"{_PG_SERIAL} PRIMARY KEY {_PG_AUTOINCREMENT}".strip()

# ON CONFLICT en PostgreSQL es identico en sintaxis a SQLite >=3.24
# INSERT OR IGNORE -> en PG usamos ON CONFLICT DO NOTHING


def init_db() -> None:
    """Crea todas las tablas si no existen. Idempotente."""
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS btc_prices (
            id          {_PG_PK},
            ts          BIGINT NOT NULL,
            ts_recv     BIGINT NOT NULL,
            source      TEXT   NOT NULL,
            symbol      TEXT   NOT NULL,
            price       DOUBLE PRECISION NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_btc_prices_ts ON btc_prices(ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_btc_prices_source ON btc_prices(source, ts)")

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS orderbook_snapshots (
            id          {_PG_PK},
            ts          BIGINT NOT NULL,
            ts_recv     BIGINT NOT NULL,
            asset_id    TEXT   NOT NULL,
            market_id   TEXT   NOT NULL,
            bids        TEXT   NOT NULL,
            asks        TEXT   NOT NULL,
            hash        TEXT
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ob_ts ON orderbook_snapshots(ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ob_asset ON orderbook_snapshots(asset_id, ts)")

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS price_changes (
            id          {_PG_PK},
            ts          BIGINT NOT NULL,
            ts_recv     BIGINT NOT NULL,
            asset_id    TEXT   NOT NULL,
            market_id   TEXT   NOT NULL,
            price       DOUBLE PRECISION NOT NULL,
            size        DOUBLE PRECISION NOT NULL,
            side        TEXT   NOT NULL,
            best_bid    DOUBLE PRECISION,
            best_ask    DOUBLE PRECISION,
            hash        TEXT
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pc_ts ON price_changes(ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pc_asset ON price_changes(asset_id, ts)")

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS last_trades (
            id          {_PG_PK},
            ts          BIGINT NOT NULL,
            ts_recv     BIGINT NOT NULL,
            asset_id    TEXT   NOT NULL,
            market_id   TEXT   NOT NULL,
            price       DOUBLE PRECISION NOT NULL,
            size        DOUBLE PRECISION NOT NULL,
            side        TEXT   NOT NULL,
            fee_rate_bps TEXT
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_lt_ts ON last_trades(ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_lt_asset ON last_trades(asset_id, ts)")

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS resolved_markets (
            id              {_PG_PK},
            market_id       TEXT NOT NULL UNIQUE,
            asset_id_yes    TEXT NOT NULL,
            asset_id_no     TEXT NOT NULL,
            question        TEXT,
            slug            TEXT,
            winning_outcome TEXT,
            winning_asset   TEXT,
            btc_price_open  DOUBLE PRECISION,
            btc_price_close DOUBLE PRECISION,
            direction       TEXT,
            ts_open         BIGINT,
            ts_resolved     BIGINT NOT NULL,
            ts_recv         BIGINT NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_rm_ts ON resolved_markets(ts_resolved)")

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS active_markets (
            id              {_PG_PK},
            market_id       TEXT NOT NULL UNIQUE,
            asset_id_yes    TEXT NOT NULL,
            asset_id_no     TEXT NOT NULL,
            question        TEXT,
            slug            TEXT,
            description     TEXT,
            status          TEXT DEFAULT 'active',
            ts_discovered   BIGINT NOT NULL,
            ts_updated      BIGINT NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_am_status ON active_markets(status)")

    conn.commit()
    conn.close()

    backend = f"PostgreSQL ({DATABASE_URL.split('@')[-1].split('/')[0]})" if USE_POSTGRES \
              else f"SQLite ({os.path.abspath(SQLITE_PATH)})"
    logger.info(f"Base de datos inicializada: {backend}")


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    """Timestamp actual en milisegundos UTC."""
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _insert_or_ignore_prefix() -> str:
    """INSERT OR IGNORE para SQLite, INSERT ... ON CONFLICT DO NOTHING para PG."""
    return "INSERT INTO" if USE_POSTGRES else "INSERT OR IGNORE INTO"


# ---------------------------------------------------------------------------
# Funciones de insercion
# ---------------------------------------------------------------------------

def insert_btc_price(source: str, symbol: str, price: float, ts: int) -> None:
    """Guarda un tick de precio BTC."""
    conn = get_connection()
    try:
        conn.cursor().execute(
            f"INSERT INTO btc_prices (ts, ts_recv, source, symbol, price) "
            f"VALUES ({PH},{PH},{PH},{PH},{PH})",
            (ts, _now_ms(), source, symbol, price)
        )
        conn.commit()
    finally:
        conn.close()


def insert_orderbook_snapshot(
    ts: int,
    asset_id: str,
    market_id: str,
    bids: list,
    asks: list,
    hash_val: Optional[str] = None
) -> None:
    """Guarda un snapshot completo del order book."""
    conn = get_connection()
    try:
        conn.cursor().execute(
            f"INSERT INTO orderbook_snapshots "
            f"(ts, ts_recv, asset_id, market_id, bids, asks, hash) "
            f"VALUES ({PH},{PH},{PH},{PH},{PH},{PH},{PH})",
            (ts, _now_ms(), asset_id, market_id,
             json.dumps(bids), json.dumps(asks), hash_val)
        )
        conn.commit()
    finally:
        conn.close()


def insert_price_change(
    ts: int,
    asset_id: str,
    market_id: str,
    price: float,
    size: float,
    side: str,
    best_bid: Optional[float] = None,
    best_ask: Optional[float] = None,
    hash_val: Optional[str] = None
) -> None:
    """Guarda un cambio de nivel en el order book."""
    conn = get_connection()
    try:
        conn.cursor().execute(
            f"INSERT INTO price_changes "
            f"(ts, ts_recv, asset_id, market_id, price, size, side, best_bid, best_ask, hash) "
            f"VALUES ({PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH})",
            (ts, _now_ms(), asset_id, market_id,
             price, size, side, best_bid, best_ask, hash_val)
        )
        conn.commit()
    finally:
        conn.close()


def insert_last_trade(
    ts: int,
    asset_id: str,
    market_id: str,
    price: float,
    size: float,
    side: str,
    fee_rate_bps: Optional[str] = None
) -> None:
    """Guarda el precio del ultimo trade ejecutado."""
    conn = get_connection()
    try:
        conn.cursor().execute(
            f"INSERT INTO last_trades "
            f"(ts, ts_recv, asset_id, market_id, price, size, side, fee_rate_bps) "
            f"VALUES ({PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH})",
            (ts, _now_ms(), asset_id, market_id,
             price, size, side, fee_rate_bps)
        )
        conn.commit()
    finally:
        conn.close()


def upsert_active_market(
    market_id: str,
    asset_id_yes: str,
    asset_id_no: str,
    question: str = "",
    slug: str = "",
    description: str = ""
) -> None:
    """Registra o actualiza un mercado activo."""
    conn = get_connection()
    now = _now_ms()
    try:
        conn.cursor().execute(
            f"INSERT INTO active_markets "
            f"(market_id, asset_id_yes, asset_id_no, question, slug, description, "
            f" status, ts_discovered, ts_updated) "
            f"VALUES ({PH},{PH},{PH},{PH},{PH},{PH},'active',{PH},{PH}) "
            f"ON CONFLICT(market_id) DO UPDATE SET "
            f"  question=EXCLUDED.question, "
            f"  slug=EXCLUDED.slug, "
            f"  description=EXCLUDED.description, "
            f"  status='active', "
            f"  ts_updated=EXCLUDED.ts_updated",
            (market_id, asset_id_yes, asset_id_no,
             question, slug, description, now, now)
        )
        conn.commit()
    finally:
        conn.close()


def insert_resolved_market(
    market_id: str,
    asset_id_yes: str,
    asset_id_no: str,
    winning_outcome: str,
    winning_asset: str,
    question: str = "",
    slug: str = "",
    btc_price_open: Optional[float] = None,
    btc_price_close: Optional[float] = None,
    ts_open: Optional[int] = None,
    ts_resolved: Optional[int] = None
) -> None:
    """Guarda un mercado resuelto. Calcula direccion UP/DOWN si hay precios."""
    direction = None
    if btc_price_open is not None and btc_price_close is not None:
        direction = "UP" if btc_price_close > btc_price_open else "DOWN"

    conn = get_connection()
    now = _now_ms()
    try:
        cur = conn.cursor()
        # INSERT con ON CONFLICT DO NOTHING (compatible PG y SQLite)
        if USE_POSTGRES:
            cur.execute(
                f"INSERT INTO resolved_markets "
                f"(market_id, asset_id_yes, asset_id_no, question, slug, "
                f" winning_outcome, winning_asset, btc_price_open, btc_price_close, "
                f" direction, ts_open, ts_resolved, ts_recv) "
                f"VALUES ({PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH}) "
                f"ON CONFLICT (market_id) DO NOTHING",
                (market_id, asset_id_yes, asset_id_no, question, slug,
                 winning_outcome, winning_asset, btc_price_open, btc_price_close,
                 direction, ts_open, ts_resolved or now, now)
            )
        else:
            cur.execute(
                f"INSERT OR IGNORE INTO resolved_markets "
                f"(market_id, asset_id_yes, asset_id_no, question, slug, "
                f" winning_outcome, winning_asset, btc_price_open, btc_price_close, "
                f" direction, ts_open, ts_resolved, ts_recv) "
                f"VALUES ({PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH})",
                (market_id, asset_id_yes, asset_id_no, question, slug,
                 winning_outcome, winning_asset, btc_price_open, btc_price_close,
                 direction, ts_open, ts_resolved or now, now)
            )

        # Marcar como resuelto en active_markets
        cur.execute(
            f"UPDATE active_markets SET status='resolved', ts_updated={PH} WHERE market_id={PH}",
            (now, market_id)
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Funciones de consulta
# ---------------------------------------------------------------------------

def get_latest_btc_price(source: str = "binance") -> Optional[dict]:
    """Devuelve el precio BTC mas reciente de la fuente indicada."""
    conn = get_connection()
    try:
        return _fetchone(
            conn.cursor(),
            f"SELECT * FROM btc_prices WHERE source={PH} ORDER BY ts DESC LIMIT 1",
            (source,)
        )
    finally:
        conn.close()


def get_active_markets() -> list[dict]:
    """Devuelve todos los mercados activos registrados."""
    conn = get_connection()
    try:
        return _fetchall(
            conn.cursor(),
            "SELECT * FROM active_markets WHERE status='active'"
        )
    finally:
        conn.close()


def get_unresolved_markets() -> list[dict]:
    """
    Devuelve mercados con status='active' cuyo intervalo de 5-min
    ya deberia haber terminado (slug: btc-updown-5m-<unix_ts>).
    """
    conn = get_connection()
    try:
        now_ms = _now_ms()
        rows = _fetchall(
            conn.cursor(),
            "SELECT * FROM active_markets WHERE status='active'"
        )
        results = []
        for r in rows:
            slug = r.get("slug") or ""
            parts = slug.split("-")
            if len(parts) >= 4 and parts[-1].isdigit():
                ts_start = int(parts[-1])
                ts_end_ms = (ts_start + 300) * 1000
                if now_ms > ts_end_ms + 60_000:
                    results.append(r)
        return results
    finally:
        conn.close()


def get_btc_price_at(ts_ms: int, source: str = "chainlink", tolerance_ms: int = 30_000) -> Optional[float]:
    """
    Devuelve el precio BTC mas cercano al timestamp dado.
    Busca en un rango de +-tolerance_ms milisegundos.
    """
    conn = get_connection()
    try:
        row = _fetchone(
            conn.cursor(),
            f"SELECT price FROM btc_prices "
            f"WHERE source={PH} AND ts BETWEEN {PH} AND {PH} "
            f"ORDER BY ABS(ts - {PH}) ASC "
            f"LIMIT 1",
            (source, ts_ms - tolerance_ms, ts_ms + tolerance_ms, ts_ms)
        )
        return row["price"] if row else None
    finally:
        conn.close()


def get_db_stats() -> dict:
    """Estadisticas rapidas de cuantos registros hay en cada tabla."""
    conn = get_connection()
    stats = {}
    tables = [
        "btc_prices", "orderbook_snapshots", "price_changes",
        "last_trades", "resolved_markets", "active_markets"
    ]
    try:
        cur = conn.cursor()
        for t in tables:
            row = _fetchone(cur, f"SELECT COUNT(*) as c FROM {t}")
            stats[t] = row["c"] if row else 0
    finally:
        conn.close()
    return stats
