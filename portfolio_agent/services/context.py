"""
Trusted request context handed to every application-service operation.

A context says WHO is acting (a verified Identity, never a role or id the UI
picked), WHERE the call came from (page, chat, batch job) and carries a
request id so every audit row of one user action can be tied together.
Services check access against the portfolio / account actually being touched.

Single-user deployments resolve everything to LOCAL_OWNER via local_context();
the interface is the same one a multi-user host would construct after
authenticating a session, so adding users does not change any service method.

Identity / verify_identity()
-----------------------------
Identity is the immutable, internal representation of "who is calling,"
producible only by verify_identity() — never by typing a user id into a form,
a URL, a tool call, or an LLM's output and handing it to a service as if it
were authentication. Identity's constructor enforces this: it raises unless
called with the module-private proof object that only verify_identity() can
supply, so `Identity(subject="someone-else", ...)` from anywhere else in the
codebase fails immediately, not just "later" when some repository happens to
check ownership.

Today verify_identity() is a stub: there is no session/credential system yet
(hosted multi-user mode is blocked — see AGENTS notes), so it always resolves
to the single configured LOCAL_OWNER regardless of input. That is the ONLY
thing that will change when real authentication lands; every caller already
goes through this one funnel, so no service method changes when it does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from portfolio_agent.domain import LOCAL_OWNER

SYSTEM_ACTOR = "system"

# Module-private sentinel. Identity.__post_init__ requires this exact object,
# which nothing outside verify_identity() (and the test-only helper below) can
# reach, so importing the Identity class is not enough to construct a valid one.
_VERIFIED = object()


class AuthenticationError(PermissionError):
    """Raised when verify_identity() cannot establish a verified principal."""


@dataclass(frozen=True)
class Identity:
    """
    An authenticated principal. subject is an opaque, stable internal id —
    never re-derived from anything a caller supplies at request time.

    Construct this ONLY via verify_identity() (production) or
    services.context._mint_identity_for_tests() (tests only, for simulating
    a second verified principal to exercise cross-user authorization). Any
    other construction attempt raises.
    """
    subject: str
    _proof: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._proof is not _VERIFIED:
            raise AuthenticationError(
                "Identity may only be constructed by services.context.verify_identity() "
                "— a typed or model-generated actor string is never authentication."
            )


def verify_identity() -> Identity:
    """
    The single funnel every RequestContext identity passes through.

    Single-user deployment, no credential accepted: this always resolves to
    LOCAL_OWNER. There is no parameter to pass a different subject — that is
    deliberate. A real auth check (session cookie, JWT, SSO) replaces this
    function's body when multi-user hosting is enabled; nothing else in the
    codebase needs to change, because RequestContext already only accepts an
    Identity, never a bare string.
    """
    return Identity(subject=LOCAL_OWNER, _proof=_VERIFIED)


def _mint_identity_for_tests(subject: str) -> Identity:
    """
    TEST-ONLY. Simulates a second verified principal so authorization tests
    can prove cross-user isolation without a real multi-user auth system.
    Never call this from application code — nothing in the app imports it,
    and it is not exported by verify_identity()'s production path.
    """
    return Identity(subject=subject, _proof=_VERIFIED)


def _new_request_id() -> str:
    return uuid4().hex


@dataclass(frozen=True)
class RequestContext:
    identity: Identity                # verified principal — see Identity above
    source: str                       # e.g. "web:portfolio", "web:accounts", "chat", "batch:evening", "cli"
    request_id: str = field(default_factory=_new_request_id)

    @property
    def actor(self) -> str:
        """The verified principal's id. Read-only — derived from identity, never settable directly."""
        return self.identity.subject

    def child(self) -> "RequestContext":
        """Same identity and source, fresh request id — for a new user action in the same session."""
        return RequestContext(identity=self.identity, source=self.source)


def require_context(ctx) -> RequestContext:
    """
    Raise a clear, intentional error for a missing/invalid context, instead of
    an incidental AttributeError deep inside whatever query runs next. Every
    authorized read/write in this codebase calls this (directly, or via
    resolve_portfolio which calls it) before touching private data.
    """
    if not isinstance(ctx, RequestContext):
        raise AuthenticationError(
            f"a verified RequestContext is required — got {ctx!r}. "
            "Every private operation needs one; there is no unscoped default."
        )
    return ctx


def local_context(source: str) -> RequestContext:
    """Context for the configured local owner (single-user deployment)."""
    return RequestContext(identity=verify_identity(), source=source)


def system_context(source: str) -> RequestContext:
    """Context for unattended jobs (price refresh, batches). Acts within the local owner's portfolio."""
    return RequestContext(identity=verify_identity(), source=f"{SYSTEM_ACTOR}:{source}")
