from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import db
from ..audit import record_audit
from ..models import User
from ..security import roles_required

bp = Blueprint("users", __name__, url_prefix="/users")
ROLES = ("administrator", "operator", "viewer")


@bp.get("/")
@login_required
@roles_required("administrator")
def index():
    users = db.session.scalars(db.select(User).order_by(User.username)).all()
    return render_template("users/index.html", users=users)


@bp.route("/new", methods=("GET", "POST"))
@login_required
@roles_required("administrator")
def create():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "viewer")
        if role not in ROLES:
            flash("Invalid role.", "danger")
        elif not username:
            flash("Username is required.", "danger")
        elif len(password) < 8:
            flash("Password must contain at least 8 characters.", "danger")
        elif db.session.scalar(db.select(User).filter_by(username=username)):
            flash("Username already exists.", "danger")
        else:
            user = User(username=username, role=role)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            record_audit("user.created", "user={} role={}".format(user.username, user.role))
            flash("User created.", "success")
            return redirect(url_for("users.index"))
    return render_template("users/form.html", roles=ROLES)


@bp.post("/<int:user_id>/role")
@login_required
@roles_required("administrator")
def update_role(user_id):
    user = db.get_or_404(User, user_id)
    role = request.form.get("role", "")
    if role not in ROLES:
        flash("Invalid role.", "danger")
    elif user.id == current_user.id and role != "administrator":
        flash("You cannot remove your own administrator role.", "danger")
    else:
        old_role = user.role
        user.role = role
        db.session.commit()
        record_audit("user.role_changed", "user={} {}->{}".format(user.username, old_role, role))
        flash("User role updated.", "success")
    return redirect(url_for("users.index"))


@bp.post("/<int:user_id>/toggle")
@login_required
@roles_required("administrator")
def toggle_active(user_id):
    user = db.get_or_404(User, user_id)
    if user.id == current_user.id:
        flash("You cannot disable your own account.", "danger")
    else:
        user.is_active_account = not user.is_active_account
        db.session.commit()
        action = "enabled" if user.is_active_account else "disabled"
        record_audit("user.{}".format(action), "user={}".format(user.username))
        flash("User {}.".format(action), "success")
    return redirect(url_for("users.index"))


@bp.post("/<int:user_id>/password")
@login_required
@roles_required("administrator")
def reset_password(user_id):
    user = db.get_or_404(User, user_id)
    password = request.form.get("password", "")
    if len(password) < 8:
        flash("Password must contain at least 8 characters.", "danger")
    else:
        user.set_password(password)
        db.session.commit()
        record_audit("user.password_reset", "user={}".format(user.username))
        flash("Password reset.", "success")
    return redirect(url_for("users.index"))
