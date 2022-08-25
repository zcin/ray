import ray
from ray import serve
from ray.serve.gradio_integrations import GradioServer, RayManager

import gradio as gr

import os
import sys
import pytest
import requests


@pytest.fixture
def serve_start_shutdown():
    ray.init()
    serve.start()
    yield
    serve.shutdown()
    ray.shutdown()


def test_gradio_ingress_correctness(serve_start_shutdown):
    """
    Ensure a Gradio app deployed to a cluster through GradioIngress still
    produces the correct output.
    """

    def greet(name):
        return f"Good morning {name}!"

    io = gr.Interface(fn=greet, inputs="text", outputs="text")
    app = GradioServer.bind(io)
    serve.run(app)

    test_input = "Alice"
    response = requests.post(
        "http://127.0.0.1:8000/api/predict/", json={"data": [test_input]}
    )
    assert response.status_code == 200 and response.json()["data"][0] == greet(
        test_input
    )


def test_gradio_ingress_scaling(serve_start_shutdown):
    """
    Check that a Gradio app that has been deployed to a cluster through
    GradioIngress scales as needed, i.e. separate client requests are served by
    different replicas.
    """

    def f(*args):
        return os.getpid()

    io = gr.Interface(fn=f, inputs="text", outputs="text")
    app = GradioServer.options(num_replicas=2).bind(io)
    serve.run(app)

    pids = []
    for _ in range(3):
        response = requests.post(
            "http://127.0.0.1:8000/api/predict/", json={"data": ["input"]}
        )
        assert response.status_code == 200
        pids.append(response.json()["data"][0])

    assert len(set(pids)) == 2


def test_gradio_parallel_correctness(serve_start_shutdown):
    """
    Ensure a Gradio Parallel interface scaled out through RayManager still produces
    correct output.
    """

    def greet(name):
        return f"Good morning {name}!"

    def goodbye(name):
        return f"See you later {name}!"

    with RayManager() as rm:
        io = gr.Parallel(
            gr.Interface(rm.rayify(greet), "text", "text"),
            gr.Interface(rm.rayify(goodbye), "text", "text"),
        )

        rm.launch_ray_backend()
        (_, url, _) = io.launch(prevent_thread_lock=True)

        test_input = "Alice"
        response = requests.post(
            f"{url.strip('/')}/api/predict/", json={"data": [test_input]}
        )
        print(response.json())
        assert response.status_code == 200 and response.json()["data"] == [
            greet(test_input),
            goodbye(test_input),
        ]


def test_gradio_parallel_scaling(serve_start_shutdown):
    """
    Check that a Gradio Parallel app that has been scaled out through RayManager 
    actually scales, i.e. separate client requests are served by different replicas.
    """

    def f(*args):
        return os.getpid()

    with RayManager() as rm:
        io = gr.Parallel(
            gr.Interface(rm.rayify(f, name='f_1', num_replicas=2), "text", "text"),
            gr.Interface(rm.rayify(f, name='f_2', num_replicas=2), "text", "text"),
        )

        rm.launch_ray_backend()
        (_, url, _) = io.launch(prevent_thread_lock=True)

        pids = []
        for _ in range(3):
            response = requests.post(
                f"{url.strip('/')}/api/predict/", json={"data": ["input"]}
            )
            assert response.status_code == 200
            pids.extend(response.json()["data"])

        assert len(set(pids)) == 4


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", "-s", __file__]))
