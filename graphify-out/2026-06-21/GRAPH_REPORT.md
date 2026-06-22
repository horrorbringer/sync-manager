# Graph Report - sync-data  (2026-06-21)

## Corpus Check
- 36 files · ~23,567 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 285 nodes · 599 edges · 18 communities (17 shown, 1 thin omitted)
- Extraction: 100% EXTRACTED · 0% INFERRED · 0% AMBIGUOUS · INFERRED: 2 edges (avg confidence: 0.5)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `b03f7b11`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_Models and Audit Logs|Models and Audit Logs]]
- [[_COMMUNITY_Sync Engine|Sync Engine]]
- [[_COMMUNITY_Application and Workers|Application and Workers]]
- [[_COMMUNITY_Notifications and Security|Notifications and Security]]
- [[_COMMUNITY_Sync Profiles and Routes|Sync Profiles and Routes]]
- [[_COMMUNITY_Authentication and Connections|Authentication and Connections]]
- [[_COMMUNITY_Sync Engine Tests|Sync Engine Tests]]
- [[_COMMUNITY_Job Task Execution|Job Task Execution]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]

## God Nodes (most connected - your core abstractions)
1. `DatabaseConnection` - 37 edges
2. `SyncJob` - 30 edges
3. `record_audit()` - 27 edges
4. `User` - 24 edges
5. `create()` - 23 edges
6. `Core Features` - 18 edges
7. `synchronize()` - 17 edges
8. `FakeInspector` - 14 edges
9. `encrypt_secret()` - 13 edges
10. `decrypt_secret()` - 13 edges

## Surprising Connections (you probably didn't know these)
- `FakeResponse` --uses--> `User`  [INFERRED]
  tests/test_notifications.py → sync_manager/models.py
- `test_viewer_cannot_access_notification_settings()` --calls--> `User`  [EXTRACTED]
  tests/test_notifications.py → sync_manager/models.py
- `test_viewer_cannot_create_connection()` --calls--> `User`  [EXTRACTED]
  tests/test_security.py → sync_manager/models.py
- `test_disabled_user_cannot_login()` --calls--> `User`  [EXTRACTED]
  tests/test_users.py → sync_manager/models.py
- `test_viewer_cannot_access_user_management()` --calls--> `User`  [EXTRACTED]
  tests/test_users.py → sync_manager/models.py

## Import Cycles
- 3-file cycle: `sync_manager/__init__.py -> sync_manager/connections/routes.py -> sync_manager/audit.py -> sync_manager/__init__.py`
- 3-file cycle: `sync_manager/__init__.py -> sync_manager/connections/routes.py -> sync_manager/sync/engine.py -> sync_manager/__init__.py`
- 3-file cycle: `sync_manager/__init__.py -> sync_manager/connections/routes.py -> sync_manager/models.py -> sync_manager/__init__.py`
- 3-file cycle: `sync_manager/__init__.py -> sync_manager/auth/routes.py -> sync_manager/audit.py -> sync_manager/__init__.py`
- 3-file cycle: `sync_manager/__init__.py -> sync_manager/auth/routes.py -> sync_manager/models.py -> sync_manager/__init__.py`
- 3-file cycle: `sync_manager/__init__.py -> sync_manager/notifications/service.py -> sync_manager/models.py -> sync_manager/__init__.py`
- 3-file cycle: `sync_manager/__init__.py -> sync_manager/main/routes.py -> sync_manager/models.py -> sync_manager/__init__.py`
- 3-file cycle: `sync_manager/__init__.py -> sync_manager/cli.py -> sync_manager/models.py -> sync_manager/__init__.py`
- 3-file cycle: `sync_manager/__init__.py -> sync_manager/notifications/routes.py -> sync_manager/audit.py -> sync_manager/__init__.py`
- 4-file cycle: `sync_manager/__init__.py -> sync_manager/connections/routes.py -> sync_manager/audit.py -> sync_manager/models.py -> sync_manager/__init__.py`
- 4-file cycle: `sync_manager/__init__.py -> sync_manager/connections/routes.py -> sync_manager/notifications/service.py -> sync_manager/models.py -> sync_manager/__init__.py`
- 4-file cycle: `sync_manager/__init__.py -> sync_manager/connections/routes.py -> sync_manager/sync/engine.py -> sync_manager/models.py -> sync_manager/__init__.py`
- 4-file cycle: `sync_manager/__init__.py -> sync_manager/auth/routes.py -> sync_manager/audit.py -> sync_manager/models.py -> sync_manager/__init__.py`
- 4-file cycle: `sync_manager/__init__.py -> sync_manager/notifications/routes.py -> sync_manager/notifications/service.py -> sync_manager/models.py -> sync_manager/__init__.py`
- 4-file cycle: `sync_manager/__init__.py -> sync_manager/notifications/routes.py -> sync_manager/audit.py -> sync_manager/models.py -> sync_manager/__init__.py`

## Communities (18 total, 1 thin omitted)

### Community 0 - "Models and Audit Logs"
Cohesion: 0.09
Nodes (29): DatabaseConnection, SyncJob, test_connection_can_be_disabled(), test_referenced_connection_cannot_be_deleted(), _failed_job(), test_completed_job_reports_full_progress_even_without_row_changes(), test_completed_job_shows_dropped_row_details(), test_discovery_renders_searchable_table_metadata() (+21 more)

### Community 1 - "Sync Engine"
Cohesion: 0.14
Nodes (32): _advance_postgresql_sequence(), _append_drop_detail(), _candidate_scalar_values(), _collect_mysql_warnings(), connection_engine(), _cursor_value(), _dependency_analysis(), dependency_cycle_tables() (+24 more)

### Community 2 - "Application and Workers"
Cohesion: 0.12
Nodes (11): init_notification_executor(), init_celery(), register_commands(), create_app(), User, app(), test_viewer_cannot_access_notification_settings(), test_viewer_cannot_create_connection() (+3 more)

### Community 3 - "Notifications and Security"
Cohesion: 0.15
Nodes (19): settings(), test_message(), get_settings(), _send_in_app(), send_telegram_message(), NotificationSettings, decrypt_secret(), encrypt_secret() (+11 more)

### Community 4 - "Sync Profiles and Routes"
Cohesion: 0.13
Nodes (21): dependency_report(), expand_tables_with_dependencies(), incremental_checkpoint_status(), SyncProfile, _all_table_names(), create(), _dry_run_with_filters(), duplicate_profile() (+13 more)

### Community 5 - "Authentication and Connections"
Cohesion: 0.14
Nodes (19): login(), logout(), _connection_fields(), _connection_form_values(), create(), delete(), edit(), index() (+11 more)

### Community 6 - "Sync Engine Tests"
Cohesion: 0.10
Nodes (8): FakeInspector, test_dependency_report_returns_safe_order_and_blocked_tables(), test_discover_tables_returns_source_metadata_and_target_presence(), test_expand_tables_with_dependencies_adds_parents_first(), test_order_tables_by_dependency(), test_order_tables_by_dependency_handles_cycles(), test_validate_table_blocks_missing_dependencies(), test_validate_table_blocks_unmappable_foreign_keys()

### Community 7 - "Job Task Execution"
Cohesion: 0.27
Nodes (9): notify_async(), sync_message(), enqueue_job(), execute_sync_job(), run_sync_job(), test_enqueue_runs_inline_by_default(), test_enqueue_uses_celery_when_configured(), test_inline_execution_does_not_remove_request_session() (+1 more)

### Community 14 - "Community 14"
Cohesion: 0.06
Nodes (34): 10. Primary-Key-Based Upsert, 11. Synchronization History, 12. Audit Trail, 13. Failed Record Logging, 14. Retry Failed Records, 15. Monitoring Dashboard, 16. Data Comparison Report, 17. Telegram Notification Service (+26 more)

### Community 15 - "Community 15"
Cohesion: 0.29
Nodes (6): Background execution, Core behavior, Database Sync Manager, Local setup, Operational notes, SWOT

### Community 17 - "Community 17"
Cohesion: 0.15
Nodes (7): audit_logs(), dashboard(), _paginate(), AuditLog, test_audit_logs_paginate(), test_dashboard_jobs_paginate(), test_dashboard_shows_short_upsert_label()

## Knowledge Gaps
- **34 isolated node(s):** `graphify`, `Core behavior`, `Operational notes`, `SWOT`, `Local setup` (+29 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **1 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `SyncJob` connect `Models and Audit Logs` to `Sync Engine`, `Notifications and Security`, `Sync Profiles and Routes`, `Authentication and Connections`, `Job Task Execution`, `Community 17`?**
  _High betweenness centrality (0.108) - this node is a cross-community bridge._
- **Why does `DatabaseConnection` connect `Models and Audit Logs` to `Notifications and Security`, `Sync Profiles and Routes`, `Authentication and Connections`, `Job Task Execution`, `Community 17`?**
  _High betweenness centrality (0.085) - this node is a cross-community bridge._
- **Why does `User` connect `Application and Workers` to `Models and Audit Logs`, `Notifications and Security`, `Authentication and Connections`, `Job Task Execution`, `Community 17`?**
  _High betweenness centrality (0.071) - this node is a cross-community bridge._
- **What connects `Build safe SQLAlchemy predicates from persisted filter rules; never accept SQL t`, `graphify`, `Core behavior` to the rest of the system?**
  _35 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Models and Audit Logs` be split into smaller, more focused modules?**
  _Cohesion score 0.09487179487179487 - nodes in this community are weakly interconnected._
- **Should `Sync Engine` be split into smaller, more focused modules?**
  _Cohesion score 0.13725490196078433 - nodes in this community are weakly interconnected._
- **Should `Application and Workers` be split into smaller, more focused modules?**
  _Cohesion score 0.12318840579710146 - nodes in this community are weakly interconnected._