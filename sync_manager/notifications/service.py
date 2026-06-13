import json
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import current_app

from .. import db
from ..models import NotificationSettings
from ..security import decrypt_secret


def init_notification_executor(app):
    app.extensions["notification_executor"] = ThreadPoolExecutor(
        max_workers=2,
        thread_name_prefix="telegram-notifier",
    )


def get_settings():
    settings = db.session.get(NotificationSettings, 1)
    if settings is None:
        settings = NotificationSettings(id=1)
        db.session.add(settings)
        db.session.commit()
    return settings


def send_telegram_message(message, require_enabled=True):
    settings = get_settings()
    if require_enabled and not settings.is_enabled:
        return False, "Telegram notifications are disabled."
    if not settings.encrypted_bot_token or not settings.chat_id:
        return False, "Telegram bot token and chat ID are required."

    token = decrypt_secret(settings.encrypted_bot_token)
    request = Request(
        "https://api.telegram.org/bot{}/sendMessage".format(token),
        data=json.dumps({"chat_id": settings.chat_id, "text": message}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode())
        if not payload.get("ok"):
            return False, payload.get("description", "Telegram rejected the message.")
        return True, "Telegram message sent."
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode()).get("description", str(exc))
        except (ValueError, AttributeError):
            detail = str(exc)
        return False, detail
    except (URLError, TimeoutError, ValueError) as exc:
        return False, str(exc)


def _send_in_app(app, event, message):
    with app.app_context():
        settings = get_settings()
        event_flags = {
            "sync_success": settings.notify_success,
            "sync_failure": settings.notify_failure,
            "connection_failure": settings.notify_connection_failure,
            "sync_retry": settings.notify_retry,
        }
        if not settings.is_enabled or not event_flags.get(event, False):
            return
        success, detail = send_telegram_message(message)
        if not success:
            app.logger.warning("Telegram notification failed: %s", detail)
        db.session.remove()


def notify_async(event, message):
    app = current_app._get_current_object()
    return app.extensions["notification_executor"].submit(_send_in_app, app, event, message)


def sync_message(job, successful, error=None):
    status = "COMPLETED" if successful else "FAILED"
    lines = [
        "Synchronization {}".format(status),
        "Job: #{}".format(job.id),
        "Table: {}".format(job.table_name),
        "Mode: {}".format(
            "Add new records only" if job.sync_mode == "insert_only" else "Add new records and update existing ones"
        ),
        "Cycle sync: {}".format("Yes" if job.cycle_sync else "No"),
        "Direction: {} -> {}".format(job.source.name, job.target.name),
        "Inserted: {}".format(job.inserted_count),
        "Updated: {}".format(job.updated_count),
        "Skipped: {}".format(job.skipped_count),
        "Dropped: {}".format(job.failed_count),
    ]
    if job.duration_seconds is not None:
        lines.append("Duration: {:.1f}s".format(job.duration_seconds))
    if error:
        lines.append("Error: {}".format(str(error)[:500]))
    return "\n".join(lines)
