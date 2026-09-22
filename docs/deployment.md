# Deployment

**Local**: `python run.py` (SQLite at `instance/ict.db`, tables and configuration seeded automatically).

**Production (Render / any WSGI host)**
1. Provision PostgreSQL; set `DATABASE_URL` (`postgres://` is normalised), `SECRET_KEY` (required), `ENVIRONMENT=production`, `AUTH_REQUIRED=1`, `ADMIN_PASSWORD`, `API_KEY`.
2. `pip install -r requirements.txt psycopg2-binary`
3. `flask --app run.py db upgrade` (Alembic migrations in `migrations/`); set `AUTO_INIT_DB=0` if you prefer migrations to be the only schema path.
4. Start: `gunicorn wsgi:app --workers 2 --threads 4 --timeout 120` (`Procfile`, `render.yaml` provided). Health check: `/health`.
5. Multiple workers are safe: cache invalidation uses the DB-stored data version. For heavy jobs set `JOBS_SYNC=0` (thread pool) or swap in Celery/RQ behind `services/jobs.py`.

Environment variables are documented in `.env.example`. Optional notification channels (Email/Teams/Slack/Webhook) stay disabled until their variables are set.
