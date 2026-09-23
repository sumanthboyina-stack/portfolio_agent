"""
Trusted request context handed to every application-service operation.

A context says WHO is acting (an authenticated principal id, never a role the
UI picked), WHERE the call came from (page, chat, batch job) and carries a
request id so every audit row of one user action can be tied together.
Services check access against the portfolio / account actually being touched.

Single-user deployments resolve everything to LOCAL_OWNER via local_context();
the interface is the same one a multi-user host would construct after
authenticating a session, so adding users does not change any service method.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from portfolio_agent.domain import LOCAL_OWNER

SYSTEM_ACTOR = "system"


def _new_request_id() -> str:
    return uuid4().hex


@dataclass(frozen=True)
class RequestContext:
    actor: str                       # authenticated principal id (owner of a portfolio)
    source: str                      # e.g. "web:portfolio", "web:accounts", "chat", "batch:evening", "cli"
    request_id: str = field(default_factory=_new_request_id)

    def child(self) -> "RequestContext":
        """Same actor and source, fresh request id — for a new user action in the same session."""
        return RequestContext(actor=self.actor, source=self.source)


def local_context(source: str) -> RequestContext:
    """Context for the configured local owner (single-user deployment)."""
    return RequestContext(actor=LOCAL_OWNER, source=source)


def system_context(source: str) -> RequestContext:
    """Context for unattended jobs (price refresh, batches). Acts within the local owner's portfolio."""
    return RequestContext(actor=LOCAL_OWNER, source=f"{SYSTEM_ACTOR}:{source}")
