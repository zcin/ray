#!/usr/bin/env python3

import click
import logging
from typing import Optional

from anyscale import service
from anyscale.compute_config.models import (
    ComputeConfig,
    HeadNodeConfig,
    WorkerNodeGroupConfig,
)
import ray

from anyscale_service_utils import start_service
from locust_utils import LocustLoadTestConfig, LocustStage, run_locust_load_test
from serve_test_utils import save_test_results


logger = logging.getLogger(__file__)

DEFAULT_FULL_TEST_NUM_REPLICA = 1000
DEFAULT_FULL_TEST_TRIAL_LENGTH_S = 60


@click.command()
@click.option("--num-replicas", type=int)
@click.option("--trial-length", type=str)
def main(num_replicas: Optional[int], trial_length: Optional[str]):
    num_replicas = num_replicas or DEFAULT_FULL_TEST_NUM_REPLICA
    trial_length = trial_length or DEFAULT_FULL_TEST_TRIAL_LENGTH_S
    noop_1k_application = {
        "name": "default",
        "import_path": "noop:app",
        "route_prefix": "/",
        "runtime_env": {"working_dir": "workloads"},
        "deployments": [
            {
                "name": "Noop",
                "num_replicas": num_replicas,
                "ray_actor_options": {"resources": {"worker_resource": 0.01}},
            }
        ],
    }
    compute_config = ComputeConfig(
        cloud="anyscale_v2_default_cloud",
        head_node=HeadNodeConfig(instance_type="m5.8xlarge"),
        worker_nodes=[
            WorkerNodeGroupConfig(
                instance_type="m5.8xlarge",
                min_nodes=0,
                max_nodes=50,
                resources={"worker_resource": 1},
            ),
        ],
    )
    stages = [
        LocustStage(
            duration_s=trial_length,
            # users=num_replicas // 10,
            users=50,
            spawn_rate=10,
        ),
        LocustStage(
            duration_s=trial_length,
            # users=num_replicas,
            users=100,
            spawn_rate=10,
        ),
        LocustStage(
            duration_s=trial_length,
            # users=num_replicas * 2,
            users=500,
            spawn_rate=10,
        ),
    ]

    with start_service(
        service_name="scalability-1k-replicas",
        compute_config=compute_config,
        applications=[noop_1k_application],
    ) as service_name:
        ray.init("auto")
        status = service.status(name=service_name)

        # Start the locust workload
        num_locust_workers = int(ray.available_resources()["CPU"]) - 1
        stats = run_locust_load_test(
            LocustLoadTestConfig(
                num_workers=num_locust_workers,
                host_url=status.query_url,
                auth_token=status.query_auth_token,
                data=None,
                stages=stages,
            )
        )
        results_per_stage = [
            [
                {
                    "perf_metric_name": f"stage_{i+1}_p50_latency",
                    "perf_metric_value": stats.stats_in_stages[i].p50_latency,
                    "perf_metric_type": "LATENCY",
                },
                {
                    "perf_metric_name": f"stage_{i+1}_p90_latency",
                    "perf_metric_value": stats.stats_in_stages[i].p90_latency,
                    "perf_metric_type": "LATENCY",
                },
                {
                    "perf_metric_name": f"stage_{i+1}_p99_latency",
                    "perf_metric_value": stats.stats_in_stages[i].p99_latency,
                    "perf_metric_type": "LATENCY",
                },
                {
                    "perf_metric_name": "rps",
                    "perf_metric_value": stats.stats_in_stages[i].rps,
                    "perf_metric_type": "THROUGHPUT",
                },
            ]
            for i in range(len(stages))
        ]
        results = {
            "total_requests": stats.total_requests,
            "history": stats.history,
            "perf_metrics": sum(
                results_per_stage,
                [
                    {
                        "perf_metric_name": "avg_latency",
                        "perf_metric_value": stats.avg_latency,
                        "perf_metric_type": "LATENCY",
                    },
                    {
                        "perf_metric_name": "p50_latency",
                        "perf_metric_value": stats.p50_latency,
                        "perf_metric_type": "LATENCY",
                    },
                    {
                        "perf_metric_name": "p90_latency",
                        "perf_metric_value": stats.p90_latency,
                        "perf_metric_type": "LATENCY",
                    },
                    {
                        "perf_metric_name": "p99_latency",
                        "perf_metric_value": stats.p99_latency,
                        "perf_metric_type": "LATENCY",
                    },
                    {
                        "perf_metric_name": "avg_rps",
                        "perf_metric_value": stats.avg_rps,
                        "perf_metric_type": "THROUGHPUT",
                    },
                ],
            ),
        }
        import json

        logger.info(f"Final aggregated metrics: {json.dumps(results, indent=4)}")
        save_test_results(results)


if __name__ == "__main__":
    main()
    import pytest
    import sys

    sys.exit(pytest.main(["-v", "-s", __file__]))
