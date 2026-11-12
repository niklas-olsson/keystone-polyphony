"""Separate advisory/experimental tasks from the canonical managed namespace."""

import json

MANAGED_TASK_REFUSAL = (
    "Managed tasks require the authenticated coordinator service; "
    "legacy task paths cannot claim, execute or accept them."
)


class ManagedTaskRefusal(ValueError):
    """An advisory or experimental operation targeted a managed task."""


def reserved_task_id(task_id):
    return isinstance(task_id, str) and task_id.startswith("task_v2_")


def require_experimental_task(task_id):
    if reserved_task_id(task_id):
        raise ManagedTaskRefusal(MANAGED_TASK_REFUSAL)


def managed_backlog_ids(backlog):
    """Classify every matching advisory record, including contradictory copies.

    This metadata rule applies to swarm_backlog, not FencedLedger's separate
    schema-v2 TaskObjective objects. Only an absent marker or exact integer 1
    denotes an advisory record. Malformed unrelated records grant no authority.
    """
    managed = set()
    if not isinstance(backlog, (list, tuple, set, frozenset)):
        return managed
    for item in backlog:
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except (ValueError, TypeError):
                continue
        if not isinstance(item, dict):
            continue
        task_id = item.get("id")
        if not isinstance(task_id, str):
            continue
        schema = item.get("schema_version", 1)
        if reserved_task_id(task_id) or type(schema) is not int or schema != 1:
            managed.add(task_id)
    return managed


def managed_task_resource(resource, managed_ids=()):
    """Ordinary file resources need no backlog lookup."""
    if not isinstance(resource, str) or not resource.startswith("task:"):
        return False
    task_id = resource[len("task:") :]
    return reserved_task_id(task_id) or task_id in managed_ids


def experimental_host_targets(targets):
    """An empty ordinary target list keeps its existing host-signal meaning."""
    return isinstance(targets, list) and all(
        isinstance(task_id, str) and task_id and not reserved_task_id(task_id)
        for task_id in targets
    )


def stored_experimental_host_targets(raw_targets):
    """Do not let forgiving historical decoding turn malformed rows into work."""
    try:
        return experimental_host_targets(json.loads(raw_targets))
    except (TypeError, ValueError):
        return False
