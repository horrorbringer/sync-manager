from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import db
from ..audit import record_audit
from ..models import DatabaseConnection, SyncJob
from ..notifications.service import notify_async
from ..security import roles_required
from .engine import dependency_report, discover_tables, dry_run, expand_tables_with_dependencies, validate_table
from .tasks import enqueue_job

bp = Blueprint("sync", __name__, url_prefix="/sync")


@bp.after_request
def _disable_sync_view_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def _format_dependency_order(table_names):
    return " → ".join(table_names) if table_names else "no safe order"


def _is_cycle_only_error(table_name, errors):
    if not errors:
        return False
    prefix = "Table '{}' is part of a cyclic foreign key dependency".format(table_name)
    return all(str(error).startswith(prefix) for error in errors)


def _is_empty_preview(result):
    return bool(result and result.get("empty"))


def _is_insert_only_noop(sync_mode, result):
    return sync_mode == "insert_only" and bool(result) and not result.get("errors") and not result.get("empty") and result.get("new_count", 0) == 0


def _selected_table_names():
    names = request.form.getlist("table_names")
    manual_name = request.form.get("table_name", "").strip()
    if manual_name:
        names.append(manual_name)
    return list(dict.fromkeys(name.strip() for name in names if name.strip()))


def _all_table_names(table_details):
    return [table["name"] for table in table_details or []]


def _expand_selected_tables(source, selected_tables):
    expanded = expand_tables_with_dependencies(source, selected_tables)
    auto_added = [table for table in expanded if table not in selected_tables]
    return expanded, auto_added


def _find_active_job(source_id, target_id, table_name):
    return db.session.scalar(
        db.select(SyncJob).where(
            SyncJob.source_connection_id == source_id,
            SyncJob.target_connection_id == target_id,
            SyncJob.table_name == table_name,
            SyncJob.status.in_(["pending", "running"]),
        )
    )


def _queue_job(job):
    db.session.add(job)
    db.session.commit()
    job_id = job.id
    try:
        enqueue_job(job_id)
    except Exception as exc:
        job = db.session.get(SyncJob, job_id)
        if job.status != "failed":
            job.status = "failed"
            job.error_message = "Unable to start synchronization: {}".format(exc)
        db.session.commit()
        return False
    return True


def _render_sync_form(
    connections,
    results,
    table_details,
    selected_source_id,
    selected_target_id,
    selected_tables,
    dependency_state=None,
    selected_sync_mode="insert_only",
):
    return render_template(
        "sync/form.html",
        connections=connections,
        results=results,
        table_details=table_details,
        selected_source_id=selected_source_id,
        selected_target_id=selected_target_id,
        selected_tables=selected_tables,
        dependency_state=dependency_state,
        selected_sync_mode=selected_sync_mode,
    )


@bp.route("/new", methods=("GET", "POST"))
@login_required
@roles_required("administrator", "operator")
def create():
    connections = db.session.scalars(
        db.select(DatabaseConnection).where(DatabaseConnection.is_enabled.is_(True)).order_by(DatabaseConnection.name)
    ).all()
    results = []
    table_details = []
    selected_source_id = request.form.get("source_id", type=int)
    selected_target_id = request.form.get("target_id", type=int)
    selected_tables = _selected_table_names()
    selected_sync_mode = request.form.get("sync_mode", "insert_only").strip().lower()
    if selected_sync_mode not in {"upsert", "insert_only"}:
        selected_sync_mode = "insert_only"
    dependency_state = None
    if request.method == "POST":
        source = db.get_or_404(DatabaseConnection, selected_source_id)
        target = db.get_or_404(DatabaseConnection, selected_target_id)
        action = request.form.get("action")
        if not action:
            flash("Select an action before submitting the synchronization form.", "warning")
            table_details = discover_tables(source, target)
            return _render_sync_form(
                connections,
                results,
                table_details,
                selected_source_id,
                selected_target_id,
                selected_tables,
                dependency_state=dependency_state,
                selected_sync_mode=selected_sync_mode,
            )
        if source.id == target.id:
            flash("Source and target must be different.", "danger")
        elif action == "discover":
            try:
                table_details = discover_tables(source, target)
                if not table_details:
                    flash("No tables were found in source database '{}'.".format(source.database_name), "warning")
            except Exception as exc:
                flash("Unable to discover source tables: {}".format(exc), "danger")
        elif action == "sync_all":
            try:
                table_details = discover_tables(source, target)
            except Exception as exc:
                flash("Unable to discover source tables: {}".format(exc), "danger")
                return _render_sync_form(
                    connections,
                    results,
                    table_details,
                    selected_source_id,
                    selected_target_id,
                    selected_tables,
                    dependency_state=dependency_state,
                    selected_sync_mode=selected_sync_mode,
                )
            selected_tables = _all_table_names(table_details)
            if not selected_tables:
                flash("No tables were found in source database '{}'.".format(source.database_name), "warning")
            else:
                selected_tables, auto_added_tables = _expand_selected_tables(source, selected_tables)
                dependency_state = dependency_report(source, selected_tables)
                cycle_tables = dependency_state["cycle_tables"]
                skipped_empty_tables = []
                skipped_noop_tables = []
                for table_name in dependency_state["ordered_tables"]:
                    try:
                        table_preview = dry_run(source, target, table_name)
                    except Exception as exc:
                        table_preview = {"errors": [str(exc)]}
                    if _is_empty_preview(table_preview):
                        skipped_empty_tables.append(table_name)
                        continue
                    if _is_insert_only_noop(selected_sync_mode, table_preview):
                        skipped_noop_tables.append(table_name)
                        continue
                    errors = list(table_preview.get("errors") or [])
                    for error in validate_table(source, target, table_name, selected_tables=selected_tables):
                        if error not in errors:
                            errors.append(error)
                    active_job = _find_active_job(source.id, target.id, table_name)
                    if active_job:
                        errors.append("Job #{} is already {} for this table".format(active_job.id, active_job.status))
                    if errors:
                        results.append({"table": table_name, "result": {"errors": errors}})
                for table_name in cycle_tables:
                    try:
                        table_preview = dry_run(source, target, table_name)
                    except Exception as exc:
                        table_preview = {"errors": [str(exc)]}
                    if _is_empty_preview(table_preview):
                        skipped_empty_tables.append(table_name)
                        continue
                    if _is_insert_only_noop(selected_sync_mode, table_preview):
                        skipped_noop_tables.append(table_name)
                        continue
                    errors = list(table_preview.get("errors") or [])
                    for error in validate_table(source, target, table_name, selected_tables=selected_tables):
                        if error not in errors:
                            errors.append(error)
                    if errors and not _is_cycle_only_error(table_name, errors):
                        results.append({"table": table_name, "result": {"errors": errors}})
                    elif errors:
                        results.append({"table": table_name, "result": {"errors": errors}})
                queued_tables = []
                ordered_tables = dependency_state["ordered_tables"]
                result_by_table = {item["table"]: item["result"] for item in results}
                for table_name in ordered_tables:
                    table_result = result_by_table.get(table_name, {})
                    if table_name in skipped_empty_tables or table_name in skipped_noop_tables:
                        continue
                    if table_result.get("errors"):
                        continue
                    if table_result.get("empty"):
                        continue
                    job = SyncJob(
                        source=source,
                        target=target,
                        table_name=table_name,
                        sync_mode=selected_sync_mode,
                        cycle_sync=False,
                        initiated_by=current_user,
                    )
                    if not _queue_job(job):
                        flash(job.error_message, "danger")
                        return redirect(url_for("main.dashboard"))
                    record_audit("sync.queued", "job={} table={}".format(job.id, table_name))
                    queued_tables.append(job)
                for table_name in cycle_tables:
                    table_result = result_by_table.get(table_name, {})
                    if table_name in skipped_empty_tables or table_name in skipped_noop_tables:
                        continue
                    if table_result.get("errors") and not _is_cycle_only_error(table_name, table_result["errors"]):
                        continue
                    if table_result.get("empty"):
                        continue
                    job = SyncJob(
                        source=source,
                        target=target,
                        table_name=table_name,
                        sync_mode=selected_sync_mode,
                        cycle_sync=True,
                        initiated_by=current_user,
                    )
                    if not _queue_job(job):
                        flash(job.error_message, "danger")
                        return redirect(url_for("main.dashboard"))
                    record_audit("sync.queued", "job={} table={}".format(job.id, table_name))
                    queued_tables.append(job)
                if queued_tables:
                    order_text = _format_dependency_order([job.table_name for job in queued_tables])
                    cycle_count = sum(1 for job in queued_tables if job.cycle_sync)
                    summary = "Queued {} table job{} in dependency order: {}. {} cyclic table{} were auto-synced with foreign-key checks disabled.".format(
                        len(queued_tables),
                        "" if len(queued_tables) == 1 else "s",
                        order_text,
                        cycle_count,
                        "" if cycle_count == 1 else "s",
                    )
                    if skipped_empty_tables:
                        summary += " {} empty table{} were skipped because the source had no rows.".format(
                            len(skipped_empty_tables),
                            "" if len(skipped_empty_tables) == 1 else "s",
                        )
                    if skipped_noop_tables:
                        summary += " {} table{} had no new records and were skipped in add-new-only mode.".format(
                            len(skipped_noop_tables),
                            "" if len(skipped_noop_tables) == 1 else "s",
                        )
                    if auto_added_tables:
                        summary += " {} parent table{} were added automatically: {}.".format(
                            len(auto_added_tables),
                            "" if len(auto_added_tables) == 1 else "s",
                            _format_dependency_order(auto_added_tables),
                        )
                    flash(summary, "warning" if cycle_count else "success")
                    return redirect(url_for("main.dashboard"))
                if skipped_empty_tables:
                    flash(
                        "{} empty table{} were skipped because the source had no rows.".format(
                            len(skipped_empty_tables),
                            "" if len(skipped_empty_tables) == 1 else "s",
                        ),
                        "info",
                    )
                if skipped_noop_tables:
                    flash(
                        "{} table{} had no new records and were skipped in add-new-only mode.".format(
                            len(skipped_noop_tables),
                            "" if len(skipped_noop_tables) == 1 else "s",
                        ),
                        "info",
                    )
                table_details = discover_tables(source, target)
                return _render_sync_form(
                    connections,
                    results,
                    table_details,
                    selected_source_id,
                    selected_target_id,
                    selected_tables,
                    dependency_state=dependency_state,
                    selected_sync_mode=selected_sync_mode,
                )
        elif action == "dry_run":
            table_details = discover_tables(source, target)
            if not selected_tables:
                flash("Select at least one source table before validation.", "danger")
            else:
                selected_tables, auto_added_tables = _expand_selected_tables(source, selected_tables)
                dependency_state = dependency_report(source, selected_tables)
                cycle_tables = dependency_state["cycle_tables"]
                skipped_empty_tables = []
                skipped_noop_tables = []
                for table_name in dependency_state["ordered_tables"]:
                    try:
                        table_result = dry_run(source, target, table_name)
                    except Exception as exc:
                        table_result = {"errors": [str(exc)]}
                    if _is_empty_preview(table_result):
                        skipped_empty_tables.append(table_name)
                        continue
                    if _is_insert_only_noop(selected_sync_mode, table_result):
                        skipped_noop_tables.append(table_name)
                        continue
                    results.append({"table": table_name, "result": table_result})
                for table_name in cycle_tables:
                    try:
                        table_result = dry_run(source, target, table_name)
                    except Exception as exc:
                        table_result = {"errors": [str(exc)]}
                    if _is_empty_preview(table_result):
                        skipped_empty_tables.append(table_name)
                        continue
                    if _is_insert_only_noop(selected_sync_mode, table_result):
                        skipped_noop_tables.append(table_name)
                        continue
                    errors = list(table_result.get("errors") or [])
                    for error in validate_table(source, target, table_name, selected_tables=selected_tables):
                        if error not in errors:
                            errors.append(error)
                    if errors and not _is_cycle_only_error(table_name, errors):
                        results.append({"table": table_name, "result": {"errors": errors}})
                    else:
                        results.append({"table": table_name, "result": table_result})
                if skipped_empty_tables:
                    flash(
                        "{} empty table{} were skipped because the source had no rows.".format(
                            len(skipped_empty_tables),
                            "" if len(skipped_empty_tables) == 1 else "s",
                        ),
                        "info",
                    )
                if skipped_noop_tables:
                    flash(
                        "{} table{} had no new records and were skipped in add-new-only mode.".format(
                            len(skipped_noop_tables),
                            "" if len(skipped_noop_tables) == 1 else "s",
                        ),
                        "info",
                    )
                if auto_added_tables:
                    flash(
                        "{} parent table{} were added automatically: {}.".format(
                            len(auto_added_tables),
                            "" if len(auto_added_tables) == 1 else "s",
                            _format_dependency_order(auto_added_tables),
                        ),
                        "info",
                    )
        elif action == "execute":
            if not selected_tables:
                flash("Select at least one source table before running synchronization.", "danger")
                table_details = discover_tables(source, target)
                return _render_sync_form(
                    connections,
                    results,
                    table_details,
                    selected_source_id,
                    selected_target_id,
                    selected_tables,
                    dependency_state=dependency_state,
                    selected_sync_mode=selected_sync_mode,
                )
            selected_tables, auto_added_tables = _expand_selected_tables(source, selected_tables)
            dependency_state = dependency_report(source, selected_tables)
            cycle_tables = dependency_state["cycle_tables"]
            skipped_empty_tables = []
            skipped_noop_tables = []
            for table_name in dependency_state["ordered_tables"]:
                try:
                    table_preview = dry_run(source, target, table_name)
                except Exception as exc:
                    table_preview = {"errors": [str(exc)]}
                if _is_empty_preview(table_preview):
                    skipped_empty_tables.append(table_name)
                    continue
                if _is_insert_only_noop(selected_sync_mode, table_preview):
                    skipped_noop_tables.append(table_name)
                    continue
                errors = list(table_preview.get("errors") or [])
                for error in validate_table(source, target, table_name, selected_tables=selected_tables):
                    if error not in errors:
                        errors.append(error)
                active_job = _find_active_job(source.id, target.id, table_name)
                if active_job:
                    errors.append("Job #{} is already {} for this table".format(active_job.id, active_job.status))
                if errors:
                    results.append({"table": table_name, "result": {"errors": errors}})
            for table_name in cycle_tables:
                try:
                    table_preview = dry_run(source, target, table_name)
                except Exception as exc:
                    table_preview = {"errors": [str(exc)]}
                if _is_empty_preview(table_preview):
                    skipped_empty_tables.append(table_name)
                    continue
                if _is_insert_only_noop(selected_sync_mode, table_preview):
                    skipped_noop_tables.append(table_name)
                    continue
                errors = list(table_preview.get("errors") or [])
                for error in validate_table(source, target, table_name, selected_tables=selected_tables):
                    if error not in errors:
                        errors.append(error)
                if errors and not _is_cycle_only_error(table_name, errors):
                    results.append({"table": table_name, "result": {"errors": errors}})
                elif errors:
                    results.append({"table": table_name, "result": {"errors": errors}})
            queued_tables = []
            ordered_tables = selected_tables if len(selected_tables) == 1 else dependency_state["ordered_tables"]
            result_by_table = {item["table"]: item["result"] for item in results}
            for table_name in ordered_tables:
                table_result = result_by_table.get(table_name, {})
                if table_name in skipped_empty_tables or table_name in skipped_noop_tables:
                    continue
                if table_result.get("errors"):
                    continue
                if table_result.get("empty"):
                    continue
                job = SyncJob(
                    source=source,
                    target=target,
                    table_name=table_name,
                    sync_mode=selected_sync_mode,
                    cycle_sync=False,
                    initiated_by=current_user,
                )
                if not _queue_job(job):
                    flash(job.error_message, "danger")
                    return redirect(url_for("main.dashboard"))
                record_audit("sync.queued", "job={} table={}".format(job.id, table_name))
                queued_tables.append(job)
            for table_name in cycle_tables:
                table_result = result_by_table.get(table_name, {})
                if table_name in skipped_empty_tables or table_name in skipped_noop_tables:
                    continue
                if table_result.get("errors") and not _is_cycle_only_error(table_name, table_result["errors"]):
                    continue
                if table_result.get("empty"):
                    continue
                job = SyncJob(
                    source=source,
                    target=target,
                    table_name=table_name,
                    sync_mode=selected_sync_mode,
                    cycle_sync=True,
                    initiated_by=current_user,
                )
                if not _queue_job(job):
                    flash(job.error_message, "danger")
                    return redirect(url_for("main.dashboard"))
                record_audit("sync.queued", "job={} table={}".format(job.id, table_name))
                queued_tables.append(job)
            if queued_tables:
                order_text = _format_dependency_order([job.table_name for job in queued_tables])
                cycle_count = sum(1 for job in queued_tables if job.cycle_sync)
                summary = "Queued {} table job{} in dependency order: {}. {} cyclic table{} were auto-synced with foreign-key checks disabled.".format(
                    len(queued_tables),
                    "" if len(queued_tables) == 1 else "s",
                    order_text,
                    cycle_count,
                    "" if cycle_count == 1 else "s",
                )
                if skipped_empty_tables:
                    summary += " {} empty table{} were skipped because the source had no rows.".format(
                        len(skipped_empty_tables),
                        "" if len(skipped_empty_tables) == 1 else "s",
                    )
                if skipped_noop_tables:
                    summary += " {} table{} had no new records and were skipped in add-new-only mode.".format(
                        len(skipped_noop_tables),
                        "" if len(skipped_noop_tables) == 1 else "s",
                    )
                if auto_added_tables:
                    summary += " {} parent table{} were added automatically: {}.".format(
                        len(auto_added_tables),
                        "" if len(auto_added_tables) == 1 else "s",
                        _format_dependency_order(auto_added_tables),
                    )
                summary += " You can leave this page; queued jobs continue in the background."
                flash(summary, "warning" if cycle_count else "success")
                if results:
                    table_details = discover_tables(source, target)
                    return _render_sync_form(
                        connections,
                        results,
                        table_details,
                        selected_source_id,
                        selected_target_id,
                        selected_tables,
                        dependency_state=dependency_state,
                        selected_sync_mode=selected_sync_mode,
                    )
                return redirect(url_for("main.dashboard"))

            if skipped_empty_tables:
                flash(
                    "{} empty table{} were skipped because the source had no rows.".format(
                        len(skipped_empty_tables),
                        "" if len(skipped_empty_tables) == 1 else "s",
                    ),
                    "info",
                )
            if skipped_noop_tables:
                flash(
                    "{} table{} had no new records and were skipped in add-new-only mode.".format(
                        len(skipped_noop_tables),
                        "" if len(skipped_noop_tables) == 1 else "s",
                    ),
                    "info",
                )
            table_details = discover_tables(source, target)
            return _render_sync_form(
                connections,
                results,
                table_details,
                selected_source_id,
                selected_target_id,
                selected_tables,
                dependency_state=dependency_state,
                selected_sync_mode=selected_sync_mode,
            )
    return _render_sync_form(
        connections,
        results,
        table_details,
        selected_source_id,
        selected_target_id,
        selected_tables,
        dependency_state=dependency_state,
        selected_sync_mode=selected_sync_mode,
    )


@bp.get("/jobs/<int:job_id>")
@login_required
def job_detail(job_id):
    job = db.get_or_404(SyncJob, job_id)
    return render_template("sync/job.html", job=job)


@bp.post("/jobs/<int:job_id>/cancel")
@login_required
@roles_required("administrator", "operator")
def cancel_job(job_id):
    job = db.get_or_404(SyncJob, job_id)
    if job.status != "pending":
        flash("Only queued jobs that have not started can be cancelled.", "warning")
        return redirect(url_for("sync.job_detail", job_id=job.id))
    job.status = "failed"
    job.error_message = "Cancelled before a background worker started the job."
    db.session.commit()
    record_audit("sync.cancelled", "job={}".format(job.id))
    flash("Queued synchronization job #{} was cancelled.".format(job.id), "success")
    return redirect(url_for("sync.job_detail", job_id=job.id))


@bp.post("/jobs/<int:job_id>/retry")
@login_required
@roles_required("administrator", "operator")
def retry_job(job_id):
    original = db.get_or_404(SyncJob, job_id)
    if original.status != "failed":
        flash("Only failed jobs can be retried.", "warning")
        return redirect(url_for("sync.job_detail", job_id=original.id))
    if not original.table_name:
        flash("This job has no table name and cannot be retried. Create a new synchronization job.", "danger")
        return redirect(url_for("sync.job_detail", job_id=original.id))

    errors = validate_table(original.source, original.target, original.table_name)
    if errors:
        flash("Retry validation failed: {}".format("; ".join(errors)), "danger")
        return redirect(url_for("sync.job_detail", job_id=original.id))

    active_job = _find_active_job(
        original.source_connection_id,
        original.target_connection_id,
        original.table_name,
    )
    if active_job:
        flash("Job #{} is already {} for this table.".format(active_job.id, active_job.status), "warning")
        return redirect(url_for("sync.job_detail", job_id=active_job.id))

    retry = SyncJob(
        source=original.source,
        target=original.target,
        table_name=original.table_name,
        sync_mode=original.sync_mode,
        cycle_sync=original.cycle_sync,
        initiated_by=current_user,
    )
    if not _queue_job(retry):
        flash(retry.error_message, "danger")
        return redirect(url_for("sync.job_detail", job_id=retry.id))
    record_audit("sync.retried", "original_job={} retry_job={}".format(original.id, retry.id))
    notify_async(
        "sync_retry",
        "Synchronization retry queued\nOriginal job: #{}\nRetry job: #{}\nTable: {}\nDirection: {} -> {}".format(
            original.id,
            retry.id,
            retry.table_name,
            retry.source.name,
            retry.target.name,
        ),
    )
    if retry.status == "completed":
        flash("Failed job #{} was retried and completed as job #{}.".format(original.id, retry.id), "success")
    else:
        flash("Failed job #{} was retried as job #{}.".format(original.id, retry.id), "success")
    return redirect(url_for("sync.job_detail", job_id=retry.id))
