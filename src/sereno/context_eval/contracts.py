"""Extension interfaces. Only explicitly loaded local plugins can register code."""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from sereno.context_eval.schema import AgentOutcome, ExperimentConfig, Intervention, MetricSpec, Session

if TYPE_CHECKING:
    from sereno.context_eval.engine import Runtime


class EnvironmentAdapter(Protocol):
    def execute(self, command: str) -> dict[str, Any]: ...
    def read(self, path: str) -> str | None: ...
    def write(self, path: str, text: str | None) -> None: ...
    def snapshot_memory(self, max_files: int, max_bytes: int) -> dict[str, str]: ...
    def collect_patch(self, base_commit: str) -> bytes: ...
    def close(self) -> None: ...


class MemoryAdapter(Protocol):
    def restore(self, env: EnvironmentAdapter, files: dict[str, str]) -> None: ...
    def context(self, files: dict[str, str]) -> str: ...


class AgentAdapter(Protocol):
    def run(
        self, runtime: "Runtime", instruction: str, memory_context: str, config: ExperimentConfig, session: Session
    ) -> AgentOutcome | dict[str, Any]: ...


class Strategy(Protocol):
    def eligible(self, event: Intervention, total_fires: int, session_fires: int, context: dict) -> bool: ...


class Metric(Protocol):
    def compute(self, artifact_dir: Path, spec: MetricSpec) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Submission:
    task_id: str
    image_id: str
    base_commit: str
    patch: Path


class EvaluatorAdapter(Protocol):
    def evaluate(self, submission: Submission, output_dir: Path) -> dict[str, Any]: ...


class Registry:
    def __init__(self):
        self.agents: dict[str, AgentAdapter] = {}
        self.strategies: dict[str, Strategy] = {}
        self.metrics: dict[str, Metric] = {}
        self.plugins: list[dict[str, str | None]] = []

    def register_agent(self, name: str, adapter: AgentAdapter) -> None:
        self._register(self.agents, name, adapter)

    def register_strategy(self, name: str, strategy: Strategy) -> None:
        self._register(self.strategies, name, strategy)

    def register_metric(self, name: str, metric: Metric) -> None:
        self._register(self.metrics, name, metric)

    @staticmethod
    def _register(destination: dict, name: str, implementation: Any) -> None:
        if name in destination:
            raise ValueError(f"duplicate extension: {name}")
        destination[name] = implementation
