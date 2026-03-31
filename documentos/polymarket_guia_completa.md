# Polymarket - Guia Completa
> Informacion actualizada: 30 de marzo de 2026  
> Fuente: polymarket.com, help.polymarket.com, docs.polymarket.com

---

## 1. QUE ES POLYMARKET

Polymarket es el **mercado de prediccion mas grande del mundo** (se autodenomina "The World's Largest Prediction Market"). Es una plataforma descentralizada basada en blockchain donde los usuarios compran y venden participaciones (shares) sobre el resultado de eventos futuros reales.

### Caracteristicas clave:
- Opera sobre la blockchain de **Polygon** (anteriormente) usando contratos inteligentes
- Moneda de operacion: **USDC** (stablecoin)
- No hay una "casa" que se lleve el dinero. El contraparte de cada operacion es otro usuario
- Los precios reflejan probabilidades reales determinadas por la oferta y demanda
- Los mercados son resueltos por el **UMA Optimistic Oracle** (oraculo basado en contratos inteligentes)

### Como funciona el mecanismo basico:
- Cada mercado tiene dos resultados posibles: **YES** y **NO**
- Las participaciones siempre valen entre **$0.01 y $1.00 USDC**
- Cada par YES + NO esta completamente colateralizado por **$1.00 USDC**
- Si compras shares YES a $0.18 y el evento ocurre, cada share vale **$1.00** (ganancia de $0.82)
- Si el evento NO ocurre, los shares YES valen **$0.00** (perdida total)
- Se puede vender antes de la resolucion para tomar ganancias o cortar perdidas

### Precio = Probabilidad:
Un share de YES a $0.18 = el mercado le asigna un **18% de probabilidad** a que ese evento ocurra.

---

## 2. CATEGORIAS Y TIPOS DE MERCADOS

Polymarket cubre una amplia variedad de categorias donde se pueden abrir posiciones:

### Politica
- Elecciones presidenciales y legislativas (EE.UU., Europa, Latinoamerica)
- Decisiones de gobierno (Trump, Biden, etc.)
- Paralisis de gobierno (shutdowns)
- Aprobacion de leyes y politicas
- Primarias y candidaturas

### Geopolitica y Conflictos
- Guerras y conflictos armados (Ucrania, Oriente Medio, Iran)
- Acuerdos de paz y ceses al fuego
- Relaciones internacionales (sanciones, acuerdos diplomaticos)
- Control de territorios estrategicos

### Crypto y Finanzas
- Precios de Bitcoin, Ethereum y otras criptomonedas
- **Mercados de 5 minutos y 15 minutos** de BTC (Up/Down)
- IPOs y lanzamientos de tokens
- Valoraciones de proyectos (FDV)
- Indices y commodities (Petroleo crudo, etc.)
- Decisiones de la Reserva Federal (Fed)

### Deportes
- Resultados de partidos de NBA, NFL, MLB, NCAA, IPL
- Ganadores de torneos (NCAA Tournament, etc.)
- Spreads y totales de puntos
- Esports: LoL, CS2 y otros

### Tecnologia e IA
- Lanzamientos de productos tech
- Hitos de inteligencia artificial
- SpaceX y conquista espacial
- IPOs tecnologicas

### Cultura Popular
- Entregas de premios (Oscars, Grammys)
- Peliculas y series
- Eventos virales

### Clima
- Temperatura diaria en ciudades especificas
- Fenomenos meteorologicos

### Tweet Markets
- Predicciones sobre contenido y actividad en redes sociales

---

## 3. ESTRUCTURA DE MERCADOS

### Mercado Simple (Single-Market Event):
```
Evento: "Will Bitcoin reach $150,000 by December 2026?"
└── YES token: vale $1 si BTC llega a $150k
└── NO token: vale $1 si BTC NO llega a $150k
```

### Mercado Multiple (Multi-Market Event):
```
Evento: "Who will win the 2026 NCAA Tournament?"
├── Michigan?  (Yes/No) - 36%
├── Arizona?   (Yes/No) - 35%
├── Illinois?  (Yes/No) - 17%
└── Connecticut? (Yes/No) - 14%
```

### Mercados en Vivo:
- Deportes en tiempo real
- Crypto de 5 y 15 minutos (BTC Up/Down)
- Eventos de noticias urgentes (Breaking)

---

## 4. COMO OPERAR

### Tipos de ordenes:
1. **Market Order**: Compra/venta inmediata al precio actual
2. **Limit Order**: Orden pendiente que se ejecuta cuando el precio llega a tu nivel deseado
   - Se pueden cancelar en cualquier momento
   - Pueden ejecutarse parcialmente
   - En deportes: se cancelan automaticamente al inicio del partido

### Proceso de operacion:
1. Depositar **USDC** en la cuenta (Polymarket no cobra fee por deposito)
2. Elegir un mercado y un outcome (YES o NO)
3. Definir precio y cantidad
4. La orden se ejecuta contra otro usuario (peer-to-peer)
5. Al resolverse el mercado: shares ganadores = $1.00 cada uno, shares perdedores = $0.00

### Vender antes de la resolucion:
- Es posible vender shares en cualquier momento
- El precio fluctua segun la probabilidad percibida del mercado
- Permite tomar ganancias anticipadas o limitar perdidas

---

## 5. COMISIONES (FEES) - INFORMACION ACTUALIZADA

> **Fuente directa: help.polymarket.com/en/articles/13364478-trading-fees**
> **Actualizado: esta semana (marzo 2026)**

### Mercados SIN comision (gratuitos):
- **Eventos Geopoliticos y del Mundo**: 100% sin comisiones, Polymarket no cobra ni se beneficia del trading en estos mercados
- **Depositos y retiros**: Sin comision por parte de Polymarket (terceros como Coinbase o MoonPay pueden tener sus propias tarifas de red)

### Mercados CON comision:
Actualmente aplican comisiones en:
- **Crypto** (mercados de precio de criptomonedas)
- **Deportes**

**A partir del 30 de marzo de 2026**, se ampliara a:
- Finance
- Politics (politica)
- Economics (economia)
- Culture (cultura)
- Weather (clima)
- Tech (tecnologia)

### Estructura de la comision:

La comision NO es fija, **varia segun el precio del share**:
- Es **maxima cuando el precio es 0.50 ($0.50)** = 50% de probabilidad
- Se reduce hacia los extremos (cerca de $0.01 o $0.99 la comision es casi cero)
- **Comision maxima efectiva: 1.80%** cuando el share esta a $0.50

### Tabla de comisiones (por 100 shares):

| Precio del Share | Valor del Trade | Fee (USDC) | Tasa Efectiva |
|-----------------|-----------------|------------|----------------|
| $0.01           | $1              | $0.00      | 0.00%          |
| $0.05           | $5              | $0.003     | 0.06%          |
| $0.10           | $10             | $0.02      | 0.20%          |
| $0.15           | $15             | $0.06      | 0.41%          |
| $0.20           | $20             | $0.13      | 0.64%          |
| $0.25           | $25             | $0.22      | 0.88%          |
| $0.30           | $30             | $0.33      | 1.10%          |
| $0.35           | $35             | $0.45      | 1.29%          |
| $0.40           | $40             | $0.58      | 1.44%          |
| $0.45           | $45             | $0.69      | 1.53%          |
| **$0.50**       | **$50**         | **$0.78**  | **1.56%**      |
| $0.55           | $55             | $0.84      | 1.53%          |
| $0.60           | $60             | $0.86      | 1.44%          |
| $0.65           | $65             | $0.84      | 1.29%          |
| $0.70           | $70             | $0.77      | 1.10%          |
| $0.75           | $75             | $0.66      | 0.88%          |
| $0.80           | $80             | $0.51      | 0.64%          |
| $0.85           | $85             | $0.35      | 0.41%          |
| $0.90           | $90             | $0.18      | 0.20%          |
| $0.95           | $95             | $0.05      | 0.06%          |
| $0.99           | $99             | $0.00      | 0.00%          |

> Nota: Los fees se redondean a 4 decimales. La comision minima es $0.0001 USDC. Trades muy pequeños cerca de los extremos pueden resultar en fee cero.

### Nota importante sobre la tabla (discrepancia encontrada):
- El articulo de "Trading Fees" indica comision maxima de **1.80%**
- El articulo de "Maker Rebates" (con tabla detallada) indica **1.56%** como maximo en $0.50
- La tabla detallada (con numeros exactos) muestra **1.56%** a $0.50
- La tasa real a usar como referencia: **~1.56% maximo en precio 0.50**

---

## 6. PROGRAMA DE MAKER REBATES

### Que es:
Market makers que proveen liquidez activa (ordenes que se ejecutan) ganan rebates diarios en USDC.

### Mercados elegibles (actualmente):
Solo **mercados de crypto de 15 minutos** tienen taker fees habilitadas.

### Como funciona:
- Los rebates son proporcionales a tu share de liquidez ejecutada
- Se pagan **diariamente en USDC**
- Son financiados por los taker fees cobrados en esos mercados

### Historial de rebates:

| Periodo | % Maker Rebate |
|---------|---------------|
| Ene 9 - Ene 11, 2026 | 100% |
| Ene 12 - Ene 18, 2026 | 20% |

---

## 7. OTROS INCENTIVOS

### Holding Rewards:
Recompensas por mantener posiciones abiertas (liquidity holding).

### Liquidity Rewards:
Recompensas para proveedores de liquidez al order book.

### Referral Program:
Programa de referidos para traer nuevos usuarios.

### Sponsor Market Rewards:
Posibilidad de patrocinar mercados y ganar recompensas.

---

## 8. RESOLUCION DE MERCADOS

### Como se resuelven:
1. Cuando el resultado es claro, el mercado puede ser "propuesto" para resolucion
2. El proponente debe depositar un **bono de $750 USDC** (que pierde si la propuesta falla)
3. El **UMA Optimistic Oracle** verifica la transaccion
4. Periodo de disputa: **2 horas** tras la propuesta
5. Si se aprueba: el proponente recupera el bono + recompensa
6. Al resolverse: shares ganadores = $1.00, shares perdedores = $0.00

### Quien puede resolver:
Cualquier usuario puede proponer una resolucion, pero se recomienda solo hacerlo si se esta seguro del resultado para no perder el bono.

---

## 9. RESTRICCIONES GEOGRAFICAS

Polymarket esta **BLOQUEADO** en los siguientes paises (33 en total):

Australia, Belgica, Bielorrusia, Burundi, Rep. Centroafricana, Congo, Cuba, **Alemania**, Etiopia, **Francia**, **Reino Unido**, **Iran**, Iraq, **Italia**, Corea del Norte, Libano, Libia, Myanmar, Nicaragua, **Polonia**, **Rusia**, **Singapur**, Somalia, Sudan del Sur, Sudan, Siria, **Tailandia**, **Taiwan**, **Estados Unidos**, Venezuela, Yemen, Zimbawe.

### Modo "Solo Cierre" (Close-Only):
Singapur, Polonia, Tailandia, Taiwan - pueden cerrar posiciones pero NO abrir nuevas.

### Latinoamerica:
La mayoria de paises latinoamericanos (incluyendo Argentina, Mexico, Colombia, Brasil, etc.) **NO estan en la lista de bloqueados**, por lo tanto pueden operar con normalidad.

> IMPORTANTE: Polymarket prohibe estrictamente el uso de VPNs para evadir restricciones geograficas (violacion de Terminos de Servicio, Seccion 2.1.4).

---

## 10. INFRAESTRUCTURA TECNICA

- **Blockchain**: Polygon (L2 de Ethereum)
- **Tokens de outcome**: ERC1155 (uno para YES, uno para NO)
- **Order Book**: CLOB (Central Limit Order Book)
- **Oracle de resolucion**: UMA Optimistic Oracle
- **API**: REST + WebSocket disponible para desarrolladores
- **SDKs**: Python, TypeScript, Rust
- **Moneda base**: USDC

---

## 11. VENTAJAS vs CASAS DE APUESTAS TRADICIONALES

| Caracteristica | Polymarket | Casa de apuestas tradicional |
|---|---|---|
| Contraparte | Otros usuarios (P2P) | La casa |
| Puede banearte por ganar | NO | SI |
| Vender antes del evento | SI | NO (en mayoria) |
| Transparencia de precios | SI (mercado libre) | NO (la casa fija odds) |
| Custodio de fondos | Smart contracts | La empresa |
| Regulacion | Descentralizado | Regulado |

---

*Documento creado para referencia interna del bot de trading.*
*Fuentes: polymarket.com, help.polymarket.com, docs.polymarket.com*
*Fecha: 30 de marzo de 2026*
