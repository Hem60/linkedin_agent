"""LinkedIn's `little` text format -- plan.md section 9.

The Posts API `commentary` field is not plain text. It reserves fifteen
characters, and LinkedIn's rule is absolute:

    "All reserved characters need to be escaped with a backslash, even if
     those characters are not used in one of the supported elements or
     templates."

Phase 0 measured what happens when they are not. Sending

    This line contains reserved characters: (parentheses), an *asterisk*...

returned **201 Created**, and the published post read:

    This line contains reserved characters:

Everything from the first "(" was silently discarded. No error, no warning,
nothing in the response to detect it by. A truncated post looks like a
successful post to the application and like a half-finished thought to everyone
reading it under a real name.

So the order here is not decorative:

    strip markdown  ->  normalise arrows  ->  escape  ->  append hashtag templates

Escaping happens exactly once, at the end. Markdown must go first, or its
asterisks get escaped and published as literal backslash-asterisks. Hashtag
templates are appended after escaping, because their own braces and hash are
syntax rather than text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/little-text-format
RESERVED = frozenset("|{}@[]()<>#\\*_~")

MAX_LENGTH = 3000        # LinkedIn's limit, counted as the reader sees it
BULLET = "•"
ARROW = "→"


# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------

def escape_little(text: str) -> str:
    """Backslash-escape every reserved character.

    Iterating per character means the backslash escapes itself correctly with
    no ordering trickery -- a `str.replace` chain would double-escape whatever
    it handled first.
    """
    return "".join("\\" + c if c in RESERVED else c for c in text)


def unescape_little(text: str) -> str:
    """Inverse of `escape_little`, for showing the user what they will see."""
    out = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text) and text[i + 1] in RESERVED:
            out.append(text[i + 1])
            i += 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Markdown, which LinkedIn does not render
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"^[ \t]*```[^\n]*\n?|^[ \t]*```[ \t]*$", re.MULTILINE)
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.MULTILINE)
_BULLET = re.compile(r"^[ \t]*[-*+][ \t]+", re.MULTILINE)
_LINK = re.compile(r"\[([^\]\n]*)\]\((\S+?)\)")
# Emphasis only when the markers hug the text: "5 * out of 5" is punctuation.
_STRIKE = re.compile(r"~~(\S(?:[^\n]*\S)?)~~")
_STRONG = re.compile(r"(\*{1,3}|_{1,3})(\S(?:[^\n]*?\S)?)\1")
_CODE = re.compile(r"`([^`\n]+)`")
_ASCII_ARROW = re.compile(r"(?<![-<])[-=]{1,2}>")


def strip_markdown(text: str) -> str:
    """Remove markdown a model emitted despite being told not to.

    Runs before escaping. Anything left behind here becomes a literal
    backslash-asterisk in the published post.
    """
    out = _FENCE.sub("", text)
    out = _CODE.sub(r"\1", out)
    out = _LINK.sub(lambda m: f"{m.group(1)} {m.group(2)}".strip(), out)
    out = _STRIKE.sub(r"\1", out)
    out = _STRONG.sub(r"\2", out)
    out = _HEADING.sub("", out)
    out = _BULLET.sub(f"{BULLET} ", out)
    # "->" carries a reserved ">"; a real arrow needs no escaping and reads better.
    out = _ASCII_ARROW.sub(ARROW, out)
    # Collapse the blank-line runs that fence removal can leave behind.
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


# ---------------------------------------------------------------------------
# Hashtags
# ---------------------------------------------------------------------------

_TAG_CHARS = re.compile(r"[^0-9A-Za-z]")


def normalise_tag(tag: str) -> str:
    """LinkedIn hashtags are one word. 'Machine Learning' must not become two
    broken tags, and punctuation must not leak into the template.

    Alphanumerics only. Underscore is dropped rather than kept: it is a
    reserved `little` character, and the text inside a hashtag template is not
    escaped, so an underscore there is syntax of uncertain meaning. Not worth
    the risk for a character nobody misses in a hashtag.
    """
    return _TAG_CHARS.sub("", tag.lstrip("#"))


def hashtag_template(tag: str) -> str:
    """A real, clickable hashtag.

    A literal '#AI' escapes to '\\#AI' and renders as inert text. Only this
    template produces a hashtag LinkedIn will link and index.
    """
    return "{hashtag|\\#|" + normalise_tag(tag) + "}"


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def build_commentary(body: str, hashtags: list[str] | tuple[str, ...] = ()) -> str:
    """The exact string to send as `commentary`. Assumes markdown is gone."""
    out = escape_little(body)
    tags = [hashtag_template(t) for t in hashtags if normalise_tag(t)]
    return f"{out}\n\n{' '.join(tags)}" if tags else out


def rendered_text(body: str, hashtags: list[str] | tuple[str, ...] = ()) -> str:
    """What a reader sees. Backslashes are encoding, not visible text."""
    tags = ["#" + normalise_tag(t) for t in hashtags if normalise_tag(t)]
    return f"{body}\n\n{' '.join(tags)}" if tags else body


@dataclass(frozen=True)
class Prepared:
    commentary: str   # escaped, ready for POST /rest/posts
    preview: str      # what the reader sees, for the UI
    length: int       # preview length, against LinkedIn's 3000
    too_long: bool

    @property
    def remaining(self) -> int:
        return MAX_LENGTH - self.length


def prepare(body: str, hashtags: list[str] | tuple[str, ...] = ()) -> Prepared:
    """Take a model's draft to something publishable, and to something showable.

    The UI shows `preview`; the API receives `commentary`. They are the same
    text in two encodings, and confusing them is how posts break.
    """
    clean = strip_markdown(body)
    preview = rendered_text(clean, hashtags)
    return Prepared(
        commentary=build_commentary(clean, hashtags),
        preview=preview,
        length=len(preview),
        too_long=len(preview) > MAX_LENGTH,
    )


# ---------------------------------------------------------------------------
# Trimming
# ---------------------------------------------------------------------------

def truncate_body(
    body: str,
    hashtags: list[str] | tuple[str, ...] = (),
    limit: int = MAX_LENGTH,
) -> str:
    """Cut an over-long body at the cleanest available boundary.

    Last resort only: the model is asked to shorten first. Cutting mid-sentence
    under someone's real name is worse than a slightly shorter post, so this
    prefers a paragraph break, then a sentence, and only then a hard cut.
    """
    clean = strip_markdown(body)
    if len(rendered_text(clean, hashtags)) <= limit:
        return clean

    tag_cost = len(rendered_text("", hashtags))
    budget = limit - tag_cost
    if budget <= 0:
        return ""

    paragraphs = clean.split("\n\n")
    kept: list[str] = []
    for para in paragraphs:
        candidate = "\n\n".join(kept + [para])
        if len(candidate) > budget:
            break
        kept.append(para)
    if kept:
        return "\n\n".join(kept).strip()

    # One long paragraph: fall back to a sentence boundary.
    head = clean[:budget]
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    if cut > budget // 3:
        return head[: cut + 1].strip()
    return head.rstrip()
