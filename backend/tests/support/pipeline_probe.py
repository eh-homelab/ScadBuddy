"""A workflow that runs pipeline source the way `TemplatePipeline` does, and reports
what came out: the measurement §3.6 asks for."""

from __future__ import annotations

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from scadbuddy.workflows.sandbox import load_pipeline_module, pipeline_error

FILE = "pipeline/pipeline.py"


@workflow.defn(name="ExecProbe")
class ExecProbe:
    @workflow.run
    async def run(self, source: str) -> str:
        try:
            namespace = load_pipeline_module(source, FILE)
            return f"ok|{await namespace['run'](None, {})}"
        except Exception as error:
            kind = f"{type(error).__module__}.{type(error).__name__}"
            return f"{kind}|{pipeline_error(error, FILE)}"
