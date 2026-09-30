"""API-key authentication, role authorization, and fine-grained scopes.

Keys look like ``sv_live_<32 hex>``; only the SHA-256 hash is stored. The raw
secret is shown once at creation time. Roles:

* ``admin``   - the control plane: keys, webhooks, policies, audit, for its own organisation
* ``analyst`` - read/verify jobs and results, manage nothing
* ``service`` - submit/verify media (machine-to-machine)

The role decides *which endpoints* a key may call; the key's ``organisation`` decides *whose data*
it may read. Only a key created with ``platform_scope`` (the bootstrap key, or one explicitly minted
with it by an existing platform admin) crosses organisations, and no role does by itself.

``REQ-IDAM-2`` adds fine-grained scopes on top: a key's ``scopes`` column carries a comma-separated
set of scope tokens that refine or restrict what its role grants. A key with NULL/empty scopes keeps
its role-equivalent grant unchanged (backward compatibility per ``AC-IDAM-2``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Protocol, runtime_checkable

from fastapi import Header, HTTPException, Request, status
from sqlalchemy import select, true

from synthverify.db import ApiKey, Session, UserRole, as_utc, utcnow

if TYPE_CHECKING:  # PyJWT is an optional extra; the annotation is for mypy, the import is lazy.
    from synthverify.oidc import BearerPrincipal


@runtime_checkable
class Principal(Protocol):
    """What every dependency factory and tenancy helper reads off a credential.

    Static API keys load an ``ApiKey`` row; a bearer JWT mints a ``BearerPrincipal`` with the
    same field surface. Typing the seam as a protocol rather than as a union keeps the callers
    (routes, tests, the audit ledger) honest about the shape they can rely on and lets either
    credential travel through ``require_role`` / ``require_scope`` / ``visible_to`` unchanged.
    """

    @property
    def key_id(self) -> str: ...

    @property
    def role(self) -> str: ...

    @property
    def organisation(self) -> str: ...

    @property
    def active(self) -> bool: ...

    @property
    def platform_scope(self) -> bool | None: ...

    @property
    def scopes(self) -> str | None: ...

# ------------------------------------------------------------------ scope vocabulary (REQ-IDAM-2)

#: The canonical set of scope tokens the system recognizes.
SCOPE_VOCABULARY: frozenset[str] = frozenset(
    {
        "media:submit",
        "media:read",
        "jobs:read",
        "jobs:write",
        "artifacts:read",
        "admin:policy:read",
        "admin:policy:write",
        "admin:keys:read",
        "admin:keys:write",
        "admin:webhooks:read",
        "admin:webhooks:write",
        "admin:audit:read",
        "retention:read",
        "retention:write",
    }
)

#: Role-equivalent scope grants: what each role can do when the key has no explicit scopes column.
#: AC-IDAM-2: "legacy keys staying role-equivalent"
ROLE_SCOPES: dict[str, frozenset[str]] = {
    UserRole.ADMIN.value: SCOPE_VOCABULARY,
    UserRole.ANALYST.value: frozenset(
        {"media:submit", "media:read", "jobs:read", "jobs:write", "artifacts:read", "admin:policy:read", "retention:read"}
    ),
    UserRole.SERVICE.value: frozenset(
        {"media:submit", "media:read", "jobs:read", "artifacts:read"}
    ),
}


def parse_scopes(raw: str | None) -> frozenset[str]:
    """Parse a comma-separated scope string into a frozenset. Empty/None → role-equivalent default."""
    if not raw or not raw.strip():
        return frozenset()
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def effective_scopes(api_key: ApiKey | BearerPrincipal) -> frozenset[str]:
    """Return the scopes this key actually carries: explicit scopes, or role-equivalent if none set."""
    explicit = parse_scopes(api_key.scopes)
    if explicit:
        return explicit
    return ROLE_SCOPES.get(api_key.role, frozenset())


def extract_key(request: Request, authorization: str | None, x_api_key: str | None) -> str | None:
    if x_api_key:
        return x_api_key.strip()
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() in ("bearer", "apikey") and token:
            return token.strip()
    return None


def get_session_from_app(request: Request) -> Session:
    return request.app.state.db.session()


def _looks_like_jwt(supplied: str) -> bool:
    """A compact JWS is three base64url segments separated by dots, first segment 'eyJ...'."""
    if not supplied.startswith("eyJ"):
        return False
    return supplied.count(".") == 2


def _authenticate(request: Request, authorization: str | None, x_api_key: str | None):
    supplied = extract_key(request, authorization, x_api_key)
    if not supplied:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key. Provide 'Authorization: Bearer <key>' or 'X-API-Key: <key>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # REQ-IDAM-1: a compact JWS in the bearer slot is verified against the configured JWKS
    # *before* the API-key lookup, so a token never reaches the hash table and a static key
    # never reaches the JWT path. When OIDC is disabled this is inert and the shape falls
    # through to "Invalid or revoked API key", which is what an operator without OIDC
    # configured should see.
    if _looks_like_jwt(supplied):
        from synthverify.config import get_settings
        from synthverify.oidc import OidcError, authenticate_bearer

        settings = get_settings()
        if settings.oidc_enabled:
            try:
                principal = authenticate_bearer(supplied, settings=settings)
            except OidcError as exc:
                _note_auth_event(request, False, "oidc:bearer")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=str(exc),
                    headers={"WWW-Authenticate": "Bearer"},
                ) from exc
            request.state.api_key = principal
            request.state.oidc_claims = principal.claims
            _note_auth_event(request, True, principal.key_id)
            return principal

    from synthverify.db import hash_key

    session: Session = request.app.state.db.session()
    try:
        api_key = session.execute(
            select(ApiKey).where(ApiKey.key_hash == hash_key(supplied))
        ).scalar_one_or_none()
    finally:
        session.close()

    if api_key is None or not api_key.active:
        _note_auth_event(request, False, supplied[:12])
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # throttled last-used touch (avoid a write per request)
    now = utcnow()
    last_used = as_utc(api_key.last_used_at)
    if last_used is None or (now - last_used).total_seconds() > 60:
        session = request.app.state.db.session()
        try:
            row = session.get(ApiKey, api_key.id)
            # `api_key` came from the cache; if the underlying row was deleted between cache-fill
            # and this touch we skip the update rather than resurrect it. The request itself has
            # already been authenticated so no auth decision depends on this.
            if row is not None:
                row.last_used_at = now
                session.commit()
        finally:
            session.close()
    request.state.api_key = api_key
    _note_auth_event(request, True, api_key.key_id)
    return api_key


def _note_auth_event(request: Request, ok: bool, subject: str) -> None:
    from synthverify.metrics import METRICS

    METRICS.inc(
        "synthverify_auth_requests_total",
        {"outcome": "ok" if ok else "rejected"},
    )


def require_role(*roles: UserRole):
    """Dependency factory: authenticate and enforce a role."""

    def dependency(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        x_api_key: Annotated[str | None, Header()] = None,
    ) -> Principal:
        api_key = _authenticate(request, authorization, x_api_key)
        if roles and UserRole(api_key.role) not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{api_key.role}' is not permitted here (requires one of: "
                f"{', '.join(r.value for r in roles)}).",
            )
        return api_key

    return dependency


require_any = require_role()
require_admin = require_role(UserRole.ADMIN)
require_analyst = require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.SERVICE)


# ------------------------------------------------------------------ scope enforcement (REQ-IDAM-2)


def require_scope(*scopes: str):
    """Dependency factory: authenticate and enforce a fine-grained scope.

    AC-IDAM-2: "a credential minted with `jobs:read` + `media:submit` is admitted to ingest and
    job-read routes and rejected **`403`** on `admin:policy:write` and `artifacts:read` naming
    the missing scope". The rejection uses 403 because the credential is valid and known; only
    cross-org reads use 404 per AC-IDAM-3.

    When a key has explicit scopes set, the scope check applies; when scopes are NULL/empty, the
    key keeps its role-equivalent grant (backward compatibility).
    """

    def dependency(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        x_api_key: Annotated[str | None, Header()] = None,
    ) -> Principal:
        api_key = _authenticate(request, authorization, x_api_key)
        granted = effective_scopes(api_key)
        missing = [s for s in scopes if s not in granted]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Scope required: {', '.join(missing)}. "
                    f"Key '{api_key.key_id}' carries: {', '.join(sorted(granted)) or '(none)'}."
                ),
            )
        return api_key

    return dependency


# ------------------------------------------------------------------ tenancy

def is_platform_scoped(api_key: ApiKey | BearerPrincipal) -> bool:
    """Does this credential reach every organisation? A property of the key, not of its role."""
    return bool(api_key.platform_scope)


def visible_to(api_key: ApiKey | BearerPrincipal, organisation: str) -> bool:
    """Can this credential see a row belonging to ``organisation``?

    Deliberately role-blind. ``admin`` used to bypass tenancy on tenant resources, which made
    "an admin key is a platform key" true by accident for every newsroom that was handed one;
    cross-organisation reach is now a flag on the credential (``ApiKey.platform_scope``), so it has
    to be granted explicitly instead of inherited from a job title.
    """
    return is_platform_scoped(api_key) or api_key.organisation == organisation


def org_clause(api_key: ApiKey | BearerPrincipal, column):
    """SQLAlchemy filter restricting a query to what ``api_key`` may read."""
    if is_platform_scoped(api_key):
        return true()
    return column == api_key.organisation


def not_found_or_self(detail: str, api_key: ApiKey | BearerPrincipal, organisation: str) -> None:
    """Raise the *same* 404 a missing row would produce, so tenancy is not an existence oracle."""
    if not visible_to(api_key, organisation):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def require_platform(*roles: UserRole):
    """Dependency factory for the control plane: role *and* platform scope.

    ``/api/v1/admin`` can mint keys into any organisation, point webhooks at any organisation and
    read every audit row, so it is not an org-level surface no matter what its role name suggests.
    Before this, any key with ``role=admin`` had all of that, which meant handing a newsroom an
    admin key handed it every other tenant.
    """

    inner = require_role(*roles)

    def dependency(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        x_api_key: Annotated[str | None, Header()] = None,
    ) -> Principal:
        api_key = inner(request, authorization, x_api_key)
        if not is_platform_scoped(api_key):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "This endpoint is the platform control plane and needs a key with "
                    f"platform_scope; the supplied key '{api_key.key_id}' is scoped to "
                    f"organisation {api_key.organisation!r}."
                ),
            )
        return api_key

    return dependency


require_platform_admin = require_platform(UserRole.ADMIN)
