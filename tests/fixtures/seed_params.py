"""Уменьшенные параметры seed для тестов.  TEST DATA ONLY."""

from __future__ import annotations

from database.seed.generator import SeedParams

PASSWORD_TEMPLATE = "bench-{user_id}"


def small(seed: int = 11, **overrides: object) -> SeedParams:
    base: dict[str, object] = dict(
        seed=seed,
        users=60,
        admins=1,
        moderators=2,
        posts=300,
        comments=900,
        reactions=2000,
        doctor_share=0.3,
        user_age_days=365,
        window_days=30,
        suspicious_share=0.1,
        body_words=(20, 40),
        comment_words=(5, 20),
        reaction_kinds={"like": 0.6, "support": 0.25, "useful": 0.15},
        password_template=PASSWORD_TEMPLATE,
        pbkdf2_iterations=5,
    )
    base.update(overrides)
    return SeedParams(**base)  # type: ignore[arg-type]
