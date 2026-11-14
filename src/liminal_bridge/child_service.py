"""Compatibility exports for the original managed delegated-grant routes."""

from src.core.collaboration.delegated_grants import (
    DelegatedGrantNotAllocated as ChildNotAllocated,
)
from .delegated_grant_service import (
    install_delegated_grant_routes as install_child_routes,
)

__all__ = ["ChildNotAllocated", "install_child_routes"]
