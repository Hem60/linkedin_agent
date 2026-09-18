#!/usr/bin/env python3
"""
Phase 0 feasibility spike for the LinkedIn Post Agent.

Proves the five things the whole plan rests on, against your own developer app:

  1. OAuth works and w_member_social is actually granted
  2. POST /rest/posts publishes a text post           -> 201 + post URN
  3. `little` format escaping is required             -> probe 2 vs probe 3
  4. Hashtag templates render as real hashtags
  5. An image post works (two-step Images API)
  6. A first comment works with w_member_social       <- the one open question

WARNING: probes 2-6 publish REAL posts to YOUR REAL LinkedIn profile. There is no
"only me" visibility on LinkedIn. The script asks for confirmation, prints every
post URL, and can delete everything it created with `cleanup`.

Usage:
    pip install -r requirements.txt
    cp .env.example .env        # fill in client id + secret
    python phase0_spike.py auth
    python phase0_spike.py whoami
    python phase0_spike.py probe
    python phase0_spike.py cleanup
"""

from __future__ import annotations

import http.server
import json
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.getenv("LINKEDIN_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("LINKEDIN_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("REDIRECT_URI", "http://localhost:8000/callback")

# LinkedIn versioned APIs want YYYYMM. If you get a 400 complaining about the
# version, try an adjacent month -- LinkedIn sunsets old ones on a rolling basis.
API_VERSION = os.getenv("LINKEDIN_API_VERSION", "202609")

SCOPES = "openid profile email w_member_social"
TOKEN_FILE = Path(__file__).parent / ".spike_token.json"
CREATED_FILE = Path(__file__).parent / ".spike_created.json"

API = "https://api.linkedin.com"


# --------------------------------------------------------------------------
# The little text format. This is the part that becomes app/sanitize.py.
# --------------------------------------------------------------------------

# Every one of these must be backslash-escaped in `commentary`, even when it is
# just ordinary punctuation. Source:
# learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/little-text-format
RESERVED = set(r"|{}@[]()<>#\*_~")


def escape_little(text: str) -> str:
    """Escape every reserved character. Iterating per-character means the
    backslash escapes itself correctly without any ordering trickery."""
    return "".join("\\" + c if c in RESERVED else c for c in text)


def hashtag_template(tag: str) -> str:
    """A real, clickable hashtag. Plain '#AI' escapes to inert '\\#AI'."""
    return "{hashtag|\\#|" + tag.lstrip("#") + "}"


def build_commentary(body: str, hashtags: list[str] | None = None) -> str:
    """Escape the body, then append hashtag templates. Order matters: the
    templates contain reserved characters that must NOT be escaped."""
    out = escape_little(body)
    if hashtags:
        out += "\n\n" + " ".join(hashtag_template(t) for t in hashtags)
    return out


def rendered_length(body: str, hashtags: list[str] | None = None) -> int:
    """What the reader sees, which is what LinkedIn's 3,000 limit applies to.
    Backslashes are encoding, not visible text, so never count the escaped
    string."""
    n = len(body)
    if hashtags:
        n += 2 + sum(len(t.lstrip("#")) + 2 for t in hashtags) - 1
    return n


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        _CallbackHandler.result = {k: v[0] for k, v in params.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        ok = "code" in _CallbackHandler.result
        msg = "Authorized. Close this tab and return to the terminal." if ok else \
              f"Failed: {_CallbackHandler.result}"
        self.wfile.write(f"<html><body><h2>{msg}</h2></body></html>".encode())

    def log_message(self, *args):
        pass  # keep the terminal clean


def cmd_auth():
    if not CLIENT_ID or not CLIENT_SECRET:
        die("Set LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET in .env first.")

    state = secrets.token_urlsafe(16)
    auth_url = "https://www.linkedin.com/oauth/v2/authorization?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "scope": SCOPES,
    })

    parsed = urllib.parse.urlparse(REDIRECT_URI)
    server = http.server.HTTPServer((parsed.hostname, parsed.port or 80), _CallbackHandler)
    threading.Thread(target=server.handle_request, daemon=True).start()

    print(f"\nOpening browser. Redirect URI must be registered EXACTLY as:\n  {REDIRECT_URI}\n")
    print(f"If the browser does not open, paste this:\n{auth_url}\n")
    webbrowser.open(auth_url)

    server_thread_timeout = 300
    import time
    waited = 0
    while not _CallbackHandler.result and waited < server_thread_timeout:
        time.sleep(0.5)
        waited += 0.5

    result = _CallbackHandler.result
    if "error" in result:
        die(f"LinkedIn returned an error: {result}")
    if result.get("state") != state:
        die("State mismatch -- aborting.")
    if "code" not in result:
        die("No authorization code received (timed out).")

    resp = requests.post(
        "https://www.linkedin.com/oauth/v2/accessToken",
        data={
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        die(f"Token exchange failed {resp.status_code}: {resp.text}")

    payload = resp.json()
    TOKEN_FILE.write_text(json.dumps(payload, indent=2))

    print("Token saved to .spike_token.json")
    print(f"  scopes granted : {payload.get('scope')}")
    print(f"  expires in     : {payload.get('expires_in', 0) // 86400} days")
    if "refresh_token" in payload:
        print("  refresh_token  : PRESENT -- you have MDP partner access (plan assumes you don't)")
    else:
        print("  refresh_token  : absent (expected -- see plan.md section 2)")

    if "w_member_social" not in (payload.get("scope") or ""):
        print("\n  !! w_member_social was NOT granted. Add the 'Share on LinkedIn'")
        print("     product in the Developer Portal Products tab, then re-run auth.")


def token() -> str:
    if not TOKEN_FILE.exists():
        die("No token. Run: python phase0_spike.py auth")
    return json.loads(TOKEN_FILE.read_text())["access_token"]


# --------------------------------------------------------------------------
# API helpers
# --------------------------------------------------------------------------

def headers(extra: dict | None = None) -> dict:
    h = {
        "Authorization": f"Bearer {token()}",
        "LinkedIn-Version": API_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def person_urn() -> str:
    resp = requests.get(f"{API}/v2/userinfo", headers={"Authorization": f"Bearer {token()}"}, timeout=30)
    if resp.status_code != 200:
        die(f"userinfo failed {resp.status_code}: {resp.text}")
    return f"urn:li:person:{resp.json()['sub']}"


def remember(urn: str):
    created = json.loads(CREATED_FILE.read_text()) if CREATED_FILE.exists() else []
    created.append(urn)
    CREATED_FILE.write_text(json.dumps(created, indent=2))


def create_post(author: str, commentary: str, image_urn: str | None = None) -> requests.Response:
    body = {
        "author": author,
        "commentary": commentary,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    if image_urn:
        body["content"] = {"media": {"title": "Phase 0 spike image", "id": image_urn}}
    return requests.post(f"{API}/rest/posts", headers=headers(), json=body, timeout=30)


def report(label: str, resp: requests.Response, expect_failure: bool = False) -> str | None:
    """Print the outcome and return the post URN if one was created."""
    urn = resp.headers.get("x-restli-id")
    if resp.status_code == 201 and urn:
        remember(urn)
        verdict = "201 created (expected a failure!)" if expect_failure else "201 created"
        print(f"  [{label}] {verdict}")
        print(f"      {urn}")
        print(f"      https://www.linkedin.com/feed/update/{urn}/")
        return urn
    verdict = "failed as expected" if expect_failure else "FAILED"
    print(f"  [{label}] {verdict} -- {resp.status_code}")
    print(f"      {resp.text[:400]}")
    return None


# --------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------

BODY_WITH_RESERVED = (
    "Testing the LinkedIn API from a throwaway script.\n\n"
    "This line contains reserved characters: (parentheses), an *asterisk*, "
    "an _underscore_ and a ~tilde~.\n\n"
    "If you are reading this as a clean sentence, escaping worked."
)


def cmd_config():
    """Check .env loaded correctly. Never prints the secret itself."""
    env_file = Path(__file__).parent / ".env"
    print()
    if not env_file.exists():
        print(f"  .env          : MISSING at {env_file}")
        print("                  run: cp .env.example .env")
        return
    print(f"  .env          : found ({env_file})")

    def show(name: str, value: str, mask: bool):
        if not value:
            print(f"  {name:<14}: MISSING")
            return
        if value != value.strip():
            print(f"  {name:<14}: has leading/trailing whitespace -- remove it")
            return
        shown = f"{value[:4]}...{value[-4:]} ({len(value)} chars)" if mask else value
        print(f"  {name:<14}: {shown}")

    show("client id", CLIENT_ID, mask=False)
    show("client secret", CLIENT_SECRET, mask=True)
    show("redirect uri", REDIRECT_URI, mask=False)
    show("api version", API_VERSION, mask=False)

    problems = []
    if CLIENT_SECRET.startswith(('"', "'")) or CLIENT_SECRET.endswith(('"', "'")):
        problems.append("secret is wrapped in quotes -- remove them")
    if REDIRECT_URI.endswith("/"):
        problems.append("redirect uri has a trailing slash -- LinkedIn matches exactly")
    if not API_VERSION.isdigit() or len(API_VERSION) != 6:
        problems.append("api version should be six digits, YYYYMM")

    print()
    if problems:
        for p in problems:
            print(f"  ! {p}")
    elif CLIENT_ID and CLIENT_SECRET:
        print("  Looks good. Next: python phase0_spike.py auth")
    print()


def cmd_whoami():
    urn = person_urn()
    resp = requests.get(f"{API}/v2/userinfo", headers={"Authorization": f"Bearer {token()}"}, timeout=30)
    info = resp.json()
    print(f"\n  name  : {info.get('name')}")
    print(f"  email : {info.get('email')}")
    print(f"  urn   : {urn}\n")


def cmd_probe():
    print(__doc__.split("Usage:")[0])
    print("These probes publish REAL posts to your REAL profile, visible to others.")
    print("Run `cleanup` straight afterwards to delete them.\n")
    if input('Type "yes" to continue: ').strip().lower() != "yes":
        print("Aborted.")
        return

    author = person_urn()
    print(f"\nAuthor: {author}")
    print(f"API version: {API_VERSION}\n")

    # --- Probe 1: plain text, properly escaped -----------------------------
    print("Probe 1 -- plain text, escaped")
    body = "Phase 0 spike: a plain text post with no special characters."
    report("plain", create_post(author, escape_little(body)))

    # --- Probe 2: reserved characters, NOT escaped ------------------------
    # This is the deliberate failure. Expect a 400, or a 201 whose rendered
    # text is mangled. Either way you will only need to see it once.
    print("\nProbe 2 -- reserved characters, deliberately NOT escaped")
    print("  (expected to 400, or to publish visibly broken text)")
    report("unescaped", create_post(author, BODY_WITH_RESERVED), expect_failure=True)

    # --- Probe 3: the same text, escaped ----------------------------------
    print("\nProbe 3 -- the same text, escaped")
    report("escaped", create_post(author, escape_little(BODY_WITH_RESERVED)))

    # --- Probe 4: hashtag templates ---------------------------------------
    print("\nProbe 4 -- hashtag templates")
    tags = ["BuildInPublic", "Python", "LinkedInAPI"]
    commentary = build_commentary(
        "Phase 0 spike: the three tags below should be clickable, not literal text.",
        tags,
    )
    print(f"  rendered length: {rendered_length('Phase 0 spike: the three tags below should be clickable, not literal text.', tags)}")
    report("hashtags", create_post(author, commentary))

    # --- Probe 5: image post ----------------------------------------------
    print("\nProbe 5 -- image post (two-step Images API)")
    image_urn = upload_test_image(author)
    if image_urn:
        print(f"  image urn: {image_urn}")
        post_urn = report("image", create_post(
            author, escape_little("Phase 0 spike: image post."), image_urn=image_urn))
    else:
        post_urn = None

    # --- Probe 6: first comment -- THE open question ----------------------
    print("\nProbe 6 -- first comment with w_member_social")
    print("  Docs conflict here: the Open Permissions table says w_member_social")
    print("  covers commenting, but the Comments API page lists w_member_social_feed.")
    target = post_urn or _last_created()
    if not target:
        print("  [comment] skipped -- no post to comment on")
    else:
        encoded = urllib.parse.quote(target, safe="")
        resp = requests.post(
            f"{API}/rest/socialActions/{encoded}/comments",
            headers=headers(),
            json={
                "actor": author,
                "object": target,
                "message": {"text": "Link in the first comment: https://example.com"},
            },
            timeout=30,
        )
        if resp.status_code in (200, 201):
            print("  [comment] OK -- F21 confirmed, links go in the first comment")
        elif resp.status_code == 403:
            print("  [comment] 403 -- w_member_social does NOT cover commenting.")
            print("            Move F21 to v2 and put links in the post body.")
            print(f"            {resp.text[:300]}")
        else:
            print(f"  [comment] {resp.status_code}: {resp.text[:300]}")

    print("\nDone. Open the URLs above and compare probe 2 against probe 3.")
    print("Then run: python phase0_spike.py cleanup\n")


def _last_created() -> str | None:
    if not CREATED_FILE.exists():
        return None
    created = json.loads(CREATED_FILE.read_text())
    return created[-1] if created else None


def upload_test_image(author: str) -> str | None:
    """Two-step: initializeUpload for a URN + upload URL, then PUT the bytes."""
    try:
        from PIL import Image
    except ImportError:
        print("  Pillow not installed -- skipping. pip install Pillow")
        return None

    tmp = Path(__file__).parent / ".spike_image.png"
    Image.new("RGB", (1200, 627), (26, 86, 219)).save(tmp)

    init = requests.post(
        f"{API}/rest/images?action=initializeUpload",
        headers=headers(),
        json={"initializeUploadRequest": {"owner": author}},
        timeout=30,
    )
    if init.status_code not in (200, 201):
        print(f"  initializeUpload failed {init.status_code}: {init.text[:300]}")
        return None

    value = init.json()["value"]
    put = requests.put(
        value["uploadUrl"],
        headers={"Authorization": f"Bearer {token()}"},
        data=tmp.read_bytes(),
        timeout=60,
    )
    if put.status_code not in (200, 201):
        print(f"  image PUT failed {put.status_code}: {put.text[:300]}")
        return None
    return value["image"]


def cmd_cleanup():
    if not CREATED_FILE.exists():
        print("Nothing recorded.")
        return
    created = json.loads(CREATED_FILE.read_text())
    if not created:
        print("Nothing to delete.")
        return

    print(f"\nAbout to permanently delete {len(created)} post(s) from your profile:")
    for urn in created:
        print(f"  {urn}")
    if input('\nType "delete" to confirm: ').strip().lower() != "delete":
        print("Aborted. Nothing deleted.")
        return

    remaining = []
    for urn in created:
        encoded = urllib.parse.quote(urn, safe="")
        resp = requests.delete(
            f"{API}/rest/posts/{encoded}",
            headers=headers({"X-RestLi-Method": "DELETE"}),
            timeout=30,
        )
        if resp.status_code in (200, 204):
            print(f"  deleted {urn}")
        else:
            print(f"  FAILED {urn} -- {resp.status_code}: {resp.text[:200]}")
            remaining.append(urn)
    CREATED_FILE.write_text(json.dumps(remaining, indent=2))
    if remaining:
        print("\nSome posts were not deleted. Remove them by hand from your profile.")


# --------------------------------------------------------------------------

def die(msg: str):
    print(f"\nERROR: {msg}\n", file=sys.stderr)
    sys.exit(1)


COMMANDS = {
    "config": cmd_config,
    "auth": cmd_auth,
    "whoami": cmd_whoami,
    "probe": cmd_probe,
    "cleanup": cmd_cleanup,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(f"Usage: python {Path(__file__).name} [{' | '.join(COMMANDS)}]")
        sys.exit(1)
    COMMANDS[sys.argv[1]]()
