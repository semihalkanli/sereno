"""Example metrics and a seeded intervention strategy.

For use, copy this module into a package on PYTHONPATH and pass --plugin your_package.extensions.
"""

import json


class MemoryFileCount:
    def compute(self, artifact_dir, spec):
        path = artifact_dir / "memory_end.json"
        if not path.exists():
            return {"value": None, "status": "missing", "evidence": []}
        return {"value": len(json.loads(path.read_text())), "status": "measured", "evidence": ["memory_end.json"]}


class SampledOnce:
    """Stateless strategy with a seeded, per-arm RNG owned by the intervention engine."""

    def eligible(self, event, total_fires, session_fires, context):
        return total_fires == 0 and context["rng"].random() < 0.5


def register(registry):
    registry.register_metric("memory_file_count", MemoryFileCount())
    registry.register_strategy("sampled-once", SampledOnce())
