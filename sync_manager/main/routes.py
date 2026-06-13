from flask import Blueprint, render_template, request
from flask_login import login_required

from .. import db
from ..models import AuditLog, DatabaseConnection, SyncJob, User

bp = Blueprint("main", __name__)


def _paginate(select_query, page, per_page):
    total = db.session.scalar(db.select(db.func.count()).select_from(select_query.order_by(None).subquery()))
    total_pages = max(1, (total + per_page - 1) // per_page) if total else 0
    page = max(1, min(page, total_pages or 1))
    items = db.session.scalars(select_query.limit(per_page).offset((page - 1) * per_page)).all()
    return {
        "items": items,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_prev": page > 1 and total_pages > 0,
        "has_next": total_pages > 0 and page < total_pages,
        "prev_page": max(1, page - 1),
        "next_page": min(total_pages, page + 1) if total_pages else 1,
        "start_item": ((page - 1) * per_page + 1) if total else 0,
        "end_item": min(total, page * per_page),
    }


@bp.get("/")
@login_required
def dashboard():
    connections = db.session.scalar(db.select(db.func.count()).select_from(DatabaseConnection))
    status = request.args.get("status", "").strip()
    table_name = request.args.get("table", "").strip()
    source_id = request.args.get("source_id", type=int)
    target_id = request.args.get("target_id", type=int)
    query = db.select(SyncJob).order_by(SyncJob.id.desc())
    if status:
        query = query.where(SyncJob.status == status)
    if table_name:
        query = query.where(SyncJob.table_name.contains(table_name))
    if source_id:
        query = query.where(SyncJob.source_connection_id == source_id)
    if target_id:
        query = query.where(SyncJob.target_connection_id == target_id)
    pagination = _paginate(query, request.args.get("page", 1, type=int) or 1, 25)
    status_counts = dict(
        db.session.execute(
            db.select(SyncJob.status, db.func.count(SyncJob.id)).group_by(SyncJob.status)
        ).all()
    )
    endpoints = db.session.scalars(db.select(DatabaseConnection).order_by(DatabaseConnection.name)).all()
    records_processed = db.session.scalar(
        db.select(db.func.coalesce(db.func.sum(SyncJob.inserted_count + SyncJob.updated_count + SyncJob.skipped_count), 0))
    )
    return render_template(
        "main/dashboard.html",
        connection_count=connections,
        recent_jobs=pagination["items"],
        jobs_pagination=pagination,
        status_counts=status_counts,
        records_processed=records_processed,
        endpoints=endpoints,
        selected_status=status,
        selected_table=table_name,
        selected_source_id=source_id,
        selected_target_id=target_id,
    )


@bp.get("/audit")
@login_required
def audit_logs():
    action = request.args.get("action", "").strip()
    user_id = request.args.get("user_id", type=int)
    query = db.select(AuditLog).order_by(AuditLog.id.desc())
    if action:
        query = query.where(AuditLog.action.contains(action))
    if user_id:
        query = query.where(AuditLog.user_id == user_id)
    pagination = _paginate(query, request.args.get("page", 1, type=int) or 1, 25)
    users = db.session.scalars(db.select(User).order_by(User.username)).all()
    return render_template(
        "main/audit.html",
        logs=pagination["items"],
        logs_pagination=pagination,
        users=users,
        selected_action=action,
        selected_user_id=user_id,
    )
