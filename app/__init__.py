"""Inventory Control Tower (ICT) - Flask application factory."""
from __future__ import annotations

import logging
import os

import click
from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException

from .config import CONFIGS
from .extensions import db, migrate


def create_app(config_name: str | None = None, register_routes: bool = True) -> Flask:
    config_name = config_name or os.environ.get("ENVIRONMENT", "development")
    cfg = CONFIGS.get(config_name, CONFIGS["development"])
    app = Flask(__name__, instance_relative_config=False)
    app.config.from_object(cfg() if config_name == "production" else cfg)
    from .utils.jsonutil import ICTJSONProvider
    app.json = ICTJSONProvider(app)
    if config_name == "production" and not os.environ.get("SECRET_KEY"):
        raise RuntimeError("SECRET_KEY environment variable is required in production")
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    db.init_app(app)
    migrate.init_app(app, db, directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), "migrations"))
    from . import models  # noqa: F401  (register tables)

    with app.app_context():
        if app.config.get("AUTO_INIT_DB"):
            db.create_all()
            from .services.seed_service import seed_reference_data
            seed_reference_data()

    if register_routes:
        _register_web(app)
    _register_cli(app)
    return app


ALIASES = {"inventory.sku360": "inventory.explorer", "inventory.location360": "inventory.explorer", "inventory.supplier360": "inventory.explorer",
           "alerts.alert_detail": "alerts.list_alerts", "alerts.incident_detail": "alerts.incidents", "risk.heatmap": "risk.center", "demand.chain": "demand.home",
           "scenarios.sop": "scenarios.lab", "scenarios.scenario_detail": "scenarios.lab", "scenarios.compare": "scenarios.lab", "finance.circular": "finance.sustainability",
           "actions.action_detail": "actions.center", "reports.view": "reports.home", "main.load_demo": "main.home"}


def _register_web(app: Flask) -> None:
    from .utils import fmt, security
    from .routes import register_blueprints

    register_blueprints(app)
    app.jinja_env.globals["csrf_token"] = security.csrf_token
    app.jinja_env.globals["has_perm"] = security.has_perm

    for name, fn in [("num", fmt.num), ("pct", fmt.pct), ("money", fmt.money), ("dt", fmt.dt), ("days", fmt.days), ("kpival", fmt.kpi_value)]:
        app.jinja_env.filters[name] = fn

    @app.before_request
    def _security():
        security.check_csrf()
        security.load_user()
        if app.config.get("AUTH_REQUIRED") and not g.user and request.endpoint not in ("main.login", "static", "main.health", "main.plotly_js"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "authentication required"}), 401
            return redirect(url_for("main.login", next=request.path))

    @app.context_processor
    def _inject():
        from .services import settings_service as S, industry_service
        from .routes.nav import NAV
        from .routes import helpers
        from .services.snapshot import PairFilter, get_snapshot
        from .models import Action, Alert
        ctx = {"NAV": NAV, "csrf_token": security.csrf_token, "user": getattr(g, "user", None), "has_perm": security.has_perm, "terms": {}, "industry_name": "General",
               "gfilter": PairFilter.from_mapping(session.get("filters", {})), "current_path": request.path, "currency": app.config["CURRENCY_SYMBOL"],
               "auth_required": app.config["AUTH_REQUIRED"], "roles": list(security.ROLE_RANK), "facets": {}, "freshness_score": None, "freshness_note": "", "nav_alerts": 0,
               "nav_actions": 0, "exec_mode": "SIMULATION_ONLY", "as_of": "", "data_ver": "", "active_ep": request.endpoint}
        try:
            prof = industry_service.active_profile()
            ctx["terms"] = (prof.terminology or {}) if prof else {}
            ctx["industry_name"] = prof.name if prof else "General"
            ctx["exec_mode"] = S.get("execution.mode")
            ctx["as_of"] = S.today().isoformat()
            ctx["data_ver"] = "%s.%s" % S.data_version()
            if helpers.has_data():
                sn = get_snapshot()
                ctx["facets"] = helpers.facets(sn)
                ctx["freshness_score"], ctx["freshness_note"] = sn.freshness["score"], sn.freshness["note"]
            ctx["nav_alerts"] = Alert.query.filter(Alert.status.in_(["New", "Acknowledged", "Investigating", "Action Proposed"]), Alert.suppressed.is_(False),
                                                   Alert.severity.in_(["HIGH", "CRITICAL"])).count()
            ctx["nav_actions"] = Action.query.filter(Action.status.in_(["PENDING_APPROVAL", "ESCALATED"])).count()
        except Exception:  # pragma: no cover - never let chrome data break a page
            app.logger.exception("context processor failed")
        eps = {ep for grp in NAV for _, ep, _ in grp["items"]}
        ep = request.endpoint or ""
        if ep not in eps:
            alias = ALIASES.get(ep)
            if not alias:
                bp = ep.split(".")[0]
                alias = next((e for e in sorted(eps) if e.split(".")[0] == bp), ep)
            ep = alias
        ctx["active_ep"] = ep
        return ctx

    @app.errorhandler(Exception)
    def _errors(e):
        if isinstance(e, HTTPException):
            code, msg = e.code, e.description
        else:
            app.logger.exception("Unhandled error")
            code, msg = 500, "Something went wrong while processing your request. The error has been logged."
        if request.path.startswith("/api/"):
            return jsonify({"error": msg, "status": code}), code
        try:
            return render_template("errors/error.html", code=code, message=msg), code
        except Exception:  # pragma: no cover
            return f"{code}: {msg}", code


def _register_cli(app: Flask) -> None:
    @app.cli.command("init-db")
    def init_db():
        """Create tables and seed reference/configuration data."""
        from .services.seed_service import seed_reference_data
        db.create_all()
        click.echo(f"Seeded: {seed_reference_data()}")

    @app.cli.command("load-demo")
    @click.option("--industries", default="ALL", help="Comma list of industries or ALL")
    @click.option("--seed", default=42)
    def load_demo(industries, seed):
        """Load the synthetic demo network."""
        from .services import demo_service
        from .services.demo_specs import SPECS
        inds = list(SPECS) if industries.upper() == "ALL" else [x.strip().upper() for x in industries.split(",")]
        click.echo(demo_service.load_demo(inds, seed=seed))

    @app.cli.command("run-detection")
    def run_detection():
        from .services import alert_service
        click.echo(alert_service.run_detection(actor="cli"))
