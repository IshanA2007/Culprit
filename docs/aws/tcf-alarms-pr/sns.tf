# Notification topic + HTTPS subscription to Culprit's /ingest/sns.
#
# OFFER-ONLY posture: this topic is a NOTIFICATION sink only. Every alarm
# publishes here; nothing subscribed to it mutates theCourseForum's infra.
# Culprit receives the delivery over HTTPS and produces a brief; remediation
# is always a human-approved action, never an automated alarm action.

data "aws_caller_identity" "current" {}

resource "aws_sns_topic" "alarms" {
  name = "${local.name_prefix}-alarms"
  tags = local.common_tags
}

# HTTPS subscription to Culprit's ingest endpoint.
#
# APPLY-ORDER GOTCHA: SNS first delivers a SubscriptionConfirmation message to
# the endpoint; the subscription stays "PendingConfirmation" until that token is
# echoed back. Culprit implements the handshake (POST /ingest/sns auto-confirms),
# so `endpoint_auto_confirms = true` lets `terraform apply` complete in one pass
# -- BUT ONLY IF Culprit is already deployed and reachable at the public HTTPS
# URL below when apply runs. Stand Culprit up first (see README).
resource "aws_sns_topic_subscription" "culprit" {
  topic_arn              = aws_sns_topic.alarms.arn
  protocol               = "https"
  endpoint               = var.culprit_ingest_sns_url
  endpoint_auto_confirms = true
}

# Allow CloudWatch to publish alarm state changes to this topic, scoped to this
# account. Least-privilege: only sns:Publish, only from cloudwatch.amazonaws.com,
# only for alarms owned by this account (SourceAccount condition), only to this
# one topic ARN.
resource "aws_sns_topic_policy" "alarms" {
  arn    = aws_sns_topic.alarms.arn
  policy = data.aws_iam_policy_document.alarms_topic.json
}

data "aws_iam_policy_document" "alarms_topic" {
  statement {
    sid       = "AllowCloudWatchAlarmsPublish"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alarms.arn]

    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}
