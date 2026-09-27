"""API-key authentication and role authorization.

Keys look like ``sv_live_<32 hex>``; only the SHA-256 hash is stored. The raw
secret is shown once at creation time. Roles:

* ``admin``   - the control plane: keys, webhooks, policies, audit, for its own organisation
* ``analyst`` - read/verify jobs and results, manage nothing
* ``service`` - submit/verify media (machine-to-machine)

The role decides *which endpoints* a key may call; the key's ``organisation`` decides *whose data*
it may read. Only a key created with ``platform_scope`` (the bootstrap key, or one explicitly minted
with it by an existing platform admin) crosses organisations, and no role does by itself.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Header, HTTPException, Request, status
from sqlalchemy import select, true

from synthverify.db import ApiKey, Session, UserRole, as_utc, utcnow


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


def _authenticate(request: Request, authorization: str | None, x_api_key: str | None) -> ApiKey:
    supplied = extract_key(request, authorization, x_api_key)
    if not supplied:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key. Provide 'Authorization: Bearer <key>' or 'X-API-Key: <key>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )
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
    ) -> ApiKey:
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


# ------------------------------------------------------------------ tenancy

def is_platform_scoped(api_key: ApiKey) -> bool:
    """Does this credential reach every organisation? A property of the key, not of its role."""
    return bool(api_key.platform_scope)


def visible_to(api_key: ApiKey, organisation: str) -> bool:
    """Can this credential see a row belonging to ``organisation``?

    Deliberately role-blind. ``admin`` used to bypass tenancy on tenant resources, which made
    "an admin key is a platform key" true by accident for every newsroom that was handed one;
    cross-organisation reach is now a flag on the credential (``ApiKey.platform_scope``), so it has
    to be granted explicitly instead of inherited from a job title.
    """
    return is_platform_scoped(api_key) or api_key.organisation == organisation


def org_clause(api_key: ApiKey, column):
    """SQLAlchemy filter restricting a query to what ``api_key`` may read."""
    if is_platform_scoped(api_key):
        return true()
    return column == api_key.organisation


def not_found_or_self(detail: str, api_key: ApiKey, organisation: str) -> None:
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
    ) -> ApiKey:
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
