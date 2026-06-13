import json

from sync_manager import db
from sync_manager.models import NotificationSettings, User
from sync_manager.notifications.service import send_telegram_message
from sync_manager.security import decrypt_secret, encrypt_secret


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return json.dumps({"ok": True}).encode()


def test_admin_can_save_encrypted_telegram_settings(app, client):
    client.post("/auth/login", data={"username": "admin", "password": "password"})
    response = client.post(
        "/settings/notifications/",
        data={
            "bot_token": "123456:secret-token",
            "chat_id": "-100123",
            "is_enabled": "on",
            "notify_success": "on",
            "notify_failure": "on",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Notification settings saved" in response.data
    with app.app_context():
        settings = db.session.get(NotificationSettings, 1)
        assert settings.encrypted_bot_token != "123456:secret-token"
        assert decrypt_secret(settings.encrypted_bot_token) == "123456:secret-token"
        assert settings.chat_id == "-100123"
        assert settings.is_enabled is True


def test_viewer_cannot_access_notification_settings(app, client):
    with app.app_context():
        viewer = User(username="notify-viewer", role="viewer")
        viewer.set_password("password123")
        db.session.add(viewer)
        db.session.commit()
    client.post("/auth/login", data={"username": "notify-viewer", "password": "password123"})
    assert client.get("/settings/notifications/").status_code == 403


def test_send_telegram_message_posts_expected_payload(app, monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode())
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("sync_manager.notifications.service.urlopen", fake_urlopen)
    with app.app_context():
        settings = NotificationSettings(
            id=1,
            encrypted_bot_token=encrypt_secret("123:token"),
            chat_id="456",
            is_enabled=True,
        )
        db.session.merge(settings)
        db.session.commit()
        success, detail = send_telegram_message("Hello")

    assert success is True
    assert detail == "Telegram message sent."
    assert captured["url"].endswith("/bot123:token/sendMessage")
    assert captured["body"] == {"chat_id": "456", "text": "Hello"}
    assert captured["timeout"] == 5


def test_disabled_notifications_do_not_call_network(app, monkeypatch):
    monkeypatch.setattr(
        "sync_manager.notifications.service.urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network called")),
    )
    with app.app_context():
        settings = NotificationSettings(id=1, is_enabled=False)
        db.session.merge(settings)
        db.session.commit()
        success, detail = send_telegram_message("Hello")
    assert success is False
    assert "disabled" in detail
