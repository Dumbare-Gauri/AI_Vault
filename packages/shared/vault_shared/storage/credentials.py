from typing import Protocol


class CredentialSource(Protocol):
    """How an adapter obtains credentials — the only door.

    An adapter never sees the credentials table, an encryption key or an
    OAuth client: it asks this for a currently valid access token (refreshed
    by the existing `ConnectorTokenService` when needed), for the scopes that
    were granted, and to revoke the grant. Nothing that calls an adapter can
    reach a credential through it, because adapter methods neither accept nor
    return one.
    """

    def access_token(self) -> str: ...

    def granted_scopes(self) -> tuple[str, ...] | None:
        """The scopes recorded for the grant, or None when the connection has
        no stored credentials at all. Scopes are not secret."""
        ...

    def revoke(self) -> None: ...
