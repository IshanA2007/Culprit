---
id: secrets-manager-env-drift
title: Secrets Manager / environment drift
summary: A rotated, expired, or changed secret in Secrets Manager no longer matches what the app expects — DB password, Cognito app-client secret, or an API key — so a whole integration fails at once with auth/permission errors.
failure_mode: config-drift
symptoms:
  - An integration that worked yesterday now fails with auth/permission errors (invalid credentials, access denied)
  - Failure is scoped to one dependency (DB, Cognito, an external API), not the whole app
  - Correlates with a secret rotation, a Terraform/config change, or a new task definition picking up changed env
checks:
  - Identify which integration is failing and the exact error (auth vs connectivity)
  - Compare the value the app is using against the current Secrets Manager value (rotation skew?)
  - Check whether a recent deploy/task-definition change altered which secret version or env var is injected
steps:
  - Scope the failure to a single integration (DB, Cognito, external API)
  - Confirm it is an auth/permission error, not connectivity, to point at the credential rather than the network
  - Compare the app's in-use value against the current Secrets Manager version to spot a rotation skew
  - Realign — restore the previous secret version if a rotation broke it, or update the app config to the current value
  - Redeploy/restart the ECS task so it injects the correct secret version and confirm recovery
rollback: Realign the secret and the app — refresh the app to the current secret value (or restore the previous secret version if a rotation broke it) and redeploy the task so it picks up the correct env. This is a config fix, not a code culprit, unless a deploy changed secret wiring.
---

# Secrets Manager / environment drift

theCourseForum keeps credentials in **Secrets Manager** (DB password, Cognito
app-client secret, API keys) injected into the ECS task. When a secret is rotated
or changed but the running task holds the old value — or vice versa — a whole
integration fails at once with **auth/permission** errors, while everything that
doesn't touch that secret keeps working.

**Culprit offers this runbook; it never executes it.** A human performs the fix.

## Diagnosis

1. Scope it: which single integration is failing (DB, Cognito, external API)?
2. Auth/permission error, not connectivity → suspect the credential, not the
   network.
3. Compare the app's in-use value against the current Secrets Manager version —
   a rotation that the task didn't pick up is the classic skew.

## Fix (human-run)

1. Realign: restore the previous secret version if a rotation broke it, or update
   the app config to the current value.
2. Redeploy / restart the ECS task so it injects the correct secret version.
3. Confirm the failing integration recovers.

## Notes

- Auth/login-specific drift (Cognito app-client secret) overlaps with
  `cognito-auth-outage`; DB-credential drift presents like
  `rds-outage-conn-exhaustion` but with an auth error, not a connection refusal.
