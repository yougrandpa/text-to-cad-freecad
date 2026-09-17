"""Lifecycle hooks (L layer): dispatcher, policy hooks, approval store."""

from tcad.hooks.approval import ApprovalRecord, ApprovalStore, JsonFileApprovalStore
from tcad.hooks.dispatcher import HookDispatcher
from tcad.hooks.policy import (
    ApprovalLike,
    NetworkGuard,
    PathGuard,
    PrivilegedTripleGate,
)

__all__ = [
    "HookDispatcher",
    "PathGuard",
    "NetworkGuard",
    "PrivilegedTripleGate",
    "ApprovalLike",
    "ApprovalRecord",
    "ApprovalStore",
    "JsonFileApprovalStore",
]
