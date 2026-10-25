"""Business mode -- plan.md F20, F3, section 8.

The honest half of the product. LinkedIn will not let this app publish to a
company page (plan section 2), so the app does everything up to the last step
and hands over the text. What matters here:

* the handed-over text is what a person pastes, not what the API would receive
* the "your page" link cannot be pointed anywhere else
* a copied post is not claimed to be published
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth, limits, llm, sanitize
from app.database import SessionLocal
from app.main import app
from app.models import (
    AccountType,
    ModelUsed,
    Post,
    PostCategory,
    PostStatus,
    Tone,
    User,
)
from app.onboarding import _valid_page_url
from app.prompts import POST_TYPES, build_prompt, hashtag_range
from tests.conftest import purge_users

SUB = "brandUser"


# ---------------------------------------------------------------------------
# The company page link (offline)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("given,expected", [
    ("https://www.linkedin.com/company/acme-valves/",
     "https://www.linkedin.com/company/acme-valves/"),
    ("linkedin.com/company/acme", "https://linkedin.com/company/acme"),
    ("https://linkedin.com/company/acme?trk=x", "https://linkedin.com/company/acme?trk=x"),
])
def test_a_real_company_page_is_kept(given, expected):
    assert _valid_page_url(given) == expected


@pytest.mark.parametrize("hostile", [
    "https://evil.example.com/company/acme",        # not LinkedIn
    "https://linkedin.com.evil.com/company/acme",   # look-alike host
    "https://linkedin.com/in/someone",              # a profile, not a page
    "javascript:alert(1)",
    "  ",
])
def test_anything_else_is_dropped(hostile):
    """The app renders this as a link and invites the user to click it, so an
    arbitrary address here is a link we vouched for."""
    assert _valid_page_url(hostile) == ""


# ---------------------------------------------------------------------------
# The brand post itself (offline)
# ---------------------------------------------------------------------------

def _brand_user() -> User:
    u = User(linkedin_person_urn="urn:li:person:x", name="Founder")
    u.role_title = "Founder"
    u.niche = "industrial valves"
    u.account_type = AccountType.business
    u.company_name = "Acme Valves"
    u.products = "ball valves, gate valves"
    u.target_markets = "UAE, Germany"
    u.buyer_types = "importers, EPC contractors"
    return u


def test_the_brand_prompt_carries_the_company_context():
    p = build_prompt(_brand_user(), "new SS316 range", PostCategory.business, Tone.professional)
    for expected in ("Acme Valves", "ball valves", "UAE", "importers"):
        assert expected in p


def test_the_brand_post_asks_for_its_own_structure():
    spec = POST_TYPES[PostCategory.business]
    for expected in ("emoji tagline hook", "feature list", "call to action", "contact invite"):
        assert expected in spec.structure


def test_brand_posts_get_more_hashtags_but_nowhere_near_thirty():
    """The original plan allowed up to 30, which suppresses reach rather than
    extending it."""
    assert hashtag_range(_brand_user(), PostCategory.business) == (8, 10)
    p = build_prompt(_brand_user(), "t", PostCategory.business, Tone.direct)
    assert "8 to 10" in p
    assert "Thirty hashtags reduces reach" in p


def test_a_personal_account_gets_fewer_hashtags_than_a_brand_one():
    """5-8, raised from 3-5. Still below the brand range, and far below the
    dozen or so at which LinkedIn starts reading a post as tag-stuffing."""
    personal = _brand_user()
    personal.account_type = AccountType.personal
    assert hashtag_range(personal, PostCategory.learning) == (5, 8)

    _, hi = hashtag_range(personal, PostCategory.learning)
    _, brand_hi = hashtag_range(_brand_user(), PostCategory.business)
    assert hi <= brand_hi, "a personal post should not out-tag a brand post"
    assert hi <= 12, "past about a dozen, more tags cost reach rather than buying it"


def test_what_gets_copied_is_the_plain_text_not_the_escaped_form():
    """The user pastes into LinkedIn's composer, which takes plain text.
    Pasting the API form would publish visible backslashes."""
    prepared = sanitize.prepare("Our new range (SS316) is here.", ["Valves", "Industry"])
    assert prepared.preview == "Our new range (SS316) is here.\n\n#Valves #Industry"
    assert "\\" not in prepared.preview
    assert "\\(" in prepared.commentary        # the API form differs, as it should


# ---------------------------------------------------------------------------
# The flow (database)
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture(autouse=True)
def _purge(request):
    if "integration" not in request.keywords:
        yield
        return
    s = SessionLocal()

    def purge():
        purge_users("brand", s)

    purge()
    yield
    purge()
    s.close()


def _fake_generation(*a, **k):
    body = ("⚙️ Corrosion resistance for demanding lines.\n\n"
            "➜ Cast in CF8M stainless steel\n➜ ISO 5211 mounting pads\n\n"
            "At Acme Valves, we build flow control equipment (built to last).\n\n"
            "Message me for the catalogue.")
    drafts = [llm.Draft(body=body, hashtags=["AcmeValves", "BallValves", "StainlessSteel",
                                             "IndustrialValves", "UAEIndustry", "EPCContractors",
                                             "FlowControl", "Manufacturing"],
                        photo_query="stainless steel valve", score={"hook": 8},
                        improvement="") for _ in range(3)]
    return llm.Generation(drafts=drafts, model_used=ModelUsed.gemini, attempts=1)


@pytest.fixture
def client(monkeypatch, db):
    monkeypatch.setattr(auth, "exchange_code_for_token",
                        lambda c: {"access_token": "tok", "expires_in": 5_184_000})
    monkeypatch.setattr(auth, "fetch_userinfo",
                        lambda t: {"sub": SUB, "name": "Brand Test", "email": f"{SUB}@x.com"})
    monkeypatch.setattr(llm, "generate", _fake_generation)
    monkeypatch.setattr(limits, "get_settings", lambda: type("S", (), {"daily_post_limit": 20})())

    c = TestClient(app, follow_redirects=False)
    st = c.get("/auth/login").headers["location"].split("state=")[1].split("&")[0]
    c.get(f"/auth/callback?code=x&state={st}")
    return c


def _make_business(db) -> User:
    user = db.query(User).filter_by(linkedin_person_urn=f"urn:li:person:{SUB}").one()
    user.account_type = AccountType.business
    user.company_name = "Acme Valves"
    user.company_page_url = "https://www.linkedin.com/company/acme-valves/"
    db.commit()
    return user


@pytest.mark.integration
def test_onboarding_stores_a_company_page(client, db):
    client.post("/onboarding", data={
        "account_type": "business", "company_name": "Acme Valves",
        "products": "valves", "timezone": "UTC", "default_tone": "direct",
        "company_page_url": "linkedin.com/company/acme-valves",
    })
    db.expire_all()
    user = db.query(User).filter_by(linkedin_person_urn=f"urn:li:person:{SUB}").one()
    assert user.company_page_url == "https://linkedin.com/company/acme-valves"


@pytest.mark.integration
def test_switching_back_to_personal_clears_the_page_link(client, db):
    client.post("/onboarding", data={
        "account_type": "business", "company_name": "Acme", "timezone": "UTC",
        "default_tone": "direct", "company_page_url": "linkedin.com/company/acme"})
    client.post("/onboarding", data={
        "account_type": "personal", "role_title": "Engineer", "timezone": "UTC",
        "default_tone": "direct"})
    db.expire_all()
    user = db.query(User).filter_by(linkedin_person_urn=f"urn:li:person:{SUB}").one()
    assert user.company_page_url == ""


@pytest.mark.integration
def test_a_business_account_lands_on_the_brand_post_type(client, db):
    _make_business(db)
    page = client.get("/new").text
    assert 'value="business"' in page
    brand_checked = page.split('value="business"')[1].split(">")[0]
    assert "checked" in brand_checked


@pytest.mark.integration
def test_a_brand_post_offers_the_company_page_flow(client, db):
    _make_business(db)
    r = client.post("/new", data={"topic": "our new SS316 valve range",
                                  "post_category": "business", "tone": "professional"})
    pid = int(r.headers["location"].split("/")[2])
    client.post(f"/posts/{pid}/choose", data={"index": 0})

    page = client.get(f"/posts/{pid}").text
    assert "Post this to your company page" in page
    assert "https://www.linkedin.com/company/acme-valves/" in page
    assert "Copy the post" in page


@pytest.mark.integration
def test_the_copy_button_carries_plain_text_not_escaped(client, db):
    """A backslash in the copied text would be pasted literally."""
    _make_business(db)
    r = client.post("/new", data={"topic": "our new range", "post_category": "business",
                                  "tone": "professional"})
    pid = int(r.headers["location"].split("/")[2])
    client.post(f"/posts/{pid}/choose", data={"index": 0})

    page = client.get(f"/posts/{pid}").text
    copy_block = page.split('id="copybtn"')[1].split("</button>")[0]
    assert "built to last" in copy_block            # the parenthesised phrase survived
    assert "\\(" not in copy_block


@pytest.mark.integration
def test_a_personal_post_has_no_company_page_panel(client, db):
    r = client.post("/new", data={"topic": "what I learned today",
                                  "post_category": "learning", "tone": "direct"})
    pid = int(r.headers["location"].split("/")[2])
    client.post(f"/posts/{pid}/choose", data={"index": 0})
    assert "Post this to your company page" not in client.get(f"/posts/{pid}").text


@pytest.mark.integration
def test_marking_as_copied_does_not_claim_it_was_published(client, db):
    """The app never sees the post reach LinkedIn. Calling it `posted` would be
    a claim we cannot support, and posted rows carry a LinkedIn id."""
    _make_business(db)
    r = client.post("/new", data={"topic": "our new range", "post_category": "business",
                                  "tone": "professional"})
    pid = int(r.headers["location"].split("/")[2])
    client.post(f"/posts/{pid}/choose", data={"index": 0})

    assert client.post(f"/posts/{pid}/copied").status_code == 303
    db.expire_all()
    post = db.get(Post, pid)
    assert post.copied_at is not None
    assert post.status is PostStatus.draft
    assert post.linkedin_post_id is None
    assert "Marked as posted to your page" in client.get(f"/posts/{pid}").text


@pytest.mark.integration
def test_someone_elses_post_cannot_be_marked_copied(client, monkeypatch, db):
    other_sub = "brandOther"
    monkeypatch.setattr(auth, "fetch_userinfo",
                        lambda t: {"sub": other_sub, "name": "B", "email": f"{other_sub}@x.com"})
    other = TestClient(app, follow_redirects=False)
    st = other.get("/auth/login").headers["location"].split("state=")[1].split("&")[0]
    other.get(f"/auth/callback?code=x&state={st}")
    r = other.post("/new", data={"topic": "their topic", "post_category": "business",
                                 "tone": "direct"})
    pid = int(r.headers["location"].split("/")[2])

    assert client.post(f"/posts/{pid}/copied").status_code == 404
