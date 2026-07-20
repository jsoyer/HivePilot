"""
Tests for Phase 18 — OpenTelemetry tracing for pipeline & task/step execution.

Covers:
- Import guard: `hivepilot` and `hivepilot.observability.tracing` import fine
  without the OTel SDK; `get_tracer()`/`init_tracing()` no-op safely.
- `get_tracer()` / `record_exception_on_span()` behave correctly whether OTel
  is installed or not.
- Orchestrator instrumentation: with tracing ENABLED (a real TracerProvider +
  InMemorySpanExporter monkeypatched into `hivepilot.orchestrator.get_tracer`),
  running a task through the orchestrator produces a `task.run` -> `step.run`
  span tree with the right attributes, and a failing step records the
  exception + ERROR status.
- With tracing OFF (default): the orchestrator behaves byte-identically
  (same return values / recorded steps) whether or not the tracing wrapper
  code runs.
- No secret ever appears in any span attribute or event.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import hivepilot.orchestrator  # noqa: F401 — side-effect import for patch resolution
from hivepilot.observability import tracing

# ---------------------------------------------------------------------------
# Import guard
# ---------------------------------------------------------------------------


class TestImportGuard:
    def test_hivepilot_imports_without_otel_sdk(self) -> None:
        """`import hivepilot` and `from hivepilot.observability import tracing`
        must never require the OTel SDK — already proven by this test module
        itself importing successfully, but assert explicitly on the
        no-otel-required contract too."""
        import hivepilot  # noqa: F401
        from hivepilot.observability import tracing as _tracing  # noqa: F401

    def test_get_tracer_noop_when_otel_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "opentelemetry", None)
        monkeypatch.setitem(sys.modules, "opentelemetry.trace", None)
        tracer = tracing.get_tracer()
        assert isinstance(tracer, tracing._NoOpTracer)
        with tracer.start_as_current_span("x") as span:
            # Must never raise, regardless of what's called on the span.
            span.set_attribute("a", "b")
            span.set_attributes({"c": "d"})
            span.record_exception(RuntimeError("boom"))
            span.set_status(None)
            assert span.is_recording() is False
            span.end()

    def test_init_tracing_noop_when_otel_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(tracing, "_initialized", False)
        monkeypatch.setitem(sys.modules, "opentelemetry", None)
        from hivepilot.config import Settings

        s = Settings(_env_file=None, enable_tracing=True)  # type: ignore[call-arg]
        tracing.init_tracing(s)  # must not raise
        assert tracing._initialized is False

    def test_record_exception_on_span_noop_when_otel_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "opentelemetry", None)
        monkeypatch.setitem(sys.modules, "opentelemetry.trace", None)
        span = tracing._NoOpSpan()
        tracing.record_exception_on_span(span, ValueError("nope"))  # must not raise

    def test_current_context_and_use_context_noop_when_otel_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "opentelemetry", None)
        monkeypatch.setitem(sys.modules, "opentelemetry.context", None)
        ctx = tracing.current_context()
        assert ctx is None
        with tracing.use_context(ctx):
            pass  # must not raise


# ---------------------------------------------------------------------------
# init_tracing gating (with OTel installed in this environment)
# ---------------------------------------------------------------------------


class TestInitTracingGating:
    def test_disabled_never_touches_otel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """enable_tracing=False must never call `set_tracer_provider` — this
        is the actual off-gate, independent of ambient global OTel state."""
        monkeypatch.setattr(tracing, "_initialized", False)
        from hivepilot.config import Settings

        s = Settings(_env_file=None, enable_tracing=False)  # type: ignore[call-arg]
        with patch("opentelemetry.trace.set_tracer_provider") as mock_set:
            tracing.init_tracing(s)
        mock_set.assert_not_called()
        assert tracing._initialized is False

    def test_enabled_sets_tracer_provider_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(tracing, "_initialized", False)
        from hivepilot.config import Settings

        s = Settings(_env_file=None, enable_tracing=True)  # type: ignore[call-arg]
        with (
            patch("opentelemetry.trace.set_tracer_provider") as mock_set,
            patch(
                "opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter"
            ) as mock_exporter,
        ):
            tracing.init_tracing(s)
            # A second call (the "init once" guard) must not re-init.
            tracing.init_tracing(s)
        mock_set.assert_called_once()
        mock_exporter.assert_called_once()
        assert tracing._initialized is True

    def test_enabled_honors_explicit_endpoint_setting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tracing, "_initialized", False)
        from hivepilot.config import Settings

        s = Settings(  # type: ignore[call-arg]
            _env_file=None,
            enable_tracing=True,
            otel_exporter_otlp_endpoint="http://collector:4317",
        )
        with (
            patch("opentelemetry.trace.set_tracer_provider"),
            patch(
                "opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter"
            ) as mock_exporter,
        ):
            tracing.init_tracing(s)
        mock_exporter.assert_called_once_with(endpoint="http://collector:4317")

    def test_enabled_without_explicit_endpoint_lets_sdk_read_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unset HIVEPILOT_OTEL_EXPORTER_OTLP_ENDPOINT -> OTLPSpanExporter()
        is called with no args, so the SDK falls back to reading the
        standard OTEL_EXPORTER_OTLP_ENDPOINT env var itself."""
        monkeypatch.setattr(tracing, "_initialized", False)
        from hivepilot.config import Settings

        s = Settings(_env_file=None, enable_tracing=True)  # type: ignore[call-arg]
        assert s.otel_exporter_otlp_endpoint is None
        with (
            patch("opentelemetry.trace.set_tracer_provider"),
            patch(
                "opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter"
            ) as mock_exporter,
        ):
            tracing.init_tracing(s)
        mock_exporter.assert_called_once_with()

    def test_sdk_wiring_error_does_not_propagate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A synchronous construction error in the SDK-wiring block (bad
        endpoint, exporter transport failure, ...) must never crash the
        calling entry point — `init_tracing` swallows it, logs the
        exception TYPE only, and leaves tracing disabled/no-op (does NOT
        set `_initialized`, so a later call could still succeed)."""
        monkeypatch.setattr(tracing, "_initialized", False)
        from hivepilot.config import Settings

        s = Settings(_env_file=None, enable_tracing=True)  # type: ignore[call-arg]
        with patch(
            "opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter",
            side_effect=ValueError("malformed endpoint"),
        ):
            tracing.init_tracing(s)  # must not raise
        assert tracing._initialized is False
        # Tracing stays no-op after the failed init.
        tracer = tracing.get_tracer()
        with tracer.start_as_current_span("x") as span:
            assert span.is_recording() is False

    def test_sdk_wiring_error_is_logged_without_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the exception TYPE name is logged — never the exception
        message/args, which could echo a secret-bearing endpoint URL."""
        monkeypatch.setattr(tracing, "_initialized", False)
        from hivepilot.config import Settings

        s = Settings(_env_file=None, enable_tracing=True)  # type: ignore[call-arg]
        with (
            patch(
                "opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter",
                side_effect=ValueError("secret-token=abc123"),
            ),
            patch.object(tracing, "logger") as mock_logger,
        ):
            tracing.init_tracing(s)
        mock_logger.warning.assert_called_once_with("tracing.init_failed", error="ValueError")
        for call in mock_logger.warning.call_args_list:
            for arg in list(call.args) + list(call.kwargs.values()):
                assert "secret-token" not in str(arg)


# ---------------------------------------------------------------------------
# record_exception_on_span — with the real OTel SDK
# ---------------------------------------------------------------------------


class TestRecordExceptionOnSpanReal:
    def test_records_exception_and_error_status(self) -> None:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from opentelemetry.trace import StatusCode

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")

        with tracer.start_as_current_span(
            "boom.span", record_exception=False, set_status_on_exception=False
        ) as span:
            tracing.record_exception_on_span(span, RuntimeError("kaboom"))

        finished = exporter.get_finished_spans()
        assert len(finished) == 1
        (recorded,) = finished
        assert recorded.status.status_code == StatusCode.ERROR
        assert len(recorded.events) == 1
        assert recorded.events[0].name == "exception"
        assert recorded.events[0].attributes["exception.message"] == "kaboom"


# ---------------------------------------------------------------------------
# Orchestrator instrumentation — span tree via a monkeypatched get_tracer
# ---------------------------------------------------------------------------


def _make_orch_with_task(task):
    from hivepilot.models import PipelineConfig, PipelinesFile
    from hivepilot.orchestrator import Orchestrator

    pipelines_file = PipelinesFile(pipelines={"p": PipelineConfig(description="d", stages=[])})
    with (
        patch("hivepilot.orchestrator.load_projects", return_value=MagicMock(projects={})),
        patch("hivepilot.orchestrator.load_tasks", return_value=MagicMock(tasks={}, runners={})),
        patch("hivepilot.orchestrator.load_pipelines", return_value=pipelines_file),
        patch("hivepilot.orchestrator.RunnerRegistry", return_value=MagicMock()),
        patch("hivepilot.orchestrator.PluginManager", return_value=MagicMock()),
    ):
        orch = Orchestrator()
    return orch


@pytest.fixture()
def in_memory_tracer():
    """A real OTel tracer bound to a fresh, isolated TracerProvider +
    InMemorySpanExporter — never touches the process-global tracer
    provider, so tests stay fully isolated from each other."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("hivepilot-test")
    return tracer, exporter


class TestOrchestratorSpanTree:
    def test_step_and_task_spans_created_on_success(self, in_memory_tracer) -> None:
        from hivepilot.models import ProjectConfig, TaskConfig, TaskStep

        tracer, exporter = in_memory_tracer
        orch = _make_orch_with_task(None)
        orch.registry = MagicMock()
        orch.registry.get_runner.return_value = MagicMock(capture=lambda payload: "agent output")
        orch.registry._definition_for.return_value = MagicMock(
            kind="claude", options={}, model=None
        )
        task = TaskConfig(
            description="t", engine="native", steps=[TaskStep(name="s1", runner="claude")]
        )
        project = ProjectConfig(path=Path("/tmp/tracing-proj"))

        with (
            patch("hivepilot.orchestrator.get_tracer", return_value=tracer),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch.object(orch, "_resolve_secrets", return_value={}),
        ):
            # Root span mimics run_pipeline's `pipeline.run` — proves task.run
            # nests under a span opened before `_execute_task` is called.
            with tracer.start_as_current_span("pipeline.run"):
                result = orch._execute_task(
                    project=project,
                    task_name="x",
                    task=task,
                    extra_prompt=None,
                    auto_git=False,
                    run_id=1,
                )

        assert result == "agent output"
        spans = {s.name: s for s in exporter.get_finished_spans()}
        assert set(spans) == {"pipeline.run", "task.run", "step.run"}

        pipeline_span = spans["pipeline.run"]
        task_span = spans["task.run"]
        step_span = spans["step.run"]

        # Parent/child nesting: step -> task -> pipeline.
        assert step_span.parent.span_id == task_span.context.span_id
        assert task_span.parent.span_id == pipeline_span.context.span_id

        assert task_span.attributes["hivepilot.task.name"] == "x"
        assert task_span.attributes["hivepilot.task.project"] == "tracing-proj"
        assert task_span.attributes["hivepilot.task.status"] == "success"

        assert step_span.attributes["hivepilot.step.name"] == "s1"
        assert step_span.attributes["hivepilot.step.runner_kind"] == "claude"
        assert step_span.attributes["hivepilot.step.status"] == "success"
        assert len(step_span.events) == 0  # no exception recorded on success

    def test_failing_step_records_exception_and_error_status(self, in_memory_tracer) -> None:
        from opentelemetry.trace import StatusCode

        from hivepilot.models import ProjectConfig, TaskConfig, TaskStep

        tracer, exporter = in_memory_tracer
        orch = _make_orch_with_task(None)
        orch.registry = MagicMock()

        def _boom(payload):
            raise RuntimeError("step blew up")

        orch.registry.get_runner.return_value = MagicMock(capture=_boom)
        orch.registry._definition_for.return_value = MagicMock(
            kind="claude", options={}, model=None
        )
        task = TaskConfig(
            description="t", engine="native", steps=[TaskStep(name="s1", runner="claude")]
        )
        project = ProjectConfig(path=Path("/tmp/tracing-proj-fail"))

        with (
            patch("hivepilot.orchestrator.get_tracer", return_value=tracer),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch.object(orch, "_resolve_secrets", return_value={}),
            pytest.raises(RuntimeError, match="step blew up"),
        ):
            orch._execute_task(
                project=project,
                task_name="x",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=1,
            )

        spans = {s.name: s for s in exporter.get_finished_spans()}
        assert set(spans) == {"task.run", "step.run"}

        step_span = spans["step.run"]
        task_span = spans["task.run"]

        assert step_span.status.status_code == StatusCode.ERROR
        assert step_span.attributes["hivepilot.step.status"] == "failed"
        assert len(step_span.events) == 1
        assert step_span.events[0].name == "exception"
        assert "step blew up" in step_span.events[0].attributes["exception.message"]

        # The task-level span also records the (re-raised) failure.
        assert task_span.status.status_code == StatusCode.ERROR
        assert task_span.attributes["hivepilot.task.status"] == "failed"

    def test_quota_deferred_step_not_recorded_as_error(self, in_memory_tracer) -> None:
        """`QuotaDeferredError` is an interrupt (like `StepApprovalPending`),
        not a failure: the step/task spans must be marked "deferred" — NOT
        ERROR — and no exception event should be recorded, so a quota
        deferral never pollutes error dashboards."""
        from opentelemetry.trace import StatusCode

        from hivepilot.models import ProjectConfig, TaskConfig, TaskStep
        from hivepilot.services.quota import QuotaDeferredError

        tracer, exporter = in_memory_tracer
        orch = _make_orch_with_task(None)
        orch.registry = MagicMock()

        def _deferred(payload):
            raise QuotaDeferredError("quota exceeded", reset_at=None)

        orch.registry.get_runner.return_value = MagicMock(capture=_deferred)
        orch.registry._definition_for.return_value = MagicMock(
            kind="claude", options={}, model=None
        )
        task = TaskConfig(
            description="t", engine="native", steps=[TaskStep(name="s1", runner="claude")]
        )
        project = ProjectConfig(path=Path("/tmp/tracing-proj-deferred"))

        with (
            patch("hivepilot.orchestrator.get_tracer", return_value=tracer),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch.object(orch, "_resolve_secrets", return_value={}),
            pytest.raises(QuotaDeferredError),
        ):
            orch._execute_task(
                project=project,
                task_name="x",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=1,
            )

        spans = {s.name: s for s in exporter.get_finished_spans()}
        assert set(spans) == {"task.run", "step.run"}

        step_span = spans["step.run"]
        task_span = spans["task.run"]

        # NOT an error span — the whole point of the fix.
        assert step_span.status.status_code != StatusCode.ERROR
        assert step_span.attributes["hivepilot.step.status"] == "deferred"
        assert len(step_span.events) == 0

        assert task_span.status.status_code != StatusCode.ERROR
        assert task_span.attributes["hivepilot.task.status"] == "deferred"
        assert len(task_span.events) == 0

    def test_no_secret_in_any_span_attribute_or_event(self, in_memory_tracer) -> None:
        """A registered secret resolved for the step must never appear in
        any span attribute or exception event — spans only ever carry step
        name / runner kind / status, never step output or secret values."""
        from hivepilot.models import ProjectConfig, TaskConfig, TaskStep

        tracer, exporter = in_memory_tracer
        secret_value = "sk-super-secret-marker-42"

        orch = _make_orch_with_task(None)
        orch.registry = MagicMock()

        def _echo_secret(payload):
            # Simulate an agent whose output/error echoes the resolved secret.
            raise RuntimeError(f"agent echoed: {secret_value}")

        orch.registry.get_runner.return_value = MagicMock(capture=_echo_secret)
        orch.registry._definition_for.return_value = MagicMock(
            kind="claude", options={}, model=None
        )
        task = TaskConfig(
            description="t",
            engine="native",
            steps=[TaskStep(name="s1", runner="claude", secrets={"TOKEN": {"source": "literal"}})],
        )
        project = ProjectConfig(path=Path("/tmp/tracing-proj-secret"))

        # Mirrors what the REAL `_resolve_secrets` does (register every
        # resolved value for masking) — this test patches `_resolve_secrets`
        # itself, so it must register the value manually to reproduce that
        # side effect (see `TestRunResultDetailRedaction` in
        # test_orchestrator.py for the same pattern).
        from hivepilot.services import config_provenance

        config_provenance.clear_secret_values()
        config_provenance.register_secret_value(secret_value)
        try:
            with (
                patch("hivepilot.orchestrator.get_tracer", return_value=tracer),
                patch("hivepilot.orchestrator.state_service.record_step"),
                patch.object(orch, "_resolve_secrets", return_value={"TOKEN": secret_value}),
                pytest.raises(RuntimeError),
            ):
                orch._execute_task(
                    project=project,
                    task_name="x",
                    task=task,
                    extra_prompt=None,
                    auto_git=False,
                    run_id=1,
                )
        finally:
            config_provenance.clear_secret_values()

        for span in exporter.get_finished_spans():
            for value in span.attributes.values():
                assert secret_value not in str(value)
            for event in span.events:
                for value in event.attributes.values():
                    assert secret_value not in str(value)


# ---------------------------------------------------------------------------
# Tracing OFF (default) — byte-identical orchestrator behaviour
# ---------------------------------------------------------------------------


class TestTracingOffByteIdentical:
    def test_execute_task_unchanged_when_tracing_off(self) -> None:
        """With tracing off (the real, un-patched `get_tracer()` — no OTel
        provider configured), `_execute_task`'s wrapper must not change the
        return value, exceptions, or the calls the body makes."""
        from hivepilot.models import ProjectConfig, TaskConfig, TaskStep

        orch = _make_orch_with_task(None)
        orch.registry = MagicMock()
        orch.registry.get_runner.return_value = MagicMock(capture=lambda payload: "plain output")
        orch.registry._definition_for.return_value = MagicMock(
            kind="claude", options={}, model=None
        )
        task = TaskConfig(
            description="t", engine="native", steps=[TaskStep(name="s1", runner="claude")]
        )
        project = ProjectConfig(path=Path("/tmp/tracing-off-proj"))

        with (
            patch("hivepilot.orchestrator.state_service.record_step") as mock_record_step,
            patch.object(orch, "_resolve_secrets", return_value={}),
        ):
            wrapped_result = orch._execute_task(
                project=project,
                task_name="x",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=1,
            )
            wrapped_calls = list(mock_record_step.call_args_list)

        with (
            patch("hivepilot.orchestrator.state_service.record_step") as mock_record_step_2,
            patch.object(orch, "_resolve_secrets", return_value={}),
        ):
            body_result = orch._execute_task_body(
                project=project,
                task_name="x",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=1,
            )
            body_calls = list(mock_record_step_2.call_args_list)

        assert wrapped_result == body_result == "plain output"
        assert wrapped_calls == body_calls

    def test_execute_task_failure_unchanged_when_tracing_off(self) -> None:
        """With tracing off (the real, un-patched `get_tracer()` — no OTel
        provider configured), a FAILING step/task must propagate the SAME
        exception (type + message) through the wrapped `_execute_task` as
        the unwrapped `_execute_task_body` — proving the span wrapper's
        `except Exception: record_exception_on_span(...); raise` doesn't
        alter the failure path when tracing is off. The success-path
        variant is `test_execute_task_unchanged_when_tracing_off` above;
        this covers the failure path it doesn't."""
        from hivepilot.models import ProjectConfig, TaskConfig, TaskStep

        def _boom(payload):
            raise RuntimeError("step blew up off-path")

        orch = _make_orch_with_task(None)
        orch.registry = MagicMock()
        orch.registry.get_runner.return_value = MagicMock(capture=_boom)
        orch.registry._definition_for.return_value = MagicMock(
            kind="claude", options={}, model=None
        )
        task = TaskConfig(
            description="t", engine="native", steps=[TaskStep(name="s1", runner="claude")]
        )
        project = ProjectConfig(path=Path("/tmp/tracing-off-proj-fail"))

        with (
            patch("hivepilot.orchestrator.state_service.record_step") as mock_record_step,
            patch.object(orch, "_resolve_secrets", return_value={}),
            pytest.raises(RuntimeError, match="step blew up off-path") as wrapped_exc_info,
        ):
            orch._execute_task(
                project=project,
                task_name="x",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=1,
            )
        wrapped_calls = list(mock_record_step.call_args_list)

        with (
            patch("hivepilot.orchestrator.state_service.record_step") as mock_record_step_2,
            patch.object(orch, "_resolve_secrets", return_value={}),
            pytest.raises(RuntimeError, match="step blew up off-path") as body_exc_info,
        ):
            orch._execute_task_body(
                project=project,
                task_name="x",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=1,
            )
        body_calls = list(mock_record_step_2.call_args_list)

        assert type(wrapped_exc_info.value) is type(body_exc_info.value)
        assert str(wrapped_exc_info.value) == str(body_exc_info.value)
        assert wrapped_calls == body_calls

    def test_get_tracer_never_raises_and_is_usable(self) -> None:
        """Sanity: the real (un-patched) get_tracer() — whatever OTel state
        the process happens to be in — is always safe to use as a context
        manager and never raises."""
        tracer = tracing.get_tracer()
        with tracer.start_as_current_span("noop-check") as span:
            span.set_attribute("k", "v")


# ---------------------------------------------------------------------------
# Cross-thread context propagation — `run_task`'s `_run_task_body` dispatches
# `_execute_task` via a `ThreadPoolExecutor`; contextvars are NOT
# automatically inherited by worker threads, so `task.run` must be explicitly
# re-parented via `current_context()`/`use_context()`.
# ---------------------------------------------------------------------------


class TestCrossThreadContextPropagation:
    def test_task_span_nests_under_caller_span_across_threadpool(
        self, in_memory_tracer, tmp_path
    ) -> None:
        from hivepilot.models import ProjectConfig, TaskConfig, TaskStep
        from hivepilot.services.policy_service import Policy

        tracer, exporter = in_memory_tracer
        orch = _make_orch_with_task(None)
        orch.projects.projects["proj"] = ProjectConfig(path=tmp_path)
        orch.tasks.tasks["x"] = TaskConfig(
            description="t",
            engine="native",
            steps=[TaskStep(name="s1", runner="claude")],
            artifacts={"capture": []},
        )
        orch.registry = MagicMock()
        orch.registry.get_runner.return_value = MagicMock(capture=lambda payload: "ok")
        orch.registry._definition_for.return_value = MagicMock(
            kind="claude", options={}, model=None
        )

        with (
            patch("hivepilot.orchestrator.get_tracer", return_value=tracer),
            patch("hivepilot.orchestrator.policy_service.enforce_policy", return_value=Policy()),
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=1),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.notification_service.send_notification"),
            patch("hivepilot.orchestrator.knowledge_service.append_feedback"),
            patch("hivepilot.orchestrator.create_run_directory", return_value=tmp_path),
            patch.object(orch, "_resolve_secrets", return_value={}),
        ):
            # `caller.span` mimics `run_pipeline`'s `pipeline.run` span —
            # opened on THIS (the calling) thread, before `run_task` submits
            # `_execute_task` to its internal ThreadPoolExecutor worker.
            with tracer.start_as_current_span("caller.span"):
                orch.run_task(
                    project_names=["proj"],
                    task_name="x",
                    extra_prompt=None,
                    auto_git=False,
                )

        spans = {s.name: s for s in exporter.get_finished_spans()}
        assert {"caller.span", "task.run", "step.run"} <= set(spans)

        caller_span = spans["caller.span"]
        task_span = spans["task.run"]
        step_span = spans["step.run"]

        # task.run (opened in the ThreadPoolExecutor worker thread) must
        # still be a child of caller.span (opened on the submitting thread)
        # — proves `current_context`/`use_context` propagation works.
        assert task_span.parent.span_id == caller_span.context.span_id
        assert step_span.parent.span_id == task_span.context.span_id


# ---------------------------------------------------------------------------
# `init_tracing` wiring at each "a run begins" entry point
# ---------------------------------------------------------------------------


class TestInitTracingWiring:
    def test_cli_run_calls_init_tracing(self) -> None:
        """The single-task `hivepilot run <project> <task>` command must
        also wire up tracing — mirrors `run-pipeline`'s wiring so
        `HIVEPILOT_ENABLE_TRACING=true hivepilot run ...` actually exports
        spans instead of silently staying a no-op."""
        from hivepilot import cli

        with (
            patch("hivepilot.observability.tracing.init_tracing") as mock_init,
            patch("hivepilot.cli._require_cli_role"),
            patch("hivepilot.cli.Orchestrator") as mock_orch_cls,
            patch("hivepilot.cli._resolve_projects", return_value=["proj"]),
        ):
            mock_orch_cls.return_value.run_task.return_value = []
            cli.run(
                project="proj",
                task="t",
                extra_prompt=None,
                auto_git=False,
                all_projects=False,
                projects=[],
                concurrency=None,
                simulate=False,
                token=None,
            )
        mock_init.assert_called_once()

    def test_cli_run_pipeline_calls_init_tracing(self) -> None:
        from hivepilot import cli

        with (
            patch("hivepilot.observability.tracing.init_tracing") as mock_init,
            patch("hivepilot.cli._require_cli_role"),
            patch("hivepilot.cli.Orchestrator") as mock_orch_cls,
            patch("hivepilot.cli.load_groups", return_value=MagicMock(groups={})),
            patch("hivepilot.cli._resolve_projects", return_value=["proj"]),
        ):
            mock_orch_cls.return_value.run_pipeline.return_value = []
            cli.run_pipeline(
                project="proj",
                pipeline="p",
                extra_prompt=None,
                auto_git=False,
                all_projects=False,
                projects=[],
                concurrency=None,
                dry_run=True,
                simulate=False,
                token=None,
            )
        mock_init.assert_called_once()

    def test_api_service_startup_calls_init_tracing(self) -> None:
        from hivepilot.services import api_service

        with patch("hivepilot.observability.tracing.init_tracing") as mock_init:
            import asyncio

            asyncio.run(api_service._init_tracing())
        mock_init.assert_called_once()

    def test_scheduler_daemon_run_calls_init_tracing(self) -> None:
        from hivepilot.services.scheduler_daemon import SchedulerDaemon

        daemon = SchedulerDaemon(check_interval=0)
        daemon._stop_event.set()  # loop body never executes — run() returns immediately
        with patch("hivepilot.observability.tracing.init_tracing") as mock_init:
            daemon.run()
        mock_init.assert_called_once()
