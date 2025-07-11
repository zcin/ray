# coding: utf-8
import asyncio
import os
import sys
import threading
import time

import pytest

import ray
from ray._private.client_mode_hook import client_mode_should_convert
from ray._common.test_utils import SignalActor, wait_for_condition
from ray._private.test_utils import (
    kill_actor_and_wait_for_failure,
    wait_for_pid_to_exit,
)


def test_asyncio_actor(ray_start_regular_shared):
    @ray.remote
    class AsyncBatcher:
        def __init__(self):
            self.batch = []
            self.event = asyncio.Event()

        async def add(self, x):
            self.batch.append(x)
            if len(self.batch) >= 3:
                self.event.set()
            else:
                await self.event.wait()
            return sorted(self.batch)

    a = AsyncBatcher.remote()
    x1 = a.add.remote(1)
    x2 = a.add.remote(2)
    x3 = a.add.remote(3)
    r1 = ray.get(x1)
    r2 = ray.get(x2)
    r3 = ray.get(x3)
    assert r1 == [1, 2, 3]
    assert r1 == r2 == r3


def test_asyncio_actor_same_thread(ray_start_regular_shared):
    @ray.remote
    class Actor:
        def sync_thread_id(self):
            return threading.current_thread().ident

        async def async_thread_id(self):
            return threading.current_thread().ident

    a = Actor.remote()
    sync_id, async_id = ray.get([a.sync_thread_id.remote(), a.async_thread_id.remote()])
    assert sync_id == async_id


def test_asyncio_actor_concurrency(ray_start_regular_shared):
    @ray.remote
    class RecordOrder:
        def __init__(self):
            self.history = []

        async def do_work(self):
            self.history.append("STARTED")
            # Force a context switch
            await asyncio.sleep(0)
            self.history.append("ENDED")

        def get_history(self):
            return self.history

    num_calls = 10

    a = RecordOrder.options(max_concurrency=1).remote()
    ray.get([a.do_work.remote() for _ in range(num_calls)])
    history = ray.get(a.get_history.remote())

    # We only care about ordered start-end-start-end sequence because
    # coroutines may be executed out of enqueued order.
    answer = []
    for _ in range(num_calls):
        for status in ["STARTED", "ENDED"]:
            answer.append(status)

    assert history == answer


def test_asyncio_actor_high_concurrency(ray_start_regular_shared):
    # This tests actor can handle concurrency above recursionlimit.

    @ray.remote
    class AsyncConcurrencyBatcher:
        def __init__(self, batch_size):
            self.batch = []
            self.event = asyncio.Event()
            self.batch_size = batch_size

        async def add(self, x):
            self.batch.append(x)
            if len(self.batch) >= self.batch_size:
                self.event.set()
            else:
                await self.event.wait()
            return sorted(self.batch)

    batch_size = sys.getrecursionlimit()
    actor = AsyncConcurrencyBatcher.options(max_concurrency=batch_size * 2).remote(
        batch_size
    )
    result = ray.get([actor.add.remote(i) for i in range(batch_size)])
    assert result[0] == list(range(batch_size))
    assert result[-1] == list(range(batch_size))


@pytest.mark.asyncio
async def test_asyncio_get(ray_start_regular_shared, event_loop):
    loop = event_loop
    asyncio.set_event_loop(loop)
    loop.set_debug(True)

    # Test Async Plasma
    @ray.remote
    def task():
        return 1

    assert await task.remote().as_future() == 1

    @ray.remote
    def task_throws():
        _ = 1 / 0

    with pytest.raises(ray.exceptions.RayTaskError):
        await task_throws.remote().as_future()

    # Test actor calls.
    str_len = 200 * 1024

    @ray.remote
    class Actor:
        def echo(self, i):
            return i

        def big_object(self):
            # 100Kb is the limit for direct call
            return "a" * (str_len)

        def throw_error(self):
            _ = 1 / 0

    actor = Actor.remote()

    actor_call_future = actor.echo.remote(2).as_future()
    assert await actor_call_future == 2

    promoted_to_plasma_future = actor.big_object.remote().as_future()
    assert await promoted_to_plasma_future == "a" * str_len

    with pytest.raises(ray.exceptions.RayTaskError):
        await actor.throw_error.remote().as_future()

    # Wrap in Remote Function to work with Ray client.
    kill_actor_ref = ray.remote(kill_actor_and_wait_for_failure).remote(actor)
    ray.get(kill_actor_ref)

    with pytest.raises(ray.exceptions.RayActorError):
        await actor.echo.remote(1)


def test_asyncio_actor_async_get(ray_start_regular_shared):
    @ray.remote
    def remote_task():
        return 1

    @ray.remote
    class AsyncGetter:
        async def get(self):
            return await remote_task.remote()

        async def plasma_get(self, plasma_object):
            return await plasma_object[0]

    plasma_object = ray.put(2)
    getter = AsyncGetter.remote()
    assert ray.get(getter.get.remote()) == 1
    assert ray.get(getter.plasma_get.remote([plasma_object])) == 2


@pytest.mark.asyncio
async def test_asyncio_double_await(ray_start_regular_shared):
    # This is a regression test for
    # https://github.com/ray-project/ray/issues/8841

    signal = SignalActor.remote()
    waiting = signal.wait.remote()

    future = waiting.as_future()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(future, timeout=0.1)
    assert future.cancelled()

    # We are explicitly waiting multiple times here to test asyncio state
    # override.
    await signal.send.remote()
    await waiting
    await waiting


@pytest.mark.asyncio
async def test_asyncio_exit_actor(ray_start_regular_shared):
    # https://github.com/ray-project/ray/issues/12649
    # The test should just hang without the fix.

    @ray.remote
    class Actor:
        async def exit(self):
            ray.actor.exit_actor()

        async def ping(self):
            return os.getpid()

        async def loop_forever(self):
            while True:
                await asyncio.sleep(5)

    a = Actor.options(max_task_retries=0).remote()
    pid = ray.get(a.ping.remote())
    a.loop_forever.remote()
    # Make sure exit_actor exits immediately, not once all tasks completed.
    with pytest.raises(ray.exceptions.RayError):
        ray.get(a.exit.remote())

    # New calls should just error.
    with pytest.raises(ray.exceptions.RayError):
        ray.get(a.ping.remote())

    # The actor should be dead in the actor table.
    # Using ray task so it works in Ray Client as well.
    @ray.remote
    def check_actor_gone_now():
        def cond():
            return ray._private.state.actors()[a._ray_actor_id.hex()]["State"] != 2

        wait_for_condition(cond)

    ray.get(check_actor_gone_now.remote())

    # Make sure there is no process leak
    wait_for_pid_to_exit(pid)


def test_asyncio_exit_actor_with_concurrency_group(ray_start_regular_shared):
    @ray.remote(concurrency_groups={"async": 2})
    class Actor:
        async def getpid(self):
            return os.getpid()

        async def exit(self):
            ray.actor.exit_actor()

        @ray.method(concurrency_group="async")
        async def loop_forever(self):
            while True:
                await asyncio.sleep(5)

    a = Actor.remote()
    a.loop_forever.remote()
    pid = ray.get(a.getpid.remote())
    with pytest.raises(ray.exceptions.RayActorError):
        ray.get(a.exit.remote())
    wait_for_pid_to_exit(pid)


def test_async_callback(ray_start_regular_shared):
    global_set = set()

    ref = ray.put(None)
    ref._on_completed(lambda _: global_set.add("completed-1"))
    wait_for_condition(lambda: "completed-1" in global_set)

    signal = SignalActor.remote()

    @ray.remote
    def wait():
        ray.get(signal.wait.remote())

    ref = wait.remote()
    ref._on_completed(lambda _: global_set.add("completed-2"))
    assert "completed-2" not in global_set
    signal.send.remote()
    wait_for_condition(lambda: "completed-2" in global_set)


@pytest.mark.parametrize("raise_in_callback", [False, True])
@pytest.mark.skipif(
    client_mode_should_convert(), reason="Different ref counting in Ray client."
)
def test_on_completed_callback_refcount(ray_start_regular_shared, raise_in_callback):
    """Check that the _on_completed callback is ref counted properly."""
    signal = SignalActor.remote()

    def callback(result):
        if raise_in_callback:
            raise Exception("ruh-roh")

    @ray.remote
    def wait():
        ray.get(signal.wait.remote())

    ref = wait.remote()

    initial_refcount = sys.getrefcount(callback)
    ref._on_completed(callback)

    # Python ref count should be incremented to avoid the callback being GC'd while the
    # C++ core worker holds a ref to it.
    assert sys.getrefcount(callback) > initial_refcount

    # Trigger the task to finish so the callback should execute.
    ray.get(signal.send.remote())

    # Now the refcount should drop back down to the initial count.
    wait_for_condition(lambda: sys.getrefcount(callback) == initial_refcount)


def test_async_function_errored(ray_start_regular_shared):
    with pytest.raises(ValueError):

        @ray.remote
        async def f():
            pass


@pytest.mark.asyncio
async def test_async_obj_unhandled_errors(ray_start_regular_shared):
    @ray.remote
    def f():
        raise ValueError()

    num_exceptions = 0

    def interceptor(e):
        nonlocal num_exceptions
        num_exceptions += 1

    # Test we report unhandled exceptions.
    ray._private.worker._unhandled_error_handler = interceptor
    x1 = f.remote()
    # NOTE: Unhandled exception is from waiting for the value of x1's ObjectID
    # in x1's destructor, and receiving an exception from f() instead.
    del x1
    wait_for_condition(lambda: num_exceptions == 1)

    # Test we don't report handled exceptions.
    x1 = f.remote()
    with pytest.raises(ray.exceptions.RayError):
        await x1
    del x1
    await asyncio.sleep(1)
    assert num_exceptions == 1, num_exceptions


# This case tests that the asyncio actor shouldn't create thread
# pool with max_concurrency threads. Otherwise it will allocate
# too many resources for threads to lead worker crash.
def test_asyncio_actor_with_large_concurrency(ray_start_regular_shared):
    @ray.remote
    class Actor:
        def sync_thread_id(self):
            time.sleep(2)
            return threading.current_thread().ident

        async def async_thread_id(self):
            time.sleep(2)
            return threading.current_thread().ident

    a = Actor.options(max_concurrency=100000).remote()
    sync_id, async_id = ray.get([a.sync_thread_id.remote(), a.async_thread_id.remote()])
    assert sync_id == async_id


def test_asyncio_actor_shutdown_when_non_async_method_mixed(ray_start_regular_shared):
    # It is a regression test.
    # https://github.com/ray-project/ray/issues/32376
    # Make sure the core worker doesn't crash when
    # exit_actor is used when async & regular actor tasks
    # are executed.
    @ray.remote
    class A:
        async def f(self):
            await asyncio.sleep(1)
            ray.actor.exit_actor()

        def ping(self):
            pass

    a = A.remote()
    a.f.remote()

    with pytest.raises(
        ray.exceptions.RayActorError,
        match=("exit_actor"),
    ):
        ray.get([a.ping.remote() for _ in range(10000)])


def test_asyncio_actor_argument_collision(ray_start_regular_shared):
    """Regression test for https://github.com/ray-project/ray/issues/41272."""

    @ray.remote
    class A:
        async def hi_async(self, task_id: str, specified_cgname: str):
            return f"Hi from async: {task_id}! cgname: {specified_cgname}."

        def hi_sync(self, task_id: str, *, specified_cgname: str):
            return f"Hi from sync: {task_id}! cgname: {specified_cgname}."

    a = A.remote()
    assert (
        ray.get(a.hi_async.remote(task_id="TEST", specified_cgname="test2"))
        == "Hi from async: TEST! cgname: test2."
    )
    assert (
        ray.get(a.hi_sync.remote(task_id="TEST", specified_cgname="test2"))
        == "Hi from sync: TEST! cgname: test2."
    )


if __name__ == "__main__":
    sys.exit(pytest.main(["-sv", __file__]))
