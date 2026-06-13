from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy import or_
import json

from .. import db
from ..audit import record_audit
from ..models import DatabaseConnection, SyncJob, utcnow
from ..notifications.service import notify_async
from ..security import encrypt_secret, roles_required
from ..sync.engine import connection_engine

bp = Blueprint("connections", __name__, url_prefix="/connections")


def _mapping_rule_summary(raw):
    raw = raw or ""
    if not raw.strip():
        return {"state": "none", "label": "Not configured", "details": []}
    try:
        rules = json.loads(raw)
    except json.JSONDecodeError:
        return {"state": "invalid", "label": "Invalid JSON", "details": []}
    if not isinstance(rules, dict) or not rules:
        return {"state": "empty", "label": "Empty rules", "details": []}
    details = []
    for table_name, table_rules in rules.items():
        if not isinstance(table_rules, dict):
            return {"state": "invalid", "label": "Invalid JSON", "details": []}
        for column_name, lookup in table_rules.items():
            details.append({"table": table_name, "column": column_name, "lookup": lookup})
    label = "{} table{}".format(len(rules), "" if len(rules) == 1 else "s")
    return {"state": "configured", "label": label, "details": details}


@bp.get("/")
@login_required
def index():
    items = db.session.scalars(db.select(DatabaseConnection).order_by(DatabaseConnection.name)).all()
    return render_template(
        "connections/index.html",
        connections=items,
        mapping_summaries={item.id: _mapping_rule_summary(item.fk_mapping_rules) for item in items},
    )


@bp.route("/new", methods=("GET", "POST"))
@login_required
@roles_required("administrator")
def create():
    if request.method == "POST":
        name = request.form["name"].strip()
        if db.session.scalar(db.select(DatabaseConnection).filter_by(name=name)):
            flash("Connection name already exists.", "danger")
            return render_template("connections/form.html", item=None, mapping_summary=_mapping_rule_summary(request.form.get("fk_mapping_rules", "")))
        fk_mapping_rules = request.form.get("fk_mapping_rules", "").strip()
        if fk_mapping_rules:
            try:
                json.loads(fk_mapping_rules)
            except json.JSONDecodeError:
                flash("FK mapping rules must be valid JSON.", "danger")
                return render_template("connections/form.html", item=None, mapping_summary=_mapping_rule_summary(fk_mapping_rules))
        item = DatabaseConnection(
            name=name,
            host=request.form["host"].strip(),
            port=int(request.form.get("port", 3306)),
            database_name=request.form["database_name"].strip(),
            username=request.form["username"].strip(),
            encrypted_password=encrypt_secret(request.form.get("password", "")),
            fk_mapping_rules=fk_mapping_rules or None,
        )
        db.session.add(item)
        db.session.commit()
        record_audit("connection.created", item.name)
        flash("Connection saved.", "success")
        return redirect(url_for("connections.index"))
    return render_template("connections/form.html", item=None, mapping_summary=_mapping_rule_summary(""))


@bp.route("/<int:connection_id>/edit", methods=("GET", "POST"))
@login_required
@roles_required("administrator")
def edit(connection_id):
    item = db.get_or_404(DatabaseConnection, connection_id)
    if request.method == "POST":
        name = request.form["name"].strip()
        duplicate = db.session.scalar(
            db.select(DatabaseConnection).where(
                DatabaseConnection.name == name,
                DatabaseConnection.id != item.id,
            )
        )
        if duplicate:
            flash("Connection name already exists.", "danger")
            return render_template(
                "connections/form.html",
                item=item,
                mapping_summary=_mapping_rule_summary(request.form.get("fk_mapping_rules", item.fk_mapping_rules or "")),
            )
        item.name = name
        item.host = request.form["host"].strip()
        item.port = int(request.form.get("port", 3306))
        item.database_name = request.form["database_name"].strip()
        item.username = request.form["username"].strip()
        fk_mapping_rules = request.form.get("fk_mapping_rules", "").strip()
        if fk_mapping_rules:
            try:
                json.loads(fk_mapping_rules)
            except json.JSONDecodeError:
                flash("FK mapping rules must be valid JSON.", "danger")
                return render_template(
                    "connections/form.html",
                    item=item,
                    mapping_summary=_mapping_rule_summary(fk_mapping_rules),
                )
            item.fk_mapping_rules = fk_mapping_rules
        else:
            item.fk_mapping_rules = None
        password = request.form.get("password", "")
        if password:
            item.encrypted_password = encrypt_secret(password)
        item.test_status = "untested"
        item.last_test_error = None
        db.session.commit()
        record_audit("connection.updated", item.name)
        flash("Connection updated.", "success")
        return redirect(url_for("connections.index"))
    return render_template("connections/form.html", item=item, mapping_summary=_mapping_rule_summary(item.fk_mapping_rules))


@bp.post("/<int:connection_id>/test")
@login_required
@roles_required("administrator")
def test(connection_id):
    item = db.get_or_404(DatabaseConnection, connection_id)
    try:
        with connection_engine(item).connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        item.test_status = "healthy"
        item.last_tested_at = utcnow()
        item.last_test_error = None
        db.session.commit()
        record_audit("connection.test_succeeded", item.name)
        flash("Connection successful.", "success")
    except Exception as exc:
        item.test_status = "failed"
        item.last_tested_at = utcnow()
        item.last_test_error = str(exc)
        db.session.commit()
        record_audit("connection.test_failed", item.name)
        notify_async(
            "connection_failure",
            "Database connection test FAILED\nConnection: {}\nEndpoint: {}:{}\nDatabase: {}\nError: {}".format(
                item.name,
                item.host,
                item.port,
                item.database_name,
                str(exc)[:500],
            ),
        )
        flash("Connection failed: {}".format(exc), "danger")
    return redirect(url_for("connections.index"))


@bp.post("/<int:connection_id>/toggle")
@login_required
@roles_required("administrator")
def toggle(connection_id):
    item = db.get_or_404(DatabaseConnection, connection_id)
    active_job = db.session.scalar(
        db.select(SyncJob).where(
            or_(SyncJob.source_connection_id == item.id, SyncJob.target_connection_id == item.id),
            SyncJob.status.in_(["pending", "running"]),
        )
    )
    if item.is_enabled and active_job:
        flash("Connection cannot be disabled while job #{} is active.".format(active_job.id), "danger")
    else:
        item.is_enabled = not item.is_enabled
        db.session.commit()
        state = "enabled" if item.is_enabled else "disabled"
        record_audit("connection.{}".format(state), item.name)
        flash("Connection {}.".format(state), "success")
    return redirect(url_for("connections.index"))


@bp.post("/<int:connection_id>/delete")
@login_required
@roles_required("administrator")
def delete(connection_id):
    item = db.get_or_404(DatabaseConnection, connection_id)
    referenced = db.session.scalar(
        db.select(SyncJob.id).where(
            or_(SyncJob.source_connection_id == item.id, SyncJob.target_connection_id == item.id)
        ).limit(1)
    )
    if referenced:
        flash("Connection is used by synchronization history and cannot be deleted. Disable it instead.", "danger")
    else:
        name = item.name
        db.session.delete(item)
        db.session.commit()
        record_audit("connection.deleted", name)
        flash("Connection deleted.", "success")
    return redirect(url_for("connections.index"))
