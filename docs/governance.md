# Governance, safety and audit

* **Audit trail** (`AuditLog`, append-only): data loads (with file name and counts), calculations (`CALC` entries hold inputs, policy, formula, model, result), configuration
  changes (old/new values), actions, approvals, executions, scenario runs. Browse and filter at `/audit`; export via `/api/audit`.
* **Model & rule governance** (`/governance`): registry of every model with formula and configuration source, *model eligibility* on your data
  (RECOMMENDED / POSSIBLE / NOT RECOMMENDED with evidence and fallback), detection rules (versioned on edit), KPI definitions, configuration change log, LEARN metrics.
* **Digital-twin governance**: every scenario stores ID, creator, base dataset, assumptions, changes, results and timestamp. Simulations copy the snapshot; a checksum of
  production balances is verified around sandbox creation.
* **Autonomy** (levels 0–4) is configuration; NEVER_AUTO rules win over ALLOW_AUTO; approval roles follow a cost ladder; segregation of duties is optional. Default level is 1 (Recommend).
  Execution modes: RECOMMENDATION, SIMULATION_ONLY (default), LIVE (refused without a real connector). Mock executions are always labelled MOCK.
* **Security**: CSRF on every state-changing request (API key alternative for machines), RBAC with configurable permissions, secure uploads (extension whitelist, size cap, magic-byte
  sniffing, macro rejection, random storage names), spreadsheet formula-injection neutralisation on exports, Jinja auto-escaping, secrets only from environment variables,
  no pickle / no `eval` (rule conditions and KPI formulas are interpreted safely), no stack traces to users, optional sign-in (`AUTH_REQUIRED=1`).
