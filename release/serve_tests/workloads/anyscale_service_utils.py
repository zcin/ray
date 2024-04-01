import boto3
from contextlib import contextmanager
import logging
import os
from typing import Dict, List, Optional, Union

from anyscale import service
from anyscale.authenticate import AuthenticationBlock
import ray
from ray._private.test_utils import wait_for_condition
from ray.serve._private.utils import get_random_string


logger = logging.getLogger(__file__)
logging.basicConfig(level=logging.INFO)

DEFAULT_CLUSTER_ENV = "default_cluster_env_nightly-ml_py39"


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


def check_service_state(service_name: str, expected_state: str):
    state = service.status(name=service_name).state
    logger.info(
        f"Waiting for service {service_name} to be {expected_state}, currently {state}"
    )
    assert (
        str(state) == expected_state
    ), f"Service {service_name} is {state}, expected {expected_state}."
    return True


# IMAGE_URI = (
#     "anyscale/image/029272617770_dkr_ecr_us-west-2_amazonaws_com_anyscale_ray-ml_pr-"
#     "42421_56a0db-py39-gpu-59a4f3f3d77a575e353cad97e5b133f9f8ed0fa5a9548c934ccbb904754877ac"  # noqa
#     "__env__44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a:1"
# )


@contextmanager
def start_service(
    service_name: str,
    compute_config: Union[str, Dict],
    applications: List[Dict],
    add_unique_suffix: bool = True,
):
    """Starts an Anyscale Service with the specified configs.

    Args:
        service_name: Name of the Anyscale Service. The actual service
            name may be modified if `add_unique_suffix` is True.
        compute_config: The configuration for the hardware resources
            that the cluster will utilize.
        applications: The list of Ray Serve applications to run in the
            service.
        add_unique_suffix: Whether to append a unique suffix to the
            service name.
    """

    if not os.environ.get("ANYSCALE_CLI_TOKEN"):
        os.environ["ANYSCALE_CLI_TOKEN"] = get_anyscale_cli_token()

    cluster_env = os.environ.get("ANYSCALE_JOB_CLUSTER_ENV_NAME", DEFAULT_CLUSTER_ENV)
    print("os.environ", os.environ)

    if add_unique_suffix:
        ray_version = (
            ray.__commit__[:8]
            if ray.__commit__ != "{{RAY_COMMIT_SHA}}"
            else get_random_string()
        )
        service_name = f"{service_name}-{ray_version}"

    service_config = service.ServiceConfig(
        name=service_name,
        image_uri=f"anyscale/image/{cluster_env}:1",
        compute_config=compute_config,
        applications=applications,
    )
    try:
        logger.info(f"Service config: {service_config}")
        service.deploy(service_config)

        wait_for_condition(
            check_service_state,
            service_name=service_name,
            expected_state="RUNNING",
            retry_interval_ms=10000,  # 10s
            timeout=600,
        )

        yield service_name

    finally:
        logger.info(f"Terminating service {service_name}.")
        service.terminate(name=service_name)
        wait_for_condition(
            check_service_state,
            service_name=service_name,
            expected_state="TERMINATED",
            retry_interval_ms=10000,  # 10s
            timeout=600,
        )
        logger.info(f"Service '{service_name}' terminated successfully.")
