"""The model contract -- plan.md section 7.

No network: `_call_model` is replaced. These test our parsing, validation and
retry, which is where the bugs live. Model quality is not something a unit test
can assert.
"""

from __future__ import annotations

import json

import pytest

from app import llm
from app.models import AccountType, PostCategory, Tone, User
from app.prompts import POST_TYPES, build_prompt, hashtag_range


def _draft(body="A hook line.\n\nA second paragraph.", tags=("RAG", "MLOps", "AI")):
    return {
        "body": body,
        "hashtags": list(tags),
        "photo_query": "server room",
        "score": {"hook": 8, "clarity": 7, "readability": 9},
        "improvement": "The close could be sharper.",
    }


def _payload(n=3, **kw):
    return json.dumps({"drafts": [_draft(**kw) for _ in range(n)]})


def _user(**kw):
    u = User(linkedin_person_urn="urn:li:person:x", name="Test")
    u.role_title = kw.get("role_title", "ML Engineer")
    u.niche = kw.get("niche", "RAG systems")
    u.account_type = kw.get("account_type", AccountType.personal)
    u.company_name = kw.get("company_name", "")
    u.products = kw.get("products", "")
    u.target_markets = ""
    u.buyer_types = ""
    return u


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parses_a_clean_reply():
    drafts = llm.parse_drafts(_payload())
    assert len(drafts) == 3
    assert drafts[0].hashtags == ["RAG", "MLOps", "AI"]
    assert drafts[0].overall == 8.0


def test_strips_code_fences():
    """Models wrap JSON in fences despite being told not to."""
    assert len(llm.parse_drafts("```json\n" + _payload() + "\n```")) == 3


def test_tolerates_a_sentence_before_the_json():
    assert len(llm.parse_drafts("Here are your drafts:\n" + _payload())) == 3


def test_hashes_are_stripped_from_hashtags():
    """Stored without '#' so sanitize.py can build LinkedIn's hashtag template."""
    raw = json.dumps({"drafts": [_draft(tags=["#RAG", "#MLOps", "#AI"]) for _ in range(3)]})
    assert llm.parse_drafts(raw)[0].hashtags == ["RAG", "MLOps", "AI"]


def test_extra_hashtags_are_trimmed_to_the_limit():
    many = [f"Tag{i}" for i in range(20)]
    raw = json.dumps({"drafts": [_draft(tags=many) for _ in range(3)]})
    drafts = llm.parse_drafts(raw, tag_range=(3, 5))
    assert len(drafts[0].hashtags) == 5


def test_rejects_non_json():
    with pytest.raises(json.JSONDecodeError):
        llm.parse_drafts("I'm sorry, I can't help with that.")


def test_rejects_the_wrong_number_of_drafts():
    with pytest.raises(ValueError, match="expected 3 drafts, got 2"):
        llm.parse_drafts(_payload(n=2))


def test_rejects_an_empty_body():
    with pytest.raises(ValueError, match="empty body"):
        llm.parse_drafts(_payload(body="   "))


def test_rejects_a_missing_drafts_array():
    with pytest.raises(ValueError, match="drafts"):
        llm.parse_drafts(json.dumps({"posts": []}))


# ---------------------------------------------------------------------------
# Length, measured as the reader sees it
# ---------------------------------------------------------------------------

def test_rendered_length_counts_body_and_hashtags():
    d = llm.parse_drafts(_payload(body="x" * 100))[0]
    assert d.rendered_length == 100 + len("#RAG #MLOps #AI") + 2


def test_over_length_drafts_are_reported_not_rejected():
    """The user should see the problem and be able to ask for a rewrite; a long
    draft is not a parse failure."""
    drafts = llm.parse_drafts(_payload(body="x" * 3200))
    assert llm.problems(drafts, (3, 5))
    assert any("over LinkedIn's 3000" in p for p in llm.problems(drafts, (3, 5)))


def test_identical_drafts_are_reported():
    assert any("identical" in p for p in llm.problems(llm.parse_drafts(_payload()), (3, 5)))


def test_too_few_hashtags_is_reported():
    raw = json.dumps({"drafts": [_draft(tags=["One"]) for _ in range(3)]})
    notes = llm.problems(llm.parse_drafts(raw), (3, 5))
    assert any("hashtags" in n for n in notes)


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------

def test_a_bad_reply_is_retried_once_and_can_succeed(monkeypatch):
    calls = []

    def fake(prompt):
        calls.append(prompt)
        return ("not json" if len(calls) == 1 else _payload()), llm.ModelUsed.gemini

    monkeypatch.setattr(llm, "_call_model", fake)
    g = llm.generate(_user(), "topic", PostCategory.learning, Tone.direct)
    assert g.attempts == 2
    assert len(g.drafts) == 3


def test_the_retry_tells_the_model_what_was_wrong(monkeypatch):
    """Repeating an identical prompt is a weaker bet than saying what failed."""
    prompts = []

    def fake(prompt):
        prompts.append(prompt)
        return ("{}" if len(prompts) == 1 else _payload()), llm.ModelUsed.gemini

    monkeypatch.setattr(llm, "_call_model", fake)
    llm.generate(_user(), "topic", PostCategory.learning, Tone.direct)
    assert "was rejected" in prompts[1]
    assert "drafts" in prompts[1]


def test_giving_up_raises_after_the_last_attempt(monkeypatch):
    monkeypatch.setattr(llm, "_call_model", lambda p: ("nope", llm.ModelUsed.gemini))
    with pytest.raises(llm.GenerationError):
        llm.generate(_user(), "topic", PostCategory.learning, Tone.direct)


def test_a_transport_error_is_retried(monkeypatch):
    calls = []

    def fake(prompt):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("read timeout")
        return _payload(), llm.ModelUsed.gemini

    monkeypatch.setattr(llm, "_call_model", fake)
    assert llm.generate(_user(), "t", PostCategory.tips, Tone.direct).attempts == 2


# ---------------------------------------------------------------------------
# Model availability
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("message", [
    "503 UNAVAILABLE. This model is currently experiencing high demand.",
    "504 DEADLINE_EXCEEDED. Deadline expired before operation could complete.",
    "429 RESOURCE_EXHAUSTED",
])
def test_capacity_errors_are_recognised_as_transient(message):
    assert llm._is_transient(Exception(message))


@pytest.mark.parametrize("message", [
    "400 INVALID_ARGUMENT: bad request",
    "404 NOT_FOUND. This model is no longer available to new users.",
])
def test_other_errors_are_not_transient(message):
    """A retired model or a malformed request will fail identically on retry."""
    assert not llm._is_transient(Exception(message))


def test_an_overloaded_model_steps_down_to_the_lighter_one(monkeypatch):
    tried = []

    def fake(prompt, model):
        tried.append(model)
        if model == "primary":
            raise RuntimeError("503 UNAVAILABLE high demand")
        return _payload()

    monkeypatch.setattr(llm, "_gemini", fake)
    monkeypatch.setattr(llm, "get_settings", lambda: type(
        "S", (), {"gemini_model": "primary", "gemini_fallback_model": "lite"})())
    text, model = llm._call_model("prompt")
    assert tried == ["primary", "lite"]
    assert model is llm.ModelUsed.gemini


def test_a_permanent_error_does_not_step_down(monkeypatch):
    """A retired model is not a capacity problem; burning the fallback on it
    just wastes a request."""
    tried = []

    def fake(prompt, model):
        tried.append(model)
        raise RuntimeError("404 NOT_FOUND no longer available")

    monkeypatch.setattr(llm, "_gemini", fake)
    monkeypatch.setattr(llm, "get_settings", lambda: type(
        "S", (), {"gemini_model": "primary", "gemini_fallback_model": "lite"})())
    with pytest.raises(RuntimeError):
        llm._call_model("prompt")
    assert tried == ["primary"]


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def test_every_post_category_has_a_spec():
    assert set(POST_TYPES) == set(PostCategory)


def test_prompt_carries_the_writer_context():
    p = build_prompt(_user(), "my topic", PostCategory.learning, Tone.storytelling)
    assert "ML Engineer" in p
    assert "RAG systems" in p
    assert "my topic" in p


def test_prompt_forbids_markdown():
    """Markdown in a published post is the failure mode from plan section 9."""
    p = build_prompt(_user(), "t", PostCategory.tips, Tone.direct)
    assert "Do not use markdown" in p


def test_business_accounts_get_the_wider_hashtag_range():
    assert hashtag_range(_user(), PostCategory.learning) == (3, 5)
    assert hashtag_range(_user(account_type=AccountType.business),
                         PostCategory.business) == (8, 10)


def test_business_prompt_caps_hashtags_well_below_thirty():
    u = _user(account_type=AccountType.business, company_name="Acme Valves",
              products="ball valves")
    p = build_prompt(u, "new product", PostCategory.business, Tone.direct)
    assert "Acme Valves" in p
    assert "8 to 10" in p
    assert "Thirty hashtags reduces reach" in p


def test_a_skipped_extra_question_tells_the_model_not_to_invent():
    p = build_prompt(_user(), "shipped it", PostCategory.project, Tone.direct)
    assert "skipped" in p and "Do not invent" in p


def test_an_answered_extra_question_is_included():
    p = build_prompt(_user(), "shipped it", PostCategory.project, Tone.direct,
                     extra_answer="github.com/hem/postpilot")
    assert "github.com/hem/postpilot" in p
