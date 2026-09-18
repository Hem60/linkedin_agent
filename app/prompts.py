"""Prompt construction -- plan.md sections 7, 8 and 10.

One call produces everything: three drafts, each with hashtags, a photo phrase
and its own score. The original design used separate calls for scoring and
safety, which cost four to seven requests per post against a shared free tier.

The model is never asked to produce LinkedIn's `little` escaping or hashtag
templates. It writes ordinary text; `sanitize.py` (phase 6) handles the
encoding. Asking a model to emit backslash-escaped punctuation reliably is a
losing bet, and getting it wrong truncates the post silently (section 9).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models import AccountType, PostCategory, Tone, User

# ---------------------------------------------------------------------------
# Shared style
# ---------------------------------------------------------------------------

STYLE_RULES = """\
How to write:
- Open with a hook under 12 words. No throat-clearing, no "In today's world".
- Paragraphs of one or two lines, blank line between them.
- Three or four concrete, practical points. Specifics beat adjectives.
- Professional but human. No buzzwords, no "game-changer", no "excited to share".
- Close with a genuine question that invites a reply.
- Do not use markdown. No **bold**, no headings, no bullet characters like "-" or "*".
  For a list, start the line with an arrow character.
- Do not invent facts, numbers, company names or outcomes the writer did not give you.
  If the topic is thin, write a shorter post rather than padding it with invention.
- Never mention that this was written with AI."""

TONES = {
    Tone.professional: (
        "Measured and credible. Plain sentences, concrete nouns, no slang. "
        "The voice of someone respected in their field explaining something clearly."
    ),
    Tone.storytelling: (
        "Start in the middle of a specific moment and let the lesson emerge. "
        "Short sentences. A turn partway through. Earned, not sentimental."
    ),
    Tone.direct: (
        "Blunt and economical. Short declaratives. Cut every qualifier. "
        "Say the useful thing first and stop when it is said."
    ),
}


@dataclass(frozen=True)
class PostTypeSpec:
    label: str
    structure: str
    hook: str
    hashtags: str
    question: str | None   # the one extra question this type asks (F8)


POST_TYPES: dict[PostCategory, PostTypeSpec] = {
    PostCategory.learning: PostTypeSpec(
        label="Learning",
        structure=(
            "What you tried, what went wrong, what you learned, the takeaway a reader "
            "can use tomorrow."
        ),
        hook="Name the failure or the wrong assumption in the first line.",
        hashtags="Skill and topic tags for the field involved.",
        question=None,
    ),
    PostCategory.achievement: PostTypeSpec(
        label="Achievement",
        structure=(
            "The result, the effort behind it, credit to the people who helped, thanks. "
            "Understate rather than celebrate."
        ),
        hook="State the result plainly. No 'thrilled to announce'.",
        hashtags="Field and milestone tags.",
        question="What made it hard?",
    ),
    PostCategory.project: PostTypeSpec(
        label="Project announcement",
        structure=(
            "The problem it solves, what you built, the tech used, what is next, then "
            "the link on its own line at the end."
        ),
        hook="Lead with the problem, not the tool.",
        hashtags="Technology and building-in-public tags.",
        question="Repo or demo link?",
    ),
    PostCategory.tips: PostTypeSpec(
        label="Tips or how-to",
        structure=(
            "Two lines of context, then three or four numbered practical points, then "
            "one line on what to do first."
        ),
        hook="Name the specific problem the tips solve.",
        hashtags="Topic and skill tags.",
        question=None,
    ),
    PostCategory.insight: PostTypeSpec(
        label="Industry insight",
        structure=(
            "A trend or opinion, your reasoning, what it means for the reader's work. "
            "Take an actual position."
        ),
        hook="State the opinion, not the topic.",
        hashtags="Industry and trend tags.",
        question=None,
    ),
    PostCategory.career: PostTypeSpec(
        label="Career update",
        structure=(
            "The news in one line, a short honest reflection, thanks to people involved. "
            "Keep it short."
        ),
        hook="The news itself, stated simply.",
        hashtags="Role, field and company tags.",
        question="Which company or course?",
    ),
    PostCategory.event: PostTypeSpec(
        label="Event recap",
        structure=(
            "Where you were, what you did there, the two or three takeaways worth "
            "passing on, who you met."
        ),
        hook="The most surprising thing you heard.",
        hashtags="Event and industry tags.",
        question="Which event?",
    ),
    PostCategory.business: PostTypeSpec(
        label="Business or brand post",
        structure=(
            "An emoji tagline hook, one or two lines on the problem or market, a "
            "paragraph on the product, a feature list with emoji bullets, a line about "
            "the company, a call to action naming the buyers, and a contact invite."
        ),
        hook="A short emoji-led line about the product promise.",
        hashtags=(
            "Mix brand, product, industry, country and buyer tags. "
            "Eight to ten — no more. Thirty hashtags reduces reach on LinkedIn "
            "rather than increasing it."
        ),
        question="Which product is this promoting?",
    ),
}

JSON_CONTRACT = """\
Reply with JSON only. No prose before or after, no code fences.

{
  "drafts": [
    {
      "body": "the post text, plain, newlines between paragraphs",
      "hashtags": ["Tag", "Tag", "Tag"],
      "photo_query": "two or three words for a stock photo search",
      "score": {"hook": 8, "clarity": 7, "readability": 9},
      "improvement": "one sentence on the weakest part of this draft"
    }
  ]
}

Exactly three drafts. They must differ in approach, not merely in wording --
different hook, different angle, different structure within the type.
Hashtags without the "#" symbol. Scores are your honest assessment out of 10,
so vary them; three identical scores tell the writer nothing.
"""


def _writer_context(user: User) -> str:
    bits = []
    if user.role_title:
        bits.append(f"Their role: {user.role_title}.")
    if user.niche:
        bits.append(f"They post about: {user.niche}.")
    if user.account_type is AccountType.business:
        if user.company_name:
            bits.append(f"Company: {user.company_name}.")
        if user.products:
            bits.append(f"What it sells: {user.products}.")
        if user.target_markets:
            bits.append(f"Target markets: {user.target_markets}.")
        if user.buyer_types:
            bits.append(f"Buyers to reach: {user.buyer_types}.")
    return " ".join(bits) or "No profile details given -- keep it general."


def hashtag_range(user: User, category: PostCategory) -> tuple[int, int]:
    """3-5 personal, 8-10 business. The original plan allowed up to 30, which
    suppresses reach rather than extending it."""
    if category is PostCategory.business or user.account_type is AccountType.business:
        return (8, 10)
    return (3, 5)


def build_prompt(
    user: User,
    topic: str,
    category: PostCategory,
    tone: Tone,
    extra_answer: str = "",
) -> str:
    spec = POST_TYPES[category]
    lo, hi = hashtag_range(user, category)

    extra = ""
    if spec.question and extra_answer.strip():
        extra = f'\nThey answered "{spec.question}" with: {extra_answer.strip()}\n'
    elif spec.question:
        extra = (
            f'\nThey skipped the question "{spec.question}". Write the post without '
            "those details. Do not invent them.\n"
        )

    return f"""\
You write LinkedIn posts for one person, in their voice.

{_writer_context(user)}

Topic they gave you: {topic.strip()}

Post type: {spec.label}
Structure: {spec.structure}
Hook: {spec.hook}
{extra}
Tone: {TONES[tone]}

{STYLE_RULES}

Hashtags: {lo} to {hi} of them. {spec.hashtags}

Length: whatever the topic deserves. Under 3000 characters including hashtags.
A tight 700-character post beats a padded 2000-character one.

{JSON_CONTRACT}"""
