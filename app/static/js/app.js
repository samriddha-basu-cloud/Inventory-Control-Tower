/* Inventory Control Tower - front-end behaviour. No frameworks: tables, plots, search, drawer, modals, theme. */
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var store = {
    get: function (k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { window.localStorage.setItem(k, v); } catch (e) { /* storage blocked: degrade silently */ } }
  };
  var csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
  window.ICT = { csrf: csrf };

  /* ---------- theme ---------- */
  function applyTheme(t) { if (t) document.documentElement.setAttribute("data-theme", t); }
  applyTheme(store.get("ict-theme"));
  var tt = $("#theme-toggle");
  if (tt) tt.addEventListener("click", function () {
    var cur = document.documentElement.getAttribute("data-theme");
    var next = cur === "dark" ? "light" : (cur === "light" ? "dark" : (window.matchMedia("(prefers-color-scheme: dark)").matches ? "light" : "dark"));
    applyTheme(next); store.set("ict-theme", next); setTimeout(renderPlots, 30);
  });
  var hb = $("#hamburger"); if (hb) hb.addEventListener("click", function () { $("#sidebar").classList.toggle("open"); });

  /* ---------- plots ---------- */
  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function resolve(o) {
    if (typeof o === "string") { return o.charAt(0) === "@" ? (css("--" + o.slice(1).replace("_", "-")) || o) : o; }
    if (Array.isArray(o)) return o.map(resolve);
    if (o && typeof o === "object") { var out = {}; Object.keys(o).forEach(function (k) { out[k] = resolve(o[k]); }); return out; }
    return o;
  }
  function themeLayout() {
    var ink = css("--ink2"), grid = css("--grid"), line = css("--line");
    return { paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)", font: { family: css("--font") || "system-ui", color: ink, size: 12 },
      xaxis: { gridcolor: grid, linecolor: line, zerolinecolor: line }, yaxis: { gridcolor: grid, linecolor: line, zerolinecolor: line } };
  }
  function merge(a, b) { Object.keys(b).forEach(function (k) { if (b[k] && typeof b[k] === "object" && !Array.isArray(b[k]) && a[k] && typeof a[k] === "object") merge(a[k], b[k]); else a[k] = b[k]; }); return a; }
  function renderPlots() {
    if (!window.Plotly) return;
    $$(".plot").forEach(function (el) {
      var raw = el.getAttribute("data-fig"); if (!raw) return;
      var fig; try { fig = resolve(JSON.parse(raw)); } catch (e) { el.textContent = "Chart unavailable."; return; }
      var layout = merge(themeLayout(), fig.layout || {});
      ["xaxis", "yaxis"].forEach(function (a) { var t = themeLayout()[a]; layout[a] = merge(Object.assign({}, t), (fig.layout || {})[a] || {}); });
      el.classList.remove("loading");
      window.Plotly.react(el, fig.data, layout, { displaylogo: false, responsive: true, displayModeBar: "hover", modeBarButtonsToRemove: ["lasso2d", "select2d"] });
      if (!el._bound) {
        el._bound = true;
        el.on && el.on("plotly_click", function (ev) { var p = ev.points && ev.points[0]; var u = p && p.customdata; if (Array.isArray(u)) u = u[0]; if (typeof u === "string" && u.charAt(0) === "/") window.location = u; });
      }
    });
  }
  window.addEventListener("load", renderPlots); window.addEventListener("resize", function () { $$(".plot").forEach(function (el) { if (window.Plotly && el.data) window.Plotly.Plots.resize(el); }); });
  $$("[data-toggle-table]").forEach(function (b) { b.addEventListener("click", function () { var box = document.getElementById(b.getAttribute("data-toggle-table")); box.classList.toggle("show-table"); b.textContent = box.classList.contains("show-table") ? "Show chart" : "Show table"; }); });

  /* ---------- data tables: sort / search / group / export ---------- */
  function cellVal(td) { var s = td.getAttribute("data-sort"); if (s !== null && s !== "") { var n = parseFloat(s); return isNaN(n) ? s.toLowerCase() : n; } var t = td.textContent.trim().replace(/[,%₹]/g, ""); var f = parseFloat(t); return (t !== "" && !isNaN(f) && /^[-\d.,\s%₹A-Za-z]*$/.test(td.textContent.trim()) && /^-?[\d.,]+/.test(t)) ? f : td.textContent.trim().toLowerCase(); }
  function enhance(table) {
    var wrap = table.closest(".tbl"); var body = table.tBodies[0]; if (!body) return;
    var rows = function () { return $$("tr:not(.group-row)", body); };
    var ths = $$("thead th", table);
    ths.forEach(function (th, i) {
      th.addEventListener("click", function () {
        var dir = th.classList.contains("sorted-asc") ? -1 : 1;
        ths.forEach(function (x) { x.classList.remove("sorted-asc", "sorted-desc"); });
        th.classList.add(dir === 1 ? "sorted-asc" : "sorted-desc");
        var rs = rows(); rs.sort(function (a, b) { var x = cellVal(a.cells[i]), y = cellVal(b.cells[i]); return (x > y ? 1 : x < y ? -1 : 0) * dir; });
        rs.forEach(function (r) { body.appendChild(r); }); regroup();
      });
    });
    var search = wrap && $("input[type=search]", wrap); var count = wrap && $(".count", wrap);
    function update() {
      var q = search ? search.value.trim().toLowerCase() : ""; var n = 0;
      rows().forEach(function (r) { var show = !q || r.textContent.toLowerCase().indexOf(q) > -1; r.hidden = !show; if (show) n++; });
      if (count) count.textContent = n + " of " + rows().length + " rows";
    }
    if (search) search.addEventListener("input", update); update();
    var gsel = wrap && $("select.group-by", wrap);
    function regroup() {
      $$("tr.group-row", body).forEach(function (r) { r.remove(); }); if (!gsel || !gsel.value) return;
      var gi = parseInt(gsel.value, 10), last = null, rs = rows().filter(function (r) { return !r.hidden; });
      rs.sort(function (a, b) { var x = a.cells[gi].textContent.trim(), y = b.cells[gi].textContent.trim(); return x > y ? 1 : x < y ? -1 : 0; });
      rs.forEach(function (r) {
        var g = r.cells[gi].textContent.trim();
        if (g !== last) { var gr = document.createElement("tr"); gr.className = "group-row"; var td = document.createElement("td"); td.colSpan = r.cells.length; td.textContent = "▾ " + (g || "(blank)"); gr.appendChild(td); body.appendChild(gr); last = g; }
        body.appendChild(r);
      });
    }
    if (gsel) gsel.addEventListener("change", regroup);
    var ex = wrap && $("[data-export]", wrap);
    if (ex) ex.addEventListener("click", function () {
      var out = [ths.map(function (t) { return t.textContent.trim(); })];
      rows().filter(function (r) { return !r.hidden; }).forEach(function (r) { out.push($$("td", r).map(function (td) { return td.textContent.trim().replace(/\s+/g, " "); })); });
      var csv = out.map(function (r) { return r.map(function (c) { c = /^[=+\-@]/.test(c) ? "'" + c : c; return '"' + c.replace(/"/g, '""') + '"'; }).join(","); }).join("\n");
      var a = document.createElement("a"); a.href = URL.createObjectURL(new Blob(["﻿" + csv], { type: "text/csv" })); a.download = (table.getAttribute("data-name") || "export") + ".csv"; a.click();
    });
    $$("tr[data-href]", body).forEach(function (r) { r.style.cursor = "pointer"; r.addEventListener("click", function (e) { if (e.target.closest("a,button,input,select,form")) return; window.location = r.getAttribute("data-href"); }); });
  }
  $$("table.dt").forEach(enhance);

  /* ---------- global search ---------- */
  var si = $("#global-search"), sr = $("#search-results"), timer = null;
  if (si) {
    si.addEventListener("input", function () {
      clearTimeout(timer); var q = si.value.trim(); if (q.length < 2) { sr.style.display = "none"; return; }
      timer = setTimeout(function () {
        fetch("/api/search?q=" + encodeURIComponent(q), { headers: { "Accept": "application/json" } }).then(function (r) { return r.json(); }).then(function (d) {
          sr.innerHTML = ""; (d.results || []).forEach(function (x) { var a = document.createElement("a"); a.href = x.url; a.innerHTML = '<span class="t"></span><span><b></b><br><span class="small muted"></span></span>'; a.children[0].textContent = x.type; a.querySelector("b").textContent = x.label; a.querySelector(".small").textContent = x.sub || ""; sr.appendChild(a); });
          if (!d.results || !d.results.length) sr.innerHTML = '<div class="empty small">No matches for “' + q.replace(/</g, "&lt;") + '”</div>'; sr.style.display = "block";
        }).catch(function () { sr.style.display = "none"; });
      }, 200);
    });
    si.addEventListener("keydown", function (e) { if (e.key === "Escape") sr.style.display = "none"; if (e.key === "Enter") { var a = $("a", sr); if (a) window.location = a.href; } });
    document.addEventListener("click", function (e) { if (!e.target.closest(".search")) sr.style.display = "none"; });
    document.addEventListener("keydown", function (e) { if (e.key === "/" && !/input|textarea|select/i.test(document.activeElement.tagName)) { e.preventDefault(); si.focus(); } });
  }

  /* ---------- drawer ---------- */
  var drawer = $("#drawer"), scrim = $("#scrim");
  function closeDrawer() { drawer.classList.remove("open"); scrim.classList.remove("open"); }
  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-drawer]"); if (t) {
      e.preventDefault(); drawer.innerHTML = '<p class="muted">Loading…</p>'; drawer.classList.add("open"); scrim.classList.add("open");
      fetch(t.getAttribute("data-drawer")).then(function (r) { return r.text(); }).then(function (h) { drawer.innerHTML = '<button class="btn sm" data-close style="float:right">Close ✕</button>' + h; renderPlots(); enhanceAll(drawer); });
    }
    if (e.target.closest("[data-close]") || e.target === scrim) closeDrawer();
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && drawer) closeDrawer(); });
  function enhanceAll(root) { $$("table.dt", root).forEach(enhance); }

  /* ---------- confirmation modal ---------- */
  var dlg = $("#confirm-dialog"), pending = null;
  document.addEventListener("submit", function (e) {
    var f = e.target; var msg = f.getAttribute && f.getAttribute("data-confirm");
    if (msg && !f._ok && dlg && dlg.showModal) { e.preventDefault(); pending = f; $("#confirm-msg", dlg).textContent = msg; dlg.showModal(); }
  }, true);
  if (dlg) { $("#confirm-yes", dlg).addEventListener("click", function () { dlg.close(); if (pending) { pending._ok = true; pending.submit(); } }); $("#confirm-no", dlg).addEventListener("click", function () { dlg.close(); pending = null; }); }

  /* ---------- misc ---------- */
  $$("form[data-autosubmit] select, form[data-autosubmit] input[type=checkbox]").forEach(function (el) { el.addEventListener("change", function () { el.form.submit(); }); });
  $$("[data-dynamic-fields]").forEach(function (sel) {
    var target = document.getElementById(sel.getAttribute("data-dynamic-fields")); if (!target) return;
    function show() { $$("[data-for]", target).forEach(function (d) { var on = d.getAttribute("data-for") === sel.value; d.hidden = !on; $$("input,select", d).forEach(function (i) { i.disabled = !on; }); }); }
    sel.addEventListener("change", show); show();
  });
  window.ICT.renderPlots = renderPlots;
})();
