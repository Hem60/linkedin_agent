"""The `little` text format -- plan.md section 9.

This is the file that stops a broken post reaching a real profile.

Measured in Phase 0 against the live API: a post containing an unescaped
reserved character returns 201 Created and is then silently truncated at that
character. There is nothing in the response to detect it by, so correctness
here cannot be checked at runtime -- only by these tests.
"""

from __future__ import annotations

import pytest

from app import sanitize

RESERVED = list("|{}@[]()<>#\\*_~")


# ---------------------------------------------------------------------------
# Escaping: every reserved character, individually
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ch", RESERVED)
def test_every_reserved_character_is_escaped(ch):
    assert sanitize.escape_little(f"a{ch}b") == f"a\\{ch}b"


@pytest.mark.parametrize("ch", RESERVED)
def test_no_reserved_character_survives_unescaped(ch):
    """The real invariant: after escaping, every reserved character is preceded
    by a backslash."""
    out = sanitize.escape_little(f"x{ch}y{ch}z")
    for i, c in enumerate(out):
        if c == ch and ch != "\\":
            assert i > 0 and out[i - 1] == "\\", f"{ch!r} unescaped at {i}"


def test_backslash_escapes_itself():
    assert sanitize.escape_little(r"C:\temp") == r"C:\\temp"


def test_escaping_a_realistic_sentence():
    assert sanitize.escape_little("We shipped v2 (finally)") == r"We shipped v2 \(finally\)"


def test_ordinary_punctuation_is_untouched():
    text = "Yes, it works. Really! 50% faster; here's why: speed & focus — 3/4 done?"
    assert sanitize.escape_little(text) == text


def test_emoji_and_accents_survive():
    text = "⚙️ Café résumé 日本語 →"
    assert sanitize.escape_little(text) == text


def test_newlines_are_preserved():
    assert sanitize.escape_little("a\n\nb") == "a\n\nb"


def test_escaping_is_not_idempotent_and_that_is_expected():
    """Escaping twice double-escapes, which is why it happens exactly once, at
    the end of the pipeline."""
    once = sanitize.escape_little("(x)")
    assert sanitize.escape_little(once) != once


# ---------------------------------------------------------------------------
# Markdown, which LinkedIn does not render
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("**bold**", "bold"),
    ("*italic*", "italic"),
    ("__bold__", "bold"),
    ("_italic_", "italic"),
    ("***both***", "both"),
    ("~~struck~~", "struck"),
    ("`code`", "code"),
])
def test_markdown_emphasis_is_removed(raw, expected):
    assert sanitize.strip_markdown(raw) == expected


def test_headings_lose_their_hashes():
    assert sanitize.strip_markdown("## What I learned") == "What I learned"


def test_code_fences_are_removed_but_content_kept():
    out = sanitize.strip_markdown("```python\nx = 1\n```")
    assert "```" not in out
    assert "x = 1" in out


@pytest.mark.parametrize("bullet", ["- ", "* ", "+ "])
def test_markdown_bullets_become_a_plain_character(bullet):
    out = sanitize.strip_markdown(f"{bullet}first\n{bullet}second")
    assert out.startswith("• first")
    assert "• second" in out
    assert not out.startswith(bullet)


def test_numbered_lists_keep_their_numbers():
    assert sanitize.strip_markdown("1. first\n2. second") == "1. first\n2. second"


def test_links_keep_both_the_text_and_the_url():
    assert sanitize.strip_markdown("[my repo](https://github.com/a/b)") == \
        "my repo https://github.com/a/b"


def test_a_bare_url_is_left_alone():
    assert sanitize.strip_markdown("see https://example.com/x") == "see https://example.com/x"


def test_an_asterisk_used_as_punctuation_is_not_treated_as_emphasis():
    """A lone asterisk is not markdown; it must survive to be escaped."""
    assert "*" in sanitize.strip_markdown("rated 5 * out of 5")


def test_ascii_arrows_are_normalised():
    """'->' contains a reserved '>'. A real arrow needs no escaping and reads
    better."""
    assert sanitize.strip_markdown("-> first\n-> second") == "→ first\n→ second"


# ---------------------------------------------------------------------------
# Hashtags
# ---------------------------------------------------------------------------

def test_hashtag_template_shape():
    assert sanitize.hashtag_template("AI") == "{hashtag|\\#|AI}"


def test_hashtag_template_strips_a_leading_hash():
    assert sanitize.hashtag_template("#AI") == sanitize.hashtag_template("AI")


def test_hashtag_template_removes_spaces_and_punctuation():
    """LinkedIn hashtags are a single word; 'Machine Learning' must not become
    two broken tags."""
    assert sanitize.hashtag_template("Machine Learning") == "{hashtag|\\#|MachineLearning}"
    assert sanitize.hashtag_template("Build-In-Public!") == "{hashtag|\\#|BuildInPublic}"


def test_empty_hashtags_are_dropped():
    assert sanitize.build_commentary("body", ["", "  ", "#"]) == "body"


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def test_commentary_escapes_the_body_but_not_the_templates():
    out = sanitize.build_commentary("Done (v2)", ["AI", "MLOps"])
    assert r"Done \(v2\)" in out
    assert "{hashtag|\\#|AI}" in out
    assert "\\{hashtag" not in out, "the template's own braces must not be escaped"


def test_commentary_separates_body_and_hashtags_with_a_blank_line():
    out = sanitize.build_commentary("body", ["AI"])
    assert out == "body\n\n{hashtag|\\#|AI}"


def test_commentary_with_no_hashtags_has_no_trailing_blank_line():
    assert sanitize.build_commentary("body", []) == "body"


def test_full_pipeline_strips_then_escapes():
    """Markdown must be removed before escaping, or its asterisks get escaped
    and published as literal backslash-asterisks."""
    out = sanitize.prepare("**Key point** (important)", ["AI"]).commentary
    assert "Key point" in out
    assert "\\*" not in out
    assert r"\(important\)" in out


# ---------------------------------------------------------------------------
# Length: what the reader sees, not what we send
# ---------------------------------------------------------------------------

def test_rendered_text_is_what_a_reader_sees():
    p = sanitize.prepare("We shipped v2 (finally)", ["AI"])
    assert p.preview == "We shipped v2 (finally)\n\n#AI"
    assert "\\" not in p.preview


def test_escaped_string_is_longer_than_the_preview():
    p = sanitize.prepare("(a) (b) (c) (d)", [])
    assert len(p.commentary) > len(p.preview)


def test_length_counts_the_preview_not_the_escaped_string():
    """Counting the escaped string would reject valid posts at roughly half the
    real budget."""
    p = sanitize.prepare("(x)" * 100, [])
    assert p.length == len(p.preview)
    assert p.length < len(p.commentary)


def test_a_normal_post_is_not_flagged_as_too_long():
    assert not sanitize.prepare("a short post", ["AI"]).too_long


def test_an_over_length_post_is_flagged():
    assert sanitize.prepare("x" * 3100, ["AI"]).too_long


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------

def test_truncation_cuts_at_a_paragraph_boundary():
    body = "\n\n".join(["para one", "para two", "x" * 3000])
    out = sanitize.truncate_body(body, ["AI"], limit=100)
    assert out == "para one\n\npara two"


def test_truncation_falls_back_to_a_sentence_boundary():
    body = "First sentence. Second sentence. " + "x" * 3000
    out = sanitize.truncate_body(body, [], limit=40)
    assert out.endswith(".")
    assert len(out) <= 40


def test_truncation_leaves_room_for_the_hashtags():
    body = "word " * 900
    tags = ["MachineLearning", "MLOps", "ArtificialIntelligence"]
    out = sanitize.truncate_body(body, tags, limit=200)
    assert sanitize.prepare(out, tags).length <= 200


def test_a_short_post_is_returned_unchanged():
    assert sanitize.truncate_body("short", ["AI"], limit=3000) == "short"


# ---------------------------------------------------------------------------
# Real output from the models, which is where this actually gets tested
# ---------------------------------------------------------------------------

GEMINI_DRAFT = """\
We assumed larger chunks meant better context for our RAG pipeline.

We were wrong. Retrieval accuracy tanked and noise skyrocketed.

-> Chunk size matters more than embedding model choice.
-> Overlapping boundaries saved us from cutting context in half.

The takeaway: drop your chunk size before you spend a week fine-tuning your LLM.

What chunk size gave you the best retrieval results?"""

GROQ_DRAFT = """\
We assumed more context always improves RAG—wrong.

The first run dumped 10 k tokens into the prompt. The LLM timed out.

→ Truncate to the top-k most relevant passages using BM25 scores.
→ Cache embeddings for static docs; we were recomputing them each query."""

BUSINESS_DRAFT = """\
⚙️ Corrosion resistance for demanding process lines.

➜ Cast in CF8M stainless steel for maximum chemical durability
➜ Available with ISO 5211 direct mounting pads for actuators

If you are an EPC contractor sourcing valves (corrosive service), let's talk."""


@pytest.mark.parametrize("draft", [GEMINI_DRAFT, GROQ_DRAFT, BUSINESS_DRAFT])
def test_real_model_output_survives_the_round_trip(draft):
    p = sanitize.prepare(draft, ["RAG", "MLOps"])
    # Nothing a reader sees should be lost.
    for line in draft.splitlines():
        stripped = line.replace("->", "→").strip()
        if stripped:
            assert stripped[:20] in p.preview, f"lost: {line[:40]!r}"


@pytest.mark.parametrize("draft", [GEMINI_DRAFT, GROQ_DRAFT, BUSINESS_DRAFT])
def test_no_reserved_character_reaches_linkedin_unescaped(draft):
    """The one that matters. Any miss here truncates a live post."""
    out = sanitize.prepare(draft, ["RAG"]).commentary
    body = out.split("\n\n{hashtag")[0]
    i = 0
    while i < len(body):
        if body[i] == "\\":
            i += 2
            continue
        assert body[i] not in RESERVED, (
            f"unescaped {body[i]!r} at {i}: ...{body[max(0,i-30):i+10]!r}"
        )
        i += 1


def test_the_arrow_bullets_the_models_produce_do_not_need_escaping():
    p = sanitize.prepare(GEMINI_DRAFT, [])
    assert "→" in p.preview
    assert "->" not in p.preview
