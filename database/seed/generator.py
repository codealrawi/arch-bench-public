"""Детерминированная генерация начального состояния БД (seed 20261004).

Чистые функции без БД. Данные синтетические по замыслу (описание рабочей нагрузки стенда),
не замена реального датасета. Статусы постов и комментариев присваивает настоящий
ContentModerator — тот же, что работает в приложении. Записи модерации создаются по тем же
правилам, что и в приложении: для каждого поста и для каждого неодобренного комментария.

Каждый поток случайности инициализируется строкой (``random.Random("posts:20261004")``):
для строк Python использует SHA-512, результат не зависит от PYTHONHASHSEED и платформы.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from apps.shared.auth import hash_password
from apps.shared.moderation import ContentModerator

SECONDS_PER_DAY = 86_400

# Справочник медицинских категорий (предметная область, а не результат)
CATEGORIES: list[tuple[str, str]] = [
    ("cardiology", "Кардиология"),
    ("endocrinology", "Эндокринология"),
    ("neurology", "Неврология"),
    ("pediatrics", "Педиатрия"),
    ("pulmonology", "Пульмонология"),
    ("gastroenterology", "Гастроэнтерология"),
    ("rheumatology", "Ревматология"),
    ("dermatology", "Дерматология"),
    ("oncology", "Онкология"),
    ("psychiatry", "Психиатрия"),
    ("ophthalmology", "Офтальмология"),
    ("otolaryngology", "Оториноларингология"),
    ("urology", "Урология"),
    ("gynecology", "Гинекология"),
    ("orthopedics", "Ортопедия"),
    ("infectious-diseases", "Инфекционные болезни"),
    ("allergology", "Аллергология"),
    ("nephrology", "Нефрология"),
    ("rehabilitation", "Реабилитация"),
    ("general-practice", "Общая практика"),
]

FIRST_NAMES = [
    "Анна",
    "Мария",
    "Елена",
    "Ольга",
    "Ирина",
    "Наталья",
    "Татьяна",
    "Светлана",
    "Алексей",
    "Дмитрий",
    "Сергей",
    "Андрей",
    "Михаил",
    "Иван",
    "Павел",
    "Николай",
]
LAST_INITIALS = "АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЭЮЯ"
CITIES = [
    "Москва",
    "Санкт-Петербург",
    "Казань",
    "Новосибирск",
    "Екатеринбург",
    "Самара",
    "Нижний Новгород",
    "Ростов-на-Дону",
    "Краснодар",
    "Воронеж",
    "Пермь",
    "Уфа",
]
SUBJECTS = [
    "пациент",
    "врач",
    "терапевт",
    "кардиолог",
    "эндокринолог",
    "невролог",
    "педиатр",
    "специалист",
    "участковый врач",
    "хирург",
]
TERMS = [
    "диагноз",
    "симптом",
    "лечение",
    "терапия",
    "препарат",
    "анализ",
    "исследование",
    "реабилитация",
    "профилактика",
    "вакцина",
    "антибиотик",
    "дозировка",
    "побочный эффект",
    "давление",
    "холестерин",
    "гемоглобин",
    "глюкоза",
    "консультация",
    "обследование",
    "рецепт",
    "курс",
    "режим",
    "питание",
    "нагрузка",
    "сон",
    "температура",
    "боль",
    "МРТ",
    "УЗИ",
    "ЭКГ",
    "анализ крови",
    "клиника",
    "приём",
    "рекомендации",
]
VERBS = [
    "назначил",
    "рекомендует",
    "обсуждаем",
    "уточните",
    "проверьте",
    "снижает",
    "повышает",
    "контролирует",
    "наблюдаем",
    "сравниваем",
    "показало",
    "требует",
]
CONNECTORS = ["после", "во время", "при", "перед", "без учёта", "с учётом", "вместе с"]
REPLIES = [
    "спасибо за ответ",
    "у меня похожая ситуация",
    "уточните дозировку",
    "согласен с врачом",
    "а сколько длился курс",
    "помогло ли лечение",
    "запишитесь на консультацию",
    "сдайте анализ повторно",
]
SUSPICIOUS_PHRASES = [
    "врачи скрывают правду",
    "гарантированное излечение за неделю",
    "отмените все лекарства",
    "чудо-средство от всех болезней",
    "секретный рецепт исцеления",
    "купить сейчас со скидкой 70%",
    "перейди по ссылке http://example.invalid",
    "пишите в telegram",
]
BIOS = [
    "Слежу за здоровьем семьи.",
    "Практикующий врач.",
    "Интересуюсь профилактикой.",
    "Восстанавливаюсь после операции.",
    "Читаю о новых методах лечения.",
    "",
]


@dataclass(frozen=True, slots=True)
class UserRow:
    id: int
    email: str
    password_hash: str
    role: str
    age_seconds: int


@dataclass(frozen=True, slots=True)
class ProfileRow:
    user_id: int
    display_name: str
    bio: str
    city: str
    birth_year: int | None
    specialty_id: int | None


@dataclass(frozen=True, slots=True)
class PostRow:
    id: int
    author_id: int
    category_id: int
    title: str
    body: str
    status: str
    comments_count: int
    reactions_count: int
    age_seconds: int


@dataclass(frozen=True, slots=True)
class CommentRow:
    id: int
    post_id: int
    author_id: int
    body: str
    status: str
    age_seconds: int


@dataclass(frozen=True, slots=True)
class ReactionRow:
    post_id: int
    user_id: int
    kind: str
    age_seconds: int


@dataclass(frozen=True, slots=True)
class ModerationRow:
    id: int
    target_type: str
    target_id: int
    label: str
    level: int
    confidence: float
    reasons: str  # JSON-массив
    age_seconds: int


@dataclass
class SeedParams:
    seed: int
    users: int
    admins: int
    moderators: int
    posts: int
    comments: int
    reactions: int
    doctor_share: float
    user_age_days: int
    window_days: int
    suspicious_share: float
    body_words: tuple[int, int]
    comment_words: tuple[int, int]
    reaction_kinds: dict[str, float]
    password_template: str
    pbkdf2_iterations: int

    def validate(self) -> None:
        if self.users < 1 or self.posts < 1:
            raise ValueError("users и posts должны быть ≥ 1")
        if self.admins + self.moderators >= self.users:
            raise ValueError("admins + moderators ≥ users")
        if self.comments < 0 or self.reactions < 0:
            raise ValueError("comments и reactions не могут быть отрицательными")
        if self.reactions > self.users * self.posts // 2:
            raise ValueError("reactions слишком близко к числу возможных пар")
        for lo, hi in (self.body_words, self.comment_words):
            if not 1 <= lo <= hi:
                raise ValueError("нужно 1 ≤ min ≤ max слов")
        if not 0 <= self.suspicious_share <= 1 or not 0 <= self.doctor_share <= 1:
            raise ValueError("доли вне [0, 1]")
        if self.window_days < 1 or self.user_age_days < 1:
            raise ValueError("периоды должны быть ≥ 1 дня")


def user_email(user_id: int) -> str:
    """Домен example.com зарезервирован RFC 2606 (.test отклоняется валидатором email)."""
    return f"user{user_id}@bench.example.com"


def user_password(template: str, user_id: int) -> str:
    """Пароль тестовой учётной записи стенда (используется только в setup() k6 и тестах)."""
    return template.format(user_id=user_id)


def _salt(seed: int, user_id: int) -> bytes:
    return hashlib.sha256(f"{seed}:{user_id}".encode()).digest()[:16]


def role_of(user_id: int, p: SeedParams, rng_value: float) -> str:
    if user_id <= p.admins:
        return "admin"
    if user_id <= p.admins + p.moderators:
        return "moderator"
    return "doctor" if rng_value < p.doctor_share else "patient"


def generate_users(p: SeedParams) -> list[UserRow]:
    rng = random.Random(f"users:{p.seed}")
    out = []
    for uid in range(1, p.users + 1):
        role = role_of(uid, p, rng.random())
        age = rng.randrange(0, p.user_age_days * SECONDS_PER_DAY)
        out.append(
            UserRow(
                uid,
                user_email(uid),
                hash_password(
                    user_password(p.password_template, uid), _salt(p.seed, uid), p.pbkdf2_iterations
                ),
                role,
                age,
            )
        )
    return out


def generate_profiles(p: SeedParams, users: list[UserRow]) -> list[ProfileRow]:
    rng = random.Random(f"profiles:{p.seed}")
    out = []
    for u in users:
        name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_INITIALS)}."
        birth = rng.randint(1940, 2008) if rng.random() < 0.9 else None
        specialty = rng.randint(1, len(CATEGORIES)) if u.role == "doctor" else None
        out.append(ProfileRow(u.id, name, rng.choice(BIOS), rng.choice(CITIES), birth, specialty))
    return out


def make_text(rng: random.Random, n_words: int, suspicious: bool) -> tuple[str, str]:
    topic = rng.choice(CATEGORIES)[1].lower()
    title = f"{rng.choice(TERMS).capitalize()}: {topic}, {rng.choice(TERMS)}"
    words: list[str] = []
    while len(words) < n_words:
        words.extend(
            f"{rng.choice(SUBJECTS)} {rng.choice(VERBS)} {rng.choice(TERMS)} "
            f"{rng.choice(CONNECTORS)} {rng.choice(TERMS)}".split()
        )
    words = words[:n_words]
    if suspicious:
        phrase = rng.choice(SUSPICIOUS_PHRASES).split()
        pos = rng.randrange(0, max(1, len(words) - len(phrase)))
        words[pos : pos + len(phrase)] = phrase
    return title, " ".join(words).capitalize() + "."


def make_comment(rng: random.Random, n_words: int, suspicious: bool) -> str:
    words: list[str] = []
    while len(words) < n_words:
        words.extend(rng.choice(REPLIES).split() + [rng.choice(TERMS)])
    words = words[:n_words]
    if suspicious:
        phrase = rng.choice(SUSPICIOUS_PHRASES).split()
        words = (words + phrase)[: max(n_words, len(phrase))]
    return " ".join(words).capitalize() + "."


@dataclass
class SeedData:
    users: list[UserRow]
    profiles: list[ProfileRow]
    posts: list[PostRow]
    comments: list[CommentRow]
    reactions: list[ReactionRow]
    moderation: list[ModerationRow]


def _verdict(moderator: ContentModerator, text: str) -> tuple[str, int, float, str]:
    r = moderator.moderate(text)
    return (r.label, int(r.level), float(r.confidence), json.dumps(r.reasons, ensure_ascii=False))


def generate(p: SeedParams, moderator: ContentModerator) -> SeedData:
    """Полное начальное состояние. Порядок потоков случайности фиксирован."""
    p.validate()
    users = generate_users(p)
    profiles = generate_profiles(p, users)

    # посты
    rng = random.Random(f"posts:{p.seed}")
    window_s = p.window_days * SECONDS_PER_DAY
    raw_posts = []
    moderation: list[ModerationRow] = []
    for pid in range(1, p.posts + 1):
        author = rng.randint(1, p.users)
        category = rng.randint(1, len(CATEGORIES))
        age = rng.randrange(0, window_s)
        title, body = make_text(rng, rng.randint(*p.body_words), rng.random() < p.suspicious_share)
        label, level, conf, reasons = _verdict(moderator, f"{title}\n{body}")
        raw_posts.append((pid, author, category, title, body, label, age))
        moderation.append(
            ModerationRow(len(moderation) + 1, "post", pid, label, level, conf, reasons, age)
        )

    # комментарии: не раньше поста
    rng = random.Random(f"comments:{p.seed}")
    comments = []
    approved_comments = [0] * (p.posts + 1)
    for cid in range(1, p.comments + 1):
        pid = rng.randint(1, p.posts)
        post_age = raw_posts[pid - 1][6]
        age = rng.randint(0, post_age)
        body = make_comment(rng, rng.randint(*p.comment_words), rng.random() < p.suspicious_share)
        label, level, conf, reasons = _verdict(moderator, body)
        comments.append(CommentRow(cid, pid, rng.randint(1, p.users), body, label, age))
        if label == "approved":
            approved_comments[pid] += 1
        else:
            moderation.append(
                ModerationRow(len(moderation) + 1, "comment", cid, label, level, conf, reasons, age)
            )

    # реакции: различные пары (пост, пользователь)
    rng = random.Random(f"reactions:{p.seed}")
    kinds = sorted(p.reaction_kinds)
    weights = [p.reaction_kinds[k] for k in kinds]
    seen: set[int] = set()
    reactions: list[ReactionRow] = []
    reaction_counts = [0] * (p.posts + 1)
    while len(reactions) < p.reactions:
        pid, uid = rng.randint(1, p.posts), rng.randint(1, p.users)
        key = pid * (p.users + 1) + uid
        if key in seen:
            continue
        seen.add(key)
        age = rng.randint(0, raw_posts[pid - 1][6])
        reactions.append(ReactionRow(pid, uid, rng.choices(kinds, weights)[0], age))
        reaction_counts[pid] += 1
    reactions.sort(key=lambda r: (r.post_id, r.user_id))

    posts = [
        PostRow(pid, a, c, t, b, s, approved_comments[pid], reaction_counts[pid], age)
        for (pid, a, c, t, b, s, age) in raw_posts
    ]
    return SeedData(users, profiles, posts, comments, reactions, moderation)


def generate_load_texts(
    seed: int,
    n: int,
    suspicious_share: float,
    body_words: tuple[int, int],
    comment_words: tuple[int, int],
) -> dict[str, Any]:
    """Тексты для операций записи в k6 (тот же генератор, отдельный поток случайности)."""
    rng = random.Random(f"load-texts:{seed}")
    posts: list[dict[str, Any]] = []
    comments: list[str] = []
    for _ in range(n):
        title, body = make_text(rng, rng.randint(*body_words), rng.random() < suspicious_share)
        posts.append({"title": title, "body": body, "category_id": rng.randint(1, len(CATEGORIES))})
        comments.append(
            make_comment(rng, rng.randint(*comment_words), rng.random() < suspicious_share)
        )
    bios = [f"{rng.choice(BIOS)} {rng.choice(CITIES)}".strip() for _ in range(n)]
    return {"posts": posts, "comments": comments, "bios": bios}


def iter_rows(data: SeedData) -> Iterator[tuple[str, int]]:
    for name in ("users", "profiles", "posts", "comments", "reactions", "moderation"):
        yield name, len(getattr(data, name))
