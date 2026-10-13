"""Gateway 가 Groq/Cerebras 어댑터를 생성할 때 더 이상 없는 모델명을 하드코딩하지
않는지 확인하는 회귀 가드.

배경(2026-10 실측 /models): Groq 는 llama-3.3-70b-versatile 등 llama/deepseek 계열을
더 이상 제공하지 않는다(openai/gpt-oss-120b·openai/gpt-oss-20b·qwen/qwen3.8-27b·
allam-2-7b 만 실존). Cerebras 는 llama3.1-70b-versatile 을 제공하지 않는다
(gpt-oss-120b·qwen-3.8-27b 만 실존). gateway.py::_get_provider_adapter 가 어댑터를
구성할 때 이 사실을 반영하는지 검증한다.
"""
import pytest

from src.adapters.providers.cerebras import DEFAULT_CEREBRAS_MODEL
from src.adapters.providers.openai_compat import DEFAULT_GROQ_MODEL
from src.domain.enums import ProviderType
from src.services.gateway import Gateway

pytestmark = pytest.mark.unit


def test_groq_adapter_has_no_stale_fixed_default_model():
    """Groq 어댑터는 고정 default_model 로 llama-3.3-70b-versatile 을 박아넣지 않고,
    discovery 로 검증받는 preferred_default_model 만 넘긴다."""
    gateway = Gateway()
    adapter = gateway._get_provider_adapter(ProviderType.GROQ, "fake-groq-key")

    assert adapter.default_model is None
    assert adapter.preferred_default_model == DEFAULT_GROQ_MODEL == "openai/gpt-oss-120b"


def test_cerebras_adapter_default_is_gpt_oss_120b_constant():
    gateway = Gateway()
    adapter = gateway._get_provider_adapter(ProviderType.CEREBRAS, "fake-cerebras-key")

    assert DEFAULT_CEREBRAS_MODEL == "gpt-oss-120b"
    # CerebrasAdapter 는 생성 시점엔 아직 미해결(지연 탐색)이고, 탐색 실패 시에만
    # DEFAULT_CEREBRAS_MODEL 로 떨어진다(test_cerebras_adapter.py 에서 상세 검증).
    assert adapter._resolved_default_model is None
