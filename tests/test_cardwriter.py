"""What a card is allowed to say.

The card goes out under a real company's name to buyers who may act on it, so
every claim on it has to be supported by the post it was written from. The
prompt says so in capitals and the model ignores it -- told never to invent a
certification it returned "Certified organic" in the same reply -- which is
why the check is in code and why these tests exist.
"""

from __future__ import annotations

from app import catalogue
from app.cardwriter import _unsupported, _vet
from app.postcard import Feature

POST = ("Our bagasse clamshells are biodegradable and ship worldwide "
        "from Gujarat.")


# ---------------------------------------------------------------------------
# Claims nothing supports
# ---------------------------------------------------------------------------

def test_a_certification_the_post_never_claimed_is_caught():
    assert _unsupported("Certified food-safe", POST) == "certified"


def test_a_claim_the_post_does_make_is_allowed_through():
    assert _unsupported("Fully biodegradable", POST) is None


def test_a_certification_the_company_holds_may_be_named():
    """Naming one from the catalogue is reporting, not inventing, and it
    brings its own vocabulary -- "ISO certified" must not trip on
    "certified"."""
    assert "ISO" in catalogue.CERTIFICATIONS
    assert _unsupported("ISO certified", POST) is None


def test_a_figure_nobody_stated_is_an_invented_specification():
    assert _unsupported("Decomposes in 90 days", POST) == "90 days"


def test_unsupported_features_are_dropped_rather_than_softened():
    """Two honest points are usable; four with an invented third are a
    liability."""
    kept = _vet([Feature("Fully biodegradable"),
                 Feature("Lab tested"),
                 Feature("Ships worldwide")], POST)
    assert [f.label for f in kept] == ["Fully biodegradable", "Ships worldwide"]


# ---------------------------------------------------------------------------
# The body
#
# This went unchecked while every two-word feature beside it was being held to
# the post, and a card went out reading "Certified biodegradable materials
# decompose fully". The paragraph is the longest thing on the card and the
# most likely to be believed.
# ---------------------------------------------------------------------------

def test_the_body_paragraph_is_checked_like_everything_else():
    assert _unsupported(
        "Certified biodegradable materials decompose fully without residues",
        POST) == "certified"


def test_an_honest_body_paragraph_survives():
    assert _unsupported(
        "Biodegradable materials decompose fully without residues",
        POST) is None
