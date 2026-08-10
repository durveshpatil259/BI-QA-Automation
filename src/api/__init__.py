"""FastAPI application layer.

Thin HTTP surface over the pipeline: routers validate input, call a service or
submit a job, and shape the response. No business logic lives here — the same
:class:`~src.pipeline.runner.PipelineRunner` drives the API, the Streamlit UI and
the test suite.
"""
