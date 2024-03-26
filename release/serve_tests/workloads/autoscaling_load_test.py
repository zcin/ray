#!/usr/bin/env python3
"""
Benchmark test.
"""

from contextlib import contextmanager
import json
import logging
import os
from itertools import chain

import click
from anyscale import AnyscaleSDK, service
from locust_load import LocustMaster, LocustWorker
from serve_test_utils import (
    build_id_to_image_uri,
    check_service_state,
    get_anyscale_cli_token,
    get_current_build_id,
    save_test_results,
)
from tqdm import tqdm

import ray
from ray._private.test_utils import wait_for_condition


logger = logging.getLogger(__file__)
logging.basicConfig(level=logging.INFO)


# SERVICE_NAME = f"autoscaling-locust-load-{ray.__commit__[:7]}"
# SERVICE_NAME = "testing-autoscaling-load-test"


@contextmanager
def start_service(sdk: AnyscaleSDK):
    if not os.environ.get("ANYSCALE_CLI_TOKEN"):
        os.environ["ANYSCALE_CLI_TOKEN"] = get_anyscale_cli_token()

    service_name = "testing-autoscaling-load-test"
    current_build_id = get_current_build_id(sdk)
    image_uri = build_id_to_image_uri(sdk, current_build_id)
    service_config = service.ServiceConfig(
        name=service_name,
        image_uri=image_uri,
        compute_config="cindy-all32-min0worker:1",
        applications=[
            {
                "name": "default",
                "import_path": "resnet_50:app",
                "route_prefix": "/",
                "runtime_env": {"working_dir": "workloads"},
                "deployments": [{"name": "Model", "num_replicas": "auto"}],
            }
        ],
    )
    try:
        service.deploy(service_config)
        logger.info(f"Service config: {service_config}")
        wait_for_condition(
            check_service_state, sdk=sdk, expected_state="RUNNING", timeout=600
        )

        yield service_name

    finally:
        logger.info(f"Terminating service {service_name}.")
        service.terminate(name=service_name)
        wait_for_condition(
            check_service_state, sdk=sdk, expected_state="TERMINATED", timeout=600
        )
        logger.info(f"Service '{service_name}' terminated successfully.")


@click.command()
def main():
    sdk = AnyscaleSDK()

    with start_service(sdk) as service_name:
        ray.init(address="auto")
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
            status.query_url, status.query_auth_token, num_locust_workers
        )
        master_ref = master_worker.run.remote()

        # Collect results and metrics
        stats = ray.get(master_ref)
        errors = sorted(chain(*ray.get(worker_refs)), key=lambda e: e["start_time"])

        if stats.pop("num_failures") > 0:
            raise RuntimeError(
                f"There were failed requests: {json.dumps(errors, indent=4)}"
            )
        else:
            logger.info(f"Final aggregated metrics: {stats}")
            save_test_results(stats)


if __name__ == "__main__":
    main()
    import pytest
    import sys

    sys.exit(pytest.main(["-v", "-s", __file__]))
