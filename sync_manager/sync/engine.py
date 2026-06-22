from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
import re

from flask import current_app, has_app_context
from sqlalchemy import Date, DateTime, Integer, MetaData, Table, and_, create_engine, inspect, or_, select, text, tuple_
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON
from sqlalchemy.engine import URL

from .. import db
from ..models import SyncCheckpoint, SyncJob, utcnow
from ..security import decrypt_secret

_LOOKUP_COLUMN_PRIORITY = ("username", "email", "name", "title", "slug", "code", "key", "identifier")
_FILTER_OPERATORS = {"equals", "not_equals", "contains", "starts_with", "in", "between", "is_empty", "is_not_empty", "json_path_equals", "json_path_contains", "json_path_exists"}
_SENSITIVE_COLUMN_MARKERS = ("password", "secret", "token", "api_key", "access_key", "private_key")
_LIMITED_FULL_CHECKPOINT = "__limited_full_primary_key__"


def _preview_value(column_name, value):
    if any(marker in column_name.lower() for marker in _SENSITIVE_COLUMN_MARKERS):
        return "••••••"
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def _source_filter_clause(source_table, filter_rules, dialect_name=None):
    """Build safe SQLAlchemy predicates from persisted filter rules; never accept SQL text."""
    clauses = []
    for rule in filter_rules or []:
        if not isinstance(rule, dict):
            raise RuntimeError("Invalid filter rule")
        column_name = rule.get("column")
        operator = rule.get("operator")
        if column_name not in source_table.c or operator not in _FILTER_OPERATORS:
            raise RuntimeError("Invalid filter column or operator")
        column = source_table.c[column_name]
        value = rule.get("value", "")
        if operator.startswith("json_path_"):
            path = [part.strip() for part in str(rule.get("json_path", "")).split(".") if part.strip()]
            if dialect_name != "postgresql" or not isinstance(column.type, (JSON, JSONB)) or not path:
                raise RuntimeError("JSON path filters require a PostgreSQL JSON or JSONB column and a JSON path")
            expression = column[tuple(path)]
            if operator == "json_path_exists":
                clauses.append(expression.is_not(None))
            elif operator == "json_path_equals":
                clauses.append(expression.as_string() == value)
            else:
                try:
                    json_value = json.loads(value)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("JSON path contains filters require valid JSON, for example {\"status\": \"open\"}") from exc
                clauses.append(expression.contains(json_value))
            continue
        if operator == "equals": clauses.append(column == value)
        elif operator == "not_equals": clauses.append(column != value)
        elif operator == "contains": clauses.append(column.contains(value))
        elif operator == "starts_with": clauses.append(column.startswith(value))
        elif operator == "in": clauses.append(column.in_([item.strip() for item in str(value).split(",") if item.strip()]))
        elif operator == "between":
            values = [item.strip() for item in str(value).split(",", 1)]
            if len(values) != 2 or not all(values): raise RuntimeError("Between filters require two comma-separated values")
            clauses.append(column.between(values[0], values[1]))
        elif operator == "is_empty": clauses.append((column.is_(None)) | (column == ""))
        elif operator == "is_not_empty": clauses.append((column.is_not(None)) & (column != ""))
    return clauses


def _filter_signature(filter_rules):
    return hashlib.sha256(json.dumps(filter_rules or [], sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _cursor_value(column, value):
    if column.type._type_affinity is DateTime:
        return datetime.fromisoformat(value)
    if column.type._type_affinity is Date:
        return date.fromisoformat(value)
    if column.type._type_affinity is Integer:
        return int(value)
    return value


def _incremental_clause(source_table, source_config, target_config, table_name, filter_rules, incremental_column):
    if not incremental_column:
        return [], None
    if incremental_column not in source_table.c:
        raise RuntimeError("Incremental column '{}' does not exist on table '{}'".format(incremental_column, table_name))
    pk_names = [column.name for column in source_table.primary_key.columns]
    if len(pk_names) != 1:
        raise RuntimeError("Incremental sync requires a table with one primary-key column")
    column = source_table.c[incremental_column]
    if column.type._type_affinity not in {DateTime, Date, Integer}:
        raise RuntimeError("Incremental sync columns must be a date, datetime, or integer column")
    _cursor_value(column, "0" if column.type._type_affinity is Integer else "1970-01-01")
    if not has_app_context():
        return [], None
    checkpoint = db.session.scalar(db.select(SyncCheckpoint).where(
        SyncCheckpoint.source_connection_id == source_config.id,
        SyncCheckpoint.target_connection_id == target_config.id,
        SyncCheckpoint.table_name == table_name,
        SyncCheckpoint.filter_signature == _filter_signature(filter_rules),
        SyncCheckpoint.incremental_column == incremental_column,
    ))
    if not checkpoint:
        return [], None
    cursor = _cursor_value(column, checkpoint.cursor_value)
    pk_column = source_table.c[pk_names[0]]
    return [or_(column > cursor, and_(column == cursor, pk_column > _cursor_value(pk_column, checkpoint.cursor_primary_key)))], checkpoint


def _limited_full_clause(source_table, source_config, target_config, table_name, filter_rules, incremental_column, row_limit):
    """Resume a limited full sync by primary key without changing normal full-sync semantics."""
    if not row_limit or incremental_column:
        return [], None
    pk_names = [column.name for column in source_table.primary_key.columns]
    if len(pk_names) != 1:
        raise RuntimeError("A limited full sync requires a table with one primary-key column so it can continue safely.")
    if not has_app_context():
        return [], None
    checkpoint = db.session.scalar(db.select(SyncCheckpoint).where(
        SyncCheckpoint.source_connection_id == source_config.id,
        SyncCheckpoint.target_connection_id == target_config.id,
        SyncCheckpoint.table_name == table_name,
        SyncCheckpoint.filter_signature == _filter_signature(filter_rules),
        SyncCheckpoint.incremental_column == _LIMITED_FULL_CHECKPOINT,
    ))
    if not checkpoint:
        return [], None
    primary_key = source_table.c[pk_names[0]]
    return [primary_key > _cursor_value(primary_key, checkpoint.cursor_primary_key)], checkpoint


def _save_incremental_checkpoint(job, filter_rules, cursor_pair):
    if not job.incremental_column or not cursor_pair:
        return
    value, primary_key = cursor_pair
    signature = _filter_signature(filter_rules)
    checkpoint = db.session.scalar(db.select(SyncCheckpoint).where(
        SyncCheckpoint.source_connection_id == job.source_connection_id,
        SyncCheckpoint.target_connection_id == job.target_connection_id,
        SyncCheckpoint.table_name == job.table_name,
        SyncCheckpoint.filter_signature == signature,
        SyncCheckpoint.incremental_column == job.incremental_column,
    ))
    if checkpoint is None:
        checkpoint = SyncCheckpoint(source_connection_id=job.source_connection_id, target_connection_id=job.target_connection_id, table_name=job.table_name, filter_signature=signature, incremental_column=job.incremental_column, cursor_value=str(value), cursor_primary_key=str(primary_key))
        db.session.add(checkpoint)
    else:
        checkpoint.cursor_value, checkpoint.cursor_primary_key, checkpoint.updated_at = str(value), str(primary_key), utcnow()


def _save_limited_full_checkpoint(job, filter_rules, primary_key):
    if job.incremental_column or not job.row_limit or primary_key is None:
        return
    signature = _filter_signature(filter_rules)
    checkpoint = db.session.scalar(db.select(SyncCheckpoint).where(
        SyncCheckpoint.source_connection_id == job.source_connection_id,
        SyncCheckpoint.target_connection_id == job.target_connection_id,
        SyncCheckpoint.table_name == job.table_name,
        SyncCheckpoint.filter_signature == signature,
        SyncCheckpoint.incremental_column == _LIMITED_FULL_CHECKPOINT,
    ))
    if checkpoint is None:
        checkpoint = SyncCheckpoint(
            source_connection_id=job.source_connection_id,
            target_connection_id=job.target_connection_id,
            table_name=job.table_name,
            filter_signature=signature,
            incremental_column=_LIMITED_FULL_CHECKPOINT,
            cursor_value=str(primary_key),
            cursor_primary_key=str(primary_key),
        )
        db.session.add(checkpoint)
    else:
        checkpoint.cursor_value = str(primary_key)
        checkpoint.cursor_primary_key = str(primary_key)
        checkpoint.updated_at = utcnow()


def incremental_checkpoint_status(source_id, target_id, table_filters, incremental_columns):
    statuses = {}
    for table_name, column_name in (incremental_columns or {}).items():
        checkpoint = db.session.scalar(db.select(SyncCheckpoint).where(
            SyncCheckpoint.source_connection_id == source_id,
            SyncCheckpoint.target_connection_id == target_id,
            SyncCheckpoint.table_name == table_name,
            SyncCheckpoint.filter_signature == _filter_signature((table_filters or {}).get(table_name)),
            SyncCheckpoint.incremental_column == column_name,
        ))
        statuses[table_name] = {
            "column": column_name,
            "cursor_value": checkpoint.cursor_value if checkpoint else None,
            "cursor_primary_key": checkpoint.cursor_primary_key if checkpoint else None,
            "updated_at": checkpoint.updated_at.isoformat() if checkpoint and checkpoint.updated_at else None,
        }
    return statuses


def _load_mapping_rules(source_config):
    raw_rules = getattr(source_config, "fk_mapping_rules", None)
    if not raw_rules:
        return {}
    try:
        rules = json.loads(raw_rules)
    except Exception as exc:
        raise RuntimeError("Invalid foreign key mapping rules JSON on connection '{}'".format(source_config.name)) from exc
    if not isinstance(rules, dict):
        raise RuntimeError("Foreign key mapping rules on connection '{}' must be a JSON object".format(source_config.name))
    return rules


def mapping_health(source_config, target_config, table_name):
    source_inspector = inspect(connection_engine(source_config))
    target_inspector = inspect(connection_engine(target_config))
    mapping_rules = _load_mapping_rules(source_config)
    plan = _foreign_key_mapping_plan(source_inspector, target_inspector, table_name, mapping_rules=mapping_rules)
    if plan["errors"]:
        return {
            "state": "blocked",
            "mapped": [],
            "heuristic": [],
            "blocked": plan["errors"],
            "needs_review": [],
        }
    mapped = [item for item in plan["preview"] if item.get("explicit")]
    heuristic = [item for item in plan["preview"] if not item.get("explicit")]
    needs_review = []
    if heuristic:
        needs_review.append(
            "Heuristic mapping is being used for {}.".format(", ".join(item["column"] for item in heuristic))
        )
    state = "mapped" if mapped and not heuristic else "heuristic" if heuristic else "mapped"
    return {
        "state": state,
        "mapped": mapped,
        "heuristic": heuristic,
        "blocked": [],
        "needs_review": needs_review,
    }


def connection_engine(config):
    if not config.is_enabled:
        raise RuntimeError("Database connection '{}' is disabled".format(config.name))
    database_type = getattr(config, "database_type", "mysql") or "mysql"
    if database_type == "mysql":
        url = URL.create("mysql+pymysql", username=config.username, password=decrypt_secret(config.encrypted_password), host=config.host, port=config.port, database=config.database_name, query={"charset": "utf8mb4"})
        return create_engine(url, pool_pre_ping=True, pool_recycle=1800)
    if database_type == "postgresql":
        url = URL.create("postgresql+psycopg", username=config.username, password=decrypt_secret(config.encrypted_password), host=config.host, port=config.port, database=config.database_name)
        return create_engine(url, pool_pre_ping=True, pool_recycle=1800, connect_args={"options": "-csearch_path=public"})
    raise RuntimeError("Unsupported database type '{}' for connection '{}'".format(database_type, config.name))


def _target_write_statement(target_table, rows, sync_mode, pk_names, dialect_name):
    insert = postgresql_insert if dialect_name == "postgresql" else mysql_insert
    statement = insert(target_table).values(rows)
    if dialect_name == "postgresql":
        update_columns = {
            column.name: statement.excluded[column.name]
            for column in target_table.columns
            if column.name not in pk_names
        }
        if sync_mode == "insert_only" or not update_columns:
            return statement.on_conflict_do_nothing(index_elements=pk_names), bool(update_columns)
        return statement.on_conflict_do_update(index_elements=pk_names, set_=update_columns), True
    update_columns = {
        column.name: statement.inserted[column.name]
        for column in target_table.columns
        if column.name not in pk_names
    }
    if sync_mode == "insert_only" or not update_columns:
        return statement.prefix_with("IGNORE"), bool(update_columns)
    return statement.on_duplicate_key_update(**update_columns), True


def _advance_postgresql_sequence(target, target_table):
    pk_columns = list(target_table.primary_key.columns)
    if len(pk_columns) != 1 or pk_columns[0].type._type_affinity is not Integer:
        return
    pk_name = pk_columns[0].name
    sequence_name = target.execute(text("SELECT pg_get_serial_sequence(:table_name, :column_name)"), {"table_name": "public." + target_table.name, "column_name": pk_name}).scalar_one_or_none()
    if not sequence_name:
        return
    preparer = target.dialect.identifier_preparer
    max_value = target.execute(text("SELECT MAX({}) FROM {}".format(preparer.quote(pk_name), preparer.quote(target_table.name)))).scalar_one()
    target.execute(text("SELECT setval(CAST(:sequence_name AS regclass), :value, :is_called)"), {"sequence_name": sequence_name, "value": max_value or 1, "is_called": max_value is not None})


def discover_tables(source_config, target_config):
    source_inspector = inspect(connection_engine(source_config))
    target_inspector = inspect(connection_engine(target_config))
    mapping_rules = _load_mapping_rules(source_config)
    target_tables = set(target_inspector.get_table_names())
    details = []
    for table_name in sorted(source_inspector.get_table_names()):
        columns = source_inspector.get_columns(table_name)
        primary_key = source_inspector.get_pk_constraint(table_name).get("constrained_columns") or []
        foreign_keys = source_inspector.get_foreign_keys(table_name)
        mapping_report = _foreign_key_mapping_plan(source_inspector, target_inspector, table_name, mapping_rules=mapping_rules)
        dependencies = sorted(
            {
                fk.get("referred_table")
                for fk in foreign_keys
                if fk.get("referred_table") and fk.get("referred_table") != table_name
            }
        )
        mapping_columns = sorted(
            {
                fk.get("constrained_columns", [None])[0]
                for fk in foreign_keys
                if fk.get("referred_table")
                and fk.get("referred_table") != table_name
                and len(fk.get("constrained_columns") or []) == 1
                and _preferred_lookup_columns(source_inspector, fk.get("referred_table"))
            }
            - {None}
        )
        if mapping_report["errors"]:
            mapping_state = "blocked"
        elif mapping_report["preview"] and any(not item.get("explicit") for item in mapping_report["preview"]):
            mapping_state = "heuristic"
        elif mapping_report["preview"]:
            mapping_state = "mapped"
        else:
            mapping_state = "mapped"
        details.append(
            {
                "name": table_name,
                "column_count": len(columns),
                "filter_columns": [{"name": column["name"], "type": str(column.get("type", ""))} for column in columns],
                "primary_key": primary_key,
                "dependencies": dependencies,
                "mapping_columns": mapping_columns,
                "mapping_preview": mapping_report["preview"],
                "mapping_state": mapping_state,
                "mapping_errors": mapping_report["errors"],
                "target_exists": table_name in target_tables,
            }
        )
    return details


def _preferred_lookup_columns(inspector, table_name):
    columns = [column["name"] for column in inspector.get_columns(table_name)]
    primary_key = inspector.get_pk_constraint(table_name).get("constrained_columns") or []
    preferred = []
    for name in _LOOKUP_COLUMN_PRIORITY:
        if name in columns and name not in primary_key:
            preferred.append(name)
    try:
        unique_constraints = inspector.get_unique_constraints(table_name) or []
    except Exception:
        unique_constraints = []
    for constraint in unique_constraints:
        for name in constraint.get("column_names") or []:
            if name in columns and name not in primary_key and name not in preferred:
                preferred.append(name)
    return preferred


def _normalize_lookup_columns(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _foreign_key_mapping_plan(source_inspector, target_inspector, table_name, mapping_rules=None):
    preview = []
    errors = []
    table_rules = (mapping_rules or {}).get(table_name, {})
    if table_rules and not isinstance(table_rules, dict):
        errors.append(
            "Foreign key mapping rules for table '{}' must be a JSON object".format(table_name)
        )
        return {"preview": preview, "errors": errors}
    for fk in source_inspector.get_foreign_keys(table_name):
        constrained = fk.get("constrained_columns") or []
        referred_table = fk.get("referred_table")
        referred_columns = fk.get("referred_columns") or []
        if not referred_table or referred_table == table_name:
            continue
        if len(constrained) != 1 or len(referred_columns) != 1:
            errors.append(
                "Table '{}' has a composite foreign key to '{}' and composite mappings are not supported.".format(
                    table_name, referred_table
                )
            )
            continue
        explicit_lookup = _normalize_lookup_columns(table_rules.get(constrained[0])) if constrained else []
        preview.append(
            {
                "column": constrained[0],
                "referred_table": referred_table,
                "lookup_columns": explicit_lookup,
                "explicit": bool(explicit_lookup),
                "display": (
                    "{} -> {}.{} (direct copy)".format(
                        constrained[0],
                        referred_table,
                        referred_columns[0] if referred_columns else "id",
                    )
                    if not explicit_lookup
                    else "{} -> {}.{}".format(constrained[0], referred_table, ", ".join(explicit_lookup))
                ),
            }
        )
    return {"preview": preview, "errors": errors}


def _direct_foreign_key_preflight_errors(target_engine, mapping_plan, table_name, rows):
    """Report missing direct-copy parent rows before a target write can hit an FK violation."""
    errors = []
    for mapping in mapping_plan.get("preview", []):
        if mapping.get("explicit"):
            continue
        fk_column = mapping["column"]
        values = {row.get(fk_column) for row in rows if row.get(fk_column) not in (None, "")}
        if not values:
            continue
        parent_table = Table(mapping["referred_table"], MetaData(), autoload_with=target_engine)
        parent_pk_names = [column.name for column in parent_table.primary_key.columns]
        if len(parent_pk_names) != 1:
            continue
        parent_pk = parent_table.c[parent_pk_names[0]]
        with target_engine.connect() as target:
            present = set(target.execute(select(parent_pk).where(parent_pk.in_(values))).scalars())
        missing = sorted(values - present, key=str)
        if missing:
            shown = ", ".join(str(value) for value in missing[:5])
            suffix = "" if len(missing) <= 5 else " (and {} more)".format(len(missing) - 5)
            errors.append(
                "Table '{}' references missing target row{} in '{}' through '{}': {}{}. "
                "Sync the parent table first, or include those parent rows in its filter/limit.".format(
                    table_name,
                    "s" if len(missing) != 1 else "",
                    mapping["referred_table"],
                    fk_column,
                    shown,
                    suffix,
                )
            )
    return errors


def _build_row_cache(engine, table_name, columns):
    table = Table(table_name, MetaData(), autoload_with=engine)
    if not columns:
        return table, []
    query_columns = list(dict.fromkeys(list(columns) + [column.name for column in table.primary_key.columns]))
    with engine.connect() as connection:
        rows = [dict(row._mapping) for row in connection.execute(select(*(table.c[name] for name in query_columns)))]
    return table, rows


def _row_key(row, columns):
    return tuple(row.get(column) for column in columns)


def _candidate_scalar_values(value):
    candidates = []
    if value is None:
        return candidates
    candidates.append(value)
    if isinstance(value, str):
        text_value = value.strip()
        if text_value and text_value not in candidates:
            candidates.append(text_value)
        if re.fullmatch(r"[+-]?\d+", text_value):
            try:
                int_value = int(text_value)
                if int_value not in candidates:
                    candidates.append(int_value)
            except Exception:
                pass
            stripped = text_value.lstrip("0") or "0"
            if stripped not in candidates:
                candidates.append(stripped)
            if text_value.startswith("-") and text_value[1:].lstrip("0"):
                negative_stripped = "-" + (text_value[1:].lstrip("0") or "0")
                if negative_stripped not in candidates:
                    candidates.append(negative_stripped)
    elif isinstance(value, int):
        string_value = str(value)
        if string_value not in candidates:
            candidates.append(string_value)
    return candidates


def _fetch_source_row_by_pk(source_engine, table_name, pk_name, pk_value):
    table_obj = Table(table_name, MetaData(), autoload_with=source_engine)
    candidate_values = _candidate_scalar_values(pk_value)
    with source_engine.connect() as source_connection:
        for candidate in candidate_values:
            rows = [
                dict(item._mapping)
                for item in source_connection.execute(
                    select(table_obj).where(table_obj.c[pk_name].in_([candidate]))
                )
            ]
            if rows:
                return rows[0]
        rows = [dict(item._mapping) for item in source_connection.execute(select(table_obj))]
        normalized_candidates = {str(value).lstrip("0") or "0" for value in candidate_values if value is not None}
        for row in rows:
            row_value = row.get(pk_name)
            if row_value is None:
                continue
            row_values = {
                str(row_value).lstrip("0") or "0",
                str(row_value),
            }
            if row_values & normalized_candidates:
                return row
    return None


def _fetch_row_by_pk(engine, table_name, pk_name, pk_value):
    table_obj = Table(table_name, MetaData(), autoload_with=engine)
    candidate_values = _candidate_scalar_values(pk_value)
    with engine.connect() as connection:
        for candidate in candidate_values:
            rows = [
                dict(item._mapping)
                for item in connection.execute(
                    select(table_obj).where(table_obj.c[pk_name].in_([candidate]))
                )
            ]
            if rows:
                return rows[0]
        rows = [dict(item._mapping) for item in connection.execute(select(table_obj))]
        normalized_candidates = {str(value).lstrip("0") or "0" for value in candidate_values if value is not None}
        for row in rows:
            row_value = row.get(pk_name)
            if row_value is None:
                continue
            row_values = {
                str(row_value).lstrip("0") or "0",
                str(row_value),
            }
            if row_values & normalized_candidates:
                return row
    return None


def _row_identifier(row, pk_names):
    if not pk_names:
        return {}
    return {name: row.get(name) for name in pk_names}


def _append_drop_detail(job, table_name, reason, row=None, count=1):
    details = job.dropped_rows
    if row is not None and len(details) >= 100:
        return
    entry = {"table": table_name, "reason": reason}
    if row is not None:
        entry["row"] = row
    if count != 1:
        entry["count"] = count
    details.append(entry)
    job.drop_details = json.dumps(details, ensure_ascii=False, default=str)


def _collect_mysql_warnings(connection):
    try:
        rows = connection.execute(text("SHOW WARNINGS")).fetchall()
    except Exception:
        return []
    messages = []
    for row in rows:
        mapping = getattr(row, "_mapping", None)
        if mapping and "Message" in mapping:
            messages.append(mapping["Message"])
        elif len(row) >= 3:
            messages.append(row[2])
        else:
            messages.append(str(row))
    return messages


def _remap_foreign_key_values(
    source_engine,
    target_engine,
    table_name,
    rows,
    mapping_plan=None,
    allow_row_skips=False,
    cycle_mode=False,
):
    source_inspector = inspect(source_engine)
    target_inspector = inspect(target_engine)
    source_table = Table(table_name, MetaData(), autoload_with=source_engine)
    source_pk = [column.name for column in source_table.primary_key.columns]
    if len(source_pk) != 1:
        return (rows, []) if allow_row_skips else rows

    if mapping_plan is None:
        mapping_plan = _foreign_key_mapping_plan(source_inspector, target_inspector, table_name)
    if mapping_plan["errors"]:
        raise RuntimeError("; ".join(mapping_plan["errors"]))

    source_by_pk = {}
    target_fk_cache = {}
    transformed = []
    skipped = []
    for row in rows:
        remapped = dict(row)
        skip_reason = None
        for mapping in mapping_plan["preview"]:
            fk_column = mapping["column"]
            if fk_column not in remapped:
                continue
            fk_value = remapped[fk_column]
            if fk_value in (None, ""):
                continue
            if not mapping.get("explicit"):
                remapped[fk_column] = fk_value
                continue
            referred_table = mapping["referred_table"]
            lookup_columns = mapping["lookup_columns"]
            if referred_table not in source_by_pk:
                referred_table_obj = Table(referred_table, MetaData(), autoload_with=source_engine)
                referred_pk_names = [column.name for column in referred_table_obj.primary_key.columns]
                if len(referred_pk_names) != 1:
                    raise RuntimeError(
                        "Table '{}' cannot remap foreign key '{}': parent table '{}' does not have a single-column primary key.".format(
                            table_name,
                            fk_column,
                            referred_table,
                        )
                    )
                referred_pk = referred_pk_names[0]
                source_row = _fetch_source_row_by_pk(source_engine, referred_table, referred_pk, fk_value)
                source_by_pk[referred_table] = {source_row[referred_pk]: source_row} if source_row else {}
            source_ref_row = source_by_pk[referred_table].get(fk_value)
            if not source_ref_row:
                normalized_source_ref_row = None
                normalized_fk_value = str(fk_value).lstrip("0") or "0"
                for cached_pk, cached_row in source_by_pk[referred_table].items():
                    cached_normalized = str(cached_pk).lstrip("0") or "0"
                    if cached_normalized == normalized_fk_value:
                        normalized_source_ref_row = cached_row
                        break
                source_ref_row = normalized_source_ref_row
            if not source_ref_row:
                skip_reason = (
                    "Table '{}' cannot remap foreign key '{}': source row '{}' was not found in '{}' on source connection '{}' ({}). Checked normalized key variants {}.".format(
                        table_name,
                        fk_column,
                        fk_value,
                        referred_table,
                        getattr(getattr(source_engine, "url", None), "database", None) or "<unknown>",
                        getattr(getattr(source_engine, "url", None), "host", None) or source_engine,
                        ", ".join(repr(value) for value in _candidate_scalar_values(fk_value)) or "none",
                    )
                )
                break
            target_table_obj = Table(referred_table, MetaData(), autoload_with=target_engine)
            target_pk_name = [column.name for column in target_table_obj.primary_key.columns]
            if len(target_pk_name) != 1:
                skip_reason = (
                    "Table '{}' cannot remap foreign key '{}': target table '{}' does not have a single-column primary key.".format(
                        table_name,
                        fk_column,
                        referred_table,
                    )
                )
                break
            target_row_by_pk = _fetch_row_by_pk(target_engine, referred_table, target_pk_name[0], fk_value)
            if target_row_by_pk is not None:
                remapped[fk_column] = target_row_by_pk[target_pk_name[0]]
                continue
            lookup_key = _row_key(source_ref_row, lookup_columns)
            if referred_table not in target_fk_cache:
                with target_engine.connect() as target_connection:
                    target_rows = [
                        dict(item._mapping)
                        for item in target_connection.execute(
                            select(*(target_table_obj.c[name] for name in list(dict.fromkeys(list(lookup_columns) + target_pk_name))))
                        )
                    ]
                target_fk_cache[referred_table] = {
                    _row_key(item, lookup_columns): item[target_pk_name[0]] for item in target_rows
                }
            target_value = target_fk_cache[referred_table].get(lookup_key)
            if target_value is None:
                for cached_lookup_key, cached_target_value in target_fk_cache[referred_table].items():
                    if tuple(str(part) for part in cached_lookup_key) == tuple(str(part) for part in lookup_key):
                        target_value = cached_target_value
                        break
            if target_value is None:
                if cycle_mode:
                    remapped[fk_column] = fk_value
                    continue
                skip_reason = "Table '{}' cannot remap foreign key '{}': no matching '{}' row in target for lookup columns {}.".format(
                    table_name,
                    fk_column,
                    referred_table,
                    ", ".join(lookup_columns),
                )
                break
            remapped[fk_column] = target_value
        if skip_reason:
            if allow_row_skips:
                skipped.append({"row": row, "reason": skip_reason})
                continue
            raise RuntimeError(skip_reason)
        transformed.append(remapped)
    return (transformed, skipped) if allow_row_skips else transformed


def _dependency_analysis(source_config, table_names):
    source_inspector = inspect(connection_engine(source_config))
    requested = list(dict.fromkeys(table_names))
    requested_set = set(requested)
    graph = {
        table_name: sorted(
            {
                fk.get("referred_table")
                for fk in source_inspector.get_foreign_keys(table_name)
                if fk.get("referred_table") in requested_set and fk.get("referred_table") != table_name
            }
        )
        for table_name in requested
    }
    reverse_graph = {table_name: set() for table_name in requested}
    indegree = {table_name: 0 for table_name in requested}
    for table_name, dependencies in graph.items():
        indegree[table_name] = len(dependencies)
        for dependency in dependencies:
            reverse_graph.setdefault(dependency, set()).add(table_name)

    ordered = []
    ready = [table_name for table_name in requested if indegree[table_name] == 0]
    seen = set()
    while ready:
        table_name = ready.pop(0)
        if table_name in seen:
            continue
        seen.add(table_name)
        ordered.append(table_name)
        for dependent in reverse_graph.get(table_name, set()):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)

    unresolved = [table_name for table_name in requested if table_name not in seen]
    return {
        "ordered_tables": ordered,
        "cycle_tables": unresolved,
        "dependencies": graph,
    }


def validate_table(source_config, target_config, table_name, selected_tables=None):
    if not table_name:
        return ["Select a source table before running synchronization"]
    source_inspector = inspect(connection_engine(source_config))
    target_inspector = inspect(connection_engine(target_config))
    errors = []
    source_tables = source_inspector.get_table_names()
    target_tables = target_inspector.get_table_names()
    if table_name not in source_tables:
        return [
            "Table '{}' does not exist in source database '{}'. Available tables: {}".format(
                table_name,
                source_config.database_name,
                ", ".join(source_tables) or "none",
            )
        ]
    if table_name not in target_tables:
        return [
            "Table '{}' does not exist in target database '{}'. Create a compatible target table first.".format(
                table_name,
                target_config.database_name,
            )
        ]

    source_columns = {c["name"]: c for c in source_inspector.get_columns(table_name)}
    target_columns = {c["name"]: c for c in target_inspector.get_columns(table_name)}
    source_pk = source_inspector.get_pk_constraint(table_name).get("constrained_columns") or []
    target_pk = target_inspector.get_pk_constraint(table_name).get("constrained_columns") or []
    if not source_pk or source_pk != target_pk:
        errors.append("Source and target must have the same primary key")
    missing = sorted(set(source_columns) - set(target_columns))
    if missing:
        errors.append("Target is missing columns: {}".format(", ".join(missing)))
    for name in set(source_columns) & set(target_columns):
        source_type = source_columns[name].get("type")
        target_type = target_columns[name].get("type")
        if source_type is None or target_type is None:
            continue
        if source_type._type_affinity != target_type._type_affinity:
            errors.append("Incompatible type for column {}".format(name))
    try:
        mapping_rules = _load_mapping_rules(source_config)
        mapping_plan = _foreign_key_mapping_plan(source_inspector, target_inspector, table_name, mapping_rules=mapping_rules)
        errors.extend(mapping_plan["errors"])
    except RuntimeError as exc:
        errors.append(str(exc))
    if selected_tables is not None:
        selected = set(selected_tables)
        missing_dependencies = sorted(
            dep
            for dep in {
                fk.get("referred_table")
                for fk in source_inspector.get_foreign_keys(table_name)
                if fk.get("referred_table") and fk.get("referred_table") != table_name
            }
            if dep not in selected
        )
        if missing_dependencies:
            errors.append(
                "Table '{}' depends on {}. Select those tables in the same batch or sync them first.".format(
                    table_name,
                    ", ".join(missing_dependencies),
                )
            )
    return errors


def order_tables_by_dependency(source_config, table_names):
    analysis = _dependency_analysis(source_config, table_names)
    return analysis["ordered_tables"] + analysis["cycle_tables"]


def dependency_cycle_tables(source_config, table_names):
    analysis = _dependency_analysis(source_config, table_names)
    return analysis["cycle_tables"]


def dependency_report(source_config, table_names):
    analysis = _dependency_analysis(source_config, table_names)
    return {
        "ordered_tables": analysis["ordered_tables"],
        "cycle_tables": analysis["cycle_tables"],
        "dependencies": analysis["dependencies"],
    }


def expand_tables_with_dependencies(source_config, table_names):
    source_inspector = inspect(connection_engine(source_config))
    requested = list(dict.fromkeys(table_names))
    available = set(source_inspector.get_table_names())
    graph = {
        table_name: sorted(
            {
                fk.get("referred_table")
                for fk in source_inspector.get_foreign_keys(table_name)
                if fk.get("referred_table") in available and fk.get("referred_table") != table_name
            }
        )
        for table_name in available
    }
    expanded = []
    seen = set()
    visiting = set()

    def visit(table_name):
        if table_name in seen or table_name in visiting:
            return
        if table_name not in available:
            return
        visiting.add(table_name)
        for dependency in graph.get(table_name, []):
            visit(dependency)
        visiting.remove(table_name)
        seen.add(table_name)
        expanded.append(table_name)

    for table_name in requested:
        visit(table_name)
    return expanded


def dry_run(source_config, target_config, table_name, filter_rules=None, incremental_column=None, row_limit=None):
    source_engine = connection_engine(source_config)
    target_engine = connection_engine(target_config)
    metadata = MetaData()
    source_table = Table(table_name, metadata, autoload_with=source_engine)
    target_table = Table(table_name, MetaData(), autoload_with=target_engine)
    pk_names = [column.name for column in source_table.primary_key.columns]
    try:
        filter_clauses = _source_filter_clause(source_table, filter_rules, source_engine.dialect.name)
        incremental_clauses, _ = _incremental_clause(source_table, source_config, target_config, table_name, filter_rules, incremental_column)
        limited_full_clauses, _ = _limited_full_clause(source_table, source_config, target_config, table_name, filter_rules, incremental_column, row_limit)
    except RuntimeError as exc:
        return {"errors": [str(exc)], "source_count": 0, "target_count": 0}
    with source_engine.connect() as source, target_engine.connect() as target:
        order_columns = ([source_table.c[incremental_column]] if incremental_column else []) + [source_table.c[name] for name in pk_names]
        source_query = select(source_table).where(*(filter_clauses + incremental_clauses + limited_full_clauses)).order_by(*order_columns)
        source_rows = [dict(row._mapping) for row in source.execute(source_query.limit(row_limit) if row_limit else source_query)]
        source_count = len(source_rows)
        target_count = target.execute(select(db.func.count()).select_from(target_table)).scalar_one()
        if source_count == 0:
            return {
                "errors": [],
                "source_count": source_count,
                "target_count": target_count,
                "new_count": 0,
                "existing_count": 0,
                "empty": True,
            }
        source_keys = {tuple(row[name] for name in pk_names) for row in source_rows}
        target_keys = set(target.execute(select(*(target_table.c[name] for name in pk_names))).all())
        preview_rows = [
            {name: _preview_value(name, value) for name, value in row.items()}
            for row in source_rows[:10]
        ]
    errors = validate_table(source_config, target_config, table_name)
    try:
        mapping_plan = _foreign_key_mapping_plan(
            inspect(source_engine),
            inspect(target_engine),
            table_name,
            mapping_rules=_load_mapping_rules(source_config),
        )
        errors.extend(_direct_foreign_key_preflight_errors(target_engine, mapping_plan, table_name, source_rows))
    except Exception as exc:
        errors.append("Unable to preflight foreign-key references: {}".format(exc))
    if errors:
        return {"errors": errors, "source_count": source_count, "target_count": target_count, "preview_rows": preview_rows}
    return {
        "errors": [],
        "source_count": source_count,
        "target_count": target_count,
        "new_count": len(source_keys - target_keys),
        "existing_count": len(source_keys & target_keys),
        "preview_rows": preview_rows,
    }


@contextmanager
def job_lock(source_id, target_id, table_name):
    active = db.session.scalar(
        db.select(SyncJob).where(
            SyncJob.source_connection_id == source_id,
            SyncJob.target_connection_id == target_id,
            SyncJob.table_name == table_name,
            SyncJob.status == "running",
        )
    )
    if active:
        raise RuntimeError("A synchronization job is already running for this table")
    yield


def synchronize(job, batch_size=500):
    errors = validate_table(job.source, job.target, job.table_name)
    if errors:
        raise RuntimeError("; ".join(errors))
    sync_mode = (job.sync_mode or "insert_only").strip().lower()
    cycle_sync = bool(job.cycle_sync)
    if cycle_sync and (getattr(job.target, "database_type", "mysql") or "mysql") == "postgresql":
        raise RuntimeError("Cyclic foreign-key synchronization is not supported for PostgreSQL targets.")
    with job_lock(job.source_connection_id, job.target_connection_id, job.table_name):
        job.status = "running"
        job.started_at = datetime.now(timezone.utc)
        db.session.commit()
        source_engine = connection_engine(job.source)
        target_engine = connection_engine(job.target)
        source_table = Table(job.table_name, MetaData(), autoload_with=source_engine)
        target_table = Table(job.table_name, MetaData(), autoload_with=target_engine)
        mapping_plan = _foreign_key_mapping_plan(
            inspect(source_engine),
            inspect(target_engine),
            job.table_name,
            mapping_rules=_load_mapping_rules(job.source),
        )
        pk_names = [column.name for column in source_table.primary_key.columns]
        filter_clauses = _source_filter_clause(source_table, job.filters, source_engine.dialect.name)
        row_limit = getattr(job, "row_limit", None)
        if row_limit is not None and row_limit < 1:
            raise RuntimeError("Maximum rows must be a positive whole number.")
        incremental_clauses, _ = _incremental_clause(source_table, job.source, job.target, job.table_name, job.filters, job.incremental_column)
        limited_full_clauses, _ = _limited_full_clause(source_table, job.source, job.target, job.table_name, job.filters, job.incremental_column, row_limit)
        with source_engine.connect() as source:
            matching_count = source.execute(select(db.func.count()).select_from(source_table).where(*(filter_clauses + incremental_clauses + limited_full_clauses))).scalar_one()
        job.source_count = min(matching_count, row_limit) if row_limit else matching_count
        db.session.commit()
        offset = 0
        processed_source_rows = 0
        wrote_target_rows = False
        max_incremental_cursor = None
        max_limited_full_primary_key = None
        try:
            while True:
                remaining = row_limit - processed_source_rows if row_limit else batch_size
                if row_limit and remaining <= 0:
                    break
                order_columns = ([source_table.c[job.incremental_column]] if job.incremental_column else []) + [source_table.c[name] for name in pk_names]
                query = select(source_table).where(*(filter_clauses + incremental_clauses + limited_full_clauses)).order_by(*order_columns).offset(offset).limit(min(batch_size, remaining))
                with source_engine.connect() as source:
                    source_rows = [dict(row._mapping) for row in source.execute(query)]
                if not source_rows:
                    break
                processed_source_rows += len(source_rows)
                if job.incremental_column:
                    candidate = max((row[job.incremental_column], row[pk_names[0]]) for row in source_rows)
                    max_incremental_cursor = candidate if max_incremental_cursor is None or candidate > max_incremental_cursor else max_incremental_cursor
                elif row_limit:
                    max_limited_full_primary_key = source_rows[-1][pk_names[0]]
                rows, skipped_rows = _remap_foreign_key_values(
                    source_engine,
                    target_engine,
                    job.table_name,
                    source_rows,
                    mapping_plan=mapping_plan,
                    allow_row_skips=True,
                    cycle_mode=cycle_sync,
                )
                if skipped_rows:
                    job.failed_count += len(skipped_rows)
                    for skipped in skipped_rows:
                        _append_drop_detail(
                            job,
                            job.table_name,
                            skipped["reason"],
                            row=_row_identifier(skipped["row"], pk_names),
                        )
                        current_app.logger.warning(
                            "Synchronization job %s dropped row %s from table %s: %s",
                            job.id,
                            skipped["row"].get(pk_names[0]) if pk_names else "<unknown>",
                            job.table_name,
                            skipped["reason"],
                        )
                    db.session.commit()
                if not rows:
                    offset += len(source_rows)
                    continue
                keys = [tuple(row[name] for name in pk_names) for row in rows]
                if len(pk_names) == 1:
                    key_filter = target_table.c[pk_names[0]].in_([key[0] for key in keys])
                else:
                    key_filter = tuple_(*(target_table.c[name] for name in pk_names)).in_(keys)
                with target_engine.connect() as target:
                    existing_keys = set(
                        target.execute(select(*(target_table.c[name] for name in pk_names)).where(key_filter)).all()
                    )
                target_dialect = target_engine.dialect.name
                mysql_warnings = []
                statement, has_update_columns = _target_write_statement(
                    target_table, rows, sync_mode, pk_names, target_dialect
                )
                if sync_mode == "insert_only":
                    updated_count = 0
                    skipped_count = len(existing_keys)
                else:
                    updated_count = len(existing_keys) if has_update_columns else 0
                    skipped_count = len(existing_keys) if not has_update_columns else 0
                with target_engine.begin() as target:
                    if cycle_sync and target.dialect.name == "mysql":
                        target.execute(text("SET FOREIGN_KEY_CHECKS=0"))
                    try:
                        result = target.execute(statement)
                        wrote_target_rows = True
                        mysql_warnings = []
                        if sync_mode == "insert_only" and target.dialect.name == "mysql":
                            mysql_warnings = _collect_mysql_warnings(target)
                    finally:
                        if cycle_sync and target.dialect.name == "mysql":
                            target.execute(text("SET FOREIGN_KEY_CHECKS=1"))
                affected_count = max(0, int(getattr(result, "rowcount", 0) or 0))
                if sync_mode == "insert_only":
                    expected_inserted = max(0, len(rows) - len(existing_keys))
                    ignored_by_db = max(0, expected_inserted - affected_count)
                    job.inserted_count += affected_count
                    job.updated_count += 0
                    job.skipped_count += skipped_count + (ignored_by_db if target_dialect == "postgresql" else 0)
                    if ignored_by_db:
                        if target_dialect == "postgresql":
                            _append_drop_detail(job, job.table_name, "PostgreSQL skipped {} insert-only row(s) due to primary-key conflicts.".format(ignored_by_db), count=ignored_by_db)
                        else:
                            job.failed_count += ignored_by_db
                            if mysql_warnings:
                                for warning in mysql_warnings[:ignored_by_db]:
                                    _append_drop_detail(
                                        job,
                                        job.table_name,
                                        "MySQL ignored row during insert-only sync: {}".format(warning),
                                    )
                                if ignored_by_db > len(mysql_warnings):
                                    _append_drop_detail(
                                        job,
                                        job.table_name,
                                        "MySQL ignored {} insert-only row(s) during insert-only sync after the preflight check.".format(
                                            ignored_by_db - len(mysql_warnings)
                                        ),
                                        count=ignored_by_db - len(mysql_warnings),
                                    )
                            else:
                                _append_drop_detail(
                                    job,
                                    job.table_name,
                                    "MySQL ignored {} insert-only row(s) during insert-only sync after the preflight check.".format(
                                        ignored_by_db
                                    ),
                                    count=ignored_by_db,
                                )
                            current_app.logger.warning(
                                "Synchronization job %s ignored %s insert-only row(s) from table %s after the preflight check.",
                                job.id,
                                ignored_by_db,
                                job.table_name,
                            )
                else:
                    job.inserted_count += len(keys) - len(existing_keys)
                    job.updated_count += updated_count
                    job.skipped_count += skipped_count
                db.session.commit()
                offset += len(source_rows)
            if wrote_target_rows and target_engine.dialect.name == "postgresql":
                with target_engine.begin() as target:
                    _advance_postgresql_sequence(target, target_table)
            if job.failed_count == 0:
                _save_incremental_checkpoint(job, job.filters, max_incremental_cursor)
                _save_limited_full_checkpoint(job, job.filters, max_limited_full_primary_key)
            job.status = "completed"
        except Exception as exc:
            job.status = "failed"
            job.error_message = str(exc)
            raise
        finally:
            job.finished_at = datetime.now(timezone.utc)
            db.session.commit()
