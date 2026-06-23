import json

from werkzeug.datastructures import MultiDict

from sync_manager import db
from sync_manager.models import DatabaseConnection, SyncJob, User
from sync_manager.sync.routes import _remember_planned_parent_values


def _failed_job(app, table_name="customers", sync_mode="insert_only"):
    with app.app_context():
        user = db.session.scalar(db.select(User).filter_by(username="admin"))
        source = DatabaseConnection(
            name="retry-source",
            host="localhost",
            database_name="source_db",
            username="root",
            encrypted_password="encrypted",
        )
        target = DatabaseConnection(
            name="retry-target",
            host="localhost",
            database_name="target_db",
            username="root",
            encrypted_password="encrypted",
        )
        job = SyncJob(
            source=source,
            target=target,
            table_name=table_name,
            sync_mode=sync_mode,
            status="failed",
            error_message="test failure",
            initiated_by=user,
        )
        db.session.add(job)
        db.session.commit()
        return job.id


def test_incremental_parent_preview_values_are_available_to_dependent_validation():
    planned_parent_values = {}

    _remember_planned_parent_values(
        planned_parent_values,
        "academic_years",
        {"errors": [], "_source_primary_key_values": {1, 2, 3, 4}},
    )

    assert planned_parent_values == {"academic_years": {1, 2, 3, 4}}


def test_invalid_parent_preview_values_are_not_available_to_dependent_validation():
    planned_parent_values = {}

    _remember_planned_parent_values(
        planned_parent_values,
        "academic_years",
        {"errors": ["Target table is missing"], "_source_primary_key_values": {1, 2, 3, 4}},
    )

    assert planned_parent_values == {}


def test_failed_job_can_be_retried(app, client, monkeypatch):
    job_id = _failed_job(app)
    monkeypatch.setattr("sync_manager.sync.routes.validate_table", lambda *args, **kwargs: [])
    queued = []
    monkeypatch.setattr("sync_manager.sync.routes.enqueue_job", lambda new_job_id: queued.append(new_job_id))
    client.post("/auth/login", data={"username": "admin", "password": "password"})

    response = client.post("/sync/jobs/{}/retry".format(job_id), follow_redirects=True)

    assert response.status_code == 200
    assert b"was retried as job" in response.data
    assert len(queued) == 1
    with app.app_context():
        retry = db.session.get(SyncJob, queued[0])
        assert retry.status == "pending"
        assert retry.table_name == "customers"
        assert retry.sync_mode == "insert_only"


def test_failed_job_retry_preserves_sync_mode(app, client, monkeypatch):
    job_id = _failed_job(app, sync_mode="insert_only")
    monkeypatch.setattr("sync_manager.sync.routes.validate_table", lambda *args, **kwargs: [])
    queued = []
    monkeypatch.setattr("sync_manager.sync.routes.enqueue_job", lambda new_job_id: queued.append(new_job_id))
    client.post("/auth/login", data={"username": "admin", "password": "password"})

    response = client.post("/sync/jobs/{}/retry".format(job_id), follow_redirects=True)

    assert response.status_code == 200
    with app.app_context():
        retry = db.session.get(SyncJob, queued[0])
        assert retry.sync_mode == "insert_only"


def test_failed_job_without_table_cannot_be_retried(app, client, monkeypatch):
    job_id = _failed_job(app, table_name="")
    monkeypatch.setattr("sync_manager.sync.routes.enqueue_job", lambda job_id: None)
    client.post("/auth/login", data={"username": "admin", "password": "password"})

    response = client.post("/sync/jobs/{}/retry".format(job_id), follow_redirects=True)

    assert response.status_code == 200
    assert b"has no table name and cannot be retried" in response.data


def test_queue_broker_failure_marks_job_failed(app, client, monkeypatch):
    from sync_manager.models import DatabaseConnection

    with app.app_context():
        source = DatabaseConnection(name="broker-source", host="localhost", database_name="source", username="root", encrypted_password="x")
        target = DatabaseConnection(name="broker-target", host="localhost", database_name="target", username="root", encrypted_password="x")
        db.session.add_all([source, target])
        db.session.commit()
        source_id, target_id = source.id, target.id
    monkeypatch.setattr("sync_manager.sync.routes.validate_table", lambda *args, **kwargs: [])
    monkeypatch.setattr("sync_manager.sync.routes.dry_run", lambda *args, **kwargs: {"errors": [], "source_count": 1, "target_count": 0, "new_count": 1, "existing_count": 0})
    monkeypatch.setattr("sync_manager.sync.routes.enqueue_job", lambda job_id: (_ for _ in ()).throw(ConnectionError("Redis unavailable")))
    monkeypatch.setattr("sync_manager.sync.routes.expand_tables_with_dependencies", lambda source, tables: list(dict.fromkeys(tables)))
    monkeypatch.setattr("sync_manager.sync.routes.dependency_report", lambda source, tables: {"ordered_tables": list(tables), "cycle_tables": [], "dependencies": {}})
    client.post("/auth/login", data={"username": "admin", "password": "password"})

    response = client.post(
        "/sync/new",
        data={"source_id": source_id, "target_id": target_id, "table_name": "customers", "action": "execute"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Unable to start synchronization" in response.data
    with app.app_context():
        job = db.session.scalar(db.select(SyncJob).order_by(SyncJob.id.desc()))
        assert job.status == "failed"
        assert "Redis unavailable" in job.error_message


def test_sync_form_without_action_shows_warning(app, client, monkeypatch):
    with app.app_context():
        source = DatabaseConnection(name="action-source", host="localhost", database_name="source", username="root", encrypted_password="x")
        target = DatabaseConnection(name="action-target", host="localhost", database_name="target", username="root", encrypted_password="x")
        db.session.add_all([source, target])
        db.session.commit()
        source_id, target_id = source.id, target.id
    monkeypatch.setattr("sync_manager.sync.routes.discover_tables", lambda *args, **kwargs: [])
    client.post("/auth/login", data={"username": "admin", "password": "password"})

    response = client.post(
        "/sync/new",
        data={"source_id": source_id, "target_id": target_id},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Select an action before submitting the synchronization form." in response.data


def test_incremental_sync_requires_checkpoint_for_every_selected_table(app, client, monkeypatch):
    with app.app_context():
        source = DatabaseConnection(name="incremental-source", host="localhost", database_name="source", username="root", encrypted_password="x")
        target = DatabaseConnection(name="incremental-target", host="localhost", database_name="target", username="root", encrypted_password="x")
        db.session.add_all([source, target])
        db.session.commit()
        source_id, target_id = source.id, target.id

    monkeypatch.setattr("sync_manager.sync.routes.discover_tables", lambda *args, **kwargs: [])
    client.post("/auth/login", data={"username": "admin", "password": "password"})

    response = client.post(
        "/sync/new",
        data={
            "source_id": source_id,
            "target_id": target_id,
            "table_name": "customers",
            "sync_scope": "incremental",
            "action": "dry_run",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Incremental sync requires a checkpoint column for every selected table: customers" in response.data


def test_execute_creates_jobs_with_selected_sync_mode(app, client, monkeypatch):
    with app.app_context():
        source = DatabaseConnection(name="mode-source", host="localhost", database_name="source", username="root", encrypted_password="x")
        target = DatabaseConnection(name="mode-target", host="localhost", database_name="target", username="root", encrypted_password="x")
        db.session.add_all([source, target])
        db.session.commit()
        source_id, target_id = source.id, target.id

    queued = []
    monkeypatch.setattr("sync_manager.sync.routes.validate_table", lambda *args, **kwargs: [])
    monkeypatch.setattr("sync_manager.sync.routes.dry_run", lambda *args, **kwargs: {"errors": [], "source_count": 1, "target_count": 0, "new_count": 1, "existing_count": 0})
    monkeypatch.setattr("sync_manager.sync.routes.enqueue_job", lambda job_id: queued.append(job_id))
    monkeypatch.setattr("sync_manager.sync.routes.expand_tables_with_dependencies", lambda source, tables: list(dict.fromkeys(tables)))
    monkeypatch.setattr("sync_manager.sync.routes.dependency_report", lambda source, tables: {"ordered_tables": list(tables), "cycle_tables": [], "dependencies": {}})
    client.post("/auth/login", data={"username": "admin", "password": "password"})

    response = client.post(
        "/sync/new",
        data={
            "source_id": source_id,
            "target_id": target_id,
            "table_name": "customers",
            "row_limit": "25",
            "action": "execute",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Synchronization jobs" in response.data
    with app.app_context():
        job = db.session.get(SyncJob, queued[0])
        assert job.sync_mode == "insert_only"
        assert job.row_limit == 25
        assert job.sync_mode_label == "Add new records only"


def test_execute_defaults_to_insert_only(app, client, monkeypatch):
    with app.app_context():
        source = DatabaseConnection(name="default-mode-source", host="localhost", database_name="source", username="root", encrypted_password="x")
        target = DatabaseConnection(name="default-mode-target", host="localhost", database_name="target", username="root", encrypted_password="x")
        db.session.add_all([source, target])
        db.session.commit()
        source_id, target_id = source.id, target.id

    queued = []
    monkeypatch.setattr("sync_manager.sync.routes.validate_table", lambda *args, **kwargs: [])
    monkeypatch.setattr("sync_manager.sync.routes.dry_run", lambda *args, **kwargs: {"errors": [], "source_count": 1, "target_count": 0, "new_count": 1, "existing_count": 0})
    monkeypatch.setattr("sync_manager.sync.routes.enqueue_job", lambda job_id: queued.append(job_id))
    monkeypatch.setattr("sync_manager.sync.routes.expand_tables_with_dependencies", lambda source, tables: list(dict.fromkeys(tables)))
    monkeypatch.setattr("sync_manager.sync.routes.dependency_report", lambda source, tables: {"ordered_tables": list(tables), "cycle_tables": [], "dependencies": {}})
    client.post("/auth/login", data={"username": "admin", "password": "password"})

    response = client.post(
        "/sync/new",
        data={
            "source_id": source_id,
            "target_id": target_id,
            "table_name": "customers",
            "action": "execute",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        job = db.session.get(SyncJob, queued[0])
        assert job.sync_mode == "insert_only"


def test_sync_all_tables_queues_every_discovered_table(app, client, monkeypatch):
    with app.app_context():
        source = DatabaseConnection(name="all-source", host="localhost", database_name="source", username="root", encrypted_password="x")
        target = DatabaseConnection(name="all-target", host="localhost", database_name="target", username="root", encrypted_password="x")
        db.session.add_all([source, target])
        db.session.commit()
        source_id, target_id = source.id, target.id

    queued = []
    monkeypatch.setattr("sync_manager.sync.routes.discover_tables", lambda *args: [{"name": "users"}, {"name": "tickets"}])
    monkeypatch.setattr("sync_manager.sync.routes.expand_tables_with_dependencies", lambda source, tables: list(dict.fromkeys(tables)))
    monkeypatch.setattr("sync_manager.sync.routes.dependency_report", lambda source, tables: {"ordered_tables": ["users", "tickets"], "cycle_tables": [], "dependencies": {}})
    monkeypatch.setattr("sync_manager.sync.routes.validate_table", lambda *args, **kwargs: [])
    monkeypatch.setattr("sync_manager.sync.routes.dry_run", lambda *args, **kwargs: {"errors": [], "source_count": 2, "target_count": 0, "new_count": 2, "existing_count": 0})
    monkeypatch.setattr("sync_manager.sync.routes.enqueue_job", lambda job_id: queued.append(job_id))
    client.post("/auth/login", data={"username": "admin", "password": "password"})

    response = client.post(
        "/sync/new",
        data={
            "source_id": source_id,
            "target_id": target_id,
            "action": "sync_all",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Synchronization jobs" in response.data
    assert len(queued) == 2
    with app.app_context():
        jobs = [db.session.get(SyncJob, job_id) for job_id in queued]
        assert [job.table_name for job in jobs] == ["users", "tickets"]


def test_completed_job_reports_full_progress_even_without_row_changes(app):
    with app.app_context():
        user = db.session.scalar(db.select(User).filter_by(username="admin"))
        source = DatabaseConnection(name="progress-source", host="localhost", database_name="source", username="root", encrypted_password="x")
        target = DatabaseConnection(name="progress-target", host="localhost", database_name="target", username="root", encrypted_password="x")
        job = SyncJob(source=source, target=target, table_name="customers", sync_mode="insert_only", status="completed", source_count=100, initiated_by=user)
        db.session.add(job)
        db.session.commit()

        assert job.inserted_count == 0
        assert job.updated_count == 0
        assert job.skipped_count == 0
        assert job.progress_percent == 100


def test_insert_only_job_tracks_skipped_rows(app):
    with app.app_context():
        user = db.session.scalar(db.select(User).filter_by(username="admin"))
        source = DatabaseConnection(name="skip-source", host="localhost", database_name="source", username="root", encrypted_password="x")
        target = DatabaseConnection(name="skip-target", host="localhost", database_name="target", username="root", encrypted_password="x")
        job = SyncJob(source=source, target=target, table_name="customers", sync_mode="insert_only", status="completed", source_count=15, initiated_by=user)
        job.inserted_count = 7
        job.skipped_count = 8
        db.session.add(job)
        db.session.commit()

        assert job.processed_count == 15
        assert job.progress_percent == 100


def test_failed_rows_count_toward_processed_count(app):
    with app.app_context():
        user = db.session.scalar(db.select(User).filter_by(username="admin"))
        source = DatabaseConnection(name="failed-count-source", host="localhost", database_name="source", username="root", encrypted_password="x")
        target = DatabaseConnection(name="failed-count-target", host="localhost", database_name="target", username="root", encrypted_password="x")
        job = SyncJob(source=source, target=target, table_name="users", sync_mode="insert_only", status="completed", source_count=10, initiated_by=user)
        job.inserted_count = 6
        job.failed_count = 4
        db.session.add(job)
        db.session.commit()

        assert job.processed_count == 10
        assert job.progress_percent == 100


def test_completed_job_shows_dropped_row_details(app, client):
    with app.app_context():
        user = db.session.scalar(db.select(User).filter_by(username="admin"))
        source = DatabaseConnection(name="drop-source", host="localhost", database_name="source", username="root", encrypted_password="x")
        target = DatabaseConnection(name="drop-target", host="localhost", database_name="target", username="root", encrypted_password="x")
        job = SyncJob(
            source=source,
            target=target,
            table_name="tickets",
            sync_mode="insert_only",
            status="completed",
            source_count=5,
            failed_count=2,
            initiated_by=user,
        )
        job.drop_details = json.dumps(
            [
                {"table": "tickets", "row": {"id": 3}, "reason": "Table 'tickets' cannot remap foreign key 'department_id': source row '3' was not found in 'departments'."},
                {"table": "tickets", "reason": "MySQL ignored row during insert-only sync: Duplicate entry '5' for key 'PRIMARY'"},
            ]
        )
        db.session.add(job)
        db.session.commit()
        job_id = job.id

    client.post("/auth/login", data={"username": "admin", "password": "password"})

    response = client.get("/sync/jobs/{}".format(job_id))

    assert response.status_code == 200
    assert b"Dropped rows" in response.data
    assert b"department_id" in response.data
    assert b"Duplicate entry" in response.data


def test_pending_job_can_be_cancelled(app, client):
    job_id = _failed_job(app)
    with app.app_context():
        job = db.session.get(SyncJob, job_id)
        job.status = "pending"
        job.error_message = None
        db.session.commit()
    client.post("/auth/login", data={"username": "admin", "password": "password"})

    response = client.post("/sync/jobs/{}/cancel".format(job_id), follow_redirects=True)

    assert response.status_code == 200
    assert b"was cancelled" in response.data
    with app.app_context():
        job = db.session.get(SyncJob, job_id)
        assert job.status == "failed"
        assert "Cancelled before" in job.error_message


def test_inline_sync_request_keeps_current_user_attached(app, client, monkeypatch):
    with app.app_context():
        source = DatabaseConnection(name="request-source", host="localhost", database_name="source", username="root", encrypted_password="x")
        target = DatabaseConnection(name="request-target", host="localhost", database_name="target", username="root", encrypted_password="x")
        db.session.add_all([source, target])
        db.session.commit()
        source_id, target_id = source.id, target.id

    monkeypatch.setattr("sync_manager.sync.routes.validate_table", lambda *args, **kwargs: [])
    monkeypatch.setattr("sync_manager.sync.routes.dry_run", lambda *args, **kwargs: {"errors": [], "source_count": 1, "target_count": 0, "new_count": 1, "existing_count": 0})
    monkeypatch.setattr("sync_manager.sync.tasks.notify_async", lambda *args: None)
    monkeypatch.setattr("sync_manager.sync.routes.expand_tables_with_dependencies", lambda source, tables: list(dict.fromkeys(tables)))
    monkeypatch.setattr("sync_manager.sync.routes.dependency_report", lambda source, tables: {"ordered_tables": list(tables), "cycle_tables": [], "dependencies": {}})

    def complete(job, batch_size):
        job.status = "completed"
        db.session.commit()

    monkeypatch.setattr("sync_manager.sync.tasks.synchronize", complete)
    client.post("/auth/login", data={"username": "admin", "password": "password"})

    response = client.post(
        "/sync/new",
        data={"source_id": source_id, "target_id": target_id, "table_name": "customers", "action": "execute"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"completed" in response.data


def test_sync_views_disable_browser_cache(app, client, monkeypatch):
    with app.app_context():
        source = DatabaseConnection(name="cache-source", host="localhost", database_name="source", username="root", encrypted_password="x")
        target = DatabaseConnection(name="cache-target", host="localhost", database_name="target", username="root", encrypted_password="x")
        db.session.add_all([source, target])
        db.session.commit()
        job = SyncJob(
            source=source,
            target=target,
            table_name="customers",
            sync_mode="insert_only",
            status="completed",
            source_count=1,
            inserted_count=1,
            initiated_by=db.session.scalar(db.select(User).filter_by(username="admin")),
        )
        db.session.add(job)
        db.session.commit()
        job_id = job.id
        source_id, target_id = source.id, target.id

    client.post("/auth/login", data={"username": "admin", "password": "password"})

    create_response = client.get("/sync/new")
    detail_response = client.get("/sync/jobs/{}".format(job_id))

    assert create_response.headers["Cache-Control"] == "no-store, no-cache, must-revalidate, max-age=0"
    assert create_response.headers["Pragma"] == "no-cache"
    assert create_response.headers["Expires"] == "0"
    assert detail_response.headers["Cache-Control"] == "no-store, no-cache, must-revalidate, max-age=0"


def test_discovery_renders_searchable_table_metadata(app, client, monkeypatch):
    with app.app_context():
        source = DatabaseConnection(name="metadata-source", host="localhost", database_name="source", username="root", encrypted_password="x")
        target = DatabaseConnection(name="metadata-target", host="localhost", database_name="target", username="root", encrypted_password="x")
        db.session.add_all([source, target])
        db.session.commit()
        source_id, target_id = source.id, target.id

    monkeypatch.setattr(
        "sync_manager.sync.routes.discover_tables",
        lambda *args: [
            {
                "name": "customers",
                "column_count": 5,
                "primary_key": ["id"],
                "target_exists": True,
                "mapping_state": "heuristic",
                "mapping_preview": [{"display": "created_by -> users.username"}],
                "mapping_columns": ["created_by"],
                "mapping_errors": [],
                "dependencies": ["users"],
            },
            {
                "name": "events",
                "column_count": 3,
                "primary_key": [],
                "target_exists": False,
                "mapping_state": "mapped",
                "mapping_preview": [],
                "mapping_columns": [],
                "mapping_errors": [],
                "dependencies": [],
            },
        ],
    )
    client.post("/auth/login", data={"username": "admin", "password": "password"})

    response = client.post(
        "/sync/new",
        data={"source_id": source_id, "target_id": target_id, "table_name": "", "action": "discover"},
    )

    assert response.status_code == 200
    assert b"Find source tables" in response.data
    assert b"5 columns" in response.data
    assert b"PK id" in response.data
    assert b"Heuristic" in response.data
    assert b"Target found" in response.data
    assert b"No primary key" in response.data
    assert b"Target missing" in response.data


def test_multi_table_execute_queues_each_selected_table(app, client, monkeypatch):
    with app.app_context():
        source = DatabaseConnection(name="bulk-source", host="localhost", database_name="source", username="root", encrypted_password="x")
        target = DatabaseConnection(name="bulk-target", host="localhost", database_name="target", username="root", encrypted_password="x")
        db.session.add_all([source, target])
        db.session.commit()
        source_id, target_id = source.id, target.id

    queued = []
    monkeypatch.setattr("sync_manager.sync.routes.validate_table", lambda *args, **kwargs: [])
    monkeypatch.setattr("sync_manager.sync.routes.dry_run", lambda *args, **kwargs: {"errors": [], "source_count": 3, "target_count": 0, "new_count": 3, "existing_count": 0})
    monkeypatch.setattr("sync_manager.sync.routes.enqueue_job", lambda job_id: queued.append(job_id))
    monkeypatch.setattr("sync_manager.sync.routes.expand_tables_with_dependencies", lambda source, tables: list(dict.fromkeys(tables)))
    monkeypatch.setattr("sync_manager.sync.routes.dependency_report", lambda source, tables: {"ordered_tables": list(tables), "cycle_tables": [], "dependencies": {}})
    client.post("/auth/login", data={"username": "admin", "password": "password"})

    response = client.post(
        "/sync/new",
        data=MultiDict(
            [
                ("source_id", source_id),
                ("target_id", target_id),
                ("action", "execute"),
                ("table_names", "customers"),
                ("table_names", "orders"),
            ]
        ),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Synchronization jobs" in response.data
    assert len(queued) == 2
    with app.app_context():
        jobs = db.session.scalars(db.select(SyncJob).order_by(SyncJob.id.desc()).limit(2)).all()
        assert {job.table_name for job in jobs} == {"customers", "orders"}
        assert all(job.status == "pending" for job in jobs)


def test_single_table_execute_auto_adds_parent_tables(app, client, monkeypatch):
    with app.app_context():
        source = DatabaseConnection(name="parent-source", host="localhost", database_name="source", username="root", encrypted_password="x")
        target = DatabaseConnection(name="parent-target", host="localhost", database_name="target", username="root", encrypted_password="x")
        db.session.add_all([source, target])
        db.session.commit()
        source_id, target_id = source.id, target.id

    queued = []
    monkeypatch.setattr("sync_manager.sync.routes.expand_tables_with_dependencies", lambda source_conn, tables: ["departments", "users"])
    monkeypatch.setattr("sync_manager.sync.routes.validate_table", lambda *args, **kwargs: [])
    monkeypatch.setattr("sync_manager.sync.routes.dry_run", lambda *args, **kwargs: {"errors": [], "source_count": 3, "target_count": 0, "new_count": 3, "existing_count": 0})
    monkeypatch.setattr("sync_manager.sync.routes.enqueue_job", lambda job_id: queued.append(job_id))
    monkeypatch.setattr("sync_manager.sync.routes.dependency_report", lambda source, tables: {"ordered_tables": ["departments", "users"], "cycle_tables": [], "dependencies": {}})
    client.post("/auth/login", data={"username": "admin", "password": "password"})

    response = client.post(
        "/sync/new",
        data=MultiDict(
            [
                ("source_id", source_id),
                ("target_id", target_id),
                ("action", "execute"),
                ("table_names", "users"),
            ]
        ),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert len(queued) == 2
    with app.app_context():
        jobs = [db.session.get(SyncJob, job_id) for job_id in queued]
        assert [job.table_name for job in jobs] == ["departments", "users"]


def test_multi_table_execute_uses_dependency_order(app, client, monkeypatch):
    with app.app_context():
        source = DatabaseConnection(name="order-source", host="localhost", database_name="source", username="root", encrypted_password="x")
        target = DatabaseConnection(name="order-target", host="localhost", database_name="target", username="root", encrypted_password="x")
        db.session.add_all([source, target])
        db.session.commit()
        source_id, target_id = source.id, target.id

    queued = []
    monkeypatch.setattr("sync_manager.sync.routes.validate_table", lambda *args, **kwargs: [])
    monkeypatch.setattr("sync_manager.sync.routes.dry_run", lambda *args, **kwargs: {"errors": [], "source_count": 3, "target_count": 0, "new_count": 3, "existing_count": 0})
    monkeypatch.setattr("sync_manager.sync.routes.enqueue_job", lambda job_id: queued.append(job_id))
    monkeypatch.setattr("sync_manager.sync.routes.expand_tables_with_dependencies", lambda source, tables: list(dict.fromkeys(tables)))
    monkeypatch.setattr(
        "sync_manager.sync.routes.dependency_report",
        lambda source, tables: {"ordered_tables": ["customers", "orders"], "cycle_tables": [], "dependencies": {}},
    )
    client.post("/auth/login", data={"username": "admin", "password": "password"})

    response = client.post(
        "/sync/new",
        data=MultiDict(
            [
                ("source_id", source_id),
                ("target_id", target_id),
                ("action", "execute"),
                ("table_names", "orders"),
                ("table_names", "customers"),
            ]
        ),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Synchronization jobs" in response.data
    assert len(queued) == 2
    with app.app_context():
        jobs = [db.session.get(SyncJob, job_id) for job_id in queued]
        assert [job.table_name for job in jobs] == ["customers", "orders"]


def test_multi_table_execute_skips_empty_tables(app, client, monkeypatch):
    with app.app_context():
        source = DatabaseConnection(name="bulk-empty-source", host="localhost", database_name="source", username="root", encrypted_password="x")
        target = DatabaseConnection(name="bulk-empty-target", host="localhost", database_name="target", username="root", encrypted_password="x")
        db.session.add_all([source, target])
        db.session.commit()
        source_id, target_id = source.id, target.id

    queued = []
    monkeypatch.setattr("sync_manager.sync.routes.validate_table", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        "sync_manager.sync.routes.dry_run",
        lambda source_conn, target_conn, table_name: {
            "errors": [],
            "source_count": 0 if table_name == "archived_logs" else 4,
            "target_count": 0,
            "new_count": 0 if table_name == "archived_logs" else 4,
            "existing_count": 0,
            "empty": table_name == "archived_logs",
        },
    )
    monkeypatch.setattr("sync_manager.sync.routes.enqueue_job", lambda job_id: queued.append(job_id))
    monkeypatch.setattr("sync_manager.sync.routes.discover_tables", lambda *args: [{"name": "customers", "column_count": 5, "primary_key": ["id"], "target_exists": True}, {"name": "archived_logs", "column_count": 2, "primary_key": ["id"], "target_exists": True}])
    monkeypatch.setattr("sync_manager.sync.routes.expand_tables_with_dependencies", lambda source, tables: list(dict.fromkeys(tables)))
    monkeypatch.setattr("sync_manager.sync.routes.dependency_report", lambda source, tables: {"ordered_tables": ["customers", "archived_logs"], "cycle_tables": [], "dependencies": {}})
    client.post("/auth/login", data={"username": "admin", "password": "password"})

    response = client.post(
        "/sync/new",
        data=MultiDict(
            [
                ("source_id", source_id),
                ("target_id", target_id),
                ("action", "execute"),
                ("table_names", "customers"),
                ("table_names", "archived_logs"),
            ]
        ),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"archived_logs" not in response.data
    assert len(queued) == 1
    with app.app_context():
        jobs = [db.session.get(SyncJob, job_id) for job_id in queued]
        assert [job.table_name for job in jobs] == ["customers"]


def test_multi_table_execute_skips_tables_with_no_new_rows_in_insert_only(app, client, monkeypatch):
    with app.app_context():
        source = DatabaseConnection(name="bulk-noop-source", host="localhost", database_name="source", username="root", encrypted_password="x")
        target = DatabaseConnection(name="bulk-noop-target", host="localhost", database_name="target", username="root", encrypted_password="x")
        db.session.add_all([source, target])
        db.session.commit()
        source_id, target_id = source.id, target.id

    queued = []
    monkeypatch.setattr("sync_manager.sync.routes.validate_table", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        "sync_manager.sync.routes.dry_run",
        lambda source_conn, target_conn, table_name: {
            "errors": [],
            "source_count": 4,
            "target_count": 4,
            "new_count": 0,
            "existing_count": 4,
        },
    )
    monkeypatch.setattr("sync_manager.sync.routes.enqueue_job", lambda job_id: queued.append(job_id))
    monkeypatch.setattr("sync_manager.sync.routes.discover_tables", lambda *args: [{"name": "customers", "column_count": 5, "primary_key": ["id"], "target_exists": True}])
    monkeypatch.setattr("sync_manager.sync.routes.expand_tables_with_dependencies", lambda source, tables: list(dict.fromkeys(tables)))
    monkeypatch.setattr("sync_manager.sync.routes.dependency_report", lambda source, tables: {"ordered_tables": ["customers"], "cycle_tables": [], "dependencies": {}})
    client.post("/auth/login", data={"username": "admin", "password": "password"})

    response = client.post(
        "/sync/new",
        data=MultiDict(
            [
                ("source_id", source_id),
                ("target_id", target_id),
                ("action", "execute"),
                ("sync_mode", "insert_only"),
                ("table_names", "customers"),
            ]
        ),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert len(queued) == 0
    assert b"No new records to add" not in response.data
    assert b"Ignored" not in response.data
    assert b"Skipped" not in response.data


def test_multi_table_execute_returns_validation_errors_for_problem_tables(app, client, monkeypatch):
    with app.app_context():
        source = DatabaseConnection(name="bulk-source-err", host="localhost", database_name="source", username="root", encrypted_password="x")
        target = DatabaseConnection(name="bulk-target-err", host="localhost", database_name="target", username="root", encrypted_password="x")
        db.session.add_all([source, target])
        db.session.commit()
        source_id, target_id = source.id, target.id

    monkeypatch.setattr(
        "sync_manager.sync.routes.discover_tables",
        lambda *args: [
            {"name": "customers", "column_count": 5, "primary_key": ["id"], "target_exists": True},
            {"name": "orders", "column_count": 3, "primary_key": ["id"], "target_exists": True},
        ],
    )
    queued = []

    def validate(source_conn, target_conn, table_name, **kwargs):
        return ["orders is blocked"] if table_name == "orders" else []

    monkeypatch.setattr("sync_manager.sync.routes.validate_table", validate)
    monkeypatch.setattr(
        "sync_manager.sync.routes.dry_run",
        lambda source_conn, target_conn, table_name: {"errors": [], "source_count": 2, "target_count": 0, "new_count": 2, "existing_count": 0},
    )
    monkeypatch.setattr("sync_manager.sync.routes.enqueue_job", lambda job_id: queued.append(job_id))
    monkeypatch.setattr("sync_manager.sync.routes.expand_tables_with_dependencies", lambda source, tables: list(dict.fromkeys(tables)))
    monkeypatch.setattr("sync_manager.sync.routes.dependency_report", lambda source, tables: {"ordered_tables": list(tables), "cycle_tables": [], "dependencies": {}})
    client.post("/auth/login", data={"username": "admin", "password": "password"})

    response = client.post(
        "/sync/new",
        data=MultiDict(
            [
                ("source_id", source_id),
                ("target_id", target_id),
                ("action", "execute"),
                ("table_names", "customers"),
                ("table_names", "orders"),
            ]
        ),
    )

    assert response.status_code == 200
    assert b"Validation found issues" in response.data
    assert b"customers" in response.data
    assert b"orders" in response.data
    assert b"orders is blocked" in response.data
    assert len(queued) == 1


def test_multi_table_execute_queues_safe_tables_and_blocks_cycles(app, client, monkeypatch):
    with app.app_context():
        source = DatabaseConnection(name="bulk-cycle-source", host="localhost", database_name="source", username="root", encrypted_password="x")
        target = DatabaseConnection(name="bulk-cycle-target", host="localhost", database_name="target", username="root", encrypted_password="x")
        db.session.add_all([source, target])
        db.session.commit()
        source_id, target_id = source.id, target.id

    queued = []
    monkeypatch.setattr("sync_manager.sync.routes.validate_table", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        "sync_manager.sync.routes.dry_run",
        lambda source_conn, target_conn, table_name: {"errors": [], "source_count": 2, "target_count": 0, "new_count": 2, "existing_count": 0},
    )
    monkeypatch.setattr("sync_manager.sync.routes.enqueue_job", lambda job_id: queued.append(job_id))
    monkeypatch.setattr(
        "sync_manager.sync.routes.discover_tables",
        lambda *args: [
            {"name": "customers", "column_count": 5, "primary_key": ["id"], "target_exists": True},
            {"name": "ticket_categories", "column_count": 4, "primary_key": ["id"], "target_exists": True},
        ],
    )
    monkeypatch.setattr(
        "sync_manager.sync.routes.dependency_report",
        lambda source, tables: {"ordered_tables": ["customers"], "cycle_tables": ["ticket_categories"], "dependencies": {}},
    )
    monkeypatch.setattr("sync_manager.sync.routes.expand_tables_with_dependencies", lambda source, tables: list(dict.fromkeys(tables)))
    client.post("/auth/login", data={"username": "admin", "password": "password"})

    response = client.post(
        "/sync/new",
        data=MultiDict(
            [
                ("source_id", source_id),
                ("target_id", target_id),
                ("action", "execute"),
                ("table_names", "customers"),
                ("table_names", "ticket_categories"),
            ]
        ),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Synchronization jobs" in response.data
    assert len(queued) == 2
    with app.app_context():
        jobs = [db.session.get(SyncJob, job_id) for job_id in queued]
        assert [job.table_name for job in jobs] == ["customers", "ticket_categories"]
        assert [job.cycle_sync for job in jobs] == [False, True]
