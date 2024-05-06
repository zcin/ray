from ray._private.runtime_env.container import ContainerPlugin


def get_container_plugin(ray_tmp_dir: str):
    return ContainerPlugin(ray_tmp_dir)
