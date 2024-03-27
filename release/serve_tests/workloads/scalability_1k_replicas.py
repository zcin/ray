#!/usr/bin/env python3

import click
from itertools import chain
import json
import logging
from tqdm import tqdm

import ray
from ray import serve
from serve_test_utils import (
    save_test_results,
    start_service,
)
from locust_utils import LocustWorker, LocustMaster
from anyscale import AnyscaleSDK, service
from typing import List, Optional

logger = logging.getLogger(__file__)

DEFAULT_FULL_TEST_NUM_REPLICA = 1000
DEFAULT_FULL_TEST_TRIAL_LENGTH = "10m"


@click.command()
@click.option("--num-replicas", type=int)
@click.option("--trial-length", type=str)
def main(num_replicas: Optional[int], trial_length: Optional[str]):
    sdk = AnyscaleSDK()
    num_replicas = num_replicas or DEFAULT_FULL_TEST_NUM_REPLICA
    trial_length = trial_length or DEFAULT_FULL_TEST_TRIAL_LENGTH
    noop_1k_application = {
        "name": "default",
        "import_path": "noop:app",
        "route_prefix": "/",
        "runtime_env": {"working_dir": "workloads"},
        "deployments": [
            {
                "name": "Noop",
                "num_replicas": 32,
                "ray_actor_options": {"resources": {"worker_resource": 0.01}},
            }
        ],
    }

    stages = [
        {"duration": 60, "users": 200, "spawn_rate": 10},
    ]

    service_name = "scalability-1k-replica-test"
    with start_service(
        sdk=sdk,
        service_name=service_name,
        compute_config="cindy-all32-min0worker:2",
        applications=[noop_1k_application],
    ):
        ray.init("auto")
        status = service.status(name=service_name)

        # Start the locust workload
        num_locust_workers = int(ray.available_resources()["CPU"]) - 1
        logger.info(f"Spawning {num_locust_workers} Locust worker Ray tasks.")
        master_address = ray.util.get_node_ip_address()
        worker_refs = []

        # Start Locust workers
        for _ in tqdm(range(num_locust_workers)):
            locust_worker = LocustWorker.remote(
                status.query_url, status.query_auth_token, master_address
            )
            worker_refs.append(locust_worker.run.remote())

        # Start Locust master
        master_worker = LocustMaster.remote(
            status.query_url, status.query_auth_token, num_locust_workers, stages
        )
        master_ref = master_worker.run.remote()

        # Collect results and metrics
        stats = ray.get(master_ref)
        errors = sorted(chain(*ray.get(worker_refs)), key=lambda e: e["start_time"])

        if stats.get("num_failures") > 0:
            raise RuntimeError(
                f"There were failed requests: {json.dumps(errors, indent=4)}"
            )
        else:
            results = {
                "total_requests": stats["total_requests"],
                "history": stats["history"],
                "perf_metrics": [
                    {
                        "perf_metric_name": "avg_latency",
                        "perf_metric_value": stats["avg_latency"],
                        "perf_metric_type": "LATENCY",
                    },
                    {
                        "perf_metric_name": "p50_latency",
                        "perf_metric_value": stats["p50_latency"],
                        "perf_metric_type": "LATENCY",
                    },
                    {
                        "perf_metric_name": "p90_latency",
                        "perf_metric_value": stats["p90_latency"],
                        "perf_metric_type": "LATENCY",
                    },
                    {
                        "perf_metric_name": "p99_latency",
                        "perf_metric_value": stats["p99_latency"],
                        "perf_metric_type": "LATENCY",
                    },
                    {
                        "perf_metric_name": "avg_rps",
                        "perf_metric_value": stats["avg_rps"],
                        "perf_metric_type": "THROUGHPUT",
                    },
                ],
            }
            logger.info(f"Final aggregated metrics: {results}")
            save_test_results(results)


if __name__ == "__main__":
    main()
    import pytest
    import sys

    sys.exit(pytest.main(["-v", "-s", __file__]))
