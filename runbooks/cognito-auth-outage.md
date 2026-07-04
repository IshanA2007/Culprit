---
id: cognito-auth-outage
title: Cognito auth / login outage
summary: Users cannot log in — the Cognito email-OTP flow or the custom JWT backend is failing. Login and any authenticated action errors while anonymous browsing still works.
failure_mode: auth-outage
symptoms:
  - Login (email OTP) fails or hangs; authenticated actions (voting, saving) 401/500 while public pages work
  - Errors trace to the Cognito integration or the custom JWT backend, not to content views
  - Spike correlates with a Cognito change, an expired secret/app-client credential, or a deploy touching auth
checks:
  - Confirm anonymous pages work but authenticated flows fail (isolates auth from a site-wide outage)
  - Check Cognito user-pool / app-client health and that the JWT signing config + secrets are current
  - Check the window for a change to the auth backend, JWT settings, or Cognito app-client credentials
steps:
  - Confirm the split — public pages 200, authenticated flows failing — to isolate auth from a site-wide outage
  - Localize the cause (Cognito user-pool/app-client, JWT backend, expired secret, or a deploy touching auth)
  - Apply the fix — revert the auth/JWT change, refresh the Cognito app-client secret, or escalate a Cognito-side degradation
  - Confirm a full login round-trip succeeds before closing
rollback: Restore the working auth configuration — revert the auth/JWT code change or refresh the Cognito app-client secret; if Cognito itself is degraded, wait out / escalate the AWS-side issue. Auth config lives in Secrets Manager, so a rotated/expired secret is a prime suspect.
---

# Cognito auth / login outage

theCourseForum authenticates with **Cognito email-OTP** and a **custom JWT
backend**; auth config and secrets live in Secrets Manager. When auth breaks,
anonymous browsing keeps working but login and every authenticated action fail —
a distinctive, partial outage.

**Culprit offers this runbook; it never executes it.** A human performs the recovery.

## Diagnosis

1. Confirm the split: public pages 200, authenticated flows failing → auth-scoped,
   not a site-wide outage.
2. Localize: Cognito user-pool/app-client degraded? JWT backend raising? An
   expired or rotated app-client secret in Secrets Manager? A deploy that touched
   the auth path?

## Fix (human-run)

- **Code/config regression** → revert the auth or JWT change.
- **Secret drift** → refresh the Cognito app-client secret in Secrets Manager (see
  `secrets-manager-env-drift`).
- **Cognito-side degradation** → escalate / wait out the AWS issue; there is no
  code culprit.
- Confirm a full login round-trip succeeds before closing.
