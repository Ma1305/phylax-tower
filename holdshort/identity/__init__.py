"""Per-agent Atlas identities generated on demand. See README.md."""

from holdshort.identity.atlas import (
    AtlasAdmin,
    AtlasError,
    Credentials,
    credentials,
    enabled,
    missing,
    mode,
)
from holdshort.identity.core import (
    Identity,
    IdentityManager,
    NotReady,
    generated_identity,
    manager,
    role_name,
    role_payload,
)

__all__ = ["AtlasAdmin", "AtlasError", "Credentials", "Identity", "IdentityManager",
           "NotReady", "credentials", "enabled", "generated_identity", "manager",
           "missing", "mode", "role_name", "role_payload"]
