# Inventory methodology & formulas

Every figure in ICT is derived from formulas documented here. Nothing is a hidden constant: thresholds, weights, rates and
methods are configuration (Settings), resolved as **explicit setting > active industry profile > default**.

## 1. Canonical inventory state (no double counting)

Physical stock is partitioned into **mutually exclusive states** stored in `InventoryBalance.state`
(`UNRESTRICTED, QUARANTINED, BLOCKED, WIP, RETURNED, REPAIR, REFURBISHMENT, SCRAP, OBSOLETE`). Claims (`Allocation`) and
pipeline (open PO/TO/production) live in other tables, so one unit can never sit in two buckets. Which states count as
on-hand / usable / in-position is configurable (`InventoryStateDef`, Settings → Inventory states).

| Quantity | Formula |
|---|---|
| **On Hand** (physical) | Σ qty in states flagged *counts_on_hand* (default: all except WIP and SCRAP) |
| **Usable On Hand** | Σ qty in states flagged *allocatable* (default: UNRESTRICTED) |
| **Available** | Usable − Allocated − Committed − Reserved |
| **Net Available** | Available − Safety Stock (stock genuinely free beyond the buffer) |
| **Inventory Position** | Usable + In-Transit + On-Order − Allocated − Committed − Reserved − Backorders |
| **Projected Available** | time-phased, §3 |

*Allocated / Committed / Reserved* are claims **against** usable stock; on-hand is never reduced by them. Open transfers not
yet shipped are a reservation at the source and inbound at the destination. Past-due unfilled sales-order lines are
*backorders* (they reduce position and are not also counted as future order demand).

## 2. Demand, lead time and safety stock

* **Demand distribution** – mean = average of the effective forecast over the protection window (L + R) if a forecast exists,
  else history; σ = std-dev of forecast error (≥ 8 paired weeks) else std-dev of history (≥ 8 weeks) else an *assumed* CV of 0.5
  (flagged as low confidence). Weekly figures convert to daily by ÷7 (mean) and ÷√7 (σ).
* **Lead-time distribution** – from `LeadTimeObservation` (keyed supplier+item+destination → supplier+item → supplier, or lane for
  internal transfers). P50/P75/P90/P95, mean and σ are reported; a lognormal is fitted (KS-tested) when at least
  `engine.min_lt_obs` (8) observations exist. The **planning basis** is configurable: `static | observed_mean | p50 | p90`.
  With P90 the percentile already contains the variability, so σL is set to 0 (no double counting). Switching *Use observed lead
  times* off reverts to the master value. The observed values drive safety stock, ROP, ETA uncertainty in the risk model and
  replenishment dates - the UI always shows *"Static lead time: 14 days / Observed P90 lead time: 21 days"*.
* **Safety stock** – z = Φ⁻¹(service level); σ_LTD = √(L·σd² + d̄²·σL²).

| Method | Formula |
|---|---|
| Basic (max–avg) | d_max·L_max − d̄·L̄ |
| Demand variability | z·σd·√L |
| Lead-time variability | z·d̄·σL |
| Combined | z·√(L·σd² + d̄²·σL²) |
| Service level (fill rate, Type-2) | k·σ_LTD with G(k) = (1−β)·Q/σ_LTD (G = normal loss function) |
| Periodic review | z·√((L+R)·σd² + d̄²·σL²) |
| Continuous review (s,Q) | z·√(L·σd² + d̄²·σL²); s = d̄·L + SS |
| Empirical (bootstrap) | P_CSL(lead-time demand sample) − mean; used automatically for intermittent/lumpy demand (Syntetos-Boylan ADI ≥ 1.32) |

A policy may instead fix SS (`fixed_qty`) or cover (`days_cover`).

* **Reorder point** ROP = d̄·L + SS. **EOQ** = √(2·D·S / H) with H = unit cost × holding rate. **Practical order quantity** = EOQ rounded up to MOQ,
  then to the order multiple, then capped by a maximum (a cap below MOQ is reported as a violation, never silently ignored).
* Pull systems (Kanban / JIT / JIS) use a 1-day review period instead of the enterprise default.

## 3. Time-phased projection and stock-out risk

Per period: `Opening + Receipts − Order demand − Unallocated forecast − Production consumption − Transfers out = Ending`, where
*Unallocated forecast = max(0, forecast − orders in the same week)* (forecast consumption, so allocated orders are not counted twice).
Receipts are dated by ETA (or promised date / a P90-delayed ETA for the pessimistic view). Gap = Ending − Safety stock.
BOM explosion of open production orders creates component requirements at the plant. The projection uses **scheduled** supply only: it
does not assume future replenishment orders.

**Stock-out probability** over the planning lead time T: each inbound line arrives by T with probability Φ((T − ETA)/σ) (σ from the lead-time
distribution). The four most uncertain lines are enumerated as a mixture (arrives / does not arrive); demand in each scenario is
N(μ_D, σd²·T). P(stock-out) = Σ P(s)·Φ(−z_s); E[shortage] = Σ P(s)·σ·G(z_s). Risk level = configurable probability bands, raised one level when
the projected stock-out falls inside the lead time. Expected lost sales = E[shortage] × lost-sale share × price. Adding supply can never raise the
probability (property tested).

## 4. Excess, slow-moving, obsolete, at-risk

`Excess qty = max(0, usable − max(policy max level, d̄ × excess DOS threshold))`. Class precedence: OBSOLETE (lifecycle OBSOLETE, no demand for
`excess.obsolete_days`, or EOL passed without demand) > NON_MOVING > SLOW > EXCESS. *Obsolescence exposure* = excess value × class probability
(configurable). *At-risk (expiry)* = FEFO cumulative stock beyond d̄ × days-to-expiry.

## 5. Segmentation

ABC on *Annual Consumption Value = annual demand × unit cost* (cumulative cut-offs, default A ≤ 80 %, B ≤ 95 %; the item that crosses a boundary stays in the
higher class); XYZ on CV = σ/μ of weekly demand (X ≤ 0.5, Y ≤ 1.0); FSN by turns and recency of demand; HML by unit-value rank; VED/SDE/criticality from
master data. Schemes can be combined (e.g. ABC-XYZ-FSN).

## 6. KPIs (configurable formulas)

Formulas are safe expressions over engine *measures* (no `eval`): `inventory_turns = cogs_annual / avg_inventory_value`,
`dio = 365 × avg_inventory_value / cogs_annual`, `service_level = lines_in_full / lines_total`, `otif = lines_otif / lines_total`,
`fill_rate = units_shipped / units_ordered`, `forecast_bias = Σ(forecast−actual) / Σactual`, `supplier_otif`, `lt_variability = σ/mean`, …
Window, thresholds (Watch / Attention / Critical), direction, entity level and alert threshold are editable per KPI. COGS comes from customer-facing
item-locations only and excludes BOM components, so multi-echelon flows are not double counted.

## 7. ICT Inventory Health Index (0–100) — *ICT-defined, not an industry standard*

Weighted mean of nine components, each a linear 0–100 score between a worst and a best anchor: availability (usable ≥ SS), service level (80→99 %),
excess (30→0 %), obsolescence (10→0 %), accuracy (85→100 %), |forecast bias| (25→0 %), stock-out risk (25→0 %), aging (value older than 180 d, 40→0 %),
lead-time reliability (60→98 %). Components without data are excluded, never guessed. Weights: Settings → `weights.health`.

## 8. Planner Priority Score, supplier risk score, confidence

* **Planner Priority Score** (0–100) = Σ wᵢ·sᵢ / Σ wᵢ over financial (log-scaled value at risk), service, production, customer, severity and
  time-to-impact (100·e^(−days/14)). The breakdown is stored on every alert and displayed.
* **Supplier risk score** = Σ wᵢ·riskᵢ·100 over 1−OTIF, 1−lead-time reliability, quality rejection, single-source concentration, configured geo risk and expedite frequency.
* **Confidence** = Σ wᵢ·cᵢ over data completeness, freshness (source sync age vs expected), stability (demand and lead-time CV), model performance (forecast WAPE)
  and constraint completeness. **Data quality** = completeness + freshness. Every component and its reason is displayed.

## 9. Policy hierarchy

`GLOBAL < INDUSTRY < REGION < NODE < CATEGORY < SKU < SKU_LOCATION`. Parameters merge key-by-key from broad to specific; the resolution trail is shown in
Master Data → Policies. Applies to safety-stock, replenishment and generic control policies (allocation, transfer, expiry, approval …).

## 10. Carbon and freight

`CO2e (kg) = weight (t) × distance (km) × emission factor (kg/t-km)`. Factors and freight rates are configuration; **all outputs are estimates**, labelled as such.
An internal *carbon price* (default ₹4/kg, Settings) is a shadow price used only to rank options.
