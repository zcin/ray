#!/usr/bin/env python3
"""
Benchmark test.
"""

import yaml
import time
import requests
import click
import subprocess
import logging
import os

import boto3
from ray import serve
from serve_test_utils import (
    is_smoke_test,
)
from typing import Optional

# from ray_release.aws import maybe_fetch_api_token
from anyscale import AnyscaleSDK
from anyscale.service import ServiceConfig
from ray._private.test_utils import wait_for_condition
from anyscale.authenticate import AuthenticationBlock

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


def get_current_build_id() -> str:
    sdk = AnyscaleSDK()

    cluster_id = os.environ["ANYSCALE_CLUSTER_ID"]
    cluster = sdk.get_cluster(cluster_id)
    build_id = cluster.result.cluster_environment_build_id

    # build = sdk.get_cluster_environment_build(build_id).result
    # cluster_env = sdk.get_cluster_environment(build.cluster_environment_id).result
    # identifier = f"anyscale/cluster_env/{cluster_env.name}:{build.revision}"

    return build_id


def deploy_replicas(min_replicas, max_replicas, max_batch_size):
    @serve.deployment(
        name="echo",
        autoscaling_config={
            "metrics_interval_s": 0.1,
            "min_replicas": min_replicas,
            "max_replicas": max_replicas,
            "look_back_period_s": 0.2,
            "downscale_delay_s": 0.2,
            "upscale_delay_s": 0.2,
        },
    )
    class Echo:
        @serve.batch(max_batch_size=max_batch_size)
        async def handle_batch(self, requests):
            return ["hi" for _ in range(len(requests))]

        async def __call__(self, request):
            return await self.handle_batch(request)

    serve.run(Echo.bind(), name="echo", route_prefix="/echo")


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

    # cluster_id = os.environ["ANYSCALE_CLUSTER_ID"]
    # sdk = AnyscaleSDK()
    # cluster = sdk.get_cluster(cluster_id)
    # build_id = cluster.result.cluster_environment_build_id
    # build = sdk.get_cluster_environment_build(build_id).result
    # cluster_env = sdk.get_cluster_environment(build.cluster_environment_id).result
    # identifier = f"anyscale/cluster_env/{cluster_env.name}:{build.revision}"

    # ServiceConfig(
    #     name="testing-autoscaling-load-test",
    #     image_uri=identifier,
    #     applications=[
    #         {
    #             "import_path": "updatable_serve_app:app",
    #             "deployments": [
    #                 {
    #                     "name": "Updatable",
    #                     "num_replicas": 2,
    #                     "user_config": {
    #                         "response": "Hello from e2e test!",
    #                         "should_fail": False,
    #                     },
    #                 },
    #             ],
    #         }
    #     ],
    # )
    service_config = {
        "name": "testing-autoscaling-load-test",
        "build_id": get_current_build_id(),
        "cloud": "anyscale_v2_default_cloud",
        "ray_serve_config": {
            "applications": [
                {
                    "name": "default",
                    "import_path": "serve_hello:entrypoint",
                    "route_prefix": "/",
                    "runtime_env": {
                        "working_dir": "https://github.com/anyscale/docs_examples/archive/refs/heads/main.zip",
                        "env_vars": {"SERVE_RESPONSE_MESSAGE": "service says hello"},
                    },
                }
            ]
        },
    }
    with open("service.yaml", "w+") as f:
        f.write(yaml.dump(service_config))

    # debugging
    with open("service.yaml", "r") as f:
        print("service yaml:", f.read())
        print(f.read())

    output = subprocess.check_output(["anyscale", "--version"])
    print(f"anyscale version output: { output.decode('utf-8') }")

    subprocess.check_output(["anyscale", "service", "rollout", "-f", "service.yaml"])

    sdk = AnyscaleSDK()

    def check_running():
        state = sdk.get_service(
            "service2_bthdfjvf8jnxcesija1tnuhpdc"
        ).result.current_state
        assert str(state) == "RUNNING"
        return True

    wait_for_condition(check_running, timeout=600)

    print("current working directory", os.getcwd())

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

    os.environ["SERVICE_TOKEN"] = "JjSBq5r3GfxIrIHb2STzH0RqAlgBzIuLjKgY1GW4c2M"
    output = subprocess.check_output(["python", "workloads/locust_runner.py"])
    print("output", output)

    # http_host = str(serve_client._http_config.host)
    # http_port = str(serve_client._http_config.port)
    # logger.info(f"Ray serve http_host: {http_host}, http_port: {http_port}")

    # deploy_proxy_replicas()

    # logger.info(
    #     f"Deploying with min {min_replicas} and max {max_replicas} "
    #     f"target replicas ....\n"
    # )
    # deploy_replicas(min_replicas, max_replicas, max_batch_size)

    # logger.info("Warming up cluster ....\n")
    # warm_up_one_cluster.remote(10, http_host, http_port, "echo")

    # logger.info(f"Starting wrk trial on all nodes for {trial_length} ....\n")
    # # For detailed discussion, see https://github.com/wg/wrk/issues/205
    # # TODO:(jiaodong) What's the best number to use here ?
    # all_endpoints = ["/echo"]

    # aggregated_metrics = aggregate_all_metrics(all_metrics)
    # logger.info("Wrk stdout on each node: ")
    # for wrk_stdout in all_wrk_stdout:
    #     logger.info(wrk_stdout)
    # logger.info("Final aggregated metrics: ")
    # for key, val in aggregated_metrics.items():
    #     logger.info(f"{key}: {val}")
    # save_test_results(
    #     aggregated_metrics,
    #     default_output_file="/tmp/autoscaling_single_deployment.json",
    # )
    print("Sleeping for 10 minutes to allow time for debugging...")
    time.sleep(600)


if __name__ == "__main__":
    main()
    import pytest
    import sys

    sys.exit(pytest.main(["-v", "-s", __file__]))
