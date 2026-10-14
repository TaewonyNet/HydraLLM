"""라우팅 차별점 검증 — '맥락에 따라 어느 LLM이 더 적합한가'를 결정트리(SPEC §5)로 단언.

analyzer.analyze 가 맥락별로 올바른 공급자/모델을 고르는지 결정적으로 검증한다(라이브 키 불필요).
각 테스트는 SPEC §5가 규정한 '올바른' 선택을 단언하므로, 실패는 라우팅 차별점의 결함을 의미한다.
"""
import pytest

from src.domain.enums import AgentType, ProviderType
from src.domain.models import ChatMessage, ChatRequest
from src.services.analyzer import ContextAnalyzer

pytestmark = pytest.mark.unit

THRESHOLD = 8192

ALL_FREE = {
    ProviderType.GEMINI: {"free"},
    ProviderType.GROQ: {"free"},
    ProviderType.CEREBRAS: {"free"},
}


def _analyzer() -> ContextAnalyzer:
    return ContextAnalyzer(max_tokens_fast_model=THRESHOLD)


def _req(content: str, model: str = "auto", has_search: bool = False) -> ChatRequest:
    return ChatRequest(
        model=model,
        messages=[ChatMessage(role="user", content=content)],
        has_search=has_search,
    )


@pytest.mark.asyncio
async def test_small_query_prefers_groq_fast():
    """작은 쿼리 → 빠른 tier(Groq)."""
    d = await _analyzer().analyze(_req("hi"), available_tiers=ALL_FREE)
    assert d.provider == ProviderType.GROQ


@pytest.mark.asyncio
async def test_groq_routing_never_sends_stale_llama_3_3_70b_versatile():
    """2026-10 실측: llama-3.3-70b-versatile 는 Groq /models 에 없다 — 어떤 Groq 라우팅
    경로도 이 모델명을 보내면 안 된다(라운드로빈으로 여러 번 돌려 분산 경로도 커버)."""
    for _ in range(10):
        d = await _analyzer().analyze(_req("hi"), available_tiers=ALL_FREE)
        if d.provider == ProviderType.GROQ:
            assert d.model_name != "llama-3.3-70b-versatile"
            assert d.model_name == "openai/gpt-oss-120b"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "explicit_model,expected_provider",
    [
        # Groq 실측 네이밍(vendor/model, 2026-10): MovieSpoiler 수집기 v2 가 이 형태로
        # 직접 보낸다 — provider 접두사가 없어도 explicit 모델 힌트로 strict 라우팅돼야
        # 한다(회귀 가드: round-robin/auto 로 떨어져 모델이 바뀌면 안 됨).
        ("openai/gpt-oss-120b", ProviderType.GROQ),
        ("openai/gpt-oss-20b", ProviderType.GROQ),
        ("qwen/qwen3.8-27b", ProviderType.GROQ),
        ("allam-2-7b", ProviderType.GROQ),
        # Cerebras 실측 네이밍(접두사 없는 bare 모델명, 2026-10).
        ("gpt-oss-120b", ProviderType.CEREBRAS),
        ("qwen-3.8-27b", ProviderType.CEREBRAS),
    ],
)
async def test_explicit_vendor_model_name_routes_to_correct_provider(
    explicit_model, expected_provider
):
    """provider 접두사 없는 실제 모델명을 그대로 요청하면, round-robin/auto 로 새지
    않고 올바른 공급자로 explicit 라우팅되며 모델명도 그대로 보존된다."""
    d = await _analyzer().analyze(_req("hi", model=explicit_model), available_tiers=ALL_FREE)
    assert d.provider == expected_provider
    assert d.model_name == explicit_model
    assert d.reason == "model_hint"


def test_known_model_providers_resolution_via_get_target_for_model():
    """_get_target_for_model 이 동적 탐색 레지스트리가 없는 상태에서도(콜드스타트/단위
    테스트) 최소 보조표로 Groq/Cerebras 를 올바르게 가린다."""
    a = _analyzer()
    assert a._get_target_for_model("openai/gpt-oss-120b") == ProviderType.GROQ
    assert a._get_target_for_model("openai/gpt-oss-20b") == ProviderType.GROQ
    assert a._get_target_for_model("qwen/qwen3.8-27b") == ProviderType.GROQ
    assert a._get_target_for_model("allam-2-7b") == ProviderType.GROQ
    assert a._get_target_for_model("gpt-oss-120b") == ProviderType.CEREBRAS
    assert a._get_target_for_model("qwen-3.8-27b") == ProviderType.CEREBRAS


def test_dynamic_registry_takes_priority_over_known_model_table():
    """discover_models() 로 실제 탐색된 결과(_dynamic_targets)가 최소 보조표보다
    우선해야 한다(요구사항: "가능하면 하드코딩 패턴보다 탐색된 /models 목록으로")."""
    a = _analyzer()
    # 보조표상 CEREBRAS 이지만, 실측 탐색 결과가 GROQ 로 등록돼 있다고 가정.
    a.register_model("gpt-oss-120b", ProviderType.GROQ, {})
    assert a._get_target_for_model("gpt-oss-120b") == ProviderType.GROQ


def test_get_equivalent_model_for_provider_maps_known_pairs():
    """413 등으로 provider 전환 시 같은 모델의 다른 공급자 이름으로 변환한다."""
    a = _analyzer()
    assert (
        a.get_equivalent_model_for_provider(
            "openai/gpt-oss-120b", ProviderType.GROQ, ProviderType.CEREBRAS
        )
        == "gpt-oss-120b"
    )
    assert (
        a.get_equivalent_model_for_provider(
            "qwen/qwen3.8-27b", ProviderType.GROQ, ProviderType.CEREBRAS
        )
        == "qwen-3.8-27b"
    )
    assert (
        a.get_equivalent_model_for_provider(
            "gpt-oss-120b", ProviderType.CEREBRAS, ProviderType.GROQ
        )
        == "openai/gpt-oss-120b"
    )


def test_get_equivalent_model_for_provider_falls_back_when_no_pair_known():
    """동일 모델이 그 공급자에 없으면(예: Groq 전용 openai/gpt-oss-20b·allam-2-7b 를
    Cerebras 로) 기본 모델로 대체한다(기존 get_default_model_for_provider 와 동일)."""
    a = _analyzer()
    assert a.get_equivalent_model_for_provider(
        "openai/gpt-oss-20b", ProviderType.GROQ, ProviderType.CEREBRAS
    ) == a.get_default_model_for_provider(ProviderType.CEREBRAS)
    assert a.get_equivalent_model_for_provider(
        "allam-2-7b", ProviderType.GROQ, ProviderType.CEREBRAS
    ) == a.get_default_model_for_provider(ProviderType.CEREBRAS)


@pytest.mark.asyncio
async def test_small_query_falls_to_cerebras_when_no_groq():
    """Groq 키 없으면 Cerebras 로 fast tier."""
    tiers = {ProviderType.GEMINI: {"free"}, ProviderType.CEREBRAS: {"free"}}
    d = await _analyzer().analyze(_req("hi"), available_tiers=tiers)
    assert d.provider == ProviderType.CEREBRAS


@pytest.mark.asyncio
async def test_large_context_routes_to_gemini():
    """대용량 컨텍스트(≥임계값) → Gemini(대용량 윈도우)."""
    big = "word " * 9000
    d = await _analyzer().analyze(_req(big), available_tiers=ALL_FREE)
    assert d.provider == ProviderType.GEMINI


@pytest.mark.asyncio
async def test_images_route_to_gemini_vision():
    """멀티모달(구조화된 image_url 파트)은 토큰 기반 fast tier 보다 우선해 Gemini Vision 으로(SPEC §5 step 3 > 5)."""
    req = ChatRequest(
        model="auto",
        messages=[
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": "이 이미지 설명해줘"},
                    {"type": "image_url", "image_url": {"url": "http://x/a.png"}},
                ],
            )
        ],
    )
    d = await _analyzer().analyze(req, available_tiers=ALL_FREE)
    assert d.provider == ProviderType.GEMINI


@pytest.mark.asyncio
async def test_plain_text_mentioning_image_url_is_not_multimodal():
    """평문에 'image_url' 단어만 있으면 멀티모달 아님 → fast tier(R11 오탐 방지)."""
    d = await _analyzer().analyze(
        _req("how do I use the image_url field?"), available_tiers=ALL_FREE
    )
    assert d.provider in (ProviderType.GROQ, ProviderType.CEREBRAS)


@pytest.mark.asyncio
async def test_web_search_intent_no_longer_forces_gemini():
    """웹 검색 의도(작은 쿼리)는 더 이상 Gemini 를 강제하지 않는다 — 웹 컨텍스트는 주입
    되므로 fast tier(Groq/Cerebras)로 라우팅해 Gemini 쏠림을 완화한다(de-funnel)."""
    d = await _analyzer().analyze(_req("hi", has_search=True), available_tiers=ALL_FREE)
    assert d.provider in (ProviderType.GROQ, ProviderType.CEREBRAS)


@pytest.mark.asyncio
async def test_fast_tier_round_robin_distributes_load():
    """동일 analyzer 의 연속 작은 쿼리는 Groq↔Cerebras 로 분산된다(라운드로빈)."""
    a = _analyzer()
    picks = [
        (await a.analyze(_req("hi"), available_tiers=ALL_FREE)).provider
        for _ in range(4)
    ]
    # 두 fast tier 가 모두 한 번 이상 선택되어야 한다(단독 쏠림 아님).
    assert ProviderType.GROQ in picks
    assert ProviderType.CEREBRAS in picks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hint,expected",
    [
        ("gemini/auto", ProviderType.GEMINI),
        ("cerebras/auto", ProviderType.CEREBRAS),
        ("groq/auto", ProviderType.GROQ),
    ],
)
async def test_provider_scoped_auto_sticks(hint, expected):
    """provider-scoped auto 는 해당 공급자에 고정된다."""
    d = await _analyzer().analyze(_req("hi", model=hint), available_tiers=ALL_FREE)
    assert d.provider == expected


def test_provider_keyword_precedence_over_family():
    """provider 키워드(cerebras/groq)가 일반 family(llama)보다 우선."""
    a = _analyzer()
    assert a._get_target_for_model("cerebras-llama-3.3-70b") == ProviderType.CEREBRAS
    assert a._get_target_for_model("llama-3.3-70b") == ProviderType.GROQ
    assert a._get_target_for_model("ollama-qwen") == AgentType.OLLAMA


@pytest.mark.asyncio
async def test_premium_model_only_with_premium_keys():
    """PREMIUM 키가 있을 때만 프리미엄 모델 선택(tier awareness)."""
    a = _analyzer()
    a._default_premium_model = "gemini-1.5-pro"
    a._default_free_model = "gemini-1.5-flash"
    big = "word " * 9000
    d_prem = await a.analyze(_req(big), available_tiers={ProviderType.GEMINI: {"premium"}})
    assert d_prem.model_name == "gemini-1.5-pro"
    d_free = await a.analyze(_req(big), available_tiers={ProviderType.GEMINI: {"free"}})
    assert d_free.model_name == "gemini-1.5-flash"


# ── 키 부재 시 라우팅 (analyzer 레벨: 가용 공급자를 선호, 없으면 클라우드 default) ──


@pytest.mark.asyncio
async def test_small_query_uses_available_provider_when_groq_absent():
    """작은 쿼리라도 Groq/Cerebras 키가 없으면 가용한 Gemini 로 간다."""
    d = await _analyzer().analyze(
        _req("hi"), available_tiers={ProviderType.GEMINI: {"free"}}
    )
    assert d.provider == ProviderType.GEMINI


@pytest.mark.asyncio
async def test_large_query_prefers_gemini_even_without_gemini_key():
    """대용량은 auto 경로에서 항상 Gemini(대용량 윈도우)를 선호한다 — Gemini 키가 없어도
    analyzer 는 Gemini 결정을 내고, 실제 키 부재 폴백은 ResilienceManager 가 처리한다.
    (analyzer 가용성 폴백은 small/None-preferred 경로에서만 적용된다.)"""
    big = "word " * 9000
    d = await _analyzer().analyze(
        _req(big), available_tiers={ProviderType.GROQ: {"free"}}
    )
    assert d.provider == ProviderType.GEMINI


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model,expected_strict",
    [
        ("gemini/gemini-1.5-pro", True),  # provider/model 명시
        ("gemini/auto", True),  # provider-scoped auto
        ("groq/llama-3.3-70b", True),
        ("local-agent/ollama/llama3", True),
        ("auto", False),  # 순수 auto
        ("default", False),
        ("gpt-4o", False),  # 교차벤더 별칭(슬래시 없음)
        ("gemini-1.5-pro", False),  # provider-scope 아님(슬래시 없음)
    ],
)
async def test_decision_strict_set_from_original_hint(model, expected_strict):
    """analyzer 가 원본 힌트 기준으로 decision.strict 를 설정한다(S1: gateway 의 request.model
    덮어쓰기와 무관하게 strict 의도가 보존됨)."""
    d = await _analyzer().analyze(_req("hi", model=model), available_tiers=ALL_FREE)
    assert d.strict is expected_strict


@pytest.mark.asyncio
async def test_no_keys_returns_cloud_default_decision():
    """가용 키가 전무하면 analyzer 는 클라우드 default 결정을 낸다(실제 키 부재는
    ResilienceManager 가 폴백/Ollama 로 처리). 작은 쿼리→Groq, 대용량→Gemini."""
    small = await _analyzer().analyze(_req("hi"), available_tiers={})
    assert small.provider == ProviderType.GROQ
    big = await _analyzer().analyze(_req("word " * 9000), available_tiers={})
    assert big.provider == ProviderType.GEMINI
