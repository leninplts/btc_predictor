"""
models/predictor.py
-------------------
Modulo de inferencia en tiempo real.

Carga un modelo XGBoost entrenado y genera predicciones
a partir de un vector de features.

Uso:
  from models.predictor import Predictor
  pred = Predictor("models/xgb_btc5m_20260401.pkl")
  result = pred.predict(features_df)
"""

import os
import glob
import joblib
import pandas as pd
import numpy as np
from loguru import logger
from typing import Optional

from features.builder import ALL_FEATURE_COLS


class Predictor:
    """
    Wrapper del modelo para inferencia en tiempo real.

    Carga el modelo al iniciar y expone un metodo predict()
    que devuelve probabilidad, direccion y si debe operar.
    """

    def __init__(self, model_path: Optional[str] = None, min_confidence: float = 0.55):
        """
        Parametros:
          model_path     : ruta al archivo .pkl del modelo.
                           Si None, carga el mas reciente en models/
          min_confidence : confianza minima para recomendar operar
        """
        self.min_confidence = min_confidence
        self.model = None
        self.model_path = model_path

        if model_path is None:
            model_path = self._find_latest_model()

        if model_path and os.path.exists(model_path):
            self.model = joblib.load(model_path)
            self.model_path = model_path
            logger.info(f"Modelo cargado: {model_path}")
        else:
            logger.warning("No se encontro modelo entrenado. predict() devolvera neutral.")

    def _find_latest_model(self) -> Optional[str]:
        """Busca el modelo .pkl mas reciente en models/."""
        models_dir = os.path.dirname(__file__)
        pattern = os.path.join(models_dir, "xgb_btc5m_*.pkl")
        files = glob.glob(pattern)
        if not files:
            return None
        # Ordenar por nombre (que incluye timestamp) y tomar el ultimo
        return sorted(files)[-1]

    def predict(self, features: pd.DataFrame) -> dict:
        """
        Genera prediccion a partir de un vector de features.

        Parametros:
          features : DataFrame de 1 fila con ALL_FEATURE_COLS

        Retorna:
          {
            "prob_up":       float (0.0 a 1.0),
            "prob_down":     float (0.0 a 1.0),
            "direction":     "UP" | "DOWN",
            "confidence":    float (0.5 a 1.0),
            "should_trade":  bool,
            "model_loaded":  bool,
          }
        """
        if self.model is None:
            return {
                "prob_up":       0.5,
                "prob_down":     0.5,
                "direction":     "NEUTRAL",
                "confidence":    0.5,
                "should_trade":  False,
                "model_loaded":  False,
            }

        # Asegurar columnas correctas en el orden correcto
        X = features.reindex(columns=ALL_FEATURE_COLS, fill_value=0.0)

        # Reemplazar NaN/inf
        X = X.fillna(0.0).replace([np.inf, -np.inf], 0.0)

        # Predecir
        prob = self.model.predict_proba(X)[0]
        prob_down = float(prob[0])  # clase 0 = No/DOWN
        prob_up   = float(prob[1])  # clase 1 = Yes/UP

        direction = "UP" if prob_up >= prob_down else "DOWN"
        confidence = max(prob_up, prob_down)
        should_trade = confidence >= self.min_confidence

        return {
            "prob_up":       prob_up,
            "prob_down":     prob_down,
            "direction":     direction,
            "confidence":    confidence,
            "should_trade":  should_trade,
            "model_loaded":  True,
        }

    def is_loaded(self) -> bool:
        """Verifica si hay un modelo cargado."""
        return self.model is not None
