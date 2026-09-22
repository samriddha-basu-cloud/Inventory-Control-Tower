# Industry modes

Profiles (`IndustryProfile`) configure **terminology, KPIs, rules, dashboards, alerts, policy overrides and recommended actions**; the core engine is unchanged.
Explicit administrator settings always beat a profile. Activate under *Industry Mode*; each profile also has a domain view.

| Profile | Configures | Domain view |
|---|---|---|
| GENERAL | neutral defaults | – |
| AUTOMOTIVE | P90 lead-time basis, 30-day excess DOS, 99 % service; ECO + lead-time rules; KANBAN/JIT/JIS policies | ECO/BOM-revision analysis (Consume / Transfer / Block / Rework / Review – never auto-scrap), plant-shutdown risk for critical parts, line-side cover |
| PHARMA | expiry thresholds 120/45 d, 99 % service, released-only allocation, FEFO | batches, expiry status, quality status, cold-chain check, allocatable flag |
| RETAIL_FMCG | 45-day DOS, short shelf-life thresholds | omnichannel shelf availability (store / DC / dark store / MFC), promotion readiness, markdown exposure, returns |
| HIGH_TECH | high obsolescence probabilities, EOL rule | lifecycle, run-out vs EOL, obsolescence exposure, BOM alternates |
| MANUFACTURING | raw material / WIP / MRP | production-order material feasibility from BOM explosion |
| SPARE_PARTS | empirical safety stock, 365-day DOS | Syntetos-Boylan demand classes (ADI, CV²), criticality, service level |

Pharma allocation eligibility: only `RELEASED` quality, `UNRESTRICTED`, unexpired lots are pegged/allocated (policy `require_released`). The demo loads seven fictional datasets
(automotive, FMCG, pharma, retail, electronics, spare parts, manufacturing) that exercise these behaviours.
