"""The trailing hashtag line a model adds to its own body.

Found while previewing the first real post. The model returned its tags in the
structured field *and* wrote them as the last line of the body -- without the
"#", so it read as four bare words. The app then appended the real hashtag
line, and the post ended with the same four words twice, once as prose and
once as tags. A dry run could not catch it: the payload was valid, just ugly.

The rule is deliberately tight. A line is only dropped when every word on it
is one of the hashtags, because the alternative -- guessing which closing
lines are "tag-like" -- would eventually delete somebody's last sentence.
"""

from __future__ import annotations

import pytest

from app import sanitize

TAGS = ["SoftwareTesting", "QualityAssurance", "ProductDevelopment", "Engineering"]


def test_the_bare_word_line_that_reached_the_preview():
    """The exact body that came back from Gemini."""
    body = ("Testing with one account proves the code compiles.\n\n"
            "How many accounts do you typically use?\n\n"
            "SoftwareTesting QualityAssurance ProductDevelopment Engineering")
    out = sanitize.strip_duplicate_tag_line(body, TAGS)
    assert out.endswith("How many accounts do you typically use?")
    assert "SoftwareTesting" not in out


def test_the_same_line_written_with_hashes():
    body = "A real sentence.\n\n#Alpha #Beta"
    assert sanitize.strip_duplicate_tag_line(body, ["Alpha", "Beta"]) == "A real sentence."


def test_a_mixed_line_is_still_removed():
    body = "A real sentence.\n\n#Alpha Beta"
    assert sanitize.strip_duplicate_tag_line(body, ["Alpha", "Beta"]) == "A real sentence."


def test_case_and_punctuation_differences_still_match():
    body = "A real sentence.\n\nsoftwaretesting ENGINEERING"
    assert sanitize.strip_duplicate_tag_line(body, TAGS) == "A real sentence."


def test_several_tag_lines_are_all_removed():
    body = "A real sentence.\n\n#Alpha\n#Beta\n"
    assert sanitize.strip_duplicate_tag_line(body, ["Alpha", "Beta"]) == "A real sentence."


# --- what must survive ----------------------------------------------------

def test_a_closing_sentence_is_never_touched():
    body = "A real sentence.\n\nWhat would you do differently?"
    assert sanitize.strip_duplicate_tag_line(body, TAGS) == body


def test_a_line_containing_a_word_that_is_not_a_tag_survives():
    """One stray word is enough to make it prose, not a tag line."""
    body = "A real sentence.\n\nSoftwareTesting is hard"
    assert sanitize.strip_duplicate_tag_line(body, TAGS) == body


def test_a_line_that_merely_mentions_one_tag_survives():
    body = "A real sentence.\n\nEngineering, honestly, is the easy part."
    assert sanitize.strip_duplicate_tag_line(body, TAGS) == body


def test_no_hashtags_means_nothing_is_removed():
    """With no tags of our own there is no duplication to prevent, and the
    body's own line is the only one the reader will get."""
    body = "A real sentence.\n\n#Alpha #Beta"
    assert sanitize.strip_duplicate_tag_line(body, []) == body


def test_a_body_that_is_only_a_tag_line_does_not_crash():
    assert sanitize.strip_duplicate_tag_line("#Alpha #Beta", ["Alpha", "Beta"]) == ""


@pytest.mark.parametrize("body", ["", "   ", "\n\n"])
def test_empty_bodies_are_safe(body):
    assert sanitize.strip_duplicate_tag_line(body, TAGS) == ""


# --- through the real entry point -----------------------------------------

def test_prepare_publishes_the_tags_exactly_once():
    body = ("Testing with one account proves the code compiles.\n\n"
            "SoftwareTesting QualityAssurance ProductDevelopment Engineering")
    prep = sanitize.prepare(body, TAGS)

    for tag in TAGS:
        assert prep.preview.count(tag) == 1, f"{tag} appears twice in the preview"
        assert prep.commentary.count(tag) == 1, f"{tag} appears twice in the payload"
    # And they are real hashtags, not bare words.
    assert "{hashtag|\\#|SoftwareTesting}" in prep.commentary


def test_prepare_still_counts_the_shorter_body():
    """The length that decides truncation must reflect the removal."""
    body = "Sentence.\n\nSoftwareTesting QualityAssurance ProductDevelopment Engineering"
    with_dupe = sanitize.prepare(body, TAGS)
    without = sanitize.prepare("Sentence.", TAGS)
    assert with_dupe.length == without.length
