"""Deploy immutable ECR images to the Terraform-managed ECS services.

The workflow supplies an OIDC role and environment-specific resource names. This
script owns release state; Terraform deliberately ignores task revision and traffic
weight drift. No database migration or downgrade is performed here.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROLES = ("api", "extraction", "materialization", "beat")
logger = logging.getLogger(__name__)
TASK_FIELDS = {
    "family",
    "taskRoleArn",
    "executionRoleArn",
    "networkMode",
    "containerDefinitions",
    "volumes",
    "placementConstraints",
    "requiresCompatibilities",
    "cpu",
    "memory",
    "runtimePlatform",
    "ephemeralStorage",
    "proxyConfiguration",
    "inferenceAccelerators",
    "pidMode",
    "ipcMode",
    "tags",
}


def args_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("staging", "release", "rollback"))
    parser.add_argument("--environment", choices=("staging", "production"), required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--repository-url", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--listener-arn")
    parser.add_argument("--stable-target-group")
    parser.add_argument("--canary-target-group")
    parser.add_argument("--alb-arn-suffix")
    return parser


def service_name(environment: str, role: str) -> str:
    return f"bancaemdia-{environment}-{role}"


def image_for_sha(ecr, repository_url: str, sha: str) -> str:
    if len(sha) != 40 or any(char not in "0123456789abcdef" for char in sha):
        raise ValueError("sha must be a full lowercase Git commit hash")
    repository = repository_url.rsplit("/", 1)[1]
    details = ecr.describe_images(repositoryName=repository, imageIds=[{"imageTag": sha}])
    digest = details["imageDetails"][0]["imageDigest"]
    return f"{repository_url}@{digest}"


def current_services(ecs, cluster: str, environment: str) -> dict[str, dict]:
    names = [service_name(environment, role) for role in ROLES]
    services = ecs.describe_services(cluster=cluster, services=names)["services"]
    found = {service["serviceName"]: service for service in services}
    if len(found) != len(names) or any(s["status"] != "ACTIVE" for s in services):
        raise RuntimeError("all four Terraform-managed ECS services must be active")
    return {role: found[service_name(environment, role)] for role in ROLES}


def register_image(ecs, task_arn: str, image: str) -> str:
    definition = ecs.describe_task_definition(taskDefinition=task_arn)["taskDefinition"]
    payload = {key: value for key, value in definition.items() if key in TASK_FIELDS}
    for container in payload["containerDefinitions"]:
        container["image"] = image
    return ecs.register_task_definition(**payload)["taskDefinition"]["taskDefinitionArn"]


def update_and_wait(ecs, cluster: str, service: str, task: str, count: int | None = None) -> None:
    change = {
        "cluster": cluster,
        "service": service,
        "taskDefinition": task,
        "forceNewDeployment": True,
    }
    if count is not None:
        change["desiredCount"] = count
    ecs.update_service(**change)
    ecs.get_waiter("services_stable").wait(
        cluster=cluster, services=[service], WaiterConfig={"Delay": 5, "MaxAttempts": 90}
    )
    actual = ecs.describe_services(cluster=cluster, services=[service])["services"][0]
    if actual["taskDefinition"] != task or actual["runningCount"] != actual["desiredCount"]:
        raise RuntimeError(f"{service} did not stabilize on requested task definition")


def rollout(ecs, cluster: str, before: dict[str, dict], revisions: dict[str, str]) -> None:
    names = []
    for role, revision in revisions.items():
        service = before[role]["serviceName"]
        ecs.update_service(
            cluster=cluster, service=service, taskDefinition=revision, forceNewDeployment=True
        )
        names.append(service)
    ecs.get_waiter("services_stable").wait(
        cluster=cluster, services=names, WaiterConfig={"Delay": 5, "MaxAttempts": 90}
    )
    actual = ecs.describe_services(cluster=cluster, services=names)["services"]
    expected = {before[role]["serviceName"]: revision for role, revision in revisions.items()}
    if len(actual) != len(names) or any(
        service["taskDefinition"] != expected[service["serviceName"]]
        or service["runningCount"] != service["desiredCount"]
        for service in actual
    ):
        raise RuntimeError("one or more ECS services did not stabilize on the requested image")


def set_weights(elb, listener: str, stable: str, canary: str, canary_weight: int) -> None:
    if not 0 <= canary_weight <= 100:
        raise ValueError("canary weight must be 0..100")
    elb.modify_listener(
        ListenerArn=listener,
        DefaultActions=[
            {
                "Type": "forward",
                "ForwardConfig": {
                    "TargetGroups": [
                        {"TargetGroupArn": stable, "Weight": 100 - canary_weight},
                        {"TargetGroupArn": canary, "Weight": canary_weight},
                    ]
                },
            }
        ],
    )
    logger.info("ALB canary weight: %s%%", canary_weight)


def require_stable_traffic(elb, listener: str, stable: str, canary: str) -> None:
    actions = elb.describe_listeners(ListenerArns=[listener])["Listeners"][0]["DefaultActions"]
    if len(actions) != 1 or actions[0]["Type"] != "forward":
        raise RuntimeError("HTTPS listener has an unexpected default action")
    groups = actions[0]["ForwardConfig"]["TargetGroups"]
    weights = {group["TargetGroupArn"]: group["Weight"] for group in groups}
    if weights != {stable: 100, canary: 0}:
        raise RuntimeError("HTTPS listener is not at the stable 100/0 baseline")


def healthy_targets(elb, target_group: str) -> int:
    response = elb.describe_target_health(TargetGroupArn=target_group)
    return sum(
        item["TargetHealth"]["State"] == "healthy" for item in response["TargetHealthDescriptions"]
    )


def probe(base_url: str, path: str, token: str | None = None) -> None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = Request(f"{base_url.rstrip('/')}{path}", headers=headers)
    try:
        with urlopen(request, timeout=10) as response:
            if response.status != 200:
                raise RuntimeError(f"{path}: HTTP {response.status}")
            response.read()
    except (HTTPError, URLError) as error:
        raise RuntimeError(f"{path} probe failed: {error}") from error


def smoke(base_url: str, token: str) -> None:
    for path in ("/health", "/ready"):
        probe(base_url, path)
    probe(base_url, "/api/v1/painel", token)


def readiness_window(base_url: str, token: str) -> None:
    for attempt in range(7):
        smoke(base_url, token)
        if attempt < 6:
            time.sleep(5)


def integrity_check(ecs, cluster: str, worker: dict) -> None:
    config = worker["networkConfiguration"]["awsvpcConfiguration"]
    task = ecs.run_task(
        cluster=cluster,
        taskDefinition=worker["taskDefinition"],
        launchType="FARGATE",
        count=1,
        networkConfiguration={"awsvpcConfiguration": config},
        overrides={
            "containerOverrides": [
                {
                    "name": "materialization",
                    "command": ["python", "scripts/conferir_numeros.py", "--todos"],
                }
            ]
        },
    )
    if task.get("failures") or len(task.get("tasks", [])) != 1:
        raise RuntimeError(f"integrity task launch failed: {task.get('failures')}")
    arn = task["tasks"][0]["taskArn"]
    ecs.get_waiter("tasks_stopped").wait(
        cluster=cluster, tasks=[arn], WaiterConfig={"Delay": 5, "MaxAttempts": 120}
    )
    stopped = ecs.describe_tasks(cluster=cluster, tasks=[arn])["tasks"][0]
    code = stopped["containers"][0].get("exitCode")
    if code != 0:
        raise RuntimeError(f"integrity check failed: ECS task exit {code}")


def metric(
    cloudwatch,
    name: str,
    statistic: str,
    dimensions: list[dict],
    minutes: int = 5,
    namespace: str = "AWS/ApplicationELB",
) -> float | None:
    end = datetime.now(UTC)
    response = cloudwatch.get_metric_statistics(
        Namespace=namespace,
        MetricName=name,
        Dimensions=dimensions,
        StartTime=end - timedelta(minutes=minutes),
        EndTime=end,
        Period=60,
        **(
            {"ExtendedStatistics": [statistic]}
            if statistic == "p99"
            else {"Statistics": [statistic]}
        ),
    )
    points = response["Datapoints"]
    if not points:
        return None
    values = [
        point["ExtendedStatistics"][statistic] if statistic == "p99" else point[statistic]
        for point in points
    ]
    return sum(values) if statistic == "Sum" else max(values)


def monitor(cloudwatch, elb, args, minutes: int, token: str) -> None:
    # Every minute samples the prior five minutes; missing traffic fails closed.
    target_suffix = args.canary_target_group.split("targetgroup/", 1)[1]
    dims = [
        {"Name": "LoadBalancer", "Value": args.alb_arn_suffix},
        {"Name": "TargetGroup", "Value": f"targetgroup/{target_suffix}"},
    ]
    for _ in range(minutes):
        if healthy_targets(elb, args.canary_target_group) < 1:
            raise RuntimeError("canary has no healthy /ready targets")
        smoke(args.base_url, token)
        time.sleep(60)
        requests = metric(cloudwatch, "RequestCount", "Sum", dims)
        errors = metric(cloudwatch, "HTTPCode_Target_5XX_Count", "Sum", dims) or 0
        p99 = metric(cloudwatch, "TargetResponseTime", "p99", dims)
        worker_errors = sum(
            metric(
                cloudwatch,
                f"bancaemdia-production-{role}-errors",
                "Sum",
                [],
                namespace="BancaEmDia/production",
            )
            or 0
            for role in ("extraction", "materialization")
        )
        if not requests or p99 is None:
            raise RuntimeError("canary metrics missing; cannot promote")
        if errors / requests > 0.01 or p99 > 2 or worker_errors:
            raise RuntimeError(
                f"canary unhealthy: 5xx={errors}/{requests}, p99={p99}s, "
                f"worker errors={worker_errors}"
            )
        logger.info(
            "canary metrics: 5xx=%s/%s, p99=%ss, worker errors=%s",
            errors,
            requests,
            p99,
            worker_errors,
        )


def rollback_to(ecs, elb, args, before: dict[str, dict], canary_service: str | None) -> None:
    if args.listener_arn and args.stable_target_group and args.canary_target_group:
        set_weights(elb, args.listener_arn, args.stable_target_group, args.canary_target_group, 0)
    rollout(ecs, args.cluster, before, {role: before[role]["taskDefinition"] for role in ROLES})
    if canary_service:
        ecs.update_service(cluster=args.cluster, service=canary_service, desiredCount=0)


def main() -> int:
    import boto3

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = args_parser().parse_args()
    if args.mode == "staging" and args.environment != "staging":
        raise ValueError("staging mode requires staging environment")
    if args.mode != "staging" and args.environment != "production":
        raise ValueError("production mode requires production environment")
    if args.mode == "release" and not all((
        args.listener_arn,
        args.stable_target_group,
        args.canary_target_group,
        args.alb_arn_suffix,
    )):
        raise ValueError("production canary requires listener, target groups and ALB suffix")
    if not args.base_url.startswith("https://"):
        raise ValueError("base URL must use HTTPS")
    token = os.environ.get("DEPLOY_SMOKE_TOKEN")
    if not token:
        raise ValueError("DEPLOY_SMOKE_TOKEN is required")
    session = boto3.Session()
    ecs, ecr, elb, cloudwatch = (
        session.client(name) for name in ("ecs", "ecr", "elbv2", "cloudwatch")
    )
    image = image_for_sha(ecr, args.repository_url, args.sha)
    before = current_services(ecs, args.cluster, args.environment)
    canary_service = (
        service_name(args.environment, "api-canary") if args.mode != "staging" else None
    )
    changed = False
    try:
        if args.mode == "release":
            require_stable_traffic(
                elb, args.listener_arn, args.stable_target_group, args.canary_target_group
            )
            candidate = register_image(ecs, before["api"]["taskDefinition"], image)
            changed = True
            update_and_wait(ecs, args.cluster, canary_service, candidate, count=2)
            if healthy_targets(elb, args.canary_target_group) < 1:
                raise RuntimeError("canary target group has no healthy targets")
            for weight, minutes in ((10, 10), (50, 5), (100, 5)):
                set_weights(
                    elb,
                    args.listener_arn,
                    args.stable_target_group,
                    args.canary_target_group,
                    weight,
                )
                monitor(cloudwatch, elb, args, minutes, token)
            # Keep canary at 100% until stable API is healthy on the new image.
            revisions = {
                role: register_image(ecs, before[role]["taskDefinition"], image)
                for role in ("extraction", "materialization", "beat", "api")
            }
            rollout(ecs, args.cluster, before, revisions)
            integrity_check(
                ecs,
                args.cluster,
                current_services(ecs, args.cluster, args.environment)["materialization"],
            )
            set_weights(
                elb, args.listener_arn, args.stable_target_group, args.canary_target_group, 0
            )
            ecs.update_service(cluster=args.cluster, service=canary_service, desiredCount=0)
        else:
            if args.mode == "rollback" and all((
                args.listener_arn,
                args.stable_target_group,
                args.canary_target_group,
            )):
                set_weights(
                    elb, args.listener_arn, args.stable_target_group, args.canary_target_group, 0
                )
            changed = True
            revisions = {
                role: register_image(ecs, before[role]["taskDefinition"], image) for role in ROLES
            }
            rollout(ecs, args.cluster, before, revisions)
            readiness_window(args.base_url, token)
            integrity_check(
                ecs,
                args.cluster,
                current_services(ecs, args.cluster, args.environment)["materialization"],
            )
            if args.mode == "rollback" and canary_service:
                ecs.update_service(cluster=args.cluster, service=canary_service, desiredCount=0)
        logger.info(
            "%s",
            json.dumps({
                "environment": args.environment,
                "sha": args.sha,
                "image": image,
                "status": "healthy",
            }),
        )
    except Exception:
        if changed:
            logger.exception("Deployment failed; restoring prior ECS revisions and ALB weights")
            try:
                rollback_to(ecs, elb, args, before, canary_service)
            except Exception as rollback_error:
                logger.exception("Automatic rollback failed: %s", rollback_error)
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
