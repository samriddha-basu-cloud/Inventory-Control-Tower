import io
from datetime import datetime
from flask import Blueprint, render_template, send_file
import pandas as pd

from app.models import Item, Location, Supplier, SafetyStockPolicy, Alert, InventoryLedger
from app.services import inventory_service, demand_stats, risk_service, financial_service
from app.analytics.aging import age_days, aging_summary
from app.routes.inventory import _network_abc_xyz_dataframe

bp = Blueprint("reports", __name__, url_prefix="/reports")


@bp.route("/")
def home():
    return render_template("reports/home.html")


@bp.route("/excel")
def excel_export():
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:

        # README / Methodology
        readme = pd.DataFrame({
            "Sheet": ["Executive Summary", "Inventory Position", "Aging", "ABC_XYZ", "Safety Stock",
                      "Stockout Risk", "Excess", "Supplier", "Alerts", "Methodology"],
            "Description": [
                "Network KPIs, health index, financials.",
                "On hand / available / allocated / in-transit / on-order per SKU-location.",
                "Inventory aging buckets by value and quantity.",
                "ABC (value) x XYZ (variability) segmentation.",
                "Calculated vs override safety stock per SKU-location.",
                "Stockout probability, days to stockout, revenue at risk.",
                "Excess inventory beyond target stock, with classification.",
                "Supplier OTIF, lead time, risk score.",
                "Open alerts by type/severity.",
                "Formula reference for every calculation in this workbook.",
            ],
        })
        readme.to_excel(writer, sheet_name="README", index=False)

        kpis = inventory_service.network_kpis()
        financials = financial_service.network_financials()
        health = financial_service.network_health_index()
        exec_df = pd.DataFrame({
            "Metric": ["On Hand Units", "Available Units", "Allocated Units", "In Transit Units",
                       "On Order Units", "Total Inventory Value", "Excess Value", "Obsolete Value",
                       "Inventory Turns", "DIO (days)", "FIT Health Index"],
            "Value": [kpis["on_hand_units"], kpis["available_units"], kpis["allocated_units"],
                      kpis["in_transit_units"], kpis["on_order_units"], kpis["total_inventory_value"],
                      kpis["excess_value"], kpis["obsolete_value"], financials["inventory_turns"],
                      financials["dio_days"], health["score"]],
        })
        exec_df.to_excel(writer, sheet_name="Executive Summary", index=False)

        position_rows = []
        for item in Item.query.filter_by(is_active=True).all():
            for loc in Location.query.all():
                qtys = inventory_service.status_quantities(item.id, loc.id)
                if not any(qtys.values()):
                    continue
                pos = inventory_service.inventory_position(item.id, loc.id)
                position_rows.append({"SKU": item.sku, "Location": loc.code, **qtys,
                                       "Inventory Position": pos["inventory_position"]})
        pd.DataFrame(position_rows).to_excel(writer, sheet_name="Inventory Position", index=False)

        aging_rows = []
        for ledger in InventoryLedger.query.filter_by(status="ON_HAND").all():
            ref = ledger.manufacture_date or ledger.as_of.date()
            aging_rows.append({"quantity": ledger.quantity, "age_days": age_days(ref),
                                "unit_cost": ledger.item.unit_cost if ledger.item else 0})
        summary = aging_summary(aging_rows)
        pd.DataFrame([{"Bucket": k, **v} for k, v in summary.items()]).to_excel(
            writer, sheet_name="Aging", index=False)

        abc_df = _network_abc_xyz_dataframe()
        if not abc_df.empty:
            abc_df.to_excel(writer, sheet_name="ABC_XYZ", index=False)

        ss_rows = [{"SKU": p.item.sku, "Location": p.location.code, "Method": p.method,
                    "Service Level %": p.service_level_pct, "Calculated SS": p.calculated_safety_stock,
                    "Override SS": p.override_safety_stock, "Effective SS": p.effective_safety_stock}
                   for p in SafetyStockPolicy.query.all()]
        pd.DataFrame(ss_rows).to_excel(writer, sheet_name="Safety Stock", index=False)

        risk_rows = []
        for item in Item.query.filter_by(is_active=True).all():
            for loc in Location.query.filter(Location.node_type.in_(["dc", "store"])).all():
                d = demand_stats.daily_demand_stats(item.id, loc.id)
                if d["avg_demand_daily"] <= 0:
                    continue
                r = risk_service.stockout_risk(item.id, loc.id)
                if r["probability_pct"] > 0:
                    risk_rows.append({"SKU": item.sku, "Location": loc.code, **r})
        pd.DataFrame(risk_rows).to_excel(writer, sheet_name="Stockout Risk", index=False)

        excess_rows = []
        for item in Item.query.filter_by(is_active=True).all():
            for loc in Location.query.all():
                e = risk_service.excess_detection(item.id, loc.id)
                if e["is_excess"]:
                    excess_rows.append({"SKU": item.sku, "Location": loc.code, **e})
        pd.DataFrame(excess_rows).to_excel(writer, sheet_name="Excess", index=False)

        supplier_rows = [{"Code": s.code, "Name": s.name, "OTIF %": s.otif_pct,
                           "Lead Time Mean": s.lead_time_mean_days, "Lead Time Std": s.lead_time_std_days,
                           "Single Source Risk": s.single_source_risk,
                           **{f"risk_{k}": v for k, v in risk_service.supplier_risk_score(s).items()
                              if k != "components"}}
                          for s in Supplier.query.all()]
        pd.DataFrame(supplier_rows).to_excel(writer, sheet_name="Supplier", index=False)

        alert_rows = [{"ID": a.id, "Type": a.alert_type, "SKU": a.item.sku if a.item else None,
                        "Location": a.location.code if a.location else None, "Severity": a.severity,
                        "Financial Impact": a.financial_impact, "Status": a.status, "Message": a.message}
                       for a in Alert.query.filter(Alert.status != "CLOSED").all()]
        pd.DataFrame(alert_rows).to_excel(writer, sheet_name="Alerts", index=False)

        methodology = pd.DataFrame({
            "Topic": ["Safety Stock (combined variability)", "Reorder Point", "EOQ", "Inventory Turns",
                      "DIO", "DOS", "FIT Health Index"],
            "Formula": ["SS = Z * sqrt(LT*sigma_D^2 + D_avg^2*sigma_LT^2)", "ROP = D_avg*LT + SS",
                        "EOQ = sqrt(2*D*S/H)", "Turns = Annualized COGS / Average Inventory Value",
                        "DIO = (Avg Inventory / COGS) * 365", "DOS = Available Inventory / Avg Daily Demand",
                        "100 - weighted penalty across stockout/excess/obsolescence/service/accuracy/lead-time risk"],
        })
        methodology.to_excel(writer, sheet_name="Methodology", index=False)

    buf.seek(0)
    filename = f"ICT_Report_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.xlsx"
    return send_file(buf, as_attachment=True, download_name=filename,
                      mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
