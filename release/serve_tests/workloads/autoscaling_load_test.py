#!/usr/bin/env python3
"""
Benchmark test.
"""

import click
import logging
import os
import boto3
from tqdm import tqdm
from typing import Optional

from anyscale import AnyscaleSDK, service
from anyscale.authenticate import AuthenticationBlock
from locust_load import LocustMaster, LocustWorker

import ray
from ray._private.test_utils import wait_for_condition
from serve_test_utils import save_test_results

logger = logging.getLogger(__file__)
logging.basicConfig(level=logging.INFO)


SERVICE_NAME = f"autoscaling-locust-load-{ray.__commit__[:6]}"


class DeferredEnvVar:
    def __init__(self, var: str, default: Optional[str] = None):
        self._var = var
        self._default = default

    def __str__(self):
        return os.environ.get(self._var, self._default)


RELEASE_AWS_ANYSCALE_SECRET_ARN = DeferredEnvVar(
    "RELEASE_AWS_ANYSCALE_SECRET_ARN",
    "arn:aws:secretsmanager:us-west-2:029272617770:secret:"
    "release-automation/"
    "anyscale-token20210505220406333800000001-BcUuKB",
)


def get_anyscale_cli_token() -> str:
    try:
        token, _ = AuthenticationBlock._load_credentials()
        logger.info("Loaded anyscale credentials from local storage.")
        return token
    except Exception:
        pass  # Ignore errors

    logger.info("Missing ANYSCALE_CLI_TOKEN, retrieving from AWS secrets store")
    return boto3.client("secretsmanager", region_name="us-west-2").get_secret_value(
        SecretId=str(RELEASE_AWS_ANYSCALE_SECRET_ARN)
    )["SecretString"]


def get_current_build_id(sdk: AnyscaleSDK) -> str:
    cluster_id = os.environ["ANYSCALE_CLUSTER_ID"]
    cluster = sdk.get_cluster(cluster_id)
    return cluster.result.cluster_environment_build_id


def build_id_to_image_uri(sdk: AnyscaleSDK, build_id: str) -> str:
    build = sdk.get_cluster_environment_build(build_id).result
    cluster_env = sdk.get_cluster_environment(build.cluster_environment_id).result
    return f"anyscale/cluster_env/{cluster_env.name}:{build.revision}"


def check_service_state(sdk: AnyscaleSDK, expected_state: str):
    state = sdk.get_service("service2_bthdfjvf8jnxcesija1tnuhpdc").result.current_state
    assert str(state) == expected_state
    return True


@click.command()
def main():
    if not os.environ.get("ANYSCALE_CLI_TOKEN"):
        os.environ["ANYSCALE_CLI_TOKEN"] = get_anyscale_cli_token()

    sdk = AnyscaleSDK()

    current_build_id = get_current_build_id(sdk)
    image_uri = build_id_to_image_uri(sdk, current_build_id)
    service_config = service.ServiceConfig(
        name=SERVICE_NAME,
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
    service.deploy(service_config)

    logger.info(f"Service config: {service_config}")

    wait_for_condition(
        check_service_state, sdk=sdk, expected_state="RUNNING", timeout=600
    )

    status = service.status(name=SERVICE_NAME)
    ray.init(address="auto")
    num_locust_workers = int(ray.available_resources()["CPU"]) - 1
    master_address = ray.util.get_node_ip_address()

    logger.info(f"Spawning {num_locust_workers} Locust worker Ray tasks.")
    locust_workers = []
    start_refs = []
    for _ in tqdm(range(num_locust_workers)):
        locust_worker = LocustWorker.remote(
            status.query_url, status.query_auth_token, master_address
        )
        locust_workers.append(locust_worker)
        start_refs.append(locust_worker.run.remote())

    # Start master locust worker and wait for it to finish
    master_worker = LocustMaster.remote(
        status.query_url, status.query_auth_token, num_locust_workers
    )
    master_ref = master_worker.run.remote()
    stats = ray.get(master_ref)
    if stats.pop("num_failures") > 0:
        raise RuntimeError()

    logger.info(f"Final aggregated metrics: {stats}")
    save_test_results(stats)

    logger.info(f"Terminating service {SERVICE_NAME}.")
    service.terminate(name=SERVICE_NAME)
    wait_for_condition(
        check_service_state, sdk=sdk, expected_state="TERMINATED", timeout=600
    )
    logger.info(f"Service '{SERVICE_NAME}' terminated successfully.")


if __name__ == "__main__":
    main()
    import pytest
    import sys

    sys.exit(pytest.main(["-v", "-s", __file__]))
