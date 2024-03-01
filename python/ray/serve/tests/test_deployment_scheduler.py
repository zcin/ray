import sys

import pytest

import ray
from ray import serve
from ray._raylet import GcsClient
from ray.serve._private.common import DeploymentID
from ray.serve._private.constants import RAY_SERVE_USE_COMPACT_SCHEDULING_STRATEGY
from ray.serve._private.default_impl import create_cluster_node_info_cache
from ray.serve._private.deployment_scheduler import (
    DefaultDeploymentScheduler,
    DeploymentDownscaleRequest,
    ReplicaSchedulingRequest,
    SpreadDeploymentSchedulingPolicy,
)
from ray.serve._private.test_utils import get_node_id
from ray.serve._private.utils import get_head_node_id
from ray.tests.conftest import *  # noqa


@ray.remote(num_cpus=1)
class Replica:
    def get_node_id(self):
        return ray.get_runtime_context().get_node_id()

    def get_placement_group(self):
        return ray.util.get_current_placement_group()


class TestSpreadScheduling:
    @pytest.mark.parametrize(
        "placement_group_config",
        [
            {},
            {"bundles": [{"CPU": 3}]},
            {
                "bundles": [{"CPU": 1}, {"CPU": 1}, {"CPU": 1}],
                "strategy": "STRICT_PACK",
            },
        ],
    )
    @pytest.mark.skipif(
        RAY_SERVE_USE_COMPACT_SCHEDULING_STRATEGY,
        reason="Need to use spread strategy",
    )
    def test_spread_deployment_scheduling_policy_upscale(
        ray_start_cluster, placement_group_config
    ):
        """Test to make sure replicas are spreaded."""
        cluster = ray_start_cluster
        cluster.add_node(num_cpus=3)
        cluster.add_node(num_cpus=3)
        cluster.wait_for_nodes()
        ray.init(address=cluster.address)

        cluster_node_info_cache = create_cluster_node_info_cache(
            GcsClient(address=ray.get_runtime_context().gcs_address)
        )
        cluster_node_info_cache.update()

        scheduler = DefaultDeploymentScheduler(
            cluster_node_info_cache, get_head_node_id()
        )
        dep_id = DeploymentID(name="deployment1")
        scheduler.on_deployment_created(dep_id, SpreadDeploymentSchedulingPolicy())
        replica_actor_handles = []
        replica_placement_groups = []

        def on_scheduled(actor_handle, placement_group):
            replica_actor_handles.append(actor_handle)
            replica_placement_groups.append(placement_group)

        deployment_to_replicas_to_stop = scheduler.schedule(
            upscales={
                dep_id: [
                    ReplicaSchedulingRequest(
                        deployment_id=dep_id,
                        replica_name="replica1",
                        actor_def=Replica,
                        actor_resources={"CPU": 1},
                        actor_options={"name": "deployment1_replica1"},
                        actor_init_args=(),
                        on_scheduled=on_scheduled,
                        placement_group_bundles=placement_group_config.get(
                            "bundles", None
                        ),
                        placement_group_strategy=placement_group_config.get(
                            "strategy", None
                        ),
                    ),
                    ReplicaSchedulingRequest(
                        deployment_id=dep_id,
                        replica_name="replica2",
                        actor_def=Replica,
                        actor_resources={"CPU": 1},
                        actor_options={"name": "deployment1_replica2"},
                        actor_init_args=(),
                        on_scheduled=on_scheduled,
                        placement_group_bundles=placement_group_config.get(
                            "bundles", None
                        ),
                        placement_group_strategy=placement_group_config.get(
                            "strategy", None
                        ),
                    ),
                ]
            },
            downscales={},
        )
        assert not deployment_to_replicas_to_stop
        assert len(replica_actor_handles) == 2
        assert len(replica_placement_groups) == 2
        assert not scheduler._pending_replicas[dep_id]
        assert len(scheduler._launching_replicas[dep_id]) == 2
        assert (
            len(
                {
                    ray.get(replica_actor_handles[0].get_node_id.remote()),
                    ray.get(replica_actor_handles[1].get_node_id.remote()),
                }
            )
            == 2
        )
        if "bundles" in placement_group_config:
            assert (
                len(
                    {
                        ray.get(replica_actor_handles[0].get_placement_group.remote()),
                        ray.get(replica_actor_handles[1].get_placement_group.remote()),
                    }
                )
                == 2
            )
        scheduler.on_replica_stopping(dep_id, "replica1")
        scheduler.on_replica_stopping(dep_id, "replica2")
        scheduler.on_deployment_deleted(dep_id)

    @pytest.mark.skipif(
        RAY_SERVE_USE_COMPACT_SCHEDULING_STRATEGY,
        reason="Need to use spread strategy",
    )
    def test_spread_deployment_scheduling_policy_downscale_multiple_deployments(
        ray_start_cluster,
    ):
        """Test to make sure downscale prefers replicas without node id
        and then replicas on a node with fewest replicas of all deployments.
        """
        cluster = ray_start_cluster
        cluster.add_node(num_cpus=3)
        cluster.wait_for_nodes()
        ray.init(address=cluster.address)

        cluster_node_info_cache = create_cluster_node_info_cache(
            GcsClient(address=ray.get_runtime_context().gcs_address)
        )
        cluster_node_info_cache.update()

        scheduler = DefaultDeploymentScheduler(
            cluster_node_info_cache, get_head_node_id()
        )
        d1_id = DeploymentID(name="deployment1")
        d2_id = DeploymentID(name="deployment2")
        scheduler.on_deployment_created(d1_id, SpreadDeploymentSchedulingPolicy())
        scheduler.on_deployment_created(d2_id, SpreadDeploymentSchedulingPolicy())
        scheduler.on_replica_running(d1_id, "replica1", "node1")
        scheduler.on_replica_running(d1_id, "replica2", "node2")
        scheduler.on_replica_running(d1_id, "replica3", "node2")
        scheduler.on_replica_running(d2_id, "replica1", "node1")
        scheduler.on_replica_running(d2_id, "replica2", "node2")
        scheduler.on_replica_running(d2_id, "replica3", "node1")
        scheduler.on_replica_running(d2_id, "replica4", "node1")
        deployment_to_replicas_to_stop = scheduler.schedule(
            upscales={},
            downscales={
                d1_id: DeploymentDownscaleRequest(deployment_id=d1_id, num_to_stop=1)
            },
        )
        assert len(deployment_to_replicas_to_stop) == 1
        # Even though node1 has fewest replicas of deployment1
        # but it has more replicas of all deployments so
        # we should stop replicas from node2.
        assert len(deployment_to_replicas_to_stop[d1_id]) == 1
        assert deployment_to_replicas_to_stop[d1_id] < {"replica2", "replica3"}

        scheduler.on_replica_stopping(d1_id, "replica3")
        scheduler.on_replica_stopping(d2_id, "replica3")
        scheduler.on_replica_stopping(d2_id, "replica4")

        deployment_to_replicas_to_stop = scheduler.schedule(
            upscales={},
            downscales={
                d1_id: DeploymentDownscaleRequest(deployment_id=d1_id, num_to_stop=1),
                d2_id: DeploymentDownscaleRequest(deployment_id=d2_id, num_to_stop=1),
            },
        )
        assert len(deployment_to_replicas_to_stop) == 2
        # We should stop replicas from the same node.
        assert len(deployment_to_replicas_to_stop[d1_id]) == 1
        assert (
            deployment_to_replicas_to_stop[d1_id]
            == deployment_to_replicas_to_stop[d2_id]
        )

        scheduler.on_replica_stopping(d1_id, "replica1")
        scheduler.on_replica_stopping(d1_id, "replica2")
        scheduler.on_replica_stopping(d2_id, "replica1")
        scheduler.on_replica_stopping(d2_id, "replica2")
        scheduler.on_deployment_deleted(d1_id)
        scheduler.on_deployment_deleted(d2_id)

    @pytest.mark.skipif(
        RAY_SERVE_USE_COMPACT_SCHEDULING_STRATEGY,
        reason="Need to use spread strategy",
    )
    def test_spread_deployment_scheduling_policy_downscale_single_deployment(
        ray_start_cluster,
    ):
        """Test to make sure downscale prefers replicas without node id
        and then replicas on a node with fewest replicas of all deployments.
        """
        cluster = ray_start_cluster
        cluster.add_node(num_cpus=3)
        cluster.wait_for_nodes()
        ray.init(address=cluster.address)

        cluster_node_info_cache = create_cluster_node_info_cache(
            GcsClient(address=ray.get_runtime_context().gcs_address)
        )
        cluster_node_info_cache.update()

        scheduler = DefaultDeploymentScheduler(
            cluster_node_info_cache, get_head_node_id()
        )
        dep_id = DeploymentID(name="deployment1")
        scheduler.on_deployment_created(dep_id, SpreadDeploymentSchedulingPolicy())
        scheduler.on_replica_running(dep_id, "replica1", "node1")
        scheduler.on_replica_running(dep_id, "replica2", "node1")
        scheduler.on_replica_running(dep_id, "replica3", "node2")
        scheduler.on_replica_recovering(dep_id, "replica4")
        deployment_to_replicas_to_stop = scheduler.schedule(
            upscales={},
            downscales={
                dep_id: DeploymentDownscaleRequest(deployment_id=dep_id, num_to_stop=1)
            },
        )
        assert len(deployment_to_replicas_to_stop) == 1
        # Prefer replica without node id
        assert deployment_to_replicas_to_stop[dep_id] == {"replica4"}
        scheduler.on_replica_stopping(dep_id, "replica4")

        deployment_to_replicas_to_stop = scheduler.schedule(
            upscales={
                dep_id: [
                    ReplicaSchedulingRequest(
                        deployment_id=dep_id,
                        replica_name="replica5",
                        actor_def=Replica,
                        actor_resources={"CPU": 1},
                        actor_options={},
                        actor_init_args=(),
                        on_scheduled=lambda actor_handle, placement_group: actor_handle,
                    ),
                ]
            },
            downscales={},
        )
        assert not deployment_to_replicas_to_stop
        deployment_to_replicas_to_stop = scheduler.schedule(
            upscales={},
            downscales={
                dep_id: DeploymentDownscaleRequest(deployment_id=dep_id, num_to_stop=1)
            },
        )
        assert len(deployment_to_replicas_to_stop) == 1
        # Prefer replica without node id
        assert deployment_to_replicas_to_stop[dep_id] == {"replica5"}
        scheduler.on_replica_stopping(dep_id, "replica5")

        deployment_to_replicas_to_stop = scheduler.schedule(
            upscales={},
            downscales={
                dep_id: DeploymentDownscaleRequest(deployment_id=dep_id, num_to_stop=1)
            },
        )
        assert len(deployment_to_replicas_to_stop) == 1
        # Prefer replica on a node with fewest replicas of all deployments.
        assert deployment_to_replicas_to_stop[dep_id] == {"replica3"}
        scheduler.on_replica_stopping(dep_id, "replica3")

        deployment_to_replicas_to_stop = scheduler.schedule(
            upscales={},
            downscales={
                dep_id: DeploymentDownscaleRequest(deployment_id=dep_id, num_to_stop=2)
            },
        )
        assert len(deployment_to_replicas_to_stop) == 1
        assert deployment_to_replicas_to_stop[dep_id] == {"replica1", "replica2"}
        scheduler.on_replica_stopping(dep_id, "replica1")
        scheduler.on_replica_stopping(dep_id, "replica2")
        scheduler.on_deployment_deleted(dep_id)

    @pytest.mark.skipif(
        RAY_SERVE_USE_COMPACT_SCHEDULING_STRATEGY,
        reason="Need to use spread strategy",
    )
    def test_spread_deployment_scheduling_policy_downscale_head_node(ray_start_cluster):
        """Test to make sure downscale deprioritizes replicas on the head node."""
        cluster = ray_start_cluster
        cluster.add_node(num_cpus=3)
        cluster.wait_for_nodes()
        ray.init(address=cluster.address)
        head_node_id = get_head_node_id()

        cluster_node_info_cache = create_cluster_node_info_cache(
            GcsClient(address=ray.get_runtime_context().gcs_address)
        )
        cluster_node_info_cache.update()

        scheduler = DefaultDeploymentScheduler(
            cluster_node_info_cache, get_head_node_id()
        )
        dep_id = DeploymentID(name="deployment1")
        scheduler.on_deployment_created(dep_id, SpreadDeploymentSchedulingPolicy())
        scheduler.on_replica_running(dep_id, "replica1", head_node_id)
        scheduler.on_replica_running(dep_id, "replica2", "node2")
        scheduler.on_replica_running(dep_id, "replica3", "node2")
        deployment_to_replicas_to_stop = scheduler.schedule(
            upscales={},
            downscales={
                dep_id: DeploymentDownscaleRequest(deployment_id=dep_id, num_to_stop=1)
            },
        )
        assert len(deployment_to_replicas_to_stop) == 1
        assert deployment_to_replicas_to_stop[dep_id] < {"replica2", "replica3"}
        scheduler.on_replica_stopping(
            dep_id, deployment_to_replicas_to_stop[dep_id].pop()
        )

        deployment_to_replicas_to_stop = scheduler.schedule(
            upscales={},
            downscales={
                dep_id: DeploymentDownscaleRequest(deployment_id=dep_id, num_to_stop=1)
            },
        )
        assert len(deployment_to_replicas_to_stop) == 1
        assert deployment_to_replicas_to_stop[dep_id] < {"replica2", "replica3"}
        scheduler.on_replica_stopping(
            dep_id, deployment_to_replicas_to_stop[dep_id].pop()
        )

        deployment_to_replicas_to_stop = scheduler.schedule(
            upscales={},
            downscales={
                dep_id: DeploymentDownscaleRequest(deployment_id=dep_id, num_to_stop=1)
            },
        )
        assert len(deployment_to_replicas_to_stop) == 1
        assert deployment_to_replicas_to_stop[dep_id] == {"replica1"}
        scheduler.on_replica_stopping(dep_id, "replica1")
        scheduler.on_deployment_deleted(dep_id)


def check_num_alive_nodes(target: int):
    alive_nodes = [node for node in ray.nodes() if node["Alive"]]
    assert len(alive_nodes) == target
    return True


@serve.deployment
def A():
    return ray.get_runtime_context().get_node_id()


app_A = A.bind()


class TestCompactScheduling:
    @pytest.mark.skipif(
        not RAY_SERVE_USE_COMPACT_SCHEDULING_STRATEGY,
        reason="Only works with compact scheduling.",
    )
    @pytest.mark.parametrize("use_pg", [True, False])
    def test_basic(self, ray_cluster, use_pg: bool):
        cluster = ray_cluster
        cluster.add_node(num_cpus=2, resources={"head": 1})
        cluster.add_node(num_cpus=3, resources={"worker1": 1})
        cluster.add_node(num_cpus=4, resources={"worker2": 1})
        cluster.wait_for_nodes()
        ray.init(address=cluster.address)

        head_node_id = ray.get(get_node_id.options(resources={"head": 1}).remote())
        worker1_node_id = ray.get(
            get_node_id.options(resources={"worker1": 1}).remote()
        )
        worker2_node_id = ray.get(
            get_node_id.options(resources={"worker2": 1}).remote()
        )
        print("head", head_node_id)
        print("worker1", worker1_node_id)
        print("worker2", worker2_node_id)

        # Both f replicas should be scheduled on head node to minimize
        # fragmentation
        if use_pg:
            app1 = A.options(
                num_replicas=2,
                ray_actor_options={"num_cpus": 0.5},
                placement_group_bundles=[{"CPU": 0.5}, {"CPU": 0.5}],
                placement_group_strategy="STRICT_PACK",
            ).bind()
        else:
            app1 = A.options(num_replicas=2, ray_actor_options={"num_cpus": 1}).bind()

        # Both app1 replicas should have been scheduled on head node
        f_handle = serve.run(app1, name="app1", route_prefix="/app1")
        refs = [f_handle.remote() for _ in range(20)]
        assert {ref.result() for ref in refs} == {head_node_id}

        if use_pg:
            app2 = A.options(
                num_replicas=1,
                ray_actor_options={"num_cpus": 0.5},
                placement_group_bundles=[{"CPU": 1.5}, {"CPU": 1.5}],
                placement_group_strategy="STRICT_PACK",
            ).bind()
        else:
            app2 = A.options(num_replicas=1, ray_actor_options={"num_cpus": 3}).bind()

        # Then there should be enough space for the g replica
        g_handle = serve.run(app2, name="app2", route_prefix="/app2")
        assert g_handle.remote().result() == worker1_node_id

        serve.shutdown()


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", "-s", __file__]))
