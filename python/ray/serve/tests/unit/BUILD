load("//bazel:python.bzl", "py_test_run_all_subdirectory")

py_library(
  name = "conftest",
  srcs = ["conftest.py"],
)

py_test_run_all_subdirectory(
  include = glob(["test_*.py"]),
  exclude = [],
  extra_srcs = [],
  size = "small",
  tags = ["team:serve"],
  deps = ["//python/ray/serve:serve_lib", "//python/ray/serve/tests:common", ":conftest"],
)

py_test_module_list(
  name_suffix="_with_stop_fully_then_start_behavior",
  env={"RAY_SERVE_STOP_FULLY_THEN_START_REPLICAS": "1"},
  files = [
    "test_deployment_state.py",
  ],
  size = "medium",
  tags = ["exclusive", "no_windows", "team:serve"],
  deps = ["//python/ray/serve:serve_lib", ":conftest", ":common"],
  data = glob(["test_config_files/**/*"]),
)