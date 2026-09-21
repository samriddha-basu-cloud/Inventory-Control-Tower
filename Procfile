web: gunicorn "run:app" --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120
release: python -c "from run import app; from app.extensions import db; app.app_context().push(); db.create_all()"
