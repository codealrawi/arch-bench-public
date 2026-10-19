"""
Сервис интеллектуальной модерации контента
Социально-ориентированная веб-система (медицинский домен)

Алгоритм: двухуровневый классификатор
  Уровень 1: TF-IDF + Logistic Regression
  Уровень 2: Усиленная проверка подозрительного контента

Перенесено из medplatforma-svd-anomaly/medplatforma-main-pkg/services/moderation_service.py
(SHA-256 исходника: D8AA8F88A7F9109B1869B8151A1DAD0C474F410D025C064F92037F39B97512DB).
Алгоритм, словари, обучающие данные и пороги не изменены. Изменения только в обвязке:
удалён logging.basicConfig при импорте (побочный эффект для приложений) и неиспользуемый
import json. Эквивалентность решений проверяется в tests/unit/test_moderation.py.
"""

import re
import math
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Tuple
from collections import Counter

logger = logging.getLogger(__name__)

# ─── Стоп-слова (медицинский домен) ─────────────────────────────────────────
STOP_WORDS = {
    "и", "в", "не", "на", "с", "что", "это", "как", "по", "но",
    "а", "к", "у", "я", "он", "она", "они", "мы", "вы", "же",
    "от", "за", "для", "до", "при", "об", "из", "то", "так",
    "был", "была", "были", "быть", "есть", "или", "если", "ещё",
    "уже", "когда", "который", "которая", "которые", "что",
}

# ─── Словари для модерации ───────────────────────────────────────────────────
DANGEROUS_PATTERNS = [
    r"лечи[тьтесь]+\s+без\s+врач",
    r"врачи\s+скрывают",
    r"официальная\s+медицина\s+лж[её]т",
    r"гарантированное\s+излечение",
    r"100%\s+результат",
    r"отменит[еь]\s+все\s+лекарства",
    r"чудо[- ]средство",
    r"секретный\s+рецепт",
]

SPAM_PATTERNS = [
    r"купи[тть]+\s+(сейчас|здесь|тут)",
    r"скидка\s+\d+%",
    r"перейди\s+по\s+ссылке",
    r"http[s]?://",
    r"telegram|whatsapp|viber",
    r"звони[тть]+\s+прямо\s+сейчас",
]

TRUSTED_MEDICAL_TERMS = {
    "диагноз", "симптом", "лечение", "терапия", "препарат",
    "врач", "пациент", "клиника", "исследование", "анализ",
    "операция", "реабилитация", "профилактика", "вакцина",
    "антибиотик", "дозировка", "побочный", "эффект",
}


@dataclass
class ModerationResult:
    label: str            # "approved" | "suspicious" | "blocked"
    confidence: float     # 0.0 – 1.0
    level: int            # 1 или 2
    reasons: List[str] = field(default_factory=list)
    scores: Dict[str, float] = field(default_factory=dict)


# ─── TF-IDF (реализация без sklearn для прозрачности) ───────────────────────
class TFIDFVectorizer:
    """Упрощённая реализация TF-IDF для демонстрации алгоритма."""

    def __init__(self, max_features: int = 500):
        self.max_features = max_features
        self.vocabulary_: Dict[str, int] = {}
        self.idf_: Dict[str, float] = {}
        self._fitted = False

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        text = text.lower()
        text = re.sub(r"[^\w\s]", " ", text)
        tokens = text.split()
        return [t for t in tokens if t not in STOP_WORDS and len(t) > 2]

    def fit(self, corpus: List[str]):
        doc_freq: Counter = Counter()
        tokenized = [self._tokenize(doc) for doc in corpus]
        n_docs = len(corpus)

        for tokens in tokenized:
            for term in set(tokens):
                doc_freq[term] += 1

        # Отбор топ-N слов по DF
        top_terms = [t for t, _ in doc_freq.most_common(self.max_features)]
        self.vocabulary_ = {term: idx for idx, term in enumerate(top_terms)}

        # IDF = log((1 + N) / (1 + df)) + 1
        for term, df in doc_freq.items():
            if term in self.vocabulary_:
                self.idf_[term] = math.log((1 + n_docs) / (1 + df)) + 1

        self._fitted = True
        logger.info(f"TF-IDF fitted: vocab_size={len(self.vocabulary_)}, docs={n_docs}")
        return self

    def transform(self, texts: List[str]) -> List[Dict[int, float]]:
        assert self._fitted, "Fit vectorizer first"
        vectors = []
        for text in texts:
            tokens = self._tokenize(text)
            tf: Counter = Counter(tokens)
            n_tokens = len(tokens) or 1
            vec: Dict[int, float] = {}
            for term, count in tf.items():
                if term in self.vocabulary_:
                    idx = self.vocabulary_[term]
                    tfidf = (count / n_tokens) * self.idf_.get(term, 1.0)
                    vec[idx] = tfidf
            vectors.append(vec)
        return vectors

    def fit_transform(self, corpus: List[str]):
        self.fit(corpus)
        return self.transform(corpus)


# ─── Логистическая регрессия (SGD, прозрачная реализация) ───────────────────
class LogisticRegressionSGD:
    """
    Бинарный логистический классификатор (SGD).
    Поддерживает разреженные входы (dict-векторы от TF-IDF).
    """

    def __init__(self, lr: float = 0.1, epochs: int = 20,
                 reg: float = 0.01, vocab_size: int = 500):
        self.lr = lr
        self.epochs = epochs
        self.reg = reg
        self.vocab_size = vocab_size
        self.weights: Dict[int, float] = {}
        self.bias: float = 0.0
        self.classes_: List[str] = []

    @staticmethod
    def _sigmoid(z: float) -> float:
        if z >= 0:
            return 1.0 / (1.0 + math.exp(-z))
        ez = math.exp(z)
        return ez / (1.0 + ez)

    def _dot(self, vec: Dict[int, float]) -> float:
        return sum(self.weights.get(i, 0.0) * v for i, v in vec.items()) + self.bias

    def fit(self, X: List[Dict[int, float]], y: List[int]):
        self.classes_ = sorted(set(y))
        for epoch in range(self.epochs):
            total_loss = 0.0
            for xi, yi in zip(X, y):
                z = self._dot(xi)
                pred = self._sigmoid(z)
                error = pred - yi
                # Обновление весов (L2-регуляризация)
                for i, v in xi.items():
                    grad = error * v + self.reg * self.weights.get(i, 0.0)
                    self.weights[i] = self.weights.get(i, 0.0) - self.lr * grad
                self.bias -= self.lr * error
                total_loss += -(yi * math.log(pred + 1e-9) +
                                (1 - yi) * math.log(1 - pred + 1e-9))
            if epoch % 5 == 0:
                logger.debug(f"Epoch {epoch}: loss={total_loss/len(y):.4f}")
        return self

    def predict_proba(self, X: List[Dict[int, float]]) -> List[float]:
        return [self._sigmoid(self._dot(xi)) for xi in X]

    def predict(self, X: List[Dict[int, float]], threshold: float = 0.5) -> List[int]:
        return [1 if p >= threshold else 0 for p in self.predict_proba(X)]


# ─── Двухуровневый классификатор ─────────────────────────────────────────────
class ContentModerator:
    """
    Двухуровневый классификатор:
      Уровень 1 → TF-IDF + Logistic Regression (быстрый, все публикации)
      Уровень 2 → Расширенный rule-based + паттерны (только подозрительный)
    """

    LABELS = {0: "approved", 1: "suspicious", 2: "blocked"}

    def __init__(self):
        self.vectorizer = TFIDFVectorizer(max_features=300)
        self.classifier = LogisticRegressionSGD(lr=0.05, epochs=30, reg=0.005)
        self._trained = False

    # ── Генерация синтетических данных для обучения ──
    @staticmethod
    def _generate_training_data() -> Tuple[List[str], List[int]]:
        approved = [
            "Как правильно принимать антибиотики при бактериальной инфекции?",
            "Врач назначил курс физиотерапии после операции на колене",
            "Какие симптомы характерны для дефицита витамина D?",
            "Результаты анализа крови показали повышенный холестерин",
            "Необходима консультация кардиолога по поводу аритмии",
            "Реабилитация после инфаркта: рекомендации специалиста",
            "Профилактика диабета 2 типа: диета и физическая активность",
            "Какова дозировка ибупрофена при температуре у взрослого?",
            "МРТ показало грыжу межпозвонкового диска в поясничном отделе",
            "Вакцинация против гриппа: противопоказания и побочные эффекты",
            "Лечение гипертонии: современные подходы и препараты",
            "Диагностика аппендицита: клиническая картина и методы",
            "Советы по уходу за ребёнком при ОРВИ",
            "Эффективность пробиотиков при дисбиозе кишечника",
            "Операция по удалению желчного пузыря: подготовка и восстановление",
        ] * 6

        disinformation = [
            "Врачи скрывают простое средство от рака — лечитесь без врача",
            "Официальная медицина лжёт: вакцины вызывают аутизм",
            "Гарантированное излечение диабета за 3 дня натуральным методом",
            "Чудо-средство от всех болезней, которое скрывают фармацевты",
            "Отмените все лекарства и пейте только этот отвар",
            "100% результат лечения онкологии без химиотерапии",
            "Секретный рецепт, который врачи не хотят вам рассказывать",
            "Вылечился от гепатита за неделю без таблеток — личный опыт",
        ] * 11

        spam = [
            "Купить сейчас! Скидка 70% на препараты для похудения",
            "Перейди по ссылке и получи бесплатно лечение позвоночника",
            "Звоните прямо сейчас — telegram +7999123456",
            "http://best-pills.ru скидка на все препараты сегодня",
            "Быстрое похудение без диет — звони прямо сейчас",
        ] * 18

        texts = approved + disinformation + spam
        # 0 = одобрено, 1 = требует блокировки/модерации
        labels = [0] * len(approved) + [1] * len(disinformation) + [1] * len(spam)
        return texts, labels

    def train(self):
        texts, labels = self._generate_training_data()
        X = self.vectorizer.fit_transform(texts)
        self.classifier.fit(X, labels)
        self._trained = True
        logger.info("ContentModerator: training complete")
        return self

    # ── Уровень 2: расширенные проверки ─────────────────────────────────────
    @staticmethod
    def _level2_analysis(text: str) -> Tuple[str, List[str], float]:
        reasons = []
        risk_score = 0.0

        # Проверка опасных паттернов дезинформации
        for pattern in DANGEROUS_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                reasons.append(f"Дезинформация: паттерн '{pattern[:30]}...'")
                risk_score += 0.35

        # Проверка спама
        for pattern in SPAM_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                reasons.append(f"Спам: паттерн '{pattern[:30]}...'")
                risk_score += 0.25

        # Доля доверенных медицинских терминов
        tokens = set(re.findall(r"\b\w+\b", text.lower()))
        trusted_ratio = len(tokens & TRUSTED_MEDICAL_TERMS) / max(len(tokens), 1)
        if trusted_ratio < 0.03:
            reasons.append("Низкая доля медицинской терминологии")
            risk_score += 0.15

        risk_score = min(risk_score, 1.0)

        if risk_score >= 0.5:
            return "blocked", reasons, 1.0 - risk_score * 0.3
        if risk_score >= 0.2:
            return "suspicious", reasons, 0.6
        return "approved", reasons, 0.85

    def moderate(self, text: str) -> ModerationResult:
        assert self._trained, "Moderator not trained. Call train() first."

        vec = self.vectorizer.transform([text])
        proba = self.classifier.predict_proba(vec)[0]

        scores = {"harm_probability": round(proba, 4)}

        # Уровень 1 — быстрая классификация
        if proba < 0.3:
            return ModerationResult(
                label="approved",
                confidence=round(1 - proba, 3),
                level=1,
                reasons=[],
                scores=scores,
            )

        # Уровень 2 — расширенный анализ
        label2, reasons, conf2 = self._level2_analysis(text)
        scores["level2_risk"] = round(1 - conf2, 4)
        return ModerationResult(
            label=label2,
            confidence=round(conf2, 3),
            level=2,
            reasons=reasons,
            scores=scores,
        )

    def evaluate(self) -> Dict:
        """Оценка на тестовой выборке."""
        test_texts = [
            ("Как лечить пневмонию антибиотиками?", 0),
            ("Врачи скрывают лечение без таблеток", 1),
            ("Рекомендации врача по реабилитации", 0),
            ("Купите сейчас со скидкой 70%!", 1),
            ("Диагностика сахарного диабета", 0),
            ("Гарантированное излечение за 3 дня", 1),
            ("Побочные эффекты антибиотиков", 0),
            ("Секретный рецепт от всех болезней", 1),
        ]
        tp = tn = fp = fn = 0
        for text, true_label in test_texts:
            result = self.moderate(text)
            pred = 0 if result.label == "approved" else 1
            if pred == 1 and true_label == 1: tp += 1
            elif pred == 0 and true_label == 0: tn += 1
            elif pred == 1 and true_label == 0: fp += 1
            else: fn += 1

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
        accuracy = (tp + tn) / len(test_texts)

        return {
            "accuracy": round(accuracy, 3),
            "precision": round(precision, 3),
            "recall": round(recall, 3),
            "f1_score": round(f1, 3),
            "tp": tp, "tn": tn, "fp": fp, "fn": fn,
        }


# ─── Точка входа для тестирования ────────────────────────────────────────────
if __name__ == "__main__":
    moderator = ContentModerator()
    moderator.train()

    test_cases = [
        "Как правильно принимать антибиотики при ангине?",
        "Врачи скрывают это простое средство от рака — лечитесь без врача!",
        "Гарантированное излечение диабета за 3 дня чудо-средством",
        "Купить таблетки со скидкой 70%! Звоните прямо сейчас",
        "Рекомендации по реабилитации после операции на позвоночнике",
    ]

    print("\n" + "=" * 60)
    print("МОДУЛЬ МОДЕРАЦИИ КОНТЕНТА — ТЕСТИРОВАНИЕ")
    print("=" * 60)
    for text in test_cases:
        result = moderator.moderate(text)
        print(f"\nТекст: {text[:55]}...")
        print(f"  Решение:      {result.label.upper()}")
        print(f"  Уверенность:  {result.confidence:.3f}")
        print(f"  Уровень:      {result.level}")
        if result.reasons:
            print(f"  Причины:      {'; '.join(result.reasons[:2])}")

    metrics = moderator.evaluate()
    print("\n" + "=" * 60)
    print("МЕТРИКИ КАЧЕСТВА (тестовая выборка)")
    print("=" * 60)
    for k, v in metrics.items():
        print(f"  {k:15s}: {v}")

