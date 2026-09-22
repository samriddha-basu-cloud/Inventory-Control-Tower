# Forecasting Intelligence Tower (FIT) ⇄ ICT contract

ICT never forces its own forecasting engine. It consumes forecasts and returns the demand signals FIT needs.

**FIT → ICT**  `POST /api/forecast`

```json
{"source":"FIT","version":"FIT-2026.09","model":"ETS-ML-ensemble","forecast_type":"BASELINE","granularity":"W","issued_at":"2026-09-21",
 "records":[{"sku":"FMC-BEV-COLA-1L","location":"FMC-RDC-S","period_start":"2026-09-28","qty":9800,"p10":8100,"p90":11700}]}
```
* `source` ∈ FIT | ERP | UPLOAD | MANUAL · `forecast_type` ∈ BASELINE | CONSENSUS | ADJUSTED (default BASELINE) · weekly buckets (period_start is aligned to Monday).
* Unknown SKU/location or negative quantity → the record is rejected (response `207` with `errors[index]`), the rest is accepted. Re-sending the same key updates it.
* ICT uses **ADJUSTED > CONSENSUS > BASELINE** per period (`engine.forecast_precedence`). Forecast-vs-actual history is used for bias, WAPE and the forecast-error σ that sizes safety stock.
* Auth: `X-API-Key` (env `API_KEY`) or an authenticated session with the `ingest` permission.

**ICT → FIT**  `GET /api/forecast/signals?sku=&location=&weeks=26` returns weekly actuals, weeks likely censored by stock-outs, and constraints (MOQ, multiple, lead-time
static and P90) so FIT can treat constrained history correctly. `GET /api/forecast/contract` returns this contract as JSON.

**Chain in ICT**: forecast → demand distribution (μ, σ) → safety stock → reorder point → inventory position → replenishment → supply → service level. The
*Forecast → inventory chain* page (`/demand/chain`, and SKU 360) shows before/after values for any forecast change without saving anything.
