# Inputs for the tcf-alarms module.
#
# Account-specific values have NO default on purpose: the reviewer must fill
# them from theCourseForum's own AWS account (see README "Variables to fill").
# Only the canary cadence carries a default, since rate(5 minutes) is a sane,
# documented starting point that the reviewer can dial down for cost.

# --- Culprit notification endpoint -------------------------------------------

variable "culprit_ingest_sns_url" {
  type        = string
  description = <<-EOT
    Culprit's public HTTPS ingest endpoint (POST /ingest/sns) that the SNS
    topic subscribes to. Must be reachable BEFORE `terraform apply` so the
    SubscriptionConfirmation handshake can complete. Example:
    https://culprit.example.com/ingest/sns
  EOT
}

# --- CloudWatch metric dimensions (from the running infra) --------------------

variable "alb_arn_suffix" {
  type        = string
  description = <<-EOT
    The ALB ARN suffix used as the CloudWatch `LoadBalancer` dimension, e.g.
    app/tcf-prod-alb/50dc6c495c0c9188. This is the trailing portion of the
    load balancer ARN, not the full ARN. Available as `arn_suffix` on the
    aws_lb resource / data source.
  EOT
}

variable "target_group_arn_suffix" {
  type        = string
  description = <<-EOT
    The target group ARN suffix used as the CloudWatch `TargetGroup` dimension,
    e.g. targetgroup/tcf-prod-tg/1234567890abcdef. Available as `arn_suffix`
    on the aws_lb_target_group resource / data source.
  EOT
}

variable "ecs_cluster_name" {
  type        = string
  description = "ECS cluster name for the MemoryUtilization alarm (ClusterName dimension)."
}

variable "ecs_service_name" {
  type        = string
  description = "ECS service name for the MemoryUtilization alarm (ServiceName dimension)."
}

variable "rds_instance_id" {
  type        = string
  description = "RDS DB instance identifier for the connections/CPU alarms (DBInstanceIdentifier dimension)."
}

variable "elasticache_cluster_id" {
  type        = string
  description = "ElastiCache cluster/node id for the health alarm (CacheClusterId dimension)."
}

# --- Search canary -----------------------------------------------------------

variable "search_url" {
  type        = string
  description = <<-EOT
    Fully qualified URL the canary requests to smoke-test search, including a
    known-good query that MUST return results, e.g.
    https://thecourseforum.com/search/?q=calculus
  EOT
}

variable "search_result_marker" {
  type        = string
  description = <<-EOT
    A substring that appears in the search-results HTML ONLY when at least one
    result is rendered (e.g. a result-card CSS class or wrapper id). The canary
    asserts this marker is present. Confirm it against theCourseForum's current
    search template before apply (see canary/python/search_smoke.py).
  EOT
  default     = "search-result"
}

variable "search_no_results_marker" {
  type        = string
  description = <<-EOT
    A substring that appears ONLY on the empty-results state (e.g. the
    "No results found" copy or its container class). The canary asserts this
    marker is ABSENT. This is what catches the silent zero-results fault: a
    200 with no exception but an empty result set. Confirm it against the
    current search template before apply.
  EOT
  default     = "no-results"
}

variable "canary_schedule_expression" {
  type        = string
  description = <<-EOT
    CloudWatch Synthetics schedule expression for the search canary. Defaults
    to rate(5 minutes); rate(15 minutes) roughly cuts the canary's monthly cost
    to a third (see README cost breakdown).
  EOT
  default     = "rate(5 minutes)"
}
