"""SSRF 방어 회귀 가드.

CLAUDE.md 가 "preserve these checks" 로 명시한 사설/예약 IP 차단을 실제로 행사한다.
IPv4-mapped IPv6(::ffff:127.0.0.1)·CGNAT 우회까지 차단되는지 확인. (services/scraper.py)
"""
import ipaddress

import pytest

from src.services.scraper import (
    _is_blocked_ip,
    _url_resolves_internal,
    _validate_url,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",  # loopback
        "10.1.2.3",  # private
        "172.16.0.1",  # private
        "192.168.1.1",  # private
        "169.254.169.254",  # link-local (cloud metadata)
        "0.0.0.0",  # unspecified
        "100.64.0.1",  # CGNAT
        "::1",  # IPv6 loopback
        "fe80::1",  # IPv6 link-local
        "fc00::1",  # IPv6 ULA
        "::",  # IPv6 unspecified
        "::ffff:127.0.0.1",  # IPv4-mapped 루프백 (우회 시도)
        "::ffff:169.254.169.254",  # IPv4-mapped 메타데이터 (우회 시도)
    ],
)
def test_internal_addresses_blocked(addr):
    assert _is_blocked_ip(ipaddress.ip_address(addr)) is True


@pytest.mark.parametrize("addr", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"])
def test_public_addresses_allowed(addr):
    assert _is_blocked_ip(ipaddress.ip_address(addr)) is False


def test_validate_url_rejects_non_http_scheme():
    with pytest.raises(ValueError, match="scheme"):
        _validate_url("file:///etc/passwd")


def test_validate_url_rejects_loopback_hostname():
    # localhost 는 127.0.0.1 로 resolve → 차단되어야 함.
    with pytest.raises(ValueError, match="internal address"):
        _validate_url("http://localhost/admin")


def test_validate_url_allows_public_host():
    # 공인 도메인은 통과하고 원본 URL 을 반환.
    assert _validate_url("https://example.com/path") == "https://example.com/path"


@pytest.mark.asyncio
async def test_scrape_url_returns_none_on_ssrf_block():
    """scrape_url 은 SSRF 차단 시 (로컬라이즈된 에러 문자열이 아니라) None 을 반환한다(R3)."""
    from src.services.scraper import WebScraper

    assert await WebScraper().scrape_url("http://127.0.0.1/admin") is None


# ── 리다이렉트 재검증 헬퍼(_url_resolves_internal) — 네트워크 의존 회피 위해 IP 리터럴 사용 ──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/x",  # loopback
        "http://169.254.169.254/latest/meta-data",  # 메타데이터
        "http://10.0.0.5/",  # private
        "http://[::1]/",  # IPv6 loopback
        "ftp://example.com/x",  # 비 http(s) 스킴
        "http:///nohost",  # 호스트 없음
        "not-a-url",  # 스킴/호스트 없음
    ],
)
async def test_url_resolves_internal_blocks(url):
    assert await _url_resolves_internal(url) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://93.184.216.34/", "https://8.8.8.8/path"])
async def test_url_resolves_internal_allows_public_ip(url):
    assert await _url_resolves_internal(url) is False
