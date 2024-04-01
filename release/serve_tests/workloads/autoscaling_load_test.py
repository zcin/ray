#!/usr/bin/env python3
"""
Benchmark test.
"""

import logging

import click
from anyscale import service
from anyscale_service_utils import start_service
from locust_utils import LocustLoadTestConfig, LocustStage, run_locust_load_test
from serve_test_utils import save_test_results

import ray


logger = logging.getLogger(__file__)
logging.basicConfig(level=logging.INFO)


URI = "https://serve-resnet-benchmark-data.s3.us-west-1.amazonaws.com/000000000019.jpeg"


@click.command()
def main():
    resnet_application = {
        "name": "default",
        "import_path": "resnet_50:app",
        "route_prefix": "/",
        "runtime_env": {"working_dir": "workloads"},
        "deployments": [{"name": "Model", "num_replicas": "auto"}],
    }

    with start_service(
        "autoscaling-locust-test",
        compute_config="cindy-all32-min0worker:1",
        applications=[resnet_application],
    ) as service_name:
        ray.init(address="auto")
        status = service.status(name=service_name)

        # Start the locust workload
        num_locust_workers = int(ray.available_resources()["CPU"]) - 1
        stats = run_locust_load_test(
            LocustLoadTestConfig(
                num_workers=num_locust_workers,
                host_url=status.query_url,
                auth_token=status.query_auth_token,
                data={"uri": URI},
                stages=[LocustStage(duration_s=60, users=10, spawn_rate=1)],
            )
        )
        results = {
            "total_requests": stats.total_requests,
            "history": stats.history,
            "perf_metrics": [
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
        }
        logger.info(f"Final aggregated metrics: {results}")
        save_test_results(results)


if __name__ == "__main__":
    main()
    import pytest
    import sys

    sys.exit(pytest.main(["-v", "-s", __file__]))
