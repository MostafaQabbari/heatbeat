# Security: threat model and controls

What this file does: lists what an attacker (or a mistake) could do, what stops it,
which test proves it, and what is NOT done yet. Method: STRIDE per component.
This is a development setup; the "Production" column says what would change.

## Assets
- Measurement data of customers' buildings (reveals presence and habits of residents)
- The ability to change heating parameters remotely (comfort, safety, equipment)
- User accounts and roles; audit trail of changes

## Trust boundaries
```
gateway --MQTT--> broker --> bridge --> database <-- API <--HTTPS-- browser (portal)
                    ^                                  |
                    +---- commands (bridge) <----------+
```

## Threats and controls

| STRIDE | Threat | Control in this project | Proof | Production |
|---|---|---|---|---|
| Spoofing | Gateway sends data as another gateway | Topic gateway must equal payload gateway; unknown (not provisioned) gateways are rejected; a plant belongs to one gateway | `test_ingest::test_gateway_cannot_speak_for_another_gateway`, `test_integration_storage::test_plant_cannot_be_taken_over_*` | One certificate or password per gateway + broker ACL (`hb/v1/<own id>/#` only), TLS |
| Spoofing | Forged API token | HS256 with a secret of at least 32 characters, algorithm pinned, required claims; API refuses to start with a short secret | `test_auth`, `test_api` | RS256 from an identity provider (ADR 0009) |
| Spoofing | Password guessing | scrypt hashes; same answer for unknown user and wrong password (a dummy hash is checked so the time is similar); scrypt makes each guess slow | `test_api::test_wrong_password_and_unknown_user_look_the_same` | Login-specific limit per IP/account, MFA |
| Tampering | Changing another tenant's data | Row-level security with `WITH CHECK`; API runs as restricted role | `test_integration_security::test_s12_*` | Same |
| Tampering | Dangerous parameter value | Writable flag and range checked by API AND gateway; four-eyes for big changes; heating off always needs approval | `test_commands`, `test_gateway`, `test_integration_commands` | Same, plus limits per plant type from the manufacturer |
| Tampering | Lost update by concurrent edits | Version check, one open command per parameter | `test_integration_commands::test_s16_*` | Same |
| Tampering | SQL injection | Only parameterised queries; dynamic column names through `psycopg.sql.Identifier` from a fixed list; ruff `S608` lint | ruff in CI | Same |
| Repudiation | "I did not change that" | Append-only audit table (trigger blocks UPDATE/DELETE), requester and approver stored, request id in every log line | `test_integration_commands` | Ship logs to a write-once store |
| Information disclosure | Reading another tenant's data | RLS + filtering views; uniform 404; API role cannot read the raw hypertable or `password_hash` | `test_integration_security` | Same |
| Information disclosure | Token stolen from the browser | Token in `sessionStorage` (closed with the tab), short expiry (60 min), strict CSP in nginx (no inline scripts, no third-party scripts) | `portal/nginx.conf` | HttpOnly SameSite cookie via a backend-for-frontend; refresh tokens |
| Information disclosure | Secrets in the repository | `.env` is git-ignored; only `.env.example` with placeholders; settings use `SecretStr` (not printed) | `test_config::test_password_is_hidden_when_printed` | Secret manager; secret scanning in CI |
| Denial of service | One tenant floods the API | Token bucket per tenant, 429 + Retry-After | `test_s17_*` | Also at the reverse proxy |
| Denial of service | Expensive queries | Time range and bucket size limits, cursor pagination, aggregate for long ranges | `test_api::test_series_limits_protect_the_database` | Statement timeout per role |
| Denial of service | Flood of MQTT messages | Broker in-flight window and queue limits; garbage is parked, not retried | `test_integration_pipeline::test_garbage_*` | Per-client rate limits in the broker |
| Elevation of privilege | Viewer changes parameters | Roles viewer < operator < admin checked on every route | `test_integration_commands::test_a_viewer_cannot_change_parameters` | Same |
| Elevation of privilege | Requester approves own change | Checked in code and by a database CHECK constraint | `test_integration_commands::test_four_eyes_*` | Same |

## Known gaps (honest list)
- Broker: anonymous access and no TLS in the development config (`mosquitto/mosquitto.conf` says so).
- The bridge and the workers connect as the table owner, not as a least-privilege role.
- No refresh tokens or token revocation; logout only forgets the token in the browser.
- The login route has no rate limit of its own yet (the per-tenant limit applies after login).
- The demo seed uses one password for all demo users (set by `HB_DEMO_PASSWORD`).
- CI audits dependencies (`pip-audit` for Python, `npm audit --audit-level=high` for the portal) and scans
  for secrets (gitleaks). Writing this file found three advisories: Vue <= 3.5.41 (server-side rendering XSS;
  not used here, upgraded to 3.5.43 anyway), Vitest <= 4.1.10 and its worker pool (test tooling only,
  upgraded to 4.1.11). One remains on purpose: ECharts < 6.1.0 (GHSA-fgmj-fm8m-jvvx) is an XSS in the
  tooltip of the *lines* series; the portal only uses *line* series, and ECharts 6 is a breaking upgrade,
  so it is planned rather than rushed.
- Images are pinned by tag, not by digest.
