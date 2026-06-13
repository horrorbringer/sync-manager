from sync_manager.sync import engine


class FakeInspector:
    def __init__(self, tables, columns=None, primary_keys=None, foreign_keys=None, unique_constraints=None):
        self.tables = tables
        self.columns = columns or {}
        self.primary_keys = primary_keys or {}
        self.foreign_keys = foreign_keys or {}
        self.unique_constraints = unique_constraints or {}

    def get_table_names(self):
        return self.tables

    def get_columns(self, table_name):
        return self.columns.get(table_name, [])

    def get_pk_constraint(self, table_name):
        return {"constrained_columns": self.primary_keys.get(table_name, [])}

    def get_foreign_keys(self, table_name):
        return self.foreign_keys.get(table_name, [])

    def get_unique_constraints(self, table_name):
        return self.unique_constraints.get(table_name, [])


def test_discover_tables_returns_source_metadata_and_target_presence(monkeypatch):
    source_engine = object()
    target_engine = object()
    source_inspector = FakeInspector(
        ["events", "customers"],
        columns={"customers": [{"name": "id"}, {"name": "email"}], "events": [{"name": "event_id"}]},
        primary_keys={"customers": ["id"]},
        foreign_keys={
            "customers": [{"referred_table": "users", "constrained_columns": ["created_by"], "referred_columns": ["id"]}],
            "events": [],
        },
    )
    target_inspector = FakeInspector(["customers"], foreign_keys={})

    monkeypatch.setattr(engine, "connection_engine", lambda config: source_engine if config == "source" else target_engine)
    monkeypatch.setattr(engine, "inspect", lambda value: source_inspector if value is source_engine else target_inspector)

    result = engine.discover_tables("source", "target")

    assert result == [
            {
                "name": "customers",
                "column_count": 2,
                "primary_key": ["id"],
                "dependencies": ["users"],
                "mapping_columns": [],
                "mapping_preview": [
                    {
                        "column": "created_by",
                        "referred_table": "users",
                        "lookup_columns": [],
                        "explicit": False,
                        "display": "created_by -> users.id (direct copy)",
                    }
                ],
                "mapping_state": "heuristic",
                "mapping_errors": [],
                "target_exists": True,
            },
        {
            "name": "events",
            "column_count": 1,
            "primary_key": [],
            "dependencies": [],
            "mapping_columns": [],
            "mapping_preview": [],
            "mapping_state": "mapped",
            "mapping_errors": [],
            "target_exists": False,
        },
    ]


def test_order_tables_by_dependency(monkeypatch):
    source_engine = object()
    inspector = FakeInspector(
        ["users", "canned_responses", "tickets"],
        foreign_keys={
            "canned_responses": [{"referred_table": "users"}],
            "tickets": [{"referred_table": "canned_responses"}],
            "users": [],
        },
    )
    monkeypatch.setattr(engine, "connection_engine", lambda config: source_engine)
    monkeypatch.setattr(engine, "inspect", lambda value: inspector)

    assert engine.order_tables_by_dependency("source", ["tickets", "canned_responses", "users"]) == [
        "users",
        "canned_responses",
        "tickets",
    ]


def test_order_tables_by_dependency_handles_cycles(monkeypatch):
    source_engine = object()
    inspector = FakeInspector(
        ["ticket_categories", "tickets"],
        foreign_keys={
            "ticket_categories": [{"referred_table": "tickets"}],
            "tickets": [{"referred_table": "ticket_categories"}],
        },
    )
    monkeypatch.setattr(engine, "connection_engine", lambda config: source_engine)
    monkeypatch.setattr(engine, "inspect", lambda value: inspector)

    ordered = engine.order_tables_by_dependency("source", ["ticket_categories", "tickets"])
    assert ordered == ["ticket_categories", "tickets"]
    assert engine.dependency_cycle_tables("source", ["ticket_categories", "tickets"]) == [
        "ticket_categories",
        "tickets",
    ]


def test_dependency_report_returns_safe_order_and_blocked_tables(monkeypatch):
    source_engine = object()
    inspector = FakeInspector(
        ["users", "canned_responses", "tickets", "ticket_categories"],
        foreign_keys={
            "canned_responses": [{"referred_table": "users"}],
            "tickets": [{"referred_table": "ticket_categories"}],
            "ticket_categories": [{"referred_table": "tickets"}],
            "users": [],
        },
    )
    monkeypatch.setattr(engine, "connection_engine", lambda config: source_engine)
    monkeypatch.setattr(engine, "inspect", lambda value: inspector)

    report = engine.dependency_report("source", ["ticket_categories", "tickets", "canned_responses", "users"])

    assert report["ordered_tables"] == ["users", "canned_responses"]
    assert report["cycle_tables"] == ["ticket_categories", "tickets"]


def test_expand_tables_with_dependencies_adds_parents_first(monkeypatch):
    source_engine = object()
    inspector = FakeInspector(
        ["departments", "users", "tickets"],
        foreign_keys={
            "users": [{"referred_table": "departments"}],
            "tickets": [{"referred_table": "users"}],
            "departments": [],
        },
    )
    monkeypatch.setattr(engine, "connection_engine", lambda config: source_engine)
    monkeypatch.setattr(engine, "inspect", lambda value: inspector)

    assert engine.expand_tables_with_dependencies("source", ["tickets"]) == [
        "departments",
        "users",
        "tickets",
    ]


def test_validate_table_blocks_missing_dependencies(monkeypatch):
    source_engine = object()
    target_engine = object()
    source_inspector = FakeInspector(
        ["users", "canned_responses"],
        columns={"users": [{"name": "id"}, {"name": "username"}], "canned_responses": [{"name": "id"}, {"name": "created_by"}]},
        primary_keys={"users": ["id"], "canned_responses": ["id"]},
        foreign_keys={"canned_responses": [{"referred_table": "users"}], "users": []},
    )
    target_inspector = FakeInspector(["users", "canned_responses"], foreign_keys={})
    monkeypatch.setattr(engine, "connection_engine", lambda config: source_engine if config == "source" else target_engine)
    monkeypatch.setattr(engine, "inspect", lambda value: source_inspector if value is source_engine else target_inspector)

    errors = engine.validate_table("source", "target", "canned_responses", selected_tables=["canned_responses"])

    assert any("depends on users" in error for error in errors)


def test_validate_table_blocks_unmappable_foreign_keys(monkeypatch):
    source_engine = object()
    target_engine = object()
    source_inspector = FakeInspector(
        ["users", "canned_responses"],
        columns={"users": [{"name": "id"}], "canned_responses": [{"name": "id"}, {"name": "created_by"}]},
        primary_keys={"users": ["id"], "canned_responses": ["id"]},
        foreign_keys={
            "canned_responses": [{"referred_table": "users", "constrained_columns": ["created_by"], "referred_columns": ["id"]}],
            "users": [],
        },
    )
    target_inspector = FakeInspector(
        ["users", "canned_responses"],
        columns={"users": [{"name": "id"}], "canned_responses": [{"name": "id"}, {"name": "created_by"}]},
        primary_keys={"users": ["id"], "canned_responses": ["id"]},
        foreign_keys={},
    )
    monkeypatch.setattr(engine, "connection_engine", lambda config: source_engine if config == "source" else target_engine)
    monkeypatch.setattr(engine, "inspect", lambda value: source_inspector if value is source_engine else target_inspector)

    errors = engine.validate_table("source", "target", "canned_responses", selected_tables=["users", "canned_responses"])

    assert errors == []


def test_remap_foreign_key_values_uses_stable_lookup_columns(tmp_path, monkeypatch):
    from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table, create_engine

    source_engine = create_engine("sqlite:///:memory:")
    target_engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()

    users = Table("users", metadata, Column("id", Integer, primary_key=True), Column("username", String, unique=True))
    categories = Table("ticket_categories", metadata, Column("id", Integer, primary_key=True), Column("title", String, unique=True))
    canned_responses = Table(
        "canned_responses",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("title", String),
        Column("category_id", Integer, ForeignKey("ticket_categories.id")),
        Column("created_by", Integer, ForeignKey("users.id")),
    )

    metadata.create_all(source_engine)
    metadata.create_all(target_engine)
    with source_engine.begin() as conn:
        conn.execute(users.insert(), [{"id": 1, "username": "admin"}])
        conn.execute(categories.insert(), [{"id": 1, "title": "General"}])
    with target_engine.begin() as conn:
        conn.execute(users.insert(), [{"id": 9, "username": "admin"}])
        conn.execute(categories.insert(), [{"id": 7, "title": "General"}])

    remapped = engine._remap_foreign_key_values(
        source_engine,
        target_engine,
        "canned_responses",
        [{"id": 3, "title": "Ack", "category_id": 1, "created_by": 1}],
    )

    assert remapped == [{"id": 3, "title": "Ack", "category_id": 1, "created_by": 1}]


def test_remap_foreign_key_values_honors_custom_mapping_rules(tmp_path):
    from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table, create_engine

    source_engine = create_engine("sqlite:///:memory:")
    target_engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()

    users = Table(
        "users",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("username", String),
        Column("external_key", String, unique=True),
    )
    canned_responses = Table(
        "canned_responses",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("created_by", Integer, ForeignKey("users.id")),
    )

    metadata.create_all(source_engine)
    metadata.create_all(target_engine)
    with source_engine.begin() as conn:
        conn.execute(users.insert(), [{"id": 1, "username": "admin", "external_key": "USR-001"}])
    with target_engine.begin() as conn:
        conn.execute(users.insert(), [{"id": 9, "username": "different", "external_key": "USR-001"}])

    source_config = type(
        "Config",
        (),
        {
            "name": "source",
            "fk_mapping_rules": '{"canned_responses": {"created_by": "external_key"}}',
        },
    )()
    plan = engine._foreign_key_mapping_plan(
        engine.inspect(source_engine),
        engine.inspect(target_engine),
        "canned_responses",
        mapping_rules=engine._load_mapping_rules(source_config),
    )
    remapped = engine._remap_foreign_key_values(
        source_engine,
        target_engine,
        "canned_responses",
        [{"id": 3, "created_by": 1}],
        mapping_plan=plan,
    )

    assert remapped == [{"id": 3, "created_by": 9}]


def test_remap_foreign_key_values_can_skip_orphan_rows(tmp_path):
    from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table, create_engine

    source_engine = create_engine("sqlite:///:memory:")
    target_engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()

    departments = Table(
        "departments",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("title", String, unique=True),
    )
    users = Table(
        "users",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("department_id", Integer, ForeignKey("departments.id")),
    )

    metadata.create_all(source_engine)
    metadata.create_all(target_engine)
    with source_engine.begin() as conn:
        conn.execute(departments.insert(), [{"id": 1, "title": "Sales"}])
        conn.execute(users.insert(), [{"id": 7, "department_id": 3}])
    with target_engine.begin() as conn:
        conn.execute(departments.insert(), [{"id": 9, "title": "Sales"}])

    remapped, skipped = engine._remap_foreign_key_values(
        source_engine,
        target_engine,
        "users",
        [{"id": 7, "department_id": 3}],
        allow_row_skips=True,
    )

    assert remapped == [{"id": 7, "department_id": 3}]
    assert skipped == []


def test_remap_foreign_key_values_normalizes_scalar_key_values(tmp_path):
    from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table, create_engine

    source_engine = create_engine("sqlite:///:memory:")
    target_engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()

    departments = Table(
        "departments",
        metadata,
        Column("id", String, primary_key=True),
        Column("title", String, unique=True),
    )
    users = Table(
        "users",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("department_id", Integer, ForeignKey("departments.id")),
    )

    metadata.create_all(source_engine)
    metadata.create_all(target_engine)
    with source_engine.begin() as conn:
        conn.execute(departments.insert(), [{"id": "003", "title": "Support"}])
        conn.execute(users.insert(), [{"id": 7, "department_id": 3}])
    with target_engine.begin() as conn:
        conn.execute(departments.insert(), [{"id": "003", "title": "Support"}])

    remapped, skipped = engine._remap_foreign_key_values(
        source_engine,
        target_engine,
        "users",
        [{"id": 7, "department_id": 3}],
        allow_row_skips=True,
    )

    assert remapped == [{"id": 7, "department_id": 3}]
    assert skipped == []


def test_remap_foreign_key_values_prefers_direct_primary_key_matches(tmp_path):
    from sqlalchemy import Column, ForeignKey, Integer, MetaData, Table, create_engine

    source_engine = create_engine("sqlite:///:memory:")
    target_engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()

    departments = Table("departments", metadata, Column("id", Integer, primary_key=True))
    users = Table(
        "users",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("department_id", Integer, ForeignKey("departments.id")),
    )

    metadata.create_all(source_engine)
    metadata.create_all(target_engine)
    with source_engine.begin() as conn:
        conn.execute(departments.insert(), [{"id": 3}])
        conn.execute(users.insert(), [{"id": 7, "department_id": 3}])
    with target_engine.begin() as conn:
        conn.execute(departments.insert(), [{"id": 3}])

    remapped, skipped = engine._remap_foreign_key_values(
        source_engine,
        target_engine,
        "users",
        [{"id": 7, "department_id": 3}],
        allow_row_skips=True,
    )

    assert remapped == [{"id": 7, "department_id": 3}]
    assert skipped == []


def test_remap_foreign_key_values_cycle_mode_keeps_source_value_when_target_missing(tmp_path):
    from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table, create_engine

    source_engine = create_engine("sqlite:///:memory:")
    target_engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()

    departments = Table(
        "departments",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("title", String, unique=True),
    )
    users = Table(
        "users",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("department_id", Integer, ForeignKey("departments.id")),
    )

    metadata.create_all(source_engine)
    metadata.create_all(target_engine)
    with source_engine.begin() as conn:
        conn.execute(departments.insert(), [{"id": 3, "title": "Support"}])
        conn.execute(users.insert(), [{"id": 7, "department_id": 3}])

    remapped, skipped = engine._remap_foreign_key_values(
        source_engine,
        target_engine,
        "users",
        [{"id": 7, "department_id": 3}],
        allow_row_skips=True,
        cycle_mode=True,
    )

    assert remapped == [{"id": 7, "department_id": 3}]
    assert skipped == []
