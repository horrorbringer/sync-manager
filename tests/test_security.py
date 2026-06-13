import pytest
import json

from sync_manager import db
from sync_manager.models import DatabaseConnection, SyncJob, User
from sync_manager.security import decrypt_secret, encrypt_secret


def test_secret_encryption_round_trip(app):
    with app.app_context():
        encrypted = encrypt_secret("database-password")
        assert encrypted != "database-password"
        assert decrypt_secret(encrypted) == "database-password"


def test_invalid_encryption_key_has_actionable_error(app):
    with app.app_context():
        app.config["CREDENTIAL_ENCRYPTION_KEY"] = "not-a-valid-key"
        with pytest.raises(RuntimeError, match="generate-encryption-key"):
            encrypt_secret("database-password")


def test_viewer_cannot_create_connection(app, client):
    with app.app_context():
        viewer = User(username="viewer", role="viewer")
        viewer.set_password("password")
        db.session.add(viewer)
        db.session.commit()
    client.post("/auth/login", data={"username": "viewer", "password": "password"})
    response = client.get("/connections/new")
    assert response.status_code == 403


def test_admin_can_save_connection_with_empty_password(app, client):
    client.post("/auth/login", data={"username": "admin", "password": "password"})
    response = client.post(
        "/connections/new",
        data={
            "name": "Local MySQL",
            "host": "localhost",
            "port": "3306",
            "database_name": "example",
            "username": "root",
            "password": "",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Connection saved" in response.data
    with app.app_context():
        item = db.session.scalar(db.select(DatabaseConnection).filter_by(name="Local MySQL"))
        assert item.encrypted_password
        assert decrypt_secret(item.encrypted_password) == ""


def test_admin_can_save_connection_with_fk_mapping_rules(app, client):
    client.post("/auth/login", data={"username": "admin", "password": "password"})
    response = client.post(
        "/connections/new",
        data={
            "name": "Mapped MySQL",
            "host": "localhost",
            "port": "3306",
            "database_name": "example",
            "username": "root",
            "password": "",
            "fk_mapping_rules": json.dumps({"canned_responses": {"created_by": "username", "category_id": "title"}}),
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Connection saved" in response.data
    with app.app_context():
        item = db.session.scalar(db.select(DatabaseConnection).filter_by(name="Mapped MySQL"))
        assert item.fk_mapping_rules
        assert json.loads(item.fk_mapping_rules)["canned_responses"]["created_by"] == "username"


def test_connection_form_shows_mapping_presets(app, client):
    client.post("/auth/login", data={"username": "admin", "password": "password"})
    response = client.get("/connections/new")
    assert response.status_code == 200
    assert b"Quick presets" in response.data
    assert b"Support defaults" in response.data
    assert b"User lookups" in response.data


def test_connection_index_shows_mapping_summary(app, client):
    client.post("/auth/login", data={"username": "admin", "password": "password"})
    client.post(
        "/connections/new",
        data={
            "name": "Summary MySQL",
            "host": "localhost",
            "port": "3306",
            "database_name": "example",
            "username": "root",
            "password": "",
            "fk_mapping_rules": json.dumps({"canned_responses": {"created_by": "username"}}),
        },
        follow_redirects=True,
    )

    response = client.get("/connections/")

    assert response.status_code == 200
    assert b"configured" in response.data.lower()
    assert b"canned_responses" in response.data
    assert b"created_by" in response.data


def test_edit_connection_keeps_password_when_blank(app, client):
    client.post("/auth/login", data={"username": "admin", "password": "password"})
    with app.app_context():
        item = DatabaseConnection(
            name="Editable",
            host="localhost",
            database_name="db1",
            username="root",
            encrypted_password=encrypt_secret("secret"),
        )
        db.session.add(item)
        db.session.commit()
        item_id = item.id
    response = client.post(
        "/connections/{}/edit".format(item_id),
        data={"name": "Edited", "host": "127.0.0.1", "port": "3307", "database_name": "db2", "username": "admin", "password": ""},
        follow_redirects=True,
    )
    assert b"Connection updated" in response.data
    with app.app_context():
        item = db.session.get(DatabaseConnection, item_id)
        assert item.name == "Edited"
        assert decrypt_secret(item.encrypted_password) == "secret"


def test_connection_can_be_disabled(app, client):
    client.post("/auth/login", data={"username": "admin", "password": "password"})
    with app.app_context():
        item = DatabaseConnection(name="Toggle", host="localhost", database_name="db", username="root", encrypted_password="x")
        db.session.add(item)
        db.session.commit()
        item_id = item.id
    client.post("/connections/{}/toggle".format(item_id))
    with app.app_context():
        assert db.session.get(DatabaseConnection, item_id).is_enabled is False


def test_referenced_connection_cannot_be_deleted(app, client):
    client.post("/auth/login", data={"username": "admin", "password": "password"})
    with app.app_context():
        admin = db.session.scalar(db.select(User).filter_by(username="admin"))
        source = DatabaseConnection(name="Used source", host="localhost", database_name="db1", username="root", encrypted_password="x")
        target = DatabaseConnection(name="Used target", host="localhost", database_name="db2", username="root", encrypted_password="x")
        db.session.add_all([source, target])
        db.session.flush()
        db.session.add(SyncJob(source=source, target=target, table_name="customers", status="completed", initiated_by=admin))
        db.session.commit()
        source_id = source.id
    response = client.post("/connections/{}/delete".format(source_id), follow_redirects=True)
    assert b"cannot be deleted" in response.data
    with app.app_context():
        assert db.session.get(DatabaseConnection, source_id) is not None
