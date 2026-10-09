"""Database models -- plan.md section 15.

Conventions used throughout:

* Every timestamp is `timestamptz` and stores UTC. Conversion to the user's
  timezone happens at display time only (plan section 18).
* Enumerated columns are VARCHAR + CHECK rather than native Postgres ENUMs,
  because adding a value to a native enum in a migration is painful and
  `PostStatus` in particular is expected to grow.
* `posts.status` carries the value `publishing`, which is the publish
  idempotency guard (F22): the worker claims a row into that state inside
  `SELECT ... FOR UPDATE SKIP LOCKED` before it ever calls LinkedIn, so a
  restart or a second instance cannot post the same draft twice.
"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class AccountType(str, enum.Enum):
    personal = "personal"
    business = "business"


class PostCategory(str, enum.Enum):
    learning = "learning"
    achievement = "achievement"
    project = "project"
    tips = "tips"
    insight = "insight"
    career = "career"
    event = "event"
    business = "business"


class Tone(str, enum.Enum):
    professional = "professional"
    storytelling = "storytelling"
    direct = "direct"


class PostStatus(str, enum.Enum):
    draft = "draft"
    scheduled = "scheduled"
    publishing = "publishing"   # claimed by the worker; see F22
    posted = "posted"
    failed = "failed"
    dry_run = "dry_run"


class ImageSource(str, enum.Enum):
    pexels = "pexels"
    upload = "upload"
    none = "none"


class ModelUsed(str, enum.Enum):
    gemini = "gemini"
    groq = "groq"


def _enum(e: type[enum.Enum], name: str) -> Enum:
    """VARCHAR + CHECK constraint, storing the value rather than the member name."""
    return Enum(
        e,
        name=name,
        native_enum=False,
        values_callable=lambda x: [m.value for m in x],
        length=32,
    )


_utcnow = func.now()


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Identity from LinkedIn OIDC. The URN is the real identity; email can change.
    linkedin_person_urn: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    email: Mapped[str] = mapped_column(String(320), nullable=False, default="")

    # Fernet ciphertext, never plaintext (plan section 13).
    access_token: Mapped[Optional[str]] = mapped_column(Text)
    token_expires_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    # When LinkedIn last refused this token with a 401. A token can die long
    # before the 60 days are up -- a password change or revoking the app kills
    # it immediately -- and nothing in the expiry date reflects that. Without
    # this the dashboard keeps reporting a healthy connection while every
    # scheduled post fails one at a time. Cleared on reconnect.
    token_rejected_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # Personal context, used to write in the member's voice.
    role_title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    niche: Mapped[str] = mapped_column(String(300), nullable=False, default="")

    account_type: Mapped[AccountType] = mapped_column(
        _enum(AccountType, "account_type"), nullable=False, default=AccountType.personal
    )

    # Business context (F3), unused for personal accounts.
    company_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    products: Mapped[str] = mapped_column(Text, nullable=False, default="")
    target_markets: Mapped[str] = mapped_column(Text, nullable=False, default="")
    buyer_types: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Where the finished brand post gets pasted. Publishing to a company page
    # through the API needs Community Management access we do not have
    # (plan section 2), so F20 hands the text to the user instead -- and a link
    # straight to their page is most of what makes that bearable.
    company_page_url: Mapped[str] = mapped_column(
        String(300), nullable=False, default="", server_default=""
    )

    default_tone: Mapped[Tone] = mapped_column(
        _enum(Tone, "tone"), nullable=False, default=Tone.professional
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")

    # Set when the user completes onboarding. Explicit, rather than inferring
    # "is set up" from whether role_title happens to be non-empty.
    onboarded_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # When we last warned that the LinkedIn token is about to expire. Without
    # this the daily sweep would send the same warning every day for a week,
    # which teaches people to ignore it. Cleared on reconnect.
    expiry_notified_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # F4: soft delete, so a deletion request can be honoured without orphaning rows.
    deleted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_utcnow
    )

    posts: Mapped[list["Post"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_users_deleted_at", "deleted_at"),
    )

    def __repr__(self) -> str:
        return f"<User {self.id} {self.linkedin_person_urn}>"


# ---------------------------------------------------------------------------
# posts
# ---------------------------------------------------------------------------

class Post(Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    topic: Mapped[str] = mapped_column(String(500), nullable=False)
    post_category: Mapped[PostCategory] = mapped_column(
        _enum(PostCategory, "post_category"), nullable=False
    )
    tone: Mapped[Tone] = mapped_column(_enum(Tone, "tone"), nullable=False)

    # Plain text, as the reader will see it -- markdown already stripped, but
    # NOT `little`-escaped. Escaping happens once, at the publish boundary, so
    # there is a single source of truth: the preview renders `content`
    # directly, and publisher.py escapes it on the way out. Storing the escaped
    # form instead would mean un-escaping to display it, and two encodings of
    # the same text is how they drift apart.
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Kept apart from `content` so publishing can build LinkedIn's
    # {hashtag|\#|Tag} templates without parsing them back out of prose.
    hashtags: Mapped[Optional[list]] = mapped_column(JSONB)
    # Kept for v2 (F21). Links live in the body in v1.
    first_comment: Mapped[Optional[str]] = mapped_column(Text)

    edited_by_user: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    quality_score: Mapped[Optional[dict]] = mapped_column(JSONB)

    # All three drafts from the generation, so the user can switch to another
    # one later without spending another model call.
    drafts: Mapped[Optional[list]] = mapped_column(JSONB)
    chosen_draft: Mapped[Optional[int]] = mapped_column(Integer)
    photo_query: Mapped[str] = mapped_column(
        String(200), nullable=False, default="", server_default=""
    )

    account_type: Mapped[AccountType] = mapped_column(
        _enum(AccountType, "account_type"), nullable=False, default=AccountType.personal
    )
    image_source: Mapped[ImageSource] = mapped_column(
        _enum(ImageSource, "image_source"), nullable=False, default=ImageSource.none
    )

    status: Mapped[PostStatus] = mapped_column(
        _enum(PostStatus, "post_status"), nullable=False, default=PostStatus.draft
    )
    scheduled_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    # When a worker moved this row to `publishing`. A row still publishing long
    # after this was interrupted mid-flight (a crash, a deploy) and needs a
    # human decision -- see worker/scheduler.py:recover_stuck.
    claimed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    model_used: Mapped[Optional[ModelUsed]] = mapped_column(_enum(ModelUsed, "model_used"))
    linkedin_post_id: Mapped[Optional[str]] = mapped_column(String(128))

    error_message: Mapped[Optional[str]] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_utcnow
    )
    posted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    # When the user copied this for their company page (F20). Not a status: the
    # app never sees it reach LinkedIn, so claiming "posted" would be a lie.
    copied_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    user: Mapped["User"] = relationship(back_populates="posts")
    images: Mapped[list["PostImage"]] = relationship(
        back_populates="post", cascade="all, delete-orphan", order_by="PostImage.position"
    )
    performance: Mapped[Optional["PostPerformance"]] = relationship(
        back_populates="post", cascade="all, delete-orphan", uselist=False
    )

    __table_args__ = (
        # The worker's only query: due posts for publishing.
        Index("ix_posts_due", "status", "scheduled_at"),
        # Every user-facing listing.
        Index("ix_posts_user_created", "user_id", "created_at"),
        # Duplicate-topic warning (F13). Declared here, not just created in the
        # migration: an index Alembic cannot see in the metadata is an index
        # every future --autogenerate will try to drop.
        Index(
            "ix_posts_topic_trgm",
            "topic",
            postgresql_using="gin",
            postgresql_ops={"topic": "gin_trgm_ops"},
        ),
        # A published post must record which LinkedIn post it became.
        CheckConstraint(
            "status <> 'posted' OR linkedin_post_id IS NOT NULL",
            name="ck_posts_posted_has_linkedin_id",
        ),
        # A scheduled post must have a time.
        CheckConstraint(
            "status <> 'scheduled' OR scheduled_at IS NOT NULL",
            name="ck_posts_scheduled_has_time",
        ),
        CheckConstraint("retry_count >= 0", name="ck_posts_retry_count_non_negative"),
    )

    def __repr__(self) -> str:
        return f"<Post {self.id} {self.status.value} user={self.user_id}>"


# ---------------------------------------------------------------------------
# post_images  (replaces the undefined image_urls column in the original plan)
# ---------------------------------------------------------------------------

class PostImage(Base):
    __tablename__ = "post_images"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("posts.id", ondelete="CASCADE"), nullable=False
    )

    url: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[ImageSource] = mapped_column(_enum(ImageSource, "image_source"), nullable=False)
    # Ordering for v2 multi-image posts; always 0 in v1.
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The URN LinkedIn returns from the Images API, once uploaded.
    linkedin_image_urn: Mapped[Optional[str]] = mapped_column(String(128))

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_utcnow
    )

    post: Mapped["Post"] = relationship(back_populates="images")

    __table_args__ = (
        UniqueConstraint("post_id", "position", name="uq_post_images_position"),
    )


# ---------------------------------------------------------------------------
# post_performance  (F26 -- self-reported, since r_member_social is closed)
# ---------------------------------------------------------------------------

class PostPerformance(Base):
    __tablename__ = "post_performance"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    likes: Mapped[Optional[int]] = mapped_column(Integer)
    comments: Mapped[Optional[int]] = mapped_column(Integer)
    # The 👍/👎 shortcut, for users who won't type numbers.
    sentiment: Mapped[Optional[str]] = mapped_column(String(8))

    reported_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_utcnow
    )

    post: Mapped["Post"] = relationship(back_populates="performance")

    __table_args__ = (
        CheckConstraint("likes IS NULL OR likes >= 0", name="ck_perf_likes_non_negative"),
        CheckConstraint("comments IS NULL OR comments >= 0", name="ck_perf_comments_non_negative"),
        CheckConstraint(
            "sentiment IS NULL OR sentiment IN ('up', 'down')",
            name="ck_perf_sentiment_values",
        ),
    )


# ---------------------------------------------------------------------------
# usage  (daily limits, F27)
# ---------------------------------------------------------------------------

class Usage(Base):
    """One row per user per day.

    The composite primary key is what makes the daily limit safe: two concurrent
    requests cannot both insert a first row for the same day, so the check in
    limits.py cannot be raced.
    """

    __tablename__ = "usage"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)

    posts_generated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rewrites: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        CheckConstraint("posts_generated >= 0", name="ck_usage_posts_non_negative"),
        CheckConstraint("rewrites >= 0", name="ck_usage_rewrites_non_negative"),
    )
