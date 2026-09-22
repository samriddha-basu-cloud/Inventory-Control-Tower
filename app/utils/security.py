"""CSRF, authentication-ready session user, RBAC decorators, and secure upload validation."""
from __future__ import annotations

import hmac
import json
import os
import secrets
import uuid
import zipfile
from functools import wraps

from flask import abort, current_app, g, jsonify, request, session
from werkzeug.utils import secure_filename

PERMISSIONS = ["view", "export", "ingest", "edit_master", "run_scenarios", "create_action", "approve", "execute",
               "alert_manage", "configure", "admin"]

ROLE_RANK = {"Inventory Planner": 1, "Demand Planner": 1, "Procurement": 1, "Warehouse Manager": 1,
             "Supply Chain Manager": 2, "Operations": 2, "Finance": 2, "Executive": 3, "Administrator": 4}

DEFAULT_ROLES = {
    "Executive": ["view", "export", "run_scenarios", "approve"],
    "Supply Chain Manager": ["view", "export", "ingest", "edit_master", "run_scenarios", "create_action", "approve",
                             "execute", "alert_manage"],
    "Demand Planner": ["view", "export", "ingest", "run_scenarios"],
    "Inventory Planner": ["view", "export", "run_scenarios", "create_action", "approve", "alert_manage"],
    "Procurement": ["view", "export", "create_action", "approve", "alert_manage"],
    "Warehouse Manager": ["view", "export", "ingest", "create_action", "alert_manage"],
    "Finance": ["view", "export", "approve"],
    "Operations": ["view", "export", "run_scenarios", "create_action", "approve", "alert_manage"],
    "Administrator": list(PERMISSIONS),
}


# ---- CSRF ------------------------------------------------------------------------------------------------------
def csrf_token() -> str:
    tok = session.get("_csrf")
    if not tok:
        tok = secrets.token_hex(16)
        session["_csrf"] = tok
    return tok


def check_csrf():
    """Protect every state-changing request. Machine clients may use X-API-Key instead of a browser session."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if not current_app.config.get("CSRF_ENABLED", True):
        return
    key = current_app.config.get("API_KEY")
    if key and hmac.compare_digest(request.headers.get("X-API-Key", ""), key):
        g.api_key_auth = True
        return
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    expected = session.get("_csrf", "")
    if not expected or not hmac.compare_digest(sent, expected):
        abort(400, description="Invalid or missing CSRF token. Reload the page and try again.")


# ---- users / RBAC ---------------------------------------------------------------------------------------------
def load_user():
    from ..models import Role, User
    role_name = session.get("role") or current_app.config["DEFAULT_ROLE"]
    username = session.get("username")
    if current_app.config.get("AUTH_REQUIRED") and not username:
        g.user = None
        return
    role = Role.query.filter_by(name=role_name).first()
    perms = list(role.permissions or []) if role else []
    if getattr(g, "api_key_auth", False):
        perms = list(PERMISSIONS)
        role_name, username = "Administrator", "api-client"
    g.user = {"username": username or role_name.lower().replace(" ", "."), "name": username or role_name,
              "role": role_name, "permissions": perms, "rank": ROLE_RANK.get(role_name, 1)}


def has_perm(perm: str) -> bool:
    u = getattr(g, "user", None)
    return bool(u and (perm in u["permissions"] or "admin" in u["permissions"]))


def require(perm: str):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not getattr(g, "user", None):
                if request.path.startswith("/api/"):
                    return jsonify({"error": "authentication required"}), 401
                abort(401)
            if not has_perm(perm):
                if request.path.startswith("/api/"):
                    return jsonify({"error": f"permission '{perm}' required for role {g.user['role']}"}), 403
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


# ---- secure upload ----------------------------------------------------------------------------------------------
class UploadError(ValueError):
    pass


def validate_upload(file_storage) -> tuple[str, bytes, str]:
    """Extension whitelist, size cap, magic-byte sniffing. Returns (safe_name, content, ext)."""
    if not file_storage or not file_storage.filename:
        raise UploadError("No file selected.")
    name = secure_filename(file_storage.filename)
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in current_app.config["ALLOWED_UPLOAD_EXT"]:
        raise UploadError(f"File type '.{ext}' is not allowed. Use CSV, XLSX or JSON.")
    limit = current_app.config["MAX_CONTENT_LENGTH"]
    data = file_storage.read(limit + 1)
    if len(data) > limit:
        raise UploadError(f"File exceeds the {current_app.config['UPLOAD_LIMIT_MB']} MB limit.")
    if not data:
        raise UploadError("The file is empty.")
    if ext == "xlsx":
        if not data.startswith(b"PK"):
            raise UploadError("File content does not look like an XLSX workbook.")
        import io
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = set(z.namelist())
                if "xl/workbook.xml" not in names:
                    raise UploadError("XLSX workbook structure is invalid.")
                if any(n.startswith("xl/vbaProject") for n in names):
                    raise UploadError("Macro-enabled content is not accepted.")
                if sum(i.file_size for i in z.infolist()) > 200 * 1024 * 1024:
                    raise UploadError("Workbook expands beyond the safe limit.")
        except zipfile.BadZipFile as e:
            raise UploadError("Corrupt XLSX file.") from e
    elif ext == "json":
        try:
            json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise UploadError(f"Invalid JSON: {e}") from e
    else:
        if b"\x00" in data[:4096]:
            raise UploadError("Binary content is not valid CSV.")
        try:
            data.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                data.decode("latin-1")
            except UnicodeDecodeError as e:
                raise UploadError("Cannot decode CSV text.") from e
    return name, data, ext


def store_upload(name: str, data: bytes) -> str:
    """Persist under a random name (never trust the client's file name for the path)."""
    folder = current_app.config["UPLOAD_FOLDER"]
    os.makedirs(folder, exist_ok=True)
    stored = f"{uuid.uuid4().hex}_{name}"
    with open(os.path.join(folder, stored), "wb") as fh:
        fh.write(data)
    return stored


def csv_safe(v):
    """Neutralise spreadsheet formula injection in exported cells."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def clean_text(s, maxlen: int = 255) -> str:
    """Strip control chars and cap length for free-text inputs (Jinja autoescape handles HTML)."""
    if s is None:
        return ""
    s = "".join(ch for ch in str(s) if ch.isprintable() or ch in "\n\t")
    return s.strip()[:maxlen]
