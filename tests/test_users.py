from sync_manager import db
from sync_manager.models import User


def test_administrator_can_create_user(app, client):
    client.post("/auth/login", data={"username": "admin", "password": "password"})
    response = client.post(
        "/users/new",
        data={"username": "operator1", "password": "password123", "role": "operator"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"User created" in response.data
    with app.app_context():
        user = db.session.scalar(db.select(User).filter_by(username="operator1"))
        assert user.role == "operator"
        assert user.is_active_account is True


def test_administrator_cannot_disable_self(app, client):
    client.post("/auth/login", data={"username": "admin", "password": "password"})
    with app.app_context():
        admin_id = db.session.scalar(db.select(User.id).filter_by(username="admin"))
    response = client.post("/users/{}/toggle".format(admin_id), follow_redirects=True)
    assert b"cannot disable your own account" in response.data
    with app.app_context():
        assert db.session.get(User, admin_id).is_active_account is True


def test_viewer_cannot_access_user_management(app, client):
    with app.app_context():
        viewer = User(username="viewer2", role="viewer")
        viewer.set_password("password123")
        db.session.add(viewer)
        db.session.commit()
    client.post("/auth/login", data={"username": "viewer2", "password": "password123"})
    assert client.get("/users/").status_code == 403


def test_disabled_user_cannot_login(app, client):
    with app.app_context():
        user = User(username="disabled", role="operator", is_active_account=False)
        user.set_password("password123")
        db.session.add(user)
        db.session.commit()
    response = client.post(
        "/auth/login",
        data={"username": "disabled", "password": "password123"},
        follow_redirects=True,
    )
    assert b"Invalid username or password" in response.data
    assert b"Synchronization jobs" not in response.data
