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


def _fails(message: str):
    """A model call that always raises -- clearer than a throwing lambda."""
    def call(prompt, model):
        raise RuntimeError(message)
    return call


class FakeSettings:
    gemini_model = "primary"
    gemini_fallback_model = "lite"
    groq_model = "groq-model"
    groq_api_key = "key"
    gemini_cooldown_minutes = 30


@pytest.fixture(autouse=True)
def _no_cooldown():
    """Cooldown is module state; a test must not leak it into the next one."""
    llm.clear_gemini_cooldown()
    yield
    llm.clear_gemini_cooldown()


@pytest.fixture
def fake_settings(monkeypatch):
    monkeypatch.setattr(llm, "get_settings", lambda: FakeSettings())
    return FakeSettings()


def test_an_overloaded_model_steps_down_to_the_lighter_one(fake_settings, monkeypatch):
    tried = []

    def fake(prompt, model):
        tried.append(model)
        if model == "primary":
            raise RuntimeError("503 UNAVAILABLE high demand")
        return _payload()

    monkeypatch.setattr(llm, "_gemini", fake)
    _, model = llm._call_model("prompt")
    assert tried == ["primary", "lite"]
    assert model is llm.ModelUsed.gemini


def test_a_permanent_error_does_not_step_down(fake_settings, monkeypatch):
    """A retired model is not a capacity problem; burning the lighter model on
    it just wastes a request. It still falls through to Groq."""
    tried = []

    def fake(prompt, model):
        tried.append(model)
        raise RuntimeError("404 NOT_FOUND no longer available")

    monkeypatch.setattr(llm, "_gemini", fake)
    monkeypatch.setattr(llm, "_groq", lambda p, m: _payload())
    _, model = llm._call_model("prompt")
    assert tried == ["primary"]
    assert model is llm.ModelUsed.groq


# ---------------------------------------------------------------------------
# Groq fallback -- plan.md section 8
# ---------------------------------------------------------------------------

def test_gemini_is_used_when_it_works(fake_settings, monkeypatch):
    monkeypatch.setattr(llm, "_gemini", lambda p, m: _payload())
    monkeypatch.setattr(llm, "_groq", lambda p, m: pytest.fail("groq must not be called"))
    _, model = llm._call_model("prompt")
    assert model is llm.ModelUsed.gemini


def test_groq_takes_over_when_every_gemini_model_fails(fake_settings, monkeypatch):
    """Phase 5's acceptance test: force Gemini to fail."""
    def boom(prompt, model):
        raise RuntimeError("503 UNAVAILABLE high demand")

    monkeypatch.setattr(llm, "_gemini", boom)
    monkeypatch.setattr(llm, "_groq", lambda p, m: _payload())
    text, model = llm._call_model("prompt")
    assert model is llm.ModelUsed.groq
    assert len(llm.parse_drafts(text)) == 3


def test_groq_gets_the_identical_prompt(fake_settings, monkeypatch):
    """Style must not change when the provider does."""
    seen: list[str] = []

    def fake_groq(prompt, model):
        seen.append(prompt)
        return _payload()

    monkeypatch.setattr(llm, "_gemini", _fails("503 UNAVAILABLE"))
    monkeypatch.setattr(llm, "_groq", fake_groq)
    llm._call_model("THE PROMPT")
    assert seen == ["THE PROMPT"]


def test_a_quota_error_starts_the_cooldown(fake_settings, monkeypatch):
    monkeypatch.setattr(llm, "_gemini", _fails("429 RESOURCE_EXHAUSTED quota exceeded"))
    monkeypatch.setattr(llm, "_groq", lambda p, m: _payload())
    assert not llm.gemini_in_cooldown()
    llm._call_model("prompt")
    assert llm.gemini_in_cooldown()


def test_a_capacity_error_does_not_start_the_cooldown(fake_settings, monkeypatch):
    """Being busy for a moment is not the same as being out of allowance."""
    monkeypatch.setattr(llm, "_gemini", _fails("503 UNAVAILABLE"))
    monkeypatch.setattr(llm, "_groq", lambda p, m: _payload())
    llm._call_model("prompt")
    assert not llm.gemini_in_cooldown()


def test_during_cooldown_gemini_is_not_called_at_all(fake_settings, monkeypatch):
    """The point of the cooldown is to stop collecting 429s on every request."""
    monkeypatch.setattr(llm, "_gemini", lambda p, m: pytest.fail("gemini called during cooldown"))
    monkeypatch.setattr(llm, "_groq", lambda p, m: _payload())
    llm.start_gemini_cooldown(minutes=30)
    _, model = llm._call_model("prompt")
    assert model is llm.ModelUsed.groq


def test_cooldown_expires_and_gemini_is_tried_again(fake_settings, monkeypatch):
    monkeypatch.setattr(llm, "_gemini", lambda p, m: _payload())
    monkeypatch.setattr(llm, "_groq", lambda p, m: pytest.fail("cooldown should have expired"))
    llm.start_gemini_cooldown(minutes=-1)     # already in the past
    _, model = llm._call_model("prompt")
    assert model is llm.ModelUsed.gemini


def test_if_groq_also_fails_during_cooldown_gemini_is_tried_anyway(fake_settings, monkeypatch):
    """A stale cooldown is a worse reason to fail than a possibly-recovered quota."""
    monkeypatch.setattr(llm, "_gemini", lambda p, m: _payload())
    monkeypatch.setattr(llm, "_groq", _fails("groq down"))
    llm.start_gemini_cooldown(minutes=30)
    _, model = llm._call_model("prompt")
    assert model is llm.ModelUsed.gemini
    assert not llm.gemini_in_cooldown(), "the stale cooldown should have been cleared"


def test_both_providers_failing_raises(fake_settings, monkeypatch):
    monkeypatch.setattr(llm, "_gemini", _fails("503"))
    monkeypatch.setattr(llm, "_groq", _fails("groq down"))
    with pytest.raises(llm.GenerationError):
        llm.generate(_user(), "topic", PostCategory.learning, Tone.direct)


def test_the_model_that_answered_is_recorded(fake_settings, monkeypatch):
    """post.model_used exists so analytics can tell the two apart."""
    monkeypatch.setattr(llm, "_gemini", _fails("503"))
    monkeypatch.setattr(llm, "_groq", lambda p, m: _payload())
    g = llm.generate(_user(), "topic", PostCategory.learning, Tone.direct)
    assert g.model_used is llm.ModelUsed.groq


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
