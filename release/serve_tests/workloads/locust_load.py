import argparse
import time
from tqdm import tqdm
from typing import Dict, List

import ray


IMAGE_URI = "https://serve-resnet-benchmark-data.s3.us-west-1.amazonaws.com/000000000019.jpeg"  # noqa
DEFAULT_STAGES = [
    {"duration": 10, "users": 10, "spawn_rate": 1},
    {"duration": 20, "users": 50, "spawn_rate": 10},
    {"duration": 30, "users": 100, "spawn_rate": 10},
    # {"duration": 1200, "users": 10, "spawn_rate": 1},
    # {"duration": 2400, "users": 50, "spawn_rate": 10},
    # {"duration": 3600, "users": 100, "spawn_rate": 10},
]


class LocustClient:
    def __init__(self, host_url: str, token: str, stages: List[Dict]):
        from locust import task, constant, events, FastHttpUser, LoadTestShape

        class EndpointUser(FastHttpUser):
            wait_time = constant(0)
            failed_requests = []
            host = host_url

            @task
            def test(self):
                headers = {"Authorization": f"Bearer {token}"} if token else None
                self.client.get("", headers=headers, json={"uri": IMAGE_URI})

            # @events.spawning_complete.add_listener
            # def on_request(user_count: int):
            #     print(user_count)
            @events.request.add_listener
            def on_request(*args, **kwargs):
                pass

        class StagesShape(LoadTestShape):
            def tick(self):
                run_time = self.get_run_time()
                for stage in stages:
                    if run_time < stage["duration"]:
                        tick_data = (stage["users"], stage["spawn_rate"])
                        return tick_data
                return None

        self.user_class = EndpointUser
        self.shape_class = StagesShape


@ray.remote(num_cpus=1)
class LocustWorker(LocustClient):
    def __init__(
        self,
        host_url: str,
        token: str,
        master_address: str,
        stages: List[Dict] = DEFAULT_STAGES,
    ):
        from locust.env import Environment

        super().__init__(host_url, token, stages)
        self.env = Environment(user_classes=[self.user_class])
        self.master_address = master_address

    def run(self):
        runner = self.env.create_worker_runner(
            master_host=self.master_address, master_port=5557
        )
        runner.greenlet.join()


@ray.remote(num_cpus=1)
class LocustMaster(LocustClient):
    def __init__(
        self,
        host_url: str,
        token: str,
        expected_num_workers: int,
        stages: List[Dict] = DEFAULT_STAGES,
    ):
        from locust.env import Environment
        from locust.log import setup_logging
        import locust

        super().__init__(host_url, token, stages)
        setup_logging("INFO")

        self.master_env = Environment(
            user_classes=[self.user_class],
            shape_class=self.shape_class(),
            events=locust.events,
        )
        self.expected_num_workers = expected_num_workers

    def run(self):
        import gevent
        from locust.stats import (
            get_stats_summary,
            get_percentile_stats_summary,
            get_error_report_summary,
            stats_history,
            stats_printer,
        )

        master_runner = self.master_env.create_master_runner("*", 5557)

        while len(master_runner.clients.ready) < self.expected_num_workers:
            print(
                f"Waiting for workers to be ready, {len(master_runner.clients.ready)} "
                f"of {self.expected_num_workers} ready."
            )
            time.sleep(1)

        # Stats stuff
        gevent.spawn(stats_printer(self.master_env.stats))
        gevent.spawn(stats_history, master_runner)

        # Start test
        master_runner.start_shape()
        master_runner.shape_greenlet.join()

        # Print stats
        for line in get_stats_summary(master_runner.stats, current=False):
            print(line)
        # Print percentile stats
        for line in get_percentile_stats_summary(master_runner.stats):
            print(line)
        # Print error report
        if master_runner.stats.errors:
            for line in get_error_report_summary(master_runner.stats):
                print(line)

        stats_entry_key = ("", "GET")
        stats_entry = master_runner.stats.entries.get(stats_entry_key)
        return {
            "history": master_runner.stats.history,
            "num_failures": master_runner.stats.num_failures,
            "perf_metrics": [
                {
                    "perf_metric_name": "avg_latency",
                    "perf_metric_value": stats_entry.avg_response_time,
                    "perf_metric_type": "LATENCY",
                },
                {
                    "perf_metric_name": "p50_latency",
                    "perf_metric_value": stats_entry.get_response_time_percentile(0.5),
                    "perf_metric_type": "LATENCY",
                },
                {
                    "perf_metric_name": "p90_latency",
                    "perf_metric_value": stats_entry.get_response_time_percentile(0.9),
                    "perf_metric_type": "LATENCY",
                },
                {
                    "perf_metric_name": "p99_latency",
                    "perf_metric_value": stats_entry.get_response_time_percentile(0.99),
                    "perf_metric_type": "LATENCY",
                },
                {
                    "perf_metric_name": "avg_rps",
                    "perf_metric_value": stats_entry.total_rps,
                    "perf_metric_type": "THROUGHPUT",
                },
            ],
        }


# For testing purposes. This can be run locally with `python locust_load.py`.
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("host", type=str, help="Host service URL.")
    parser.add_argument("--token", type=str, help="Service token.")
    args = parser.parse_args()

    ray.init(address="auto")
    num_locust_workers = int(ray.available_resources()["CPU"]) - 1
    master_address = ray.util.get_node_ip_address()

    # Hold reference to each locust worker to prevent them from being torn down
    print(f"Spawning {num_locust_workers} Locust worker Ray tasks.")
    locust_workers = []
    start_refs = []
    for _ in tqdm(range(num_locust_workers)):
        locust_worker = LocustWorker.remote(args.host, args.token, master_address)
        locust_workers.append(locust_worker)
        start_refs.append(locust_worker.run.remote())

    # Start master locust worker and wait for it to finish
    master_worker = LocustMaster.remote(args.host, args.token, num_locust_workers)
    master_ref = master_worker.run.remote()
    stats = ray.get(master_ref)
    print(f"Final stats: {stats}")
