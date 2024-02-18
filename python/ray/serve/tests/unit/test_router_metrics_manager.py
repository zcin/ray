import pytest
from unittest.mock import patch
from ray.serve._private.test_utils import FakeCounter
from ray.serve._private.router import RouterMetricsManager
from ray.serve._private.common import DeploymentID, RequestMetadata, RunningReplicaInfo


def test_num_router_requests():
    with patch("metrics.Counter", new=FakeCounter):
        metrics_manager = RouterMetricsManager(DeploymentID("a", "b"), "random", None)


def test_shutdown():
    pass
