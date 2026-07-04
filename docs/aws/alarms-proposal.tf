# Culprit — proposed CloudWatch alarm suite for theCourseForum2
#
# theCourseForum has ZERO alarms today (their iac/cloudwatch.tf is two log groups).
# This file is BOTH the second instrumentation PR for the pitch (HANDOFF §6) AND
# the source of truth for the alarm names / metrics / dimensions that the
# synthesized SNS fixtures replay (harness/snsfeed.py — the fixtures' Message
# bodies use exactly these AlarmNames + Trigger fields). It mirrors their iac
# style: a local name prefix, tagged resources, an SNS topic + HTTPS subscription.
#
# OFFER-ONLY posture holds end-to-end: these alarms only *notify* (SNS -> Culprit);
# no alarm action mutates infrastructure.

locals {
  name_prefix = "tcf-prod"
  common_tags = {
    Project   = "theCourseForum2"
    ManagedBy = "terraform"
    AddedBy   = "culprit-incident-response"
  }
}

# --- notification topic + HTTPS subscription to Culprit's /ingest/sns ---------

resource "aws_sns_topic" "alarms" {
  name = "${local.name_prefix}-alarms"
  tags = local.common_tags
}

resource "aws_sns_topic_subscription" "culprit" {
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "https"
  endpoint  = var.culprit_ingest_sns_url # e.g. https://culprit.example.com/ingest/sns
  # SNS sends a SubscriptionConfirmation first; Culprit completes the handshake.
  endpoint_auto_confirms = true
}

variable "culprit_ingest_sns_url" {
  type        = string
  description = "Culprit's POST /ingest/sns endpoint (Mode B self-host or Fargate)."
}

# --- ALB latency: the N+1 / cartesian-join / lost-index regressions -----------

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
  treat_missing_data  = "missing"
  dimensions          = { LoadBalancer = var.alb_arn_suffix }
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
  tags                = local.common_tags
}

# --- ALB 5xx: the gunicorn-OOM 502s (worker SIGKILLed, Sentry blind) ----------

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
  treat_missing_data  = "notBreaching"
  dimensions          = { LoadBalancer = var.alb_arn_suffix }
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
  tags                = local.common_tags
}

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

# --- RDS: the db.t3.micro connection ceiling / CPU ----------------------------

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

# --- ElastiCache: the cachalot-wrapped Redis outage ---------------------------

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
  treat_missing_data  = "breaching"
  dimensions          = { CacheClusterId = var.elasticache_cluster_id }
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
  tags                = local.common_tags
}

# --- Synthetic search canary: the ONLY detector for silent-zero-results -------
# search-silent-zero-results is invisible to error monitoring (200, no exception).
# A CloudWatch Synthetics canary runs a known-good query on a schedule and asserts
# the results count is > 0; when it returns 0, SuccessPercent drops and this fires.
# Stated honestly (plan decision 3): without this canary that fault is undetectable
# in production — which is the argument FOR the instrumentation.

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
  treat_missing_data  = "breaching"
  dimensions          = { CanaryName = "${local.name_prefix}-search-smoke" }
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
  tags                = local.common_tags
}

variable "alb_arn_suffix" { type = string }
variable "target_group_arn_suffix" { type = string }
variable "ecs_cluster_name" { type = string }
variable "ecs_service_name" { type = string }
variable "rds_instance_id" { type = string }
variable "elasticache_cluster_id" { type = string }
