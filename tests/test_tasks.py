from sync_manager import db
from sync_manager.models import DatabaseConnection, SyncJob, User
from sync_manager.sync.tasks import enqueue_job, enqueue_jobs_in_order, execute_sync_job, run_sync_job


def test_enqueue_runs_inline_by_default(app, monkeypatch):
    executed = []
    monkeypatch.setattr("sync_manager.sync.tasks.execute_sync_job", lambda job_id: executed.append(job_id))
    with app.app_context():
        enqueue_job(42)
    assert executed == [42]


def test_enqueue_uses_celery_when_configured(app, monkeypatch):
    queued = []
    inspector = type("Inspector", (), {"ping": lambda self: {"worker": {"ok": "pong"}}})()
    monkeypatch.setattr(run_sync_job.app.control, "inspect", lambda timeout: inspector)
    monkeypatch.setattr(run_sync_job, "delay", lambda job_id: queued.append(job_id))
    app.config["SYNC_EXECUTION_MODE"] = "celery"
    with app.app_context():
        enqueue_job(42)
    assert queued == [42]


def test_dependency_ordered_jobs_use_one_immutable_celery_chain(app, monkeypatch):
    dispatched = []

    class ChainResult:
        def apply_async(self):
            dispatched.append(True)
            return "queued-chain"

    inspector = type("Inspector", (), {"ping": lambda self: {"worker": {"ok": "pong"}}})()
    monkeypatch.setattr(run_sync_job.app.control, "inspect", lambda timeout: inspector)
    monkeypatch.setattr(run_sync_job, "si", lambda job_id: "job-{}".format(job_id))
    monkeypatch.setattr("sync_manager.sync.tasks.chain", lambda *signatures: dispatched.append(signatures) or ChainResult())
    app.config["SYNC_EXECUTION_MODE"] = "celery"

    with app.app_context():
        result = enqueue_jobs_in_order([4, 8, 15])

    assert result == "queued-chain"
    assert dispatched == [("job-4", "job-8", "job-15"), True]


def test_celery_is_configured_for_eager_tests(app):
    celery_app = app.extensions["celery"]
    assert celery_app.conf.task_always_eager is True
    assert celery_app.conf.broker_url == "memory://"


def test_missing_job_task_returns_without_error(app):
    with app.app_context():
        assert run_sync_job.run(99999) is None


def test_task_skips_job_that_is_no_longer_pending(app, monkeypatch):
    with app.app_context():
        user = db.session.scalar(db.select(User).filter_by(username="admin"))
        source = DatabaseConnection(name="skip-source", host="localhost", database_name="source", username="root", encrypted_password="x")
        target = DatabaseConnection(name="skip-target", host="localhost", database_name="target", username="root", encrypted_password="x")
        job = SyncJob(source=source, target=target, table_name="customers", status="failed", initiated_by=user)
        db.session.add(job)
        db.session.commit()
        job_id = job.id
    synchronize_calls = []
    monkeypatch.setattr("sync_manager.sync.tasks.synchronize", lambda *args, **kwargs: synchronize_calls.append(args))
    with app.app_context():
        run_sync_job.run(job_id)
    assert synchronize_calls == []


def test_inline_execution_does_not_remove_request_session(app, monkeypatch):
    with app.app_context():
        user = db.session.scalar(db.select(User).filter_by(username="admin"))
        source = DatabaseConnection(name="inline-source", host="localhost", database_name="source", username="root", encrypted_password="x")
        target = DatabaseConnection(name="inline-target", host="localhost", database_name="target", username="root", encrypted_password="x")
        job = SyncJob(source=source, target=target, table_name="customers", initiated_by=user)
        db.session.add(job)
        db.session.commit()
        job_id = job.id

        def complete(sync_job, batch_size):
            sync_job.status = "completed"
            db.session.commit()

        monkeypatch.setattr("sync_manager.sync.tasks.synchronize", complete)
        monkeypatch.setattr("sync_manager.sync.tasks.notify_async", lambda *args: None)
        execute_sync_job(job_id)

        assert user.username == "admin"
        assert db.session.get(SyncJob, job_id).status == "completed"
