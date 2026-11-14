"""Compatibility exports for the original managed delegated-grant module.

The maintained implementation lives in :mod:`delegated_grants`. These aliases
preserve the v1 imports and exception identity without duplicating authority.
"""

from .delegated_grants import (
    CAPTURES,
    DELEGATED_GRANT_POLICY_FIELDS as CHILD_POLICY_FIELDS,
    DelegatedGrantAuthority as ChildGrantAuthority,
    DelegatedGrantNotAllocated as ChildNotAllocated,
    delegated_grant_policy as child_policy,
)

__all__ = [
    "CAPTURES",
    "CHILD_POLICY_FIELDS",
    "ChildGrantAuthority",
    "ChildNotAllocated",
    "child_policy",
]
