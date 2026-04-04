# Mejoras Pendientes — Bot BTC Predictor

> Documento de mejoras identificadas pero no implementadas.
> Ultima actualizacion: 4 de abril de 2026

---

## 1. Platt Scaling — Calibracion de probabilidades del modelo

### Problema
El modelo V3 (20260404) tiene AUC excelente (0.977) pero Brier Score mediocre (0.082).
Esto significa que **clasifica bien** (acierta la direccion) pero **estima mal la probabilidad**
(dice 0.62 cuando la realidad es 0.88).

### Impacto
- El Kelly Criterion usa `prob_win` directamente para calcular cuanto apostar
- Probabilidades subestimadas = apuestas mas chicas de lo optimo = menos ganancias
- Probabilidades sobreestimadas = apuestas mas grandes = mas riesgo

### Solucion propuesta
Agregar Platt Scaling (regresion logistica sobre las probabilidades crudas) como capa
post-entrenamiento.

```python
from sklearn.calibration import CalibratedClassifierCV

# Opcion 1: Platt Scaling (sigmoide, 2 params — recomendado con <500 muestras val)
calibrated = CalibratedClassifierCV(model, method='sigmoid', cv='prefit')
calibrated.fit(X_val, y_val)

# Opcion 2: Isotonic Regression (no parametrico — mejor con >500 muestras val)
calibrated = CalibratedClassifierCV(model, method='isotonic', cv='prefit')
calibrated.fit(X_val, y_val)
```

### Archivos a modificar
- `models/trainer.py` — agregar calibracion despues del grid search, guardar modelo calibrado
- `models/predictor.py` — cargar modelo calibrado en vez del crudo

### Cuando implementar
Cuando tengamos >500 muestras en validation set (~3,500 mercados totales) para que
isotonic regression sea viable. Con los datos actuales (~155 val), Platt Scaling es suficiente.

### Metricas esperadas
- Brier Score deberia bajar de 0.082 a <0.05
- Kelly sizing seria mas optimo = mayor rentabilidad sin cambiar el modelo

---

## 2. Dry Run Orders — Validacion con ordenes reales minimas

### Problema
La Opcion A (Simulated Fill) que se implemento consulta el order book para simular
fills, pero no valida contra la infraestructura real del CLOB de Polymarket (latencia
de red, matching engine, rate limits, errores de API, etc).

### Solucion propuesta
Enviar ordenes REALES al CLOB de Polymarket con el minimo posible (5 shares, ~$2.50)
para cada decision del bot, y usar el resultado real (fill_price, fee, si se lleno o no)
para alimentar el paper wallet escalando al monto completo.

### Requisitos
- $50-100 USDC depositados en Polymarket
- Credenciales CLOB configuradas (POLY_PRIVATE_KEY, POLY_FUNDER_ADDRESS)

### Archivos a modificar
- `main.py` — agregar modo "dry_run" que envia ordenes minimas y escala el resultado
- `execution/order_manager.py` — parametro para forzar size minimo
- `execution/paper_wallet.py` — metodo para recibir OrderResult real y escalar

### Ventaja sobre Simulated Fill
- Captura latencia de red real, errores del matching engine, rate limits
- Valida que la infraestructura del bot funciona end-to-end
- El fill price es 100% real, no simulado

### Cuando implementar
Cuando se quiera validar el bot antes de pasar a modo live con capital completo.
Requiere tener USDC real en Polymarket.
