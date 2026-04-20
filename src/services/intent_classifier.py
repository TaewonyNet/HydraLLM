import json
import logging
import math
import re
from typing import Any

import httpx

from src.i18n import t, t_list
from src.services.keyword_store import KeywordStore, detect_language

logger = logging.getLogger(__name__)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class IntentClassifier:
    def __init__(
        self,
        ollama_base_url: str = "http://localhost:11434",
        model: str = "bge-m3:latest",
        keyword_store: KeywordStore | None = None,
        extraction_model: str | None = None,
    ) -> None:
        self._base_url = ollama_base_url.rstrip("/")
        self._model = model
        self._positive_embeddings: list[list[float]] = []
        self._negative_embeddings: list[list[float]] = []
        self._ready = False
        self._threshold: float = 0.65
        self._keyword_store = keyword_store
        self._extraction_model = extraction_model

    async def initialize(self) -> None:
        positive = t_list("intent.examples_need_search")
        negative = t_list("intent.examples_no_search")
        threshold_val = t("intent.threshold")
        try:
            if isinstance(threshold_val, int | float):
                self._threshold = float(threshold_val)
            elif isinstance(threshold_val, str):
                self._threshold = float(threshold_val)
        except (ValueError, TypeError):
            logger.warning(
                f"Invalid threshold value: {threshold_val}, using default {self._threshold}"
            )

        try:
            self._positive_embeddings = await self._embed_batch(positive)
            self._negative_embeddings = await self._embed_batch(negative)
            if self._positive_embeddings and self._negative_embeddings:
                self._ready = True
                logger.info(
                    f"IntentClassifier ready: {len(self._positive_embeddings)} positive, "
                    f"{len(self._negative_embeddings)} negative examples"
                )
            else:
                logger.warning(
                    "IntentClassifier: no embeddings computed, falling back to disabled"
                )
        except Exception as e:
            logger.warning(f"IntentClassifier init failed (will be disabled): {e}")

    async def needs_web_search(self, query: str) -> bool:
        if not query.strip():
            return False

        # 키워드 저장소 우선 매칭: URL/고신뢰 시그널은 즉시 True.
        if self._keyword_store is not None:
            match = self._keyword_store.matches(query)
            if match:
                logger.debug(f"Intent keyword match: '{match}' → web required")
                return True

        # Trivial/meta 쿼리는 embedding classifier 의 false positive 가 잦다.
        # 길이·토큰 수·대표 meta 어휘 기반으로 짧고 독립성 낮은 쿼리는 early-return.
        if self._is_trivial_query(query):
            logger.debug(f"Intent trivial-query skip: {query!r}")
            return False

        if not self._ready:
            return False

        try:
            query_emb = await self._embed(query)
            if not query_emb:
                return False

            pos_scores = [
                _cosine_similarity(query_emb, emb) for emb in self._positive_embeddings
            ]
            neg_scores = [
                _cosine_similarity(query_emb, emb) for emb in self._negative_embeddings
            ]

            avg_pos = sum(pos_scores) / len(pos_scores) if pos_scores else 0
            avg_neg = sum(neg_scores) / len(neg_scores) if neg_scores else 0
            max_pos = max(pos_scores) if pos_scores else 0

            needs_search = max_pos >= self._threshold and avg_pos > avg_neg

            logger.debug(
                f"Intent classification: max_pos={max_pos:.3f}, "
                f"avg_pos={avg_pos:.3f}, avg_neg={avg_neg:.3f}, "
                f"result={needs_search}"
            )
            return needs_search
        except Exception as e:
            logger.warning(f"Intent classification failed: {e}")
            return False

    @staticmethod
    def _is_trivial_query(query: str) -> bool:
        """embedding classifier 를 돌릴 가치가 없는 쿼리인지 판단.

        기준:
        - URL 포함 시 False (웹 가치 있음)
        - 한/영 토큰 < 2 개
        - 총 한글·영문자 수 < 4
        - 대표적인 meta/acknowledgement 표현만 있는 경우

        keyword_store 에서 이미 매칭된 경우에는 호출되지 않으므로, 이 함수가 True 를
        반환하면 웹 검색은 전적으로 사용자 명시(has_search/URL) 가 있을 때만 수행된다.
        """
        q = query.strip()
        if not q:
            return True
        if re.search(r"https?://", q):
            return False
        tokens = re.findall(r"[\w가-힣]+", q)
        if len(tokens) < 2:
            return True
        alpha_len = len(re.findall(r"[가-힣A-Za-z]", q))
        if alpha_len < 4:
            return True
        meta_terms = {
            "ok",
            "okay",
            "응",
            "네",
            "음",
            "아",
            "hi",
            "hello",
            "hey",
            "yes",
            "no",
            "thanks",
            "thank",
            "다시",
            "또",
            "아니",
            "맞아",
            "좋아",
            "그래",
            "계속",
            "더",
            "다음",
        }
        lowered = [t.lower() for t in tokens]
        if all(tok in meta_terms for tok in lowered):
            return True
        return False

    async def _embed(self, text: str) -> list[float]:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{self._base_url}/api/embed",
                json={"model": self._model, "input": text},
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            embeddings: list[list[float]] = data.get("embeddings", [])
            if embeddings:
                return embeddings[0]
            return []

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float]] = []
        for text in texts:
            emb = await self._embed(text)
            if emb:
                results.append(emb)
        return results

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def keyword_store(self) -> KeywordStore | None:
        return self._keyword_store

    async def learn_from_missed_query(self, query: str) -> list[str]:
        """웹 불요로 판정했으나 실제로는 웹이 필요했던 쿼리에서 핵심어를 추출해 저장.

        로컬 LLM(Ollama) 에 간결한 JSON 추출 프롬프트를 보내 1~3개의 짧은 키워드를 받는다.
        LLM 이 실패하면 정규식 기반 단순 폴백을 사용한다.
        """
        if self._keyword_store is None or not query.strip():
            return []
        lang = detect_language(query)
        keywords = await self._extract_keywords_via_llm(query, lang)
        if not keywords:
            keywords = self._fallback_keywords(query, lang)
        if not keywords:
            return []
        added = self._keyword_store.add(lang, keywords)
        if added:
            logger.info(f"Learned web keywords ({lang}): {added}")
        return added

    async def _extract_keywords_via_llm(self, query: str, lang: str) -> list[str]:
        model = self._extraction_model or self._ollama_default_chat_model()
        if not model:
            return []
        lang_hint = "Korean" if lang == "ko" else "English"
        prompt = (
            f"You will receive a user query written in {lang_hint}. The query was "
            "mis-classified as not needing a web search, but web information turned "
            "out to be required. Extract 1 to 3 SHORT phrases (2~4 words each) from "
            "the query that indicate a need for up-to-date or external web "
            "information. Output ONLY a compact JSON array of strings, no prose, "
            "no markdown.\n"
            f"Query: {query}\n"
            "JSON:"
        )
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    f"{self._base_url}/api/generate",
                    json={
                        "model": model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {"temperature": 0.0},
                    },
                )
                resp.raise_for_status()
                raw = resp.json().get("response", "")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Keyword extraction LLM call failed: {exc}")
            return []
        return self._parse_keyword_json(raw)

    @staticmethod
    def _parse_keyword_json(raw: str) -> list[str]:
        if not raw:
            return []
        start = raw.find("[")
        end = raw.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return []
        snippet = raw[start : end + 1]
        try:
            parsed = json.loads(snippet)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        out: list[str] = []
        for item in parsed:
            if isinstance(item, str):
                kw = item.strip()
                if kw:
                    out.append(kw)
        return out[:3]

    @staticmethod
    def _fallback_keywords(query: str, lang: str) -> list[str]:
        """LLM 호출 실패 시 사용하는 보수적인 폴백.

        시그널 단어가 있으면 그 주변 토큰을 짧은 구문으로 묶어 한두 개만 반환한다.
        """
        signals_en = (
            "today",
            "latest",
            "current",
            "recent",
            "now",
            "price",
            "score",
            "news",
            "release",
            "update",
        )
        signals_ko = (
            "오늘",
            "최근",
            "현재",
            "지금",
            "실시간",
            "가격",
            "시세",
            "뉴스",
            "최신",
        )
        signals = signals_ko if lang == "ko" else signals_en
        low = query.lower() if lang == "en" else query
        tokens = re.findall(r"[\w가-힣']+", low)
        out: list[str] = []
        for i, tok in enumerate(tokens):
            if tok in signals:
                # 시그널 단어 + 뒤 토큰 1개 (2단어 구문). 다음 토큰이 없으면 단일어 사용.
                next_tok = tokens[i + 1] if i + 1 < len(tokens) else ""
                phrase = f"{tok} {next_tok}".strip() if next_tok else tok
                if 2 <= len(phrase) <= 60 and phrase not in out:
                    out.append(phrase)
            if len(out) >= 2:
                break
        return out

    def _ollama_default_chat_model(self) -> str | None:
        """추출용 로컬 LLM 모델 이름. 호출 전 임베딩 모델명에서 유추."""
        if self._extraction_model:
            return self._extraction_model
        # 임베딩 모델과 동일 호스트에서 작은 범용 LLM 이 있다고 가정하지 않는다.
        # 호출자 측에서 extraction_model 을 주입하는 것이 권장되지만, 없을 경우
        # 대체로 존재할 법한 소형 모델명을 순차 시도한다.
        return None
