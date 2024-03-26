#!/usr/bin/env python3
"""
Benchmark test.
"""

import time
import click
import subprocess
import logging
import os
import boto3
from tqdm import tqdm
from typing import Optional
import yaml

from anyscale import AnyscaleSDK, service
from anyscale.authenticate import AuthenticationBlock
from locust_runner import LocustMaster, LocustWorker

import ray
from ray import serve
from ray._private.test_utils import wait_for_condition
from serve_test_utils import is_smoke_test, save_test_results

logger = logging.getLogger(__file__)

# Experiment configs
DEFAULT_SMOKE_TEST_MIN_NUM_REPLICA = 0
DEFAULT_SMOKE_TEST_MAX_NUM_REPLICA = 4
DEFAULT_FULL_TEST_MIN_NUM_REPLICA = 0
DEFAULT_FULL_TEST_MAX_NUM_REPLICA = 1000

# Deployment configs
DEFAULT_MAX_BATCH_SIZE = 16

# Experiment configs - wrk specific
DEFAULT_SMOKE_TEST_TRIAL_LENGTH = "15s"
DEFAULT_FULL_TEST_TRIAL_LENGTH = "10m"

SERVICE_NAME = "testing-autoscaling-load-test"


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
        print("Loaded anyscale credentials from local storage.")
        # os.environ["ANYSCALE_CLI_TOKEN"] = token
        return token
    except Exception:
        pass  # Ignore errors

    print("Missing ANYSCALE_CLI_TOKEN, retrieving from AWS secrets store")
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
@click.option("--min-replicas", "-min", type=int)
@click.option("--max-replicas", "-max", type=int)
@click.option("--trial-length", "-tl", type=str)
@click.option("--max-batch-size", type=int, default=DEFAULT_MAX_BATCH_SIZE)
def main(
    min_replicas: Optional[int],
    max_replicas: Optional[int],
    trial_length: Optional[str],
    max_batch_size: Optional[int],
):
    if is_smoke_test():
        pass
    else:
        min_replicas = min_replicas or DEFAULT_FULL_TEST_MIN_NUM_REPLICA
        max_replicas = max_replicas or DEFAULT_FULL_TEST_MAX_NUM_REPLICA
        trial_length = trial_length or DEFAULT_FULL_TEST_TRIAL_LENGTH
        print(
            f"Running full test with min {min_replicas} and max "
            f"{max_replicas} replicas ..\n"
        )
        print("Setting up anyscale ray cluster .. \n")

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

    print("service config", service_config)

    wait_for_condition(
        check_service_state, sdk=sdk, expected_state="RUNNING", timeout=600
    )

    # r = requests.get(
    #     "https://gist.githubusercontent.com/zcin/daffaf8b25e961728070dab5c43feeb6/raw/locust_runner.py"
    # )
    # open("locust_runner.py", "wb").write(r.content)
    # with open("locust_runner.py", "r") as f:
    #     print("reading locust_runner.py", f.read())

    # r = requests.get(
    #     "https://gist.githubusercontent.com/zcin/daffaf8b25e961728070dab5c43feeb6/raw/locustfile.py"
    # )
    # open("locustfile.py", "wb").write(r.content)
    # open("/home/ray/default/locustfile.py", "wb").write(r.content)
    # with open("locustfile.py", "r") as f:
    #     print("reading locustfile.py", f.read())
    # with open("/home/ray/default/locustfile.py", "r") as f:
    #     print("reading /home/ray/default/locustfile.py", f.read())

    # os.environ["SERVICE_TOKEN"] = "JjSBq5r3GfxIrIHb2STzH0RqAlgBzIuLjKgY1GW4c2M"
    status = service.status(name=SERVICE_NAME)
    print("status query url", status.query_url)
    print("status query auth token", status.query_auth_token)
    # proc = subprocess.Popen(
    #     [
    #         "python",
    #         "workloads/locust_runner.py",
    #         status.query_url,
    #         "--token",
    #         status.query_auth_token,
    #     ],
    #     stderr=subprocess.STDOUT,
    # )
    # proc.communicate()
    ray.init(address="auto")
    num_locust_workers = int(ray.available_resources()["CPU"]) - 1
    master_address = ray.util.get_node_ip_address()

    print(f"Spawning {num_locust_workers} Locust worker Ray tasks.")
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
    print(f"Final aggregated metrics: {stats}")
    save_test_results(stats)

    print(f"Terminating service {SERVICE_NAME}.")
    service.terminate(name=SERVICE_NAME)
    wait_for_condition(
        check_service_state, sdk=sdk, expected_state="TERMINATED", timeout=600
    )
    print(f"Service '{SERVICE_NAME}' terminated successfully.")


if __name__ == "__main__":
    main()
    import pytest
    import sys

    sys.exit(pytest.main(["-v", "-s", __file__]))
