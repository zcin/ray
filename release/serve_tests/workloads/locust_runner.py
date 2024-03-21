import os
import ray
import time
import argparse
import subprocess
from tqdm import tqdm

ray.init(address="auto")
num_locust_workers = int(ray.available_resources()["CPU"])
master_address = ray.util.get_node_ip_address()
print("Locust master address:", master_address)


class LocustClient:
    def __init__(self, host_url: str, token: str):
        from locust import HttpUser, events, task, constant, FastHttpUser, LoadTestShape

        class EndpointUser(FastHttpUser):
            wait_time = constant(0)
            failed_requests = []
            host = host_url

            @task
            def test(self):
                resp = self.client.get(
                    "",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "uri": "https://serve-resnet-benchmark-data.s3.us-west-1.amazonaws.com/000000000019.jpeg"
                    },
                )

        class StagesShape(LoadTestShape):
            stages = [
                {"duration": 30, "users": 10, "spawn_rate": 1},
                {"duration": 60, "users": 50, "spawn_rate": 10},
                {"duration": 180, "users": 100, "spawn_rate": 10},
            ]

            def tick(self):
                run_time = self.get_run_time()
                for stage in self.stages:
                    if run_time < stage["duration"]:
                        tick_data = (stage["users"], stage["spawn_rate"])
                        return tick_data
                return None

        self.user_class = EndpointUser
        self.shape_class = StagesShape


@ray.remote(num_cpus=1)
class LocustWorker(LocustClient):
    def __init__(self, host_url: str, token: str):
        from locust.env import Environment

        super().__init__(host_url, token)
        self.env = Environment(user_classes=[self.user_class])

    def run(self):
        print("starting worker runner")
        runner = self.env.create_worker_runner(
            master_host=master_address, master_port=5557
        )
        runner.greenlet.join()


@ray.remote(num_cpus=1)
class LocustMaster(LocustClient):
    def __init__(self, host_url: str, token: str):
        from locust.env import Environment
        import locust

        super().__init__(host_url, token)
        self.master_env = Environment(
            user_classes=[self.user_class],
            shape_class=self.shape_class(),
            events=locust.events,
        )

    def run(self):
        from locust.stats import (
            get_stats_summary,
            get_percentile_stats_summary,
            get_error_report_summary,
        )

        print("starting master worker")
        master_runner = self.master_env.create_master_runner("*", 5557)
        print("created master runner")
        time.sleep(2)
        print(
            "runner clients ready",
            len(master_runner.clients.ready),
            master_runner.clients.ready,
        )
        print("starting shape")
        master_runner.start_shape()
        master_runner.shape_greenlet.join()

        print("Finished running Locust workload.")
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


# Hold reference to each locust worker to prevent them from being torn down
print(f"Spawning {num_locust_workers} Locust worker Ray tasks.")
locust_workers = []
start_refs = []
for _ in tqdm(range(num_locust_workers)):
    locust_worker = LocustWorker.remote(
        "https://testing-autoscaling-load-test-jrvwy.cld-kvedzwag2qa8i5bj.s.anyscaleuserdata-staging.com/",
        "JjSBq5r3GfxIrIHb2STzH0RqAlgBzIuLjKgY1GW4c2M",
    )
    locust_workers.append(locust_worker)
    start_refs.append(locust_worker.run.remote())

# Start master locust worker and wait for it to finish
master_worker = LocustMaster.remote(
    "https://testing-autoscaling-load-test-jrvwy.cld-kvedzwag2qa8i5bj.s.anyscaleuserdata-staging.com/",
    "JjSBq5r3GfxIrIHb2STzH0RqAlgBzIuLjKgY1GW4c2M",
)
ray.get(master_worker.run.remote())
