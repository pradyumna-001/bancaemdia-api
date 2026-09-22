"""Release control tests that do not require an AWS account."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from scripts import deploy_ecs


def test_image_for_sha_uses_ecr_digest() -> None:
    ecr = Mock()
    ecr.describe_images.return_value = {"imageDetails": [{"imageDigest": "sha256:" + "a" * 64}]}
    sha = "b" * 40
    result = deploy_ecs.image_for_sha(ecr, "123.dkr.ecr.us-east-1.amazonaws.com/api", sha)
    assert result.endswith("@sha256:" + "a" * 64)
    ecr.describe_images.assert_called_once_with(repositoryName="api", imageIds=[{"imageTag": sha}])
    with pytest.raises(ValueError):
        deploy_ecs.image_for_sha(ecr, "repository", "main")


def test_weights_are_explicit_and_bounded() -> None:
    elb = Mock()
    deploy_ecs.set_weights(elb, "listener", "stable", "canary", 10)
    groups = elb.modify_listener.call_args.kwargs["DefaultActions"][0]["ForwardConfig"][
        "TargetGroups"
    ]
    assert groups == [
        {"TargetGroupArn": "stable", "Weight": 90},
        {"TargetGroupArn": "canary", "Weight": 10},
    ]
    with pytest.raises(ValueError):
        deploy_ecs.set_weights(elb, "listener", "stable", "canary", 101)


def test_canary_monitor_fails_closed_when_metrics_missing() -> None:
    args = SimpleNamespace(
        canary_target_group="arn:aws:elasticloadbalancing:us-east-1:123:targetgroup/canary/abc",
        alb_arn_suffix="app/api/abc",
        base_url="https://example.test",
    )
    elb = Mock()
    with (
        patch.object(deploy_ecs, "healthy_targets", return_value=2),
        patch.object(deploy_ecs, "smoke"),
        patch.object(deploy_ecs.time, "sleep"),
        patch.object(deploy_ecs, "metric", return_value=None),
    ):
        with pytest.raises(RuntimeError, match="metrics missing"):
            deploy_ecs.monitor(Mock(), elb, args, 1, "synthetic-token")


def test_rollback_restores_traffic_before_tasks() -> None:
    before = {
        role: {"serviceName": role, "taskDefinition": f"old-{role}"} for role in deploy_ecs.ROLES
    }
    args = SimpleNamespace(
        listener_arn="listener",
        stable_target_group="stable",
        canary_target_group="canary",
        cluster="cluster",
    )
    events = []
    with (
        patch.object(deploy_ecs, "set_weights", side_effect=lambda *a: events.append("traffic")),
        patch.object(deploy_ecs, "rollout", side_effect=lambda *a: events.append("services")),
    ):
        deploy_ecs.rollback_to(Mock(), Mock(), args, before, None)
    assert events == ["traffic", "services"]


def test_rollout_rejects_ecs_circuit_breaker_reversion() -> None:
    ecs = Mock()
    ecs.describe_services.return_value = {
        "services": [
            {
                "serviceName": "api",
                "taskDefinition": "old-api",
                "runningCount": 2,
                "desiredCount": 2,
            }
        ]
    }
    before = {"api": {"serviceName": "api", "taskDefinition": "old-api"}}
    with pytest.raises(RuntimeError, match="did not stabilize"):
        deploy_ecs.rollout(ecs, "cluster", before, {"api": "new-api"})
