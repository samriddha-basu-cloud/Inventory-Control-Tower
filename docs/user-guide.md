# User Guide

## 1. Launch the demo

Click **Launch Demo** in the top bar from any page. This loads a synthetic
FMCG network (4 suppliers, plant, central DC, 3 regional DCs, 4 stores, 5
customers, 24 SKUs, 90 days of demand history) with an injected
supplier-delay disruption.

## 2. The Control Tower dashboard

Top-level KPIs (inventory value, available units, excess/obsolete value,
turns/DIO), the FIT Health Index, open incidents, top alerts by financial
impact, and pending recommendations. Everything here links deeper - click an
incident to investigate it, click "Review & Approve" to act on
recommendations.

## 3. Walk the disruption story

1. **Exceptions → Exception Workbench**, click "Regenerate Alerts" (also
   auto-run once by Launch Demo). You'll see `stockout_risk`,
   `safety_stock_breach`, `excess`, `expiry`, and `late_po` alerts.
2. **Exceptions → Incidents**: the late POs from the injected disruption
   supplier are clustered into one incident instead of dozens of separate
   SKU alerts, with an observed root cause and financial impact.
3. **Optimization → Rebalancing**: run it. Because the demo deliberately
   seeds DC-NORTH with surplus and DC-SOUTH with a deficit for the
   disrupted SKUs, you should see transfer recommendations between them.
4. **Exceptions → Recommendations & Approvals**: approve a transfer
   recommendation (shows why, expected impact, cost, confidence), then
   execute it (simulated - no live WMS/TMS connector configured).

## 4. Explore the analytics

- **Inventory → ABC × XYZ Matrix**: network-wide segmentation with
  per-segment policy suggestions.
- **Inventory → Aging & Expiry**: aging buckets + near-expiry/expired
  batches (FEFO-relevant).
- **Network → Network Map / Heatmap**: Plotly-based visualizations, hover
  for detail, switch heatmap metric (days of supply / value / stockout
  risk).
- **Optimization → MEIO**: compare single- vs multi-echelon safety stock -
  read the per-SKU verdict, which can go either way (see
  `docs/optimization.md`).
- **Scenarios**: run a demand/lead-time/safety-stock what-if; it never
  touches live data.

## 5. Reports & API

**Reports → Full Excel Export** downloads a multi-sheet workbook. The
`/api/...` endpoints (see `docs/api.md`) give you the same data as JSON for
scripting or integration testing.
