from flask import Flask
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from dotenv import load_dotenv
from sqlalchemy import inspect, text

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "auth.login"
csrf = CSRFProtect()


def create_app(test_config=None):
    load_dotenv()
    app = Flask(
        __name__,
        instance_relative_config=True,
        template_folder="../templates",
        static_folder="../static",
    )
    app.config.from_mapping(
        SECRET_KEY="dev-only-change-me",
        SQLALCHEMY_DATABASE_URI="sqlite:///sync_manager.db",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        CREDENTIAL_ENCRYPTION_KEY=None,
        SYNC_BATCH_SIZE=500,
        SYNC_EXECUTION_MODE="inline",
        CELERY={
            "broker_url": "redis://localhost:6379/0",
            "result_backend": "redis://localhost:6379/1",
            "task_ignore_result": True,
            "task_track_started": True,
            "task_acks_late": True,
            "task_reject_on_worker_lost": True,
            "worker_prefetch_multiplier": 1,
            "task_soft_time_limit": 3300,
            "task_time_limit": 3600,
            "broker_connection_retry_on_startup": True,
        },
    )
    app.config.from_prefixed_env()
    if test_config:
        app.config.update(test_config)
    app.config["SYNC_EXECUTION_MODE"] = str(app.config["SYNC_EXECUTION_MODE"]).lower()
    if app.config["SYNC_EXECUTION_MODE"] not in {"inline", "celery"}:
        raise ValueError("SYNC_EXECUTION_MODE must be 'inline' or 'celery'")

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)

    from .celery_app import init_celery
    init_celery(app)

    from .auth.routes import bp as auth_bp
    from .users.routes import bp as users_bp
    from .connections.routes import bp as connections_bp
    from .main.routes import bp as main_bp
    from .notifications.routes import bp as notifications_bp
    from .sync.routes import bp as sync_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(notifications_bp)
    app.register_blueprint(connections_bp)
    app.register_blueprint(sync_bp)

    from .cli import register_commands
    register_commands(app)

    from .models import NotificationSettings, SyncCheckpoint, SyncProfile
    from .notifications.service import init_notification_executor
    with app.app_context():
        NotificationSettings.__table__.create(db.engine, checkfirst=True)
        SyncCheckpoint.__table__.create(db.engine, checkfirst=True)
        SyncProfile.__table__.create(db.engine, checkfirst=True)
        if inspect(db.engine).has_table("user"):
            user_columns = {column["name"] for column in inspect(db.engine).get_columns("user")}
            if "is_active_account" not in user_columns:
                with db.engine.begin() as connection:
                    connection.execute(
                        text("ALTER TABLE user ADD COLUMN is_active_account BOOLEAN NOT NULL DEFAULT 1")
                    )
        if inspect(db.engine).has_table("database_connection"):
            connection_columns = {
                column["name"] for column in inspect(db.engine).get_columns("database_connection")
            }
            connection_upgrades = {
                "database_type": "ALTER TABLE database_connection ADD COLUMN database_type VARCHAR(20) NOT NULL DEFAULT 'mysql'",
                "usage_role": "ALTER TABLE database_connection ADD COLUMN usage_role VARCHAR(20) NOT NULL DEFAULT 'both'",
                "environment": "ALTER TABLE database_connection ADD COLUMN environment VARCHAR(20) NOT NULL DEFAULT 'development'",
                "is_enabled": "ALTER TABLE database_connection ADD COLUMN is_enabled BOOLEAN NOT NULL DEFAULT 1",
                "test_status": "ALTER TABLE database_connection ADD COLUMN test_status VARCHAR(20) NOT NULL DEFAULT 'untested'",
                "last_tested_at": "ALTER TABLE database_connection ADD COLUMN last_tested_at DATETIME",
                "last_test_error": "ALTER TABLE database_connection ADD COLUMN last_test_error TEXT",
                "fk_mapping_rules": "ALTER TABLE database_connection ADD COLUMN fk_mapping_rules TEXT",
            }
            for column_name, statement in connection_upgrades.items():
                if column_name not in connection_columns:
                    with db.engine.begin() as connection:
                        connection.execute(text(statement))
        if inspect(db.engine).has_table("sync_job"):
            columns = {column["name"] for column in inspect(db.engine).get_columns("sync_job")}
            if "source_count" not in columns:
                with db.engine.begin() as connection:
                    connection.execute(text("ALTER TABLE sync_job ADD COLUMN source_count INTEGER"))
            if "drop_details" not in columns:
                with db.engine.begin() as connection:
                    connection.execute(text("ALTER TABLE sync_job ADD COLUMN drop_details TEXT"))
            if "skipped_count" not in columns:
                with db.engine.begin() as connection:
                    connection.execute(
                        text("ALTER TABLE sync_job ADD COLUMN skipped_count INTEGER NOT NULL DEFAULT 0")
                    )
            if "sync_mode" not in columns:
                with db.engine.begin() as connection:
                    connection.execute(
                        text("ALTER TABLE sync_job ADD COLUMN sync_mode VARCHAR(20) NOT NULL DEFAULT 'insert_only'")
                    )
            if "cycle_sync" not in columns:
                with db.engine.begin() as connection:
                    connection.execute(
                        text("ALTER TABLE sync_job ADD COLUMN cycle_sync BOOLEAN NOT NULL DEFAULT 0")
                    )
            if "filter_rules" not in columns:
                with db.engine.begin() as connection:
                    connection.execute(text("ALTER TABLE sync_job ADD COLUMN filter_rules TEXT"))
            if "incremental_column" not in columns:
                with db.engine.begin() as connection:
                    connection.execute(text("ALTER TABLE sync_job ADD COLUMN incremental_column VARCHAR(128)"))
            if "row_limit" not in columns:
                with db.engine.begin() as connection:
                    connection.execute(text("ALTER TABLE sync_job ADD COLUMN row_limit INTEGER"))
    init_notification_executor(app)

    @app.template_filter("compact_datetime")
    def compact_datetime(value):
        return value.strftime("%b %d, %Y %H:%M") if value else "-"

    @app.template_filter("duration")
    def duration(value):
        if value is None:
            return "-"
        if value < 60:
            return "{:.1f}s".format(value)
        minutes, seconds = divmod(int(value), 60)
        return "{}m {}s".format(minutes, seconds)
    return app
