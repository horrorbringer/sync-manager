from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required

from .. import db
from ..audit import record_audit
from ..security import encrypt_secret, roles_required
from .service import get_settings, send_telegram_message

bp = Blueprint("notifications", __name__, url_prefix="/settings/notifications")


@bp.route("/", methods=("GET", "POST"))
@login_required
@roles_required("administrator")
def settings():
    item = get_settings()
    if request.method == "POST":
        token = request.form.get("bot_token", "").strip()
        if token:
            item.encrypted_bot_token = encrypt_secret(token)
        item.chat_id = request.form.get("chat_id", "").strip()
        item.is_enabled = "is_enabled" in request.form
        item.notify_success = "notify_success" in request.form
        item.notify_failure = "notify_failure" in request.form
        item.notify_connection_failure = "notify_connection_failure" in request.form
        item.notify_retry = "notify_retry" in request.form
        db.session.commit()
        record_audit("notifications.updated", "telegram enabled={}".format(item.is_enabled))
        flash("Notification settings saved.", "success")
        return redirect(url_for("notifications.settings"))
    return render_template("notifications/settings.html", settings=item)


@bp.post("/test")
@login_required
@roles_required("administrator")
def test_message():
    success, detail = send_telegram_message("Sync Manager test notification", require_enabled=False)
    record_audit("notifications.test", "success={} detail={}".format(success, detail))
    flash(detail, "success" if success else "danger")
    return redirect(url_for("notifications.settings"))
