# Graph Report - sync-data  (2026-06-23)

## Corpus Check
- 36 files · ~25,933 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 312 nodes · 668 edges · 17 communities (16 shown, 1 thin omitted)
- Extraction: 99% EXTRACTED · 1% INFERRED · 0% AMBIGUOUS · INFERRED: 5 edges (avg confidence: 0.5)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `d034b69b`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_Models and Audit Logs|Models and Audit Logs]]
- [[_COMMUNITY_Sync Engine|Sync Engine]]
- [[_COMMUNITY_Application and Workers|Application and Workers]]
- [[_COMMUNITY_Notifications and Security|Notifications and Security]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Authentication and Connections|Authentication and Connections]]
- [[_COMMUNITY_Sync Engine Tests|Sync Engine Tests]]
- [[_COMMUNITY_Job Task Execution|Job Task Execution]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]

## God Nodes (most connected - your core abstractions)
1. `DatabaseConnection` - 41 edges
2. `SyncJob` - 33 edges
3. `create()` - 28 edges
4. `record_audit()` - 27 edges
5. `User` - 26 edges
6. `synchronize()` - 19 edges
7. `FakeInspector` - 18 edges
8. `Core Features` - 18 edges
9. `encrypt_secret()` - 13 edges
10. `decrypt_secret()` - 13 edges

## Surprising Connections (you probably didn't know these)
- `FakeResponse` --uses--> `User`  [INFERRED]
  tests/test_notifications.py → sync_manager/models.py
- `test_viewer_cannot_access_notification_settings()` --calls--> `User`  [EXTRACTED]
  tests/test_notifications.py → sync_manager/models.py
- `test_viewer_cannot_create_connection()` --calls--> `User`  [EXTRACTED]
  tests/test_security.py → sync_manager/models.py
- `FakeInspector` --uses--> `User`  [INFERRED]
  tests/test_sync_engine.py → sync_manager/models.py
- `test_connection_can_be_disabled()` --calls--> `DatabaseConnection`  [EXTRACTED]
  tests/test_security.py → sync_manager/models.py

## Import Cycles
- 3-file cycle: `sync_manager/__init__.py -> sync_manager/auth/routes.py -> sync_manager/audit.py -> sync_manager/__init__.py`
- 3-file cycle: `sync_manager/__init__.py -> sync_manager/auth/routes.py -> sync_manager/models.py -> sync_manager/__init__.py`
- 3-file cycle: `sync_manager/__init__.py -> sync_manager/connections/routes.py -> sync_manager/audit.py -> sync_manager/__init__.py`
- 3-file cycle: `sync_manager/__init__.py -> sync_manager/connections/routes.py -> sync_manager/sync/engine.py -> sync_manager/__init__.py`
- 3-file cycle: `sync_manager/__init__.py -> sync_manager/connections/routes.py -> sync_manager/models.py -> sync_manager/__init__.py`
- 3-file cycle: `sync_manager/__init__.py -> sync_manager/main/routes.py -> sync_manager/models.py -> sync_manager/__init__.py`
- 3-file cycle: `sync_manager/__init__.py -> sync_manager/notifications/routes.py -> sync_manager/audit.py -> sync_manager/__init__.py`
- 3-file cycle: `sync_manager/__init__.py -> sync_manager/notifications/service.py -> sync_manager/models.py -> sync_manager/__init__.py`
- 3-file cycle: `sync_manager/__init__.py -> sync_manager/cli.py -> sync_manager/models.py -> sync_manager/__init__.py`
- 4-file cycle: `sync_manager/__init__.py -> sync_manager/auth/routes.py -> sync_manager/audit.py -> sync_manager/models.py -> sync_manager/__init__.py`
- 4-file cycle: `sync_manager/__init__.py -> sync_manager/connections/routes.py -> sync_manager/audit.py -> sync_manager/models.py -> sync_manager/__init__.py`
- 4-file cycle: `sync_manager/__init__.py -> sync_manager/connections/routes.py -> sync_manager/notifications/service.py -> sync_manager/models.py -> sync_manager/__init__.py`
- 4-file cycle: `sync_manager/__init__.py -> sync_manager/connections/routes.py -> sync_manager/sync/engine.py -> sync_manager/models.py -> sync_manager/__init__.py`
- 4-file cycle: `sync_manager/__init__.py -> sync_manager/notifications/routes.py -> sync_manager/audit.py -> sync_manager/models.py -> sync_manager/__init__.py`
- 4-file cycle: `sync_manager/__init__.py -> sync_manager/notifications/routes.py -> sync_manager/notifications/service.py -> sync_manager/models.py -> sync_manager/__init__.py`

## Communities (17 total, 1 thin omitted)

### Community 0 - "Models and Audit Logs"
Cohesion: 0.07
Nodes (36): DatabaseConnection, SyncJob, Make exact, successfully previewed parent rows available to later FK checks., _remember_planned_parent_values(), test_audit_logs_paginate(), test_dashboard_jobs_paginate(), test_dashboard_shows_short_upsert_label(), test_limited_full_sync_preview_continues_after_saved_primary_key() (+28 more)

### Community 1 - "Sync Engine"
Cohesion: 0.12
Nodes (39): _advance_postgresql_sequence(), _append_drop_detail(), _candidate_scalar_values(), _collect_mysql_warnings(), connection_engine(), _cursor_value(), _dependency_analysis(), dependency_cycle_tables() (+31 more)

### Community 2 - "Application and Workers"
Cohesion: 0.36
Nodes (9): _connection_fields(), _connection_form_values(), create(), delete(), edit(), index(), _mapping_rule_summary(), test() (+1 more)

### Community 3 - "Notifications and Security"
Cohesion: 0.12
Nodes (23): settings(), test_message(), get_settings(), _send_in_app(), send_telegram_message(), NotificationSettings, decrypt_secret(), encrypt_secret() (+15 more)

### Community 4 - "Community 4"
Cohesion: 0.10
Nodes (29): dependency_report(), expand_tables_with_dependencies(), incremental_checkpoint_status(), SyncProfile, _all_table_names(), checkpoint_status(), create(), _dependency_parent_tables() (+21 more)

### Community 5 - "Authentication and Connections"
Cohesion: 0.09
Nodes (22): login(), logout(), audit_logs(), dashboard(), _paginate(), init_notification_executor(), record_audit(), init_celery() (+14 more)

### Community 6 - "Sync Engine Tests"
Cohesion: 0.09
Nodes (9): FakeInspector, test_dependency_report_returns_safe_order_and_blocked_tables(), test_discover_tables_exposes_only_engine_supported_checkpoint_columns(), test_discover_tables_returns_source_metadata_and_target_presence(), test_expand_tables_with_dependencies_adds_parents_first(), test_order_tables_by_dependency(), test_order_tables_by_dependency_handles_cycles(), test_validate_table_blocks_missing_dependencies() (+1 more)

### Community 7 - "Job Task Execution"
Cohesion: 0.23
Nodes (12): notify_async(), sync_message(), enqueue_job(), enqueue_jobs_in_order(), execute_sync_job(), Run a dependency-ordered batch sequentially in either execution mode., run_sync_job(), test_dependency_ordered_jobs_use_one_immutable_celery_chain() (+4 more)

### Community 14 - "Community 14"
Cohesion: 0.06
Nodes (34): 10. Primary-Key-Based Upsert, 11. Synchronization History, 12. Audit Trail, 13. Failed Record Logging, 14. Retry Failed Records, 15. Monitoring Dashboard, 16. Data Comparison Report, 17. Telegram Notification Service (+26 more)

### Community 15 - "Community 15"
Cohesion: 0.29
Nodes (6): Background execution, Core behavior, Database Sync Manager, Local setup, Operational notes, SWOT

## Knowledge Gaps
- **34 isolated node(s):** `graphify`, `Core behavior`, `Operational notes`, `SWOT`, `Local setup` (+29 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **1 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `SyncJob` connect `Models and Audit Logs` to `Sync Engine`, `Application and Workers`, `Notifications and Security`, `Community 4`, `Authentication and Connections`, `Sync Engine Tests`, `Job Task Execution`?**
  _High betweenness centrality (0.122) - this node is a cross-community bridge._
- **Why does `DatabaseConnection` connect `Models and Audit Logs` to `Application and Workers`, `Notifications and Security`, `Community 4`, `Authentication and Connections`, `Sync Engine Tests`, `Job Task Execution`?**
  _High betweenness centrality (0.114) - this node is a cross-community bridge._
- **Why does `User` connect `Authentication and Connections` to `Models and Audit Logs`, `Notifications and Security`, `Sync Engine Tests`, `Job Task Execution`?**
  _High betweenness centrality (0.098) - this node is a cross-community bridge._
- **Are the 2 inferred relationships involving `User` (e.g. with `FakeResponse` and `FakeInspector`) actually correct?**
  _`User` has 2 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Build safe SQLAlchemy predicates from persisted filter rules; never accept SQL t`, `Resume a limited full sync by primary key without changing normal full-sync sema`, `Report missing direct-copy parent rows before a target write can hit an FK viola` to the rest of the system?**
  _41 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Models and Audit Logs` be split into smaller, more focused modules?**
  _Cohesion score 0.06862745098039216 - nodes in this community are weakly interconnected._
- **Should `Sync Engine` be split into smaller, more focused modules?**
  _Cohesion score 0.11951219512195121 - nodes in this community are weakly interconnected._