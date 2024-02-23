import asyncio
import sys
import time
from typing import Optional
from unittest.mock import patch

import pytest

from ray._private.test_utils import wait_for_condition
from ray._private.utils import get_or_create_event_loop
from ray.serve._private.metrics_utils import InMemoryMetricsStore, MetricsPusher
from ray.serve._private.utils import get_random_string

# from ray.serve._private.test_utils import MockTimer


class MockAsyncTimer:
    def __init__(self, start_time: Optional[float] = 0):
        self.async_sleeps = list()
        self.reset(start_time=start_time)

    def reset(self, start_time: 0):
        self._curr = start_time

    def time(self) -> float:
        return self._curr

    async def sleep(self, amt: float):
        id = get_random_string()
        try:
            self.async_sleeps.append((id, self._curr + amt))

            # Sort by time
            self.async_sleeps.sort(key=lambda x: x[1])

            while True:
                # Realistically move time forward
                self._curr += 0.01
                # Give up the event loop
                await asyncio.sleep(0)
                if self.async_sleeps[0][0] == id:
                    break

            completed_task = self.async_sleeps.pop(0)
            self._curr = completed_task[1]
        except asyncio.CancelledError:
            self.async_sleeps = [item for item in self.async_sleeps if item[0] != id]
            raise


class TestMetricsPusher:
    def test_no_tasks(self):
        """Test that a metrics pusher can be started with zero tasks.

        After a task is registered, it should work.
        """
        val = 0

        def inc():
            nonlocal val
            val += 1

        MetricsPusher.NO_TASKS_REGISTERED_INTERVAL_S = 0.01
        metrics_pusher = MetricsPusher()
        metrics_pusher.start()
        assert len(metrics_pusher._tasks) == 0
        assert metrics_pusher.pusher_thread.is_alive()

        metrics_pusher.register_or_update_task("inc", inc, 0.01)

        wait_for_condition(lambda: val > 0, timeout=10)

    @pytest.mark.asyncio
    async def test_basic(self):
        timer = MockAsyncTimer(0)

        with patch("ray.serve._private.metrics_utils.async_sleep", timer.sleep):
            state = {"val": 0}
            event = asyncio.Event()

            def task(s):
                # At 10 seconds, this task should have been called 20 times
                if timer.time() >= 10 and "res" not in s:
                    s["res"] = s["val"]
                    event.set()

                s["val"] += 1

            metrics_pusher = MetricsPusher(get_or_create_event_loop())
            metrics_pusher.start()

            metrics_pusher.register_or_update_task("basic", lambda: task(state), 0.5)
            await event.wait()
            assert state["res"] == 20

        metrics_pusher.shutdown()

    @pytest.mark.asyncio
    async def test_multiple_tasks(self):
        timer = MockAsyncTimer(0)

        with patch("ray.serve._private.metrics_utils.async_sleep", timer.sleep):
            state = {"A": 0, "B": 0, "C": 0}
            events = {"A": asyncio.Event(), "B": asyncio.Event(), "C": asyncio.Event()}

            def task(key, s):
                # At 7 seconds, tasks A, B, C should have executed 35, 14, and 10
                # times respectively.
                if timer.time() >= 7 and f"res_{key}" not in s:
                    events[key].set()
                    s[f"res_{key}"] = s[key]

                s[key] += 1

            metrics_pusher = MetricsPusher(get_or_create_event_loop())
            metrics_pusher.start()

            # Each task interval is different, and they don't divide each other.
            metrics_pusher.register_or_update_task("A", lambda: task("A", state), 0.2)
            metrics_pusher.register_or_update_task("B", lambda: task("B", state), 0.5)
            metrics_pusher.register_or_update_task("C", lambda: task("C", state), 0.7)

            await asyncio.wait(
                [
                    asyncio.create_task(events["A"].wait()),
                    asyncio.create_task(events["B"].wait()),
                    asyncio.create_task(events["C"].wait()),
                ],
                return_when=asyncio.ALL_COMPLETED,
            )

            assert state["res_A"] == 35  # 7 / 0.2
            assert state["res_B"] == 14  # 7 / 0.5
            assert state["res_C"] == 10  # 7 / 0.7

    @pytest.mark.asyncio
    async def test_update_task(self):
        _start = {"A": 0}
        timer = MockAsyncTimer(_start["A"])

        with patch("ray.serve._private.metrics_utils.async_sleep", timer.sleep):
            state = {"A": 0, "B": 0}
            events = {"A": asyncio.Event(), "B": asyncio.Event()}

            # Task that:
            # - increments state["A"] on each iteration
            # - writes to state["res_A"] when time has reached 10
            def f(s):
                if timer.time() >= 10 and "res_A" not in s:
                    s["res_A"] = s["A"]
                    events["A"].set()

                s["A"] += 1

            # Start metrics pusher and register task() with interval 1s.
            # After (fake) 10s, the task should have executed 10 times
            metrics_pusher = MetricsPusher(get_or_create_event_loop())
            metrics_pusher.start()

            # Give the metrics pusher thread opportunity to execute task
            # The only thing that should be moving the timer forward is
            # the metrics pusher thread. So if the timer has reached 11,
            # the task should have at least executed 10 times.
            metrics_pusher.register_or_update_task("my_task", lambda: f(state), 1)
            # while timer.time() < 11:
            #     time.sleep(0.001)
            # assert result["A"] == 10
            await events["A"].wait()
            assert state["res_A"] == 10

            # New task that:
            # - increments state["B"] on each iteration
            # - writes to state["res_B"] when 450 seconds have passed since first executed
            def new_f(s, start):
                if "B" not in start:
                    start["B"] = timer.time()

                s["B"] += 1
                if timer.time() >= start["B"] + 450 and "res_B" not in s:
                    s["res_B"] = s["B"]
                    events["B"].set()

                time.sleep(0.001)

            _start = {}
            # Re-register new_task() with interval 50s. After (fake)
            # 500s, the task should have executed 10 times.
            metrics_pusher.register_or_update_task(
                "my_task", lambda: new_f(state, _start), 50
            )

            # Wait for the metrics pusher thread to execute the new task
            # at least once, and fetch the time of first execution.
            # while "B" not in _start:
            #     time.sleep(0.001)

            # When the timer has advanced at least 500 past the time of
            # first execution, the new task should have at least
            # executed 10 times.
            # while timer.time() < _start["B"] + 500:
            #     time.sleep(0.001)
            await events["B"].wait()
            assert state["res_B"] == 10


class TestInMemoryMetricsStore:
    def test_basics(self):
        s = InMemoryMetricsStore()
        s.add_metrics_point({"m1": 1}, timestamp=1)
        s.add_metrics_point({"m1": 2}, timestamp=2)
        assert s.window_average("m1", window_start_timestamp_s=0) == 1.5
        assert s.max("m1", window_start_timestamp_s=0) == 2

    def test_out_of_order_insert(self):
        s = InMemoryMetricsStore()
        s.add_metrics_point({"m1": 1}, timestamp=1)
        s.add_metrics_point({"m1": 5}, timestamp=5)
        s.add_metrics_point({"m1": 3}, timestamp=3)
        s.add_metrics_point({"m1": 2}, timestamp=2)
        s.add_metrics_point({"m1": 4}, timestamp=4)
        assert s.window_average("m1", window_start_timestamp_s=0) == 3
        assert s.max("m1", window_start_timestamp_s=0) == 5

    def test_window_start_timestamp(self):
        s = InMemoryMetricsStore()
        assert s.window_average("m1", window_start_timestamp_s=0) is None
        assert s.max("m1", window_start_timestamp_s=0) is None

        s.add_metrics_point({"m1": 1}, timestamp=2)
        assert s.window_average("m1", window_start_timestamp_s=0) == 1
        assert (
            s.window_average("m1", window_start_timestamp_s=10, do_compact=False)
            is None
        )

    def test_compaction_window(self):
        s = InMemoryMetricsStore()

        s.add_metrics_point({"m1": 1}, timestamp=1)
        s.add_metrics_point({"m1": 2}, timestamp=2)

        assert (
            s.window_average("m1", window_start_timestamp_s=0, do_compact=False) == 1.5
        )
        s.window_average("m1", window_start_timestamp_s=1.1, do_compact=True)
        # First record should be removed.
        assert s.window_average("m1", window_start_timestamp_s=0, do_compact=False) == 2

    def test_compaction_max(self):
        s = InMemoryMetricsStore()

        s.add_metrics_point({"m1": 1}, timestamp=2)
        s.add_metrics_point({"m1": 2}, timestamp=1)

        assert s.max("m1", window_start_timestamp_s=0, do_compact=False) == 2

        s.window_average("m1", window_start_timestamp_s=1.1, do_compact=True)

        assert s.window_average("m1", window_start_timestamp_s=0, do_compact=False) == 1

    def test_multiple_metrics(self):
        s = InMemoryMetricsStore()
        s.add_metrics_point({"m1": 1, "m2": -1}, timestamp=1)
        s.add_metrics_point({"m1": 2, "m2": -2}, timestamp=2)
        assert s.window_average("m1", window_start_timestamp_s=0) == 1.5
        assert s.max("m1", window_start_timestamp_s=0) == 2
        assert s.max("m2", window_start_timestamp_s=0) == -1


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", "-s", __file__]))
