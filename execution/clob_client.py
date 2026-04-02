"""
execution/clob_client.py
------------------------
Wrapper del SDK py_clob_client para interactuar con Polymarket CLOB.

Responsabilidades:
  - Inicializar el cliente autenticado (MetaMask browser proxy, signature_type=2)
  - Derivar API credentials automaticamente
  - Verificar y aprobar token allowances (USDC + Conditional Tokens)
  - Consultar balance real de USDC y tokens condicionales
  - Exponer el cliente listo para que order_manager lo use

Nota sobre signature_type:
  Polymarket crea un proxy wallet cuando conectas MetaMask desde el navegador.
  Tu MetaMask firma las transacciones pero los fondos viven en el proxy.
  Por eso usamos signature_type=2 (browser wallet proxy), no 0 (EOA directo).
  El balance viene en unidades atomicas de USDC (6 decimales): dividir por 1e6.

Variables de entorno requeridas (en .env o Dokploy):
  POLY_PRIVATE_KEY      : private key de MetaMask (con o sin 0x)
  POLY_FUNDER_ADDRESS   : direccion publica de MetaMask (0x...)
"""

import os
from typing import Optional
from loguru import logger

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType


# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet


# ---------------------------------------------------------------------------
# Cliente singleton
# ---------------------------------------------------------------------------

class PolymarketClient:
    """
    Wrapper alrededor de ClobClient que maneja autenticacion,
    derivacion de API credentials y verificacion de allowances.

    Uso:
        client = PolymarketClient()
        if client.initialize():
            clob = client.clob  # ClobClient listo para usar
    """

    def __init__(self):
        self.clob: Optional[ClobClient] = None
        self.address: str = ""
        self.initialized: bool = False
        self._api_creds: Optional[ApiCreds] = None

    def initialize(self) -> bool:
        """
        Inicializa el cliente autenticado.
        Lee POLY_PRIVATE_KEY y POLY_FUNDER_ADDRESS del entorno.
        Retorna True si se inicializo correctamente.
        """
        private_key = os.environ.get("POLY_PRIVATE_KEY", "").strip()
        funder = os.environ.get("POLY_FUNDER_ADDRESS", "").strip()

        if not private_key:
            logger.warning(
                "POLY_PRIVATE_KEY no definida — modo live no disponible. "
                "Exportala desde MetaMask: Settings > Security > Reveal Private Key"
            )
            return False

        if not private_key.startswith("0x"):
            private_key = "0x" + private_key

        if not funder:
            logger.warning(
                "POLY_FUNDER_ADDRESS no definida — es tu direccion publica de MetaMask (0x...). "
                "Copiala desde MetaMask haciendo click en tu direccion."
            )
            return False

        try:
            # signature_type=2 = browser wallet proxy (MetaMask via Polymarket web)
            # Polymarket crea un proxy contract cuando conectas MetaMask desde el sitio.
            # Los fondos depositados viven en ese proxy, no en tu EOA directamente.
            self.clob = ClobClient(
                host=CLOB_HOST,
                key=private_key,
                chain_id=CHAIN_ID,
                signature_type=2,
                funder=funder,
            )
            self.address = funder

            # Derivar API credentials automaticamente
            self._api_creds = self.clob.create_or_derive_api_creds()
            self.clob.set_api_creds(self._api_creds)

            logger.success(
                f"Polymarket CLOB client inicializado | "
                f"address={funder[:10]}...{funder[-6:]}"
            )

            self.initialized = True
            return True

        except Exception as e:
            logger.error(f"Error inicializando Polymarket client: {e}")
            self.initialized = False
            return False

    # -----------------------------------------------------------------------
    # Allowances
    # -----------------------------------------------------------------------

    def check_allowances(self) -> dict:
        """
        Verifica el estado de allowances de USDC y Conditional Tokens.
        Retorna dict con balance y allowance info.
        """
        if not self.initialized:
            return {"error": "client not initialized"}

        try:
            collateral = self.clob.get_balance_allowance(
                params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            # allowances viene como dict de contratos -> valor
            allowances = collateral.get("allowances", {})
            max_allowance = max(
                (float(v) for v in allowances.values()), default=0.0
            )
            return {
                "usdc_balance": collateral.get("balance", "0"),
                "usdc_allowance": str(max_allowance),
                "allowances_detail": allowances,
            }
        except Exception as e:
            logger.error(f"Error verificando allowances: {e}")
            return {"error": str(e)}

    def ensure_allowances(self) -> bool:
        """
        Verifica que los token allowances esten aprobados.
        Si no lo estan, intenta aprobarlos via update_balance_allowance.
        Retorna True si la verificacion se completo (no significa que tenga fondos).
        """
        if not self.initialized:
            return False

        try:
            info = self.check_allowances()
            if "error" in info:
                logger.warning(f"No se pudieron verificar allowances: {info['error']}")
                return False

            usdc_allowance = float(info.get("usdc_allowance", "0"))

            if usdc_allowance <= 0:
                logger.info("Allowances en 0 — se actualizaran automaticamente al primer trade")
                # update_balance_allowance puede requerir fondos en la wallet
                # Si no hay fondos, simplemente loguear y continuar
                try:
                    self.clob.update_balance_allowance(
                        params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                    )
                    logger.success("USDC allowance actualizado")
                except Exception as e:
                    logger.info(f"Allowance update pendiente (normal si no hay fondos): {e}")

            logger.info(
                f"Balance USDC: {info.get('usdc_balance', '?')} | "
                f"Allowance max: {info.get('usdc_allowance', '?')}"
            )
            return True

        except Exception as e:
            logger.error(f"Error configurando allowances: {e}")
            return False

    # -----------------------------------------------------------------------
    # Balance
    # -----------------------------------------------------------------------

    def get_usdc_balance(self) -> float:
        """Devuelve el balance de USDC disponible en Polymarket (en dolares)."""
        if not self.initialized:
            return 0.0

        try:
            result = self.clob.get_balance_allowance(
                params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            balance_raw = float(result.get("balance", "0"))
            # El balance SIEMPRE viene en unidades atomicas de USDC (6 decimales)
            # Ej: 50000004 = $50.000004 USDC
            return balance_raw / 1e6
        except Exception as e:
            logger.error(f"Error obteniendo balance USDC: {e}")
            return 0.0

    def get_token_balance(self, token_id: str) -> float:
        """Devuelve el balance de un token condicional (YES/NO shares)."""
        if not self.initialized:
            return 0.0

        try:
            result = self.clob.get_balance_allowance(
                params=BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=token_id,
                )
            )
            balance_raw = float(result.get("balance", "0"))
            return balance_raw / 1e6
        except Exception as e:
            logger.error(f"Error obteniendo balance token: {e}")
            return 0.0

    def is_ready(self) -> bool:
        """True si el client esta inicializado y listo para operar."""
        return self.initialized and self.clob is not None
