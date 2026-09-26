# Runtime contract tests

Run from the repository root:

```bash
python3 -m unittest discover -s tests/runtime_demo -v
```

The suite uses temporary directories, real SQLite databases, and fake visual resources.
It covers cross-resource workflows, human handoff and verification, durable recovery,
uncertain actions, resource ownership, stale observations, read-only database enforcement,
and the real CLI entry point. It does not control devices or call an LLM.

Workflow format and examples: [runtime guide](../../docs/runtime.md).
