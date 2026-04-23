"""프롬프트/컨텍스트 최적화(ContextCompressor) 검증 단위 테스트.

대상: src/services/compressor.py::ContextCompressor
    - 짧은 입력은 그대로 통과
    - LLMLingua 미설치 시 simple extractive fallback 동작
    - LLMLingua 실패 시 simple fallback 로 자동 복귀
    - target_token 기반 축소 (앞/뒤 핵심 보존, 중간 생략 마커 삽입)
    - LLMLingua 설치 시 해당 API 로 프롬프트 압축 위임
"""

from unittest.mock import MagicMock

import pytest

from src.services.compressor import ContextCompressor

pytestmark = pytest.mark.unit


class TestShortInputPassThrough:
    def test_short_text_returned_as_is(self) -> None:
        c = ContextCompressor()
        short = "짧은 문장입니다. 길이 500자 미만이면 원문 그대로."
        assert c.compress(short) == short

    def test_empty_text_returned_as_is(self) -> None:
        c = ContextCompressor()
        assert c.compress("") == ""


class TestSimpleCompressionFallback:
    def test_long_text_is_truncated_with_marker(self) -> None:
        c = ContextCompressor()
        # model 유무와 무관하게 simple 경로를 검증
        c.model = None

        big = "ABCDEFG " * 2000  # 16000 chars
        out = c.compress(big, target_token=500)  # max_chars = 500 * 4 = 2000

        assert len(out) < len(big)
        assert "content compressed" in out
        # 시작과 끝이 보존되어야 함
        assert out.startswith("ABCDEFG")
        assert out.rstrip().endswith("ABCDEFG")

    def test_simple_compression_not_triggered_under_limit(self) -> None:
        c = ContextCompressor()
        c.model = None
        text = "X" * 1500  # > 500 so compress enters the path; target_token=1000 → max_chars=4000
        out = c.compress(text, target_token=1000)
        # 1500 <= 4000 이라 _simple_compress는 원문을 그대로 반환
        assert out == text

    def test_target_token_controls_output_size(self) -> None:
        c = ContextCompressor()
        c.model = None
        text = "Y" * 8000
        out_small = c.compress(text, target_token=200)
        out_large = c.compress(text, target_token=1500)
        assert len(out_small) < len(out_large) < len(text)


class TestLLMLinguaDelegation:
    def test_delegates_to_llmlingua_when_available(self) -> None:
        c = ContextCompressor()
        fake_model = MagicMock()
        fake_model.compress_prompt.return_value = {
            "compressed_prompt": "COMPRESSED"
        }
        c.model = fake_model

        big = "Some quite long context " * 60  # > 500 chars
        out = c.compress(big, instruction="summarize", target_token=300)

        assert out == "COMPRESSED"
        fake_model.compress_prompt.assert_called_once()
        _, kwargs = fake_model.compress_prompt.call_args
        assert kwargs["instruction"] == "summarize"
        assert kwargs["target_token"] == 300
        assert kwargs["rank_method"] == "longllmlingua"

    def test_falls_back_to_simple_when_llmlingua_raises(self) -> None:
        c = ContextCompressor()
        fake_model = MagicMock()
        fake_model.compress_prompt.side_effect = RuntimeError("boom")
        c.model = fake_model

        big = "Z" * 12000
        out = c.compress(big, target_token=500)  # max_chars 2000

        # simple fallback 흔적
        assert "content compressed" in out
        assert len(out) < len(big)


class TestIntegrationWithWebContext:
    """ContextCompressor가 반환한 텍스트가 웹 컨텍스트에 사용 가능한 문자열임을 확인."""

    def test_output_is_always_str(self) -> None:
        c = ContextCompressor()
        c.model = None
        out = c.compress("text " * 1000, instruction="relevant to query")
        assert isinstance(out, str)
        assert len(out) > 0
