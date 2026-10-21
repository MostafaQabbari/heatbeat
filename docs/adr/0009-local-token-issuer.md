# ADR 0009: A small built-in token issuer instead of Keycloak (for now)

Status: accepted (2026-10-21)

## Context
The API needs login, roles and the tenant of the caller. The plan mentioned Keycloak.

## Decision
The API issues its own short-lived JWTs (HS256, 60 min) after checking an
scrypt-hashed password:
- the algorithm is pinned on decode (a token saying `alg: none` is rejected),
  expiry, issuer, subject, tenant and role are required claims
- wrong e-mail and wrong password give the same answer in the same time
- the secret comes from the environment (`HB_JWT_SECRET`, at least 32 characters);
  the API refuses to start without it

## Why not Keycloak now
One more service to run and configure for a demo with six users. The API only needs
"who is it, which tenant, which role", which is exactly what the token carries.

## What would change my mind
Single sign-on with customer identity providers, password reset flows, MFA, or several
services that must trust the same tokens: then an identity provider (Keycloak or a
managed one) issues RS256 tokens and the API only verifies them with the public key.
`decode_token` is the only place that would change.

## Known gaps
No refresh tokens, no revocation list (logout only deletes the token in the browser),
token kept in sessionStorage (see docs/security.md).
