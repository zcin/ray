#!/usr/bin/env python3
"""
Benchmark test.
"""

import yaml
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
from ray._private.test_utils import wait_for_condition

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
        logger.info(
            f"Running full test with min {min_replicas} and max "
            f"{max_replicas} replicas ..\n"
        )
        logger.info("Setting up anyscale ray cluster .. \n")
        # serve_client = setup_anyscale_cluster()

    # maybe_fetch_api_token()
    from anyscale.authenticate import AuthenticationBlock

    if not os.environ.get("ANYSCALE_CLI_TOKEN"):
        try:
            token, _ = AuthenticationBlock._load_credentials()
            logger.info("Loaded anyscale credentials from local storage.")
            os.environ["ANYSCALE_CLI_TOKEN"] = token
            return
        except Exception:
            pass  # Ignore errors

        logger.info("Missing ANYSCALE_CLI_TOKEN, retrieving from AWS secrets store")
        # NOTE(simon) This should automatically retrieve
        # release-automation@anyscale.com's anyscale token
        cli_token = boto3.client(
            "secretsmanager", region_name="us-west-2"
        ).get_secret_value(SecretId=str(RELEASE_AWS_ANYSCALE_SECRET_ARN))[
            "SecretString"
        ]
        os.environ["ANYSCALE_CLI_TOKEN"] = cli_token

    cluster_id = os.environ["ANYSCALE_CLUSTER_ID"]
    sdk = AnyscaleSDK()
    cluster = sdk.get_cluster(cluster_id)
    build_id = cluster.result.cluster_environment_build_id

    service_config = {
        "name": "testing-autoscaling-load-test",
        "build_id": build_id,
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
        print(f.read())
        logger.info(f.read())

    output = subprocess.check_output(["anyscale", "--version"])
    logger.info(f"anyscale version output: { output.decode('utf-8') }")

    subprocess.check_output(["anyscale", "service", "rollout", "-f", "service.yaml"])

    def check_running():
        state = sdk.get_service(
            "service2_bthdfjvf8jnxcesija1tnuhpdc"
        ).result.current_state
        assert str(state) == "RUNNING"
        return True

    wait_for_condition(check_running, timeout=600)

    r = requests.get(
        "https://gist.githubusercontent.com/zcin/daffaf8b25e961728070dab5c43feeb6/raw/741dd04ea003be5a51acd425797e88b181289c76/locust_runner.py"
    )
    open("locust_runner.py", "wb").write(r.content)
    r = requests.get(
        "https://gist.githubusercontent.com/zcin/daffaf8b25e961728070dab5c43feeb6/raw/741dd04ea003be5a51acd425797e88b181289c76/locustfile.py"
    )
    open("locustfile.py", "wb").write(r.content)

    output = subprocess.check_output(
        [
            'SERVICE_TOKEN="JjSBq5r3GfxIrIHb2STzH0RqAlgBzIuLjKgY1GW4c2M"',
            "python",
            "locust_runner.py",
            "-f",
            "locustfile.py",
            "--host",
            "https://testing-autoscaling-load-test-jrvwy.cld-kvedzwag2qa8i5bj.s.anyscaleuserdata-staging.com/",
        ]
    )
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


if __name__ == "__main__":
    main()
    import pytest
    import sys

    sys.exit(pytest.main(["-v", "-s", __file__]))
