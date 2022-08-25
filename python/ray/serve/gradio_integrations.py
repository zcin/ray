from ray import serve
from ray.serve._private.http_util import ASGIHTTPSender
from ray.util.annotations import PublicAPI
from ray.serve.drivers import DAGDriver

import starlette
from typing import Callable

try:
    import gradio as gr
except ModuleNotFoundError:
    print("Gradio isn't installed. Run `pip install gradio` to install Gradio.")
    raise


@PublicAPI(stability="alpha")
class GradioIngress:
    """User-facing class that wraps a Gradio App in a Serve Deployment"""

    def __init__(self, io: gr.Blocks):
        self.app = gr.routes.App.create_app(io)

    async def __call__(self, request: starlette.requests.Request):
        sender = ASGIHTTPSender()
        await self.app(request.scope, receive=request.receive, send=sender)
        return sender.build_asgi_response()


GradioServer = serve.deployment(GradioIngress)


@PublicAPI(stability="alpha")
class RayManager:
    """Context Manager that allows users to independently scale out Gradio interfaces.

    Example usage:
        with RayManager() as rm:
            demo = gr.Parallel(
                gr.Interface(rm.rayify(fn1), "text", "text"),
                gr.Interface(rm.rayify(fn2), "text", "text"),
            )

            rm.launch_ray_backend()
            demo.launch()
    """

    def __init__(self):
        self.route_to_node = {}

    def __enter__(self):
        return self

    def rayify(self, fn: Callable, name: str = None, num_replicas: int = 1) -> Callable:
        """Scales out a function.

        When launch_ray_backend() is called, deploys the function as a Serve deployment.
        """
        if name is None:
            name = fn.__name__
        route = f"/{name}"

        @serve.deployment(name=name, num_replicas=num_replicas)
        class Model:
            def __init__(self, fn):
                self.fn = fn

            async def __call__(self, *args):
                return self.fn(*args)

        self.route_to_node[route] = Model.bind(fn)

        async def f(*args):
            return await self.handle.predict_with_route.remote(route, *args)

        return f

    def launch_ray_backend(self):
        """Deploys the functions processed through rayify()"""
        app = DAGDriver.bind(self.route_to_node)
        self.handle = serve.run(app)

    def __exit__(self, *args):
        serve.shutdown()
