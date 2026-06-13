import pytest
from cryptography.fernet import Fernet

from sync_manager import create_app, db
from sync_manager.models import User


@pytest.fixture()
def app(tmp_path):
    app = create_app({
        "TESTING": True,
        "WTF_CSRF_ENABLED": False,
        "SECRET_KEY": "test",
        "CREDENTIAL_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "SQLALCHEMY_DATABASE_URI": "sqlite:///{}".format(tmp_path / "test.db"),
        "SYNC_EXECUTION_MODE": "inline",
        "CELERY": {
            "broker_url": "memory://",
            "result_backend": "cache+memory://",
            "task_always_eager": True,
            "task_eager_propagates": True,
            "task_ignore_result": True,
        },
    })
    with app.app_context():
        db.create_all()
        user = User(username="admin", role="administrator")
        user.set_password("password")
        db.session.add(user)
        db.session.commit()
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()
