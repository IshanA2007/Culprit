# Synthetic search canary: the only detector for the silent zero-results fault.
#
# A CloudWatch Synthetics canary GETs a known-good search URL on a schedule and
# asserts the results marker is present and the no-results marker is absent (see
# canary/python/search_smoke.py). When search silently returns zero results
# (HTTP 200, no exception), the handler raises, SuccessPercent drops, and the
# tcf-prod-search-canary alarm fires.

# --- Artifact bucket ---------------------------------------------------------
# Synthetics writes screenshots/HAR/logs per run here. The bucket is locked
# down: all public access blocked, no public policy.

resource "aws_s3_bucket" "canary_artifacts" {
  # Bucket names are globally unique; suffix with the account id to avoid
  # collisions across AWS accounts.
  bucket = "${local.name_prefix}-canary-artifacts-${data.aws_caller_identity.current.account_id}"
  tags   = local.common_tags
}

resource "aws_s3_bucket_public_access_block" "canary_artifacts" {
  bucket = aws_s3_bucket.canary_artifacts.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Expire run artifacts so the bucket doesn't grow without bound: a rate(5 minutes)
# canary writes ~8,640 objects/month (screenshots, HAR, logs), none needed long.
resource "aws_s3_bucket_lifecycle_configuration" "canary_artifacts" {
  bucket = aws_s3_bucket.canary_artifacts.id

  rule {
    id     = "expire-canary-artifacts"
    status = "Enabled"

    filter {} # all objects

    expiration {
      days = 30
    }
  }
}

# --- Canary execution role (least privilege) ---------------------------------
# Synthetics canaries run on Lambda under the hood, so the role is assumed by
# lambda.amazonaws.com. The inline policy grants ONLY what the canary needs:
#   * write artifacts to (only) this bucket + read its location,
#   * create/write its own CloudWatch Logs,
#   * publish metrics restricted to the CloudWatchSynthetics namespace.

resource "aws_iam_role" "canary" {
  name               = "${local.name_prefix}-search-canary-role"
  assume_role_policy = data.aws_iam_policy_document.canary_assume.json
  tags               = local.common_tags
}

data "aws_iam_policy_document" "canary_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "canary" {
  name   = "${local.name_prefix}-search-canary-policy"
  role   = aws_iam_role.canary.id
  policy = data.aws_iam_policy_document.canary.json
}

data "aws_iam_policy_document" "canary" {
  # Write run artifacts into this bucket only.
  statement {
    sid       = "WriteArtifacts"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.canary_artifacts.arn}/*"]
  }

  # Synthetics resolves the bucket region before writing.
  statement {
    sid       = "GetBucketLocation"
    effect    = "Allow"
    actions   = ["s3:GetBucketLocation"]
    resources = [aws_s3_bucket.canary_artifacts.arn]
  }

  # Canary's own log group/streams. Scoped to the Synthetics log-group prefix.
  statement {
    sid    = "WriteLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [
      "arn:aws:logs:*:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/cwsyn-${local.name_prefix}-search-smoke-*",
    ]
  }

  # Publish the SuccessPercent/Duration metrics, restricted to the Synthetics
  # namespace via condition so the role cannot write arbitrary metrics.
  statement {
    sid       = "PublishSyntheticsMetrics"
    effect    = "Allow"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["CloudWatchSynthetics"]
    }
  }
}

# --- Canary source bundle ----------------------------------------------------
# Synthetics expects the handler under python/ inside the zip. The handler file
# lives at canary/python/search_smoke.py; we zip the canary/ directory so the
# archive's internal layout is python/search_smoke.py.

data "archive_file" "canary" {
  type        = "zip"
  source_dir  = "${path.module}/canary"
  output_path = "${path.module}/.build/search_smoke.zip"
}

# --- The canary --------------------------------------------------------------

resource "aws_synthetics_canary" "search_smoke" {
  name                 = "${local.name_prefix}-search-smoke"
  artifact_s3_location = "s3://${aws_s3_bucket.canary_artifacts.bucket}/canary/search-smoke"
  execution_role_arn   = aws_iam_role.canary.arn
  handler              = "search_smoke.handler"

  # NOTE: runtime versions are periodically deprecated by AWS. Confirm this is
  # still a supported syn-python-selenium version at apply time and bump if the
  # provider/plan flags it. (List: aws synthetics describe-runtime-versions.)
  runtime_version = "syn-python-selenium-5.1"

  zip_file = data.archive_file.canary.output_path

  schedule {
    expression = var.canary_schedule_expression
  }

  run_config {
    # Search markers are passed as env vars so the reviewer can align them to
    # theCourseForum's current search template without editing the handler.
    environment_variables = {
      SEARCH_URL        = var.search_url
      RESULT_MARKER     = var.search_result_marker
      NO_RESULTS_MARKER = var.search_no_results_marker
    }
  }

  tags = local.common_tags
}
