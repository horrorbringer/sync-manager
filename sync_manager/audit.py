from flask_login import current_user

from . import db
from .models import AuditLog


def record_audit(action, details=None, user_id=None):
    actor_id = user_id
    if actor_id is None and current_user.is_authenticated:
        actor_id = current_user.id
    db.session.add(AuditLog(user_id=actor_id, action=action, details=details))
    db.session.commit()

