"""Regenerates the small CSV sample set used to try the Data Hub (fictional 'Sample Co.'). Run: python data/sample/make_sample.py
Upload in this order: locations, suppliers, customers, items, item_suppliers, balances, demand, lead_times, purchase_orders, sales_orders."""
import csv
import random
from datetime import date, timedelta
from pathlib import Path

out = Path(__file__).parent
rnd = random.Random(7)
today = date.today()
monday = today - timedelta(days=today.weekday())


def w(name, header, rows):
    with open(out / f"{name}.csv", "w", newline="") as f:
        cw = csv.writer(f)
        cw.writerow(header)
        cw.writerows(rows)


w("locations", ["code", "name", "type", "region", "country", "city", "lat", "lon", "capacity_units", "parent", "echelon"], [
    ["SMP-PLANT", "Sample Plant Pune", "PLANT", "West", "IN", "Pune", 18.52, 73.86, 200000, "", 1],
    ["SMP-DC-N", "Sample DC North", "DC", "North", "IN", "Delhi", 28.61, 77.21, 120000, "SMP-PLANT", 2],
    ["SMP-DC-S", "Sample DC South", "DC", "South", "IN", "Chennai", 13.08, 80.27, 100000, "SMP-PLANT", 2]])
w("suppliers", ["code", "name", "country", "city", "tier", "lat", "lon", "payment_terms_days"], [
    ["SMP-SUP-A", "Alpha Components (fictional)", "IN", "Nashik", 1, 19.99, 73.79, 45],
    ["SMP-SUP-B", "Beta Packaging (fictional)", "CN", "Shenzhen", 1, 22.54, 114.06, 60]])
w("customers", ["code", "name", "segment", "priority", "region", "country"], [["SMP-CUST-1", "Sample Retail Chain (fictional)", "KEY_ACCOUNT", 1, "North", "IN"]])
items = [("SMP-100", "Widget A", 120, 210, 50, 10), ("SMP-200", "Widget B", 45, 90, 100, 20), ("SMP-300", "Bracket C", 15, 40, 500, 100),
         ("SMP-400", "Carton D", 4, 9, 1000, 250), ("SMP-500", "Sensor E", 640, 990, 20, 5)]
w("items", ["sku", "description", "category", "family", "industry", "uom", "unit_cost", "selling_price", "moq", "order_multiple", "criticality"],
  [[s, d, "FG", "SMP", "MANUFACTURING", "EA", c, p, moq, mult, "MEDIUM"] for s, d, c, p, moq, mult in items])
w("item_suppliers", ["sku", "supplier", "lead_time_days", "moq", "order_multiple", "price", "mode"],
  [[s, "SMP-SUP-A" if i % 2 == 0 else "SMP-SUP-B", 12 if i % 2 == 0 else 35, moq, mult, c, "ROAD" if i % 2 == 0 else "SEA"] for i, (s, d, c, p, moq, mult) in enumerate(items)])
locs = ["SMP-DC-N", "SMP-DC-S"]
w("balances", ["sku", "location", "qty"], [[s, l, rnd.randint(300, 2500)] for s, *_ in items for l in locs])
dem = []
for s, *_ in items:
    base = rnd.randint(80, 400)
    for l in locs:
        for k in range(26, 0, -1):
            dem.append([s, l, (monday - timedelta(weeks=k)).isoformat(), max(0, round(rnd.gauss(base, base * 0.25)))])
w("demand", ["sku", "location", "period_start", "qty"], dem)
w("lead_times", ["supplier", "sku", "location", "lead_time_days", "promised_days", "received_date"],
  [["SMP-SUP-A" if i % 2 == 0 else "SMP-SUP-B", s, l, round(rnd.gauss(12 if i % 2 == 0 else 35, 3 if i % 2 == 0 else 8)), 12 if i % 2 == 0 else 35,
    (today - timedelta(days=rnd.randint(5, 200))).isoformat()] for i, (s, *_) in enumerate(items) for l in locs for _ in range(10)])
w("purchase_orders", ["po_number", "supplier", "location", "sku", "qty", "promised_date", "eta_date", "unit_price"],
  [[f"SMP-PO-{i + 1:03d}", "SMP-SUP-A" if i % 2 == 0 else "SMP-SUP-B", locs[i % 2], s, 1500, (today + timedelta(days=9 + i * 3)).isoformat(),
    (today + timedelta(days=12 + i * 3)).isoformat(), c] for i, (s, d, c, *_) in enumerate(items)])
w("sales_orders", ["so_number", "customer", "location", "sku", "qty", "requested_date", "unit_price"],
  [[f"SMP-SO-{i + 1:03d}", "SMP-CUST-1", locs[i % 2], s, 400, (today + timedelta(days=4 + i * 2)).isoformat(), p] for i, (s, d, c, p, *_) in enumerate(items)])
print("written:", sorted(p.name for p in out.glob("*.csv")))
