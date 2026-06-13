from sync_manager import db
from sync_manager.models import AuditLog, DatabaseConnection, SyncJob, User


def test_login_required(client):
    response = client.get("/")
    assert response.status_code == 302
    assert "/auth/login" in response.headers["Location"]


def test_login(client):
    response = client.post("/auth/login", data={"username": "admin", "password": "password"}, follow_redirects=True)
    assert response.status_code == 200
    assert b"Dashboard" in response.data


def test_invalid_login(client):
    response = client.post("/auth/login", data={"username": "admin", "password": "wrong"})
    assert response.status_code == 200
    assert b"Invalid username or password" in response.data


def test_job_detail_requires_login(client):
    response = client.get("/sync/jobs/1")
    assert response.status_code == 302
    assert "/auth/login" in response.headers["Location"]


def test_sync_form_renders_for_admin(client):
    client.post("/auth/login", data={"username": "admin", "password": "password"})
    response = client.get("/sync/new")
    assert response.status_code == 200
    assert b"New synchronization" in response.data


def test_audit_page_renders_for_authenticated_user(client):
    client.post("/auth/login", data={"username": "admin", "password": "password"})
    response = client.get("/audit")
    assert response.status_code == 200
    assert b"Audit log" in response.data


def test_dashboard_jobs_paginate(client, app):
    client.post("/auth/login", data={"username": "admin", "password": "password"})
    with app.app_context():
        admin = db.session.scalar(db.select(User).filter_by(username="admin"))
        source = DatabaseConnection(name="jobs-source", host="localhost", database_name="source", username="root", encrypted_password="x")
        target = DatabaseConnection(name="jobs-target", host="localhost", database_name="target", username="root", encrypted_password="x")
        db.session.add_all([source, target])
        db.session.commit()
        jobs = [
            SyncJob(source=source, target=target, table_name="table{}".format(index), status="completed", initiated_by=admin)
            for index in range(30)
        ]
        db.session.add_all(jobs)
        db.session.commit()

    response = client.get("/?page=2")

    assert response.status_code == 200
    assert b"Showing 26-30 of 30" in response.data
    assert b"table4" in response.data
    assert b"table29" not in response.data


def test_dashboard_shows_short_upsert_label(client, app):
    client.post("/auth/login", data={"username": "admin", "password": "password"})
    with app.app_context():
        admin = db.session.scalar(db.select(User).filter_by(username="admin"))
        source = DatabaseConnection(name="upsert-source", host="localhost", database_name="source", username="root", encrypted_password="x")
        target = DatabaseConnection(name="upsert-target", host="localhost", database_name="target", username="root", encrypted_password="x")
        db.session.add_all([source, target])
        db.session.commit()
        job = SyncJob(
            source=source,
            target=target,
            table_name="tickets",
            sync_mode="upsert",
            status="completed",
            initiated_by=admin,
        )
        db.session.add(job)
        db.session.commit()

    response = client.get("/")

    assert response.status_code == 200
    assert b"Upsert" in response.data


def test_audit_logs_paginate(client, app):
    client.post("/auth/login", data={"username": "admin", "password": "password"})
    with app.app_context():
        admin = db.session.scalar(db.select(User).filter_by(username="admin"))
        logs = [AuditLog(user_id=admin.id, action="test.event.{}".format(index), details="detail {}".format(index)) for index in range(30)]
        db.session.add_all(logs)
        db.session.commit()

    response = client.get("/audit?page=2")

    assert response.status_code == 200
    assert b"Showing 26-" in response.data
    assert b"test.event.4" in response.data
    assert b"test.event.29" not in response.data


def test_application_stylesheet_is_served(client):
    response = client.get("/static/app.css")
    assert response.status_code == 200
    assert response.mimetype == "text/css"
    assert b".sidebar" in response.data
