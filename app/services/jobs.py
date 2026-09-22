"""Background-job architecture with a synchronous fallback.

`submit()` records a Job row and either runs the callable immediately (JOBS_SYNC=1, the default: nothing to install) or
hands it to a small thread pool that re-enters the Flask app context. Swap the executor for Celery/RQ later - callers only
depend on submit()/get().
"""
from __future__ import annotations

import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor

from flask import current_app

from ..extensions import db
from ..models import Job
from ..models.base import utcnow

_pool: ThreadPoolExecutor | None = None


def _executor(n: int) -> ThreadPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=n, thread_name_prefix="ict-job")
    return _pool


def _run(app, job_id: str, fn, kwargs):
    with app.app_context():
        job = Job.query.filter_by(job_id=job_id).first()
        try:
            job.status = "RUNNING"
            db.session.commit()
            result = fn(**kwargs)
            job.result = result if isinstance(result, (dict, list)) else {"value": result}
            job.status = "DONE"
        except Exception as e:  # never leak stack traces to users; keep them in the log
            current_app.logger.error("job %s failed: %s", job_id, traceback.format_exc())
            db.session.rollback()
            job = Job.query.filter_by(job_id=job_id).first()
            job.status, job.error = "FAILED", str(e)[:390]
        job.finished_at = utcnow()
        db.session.commit()


def _run_inline(job: Job, fn, kwargs) -> Job:
    """Synchronous fallback: run in the caller's app context and session (nothing else to install or operate)."""
    jid = job.job_id
    try:
        job.status = "RUNNING"
        db.session.commit()
        result = fn(**kwargs)
        job = Job.query.filter_by(job_id=jid).first()
        job.result = result if isinstance(result, (dict, list)) else {"value": result}
        job.status = "DONE"
    except Exception as e:  # never leak stack traces to users; keep them in the log
        current_app.logger.error("job %s failed: %s", jid, traceback.format_exc())
        db.session.rollback()
        job = Job.query.filter_by(job_id=jid).first()
        job.status, job.error = "FAILED", str(e)[:390]
    job.finished_at = utcnow()
    db.session.commit()
    return job


def submit(kind: str, fn, **kwargs) -> Job:
    job = Job(job_id=uuid.uuid4().hex[:16], kind=kind, status="QUEUED", params={k: v for k, v in kwargs.items() if isinstance(v, (str, int, float, bool, list, dict, type(None)))})
    db.session.add(job)
    db.session.commit()
    app = current_app._get_current_object()
    if app.config.get("JOBS_SYNC", True):
        return _run_inline(job, fn, kwargs)
    _executor(app.config.get("JOB_WORKERS", 2)).submit(_run, app, job.job_id, fn, kwargs)
    return job


def get(job_id: str) -> Job | None:
    return Job.query.filter_by(job_id=job_id).first()
