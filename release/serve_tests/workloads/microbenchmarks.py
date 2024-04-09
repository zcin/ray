"""Runs benchmarks.

Latency benchmarks:
    Runs a no-op workload with 1 replica.
    Sends 100 requests to it and records average, P50, P90, P95, P99 latencies.

Throughput benchmarks:
    Asynchronously send batches of 100 requests.
    Calculate the average throughput achieved on 10 batches of requests.
"""
import asyncio
from functools import partial
import json
import logging

import click
import grpc
import pandas as pd
import requests
from typing import Dict, List

from ray import serve
from ray.serve._private.benchmarks.common import (
    Benchmarker,
    do_single_grpc_batch,
    do_single_http_batch,
    Hello,
    Noop,
    run_latency_benchmark,
    run_throughput_benchmark,
)
from ray.serve.generated import serve_pb2, serve_pb2_grpc
from ray.serve.config import gRPCOptions
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
    def __init__(self):
        logging.getLogger("ray.serve").setLevel(logging.WARNING)

    async def grpc_call(self, user_message):
        return serve_pb2.ModelOutput(output=9)


def convert_throughput_to_perf_metrics(
    name: str, mean: float, std: float
) -> List[Dict]:
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
    # Start and configure Serve
    serve.start(
        grpc_options=gRPCOptions(
            port=9000,
            grpc_servicer_functions=[
                "ray.serve.generated.serve_pb2_grpc.add_RayServeBenchmarkServiceServicer_to_server",  # noqa
            ],
        )
    )
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

    # Microbenchmark: GRPC latencies
    serve.run(GrpcDeployment.bind())
    channel = grpc.insecure_channel("localhost:9000")
    stub = serve_pb2_grpc.RayServeBenchmarkServiceStub(channel)
    http_latencies: pd.Series = asyncio.run(
        run_latency_benchmark(
            lambda: stub.grpc_call(serve_pb2.RawData(nums=[1])),
            num_requests=NUM_REQUESTS,
        )
    )
    perf_metrics.extend(convert_latencies_to_perf_metrics("grpc", http_latencies))

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

    # Microbenchmark: GRPC throughput
    serve.run(GrpcDeployment.bind())
    mean, std = asyncio.run(
        run_throughput_benchmark(
            fn=partial(do_single_grpc_batch, batch_size=BATCH_SIZE),
            multiplier=100,
            num_trials=10,
            trial_runtime=1,
        )
    )
    perf_metrics.extend(convert_throughput_to_perf_metrics("grpc", mean, std))

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
