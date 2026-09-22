# Known limitations (read before relying on results)

* **No live integrations.** SAP, Oracle, Dynamics, NetSuite, WMS, TMS, MES, 3PL, IoT and telematics adapters are declared as *NOT CONFIGURED*; only file/REST ingestion, mock
  adapters and an EDI *parser* exist. `LIVE` execution is refused. Executing an action never posts to an ERP.
* **Demo data is synthetic** (fictional names, generated history). Weekly demand history and trend snapshots are generated; KPI trend history before today is flagged `DEMO`.
* **Statistical assumptions**: normal demand within a protection interval, independent demand and lead time, lognormal lead-time fit; intermittent demand falls back to a bootstrap.
  Stock-out probability is an approximation (mixture over the four most uncertain arrivals), not a full simulation – use the Scenario Lab for that.
* **Projection uses scheduled supply only** (no future replenishment), so continuously replenished nodes trend negative in the chart; risk uses the lead-time window instead.
* **Simulation** is a network-level Monte-Carlo with simplified replenishment ((s,S) using the pair's ROP/EOQ); it does not model detailed production scheduling, transport capacity
  networks or substitutions beyond the configured effects. Results are directional decision support.
* **Optimisation** solves per-SKU order quantities under linear cost proxies (holding, expected stock-out cost, priced carbon…); truck-count and expedite recourse are simplified.
  Transfer planning is a transportation LP per item (no multi-item lane capacity sharing).
* **Carbon** figures are estimates from configured factors; no certified footprinting. Distances are great-circle × circuity factor.
* **Currency** is a display setting (INR default, lakh/crore compaction); no FX conversion.
* **Alerts at scale**: the demo yields ~190 open alerts across seven industries (grouped into ~13 incidents); tune materiality thresholds and suppression rules to your data.
* Only the first 105 sections of the specification were visible to the builder (the prompt was truncated); features requested beyond that point may be missing.
