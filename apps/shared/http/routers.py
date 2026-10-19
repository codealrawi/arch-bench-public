"""Роутеры API v1, сгруппированные по доменам (границы сервисов — решение M-2).

| Группа    | Пути                                   | Микросервис | Функция OpenFaaS |
|-----------|----------------------------------------|-------------|------------------|
| auth      | /auth/*                                 | auth        | auth             |
| users     | /users/*                                | auth        | auth             |
| content   | /categories, /posts/*, /search          | content     | content          |
| moderation| /moderation/* (очередь, решения)        | content     | content          |
| stats     | /stats                                  | analytics   | stats            |
| internal  | /internal/moderate (не публикуется)      | moderation  | — (в процессе)   |

Монолит и 3-tier подключают все группы, кроме internal.
"""

from __future__ import annotations

from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, Query, Request, Response

from apps.shared.auth import Principal
from apps.shared.domain import accounts, content, stats
from apps.shared.domain.moderation import ModerationClient, verdict_from
from apps.shared.http import schemas as s
from apps.shared.http.ratelimit import auth_rate_limit
from apps.shared.http.security import current_user, moderator_user
from apps.shared.settings import AppSettings

ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": s.ErrorOut} for code in (401, 403, 404, 409, 422, 429, 503, 504)
}


def _pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool


def _settings(request: Request) -> AppSettings:
    settings: AppSettings = request.app.state.settings
    return settings


def _moderation(request: Request) -> ModerationClient:
    moderation: ModerationClient = request.app.state.moderation
    return moderation


# ── auth ────────────────────────────────────────────────────────────────────────────────
auth_router = APIRouter(prefix="/auth", tags=["auth"], responses=ERRORS)


@auth_router.post(
    "/register", status_code=201, response_model=s.AuthOut, dependencies=[Depends(auth_rate_limit)]
)
async def register(
    body: s.RegisterIn, pool: asyncpg.Pool = Depends(_pool), cfg: AppSettings = Depends(_settings)
) -> Any:
    return await accounts.register(
        pool,
        email=str(body.email),
        password=body.password,
        display_name=body.display_name,
        role=body.role,
        secret=cfg.jwt_secret,
        access_ttl_s=cfg.access_token_ttl_s,
        refresh_ttl_s=cfg.refresh_token_ttl_s,
        pbkdf2_iterations=cfg.pbkdf2_iterations,
    )


@auth_router.post("/login", response_model=s.AuthOut, dependencies=[Depends(auth_rate_limit)])
async def login(
    body: s.LoginIn, pool: asyncpg.Pool = Depends(_pool), cfg: AppSettings = Depends(_settings)
) -> Any:
    return await accounts.login(
        pool,
        email=str(body.email),
        password=body.password,
        secret=cfg.jwt_secret,
        access_ttl_s=cfg.access_token_ttl_s,
        refresh_ttl_s=cfg.refresh_token_ttl_s,
    )


@auth_router.post("/refresh", response_model=s.TokenPair, dependencies=[Depends(auth_rate_limit)])
async def refresh(
    body: s.RefreshIn, pool: asyncpg.Pool = Depends(_pool), cfg: AppSettings = Depends(_settings)
) -> Any:
    return await accounts.refresh(
        pool,
        refresh_token=body.refresh_token,
        secret=cfg.jwt_secret,
        access_ttl_s=cfg.access_token_ttl_s,
        refresh_ttl_s=cfg.refresh_token_ttl_s,
    )


@auth_router.post("/logout", status_code=204, response_class=Response)
async def logout(body: s.RefreshIn, pool: asyncpg.Pool = Depends(_pool)) -> Response:
    await accounts.logout(pool, refresh_token=body.refresh_token)
    return Response(status_code=204)


# ── users / profiles ────────────────────────────────────────────────────────────────────
users_router = APIRouter(prefix="/users", tags=["users"], responses=ERRORS)


@users_router.get("/me", response_model=s.ProfileOut)
async def me(user: Principal = Depends(current_user), pool: asyncpg.Pool = Depends(_pool)) -> Any:
    return await accounts.get_profile(pool, user.user_id, include_private=True)


@users_router.patch("/me", response_model=s.ProfileOut)
async def update_me(
    body: s.ProfileUpdateIn,
    user: Principal = Depends(current_user),
    pool: asyncpg.Pool = Depends(_pool),
) -> Any:
    return await accounts.update_profile(pool, user.user_id, body.model_dump(exclude_unset=True))


@users_router.get("/{user_id}", response_model=s.ProfileOut, response_model_exclude_none=True)
async def get_user(
    user_id: int, viewer: Principal = Depends(current_user), pool: asyncpg.Pool = Depends(_pool)
) -> Any:
    return await accounts.get_profile(pool, user_id, include_private=viewer.user_id == user_id)


# ── content ─────────────────────────────────────────────────────────────────────────────
content_router = APIRouter(tags=["content"], responses=ERRORS)


@content_router.get("/categories", response_model=list[s.CategoryOut])
async def categories(pool: asyncpg.Pool = Depends(_pool)) -> Any:
    return await content.list_categories(pool)


@content_router.get("/posts", response_model=list[s.PostOut])
async def feed(
    request: Request,
    _: Principal = Depends(current_user),
    category_id: int | None = Query(default=None, ge=1, le=32767),
    before_id: int | None = Query(default=None, ge=1),
    limit: int | None = Query(default=None, ge=1, le=100),
    pool: asyncpg.Pool = Depends(_pool),
) -> Any:
    cfg = _settings(request)
    return await content.feed(
        pool,
        limit=min(limit or cfg.feed_limit, cfg.feed_limit),
        category_id=category_id,
        before_id=before_id,
    )


@content_router.post("/posts", status_code=201, response_model=s.PostCreated)
async def create_post(
    body: s.PostIn,
    user: Principal = Depends(current_user),
    pool: asyncpg.Pool = Depends(_pool),
    moderation: ModerationClient = Depends(_moderation),
) -> Any:
    return await content.create_post(
        pool, moderation, user, title=body.title, body=body.body, category_id=body.category_id
    )


@content_router.get("/posts/{post_id}", response_model=s.PostDetail)
async def get_post(
    post_id: int,
    request: Request,
    user: Principal = Depends(current_user),
    pool: asyncpg.Pool = Depends(_pool),
) -> Any:
    return await content.get_post(
        pool, post_id, user, comments_limit=_settings(request).comments_page_limit
    )


@content_router.delete("/posts/{post_id}", status_code=204, response_class=Response)
async def delete_post(
    post_id: int, user: Principal = Depends(current_user), pool: asyncpg.Pool = Depends(_pool)
) -> Response:
    await content.delete_post(pool, post_id, user)
    return Response(status_code=204)


@content_router.get("/posts/{post_id}/comments", response_model=list[s.CommentOut])
async def comments(
    post_id: int,
    request: Request,
    _: Principal = Depends(current_user),
    after_id: int | None = Query(default=None, ge=1),
    pool: asyncpg.Pool = Depends(_pool),
) -> Any:
    return await content.list_comments(
        pool, post_id, after_id=after_id, limit=_settings(request).comments_page_limit
    )


@content_router.post("/posts/{post_id}/comments", status_code=201, response_model=s.CommentCreated)
async def add_comment(
    post_id: int,
    body: s.CommentIn,
    user: Principal = Depends(current_user),
    pool: asyncpg.Pool = Depends(_pool),
    moderation: ModerationClient = Depends(_moderation),
) -> Any:
    return await content.add_comment(pool, moderation, user, post_id, body.body)


@content_router.put("/posts/{post_id}/reaction", response_model=s.ReactionOut)
async def react(
    post_id: int,
    body: s.ReactionIn,
    user: Principal = Depends(current_user),
    pool: asyncpg.Pool = Depends(_pool),
) -> Any:
    return await content.set_reaction(pool, user, post_id, body.kind)


@content_router.delete("/posts/{post_id}/reaction", status_code=204, response_class=Response)
async def unreact(
    post_id: int, user: Principal = Depends(current_user), pool: asyncpg.Pool = Depends(_pool)
) -> Response:
    await content.remove_reaction(pool, user, post_id)
    return Response(status_code=204)


@content_router.get("/search", response_model=list[s.SearchHit])
async def search(
    request: Request,
    q: str = Query(min_length=1, max_length=200),
    _: Principal = Depends(current_user),
    pool: asyncpg.Pool = Depends(_pool),
) -> Any:
    return await content.search(pool, q, limit=_settings(request).search_limit)


# ── moderation (очередь и решения) ──────────────────────────────────────────────────────
moderation_router = APIRouter(prefix="/moderation", tags=["moderation"], responses=ERRORS)


@moderation_router.get("/queue", response_model=list[s.PostOut])
async def queue(
    request: Request, _: Principal = Depends(moderator_user), pool: asyncpg.Pool = Depends(_pool)
) -> Any:
    return await content.moderation_queue(pool, limit=_settings(request).feed_limit)


@moderation_router.post("/posts/{post_id}/decision", response_model=s.DecisionOut)
async def decision(
    post_id: int,
    body: s.DecisionIn,
    user: Principal = Depends(moderator_user),
    pool: asyncpg.Pool = Depends(_pool),
) -> Any:
    return await content.decide(pool, user, post_id, body.decision)


# ── stats ───────────────────────────────────────────────────────────────────────────────
stats_router = APIRouter(tags=["stats"], responses=ERRORS)


@stats_router.get("/stats", response_model=s.StatsOut)
async def platform_stats(
    request: Request, _: Principal = Depends(current_user), pool: asyncpg.Pool = Depends(_pool)
) -> Any:
    return await stats.platform_stats(pool, _settings(request).stats_window_days)


# ── internal: сервис moderation (микросервисная конфигурация) ───────────────────────────
internal_router = APIRouter(prefix="/internal", tags=["internal"], include_in_schema=False)


@internal_router.post("/moderate", response_model=s.ModerationOut)
async def moderate(body: s.ModerateIn, request: Request) -> Any:
    return verdict_from(request.app.state.moderator, body.text).to_dict()


PUBLIC_ROUTERS = [auth_router, users_router, content_router, moderation_router, stats_router]
