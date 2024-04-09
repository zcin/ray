"""Runs benchmarks.

Latency benchmarks:
    Runs a no-op workload with 1 replica.
    Sends 100 requests to it and records average, P50, P90, P95, P99 latencies.

Throughput benchmarks:

"""
import asyncio
from functools import partial
import json
import logging

import click
import pandas as pd
import requests
from typing import Dict, List, Tuple

from ray import serve
from ray.serve._private.benchmarks.common import (
    Benchmarker,
    do_single_http_batch,
    Hello,
    Noop,
    run_latency_benchmark,
    run_throughput_benchmark,
)
from ray.serve._private.benchmarks.streaming._grpc import (
    test_server_pb2,
    test_server_pb2_grpc,
)
from ray.serve.handle import DeploymentHandle

from serve_test_utils import save_test_results

logger = logging.getLogger(__file__)
logging.basicConfig(level=logging.INFO)


# For latency benchmarks
NUM_REQUESTS = 100

# For throughput benchmarks
BATCH_SIZE = 100
NUM_TRIALS = 10
TRIAL_RUNTIME_S = 1


@serve.deployment
class GrpcDeployment:
    def Unary(self, request, context):
        pass

    def ClientStreaming(self, request_iterator, context):
        pass

    def ServerStreaming(self, request, context):
        pass

    def BidiStreaming(self, request_iterator, context):
        pass



def convert_throughput_to_perf_metrics(name: str, mean: float, std: float) -> List[Dict]:
    return [
        {
            "perf_metric_name": f"{name}_avg_rps",
            "perf_metric_value": mean,
            "perf_metric_type": "THROUGHPUT",
        },
        {
            "perf_metric_name": f"{name}_throughput_std",
            "perf_metric_value": std,
            "perf_metric_type": "THROUGHPUT",
        },
    ]


def convert_latencies_to_perf_metrics(name: str, latencies: pd.Series) -> List[Dict]:
    return [
        {
            "perf_metric_name": f"{name}_avg_latency",
            "perf_metric_value": latencies.mean(),
            "perf_metric_type": "LATENCY",
        },
        {
            "perf_metric_name": f"{name}_p50_latency",
            "perf_metric_value": latencies.quantile(0.5),
            "perf_metric_type": "LATENCY",
        },
        {
            "perf_metric_name": f"{name}_p90_latency",
            "perf_metric_value": latencies.quantile(0.9),
            "perf_metric_type": "LATENCY",
        },
        {
            "perf_metric_name": f"{name}_p95_latency",
            "perf_metric_value": latencies.quantile(0.95),
            "perf_metric_type": "LATENCY",
        },
        {
            "perf_metric_name": f"{name}_p99_latency",
            "perf_metric_value": latencies.quantile(0.99),
            "perf_metric_type": "LATENCY",
        },
    ]


@click.command(help="Benchmark Serve performance.")
def main():
    perf_metrics = []

    # Microbenchmark: HTTP noop latencies
    serve.run(Noop.bind())
    http_latencies: pd.Series = asyncio.run(
        run_latency_benchmark(
            lambda: requests.get("http://localhost:8000"),
            num_requests=NUM_REQUESTS,
        )
    )
    perf_metrics.extend(convert_latencies_to_perf_metrics("http", http_latencies))

    # Microbenchmark: GRPC noop latencies

    # Microbenchmark: Handle noop latencies
    h: DeploymentHandle = serve.run(Benchmarker.bind(Noop.bind()))
    handle_latencies: pd.Series = h.run_latency_benchmark.remote(NUM_REQUESTS).result()
    perf_metrics.extend(convert_latencies_to_perf_metrics("handle", handle_latencies))

    # Microbenchmark: HTTP throughput
    serve.run(Hello.bind())
    mean, std = asyncio.run(
        run_throughput_benchmark(
            fn=partial(do_single_http_batch, batch_size=BATCH_SIZE),
            multiplier=BATCH_SIZE,
            num_trials=NUM_TRIALS,
            trial_runtime=TRIAL_RUNTIME_S,
        )
    )
    perf_metrics.extend(convert_throughput_to_perf_metrics("http", mean, std))

    # Microbenchmark: Handle throughput
    h: DeploymentHandle = serve.run(Benchmarker.bind(Hello.bind()))
    mean, std = h.run_throughput_benchmark.remote(
        batch_size=BATCH_SIZE, num_trials=NUM_TRIALS, trial_runtime=TRIAL_RUNTIME_S
    ).result()
    perf_metrics.extend(convert_throughput_to_perf_metrics("handle", mean, std))

    logging.info(f"Perf metrics:\n {json.dumps(perf_metrics, indent=4)}")
    results = {"perf_metrics": perf_metrics}
    save_test_results(results)


if __name__ == "__main__":
    main()
