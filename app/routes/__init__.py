"""Blueprint registration."""
from flask import Flask


def register_blueprints(app: Flask) -> None:
    from . import (actions, alerts, api, datahub, demand, finance, governance, industry, inventory, main, master, network, optimization, replenishment,
                   reports, risk, scenarios, settings)
    for mod in (main, inventory, network, datahub, master, demand, replenishment, risk, alerts, scenarios, optimization, finance, industry, actions, governance,
                reports, settings):
        app.register_blueprint(mod.bp)
    app.register_blueprint(api.bp, url_prefix="/api")
    app.jinja_env.filters["rowfmt"] = lambda tpl, row: tpl.format(**{k: v for k, v in row.items()})
