import logging
import os
from typing import List, Optional

from ray._private.runtime_env.context import RuntimeEnvContext
from ray._private.runtime_env.plugin import RuntimeEnvPlugin

default_logger = logging.getLogger(__name__)


class ContainerPlugin(RuntimeEnvPlugin):
    """Starts worker in container."""

    name = "container"

    def __init__(self, ray_tmp_dir: str):
        self._ray_tmp_dir = ray_tmp_dir

    def modify_context(
        self,
        uris: List[str],
        runtime_env: "RuntimeEnv",  # noqa: F821
        context: RuntimeEnvContext,
        logger: Optional[logging.Logger] = default_logger,
    ):
        if not runtime_env.has_py_container() or not runtime_env.py_container_image():
            return

        container_driver = "podman"
        context.container = runtime_env["container"]
        container_command = [
            container_driver,
            "run",
            "-v",
            self._ray_tmp_dir + ":" + self._ray_tmp_dir,
            "--cgroup-manager=cgroupfs",
            "--network=host",
            "--pid=host",
            "--ipc=host",
            # NOTE(zcin): Mounted volumes in rootless containers are
            # owned by the user `root`. The user on host (which will
            # usually be `ray` if this is being run in a ray docker
            # image) who started the container is mapped using user
            # namespaces to the user `root` in a rootless container. In
            # order for the Ray Python worker to access the mounted ray
            # tmp dir, we need to use keep-id mode which maps the user
            # as itself (instead of as `root`) into the container.
            # https://www.redhat.com/sysadmin/rootless-podman-user-namespace-modes
            "--userns=keep-id",
        ]

        # The RAY_RAYLET_PID and RAY_JOB_ID environment variables are
        # needed for the default worker.
        container_command.append("--env")
        container_command.append("RAY_RAYLET_PID=" + os.getenv("RAY_RAYLET_PID"))
        container_command.append("--env")
        container_command.append("RAY_JOB_ID=$RAY_JOB_ID")
        for env_var_name, env_var_value in os.environ.items():
            if env_var_name.startswith("RAY_") and env_var_name not in [
                "RAY_RAYLET_PID",
                "RAY_JOB_ID",
            ]:
                container_command.append("--env")
                container_command.append(f"{env_var_name}='{env_var_value}'")

        if runtime_env.py_container_run_options():
            container_command.extend(runtime_env.py_container_run_options())
        # TODO(chenk008): add resource limit
        container_command.append("--entrypoint")
        container_command.append("python")
        container_command.append(runtime_env.py_container_image())

        # Example:
        # podman run -v /tmp/ray:/tmp/ray
        # --cgroup-manager=cgroupfs --network=host --pid=host --ipc=host
        # --userns=keep-id --env RAY_RAYLET_PID=23478 --env RAY_JOB_ID=$RAY_JOB_ID
        # --entrypoint python rayproject/ray:nightly-py39
        container_command_str = " ".join(container_command)
        logger.info(f"Starting worker in container with prefix {container_command_str}")

        context.py_executable = container_command_str
