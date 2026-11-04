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
import re

from app import llm, postcard

log = logging.getLogger(__name__)

ICONS = postcard.ICON_NAMES
MAX_FEATURES = 4

# Layouts the writer may choose, with what each is for. Kept here rather than
# in the prompt string so adding one is a single edit.
#
# Described in the brand brief's own words. It names four styles to rotate
# between -- Product Spotlight, Material Explained, Product in Use and
# Export-Ready Catalogue -- and choosing in that vocabulary rather than ours
# means the rotation it asks for is the rotation the model actually makes.
_LAYOUTS = {
    "spotlight": "Product Spotlight -- one product, named, with two or three "
                 "short benefit labels. For product promotion and buyer enquiries.",
    "product": "Product Spotlight in catalogue form -- a named product with its "
               "specification and four benefits.",
    "feature": "Material Explained -- how something is made, or why it behaves "
               "as it does, with several supporting points. For credibility and "
               "product education.",
    "split": "Product in Use -- one thing shown in its real setting, with a few "
             "supporting points. For caterers, kitchens and distributors.",
    "range": "Export-Ready Catalogue -- the breadth of what is offered rather "
             "than any one item. For importers and wholesale buyers.",
    "compare": "Two things set against each other -- only when the post really "
               "does contrast a pair.",
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
- Three or four features, each a distinct point drawn from the post, never a \
restatement of the headline. For "spotlight" give three; for "compare" give none.

NEVER INVENT ANYTHING. This rule outranks the rest, because the card goes out \
under a real company's name to buyers who may act on it. Every feature, badge, \
subtitle and column must be supported by something the post actually says. Do \
not state certifications, environmental claims, food-safety approvals, test \
results, tolerances, percentages, export markets, customer names, prices, \
capacity, or any product specification unless the post states it. Do not call \
anything compostable, biodegradable, microwave-safe, leakproof or suitable for \
hot food unless the post does. If the post does not give you enough for four \
features, give two or three. A short card is correct; an invented one is not.
- columns: exactly two, and only when layout is "compare". Otherwise an empty list.
- badges: three to five very short reassurances, and only when layout is \
"feature". Otherwise an empty list.
- Choose each icon from the list for what it depicts, not at random.
- Keep the voice plain and commercial: a buyer skim-reading on a phone should \
take one clear message away.
- photo_subject must be a concrete physical subject a camera could photograph, \
4-10 words, and must be the real subject of the post -- never a different \
product that merely resembles it. Never a chart, a screen, a logo, or anything \
with writing in it.
- Do not put an exact count or dimension in photo_subject -- "six compartments", \
"9 inch" -- unless the post states it, and prefer leaving it out even then. A \
generated photograph will not reliably show the number you asked for, and a \
picture that contradicts the specification printed beside it is worse than a \
generic one.
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


# Claims a buyer could act on, and which a model will cheerfully invent. The
# brand brief forbids all of them unless the post itself makes the claim, and
# asking the prompt nicely does not work: told in capitals never to invent a
# certification, it returned "Certified organic" in the same reply. So the
# output is checked against the post rather than trusted.
_REGULATED = (
    "certified", "certification", "approved", "compliant", "compostable",
    "biodegradable", "recyclable", "microwave", "leakproof", "leak proof",
    "leak-resistant", "food-safe", "food safe", "fda", "iso", "bpa", "pfas",
    "organic", "non-toxic", "toxin free", "guaranteed", "guarantees",
    "tested", "lab", "patent", "award",
)


def _unsupported(text: str, source: str) -> str | None:
    """The first regulated word in `text` that nothing supports."""
    from app import catalogue

    low, src = text.lower(), source.lower()
    # A certification the company actually holds is reporting, not inventing,
    # so naming one from the catalogue is allowed wherever it appears.
    held = {c.lower() for c in catalogue.CERTIFICATIONS}
    names_a_real_cert = any(c in low for c in held)
    # "ISO certified" tripped on "certified" even though ISO is genuinely
    # held. Naming a real certification brings its own vocabulary with it;
    # what must stay blocked is the word on its own, asserting an approval
    # nobody can point at.
    _carried = {"certified", "certification", "approved", "compliant"}

    for word in _REGULATED:
        if word not in low or word in src or word in held:
            continue
        if names_a_real_cert and word in _carried:
            continue
        return word
    # A figure nobody stated is an invented specification too.
    for token in re.findall(r"\d+(?:\.\d+)?\s*(?:%|percent|kg|mm|cm|days?|hours?)", low):
        if token.split()[0] not in src:
            return token
    return None


def _vet(items: list[postcard.Feature], source: str) -> list[postcard.Feature]:
    """Drop anything claiming something the post never said.

    Dropping is the right failure: a card with two honest points is usable,
    and one with four where a third is invented is a liability.
    """
    kept: list[postcard.Feature] = []
    for item in items:
        bad = _unsupported(f"{item.label} {item.detail}", source)
        if bad:
            log.info("dropped unsupported card claim %r (%r not in the post)",
                     item.label, bad)
            continue
        kept.append(item)
    return kept


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
        subhead=("" if _unsupported(_clean(data.get("subhead"), 70), post_text)
                 else _clean(data.get("subhead"), 70)),
        # Vetted like the rest. It was not, and a card went out reading
        # "Certified biodegradable materials decompose fully" -- the whole
        # paragraph unchecked while every two-word feature beside it was
        # being held to the post.
        body=("" if _unsupported(_clean(data.get("body"), 170), post_text)
              else _clean(data.get("body"), 170)),
        features=_vet(_features(data.get("features"), MAX_FEATURES), post_text),
        columns=_vet(columns, post_text),
        badges=_vet(_features(data.get("badges"), 5), post_text),
        tagline=_clean(data.get("tagline"), 56),
    )

    # A comparison with only one side is not a comparison. Rather than draw a
    # half-empty card, fall back to the arrangement that can carry what we do
    # have.
    if card.layout == "compare" and len(columns) < 2:
        log.info("compare chosen without two columns, using feature instead")
        card.layout = "feature"

    return card, _clean(data.get("photo_subject"), 120)
