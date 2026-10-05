"""Operator-selected permissions, snapshotted per turn; never tool arguments."""
from enum import Enum


class AccessMode(str, Enum):
    AUTO = "auto"
    READ_ONLY = "read_only"
    FULL = "full"


# Some legacy READ tools write exports or import documents. Only offer these
# inspection tools in read-only mode; previews may create disposable caches.
READ_ONLY_TOOLS = frozenset({"ir_get", "ir_digest", "ir_list_features", "ir_help", "geo_view", "geo_measure"})
