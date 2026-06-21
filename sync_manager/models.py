from datetime import datetime, timezone
import json

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from flask_login import UserMixin

from . import db, login_manager

password_hasher = PasswordHasher()


def utcnow():
    return datetime.now(timezone.utc)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="viewer")
    is_active_account = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    @property
    def is_active(self):
        return self.is_active_account

    def set_password(self, password):
        self.password_hash = password_hasher.hash(password)

    def check_password(self, password):
        try:
            return password_hasher.verify(self.password_hash, password)
        except (InvalidHashError, VerifyMismatchError):
            return False


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


class DatabaseConnection(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    database_type = db.Column(db.String(20), nullable=False, default="mysql")
    usage_role = db.Column(db.String(20), nullable=False, default="both")
    environment = db.Column(db.String(20), nullable=False, default="development")
    host = db.Column(db.String(255), nullable=False)
    port = db.Column(db.Integer, nullable=False, default=3306)
    database_name = db.Column(db.String(100), nullable=False)
    username = db.Column(db.String(100), nullable=False)
    encrypted_password = db.Column(db.Text, nullable=False)
    fk_mapping_rules = db.Column(db.Text)
    is_enabled = db.Column(db.Boolean, nullable=False, default=True)
    test_status = db.Column(db.String(20), nullable=False, default="untested")
    last_tested_at = db.Column(db.DateTime(timezone=True))
    last_test_error = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)


class SyncJob(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    source_connection_id = db.Column(db.Integer, db.ForeignKey("database_connection.id"), nullable=False)
    target_connection_id = db.Column(db.Integer, db.ForeignKey("database_connection.id"), nullable=False)
    table_name = db.Column(db.String(128), nullable=False)
    sync_mode = db.Column(db.String(20), nullable=False, default="insert_only")
    filter_rules = db.Column(db.Text)
    incremental_column = db.Column(db.String(128))
    cycle_sync = db.Column(db.Boolean, nullable=False, default=False)
    status = db.Column(db.String(30), nullable=False, default="pending")
    inserted_count = db.Column(db.Integer, nullable=False, default=0)
    updated_count = db.Column(db.Integer, nullable=False, default=0)
    skipped_count = db.Column(db.Integer, nullable=False, default=0)
    failed_count = db.Column(db.Integer, nullable=False, default=0)
    drop_details = db.Column(db.Text)
    source_count = db.Column(db.Integer)
    error_message = db.Column(db.Text)
    started_at = db.Column(db.DateTime(timezone=True))
    finished_at = db.Column(db.DateTime(timezone=True))
    initiated_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    source = db.relationship("DatabaseConnection", foreign_keys=[source_connection_id])
    target = db.relationship("DatabaseConnection", foreign_keys=[target_connection_id])
    initiated_by = db.relationship("User")

    @property
    def processed_count(self):
        return self.inserted_count + self.updated_count + self.skipped_count + self.failed_count

    @property
    def dropped_rows(self):
        if not self.drop_details:
            return []
        try:
            value = json.loads(self.drop_details)
        except Exception:
            return []
        return value if isinstance(value, list) else []

    def add_drop_detail(self, detail):
        details = self.dropped_rows
        details.append(detail)
        self.drop_details = json.dumps(details, ensure_ascii=False, default=str)

    @property
    def sync_mode_label(self):
        return (
            "Add new records only"
            if (self.sync_mode or "insert_only") == "insert_only"
            else "Add new records and update existing ones"
        )

    @property
    def filters(self):
        if not self.filter_rules:
            return []
        try:
            value = json.loads(self.filter_rules)
        except Exception:
            return []
        return value if isinstance(value, list) else []

    @property
    def progress_percent(self):
        if self.status == "completed":
            return 100
        if not self.source_count:
            return 100 if self.status == "completed" else 0
        return min(100, round((self.processed_count / self.source_count) * 100))

    @property
    def duration_seconds(self):
        if not self.started_at:
            return None
        end = self.finished_at or utcnow()
        started = self.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return max(0, (end - started).total_seconds())


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    action = db.Column(db.String(100), nullable=False)
    details = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    user = db.relationship("User")


class SyncCheckpoint(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    source_connection_id = db.Column(db.Integer, db.ForeignKey("database_connection.id"), nullable=False)
    target_connection_id = db.Column(db.Integer, db.ForeignKey("database_connection.id"), nullable=False)
    table_name = db.Column(db.String(128), nullable=False)
    filter_signature = db.Column(db.String(64), nullable=False, default="")
    incremental_column = db.Column(db.String(128), nullable=False)
    cursor_value = db.Column(db.Text, nullable=False)
    cursor_primary_key = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    __table_args__ = (db.UniqueConstraint("source_connection_id", "target_connection_id", "table_name", "filter_signature", "incremental_column", name="uq_sync_checkpoint_scope"),)


class SyncProfile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    source_connection_id = db.Column(db.Integer, db.ForeignKey("database_connection.id"), nullable=False)
    target_connection_id = db.Column(db.Integer, db.ForeignKey("database_connection.id"), nullable=False)
    table_names = db.Column(db.Text, nullable=False)
    filter_rules = db.Column(db.Text)
    incremental_columns = db.Column(db.Text)
    sync_mode = db.Column(db.String(20), nullable=False, default="insert_only")
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    source = db.relationship("DatabaseConnection", foreign_keys=[source_connection_id])
    target = db.relationship("DatabaseConnection", foreign_keys=[target_connection_id])
    created_by = db.relationship("User")

    @property
    def tables(self):
        try: return json.loads(self.table_names)
        except Exception: return []

    @property
    def table_filters(self):
        try: return json.loads(self.filter_rules or "{}")
        except Exception: return {}

    @property
    def table_incremental_columns(self):
        try: return json.loads(self.incremental_columns or "{}")
        except Exception: return {}


class NotificationSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True, default=1)
    encrypted_bot_token = db.Column(db.Text)
    chat_id = db.Column(db.String(100))
    is_enabled = db.Column(db.Boolean, nullable=False, default=False)
    notify_success = db.Column(db.Boolean, nullable=False, default=True)
    notify_failure = db.Column(db.Boolean, nullable=False, default=True)
    notify_connection_failure = db.Column(db.Boolean, nullable=False, default=True)
    notify_retry = db.Column(db.Boolean, nullable=False, default=True)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
