"""Turn a finished post into the card that suits it.

The card form asked for a headline, an eyebrow, four features and a picture
subject. All of that is already in the post -- making someone retype it is
asking for work the system has the material to do.

So this reads the post and returns a card, including *which* card: there are
six arrangements and they are not interchangeable. A list of claims cannot say
"one crop, two outputs", and a comparison forced into a feature list
misrepresents what was written. Picking the shape is part of writing it.

It borrows llm._call_model, the Gemini-then-Groq path drafting already uses,
and leaves the drafting prompt, its models and its settings alone: a bad card
should never be able to spoil a good draft.

It is not designing. Layout, palette and type belong to postcard.py, where
they are drawn and therefore correct. This only decides what the words are and
which arrangement holds them.
"""

from __future__ import annotations

import logging

from app import llm, postcard

log = logging.getLogger(__name__)

ICONS = postcard.ICON_NAMES
MAX_FEATURES = 4

# Layouts the writer may choose, with what each is for. Kept here rather than
# in the prompt string so adding one is a single edit.
_LAYOUTS = {
    "feature": "a product or idea with several qualities worth listing",
    "split": "one thing explained, with a few supporting points",
    "compare": "two things set against each other -- only when the post really "
               "does contrast a pair",
    "range": "the breadth of what is offered rather than one item",
    "spotlight": "a single product, named, with two or three short claims",
    "product": "a named product with its specification -- the most catalogue-like",
}


class CardWriteError(Exception):
    """The model could not be reached, or sent back nothing usable."""


_PROMPT = """You are writing a promotional card to sit beside a LinkedIn post. \
The card is drawn from a template, so give short phrases that fit fixed \
spaces -- not sentences, and no markdown.

The post:
---
{post}
---

First choose the arrangement that fits what the post actually says:
{layouts}

Then return only this JSON object:

{{
  "layout": "one of the names above",
  "eyebrow": "two or three words in capitals naming the kind of card -- \
PRODUCT SPOTLIGHT, MATERIAL EXPLAINED, CASE STUDY. Empty string if none fits.",
  "headline": ["first line", "second line"],
  "subhead": "one line of at most 60 characters",
  "body": "one or two sentences, at most 150 characters, expanding the headline",
  "features": [
    {{"label": "2-4 words", "detail": "at most 40 characters", "icon": "one of: {icons}"}}
  ],
  "columns": [
    {{"label": "the thing being compared, 1-3 words", "detail": "at most 45 characters"}}
  ],
  "badges": [{{"label": "1-3 words", "icon": "one of: {icons}"}}],
  "tagline": "a short brand line, at most 50 characters",
  "photo_subject": "what a photograph on this card should show"
}}

Rules:
- headline is exactly two lines, each at most 24 characters. Write it in \
sentence case, not capitals -- the template capitalises where it needs to. The \
second line is printed in the accent colour, so put the part worth emphasising \
there.
- {max_features} features, each a distinct concrete claim, never a restatement \
of the headline. For "spotlight" give three; for "compare" give none.
- columns: exactly two, and only when layout is "compare". Otherwise an empty list.
- badges: three to five very short reassurances, and only when layout is \
"feature". Otherwise an empty list.
- Choose each icon from the list for what it depicts, not at random.
- photo_subject must be a concrete physical subject a camera could photograph, \
4-10 words. Never a chart, a screen, a logo, or anything with writing in it.
"""


def _prompt_for(post_text: str) -> str:
    return _PROMPT.format(
        post=post_text.strip()[:2000],
        layouts="\n".join(f'  "{k}" -- {v}' for k, v in _LAYOUTS.items()),
        icons=", ".join(ICONS),
        max_features=MAX_FEATURES,
    )


def _clean(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _features(raw: object, limit: int) -> list[postcard.Feature]:
    out: list[postcard.Feature] = []
    for item in (raw or [])[:limit] if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        label = _clean(item.get("label"), 30)
        if not label:
            continue
        icon = str(item.get("icon") or "").strip().lower()
        out.append(postcard.Feature(
            label=label,
            detail=_clean(item.get("detail"), 46),
            icon=icon if icon in ICONS else "leaf",
        ))
    return out


def write(post_text: str) -> tuple[postcard.Card, str]:
    """Read a post and return the card for it, plus the picture subject.

    Raises CardWriteError when the model cannot be reached or sends back
    something with no headline: a card whose headline the model invented from
    nothing is worse than no card.
    """
    if not post_text.strip():
        raise CardWriteError("There is no post text to build a card from yet.")

    try:
        reply, _model = llm._call_model(_prompt_for(post_text))
    except Exception as exc:
        raise CardWriteError(f"Could not reach the writing model: {exc}") from exc

    try:
        data = llm._extract_json(reply)
    except Exception as exc:
        raise CardWriteError("The model's reply could not be read as a card.") from exc

    lines = data.get("headline") or []
    if isinstance(lines, str):
        lines = [lines]
    lines = [_clean(line, 30) for line in lines if _clean(line, 30)][:2]
    if not lines:
        raise CardWriteError("The model returned a card with no headline.")

    # The second line carries the accent colour; postcard.py reads the marker.
    headline = "\n".join(line if i == 0 else f"|{line}" for i, line in enumerate(lines))

    layout = str(data.get("layout") or "").strip().lower()
    if layout not in _LAYOUTS:
        log.info("card writer returned unknown layout %r, using feature", layout)
        layout = "feature"

    columns = _features(data.get("columns"), 2)
    card = postcard.Card(
        layout=layout,
        headline=headline,
        eyebrow=_clean(data.get("eyebrow"), 26).upper(),
        subhead=_clean(data.get("subhead"), 70),
        body=_clean(data.get("body"), 170),
        features=_features(data.get("features"), MAX_FEATURES),
        columns=columns,
        badges=_features(data.get("badges"), 5),
        tagline=_clean(data.get("tagline"), 56),
    )

    # A comparison with only one side is not a comparison. Rather than draw a
    # half-empty card, fall back to the arrangement that can carry what we do
    # have.
    if card.layout == "compare" and len(columns) < 2:
        log.info("compare chosen without two columns, using feature instead")
        card.layout = "feature"

    return card, _clean(data.get("photo_subject"), 120)
