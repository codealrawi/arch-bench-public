"""Генератор seed: детерминированность, диапазоны, согласованность связей и счётчиков."""

from __future__ import annotations

import json
from collections import Counter

import pytest

from apps.shared.auth import verify_password
from apps.shared.moderation import ContentModerator
from database.seed.generator import (
    CATEGORIES,
    SECONDS_PER_DAY,
    generate,
    generate_load_texts,
    user_email,
    user_password,
)
from tests.fixtures.seed_params import PASSWORD_TEMPLATE, small


@pytest.fixture(scope="module")
def moderator() -> ContentModerator:
    return ContentModerator().train()


@pytest.fixture(scope="module")
def data(moderator: ContentModerator):  # type: ignore[no-untyped-def]
    return generate(small(), moderator)


def test_deterministic(moderator: ContentModerator, data) -> None:  # type: ignore[no-untyped-def]
    again = generate(small(), moderator)
    for name in ("users", "profiles", "posts", "comments", "reactions", "moderation"):
        assert getattr(again, name) == getattr(data, name), name


def test_different_seed_differs(moderator: ContentModerator, data) -> None:  # type: ignore[no-untyped-def]
    other = generate(small(seed=12), moderator)
    assert [p.title for p in other.posts] != [p.title for p in data.posts]


def test_counts_match_parameters(data) -> None:  # type: ignore[no-untyped-def]
    p = small()
    assert (
        len(data.users),
        len(data.profiles),
        len(data.posts),
        len(data.comments),
        len(data.reactions),
    ) == (p.users, p.users, p.posts, p.comments, p.reactions)


def test_users_roles_and_passwords(data) -> None:  # type: ignore[no-untyped-def]
    roles = [u.role for u in data.users]
    assert roles[0] == "admin" and roles[1:3] == ["moderator", "moderator"]
    assert set(roles[3:]) <= {"doctor", "patient"}
    u = data.users[9]
    assert u.email == user_email(10)
    assert verify_password(user_password(PASSWORD_TEMPLATE, 10), u.password_hash)
    assert len({u.email for u in data.users}) == len(data.users)


def test_profiles_specialty_only_for_doctors(data) -> None:  # type: ignore[no-untyped-def]
    roles = {u.id: u.role for u in data.users}
    for pr in data.profiles:
        if pr.specialty_id is not None:
            assert roles[pr.user_id] == "doctor"
            assert 1 <= pr.specialty_id <= len(CATEGORIES)


def test_posts_ranges(data) -> None:  # type: ignore[no-untyped-def]
    p = small()
    for post in data.posts:
        assert 1 <= post.author_id <= p.users
        assert 1 <= post.category_id <= len(CATEGORIES)
        assert 0 <= post.age_seconds < p.window_days * SECONDS_PER_DAY
        assert post.status in {"approved", "suspicious", "blocked"}


def test_comments_not_older_than_post(data) -> None:  # type: ignore[no-untyped-def]
    age = {p.id: p.age_seconds for p in data.posts}
    assert all(c.age_seconds <= age[c.post_id] for c in data.comments)
    assert all(r.age_seconds <= age[r.post_id] for r in data.reactions)


def test_reactions_unique_pairs(data) -> None:  # type: ignore[no-untyped-def]
    pairs = [(r.post_id, r.user_id) for r in data.reactions]
    assert len(pairs) == len(set(pairs))
    assert {r.kind for r in data.reactions} <= {"like", "support", "useful"}


def test_denormalized_counters(data) -> None:  # type: ignore[no-untyped-def]
    approved = Counter(c.post_id for c in data.comments if c.status == "approved")
    reacts = Counter(r.post_id for r in data.reactions)
    for post in data.posts:
        assert post.comments_count == approved[post.id]
        assert post.reactions_count == reacts[post.id]


def test_moderation_records_rule(data, moderator: ContentModerator) -> None:  # type: ignore[no-untyped-def]
    """Запись на каждый пост и на каждый неодобренный комментарий — как в приложении."""
    by_type = Counter(m.target_type for m in data.moderation)
    assert by_type["post"] == len(data.posts)
    assert by_type["comment"] == sum(c.status != "approved" for c in data.comments)
    post = data.posts[0]
    rec = next(m for m in data.moderation if m.target_type == "post" and m.target_id == post.id)
    r = moderator.moderate(f"{post.title}\n{post.body}")
    assert (rec.label, rec.level) == (r.label, r.level)
    assert json.loads(rec.reasons) == r.reasons
    assert [m.id for m in data.moderation] == list(range(1, len(data.moderation) + 1))


@pytest.mark.parametrize(
    "bad",
    [
        dict(users=0),
        dict(admins=50, moderators=20),
        dict(comments=-1),
        dict(body_words=(10, 5)),
        dict(suspicious_share=2.0),
        dict(reactions=10**9),
    ],
)
def test_invalid_params(moderator: ContentModerator, bad: dict) -> None:  # type: ignore[type-arg]
    with pytest.raises(ValueError):
        generate(small(**bad), moderator)


def test_load_texts_deterministic() -> None:
    a = generate_load_texts(1, 30, 0.05, (20, 40), (5, 20))
    assert a == generate_load_texts(1, 30, 0.05, (20, 40), (5, 20))
    assert len(a["posts"]) == len(a["comments"]) == len(a["bios"]) == 30
    assert all(1 <= p["category_id"] <= len(CATEGORIES) for p in a["posts"])
