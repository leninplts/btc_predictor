"""
rest_client.py
--------------
Cliente REST para la API publica de Polymarket (sin autenticacion).

Responsabilidades:
  1. Descubrir el mercado BTC 5-min activo en este momento
  2. Obtener historico de precios del share de un mercado
  3. Obtener el order book actual (snapshot REST)
  4. Obtener el midpoint y spread actuales
  5. Obtener la lista de mercados BTC 5-min recientes/resueltos

Endpoints usados:
  - Gamma API : https://gamma-api.polymarket.com  (market discovery)
  - CLOB API  : https://clob.polymarket.com       (precios, order book)
"""

import json
import time
import requests
from typing import Optional
from loguru import logger


# ---------------------------------------------------------------------------
# Configuracion base
# ---------------------------------------------------------------------------

GAMMA_BASE   = "https://gamma-api.polymarket.com"
CLOB_BASE    = "https://clob.polymarket.com"

# Texto clave que aparece en el slug/titulo de los mercados BTC 5-min
BTC_5M_SLUG_KEYWORD  = "btc-updown-5m"
BTC_15M_SLUG_KEYWORD = "btc-updown-15m"

SESSION = requests.Session()
SESSION.headers.update({"Accept": "application/json", "User-Agent": "btc-bot/1.0"})


# ---------------------------------------------------------------------------
# Utilidades internas
# ---------------------------------------------------------------------------

def _get(url: str, params: Optional[dict] = None, retries: int = 3) -> Optional[dict | list]:
    """GET con reintentos y logging. Devuelve None si falla."""
    for attempt in range(1, retries + 1):
        try:
            resp = SESSION.get(url, params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            logger.warning(f"HTTP {e.response.status_code} en {url} (intento {attempt}/{retries})")
        except requests.exceptions.RequestException as e:
            logger.warning(f"Error de red en {url} (intento {attempt}/{retries}): {e}")
        if attempt < retries:
            time.sleep(1.5 * attempt)
    logger.error(f"Fallo definitivo consultando {url}")
    return None


# ---------------------------------------------------------------------------
# Market Discovery (Gamma API)
# ---------------------------------------------------------------------------

def _parse_token_ids(raw) -> list[str]:
    """
    clobTokenIds puede venir como lista Python o como string JSON.
    Normaliza siempre a lista de strings.
    """
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return []


def get_active_btc_5m_market() -> Optional[dict]:
    """
    Busca el mercado BTC 5-min que este activo AHORA.

    Los mercados BTC 5-min siguen el patron de slug:
      btc-updown-5m-<unix_timestamp>
    donde el timestamp es el inicio del intervalo de 5 minutos (multiplo de 300).

    Estrategia:
      1. Calcular el slug esperado a partir del timestamp actual
      2. Probar el intervalo actual + siguiente (puede estar pre-creado)
      3. Fallback: buscar por titulo en Markets API
    """
    now_ts  = int(time.time())
    rounded = (now_ts // 300) * 300   # multiplo de 300 mas cercano hacia abajo

    # Probar los proximos candidatos: intervalo actual y el siguiente
    candidates = [rounded, rounded + 300, rounded - 300]

    for ts_candidate in candidates:
        slug = f"{BTC_5M_SLUG_KEYWORD}-{ts_candidate}"
        data = _get(f"{GAMMA_BASE}/events", params={"slug": slug})
        if not data:
            continue

        events = data if isinstance(data, list) else [data]
        for event in events:
            if not event.get("slug"):
                continue
            markets_in_event = event.get("markets", [])
            for m in markets_in_event:
                # Solo tomar mercados activos y no cerrados
                if not m.get("active", True) or m.get("closed", False):
                    continue
                token_ids = _parse_token_ids(m.get("clobTokenIds") or m.get("clob_token_ids"))
                if len(token_ids) < 2:
                    continue
                result = {
                    "market_id":    m.get("conditionId") or m.get("condition_id", ""),
                    "asset_id_yes": token_ids[0],
                    "asset_id_no":  token_ids[1],
                    "question":     m.get("question", ""),
                    "slug":         m.get("slug", slug),
                    "description":  m.get("description", event.get("description", "")),
                    "active":       True,
                }
                logger.info(f"Mercado BTC 5-min encontrado: {result['slug']}")
                return result

    # Fallback: buscar por titulo en Markets API
    logger.debug("Intentando fallback via Markets API...")
    data2 = _get(f"{GAMMA_BASE}/markets", params={"limit": 200, "closed": "false"})
    if data2:
        markets = data2 if isinstance(data2, list) else data2.get("markets", [])
        for m in markets:
            slug     = m.get("slug", "")
            question = m.get("question", "").lower()
            if (BTC_5M_SLUG_KEYWORD in slug or
                    ("bitcoin" in question and "up or down" in question)):
                token_ids = _parse_token_ids(m.get("clobTokenIds") or m.get("clob_token_ids"))
                if len(token_ids) < 2:
                    continue
                if m.get("closed", False):
                    continue
                result = {
                    "market_id":    m.get("conditionId") or m.get("condition_id", ""),
                    "asset_id_yes": token_ids[0],
                    "asset_id_no":  token_ids[1],
                    "question":     m.get("question", ""),
                    "slug":         slug,
                    "description":  m.get("description", ""),
                    "active":       m.get("active", True),
                }
                logger.info(f"Mercado BTC 5-min encontrado (fallback): {slug}")
                return result

    logger.warning("No se encontro mercado BTC 5-min activo en este momento")
    return None


def get_recent_btc_5m_markets(limit: int = 100) -> list[dict]:
    """
    Obtiene los ultimos N mercados BTC 5-min (activos + resueltos).
    Util para recolectar datos historicos de resoluciones.
    """
    params = {
        "tag_slug":  "crypto",
        "order":     "startDate",
        "ascending": "false",
        "limit":     limit,
    }
    data = _get(f"{GAMMA_BASE}/markets", params=params)
    if not data:
        return []

    markets = data if isinstance(data, list) else data.get("markets", [])
    results = []

    for m in markets:
        slug = m.get("slug", "")
        if BTC_5M_SLUG_KEYWORD not in slug:
            continue
        token_ids = _parse_token_ids(m.get("clobTokenIds") or m.get("clob_token_ids"))
        if len(token_ids) < 2:
            continue

        results.append({
            "market_id":        m.get("conditionId") or m.get("condition_id", ""),
            "asset_id_yes":     token_ids[0],
            "asset_id_no":      token_ids[1],
            "question":         m.get("question", ""),
            "slug":             slug,
            "active":           m.get("active", False),
            "closed":           m.get("closed", True),
            "winning_outcome":  m.get("outcomePrices"),  # raw, se procesa aparte
        })

    logger.info(f"Encontrados {len(results)} mercados BTC 5-min (activos + resueltos)")
    return results


def search_btc_markets(keyword: str = "Bitcoin Up or Down") -> list[dict]:
    """
    Busqueda por texto en la API de Gamma.
    Util como fallback si get_active_btc_5m_market() no encuentra nada.
    """
    params = {"q": keyword, "limit": 20}
    data = _get(f"{GAMMA_BASE}/markets", params=params)
    if not data:
        return []
    markets = data if isinstance(data, list) else data.get("markets", [])
    results = []
    for m in markets:
        token_ids = _parse_token_ids(m.get("clobTokenIds") or m.get("clob_token_ids"))
        if len(token_ids) < 2:
            continue
        results.append({
            "market_id":    m.get("conditionId") or m.get("condition_id", ""),
            "asset_id_yes": token_ids[0],
            "asset_id_no":  token_ids[1],
            "question":     m.get("question", ""),
            "slug":         m.get("slug", ""),
            "active":       m.get("active", False),
        })
    return results


# ---------------------------------------------------------------------------
# Precios historicos del share (CLOB API)
# ---------------------------------------------------------------------------

def get_share_price_history(
    asset_id: str,
    interval: str = "1d",
    fidelity: int = 1
) -> list[dict]:
    """
    Obtiene el historico de precios del share YES/NO en Polymarket.

    Parametros:
      asset_id  : token ID del share (YES o NO)
      interval  : 'max' | 'all' | '1m' | '1w' | '1d' | '6h' | '1h'
      fidelity  : granularidad en minutos (default 1)

    Devuelve lista de {t: unix_timestamp, p: precio_float}
    """
    params = {
        "market":   asset_id,
        "interval": interval,
        "fidelity": fidelity,
    }
    data = _get(f"{CLOB_BASE}/prices-history", params=params)
    if not data:
        return []

    history = data.get("history", [])
    logger.debug(f"Historico de precios: {len(history)} puntos para asset {asset_id[:12]}...")
    return history


# ---------------------------------------------------------------------------
# Order Book actual (CLOB API)
# ---------------------------------------------------------------------------

def get_order_book(asset_id: str) -> Optional[dict]:
    """
    Obtiene el order book REST actual para un asset_id.
    Devuelve {bids, asks, market, asset_id, last_trade_price, ...}
    """
    data = _get(f"{CLOB_BASE}/book", params={"token_id": asset_id})
    if not data:
        return None
    logger.debug(f"Order book obtenido: {len(data.get('bids',[]))} bids, "
                 f"{len(data.get('asks',[]))} asks")
    return data


def get_midpoint(asset_id: str) -> Optional[float]:
    """Obtiene el precio midpoint actual (promedio best_bid + best_ask)."""
    data = _get(f"{CLOB_BASE}/midpoint", params={"token_id": asset_id})
    if not data:
        return None
    try:
        return float(data.get("mid", 0))
    except (ValueError, TypeError):
        return None


def get_spread(asset_id: str) -> Optional[float]:
    """Obtiene el spread actual (best_ask - best_bid)."""
    data = _get(f"{CLOB_BASE}/spread", params={"token_id": asset_id})
    if not data:
        return None
    try:
        return float(data.get("spread", 0))
    except (ValueError, TypeError):
        return None


def get_last_trade_price(asset_id: str) -> Optional[float]:
    """Obtiene el ultimo precio de trade ejecutado."""
    data = _get(f"{CLOB_BASE}/last-trade-price", params={"token_id": asset_id})
    if not data:
        return None
    try:
        return float(data.get("price", 0))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Informacion de un mercado especifico (Gamma API)
# ---------------------------------------------------------------------------

def get_market_by_condition_id(condition_id: str) -> Optional[dict]:
    """Obtiene la informacion completa de un mercado por su condition_id."""
    data = _get(f"{GAMMA_BASE}/markets/{condition_id}")
    return data if data else None


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def ping_clob() -> bool:
    """Verifica que el CLOB API este respondiendo."""
    try:
        resp = SESSION.get(f"{CLOB_BASE}/time", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def ping_gamma() -> bool:
    """Verifica que la Gamma API este respondiendo."""
    try:
        resp = SESSION.get(f"{GAMMA_BASE}/markets", params={"limit": 1}, timeout=5)
        return resp.status_code == 200
    except Exception:
        return False
