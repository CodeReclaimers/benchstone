from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class InvocationConfig:
    """JSON payload passed from the harness to the project entry point.

    Written to the path given by `{config_path}` in the manifest's invocation
    template; read back by the project's benchmark script.
    """

    benchmark: str
    seed: int
    corpus_path: str
    repetition_index: int
    repetition_total: int

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    def write(self, path: str | Path) -> None:
        Path(path).write_text(self.to_json())


@dataclass(frozen=True)
class ProjectResult:
    """JSON payload written by the project entry point to `{output_path}`.

    `status` is "ok" on success; "error" accompanies a non-empty `message`.
    `metric` is the scalar the gate compares; `metric_components` is optional
    structured detail. `metadata` carries free-form project-specific annotations.
    """

    status: str
    metric: float | None = None
    metric_components: dict[str, Any] | None = None
    wall_clock_seconds: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    message: str | None = None

    @classmethod
    def from_json(cls, text: str) -> "ProjectResult":
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ProtocolError("result JSON must be an object at the top level")
        status = data.get("status")
        if status not in ("ok", "error"):
            raise ProtocolError(
                f"result status must be 'ok' or 'error', got {status!r}"
            )
        if status == "ok" and data.get("metric") is None:
            raise ProtocolError("result status='ok' requires a non-null 'metric'")
        if status == "error" and not data.get("message"):
            raise ProtocolError("result status='error' requires a 'message'")
        metric = data.get("metric")
        if metric is not None:
            try:
                metric = float(metric)
            except (TypeError, ValueError) as exc:
                raise ProtocolError(f"metric must be a number, got {metric!r}") from exc
        return cls(
            status=status,
            metric=metric,
            metric_components=data.get("metric_components"),
            wall_clock_seconds=data.get("wall_clock_seconds"),
            metadata=data.get("metadata", {}) or {},
            message=data.get("message"),
        )

    @classmethod
    def read(cls, path: str | Path) -> "ProjectResult":
        return cls.from_json(Path(path).read_text())


class ProtocolError(ValueError):
    """Raised when a project's output violates the result schema."""
