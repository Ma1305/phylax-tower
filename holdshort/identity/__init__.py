"""Per-agent Atlas identities generated on demand. See README.md."""

from holdshort.identity.atlas import AtlasAdmin, AtlasError, Credentials, credentials, missing
from holdshort.identity.core import (
    Identity,
    IdentityManager,
    generated_identity,
    manager,
    role_name,
    role_payload,
)

__all__ = ["AtlasAdmin", "AtlasError", "Credentials", "Identity", "IdentityManager",
           "credentials", "generated_identity", "manager", "missing", "role_name",
           "role_payload"]
