"""Legacy imports retain the canonical delegated-grant objects."""

import pytest

from src.core.collaboration import child_grants, delegated_grants
from src.core.collaboration.managed_runtime import ManagedRuntimeAuthority
from src.liminal_bridge import child_service, delegated_grant_service


@pytest.mark.parametrize(
    ("legacy", "canonical"),
    [
        ("CAPTURES", "CAPTURES"),
        ("CHILD_POLICY_FIELDS", "DELEGATED_GRANT_POLICY_FIELDS"),
        ("ChildGrantAuthority", "DelegatedGrantAuthority"),
        ("ChildNotAllocated", "DelegatedGrantNotAllocated"),
        ("child_policy", "delegated_grant_policy"),
    ],
)
def test_legacy_grant_exports_are_the_same_objects(legacy, canonical):
    assert getattr(child_grants, legacy) is getattr(delegated_grants, canonical)


def test_legacy_routes_and_exception_use_the_canonical_implementation():
    assert (
        child_service.install_child_routes
        is delegated_grant_service.install_delegated_grant_routes
    )
    assert (
        child_service.ChildNotAllocated is delegated_grants.DelegatedGrantNotAllocated
    )
    assert issubclass(ManagedRuntimeAuthority, delegated_grants.DelegatedGrantAuthority)
    with pytest.raises(child_grants.ChildNotAllocated):
        raise delegated_grants.DelegatedGrantNotAllocated("grant refused")
