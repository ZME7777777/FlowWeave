"""Compatibility exports; new code imports from :mod:`flowweave.shared.domain`."""

from flowweave.shared.domain.errors import DomainError, conflict, illegal, not_found

__all__ = ("DomainError", "conflict", "illegal", "not_found")
