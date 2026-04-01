"""
models/trainer.py
-----------------
Entrenamiento del modelo XGBoost para prediccion BTC Up/Down.

Flujo:
  1. Conecta a la DB de produccion (TRAINING_DATABASE_URL) para leer datos
  2. Llama a features.builder.build_training_dataset() para obtener (X, y)
  3. Split temporal 70/15/15 (train/val/test)
  4. Entrena XGBoost con grid search de hiperparametros
  5. Evalua en validation y test sets
  6. Guarda modelo + metricas + feature importance

Uso:
  python -m models.trainer
  o
  from models.trainer import train_model
  result = train_model()
"""

import os
import json
import joblib
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from loguru import logger

import xgboost as xgb
from sklearn.model_selection import ParameterGrid
from sklearn.metrics import (
    accuracy_score, precision_score, recall_score,
    f1_score, roc_auc_score, brier_score_loss,
    classification_report, confusion_matrix
)

from data import storage
from features.builder import build_training_dataset, ALL_FEATURE_COLS


# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------

MODELS_DIR = os.path.join(os.path.dirname(__file__))

# Hiperparametros a probar (grid search)
PARAM_GRID = {
    "max_depth":       [3, 5, 7],
    "learning_rate":   [0.01, 0.05, 0.1],
    "n_estimators":    [100, 200, 500],
    "subsample":       [0.8, 1.0],
    "colsample_bytree": [0.8, 1.0],
}

# Hiperparametros rapidos para dev/testing
PARAM_GRID_FAST = {
    "max_depth":       [3, 5],
    "learning_rate":   [0.05, 0.1],
    "n_estimators":    [100, 200],
    "subsample":       [0.8],
    "colsample_bytree": [0.8],
}


# ---------------------------------------------------------------------------
# Split temporal
# ---------------------------------------------------------------------------

def temporal_split(
    X: pd.DataFrame,
    y: pd.Series,
    train_ratio: float = 0.70,
    val_ratio: float = 0.15
) -> tuple:
    """
    Split temporal estricto: train -> val -> test en orden cronologico.
    NO usar shuffle/random — seria data leakage temporal.
    """
    n = len(X)
    train_end = int(n * train_ratio)
    val_end   = int(n * (train_ratio + val_ratio))

    X_train = X.iloc[:train_end]
    y_train = y.iloc[:train_end]

    X_val = X.iloc[train_end:val_end]
    y_val = y.iloc[train_end:val_end]

    X_test = X.iloc[val_end:]
    y_test = y.iloc[val_end:]

    logger.info(
        f"Split temporal: train={len(X_train)} | val={len(X_val)} | test={len(X_test)}"
    )

    return X_train, y_train, X_val, y_val, X_test, y_test


# ---------------------------------------------------------------------------
# Evaluacion
# ---------------------------------------------------------------------------

def evaluate_model(model, X: pd.DataFrame, y: pd.Series, label: str = "") -> dict:
    """
    Evalua un modelo y devuelve todas las metricas.
    """
    y_pred = model.predict(X)
    y_prob = model.predict_proba(X)[:, 1]

    metrics = {
        "accuracy":   accuracy_score(y, y_pred),
        "precision":  precision_score(y, y_pred, zero_division=0),
        "recall":     recall_score(y, y_pred, zero_division=0),
        "f1":         f1_score(y, y_pred, zero_division=0),
        "roc_auc":    roc_auc_score(y, y_prob) if len(y.unique()) > 1 else 0.5,
        "brier":      brier_score_loss(y, y_prob),
        "n_samples":  len(y),
        "up_pct":     float(y.mean()) * 100,
    }

    if label:
        logger.info(
            f"[{label}] acc={metrics['accuracy']:.4f} | "
            f"auc={metrics['roc_auc']:.4f} | "
            f"prec={metrics['precision']:.4f} | "
            f"f1={metrics['f1']:.4f} | "
            f"brier={metrics['brier']:.4f} | "
            f"n={metrics['n_samples']}"
        )

    return metrics


# ---------------------------------------------------------------------------
# Entrenamiento principal
# ---------------------------------------------------------------------------

def train_model(
    min_markets: int = 50,
    fast_mode: bool = False
) -> dict:
    """
    Pipeline completo de entrenamiento.

    Parametros:
      min_markets: minimo de mercados resueltos requeridos
      fast_mode:   True = grid search reducido (para testing)

    Retorna dict con:
      {model, metrics_val, metrics_test, feature_importance, model_path}
    """
    # 1. Construir dataset (conectando a la DB de produccion)
    logger.info("=" * 60)
    logger.info("  ENTRENAMIENTO DEL MODELO")
    logger.info("=" * 60)

    with storage.use_training_db():
        result = build_training_dataset(min_markets=min_markets)

    if result is None:
        logger.error("No hay suficientes datos para entrenar")
        return {"error": "insufficient_data"}

    X, y = result

    # 2. Split temporal
    X_train, y_train, X_val, y_val, X_test, y_test = temporal_split(X, y)

    if len(X_train) < 20 or len(X_val) < 5 or len(X_test) < 5:
        logger.error("Splits demasiado pequenos para entrenar de forma fiable")
        return {"error": "splits_too_small"}

    # 3. Grid search en validation set
    param_grid = PARAM_GRID_FAST if fast_mode else PARAM_GRID
    grid = list(ParameterGrid(param_grid))
    logger.info(f"Grid search: {len(grid)} combinaciones de hiperparametros")

    best_auc = -1
    best_model = None
    best_params = {}

    for i, params in enumerate(grid):
        model = xgb.XGBClassifier(
            **params,
            objective="binary:logistic",
            eval_metric="logloss",
            use_label_encoder=False,
            random_state=42,
            verbosity=0,
            early_stopping_rounds=20,
        )

        model.fit(
            X_train, y_train,
            eval_set=[(X_val, y_val)],
            verbose=False
        )

        # Evaluar en validation
        y_val_prob = model.predict_proba(X_val)[:, 1]
        val_auc = roc_auc_score(y_val, y_val_prob) if len(y_val.unique()) > 1 else 0.5

        if val_auc > best_auc:
            best_auc = val_auc
            best_model = model
            best_params = params

        if (i + 1) % 10 == 0 or i == len(grid) - 1:
            logger.debug(f"Grid search [{i+1}/{len(grid)}] best_auc={best_auc:.4f}")

    logger.success(f"Mejor modelo: AUC={best_auc:.4f} | params={best_params}")

    # 4. Evaluar en validation y test
    metrics_val  = evaluate_model(best_model, X_val, y_val, label="VALIDATION")
    metrics_test = evaluate_model(best_model, X_test, y_test, label="TEST")

    # 5. Feature importance
    importance = pd.Series(
        best_model.feature_importances_,
        index=ALL_FEATURE_COLS
    ).sort_values(ascending=False)

    logger.info("Top 10 features:")
    for feat, imp in importance.head(10).items():
        logger.info(f"  {feat}: {imp:.4f}")

    # 6. Guardar modelo y reporte
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    model_filename = f"xgb_btc5m_{timestamp}.pkl"
    model_path = os.path.join(MODELS_DIR, model_filename)

    joblib.dump(best_model, model_path)
    logger.success(f"Modelo guardado: {model_path}")

    # Guardar reporte
    report = {
        "timestamp":          timestamp,
        "n_train":            len(X_train),
        "n_val":              len(X_val),
        "n_test":             len(X_test),
        "best_params":        best_params,
        "metrics_validation": metrics_val,
        "metrics_test":       metrics_test,
        "feature_importance": importance.to_dict(),
        "model_filename":     model_filename,
        "feature_columns":    ALL_FEATURE_COLS,
        "target_threshold":   0.53,  # minimo para rentabilidad
    }

    report_path = os.path.join(MODELS_DIR, f"report_{timestamp}.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    logger.info(f"Reporte guardado: {report_path}")

    # 7. Veredicto
    test_acc = metrics_test["accuracy"]
    if test_acc >= 0.53:
        logger.success(
            f"MODELO VIABLE: accuracy test={test_acc:.4f} >= 0.53 (umbral de rentabilidad)"
        )
    else:
        logger.warning(
            f"MODELO AUN NO RENTABLE: accuracy test={test_acc:.4f} < 0.53. "
            "Necesita mas datos o mejores features."
        )

    return {
        "model":              best_model,
        "metrics_val":        metrics_val,
        "metrics_test":       metrics_test,
        "feature_importance": importance,
        "model_path":         model_path,
        "report":             report,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

    # Cargar .env para obtener TRAINING_DATABASE_URL
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
    except ImportError:
        pass

    # Recargar las URLs despues de dotenv
    storage.TRAINING_DATABASE_URL = os.environ.get("TRAINING_DATABASE_URL", "")
    storage.DATABASE_URL = os.environ.get("DATABASE_URL", "")

    fast = "--fast" in sys.argv
    result = train_model(min_markets=30, fast_mode=fast)

    if "error" in result:
        print(f"Error: {result['error']}")
    else:
        print(f"\nTest accuracy: {result['metrics_test']['accuracy']:.4f}")
        print(f"Test AUC:      {result['metrics_test']['roc_auc']:.4f}")
