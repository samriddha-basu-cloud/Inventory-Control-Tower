"""Sidebar navigation (26 top-level entries requested in the spec, plus Optimization). Each item: (label, endpoint, icon)."""
NAV = [
    {"group": "Command", "items": [("Control Tower", "main.home", "◎"), ("Inventory", "inventory.explorer", "▤"), ("Network", "network.view", "⌘")]},
    {"group": "Data", "items": [("Data Hub", "datahub.home", "⇪"), ("Master Data", "master.home", "▦"), ("Demand & Forecast", "demand.home", "∿")]},
    {"group": "Plan", "items": [("Replenishment", "replenishment.home", "⟳"), ("Safety Stock", "replenishment.safety_stock", "◔"), ("Optimization", "optimization.home", "≋"),
                                ("Order Pegging", "risk.pegging", "⛓"), ("Supply Visibility", "risk.supply", "⛴")]},
    {"group": "Risk", "items": [("Risk Center", "risk.center", "⚑"), ("Alerts", "alerts.list_alerts", "▲"), ("Root Cause", "alerts.incidents", "⌖")]},
    {"group": "Simulate", "items": [("Scenario Lab", "scenarios.lab", "⚗"), ("Digital Twin", "scenarios.twin", "◈"), ("Rebalancing", "optimization.rebalancing", "⇄")]},
    {"group": "Value", "items": [("Financial", "finance.financial", "₹"), ("Sustainability", "finance.sustainability", "♻"), ("Industry Mode", "industry.home", "⚙")]},
    {"group": "Decide", "items": [("Action Center", "actions.center", "✔"), ("Autonomy", "actions.autonomy", "⏻"), ("Experiments", "governance.experiments", "⚖"),
                                  ("Governance", "governance.home", "§")]},
    {"group": "System", "items": [("Reports", "reports.home", "⎙"), ("Settings", "settings.home", "⚒")]},
]
