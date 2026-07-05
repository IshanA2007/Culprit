#!/usr/bin/env python3
"""Read-only AWS readiness probe for Culprit's live upgrade (M5).

Answers one question with facts: *what can Culprit actually read in theCourseForum's
account today, and is the SNS/alarm trigger side wired up yet?* Every call here is
read-only (Describe/List/Get) — it never mutates anything, consistent with the
offer-only stance.

It also validates the scoped IAM policy: each probe reports AccessDenied distinctly
from "worked but empty", so you can see exactly which permission (if any) is missing
versus which resource simply doesn't exist yet.

Run it (boto3 pulled in ephemerally so it isn't a committed dep yet):

    # option A: paste the 3 values from the SSO access portal
    export AWS_ACCESS_KEY_ID=...  AWS_SECRET_ACCESS_KEY=...  AWS_SESSION_TOKEN=...
    uv run --with boto3 python scripts/aws_probe.py

    # option B: a configured SSO profile
    aws sso login --profile tcf
    AWS_PROFILE=tcf uv run --with boto3 python scripts/aws_probe.py

Optional: pass one or more log-group name substrings to run a sample Logs Insights
query against the matching groups (confirms the middleware exception JSON is there):

    uv run --with boto3 python scripts/aws_probe.py --logs-query ecs django
"""

from __future__ import annotations

import argparse
import os
import sys
import time

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
except ImportError:
    sys.exit("boto3 not available — run with:  uv run --with boto3 python scripts/aws_probe.py")

# Fail fast so the region sweep doesn't hang 60s+ on opt-in/disabled regions.
_FAST = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1})

OK = "\033[92m✓\033[0m"
NO = "\033[91m✗\033[0m"
WARN = "\033[93m!\033[0m"


def _err_code(e: ClientError) -> str:
    return e.response.get("Error", {}).get("Code", "Unknown")


def _denied(e: ClientError) -> bool:
    return _err_code(e) in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation")


def probe_identity(session) -> None:
    print("\n== Identity ==")
    try:
        ident = session.client("sts").get_caller_identity()
        print(f"  {OK} authenticated as {ident['Arn']}")
        print(f"     account {ident['Account']}")
    except (ClientError, NoCredentialsError) as e:
        sys.exit(f"  {NO} no working credentials ({e}). Set the SSO env vars or AWS_PROFILE and retry.")


def probe_logs(session, query_terms: list[str]) -> None:
    print("\n== CloudWatch Logs (stack-trace evidence source) ==")
    logs = session.client("logs")
    groups: list[str] = []
    try:
        paginator = logs.get_paginator("describe_log_groups")
        for page in paginator.paginate():
            for g in page.get("logGroups", []):
                groups.append(g["logGroupName"])
        if groups:
            print(f"  {OK} describe_log_groups: {len(groups)} group(s) readable:")
            for name in groups:
                print(f"       - {name}")
            print("     ^ put the real Django/exception group name(s) into aws_log_groups (config step)")
        else:
            print(f"  {WARN} describe_log_groups worked but returned 0 groups (wrong region? nothing provisioned?)")
    except ClientError as e:
        print(f"  {NO} describe_log_groups: {'AccessDenied — policy missing logs:DescribeLogGroups' if _denied(e) else _err_code(e)}")
        return

    if not query_terms:
        print(f"  {WARN} skipping the sample Logs Insights query (pass --logs-query <term> to run it)")
        return

    targets = [g for g in groups if any(t.lower() in g.lower() for t in query_terms)]
    if not targets:
        print(f"  {WARN} no log group matched {query_terms}; skipping the query")
        return
    print(f"  .. running a sample Logs Insights query on {targets} (last 24h)")
    try:
        start = logs.start_query(
            logGroupNames=targets,
            startTime=int(time.time()) - 86400,
            endTime=int(time.time()),
            queryString='fields @message | filter @message like /ERROR/ | limit 5',
        )
        qid = start["queryId"]
        for _ in range(20):
            res = logs.get_query_results(queryId=qid)
            if res["status"] in ("Complete", "Failed", "Cancelled"):
                break
            time.sleep(0.5)
        rows = res.get("results", [])
        if rows:
            print(f"  {OK} query returned {len(rows)} ERROR line(s) — the middleware exception JSON is reachable")
        else:
            print(f"  {WARN} query ran but found 0 ERROR lines in 24h (quiet window, or exceptions log elsewhere)")
    except ClientError as e:
        print(f"  {NO} logs query: {'AccessDenied — policy missing logs:StartQuery/GetQueryResults' if _denied(e) else _err_code(e)}")


def probe_metrics(session) -> None:
    print("\n== CloudWatch Metrics / Alarms (impact + the SNS trigger source) ==")
    cw = session.client("cloudwatch")
    try:
        alarms = cw.describe_alarms(MaxRecords=100)
        metric_alarms = alarms.get("MetricAlarms", [])
        if metric_alarms:
            print(f"  {OK} describe_alarms: {len(metric_alarms)} alarm(s) exist:")
            for a in metric_alarms[:20]:
                print(f"       - {a['AlarmName']}  ->  {a.get('AlarmActions') or 'no action'}")
            print("     ^ any alarm with an SNS action can feed /ingest/sns")
        else:
            print(f"  {WARN} describe_alarms worked but there are 0 alarms")
            print("     => the SNS trigger side is NOT ready: nothing can fire yet.")
            print("        Needs docs/aws/alarms-proposal.tf merged by their infra team.")
    except ClientError as e:
        print(f"  {NO} describe_alarms: {'AccessDenied — policy missing cloudwatch:DescribeAlarms' if _denied(e) else _err_code(e)}")


def probe_sns(session) -> None:
    print("\n== SNS (delivery path into /ingest/sns) ==")
    sns = session.client("sns")
    try:
        topics = []
        for page in sns.get_paginator("list_topics").paginate():
            topics.extend(page.get("Topics", []))
        if topics:
            print(f"  {OK} list_topics: {len(topics)} topic(s):")
            for t in topics[:20]:
                print(f"       - {t['TopicArn']}")
            print("     -> allowlist the right ARN in SNS_ALLOWED_TOPIC_ARNS once a topic is chosen")
        else:
            print(f"  {WARN} 0 SNS topics — no delivery path exists yet (expected until the alarm PR lands)")
    except ClientError as e:
        print(f"  {NO} list_topics: {'AccessDenied — policy missing sns:ListTopics' if _denied(e) else _err_code(e)}")


def _count(session, region: str, service: str, method: str, key: str, **mkw):
    """Read-only count of a resource in one region; fails fast on dead regions."""
    try:
        client = session.client(service, region_name=region, config=_FAST)
        return len(getattr(client, method)(**mkw).get(key, []))
    except ClientError as e:
        return f"denied({_err_code(e)})" if _denied(e) else "err"
    except BotoCoreError:
        return "unreachable"  # opt-in/disabled region — skip fast


def sweep_regions(session, account: str) -> None:
    """Find which region(s), if any, actually hold CloudWatch/SNS resources."""
    print("\n== Region sweep (where does anything actually live?) ==")
    found_any = False
    for region in session.get_available_regions("logs"):
        counts = {
            "logs": _count(session, region, "logs", "describe_log_groups",
                           "logGroups", limit=50),
            "alarms": _count(session, region, "cloudwatch", "describe_alarms",
                             "MetricAlarms", MaxRecords=100),
            "topics": _count(session, region, "sns", "list_topics", "Topics"),
        }
        if any(isinstance(v, int) and v for v in counts.values()):
            found_any = True
            print(f"  {OK} {region}: {counts['logs']} log groups, "
                  f"{counts['alarms']} alarms, {counts['topics']} topics")
    if not found_any:
        print(f"  {NO} nothing in ANY region — account {account} is empty of CloudWatch/SNS resources.")
        print("     => almost certainly the wrong account (a fresh monitoring/sandbox account, not the")
        print("        one running the Django app), OR the app account is separate and needs")
        print("        cross-account observability. Ask their VP of Infra: which account ID + region")
        print("        runs the ECS Django service, and does this monitoring account aggregate it?")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--logs-query", nargs="*", default=[],
                    help="log-group name substrings to run a sample ERROR query against")
    ap.add_argument("--all-regions", action="store_true",
                    help="sweep every region to locate where resources actually live")
    args = ap.parse_args()

    session = boto3.Session(region_name=args.region)
    print(f"Culprit AWS readiness probe — region {args.region} (read-only)")
    probe_identity(session)
    probe_logs(session, args.logs_query)
    probe_metrics(session)
    probe_sns(session)
    if args.all_regions:
        account = session.client("sts").get_caller_identity()["Account"]
        sweep_regions(session, account)
    print("\nReading the result:")
    print("  - Logs group(s) listed + query returns ERROR lines -> ready to wire Boto3LogsProvider (Step 2).")
    print("  - 0 alarms / 0 topics -> the pull side is ready but the SNS trigger side needs the infra PR (Step 4).")
    print("  - Any AccessDenied above -> the granted policy is missing that action; hand it back for a scope fix.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
