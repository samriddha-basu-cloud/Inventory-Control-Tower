"""Plotly figure builders. Colours are *tokens* ("@s1", "@good", "@seq4"...) resolved in the browser from CSS variables so
charts follow light/dark themes. Categorical hues are assigned in fixed order; status colours are reserved for status;
heatmaps use a single-hue sequential ramp with the value printed in every cell (never colour-only)."""
from __future__ import annotations

import math

SEQ = ["@seq1", "@seq2", "@seq3", "@seq4", "@seq5", "@seq6", "@seq7"]
STATUS_COLOR = {"LOW": "@good", "NORMAL": "@good", "MEDIUM": "@warn", "WATCH": "@warn", "HIGH": "@serious", "ATTENTION": "@serious", "CRITICAL": "@crit"}
SYMBOL = {"LOW": "circle", "NORMAL": "circle", "MEDIUM": "diamond", "WATCH": "diamond", "HIGH": "triangle-up", "ATTENTION": "triangle-up", "CRITICAL": "x"}


def _base(title: str | None = None, height: int = 320, **layout) -> dict:
    lay = {"height": height, "margin": {"l": 52, "r": 16, "t": 28 if title else 12, "b": 44}, "hovermode": "closest", "showlegend": True,
           "legend": {"orientation": "h", "y": -0.22}, "xaxis": {"automargin": True}, "yaxis": {"automargin": True}}
    if title:
        lay["title"] = {"text": title, "font": {"size": 13}, "x": 0.01}
    lay.update(layout)
    return lay


def _clean(v):
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def line_chart(x, series: list[dict], title=None, height=300, ytitle=None, xtitle=None, fill_first=False) -> dict:
    data = []
    for i, s in enumerate(series):
        tr = {"x": list(x), "y": [_clean(v) for v in s["y"]], "name": s["name"], "type": "scatter", "mode": s.get("mode", "lines"),
              "line": {"color": s.get("color", f"@s{i + 1}"), "width": 2, "dash": s.get("dash", "solid")}, "hovertemplate": s.get("hover", "%{x}<br>%{y:,.0f}<extra>" + s["name"] + "</extra>")}
        if s.get("marker"):
            tr["marker"] = {"size": 6, "color": s.get("color", f"@s{i + 1}")}
        if i == 0 and fill_first:
            tr["fill"] = "tozeroy"
            tr["fillcolor"] = "@fill1"
        data.append(tr)
    return {"data": data, "layout": _base(title, height, yaxis={"title": ytitle, "automargin": True, "rangemode": "tozero"}, xaxis={"title": xtitle, "automargin": True})}


def bar_chart(x, y, title=None, height=300, color="@s1", ytitle=None, horizontal=False, text=None, hover=None, customdata=None, colors=None, name=None, fmt=",.0f") -> dict:
    tr = {"type": "bar", "orientation": "h" if horizontal else "v", "marker": {"color": colors or color, "line": {"width": 0}}, "name": name or "", "textposition": "outside",
          "cliponaxis": False, "hovertemplate": hover or ("%{y}<br>%{x:" + fmt + "}<extra></extra>" if horizontal else "%{x}<br>%{y:" + fmt + "}<extra></extra>")}
    if horizontal:
        tr["x"], tr["y"] = [_clean(v) for v in y], list(x)
    else:
        tr["x"], tr["y"] = list(x), [_clean(v) for v in y]
    if text:
        tr["text"] = text
    if customdata:
        tr["customdata"] = customdata
    lay = _base(title, height, showlegend=False, bargap=0.35)
    if horizontal:
        lay["yaxis"] = {"automargin": True, "autorange": "reversed"}
        lay["xaxis"] = {"title": ytitle, "automargin": True}
        lay["margin"]["l"] = 110
    else:
        lay["yaxis"] = {"title": ytitle, "automargin": True}
    return {"data": [tr], "layout": lay}


def grouped_bars(x, series: list[dict], title=None, height=300, ytitle=None, stack=False, fmt=",.0f", bottom: int = 44) -> dict:
    data = [{"type": "bar", "x": list(x), "y": [_clean(v) for v in s["y"]], "name": s["name"], "marker": {"color": s.get("color", f"@s{i + 1}")},
             "hovertemplate": "%{x}<br>%{y:" + fmt + "}<extra>" + s["name"] + "</extra>"} for i, s in enumerate(series)]
    lay = _base(title, height, barmode="stack" if stack else "group", bargap=0.3, yaxis={"title": ytitle, "automargin": True}, xaxis={"automargin": True, "tickangle": -35 if len(x) > 6 else 0})
    lay["margin"]["b"] = bottom
    lay["legend"] = {"orientation": "h", "y": -0.3 if bottom <= 60 else -0.5}
    return {"data": data, "layout": lay}


def heatmap(cols, rows, z, title=None, height=None, text=None, hrefs=None) -> dict:
    """z values 0-1 (share); text = per-cell labels shown in the cell. Sequential single-hue ramp."""
    height = height or max(260, 34 * len(rows) + 90)
    scale = [[i / (len(SEQ) - 1), c] for i, c in enumerate(SEQ)]
    tr = {"type": "heatmap", "x": cols, "y": rows, "z": z, "colorscale": scale, "zmin": 0, "zmax": max(0.5, max((max(r) for r in z if r), default=1)), "showscale": True,
          "xgap": 2, "ygap": 2, "hovertemplate": "%{y} · %{x}<br>%{text}<extra></extra>", "text": text or [[f"{v:.0%}" for v in r] for r in z],
          "colorbar": {"thickness": 10, "tickformat": ".0%", "len": 0.9}}
    if hrefs:
        tr["customdata"] = hrefs
    ann = []
    zmax = tr["zmax"]
    for i, r in enumerate(rows):
        for j, c in enumerate(cols):
            v = z[i][j]
            ann.append({"x": c, "y": r, "text": (text[i][j] if text else f"{v:.0%}"), "showarrow": False, "font": {"size": 10, "color": "@onseq_hi" if v / zmax > 0.45 else "@onseq_lo"}})
    lay = _base(title, height, showlegend=False, annotations=ann, yaxis={"autorange": "reversed", "automargin": True}, xaxis={"side": "top", "automargin": True})
    lay["margin"] = {"l": 130, "r": 16, "t": 60 if not title else 80, "b": 10}
    return {"data": [tr], "layout": lay}


def pareto(labels, values, classes, title=None, height=300) -> dict:
    cmap = {"A": "@s1", "B": "@s2", "C": "@s3"}
    total = sum(values) or 1
    cum, run = [], 0
    for v in values:
        run += v
        cum.append(run / total)
    tr = {"type": "bar", "x": labels, "y": values, "marker": {"color": [cmap.get(c, "@s8") for c in classes]}, "customdata": [[c, f"{u:.0%}"] for c, u in zip(classes, cum)],
          "hovertemplate": "%{x}<br>value %{y:,.0f}<br>class %{customdata[0]} · cumulative %{customdata[1]}<extra></extra>", "showlegend": False}
    legend = [{"type": "bar", "x": [None], "y": [None], "name": f"Class {k}", "marker": {"color": v}} for k, v in cmap.items()]
    return {"data": [tr] + legend, "layout": _base(title, height, showlegend=True, bargap=0.15, xaxis={"showticklabels": len(labels) <= 30, "tickangle": -60, "automargin": True},
                                                 yaxis={"title": "Annual consumption value", "automargin": True})}


def histogram_lt(obs, static, p50, p90, title=None, height=280) -> dict:
    data = [{"type": "histogram", "x": list(obs), "marker": {"color": "@s1"}, "name": "Observed lead times", "nbinsx": 14, "hovertemplate": "%{x} d<br>%{y} receipts<extra></extra>"}]
    shapes, ann = [], []
    for val, lab, col, dash in ((static, "Static", "@s2", "dash"), (p50, "P50", "@s3", "dot"), (p90, "P90", "@crit", "solid")):
        if val is not None:
            shapes.append({"type": "line", "x0": val, "x1": val, "y0": 0, "y1": 1, "yref": "paper", "line": {"color": col, "width": 2, "dash": dash}})
            ann.append({"x": val, "y": {"Static": 1.16, "P50": 1.08, "P90": 1.0}[lab], "yref": "paper", "text": f"{lab} {val:.0f} d", "showarrow": False, "font": {"size": 11}})
    return {"data": data, "layout": _base(title, height, shapes=shapes, annotations=ann, showlegend=False, margin={"l": 50, "r": 10, "t": 50, "b": 44}, xaxis={"title": "Lead time (days)", "automargin": True}, yaxis={"title": "Receipts", "automargin": True})}


def projection_chart(rows, ss, title=None, height=320, labels=None) -> dict:
    x = labels or [f"W{r['period']}" for r in rows]
    end = [r["ending"] for r in rows]
    data = [
        {"type": "bar", "x": x, "y": [r["receipts"] for r in rows], "name": "Receipts", "marker": {"color": "@s3"}, "opacity": 0.85, "hovertemplate": "%{x}<br>receipts %{y:,.0f}<extra></extra>"},
        {"type": "bar", "x": x, "y": [-(r["order_demand"] + r["forecast_demand"] + r["production_consumption"] + r["transfers_out"]) for r in rows], "name": "Demand (−)", "marker": {"color": "@s2"},
         "opacity": 0.85, "hovertemplate": "%{x}<br>demand %{customdata:,.0f}<extra></extra>", "customdata": [(r["order_demand"] + r["forecast_demand"] + r["production_consumption"] + r["transfers_out"]) for r in rows]},
        {"type": "scatter", "mode": "lines+markers", "x": x, "y": end, "name": "Projected ending inventory", "line": {"color": "@s1", "width": 3}, "marker": {"size": 7, "symbol": [("x" if e < 0 else "circle") for e in end]},
         "hovertemplate": "%{x}<br>ending %{y:,.0f}<extra></extra>"},
        {"type": "scatter", "mode": "lines", "x": x, "y": [ss] * len(x), "name": "Safety stock", "line": {"color": "@crit", "width": 2, "dash": "dash"}, "hovertemplate": "safety stock %{y:,.0f}<extra></extra>"},
    ]
    return {"data": data, "layout": _base(title, height, barmode="relative", bargap=0.35, yaxis={"title": "Units", "automargin": True, "zeroline": True})}


def network_graph(nodes: list[dict], edges: list[dict], title=None, height=460) -> dict:
    """nodes: {id,label,x,y,size,risk,hover,url}; edges: {src,dst,kind,qty}. Risk encoded by colour AND marker symbol."""
    pos = {n["id"]: (n["x"], n["y"]) for n in nodes}
    data = []
    for kind, dash, color in (("supply", "solid", "@line"), ("transfer", "dot", "@s1"), ("shipment", "dash", "@s2")):
        xs, ys = [], []
        for e in edges:
            if e["kind"] != kind or e["src"] not in pos or e["dst"] not in pos:
                continue
            xs += [pos[e["src"]][0], pos[e["dst"]][0], None]
            ys += [pos[e["src"]][1], pos[e["dst"]][1], None]
        if xs:
            data.append({"type": "scatter", "mode": "lines", "x": xs, "y": ys, "name": kind.title() + " flow", "line": {"color": color, "width": 1.5, "dash": dash}, "hoverinfo": "skip"})
    for lvl in ("LOW", "MEDIUM", "HIGH", "CRITICAL"):
        ns = [n for n in nodes if n["risk"] == lvl]
        if not ns:
            continue
        crowded = len(nodes) > 40
        data.append({"type": "scatter", "mode": "markers+text", "x": [n["x"] for n in ns], "y": [n["y"] for n in ns], "text": ["" if (crowded and n["kind"] == "Supplier") else n["label"] for n in ns], "textposition": "bottom center",
                     "textfont": {"size": 10}, "name": f"Risk {lvl.title()}", "marker": {"size": [n["size"] for n in ns], "color": STATUS_COLOR[lvl], "symbol": SYMBOL[lvl],
                                                                                  "line": {"width": 2, "color": "@surface"}}, "hovertext": [n["hover"] for n in ns], "hoverinfo": "text",
                     "customdata": [n.get("url") for n in ns]})
    return {"data": data, "layout": _base(title, height, xaxis={"visible": False}, yaxis={"visible": False, "autorange": "reversed"}, margin={"l": 10, "r": 10, "t": 10, "b": 10},
                                         legend={"orientation": "h", "y": -0.04})}


def waterfall(labels, values, title=None, height=300) -> dict:
    return {"data": [{"type": "waterfall", "x": labels, "y": values, "measure": ["relative"] * (len(values) - 1) + ["total"], "connector": {"line": {"color": "@line"}},
                      "increasing": {"marker": {"color": "@crit"}}, "decreasing": {"marker": {"color": "@good"}}, "totals": {"marker": {"color": "@s1"}},
                      "hovertemplate": "%{x}<br>%{y:,.0f}<extra></extra>"}], "layout": _base(title, height, showlegend=False)}


def compare_bars(metric_label, baseline, scenario, fmt=",.0f", height=200) -> dict:
    return {"data": [{"type": "bar", "x": ["Baseline", "Scenario"], "y": [_clean(baseline), _clean(scenario)], "marker": {"color": ["@s3", "@s1"]}, "text": [format(baseline or 0, fmt), format(scenario or 0, fmt)],
                      "textposition": "outside", "cliponaxis": False, "hovertemplate": "%{x}<br>%{y:" + fmt + "}<extra></extra>"}],
            "layout": _base(metric_label, height, showlegend=False, bargap=0.45, margin={"l": 40, "r": 10, "t": 34, "b": 30}, yaxis={"rangemode": "tozero", "automargin": True})}
