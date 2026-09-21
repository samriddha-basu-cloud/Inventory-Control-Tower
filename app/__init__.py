import os
from flask import Flask, render_template

from app.config import config_map
from app.extensions import db


def create_app(config_name=None):
    config_name = config_name or os.environ.get("ENVIRONMENT", "development")
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_map.get(config_name, config_map["development"]))

    os.makedirs(app.instance_path, exist_ok=True)

    db.init_app(app)

    from app import models as _models  # noqa: F401 - ensures metadata is populated before create_all()

    with app.app_context():
        db.create_all()

    register_blueprints(app)
    register_cli(app)
    register_template_helpers(app)
    register_error_handlers(app)

    return app


def register_blueprints(app):
    from app.routes.main import bp as main_bp
    from app.routes.dashboard import bp as dashboard_bp
    from app.routes.inventory import bp as inventory_bp
    from app.routes.network import bp as network_bp
    from app.routes.optimization import bp as optimization_bp
    from app.routes.alerts import bp as alerts_bp
    from app.routes.scenarios import bp as scenarios_bp
    from app.routes.allocation import bp as allocation_bp
    from app.routes.replenishment import bp as replenishment_bp
    from app.routes.reports import bp as reports_bp
    from app.routes.master_data import bp as master_data_bp
    from app.routes.api import bp as api_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(inventory_bp)
    app.register_blueprint(network_bp)
    app.register_blueprint(optimization_bp)
    app.register_blueprint(alerts_bp)
    app.register_blueprint(scenarios_bp)
    app.register_blueprint(allocation_bp)
    app.register_blueprint(replenishment_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(master_data_bp)
    app.register_blueprint(api_bp, url_prefix="/api")


def register_cli(app):
    @app.cli.command("init-db")
    def init_db():
        """Create all database tables."""
        with app.app_context():
            db.create_all()
        print("Database initialized.")

    @app.cli.command("load-demo")
    def load_demo():
        """Load synthetic demo network (FMCG-led multi-node scenario with a supplier-delay incident)."""
        from app.services.demo_data_service import load_demo_data

        with app.app_context():
            db.create_all()
            summary = load_demo_data(reset=True)
            print(f"Demo data loaded: {summary}")


def register_template_helpers(app):
    from datetime import datetime

    @app.context_processor
    def inject_globals():
        return {
            "now": datetime.utcnow(),
            "app_name": "Inventory Control Tower",
            "app_short": "ICT",
            "tagline": "See. Predict. Optimize. Execute.",
        }

    @app.template_filter("money")
    def money_filter(value, currency="₹"):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return value
        if abs(value) >= 10_000_000:
            return f"{currency}{value/10_000_000:,.2f}Cr"
        if abs(value) >= 100_000:
            return f"{currency}{value/100_000:,.2f}L"
        return f"{currency}{value:,.0f}"

    @app.template_filter("num")
    def num_filter(value):
        try:
            return f"{float(value):,.0f}"
        except (TypeError, ValueError):
            return value

    @app.template_filter("pct")
    def pct_filter(value, digits=1):
        try:
            return f"{float(value):.{digits}f}%"
        except (TypeError, ValueError):
            return value


def register_error_handlers(app):
    @app.errorhandler(404)
    def not_found(e):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(e):
        return render_template("errors/500.html"), 500
