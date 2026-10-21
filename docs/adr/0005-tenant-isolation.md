# ADR 0005: Tenant isolation in the database (row-level security + filtering views)

Status: accepted (2026-10-21)

## Context
Several customers (tenants) share one database. A single forgotten
`WHERE tenant_id = ...` in one query would show one customer's data to another
(scenario S12). Isolation should not depend on every developer remembering it.

## Decision
1. The API runs every request in a transaction as the restricted role
   `heatbeat_api` (`SET LOCAL ROLE`) with `app.tenant_id` set (`set_config(..., true)`,
   transaction-local, so a pooled connection can never keep a previous tenant).
2. Relational tables (`plant`, `site`, `gateway`, `app_user`, `alarm*`, `command`,
   `parameter*`, `recommendation`) have **row-level security** policies
   `tenant_id = current_tenant()`, also for writes (`WITH CHECK`).
3. **Finding:** TimescaleDB 2.17 refuses row-level security on a compressed hypertable
   ("compression cannot be used on table with row security", in both orders). So the
   API role gets **no privilege at all** on `measurement` and `measurement_1h`, only on two
   views (`measurement_visible`, `measurement_1h_visible`) that join `plant` and filter
   by `current_tenant()`. The views are `security_barrier` views, so no user-supplied
   function can see rows before the filter.
4. No tenant set → `current_tenant()` is NULL → no rows (secure default).
5. The API role can never read `password_hash` (column-level grant).
6. A plant of another tenant gives the same 404 as a plant that does not exist.

## Consequences
- Tests prove it: a query without any tenant filter sees only its own rows; the raw
  hypertable gives "permission denied"; and a mutation test switches RLS off to show
  the check WOULD catch a missing policy (`tests/test_integration_security.py`).
- The ingest bridge and workers run as the table owner (they serve all tenants).
  Next step: a dedicated `heatbeat_ingest` role with only the rights it needs.
- In the Docker image the owner is a superuser; in production it would not be.

Sources: DDIA 2nd ed., ch. 7 "Sharding", Multitenancy (p. 254-255); PostgreSQL
documentation "Row Security Policies"; verified against TimescaleDB 2.17.2.
