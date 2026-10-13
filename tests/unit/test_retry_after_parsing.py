"""parse_retry_after_seconds() 회귀 가드.

429 예외에서 "언제 다시 시도하면 될지"를 뽑아내는 공용 헬퍼(core/exceptions.py).
Groq/Cerebras 어댑터(openai SDK, httpx 기반) 양쪽에서 쓴다.
"""
import pytest

from src.core.exceptions import parse_retry_after_seconds

pytestmark = pytest.mark.unit


def test_extracts_from_response_headers():
    class Resp:
        headers = {"retry-after": "30"}

    class FakeHTTPError(Exception):
        response = Resp()

    assert parse_retry_after_seconds(FakeHTTPError("429")) == 30.0


def test_extracts_float_seconds_from_headers():
    class Resp:
        headers = {"retry-after": "2.5"}

    class FakeHTTPError(Exception):
        response = Resp()

    assert parse_retry_after_seconds(FakeHTTPError("429")) == 2.5


def test_returns_none_when_no_header_and_no_message_hint():
    assert parse_retry_after_seconds(Exception("rate limited")) is None


def test_returns_none_when_response_has_no_headers_attr():
    class Resp:
        pass

    class FakeHTTPError(Exception):
        response = Resp()

    assert parse_retry_after_seconds(FakeHTTPError("429")) is None


def test_falls_back_to_message_pattern_try_again_in():
    assert parse_retry_after_seconds(Exception("Please try again in 8s.")) == 8.0


def test_falls_back_to_message_pattern_retry_after():
    assert parse_retry_after_seconds(Exception("retry after 4.2s")) == 4.2


def test_header_takes_priority_over_message():
    class Resp:
        headers = {"retry-after": "10"}

    class FakeHTTPError(Exception):
        response = Resp()

    err = FakeHTTPError("try again in 99s")
    assert parse_retry_after_seconds(err) == 10.0


def test_invalid_header_value_falls_through_to_message():
    class Resp:
        headers = {"retry-after": "not-a-number"}

    class FakeHTTPError(Exception):
        response = Resp()

    err = FakeHTTPError("try again in 6s")
    assert parse_retry_after_seconds(err) == 6.0
