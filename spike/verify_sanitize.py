#!/usr/bin/env python3
"""Prove the sanitizer against the live LinkedIn API.

The 77 unit tests show app/sanitize.py is self-consistent. They cannot show
that LinkedIn agrees with our reading of its `little` format. Only a real post
can, because the failure mode is a silent truncation behind a 201.

Publishes one post containing every reserved character, prints what a reader
should see, then deletes it.

    python spike/verify_sanitize.py publish
    python spike/verify_sanitize.py delete <urn>
"""

from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import security                      # noqa: E402
from app.config import get_settings           # noqa: E402
from app.database import SessionLocal         # noqa: E402
from app.models import User                   # noqa: E402
from app import sanitize                      # noqa: E402

API = "https://api.linkedin.com"

# Every reserved character, plus the markdown and arrows the models actually
# emit. If any of this is mishandled, the post truncates at that point.
BODY = """\
## Sanitizer check — please ignore

This post exists to test text encoding and will be deleted in a minute.

- Parentheses (like this) and brackets [like this]
- Braces {like this} and angles <like this>
- Symbols: at @ hash # pipe | tilde ~ star * under _ backslash \\
- **Bold markdown** and *italic* and `inline code`
-> An ASCII arrow that should render as a real arrow
- Money: $1,200/mo — 50% faster; 3/4 done?

[A markdown link](https://example.com/a_b?x=1#frag)

If you can read this whole post down to here, the encoding is correct."""

TAGS = ["SanitizerTest", "Ignore"]


def _auth() -> tuple[str, str]:
    db = SessionLocal()
    user = db.query(User).filter(User.deleted_at.is_(None)).first()
    if not user or not user.access_token:
        sys.exit("No signed-in user with a token. Sign in at http://localhost:8000 first.")
    return security.decrypt(user.access_token), user.linkedin_person_urn


def _headers(token: str) -> dict:
    s = get_settings()
    return {
        "Authorization": f"Bearer {token}",
        "LinkedIn-Version": s.linkedin_api_version,
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }


def publish() -> None:
    token, author = _auth()
    prepared = sanitize.prepare(BODY, TAGS)

    print("=" * 70)
    print("WHAT A READER SHOULD SEE:")
    print("=" * 70)
    print(prepared.preview)
    print("=" * 70)
    print(f"preview {prepared.length} chars | escaped {len(prepared.commentary)} chars")
    print()

    resp = requests.post(
        f"{API}/rest/posts",
        headers=_headers(token),
        json={
            "author": author,
            "commentary": prepared.commentary,
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        },
        timeout=30,
    )
    if resp.status_code != 201:
        sys.exit(f"publish failed {resp.status_code}: {resp.text[:400]}")

    urn = resp.headers.get("x-restli-id", "")
    print(f"published: {urn}")
    print(f"url:       https://www.linkedin.com/feed/update/{urn}/")
    print()
    print(f"delete it with:  python spike/verify_sanitize.py delete {urn}")
    Path(__file__).with_name(".verify_urn").write_text(urn)


def delete(urn: str) -> None:
    token, _ = _auth()
    encoded = urllib.parse.quote(urn, safe="")
    headers = _headers(token) | {"X-RestLi-Method": "DELETE"}
    resp = requests.delete(f"{API}/rest/posts/{encoded}", headers=headers, timeout=30)
    if resp.status_code in (200, 204):
        print(f"deleted {urn}")
    else:
        print(f"delete failed {resp.status_code}: {resp.text[:300]}")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "publish":
        publish()
    elif len(sys.argv) == 3 and sys.argv[1] == "delete":
        delete(sys.argv[2])
    else:
        print(__doc__)
