"""Authentication-neutral principals and authorization policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


LOCAL_USER_ID = "local-owner"


class Role(StrEnum):
    ADMIN = "admin"
    USER = "user"


class Permission(StrEnum):
    PROJECT_READ = "project:read"
    PROJECT_WRITE = "project:write"
    PROJECT_DELETE = "project:delete"
    PROVIDER_MANAGE = "provider:manage"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.ADMIN: frozenset(Permission),
    Role.USER: frozenset(
        {
            Permission.PROJECT_READ,
            Permission.PROJECT_WRITE,
            Permission.PROJECT_DELETE,
            Permission.PROVIDER_MANAGE,
        }
    ),
}


class AuthorizationError(PermissionError):
    pass


@dataclass(frozen=True)
class Principal:
    user_id: str
    roles: frozenset[Role]
    email: str = ""
    display_name: str = ""

    @property
    def permissions(self) -> frozenset[Permission]:
        return frozenset(
            permission
            for role in self.roles
            for permission in ROLE_PERMISSIONS.get(role, frozenset())
        )

    def require(self, permission: Permission) -> None:
        if permission not in self.permissions:
            raise AuthorizationError(f"Permission required: {permission}")


def local_owner_principal() -> Principal:
    """Trusted identity used only by the single-workspace local edition."""
    return Principal(
        user_id=LOCAL_USER_ID,
        roles=frozenset({Role.ADMIN}),
        display_name="Local owner",
    )
