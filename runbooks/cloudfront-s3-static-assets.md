---
id: cloudfront-s3-static-assets
title: CloudFront / S3 static assets broken
summary: Pages render but CSS/JS/images 403 or 404 — a collectstatic that didn't publish, an S3/CloudFront misconfig, or a stale cache. The app is up; only static delivery is broken.
failure_mode: static-assets
symptoms:
  - HTML loads but the site looks unstyled; browser console shows 403/404 for static files
  - Static asset URLs point at S3/CloudFront paths that don't exist or are access-denied
  - Started after a deploy whose release task ran collectstatic, or after a CloudFront/S3 change
checks:
  - Fetch a failing asset URL directly and read the status (403 = permissions/OAI, 404 = not published)
  - Confirm the release task's collectstatic step succeeded and published the new hashed filenames
  - Check CloudFront cache / invalidation and the S3 bucket policy / origin-access config
steps:
  - Confirm HTML is 200 but assets fail (view source, fetch a failing asset, read the status)
  - Read the status — 403 is S3 policy / CloudFront origin-access, 404 is not-published or a changed hashed name
  - Re-run collectstatic to publish the current hashed assets to S3
  - Invalidate the CloudFront distribution and revert any bucket-policy/distribution change that broke access
  - Hard-refresh and confirm assets load
rollback: Republish static assets (re-run collectstatic to S3) and/or invalidate the CloudFront distribution; if a CloudFront/S3 config change broke access, revert it. No application code culprit unless the deploy changed static handling.
---

# CloudFront / S3 static assets broken

theCourseForum serves static files from **S3** behind **CloudFront**; the release
task runs `collectstatic`. When HTML renders but assets 403/404, the app is
healthy and only static delivery is broken — a distinct, low-severity failure
mode that is easy to misread as "the site is down".

**Culprit offers this runbook; it never executes it.** A human performs the fix.

## Diagnosis

1. Confirm HTML is 200 but assets fail (view source → asset URLs → fetch one).
2. Read the status: **403** → S3 bucket policy / CloudFront origin-access; **404**
   → the asset was never published (collectstatic) or the hashed name changed.

## Fix (human-run)

1. Re-run `collectstatic` to publish the current hashed assets to S3.
2. Invalidate the CloudFront distribution so it stops serving stale/missing paths.
3. If a bucket-policy or distribution change caused it, revert that change.
4. Hard-refresh and confirm assets load.
