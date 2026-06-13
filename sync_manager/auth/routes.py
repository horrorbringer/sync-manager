from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_user, logout_user

from .. import db
from ..audit import record_audit
from ..models import User

bp = Blueprint("auth", __name__, url_prefix="/auth")


@bp.before_app_request
def enforce_active_account():
    if current_user.is_authenticated and not current_user.is_active_account:
        logout_user()
        flash("Your account has been disabled.", "danger")
        return redirect(url_for("auth.login"))


@bp.route("/login", methods=("GET", "POST"))
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        user = db.session.scalar(db.select(User).filter_by(username=username))
        if user and user.is_active_account and user.check_password(request.form.get("password", "")):
            login_user(user)
            record_audit("user.login", user_id=user.id)
            return redirect(url_for("main.dashboard"))
        flash("Invalid username or password.", "danger")
    return render_template("auth/login.html")


@bp.post("/logout")
def logout():
    if current_user.is_authenticated:
        record_audit("user.logout")
        logout_user()
    return redirect(url_for("auth.login"))
