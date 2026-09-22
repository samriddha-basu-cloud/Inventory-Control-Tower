# Deployment

**Local**: `python run.py` (SQLite at `instance/ict.db`, tables and configuration seeded automatically).

**Production (Render / any WSGI host)**
1. **Pin the Python version.** `runtime.txt` (`python-3.11.9`) and `render.yaml`'s `PYTHON_VERSION` env var do this for Render. This matters: `pandas==2.2.2`/`numpy==1.26.4` only ship
   prebuilt wheels up to Python 3.12, so an unpinned build host that defaults to a newer Python (Render currently defaults to 3.14) falls back to compiling pandas from source and fails
   (a Cython/GCC `[[maybe_unused]]` attribute error). If Render already created the service before `render.yaml` had `PYTHON_VERSION`, set it manually once under the service's
   Environment tab (or trigger a Blueprint sync) and clear the build cache before redeploying.
2. Provision PostgreSQL; set `DATABASE_URL` (`postgres://` is normalised), `SECRET_KEY` (required), `ENVIRONMENT=production`, `AUTH_REQUIRED=1`, `ADMIN_PASSWORD`, `API_KEY`.
3. `pip install -r requirements.txt` (includes `psycopg2-binary`, needed whenever `DATABASE_URL` points at Postgres).
4. `flask --app run.py db upgrade` (Alembic migrations in `migrations/`); set `AUTO_INIT_DB=0` if you prefer migrations to be the only schema path.
5. Start: `gunicorn wsgi:app --workers 2 --threads 4 --timeout 120` (`Procfile`, `render.yaml` provided). Health check: `/health`.
6. Multiple workers are safe: cache invalidation uses the DB-stored data version. For heavy jobs set `JOBS_SYNC=0` (thread pool) or swap in Celery/RQ behind `services/jobs.py`.

Environment variables are documented in `.env.example`. Optional notification channels (Email/Teams/Slack/Webhook) stay disabled until their variables are set.
