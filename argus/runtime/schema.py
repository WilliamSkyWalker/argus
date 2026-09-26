"""Versioned JSON workflow format and typed value references."""

import copy
import re

NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
KINDS = {"observe", "action", "tool", "human", "check", "pages"}
VISUAL = {"android", "ios", "browser", "mac", "windows"}


def validate(workflow: dict) -> dict:
    if not isinstance(workflow, dict) or workflow.get("version") != 1:
        raise ValueError("workflow.version must be 1")
    resources, steps = workflow.get("resources"), workflow.get("steps")
    if not isinstance(workflow.get("inputs", {}), dict):
        raise ValueError("inputs must be an object")
    if not isinstance(resources, dict) or not resources:
        raise ValueError("resources must be a nonempty object")
    if not isinstance(steps, list) or not steps:
        raise ValueError("steps must be a nonempty list")
    for name, spec in resources.items():
        if not NAME.fullmatch(name) or not isinstance(spec, dict):
            raise ValueError("invalid resource name or specification")
        kind = spec.get("kind")
        if not isinstance(kind, str) or kind not in VISUAL | {"sqlite"}:
            raise ValueError(f"unsupported resource kind: {kind}")
        if kind == "browser":
            if spec.get("backend", "playwright") not in ("playwright", "selenium", "extension"):
                raise ValueError(f"{name}: backend must be playwright, selenium or extension")
            if "page_id" in spec and (not isinstance(spec["page_id"], str) or not spec["page_id"]):
                raise ValueError(f"{name}: page_id must be a nonempty string")
            if spec.get("backend") == "selenium" and "page_id" in spec:
                raise ValueError(f"{name}: page_id requires Playwright or extension")
        if kind in {"android", "ios", "browser"}:
            if not isinstance(spec.get("session"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", spec["session"]):
                raise ValueError(f"{name}: session must be an explicit safe session name")
        elif kind in {"mac", "windows"}:
            if not isinstance(spec.get("app"), str) or not spec["app"].strip():
                raise ValueError(f"{name}: app is required")
        elif kind == "sqlite":
            if not isinstance(spec.get("path"), str) or not spec["path"]:
                raise ValueError(f"{name}: database path required")
            queries = spec.get("queries")
            if not isinstance(queries, dict) or not queries or not all(
                isinstance(q, str) and q.strip() for q in queries.values()
            ):
                raise ValueError(f"{name}: named queries are required")
    ids = set()
    for i, step in enumerate(steps):
        if not isinstance(step, dict) or not isinstance(step.get("id"), str):
            raise ValueError("every step needs an id")
        sid, kind = step["id"], step.get("kind")
        if not NAME.fullmatch(sid) or sid in ids or not isinstance(kind, str) or kind not in KINDS:
            raise ValueError(f"invalid/duplicate step id or kind: {sid}")
        ids.add(sid)
        if kind != "check":
            resource = step.get("resource")
            if not isinstance(resource, str) or resource not in resources:
                raise ValueError(f"{sid}: unknown resource")
            rkind = resources[resource]["kind"]
            if kind in {"observe", "action", "human"} and rkind not in VISUAL:
                raise ValueError(f"{sid}: requires a visual resource")
            if kind == "pages" and (rkind != "browser" or resources[resource].get("backend") == "selenium"):
                raise ValueError(f"{sid}: pages requires a Playwright or extension browser resource")
            if kind == "tool" and (rkind != "sqlite" or not isinstance(step.get("operation"), str) or step["operation"] not in resources[resource]["queries"]):
                raise ValueError(f"{sid}: unknown database operation")
        if kind == "action" and (not isinstance(step.get("action"), dict) or "observation" not in step):
            raise ValueError(f"{sid}: action and observation are required")
        if kind == "human":
            if not isinstance(step.get("instructions"), str) or not step["instructions"].strip():
                raise ValueError(f"{sid}: human instructions required")
            # Acknowledgement alone cannot certify payment/login success.
            later = steps[i + 1:]
            if not any(s.get("id") == step.get("verify_step") and s.get("kind") == "check"
                       for s in later if isinstance(s, dict)):
                raise ValueError(f"{sid}: verify_step must name a later check step")
        if kind == "check" and not {"actual", "equals"} <= step.keys():
            raise ValueError(f"{sid}: actual and equals required")
    return copy.deepcopy(workflow)


def resolve(value, context):
    """An exact {$ref: 'steps.lookup.rows.0.status'} preserves JSON types."""
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            path = value["$ref"]
            if not isinstance(path, str):
                raise ValueError("$ref must be a dotted path")
            cur = context
            try:
                for part in path.split("."):
                    cur = cur[int(part)] if isinstance(cur, list) else cur[part]
            except (KeyError, IndexError, ValueError, TypeError) as exc:
                raise ValueError(f"unresolved reference: {path}") from exc
            return copy.deepcopy(cur)
        return {k: resolve(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, context) for v in value]
    return value
