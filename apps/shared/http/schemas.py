"""Схемы запросов и ответов API v1 — единый контракт для всех архитектур (OpenAPI)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _not_blank(v: str) -> str:
    if not v.strip():
        raise ValueError("не может быть пустым")
    return v


# ── auth ────────────────────────────────────────────────────────────────────────────────
class RegisterIn(_In):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=100)
    role: Literal["patient", "doctor"] = "patient"


class LoginIn(_In):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshIn(_In):
    refresh_token: str = Field(min_length=20, max_length=200)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class UserBrief(BaseModel):
    id: int
    role: str
    email: str | None = None


class AuthOut(TokenPair):
    user: UserBrief


# ── profiles ────────────────────────────────────────────────────────────────────────────
class ProfileOut(BaseModel):
    id: int
    role: str
    display_name: str
    bio: str
    city: str
    specialty_id: int | None
    specialty_name: str | None
    member_since: str
    birth_year: int | None = None
    updated_at: str | None = None


class ProfileUpdateIn(_In):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    bio: str | None = Field(default=None, max_length=2000)
    city: str | None = Field(default=None, max_length=100)
    birth_year: int | None = Field(default=None, ge=1900, le=2100)
    specialty_id: int | None = Field(default=None, ge=1, le=32767)


# ── content ─────────────────────────────────────────────────────────────────────────────
class CategoryOut(BaseModel):
    id: int
    slug: str
    name: str


class PostIn(_In):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=20000)
    category_id: int = Field(ge=1, le=32767)

    _blank = field_validator("title", "body")(_not_blank)


class CommentIn(_In):
    body: str = Field(min_length=1, max_length=5000)

    _blank = field_validator("body")(_not_blank)


class ReactionIn(_In):
    kind: Literal["like", "support", "useful"]


class DecisionIn(_In):
    decision: Literal["approve", "block"]


class ModerationOut(BaseModel):
    label: str
    level: int
    confidence: float
    reasons: list[str]


class PostCreated(BaseModel):
    id: int
    status: str
    moderation: ModerationOut


class CommentOut(BaseModel):
    id: int
    body: str
    created_at: str
    author_id: int
    author_name: str


class CommentCreated(BaseModel):
    id: int
    status: str


class PostOut(BaseModel):
    id: int
    title: str
    body: str
    status: str
    category_id: int
    category_name: str
    comments_count: int
    reactions_count: int
    created_at: str
    author_id: int
    author_name: str
    author_role: str


class PostDetail(PostOut):
    comments: list[CommentOut]
    my_reaction: str | None


class SearchHit(PostOut):
    rank: float


class ReactionOut(BaseModel):
    post_id: int
    kind: str
    created: bool
    reactions_count: int


class DecisionOut(BaseModel):
    id: int
    status: str


# ── stats ───────────────────────────────────────────────────────────────────────────────
class DayCount(BaseModel):
    day: str
    status: str
    count: int


class TopCategory(BaseModel):
    category_id: int
    name: str
    posts: int


class StatsOut(BaseModel):
    window_days: int
    posts_total: int
    posts_by_day: list[DayCount]
    comments_total: int
    reactions_total: int
    new_users: int
    top_categories: list[TopCategory]


# ── internal / service ──────────────────────────────────────────────────────────────────
class ModerateIn(_In):
    text: str = Field(min_length=1, max_length=25000)


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str | None = None


class ErrorOut(BaseModel):
    error: ErrorBody
