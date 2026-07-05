# CloudWatch alarm suite for theCourseForum2 (prod).
#
# theCourseForum has ZERO alarms today -- this is their first monitoring. Each
# alarm both fires (alarm_actions) AND clears (ok_actions) to the SNS topic:
# the OK transition is what lets Culprit auto-resolve an incident when the
# metric recovers, so it is deliberately wired on every alarm.
#
# The first five alarms below are a HARD PARITY CONTRACT with
# harness/snsfeed.py (the ALARMS dict): alarm_name / namespace / metric_name /
# statistic / comparison_operator / threshold / period / evaluation_periods
# MUST match the recorded SNS fixtures byte-for-byte. Do not edit those fields
# without regenerating the fixtures. Dimension VALUES are variables; only the
# example values live in snsfeed.py.

locals {
  name_prefix = "tcf-prod"

  common_tags = {
    Project   = "theCourseForum2"
    ManagedBy = "terraform"
    AddedBy   = "culprit-incident-response"
  }
}

# =============================================================================
# Fixture-parity alarms (must match harness/snsfeed.py ALARMS exactly)
# =============================================================================

# --- ALB p95 latency: N+1 / cartesian-join / dropped-index regressions --------
# PARITY: AWS/ApplicationELB / TargetResponseTime / p95 / GreaterThanThreshold
#         / 2.0 / 60 / 3
resource "aws_cloudwatch_metric_alarm" "alb_target_response_time" {
  alarm_name          = "${local.name_prefix}-alb-target-response-time"
  alarm_description   = "ALB p95 target response time is elevated (latency regression)."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "TargetResponseTime"
  extended_statistic  = "p95"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 2.0
  period              = 60
  evaluation_periods  = 3
  # Latency has no datapoints when there is no traffic; don't page on quiet.
  treat_missing_data = "missing"
  dimensions         = { LoadBalancer = var.alb_arn_suffix }
  alarm_actions      = [aws_sns_topic.alarms.arn]
  ok_actions         = [aws_sns_topic.alarms.arn]
  tags               = local.common_tags
}

# --- ALB 5xx: the gunicorn-worker-OOM 502s (SIGKILLed worker, Sentry blind) ---
# PARITY: AWS/ApplicationELB / HTTPCode_ELB_5XX_Count / Sum / GreaterThanThreshold
#         / 5 / 60 / 2
resource "aws_cloudwatch_metric_alarm" "alb_5xx" {
  alarm_name          = "${local.name_prefix}-alb-5xx"
  alarm_description   = "Elevated ELB/target 5xx responses from the ALB."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_ELB_5XX_Count"
  statistic           = "Sum"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 5
  period              = 60
  evaluation_periods  = 2
  # No 5xx datapoint emitted means no errors; absence is healthy here.
  treat_missing_data = "notBreaching"
  dimensions         = { LoadBalancer = var.alb_arn_suffix }
  alarm_actions      = [aws_sns_topic.alarms.arn]
  ok_actions         = [aws_sns_topic.alarms.arn]
  tags               = local.common_tags
}

# --- ElastiCache health: the cachalot-wrapped Redis outage --------------------
# PARITY: AWS/ElastiCache / CurrConnections / Minimum / LessThanThreshold
#         / 1 / 60 / 2
resource "aws_cloudwatch_metric_alarm" "elasticache_health" {
  alarm_name          = "${local.name_prefix}-elasticache-health"
  alarm_description   = "ElastiCache node unreachable / connection failures (cachalot has no IGNORE_EXCEPTIONS)."
  namespace           = "AWS/ElastiCache"
  metric_name         = "CurrConnections"
  statistic           = "Minimum"
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  period              = 60
  evaluation_periods  = 2
  # A node that stops reporting is exactly the outage we want to catch:
  # missing data should page, not be silently tolerated.
  treat_missing_data = "breaching"
  dimensions         = { CacheClusterId = var.elasticache_cluster_id }
  alarm_actions      = [aws_sns_topic.alarms.arn]
  ok_actions         = [aws_sns_topic.alarms.arn]
  tags               = local.common_tags
}

# --- RDS connections: the db.t3.micro connection ceiling ----------------------
# PARITY: AWS/RDS / DatabaseConnections / Maximum / GreaterThanThreshold
#         / 80 / 60 / 2
resource "aws_cloudwatch_metric_alarm" "rds_connections" {
  alarm_name          = "${local.name_prefix}-rds-connections"
  alarm_description   = "RDS database connections near the db.t3.micro ceiling."
  namespace           = "AWS/RDS"
  metric_name         = "DatabaseConnections"
  statistic           = "Maximum"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 80
  period              = 60
  evaluation_periods  = 2
  treat_missing_data  = "missing"
  dimensions          = { DBInstanceIdentifier = var.rds_instance_id }
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
  tags                = local.common_tags
}

# --- Search canary: the ONLY detector for silent zero-results -----------------
# search-silent-zero-results is invisible to error monitoring (HTTP 200, no
# exception). The Synthetics canary (search_canary.tf) runs a known-good query
# on a schedule; when it returns zero results the handler raises, SuccessPercent
# drops below 100, and this alarm fires.
# PARITY: CloudWatchSynthetics / SuccessPercent / Average / LessThanThreshold
#         / 100 / 300 / 1  (CanaryName = tcf-prod-search-smoke)
resource "aws_cloudwatch_metric_alarm" "search_canary" {
  alarm_name          = "${local.name_prefix}-search-canary"
  alarm_description   = "Search-smoke synthetic canary: a known-good query returned zero results."
  namespace           = "CloudWatchSynthetics"
  metric_name         = "SuccessPercent"
  statistic           = "Average"
  comparison_operator = "LessThanThreshold"
  threshold           = 100
  period              = 300
  evaluation_periods  = 1
  # If the canary itself stops reporting, treat that as a failure to detect.
  treat_missing_data = "breaching"
  dimensions         = { CanaryName = aws_synthetics_canary.search_smoke.name }
  alarm_actions      = [aws_sns_topic.alarms.arn]
  ok_actions         = [aws_sns_topic.alarms.arn]
  tags               = local.common_tags
}

# =============================================================================
# Additional suite alarms (not in the fixtures, but part of the proposal)
# =============================================================================

# --- ALB unhealthy hosts: target group with failing health checks -------------
resource "aws_cloudwatch_metric_alarm" "alb_unhealthy_hosts" {
  alarm_name          = "${local.name_prefix}-alb-unhealthy-hosts"
  alarm_description   = "ALB target group has unhealthy hosts."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  statistic           = "Maximum"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  period              = 60
  evaluation_periods  = 2
  treat_missing_data  = "notBreaching"
  dimensions          = { LoadBalancer = var.alb_arn_suffix, TargetGroup = var.target_group_arn_suffix }
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
  tags                = local.common_tags
}

# --- ECS memory: the 0.5 vCPU / 2 GB task OOM ---------------------------------
resource "aws_cloudwatch_metric_alarm" "ecs_memory" {
  alarm_name          = "${local.name_prefix}-ecs-memory-utilization"
  alarm_description   = "ECS service memory utilization is high (OOM risk)."
  namespace           = "AWS/ECS"
  metric_name         = "MemoryUtilization"
  statistic           = "Average"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 85
  period              = 60
  evaluation_periods  = 3
  treat_missing_data  = "missing"
  dimensions          = { ClusterName = var.ecs_cluster_name, ServiceName = var.ecs_service_name }
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
  tags                = local.common_tags
}

# --- RDS CPU: sustained high database CPU -------------------------------------
resource "aws_cloudwatch_metric_alarm" "rds_cpu" {
  alarm_name          = "${local.name_prefix}-rds-cpu"
  alarm_description   = "RDS CPU utilization is high."
  namespace           = "AWS/RDS"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 85
  period              = 60
  evaluation_periods  = 3
  treat_missing_data  = "missing"
  dimensions          = { DBInstanceIdentifier = var.rds_instance_id }
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
  tags                = local.common_tags
}
