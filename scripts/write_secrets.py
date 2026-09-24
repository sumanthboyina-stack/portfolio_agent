#!/usr/bin/env python3
"""
Render .streamlit/secrets.toml from environment variables.

Streamlit's [auth] block only ever reads secrets.toml — never env vars — so
a host that only exposes config as env vars / App Settings (e.g. Azure App
Service) needs this run once, before Streamlit starts, from the startup
command:

    python scripts/write_secrets.py && streamlit run web/app.py

Required env vars: AUTH_REDIRECT_URI, AUTH_COOKIE_SECRET, ENTRA_CLIENT_ID,
ENTRA_CLIENT_SECRET, ENTRA_METADATA_URL.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_REQUIRED = [
    "AUTH_REDIRECT_URI",
    "AUTH_COOKIE_SECRET",
    "ENTRA_CLIENT_ID",
    "ENTRA_CLIENT_SECRET",
    "ENTRA_METADATA_URL",
]

_TEMPLATE = """\
[auth]
redirect_uri = "{redirect_uri}"
cookie_secret = "{cookie_secret}"

[auth.entra]
client_id = "{client_id}"
client_secret = "{client_secret}"
server_metadata_url = "{metadata_url}"
client_kwargs = {{ scope = "openid profile email" }}
"""


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def main() -> None:
    missing = [name for name in _REQUIRED if not os.environ.get(name)]
    if missing:
        print(f"write_secrets: missing required env var(s): {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    out = Path(__file__).resolve().parents[1] / ".streamlit" / "secrets.toml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        _TEMPLATE.format(
            redirect_uri=_escape(os.environ["AUTH_REDIRECT_URI"]),
            cookie_secret=_escape(os.environ["AUTH_COOKIE_SECRET"]),
            client_id=_escape(os.environ["ENTRA_CLIENT_ID"]),
            client_secret=_escape(os.environ["ENTRA_CLIENT_SECRET"]),
            metadata_url=_escape(os.environ["ENTRA_METADATA_URL"]),
        )
    )
    print(f"write_secrets: wrote {out}")


if __name__ == "__main__":
    main()
