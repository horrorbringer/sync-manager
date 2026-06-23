from flask import Blueprint, Response, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
import csv
import io
import json

from .. import db
from ..audit import record_audit
from ..models import DatabaseConnection, SyncJob, SyncProfile
from ..notifications.service import notify_async
from ..security import roles_required
from .engine import dependency_report, discover_tables, dry_run, expand_tables_with_dependencies, incremental_checkpoint_status, validate_table
from .tasks import enqueue_job, enqueue_jobs_in_order

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


def _postgresql_cycle_error(target):
    return (
        "Cyclic foreign-key synchronization is not supported for PostgreSQL targets. "
        "Use a MySQL target or resolve the cycle before synchronizing."
        if (getattr(target, "database_type", "mysql") or "mysql") == "postgresql"
        else None
    )


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


def _selected_table_filters():
    raw = request.form.get("table_filter_rules", "").strip()
    if not raw:
        return {}
    filter_error = None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("Table filters must be valid data.")
    if not isinstance(value, dict):
        raise ValueError("Table filters must be grouped by table.")
    filters = {}
    for table_name, rules in value.items():
        if not isinstance(table_name, str) or not isinstance(rules, list):
            raise ValueError("Invalid table filter configuration.")
        clean_rules = [
            {"column": str(rule.get("column", "")).strip(), "operator": str(rule.get("operator", "")).strip(), "value": str(rule.get("value", "")).strip(), "json_path": str(rule.get("json_path", "")).strip()}
            for rule in rules if isinstance(rule, dict) and str(rule.get("column", "")).strip()
        ]
        if clean_rules:
            filters[table_name] = clean_rules
    return filters


def _selected_incremental_columns():
    raw = request.form.get("table_incremental_columns", "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("Incremental settings must be valid data.")
    if not isinstance(value, dict) or not all(isinstance(table, str) and isinstance(column, str) for table, column in value.items()):
        raise ValueError("Invalid incremental table configuration.")
    return {table: column for table, column in value.items() if column}


def _selected_row_limit():
    raw = request.form.get("row_limit", "").strip()
    if not raw:
        return None
    try:
        row_limit = int(raw)
    except ValueError as exc:
        raise ValueError("Maximum rows must be a whole number.") from exc
    if not 1 <= row_limit <= 1_000_000:
        raise ValueError("Maximum rows must be between 1 and 1,000,000.")
    return row_limit


def _dry_run_with_filters(
    source,
    target,
    table_name,
    filter_rules,
    incremental_column=None,
    row_limit=None,
    planned_parent_values=None,
):
    if not filter_rules and not incremental_column and not row_limit and not planned_parent_values:
        return dry_run(source, target, table_name)
    return dry_run(
        source,
        target,
        table_name,
        filter_rules=filter_rules,
        incremental_column=incremental_column,
        row_limit=row_limit,
        planned_parent_values=planned_parent_values,
    )


def _remember_planned_parent_values(planned_parent_values, table_name, result):
    """Make exact, successfully previewed parent rows available to later FK checks."""
    if result.get("errors"):
        return
    values = result.get("_source_primary_key_values") or set()
    if values:
        planned_parent_values[table_name] = values


def _all_table_names(table_details):
    return [table["name"] for table in table_details or []]


def _expand_selected_tables(source, selected_tables):
    expanded = expand_tables_with_dependencies(source, selected_tables)
    auto_added = [table for table in expanded if table not in selected_tables]
    return expanded, auto_added


def _dependency_parent_tables(dependency_state):
    return {
        dependency
        for dependencies in (dependency_state or {}).get("dependencies", {}).values()
        for dependency in dependencies
    }


def _row_limit_for_table(table_name, selected_row_limit, parent_tables):
    """Keep parent rows complete so a limited child batch cannot break direct FKs."""
    return None if table_name in parent_tables else selected_row_limit


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


def _queue_jobs_in_order(jobs):
    """Persist a validated batch before dispatching it as one dependency-ordered run."""
    db.session.add_all(jobs)
    db.session.commit()
    job_ids = [job.id for job in jobs]
    try:
        if current_app.config["SYNC_EXECUTION_MODE"] == "inline":
            for job_id in job_ids:
                enqueue_job(job_id)
        else:
            enqueue_jobs_in_order(job_ids)
    except Exception as exc:
        for job_id in job_ids:
            job = db.session.get(SyncJob, job_id)
            if job.status == "pending":
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
    selected_table_filters=None,
    selected_incremental_columns=None,
):
    table_filters = selected_table_filters if selected_table_filters is not None else _selected_table_filters()
    incremental_columns = selected_incremental_columns if selected_incremental_columns is not None else _selected_incremental_columns()
    checkpoint_statuses = incremental_checkpoint_status(selected_source_id, selected_target_id, table_filters, incremental_columns) if selected_source_id and selected_target_id else {}
    profile_id = request.args.get("profile_id", request.form.get("profile_id"), type=int)
    active_profile = db.session.get(SyncProfile, profile_id) if profile_id else None
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
        selected_table_filters=table_filters,
        selected_incremental_columns=incremental_columns,
        checkpoint_statuses=checkpoint_statuses,
        profiles=db.session.scalars(db.select(SyncProfile).where(SyncProfile.created_by_id == current_user.id).order_by(SyncProfile.name)).all(),
        active_profile=active_profile if active_profile and active_profile.created_by_id == current_user.id else None,
    )


@bp.route("/checkpoint-status", methods=("POST",))
@login_required
@roles_required("administrator", "operator")
def checkpoint_status():
    payload = request.get_json(silent=True) or {}
    try:
        source_id = int(payload.get("source_id"))
        target_id = int(payload.get("target_id"))
    except (TypeError, ValueError):
        return jsonify(error="Choose valid source and target connections."), 400
    table_filters = payload.get("table_filters") or {}
    incremental_columns = payload.get("incremental_columns") or {}
    if not isinstance(table_filters, dict) or not isinstance(incremental_columns, dict):
        return jsonify(error="Invalid checkpoint configuration."), 400
    source = db.session.get(DatabaseConnection, source_id)
    target = db.session.get(DatabaseConnection, target_id)
    if source is None or target is None or source.id == target.id:
        return jsonify(error="Choose different source and target connections."), 400
    return jsonify(statuses=incremental_checkpoint_status(source.id, target.id, table_filters, incremental_columns))


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
    dependency_state = None
    filter_error = None
    try:
        selected_table_filters = _selected_table_filters()
        selected_incremental_columns = _selected_incremental_columns()
        selected_row_limit = _selected_row_limit()
        unknown_filter_tables = sorted(set(selected_table_filters) - set(selected_tables))
        unknown_incremental_tables = sorted(set(selected_incremental_columns) - set(selected_tables))
        if unknown_filter_tables or unknown_incremental_tables:
            raise ValueError("Filters were supplied for unselected tables: {}".format(", ".join(unknown_filter_tables)))
        sync_scope = request.form.get("sync_scope") or ("incremental" if selected_incremental_columns else "full")
        if sync_scope not in {"full", "incremental"}:
            raise ValueError("Invalid sync scope.")
        if sync_scope == "full":
            selected_incremental_columns = {}
        else:
            missing_incremental_columns = sorted(set(selected_tables) - set(selected_incremental_columns))
            if missing_incremental_columns:
                raise ValueError("Incremental sync requires a checkpoint column for every selected table: {}".format(", ".join(missing_incremental_columns)))
    except ValueError as exc:
        filter_error = str(exc)
        selected_table_filters = {}
        selected_incremental_columns = {}
        selected_row_limit = None
    if selected_sync_mode not in {"upsert", "insert_only"}:
        selected_sync_mode = "insert_only"
    if request.method == "GET" and request.args.get("profile_id", type=int):
        profile = db.get_or_404(SyncProfile, request.args.get("profile_id", type=int))
        if profile.created_by_id != current_user.id:
            return ("Not found", 404)
        selected_source_id, selected_target_id = profile.source_connection_id, profile.target_connection_id
        selected_tables, selected_sync_mode = profile.tables, profile.sync_mode
        selected_table_filters, selected_incremental_columns = profile.table_filters, profile.table_incremental_columns
        try:
            table_details = discover_tables(profile.source, profile.target)
            dependency_state = dependency_report(profile.source, selected_tables) if selected_tables else None
        except Exception as exc:
            flash("Profile loaded, but source tables could not be discovered: {}".format(exc), "warning")
    if request.method == "POST":
        source = db.get_or_404(DatabaseConnection, selected_source_id)
        target = db.get_or_404(DatabaseConnection, selected_target_id)
        action = request.form.get("action")
        if source.usage_role == "target" or target.usage_role == "source":
            flash("Connection roles do not allow this direction. Choose a source-capable source and target-capable target.", "danger")
            table_details = discover_tables(source, target)
            return _render_sync_form(connections, results, table_details, selected_source_id, selected_target_id, selected_tables, selected_sync_mode=selected_sync_mode, selected_table_filters=selected_table_filters, selected_incremental_columns=selected_incremental_columns)
        if action in {"execute", "sync_all"} and target.environment == "production" and request.form.get("target_confirmation", "").strip() != target.name:
            flash("Type the Production target connection name exactly before running synchronization.", "danger")
            table_details = discover_tables(source, target)
            return _render_sync_form(connections, results, table_details, selected_source_id, selected_target_id, selected_tables, selected_sync_mode=selected_sync_mode, selected_table_filters=selected_table_filters, selected_incremental_columns=selected_incremental_columns)
        if action == "save_profile":
            name = request.form.get("profile_name", "").strip()
            if not name or not selected_tables:
                flash("Provide a profile name and select at least one table.", "danger")
                table_details = discover_tables(source, target)
                return _render_sync_form(connections, results, table_details, selected_source_id, selected_target_id, selected_tables, selected_sync_mode=selected_sync_mode, selected_table_filters=selected_table_filters, selected_incremental_columns=selected_incremental_columns)
            profile_id = request.form.get("profile_id", type=int)
            profile = db.session.get(SyncProfile, profile_id) if profile_id else None
            if profile and profile.created_by_id != current_user.id:
                return ("Not found", 404)
            if profile is None:
                profile = SyncProfile(created_by=current_user)
                db.session.add(profile)
            profile.name, profile.source, profile.target = name, source, target
            profile.table_names, profile.filter_rules = json.dumps(selected_tables), json.dumps(selected_table_filters)
            profile.incremental_columns, profile.sync_mode = json.dumps(selected_incremental_columns), selected_sync_mode
            db.session.commit()
            record_audit("sync.profile_saved", "profile={} tables={}".format(profile.id, ",".join(selected_tables)))
            flash("Synchronization profile updated." if profile_id else "Synchronization profile saved.", "success")
            return redirect(url_for("sync.create", profile_id=profile.id))
        if filter_error:
            flash(filter_error, "danger")
            table_details = discover_tables(source, target)
            return _render_sync_form(connections, results, table_details, selected_source_id, selected_target_id, selected_tables, selected_sync_mode=selected_sync_mode)
        if selected_table_filters and action == "sync_all":
            flash("Bulk sync all tables does not use table filters. Select the tables you want to filter instead.", "danger")
            table_details = discover_tables(source, target)
            return _render_sync_form(connections, results, table_details, selected_source_id, selected_target_id, selected_tables, selected_sync_mode=selected_sync_mode, selected_table_filters=selected_table_filters)
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
                selected_tables, auto_added_tables = _expand_selected_tables(source, selected_tables) if not (selected_table_filters or selected_incremental_columns) else (selected_tables, [])
                dependency_state = dependency_report(source, selected_tables)
                cycle_tables = dependency_state["cycle_tables"]
                parent_tables = _dependency_parent_tables(dependency_state)
                skipped_empty_tables = []
                skipped_noop_tables = []
                planned_parent_values = {}
                for table_name in dependency_state["ordered_tables"]:
                    try:
                        table_preview = _dry_run_with_filters(
                            source,
                            target,
                            table_name,
                            selected_table_filters.get(table_name),
                            selected_incremental_columns.get(table_name),
                            _row_limit_for_table(table_name, selected_row_limit, parent_tables),
                            planned_parent_values,
                        )
                    except Exception as exc:
                        table_preview = {"errors": [str(exc)]}
                    if _is_empty_preview(table_preview):
                        skipped_empty_tables.append(table_name)
                        continue
                    _remember_planned_parent_values(planned_parent_values, table_name, table_preview)
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
                    if _postgresql_cycle_error(target):
                        errors.append(_postgresql_cycle_error(target))
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
                        filter_rules=json.dumps(selected_table_filters.get(table_name)) if selected_table_filters.get(table_name) else None,
                        incremental_column=selected_incremental_columns.get(table_name),
                        row_limit=_row_limit_for_table(table_name, selected_row_limit, parent_tables),
                        cycle_sync=False,
                        initiated_by=current_user,
                    )
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
                        filter_rules=json.dumps(selected_table_filters.get(table_name)) if selected_table_filters.get(table_name) else None,
                        incremental_column=selected_incremental_columns.get(table_name),
                        row_limit=_row_limit_for_table(table_name, selected_row_limit, parent_tables),
                        cycle_sync=True,
                        initiated_by=current_user,
                    )
                    queued_tables.append(job)
                if queued_tables:
                    if not _queue_jobs_in_order(queued_tables):
                        flash(queued_tables[0].error_message, "danger")
                        return redirect(url_for("main.dashboard"))
                    for job in queued_tables:
                        record_audit("sync.queued", "job={} table={}".format(job.id, job.table_name))
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
        elif action in {"dry_run", "export_report"}:
            table_details = discover_tables(source, target)
            if not selected_tables:
                flash("Select at least one source table before validation.", "danger")
            else:
                selected_tables, auto_added_tables = _expand_selected_tables(source, selected_tables) if not (selected_table_filters or selected_incremental_columns) else (selected_tables, [])
                dependency_state = dependency_report(source, selected_tables)
                cycle_tables = dependency_state["cycle_tables"]
                parent_tables = _dependency_parent_tables(dependency_state)
                skipped_empty_tables = []
                skipped_noop_tables = []
                planned_parent_values = {}
                for table_name in dependency_state["ordered_tables"]:
                    try:
                        table_result = _dry_run_with_filters(
                            source,
                            target,
                            table_name,
                            selected_table_filters.get(table_name),
                            selected_incremental_columns.get(table_name),
                            _row_limit_for_table(table_name, selected_row_limit, parent_tables),
                            planned_parent_values,
                        )
                    except Exception as exc:
                        table_result = {"errors": [str(exc)]}
                    if _is_empty_preview(table_result):
                        skipped_empty_tables.append(table_name)
                        continue
                    _remember_planned_parent_values(planned_parent_values, table_name, table_result)
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
                    if _postgresql_cycle_error(target):
                        errors.append(_postgresql_cycle_error(target))
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
                if action == "export_report":
                    output = io.StringIO()
                    writer = csv.writer(output)
                    writer.writerow(["table", "filters", "incremental_column", "checkpoint", "source_count", "target_count", "new_count", "existing_count", "errors"])
                    statuses = incremental_checkpoint_status(source.id, target.id, selected_table_filters, selected_incremental_columns)
                    for item in results:
                        table_name, result = item["table"], item["result"]
                        checkpoint = statuses.get(table_name) or {}
                        writer.writerow([table_name, json.dumps(selected_table_filters.get(table_name, [])), selected_incremental_columns.get(table_name, ""), checkpoint.get("cursor_value", ""), result.get("source_count", ""), result.get("target_count", ""), result.get("new_count", ""), result.get("existing_count", ""), "; ".join(result.get("errors", []))])
                    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=sync-validation-report.csv"})
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
            selected_tables, auto_added_tables = _expand_selected_tables(source, selected_tables) if not (selected_table_filters or selected_incremental_columns) else (selected_tables, [])
            dependency_state = dependency_report(source, selected_tables)
            cycle_tables = dependency_state["cycle_tables"]
            parent_tables = _dependency_parent_tables(dependency_state)
            skipped_empty_tables = []
            skipped_noop_tables = []
            planned_parent_values = {}
            for table_name in dependency_state["ordered_tables"]:
                try:
                    table_preview = _dry_run_with_filters(
                        source,
                        target,
                        table_name,
                        selected_table_filters.get(table_name),
                        selected_incremental_columns.get(table_name),
                        _row_limit_for_table(table_name, selected_row_limit, parent_tables),
                        planned_parent_values,
                    )
                except Exception as exc:
                    table_preview = {"errors": [str(exc)]}
                if _is_empty_preview(table_preview):
                    skipped_empty_tables.append(table_name)
                    continue
                _remember_planned_parent_values(planned_parent_values, table_name, table_preview)
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
                if _postgresql_cycle_error(target):
                    errors.append(_postgresql_cycle_error(target))
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
                    filter_rules=json.dumps(selected_table_filters.get(table_name)) if selected_table_filters.get(table_name) else None,
                    incremental_column=selected_incremental_columns.get(table_name),
                    row_limit=_row_limit_for_table(table_name, selected_row_limit, parent_tables),
                    cycle_sync=False,
                    initiated_by=current_user,
                )
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
                    filter_rules=json.dumps(selected_table_filters.get(table_name)) if selected_table_filters.get(table_name) else None,
                    incremental_column=selected_incremental_columns.get(table_name),
                    row_limit=_row_limit_for_table(table_name, selected_row_limit, parent_tables),
                    cycle_sync=True,
                    initiated_by=current_user,
                )
                queued_tables.append(job)
            if queued_tables:
                if not _queue_jobs_in_order(queued_tables):
                    flash(queued_tables[0].error_message, "danger")
                    return redirect(url_for("main.dashboard"))
                for job in queued_tables:
                    record_audit("sync.queued", "job={} table={}".format(job.id, job.table_name))
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


@bp.get("/profiles")
@login_required
@roles_required("administrator", "operator")
def profiles():
    items = db.session.scalars(db.select(SyncProfile).where(SyncProfile.created_by_id == current_user.id).order_by(SyncProfile.name)).all()
    return render_template("sync/profiles.html", profiles=items)


@bp.post("/profiles/<int:profile_id>/duplicate")
@login_required
@roles_required("administrator", "operator")
def duplicate_profile(profile_id):
    original = db.get_or_404(SyncProfile, profile_id)
    if original.created_by_id != current_user.id:
        return ("Not found", 404)
    copy = SyncProfile(name="{} (copy)".format(original.name), source=original.source, target=original.target, table_names=original.table_names, filter_rules=original.filter_rules, incremental_columns=original.incremental_columns, sync_mode=original.sync_mode, created_by=current_user)
    db.session.add(copy); db.session.commit()
    record_audit("sync.profile_duplicated", "profile={} copy={}".format(original.id, copy.id))
    flash("Profile duplicated.", "success")
    return redirect(url_for("sync.profiles"))


@bp.post("/profiles/<int:profile_id>/delete")
@login_required
@roles_required("administrator", "operator")
def delete_profile(profile_id):
    profile = db.get_or_404(SyncProfile, profile_id)
    if profile.created_by_id != current_user.id:
        return ("Not found", 404)
    name = profile.name
    db.session.delete(profile); db.session.commit()
    record_audit("sync.profile_deleted", "profile={} name={}".format(profile_id, name))
    flash("Profile deleted.", "success")
    return redirect(url_for("sync.profiles"))


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
        filter_rules=original.filter_rules,
        incremental_column=original.incremental_column,
        row_limit=original.row_limit,
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
