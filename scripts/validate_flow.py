#!/usr/bin/env python3
"""HydraLLM + openclaw 통합 검증 스크립트.

흐름:
1. 게이트웨이/서버 생존 확인
2. openclaw 로 웹 탐색이 필요한 질문(seed) 생성. seed 의 자체 답변은 기준으로 쓰지 않는다.
3. Hydra `강제 웹 사용` 경로(has_search=true) 응답을 **라이브 기준 답변** 으로 채택.
   스크래핑 소스가 확인되지 않으면 검증을 중단한다.
4. 나머지 5채널(Hydra api/자동 웹, openclaw 직접/자동/강제) 을 라이브 기준과 교차 판정.
5. `api` 또는 `웹 자동 감지` 채널이 판정에서 불일치(즉, 인텐트 필터의 false negative)
   인 경우 `/v1/admin/intent/keywords/learn` 에 해당 질문을 보내 키워드를 학습·저장한다.

각 외부 호출에는 타임아웃 + 재시도(최대 2회, 재시도도 타임아웃 유지) 가 적용된다.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import time
from typing import Any, Callable

import httpx

HYDRA_BASE = "http://127.0.0.1:8000"
OPENCLAW_AGENT = "mllm-auto"
HYDRA_TIMEOUT = 120.0
OPENCLAW_TIMEOUT = 180
RETRY_LIMIT = 2

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def extract_first_json(text: str) -> str | None:
    text = strip_ansi(text)
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                return text[start : i + 1]
    return None


async def hydra_chat(
    client: httpx.AsyncClient,
    question: str,
    *,
    has_search: bool = False,
    model: str | None = None,
    system_prompt: str | None = None,
) -> str:
    messages: list[dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": question})
    payload: dict[str, Any] = {"messages": messages, "has_search": has_search}
    if model:
        payload["model"] = model
    r = await client.post(f"{HYDRA_BASE}/v1/chat/completions", json=payload)
    r.raise_for_status()
    data = r.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"no choices in response: {data}")
    content = choices[0].get("message", {}).get("content") or ""
    if not content:
        raise RuntimeError("empty content from Hydra")
    return str(content).strip()


def openclaw_agent(message: str, *, timeout: int = OPENCLAW_TIMEOUT) -> str:
    proc = subprocess.run(
        [
            "openclaw",
            "agent",
            "--agent",
            OPENCLAW_AGENT,
            "--message",
            message,
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"openclaw exit {proc.returncode}: {strip_ansi(proc.stderr)[-400:]}"
        )
    # 게이트웨이 폴백 경로에서는 openclaw 가 JSON 결과를 stderr 로 내보낸다.
    # stdout 이 비어 있으면 stderr 까지 검색 대상에 포함한다.
    candidates = [proc.stdout, proc.stderr]
    data: dict[str, Any] | None = None
    last_raw = ""
    for raw_in in candidates:
        raw = strip_ansi(raw_in or "")
        last_raw = raw
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
            break
        except json.JSONDecodeError:
            snippet = extract_first_json(raw)
            if snippet:
                try:
                    data = json.loads(snippet)
                    break
                except json.JSONDecodeError:
                    continue
    if data is None:
        raise RuntimeError(f"cannot parse openclaw JSON: {last_raw[-300:]}")
    payloads = (
        data.get("payloads")
        or data.get("result", {}).get("payloads")
        or []
    )
    if not payloads:
        raise RuntimeError(f"no payloads: {data.get('summary') or list(data.keys())}")
    text = payloads[0].get("text") or ""
    if not text:
        raise RuntimeError("empty openclaw payload text")
    return str(text).strip()


async def with_retry_async(
    fn: Callable[[], Any], label: str, *, attempts: int = RETRY_LIMIT
) -> tuple[bool, str]:
    last_err = ""
    for attempt in range(1, attempts + 1):
        t0 = time.time()
        try:
            result = await asyncio.wait_for(fn(), timeout=HYDRA_TIMEOUT + 10)
            return True, str(result)
        except asyncio.TimeoutError:
            last_err = f"timeout after {time.time()-t0:.1f}s"
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
        print(f"    [retry {attempt}/{attempts}] {label}: {last_err}", file=sys.stderr)
    return False, last_err


def with_retry_sync(
    fn: Callable[[], Any], label: str, *, attempts: int = RETRY_LIMIT
) -> tuple[bool, str]:
    last_err = ""
    for attempt in range(1, attempts + 1):
        try:
            result = fn()
            return True, str(result)
        except subprocess.TimeoutExpired as exc:
            last_err = f"timeout after {exc.timeout}s"
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
        print(f"    [retry {attempt}/{attempts}] {label}: {last_err}", file=sys.stderr)
    return False, last_err


async def ping_gateway(client: httpx.AsyncClient) -> bool:
    try:
        r = await asyncio.wait_for(client.get(f"{HYDRA_BASE}/"), timeout=5)
        return r.status_code == 200 and r.json().get("status") == "online"
    except Exception:
        return False


async def ping_server(client: httpx.AsyncClient) -> bool:
    try:
        r = await asyncio.wait_for(client.get(f"{HYDRA_BASE}/v1/models"), timeout=10)
        return r.status_code == 200
    except Exception:
        return False


def seed_question() -> str:
    """openclaw 를 통해 웹 탐색이 필요한 짧은 질문만 생성한다.

    seed 자체의 답변은 모델 내부 지식에 의존해 시점이 어긋나므로 여기서는 질문만 취한다.
    """
    prompt = (
        "Produce ONE short factual question whose answer requires up-to-date "
        "public web information (news, recent release, latest score, today's "
        "weather, current price, etc.). "
        'Respond ONLY as compact JSON: {"question":"<one sentence question>"} '
        "No markdown, no commentary."
    )
    ok, result = with_retry_sync(
        lambda: openclaw_agent(prompt, timeout=OPENCLAW_TIMEOUT),
        "seed",
    )
    if not ok:
        raise RuntimeError(f"seed 생성 실패: {result}")
    snippet = extract_first_json(result)
    if not snippet:
        raise RuntimeError(f"seed JSON 파싱 실패: {result[:300]}")
    obj = json.loads(snippet)
    q = str(obj.get("question", "")).strip()
    if not q:
        raise RuntimeError(f"seed 질문 누락: {obj}")
    return q


async def fetch_live_reference(
    client: httpx.AsyncClient, question: str
) -> tuple[str, int]:
    """Hydra 강제 웹 경로 응답을 라이브 기준 답변으로 반환.

    두 번째 값은 응답에서 추출된 URL/도메인 개수(신뢰 지표).
    """
    ok, answer = await with_retry_async(
        lambda: hydra_chat(client, question, has_search=True),
        "live-ref",
    )
    if not ok:
        raise RuntimeError(f"라이브 기준 답변 생성 실패: {answer}")
    url_count = len(re.findall(r"https?://[^\s)\"']+", answer))
    return answer, url_count


async def register_missed_query(client: httpx.AsyncClient, question: str) -> list[str]:
    """false negative 로 판정된 질문을 키워드 학습 엔드포인트에 전달."""
    try:
        r = await client.post(
            f"{HYDRA_BASE}/v1/admin/intent/keywords/learn",
            json={"query": question},
            timeout=30,
        )
        r.raise_for_status()
        return list(r.json().get("added", []))
    except Exception as exc:  # noqa: BLE001
        print(f"    [learn] 키워드 학습 실패: {exc}", file=sys.stderr)
        return []


async def cross_validate(
    client: httpx.AsyncClient,
    question: str,
    reference: str,
    candidate: str,
) -> str:
    """Hydra 를 판정자로 호출하여 기준 답변과 후보 답변의 정합성을 한국어로 반환."""
    judge_prompt = (
        "너는 답변 정합성 판정자다. 아래 질문에 대한 [기준 답변] 과 [후보 답변] 이 "
        "사실상 같은 정보를 담고 있는지 평가한 뒤, 정확히 아래 JSON 만 출력하라.\n"
        '{"verdict":"일치|부분일치|불일치","reason":"<한국어 한 문장 요약>"}\n\n'
        f"질문: {question}\n"
        f"[기준 답변]: {reference}\n"
        f"[후보 답변]: {candidate}"
    )
    ok, result = await with_retry_async(
        lambda: hydra_chat(client, judge_prompt, has_search=False),
        "judge",
    )
    if not ok:
        return f"판정 실패({result})"
    snippet = extract_first_json(result)
    if not snippet:
        return f"판정 응답 파싱 실패: {result[:120]}"
    try:
        obj = json.loads(snippet)
    except json.JSONDecodeError:
        return f"판정 JSON 오류: {snippet[:120]}"
    verdict = str(obj.get("verdict", "")).strip() or "?"
    reason = str(obj.get("reason", "")).strip()
    return f"{verdict} ({reason})" if reason else verdict


def preflight_interface_checks() -> list[str]:
    """analyzer 가 gateway/web_context 가 기대하는 공용 API 를 모두 제공하는지 확인.

    이전 회귀(2026-04-20)에서 `provider_priority`, `get_default_model_for_provider`
    가 private → public 경계에서 누락되어 500 이 발생한 사례가 있다. 서버 기동과
    무관하게 정적 검사가 가능하므로 스크립트 초반에 실행한다.
    """
    # 프로젝트 루트를 sys.path 에 추가 (스크립트를 다른 경로에서 실행해도 동작하도록).
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    failures: list[str] = []
    try:
        from src.domain.enums import ProviderType  # noqa: F401
        from src.services.analyzer import ContextAnalyzer
    except Exception as exc:  # noqa: BLE001
        return [f"analyzer 임포트 실패: {exc}"]

    required_methods = [
        "analyze",
        "detect_web_intent",
        "extract_last_user_content",
        "get_provider_limits",
        "get_supported_models_info",
        "get_all_discovered_models_info",
        "get_default_model_for_provider",
        "register_model",
    ]
    required_attrs = ["provider_priority"]

    analyzer = ContextAnalyzer()
    for method in required_methods:
        if not callable(getattr(analyzer, method, None)):
            failures.append(f"analyzer.{method}() 미구현/private")
    for attr in required_attrs:
        if not hasattr(analyzer, attr):
            failures.append(f"analyzer.{attr} 속성 누락")
    return failures


def preflight_web_cost_controls() -> tuple[bool, str]:
    """불필요한 웹 호출을 막는 가드(trivial skip, search-cache key 정규화)가 작동하는지 확인.

    이전 회귀: 'hi' 같은 2글자 쿼리, "다시 검색해줘" 메타 쿼리가 검색을 유발.
    동일 주제 다른 표현("로이터 오늘 뉴스", "로이터 오늘 기준 뉴스" 등) 이 전부
    신규 캐시 미스로 처리되어 같은 세션에서 4회 이상 중복 스크래핑 발생.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from src.services.intent_classifier import IntentClassifier
        from src.services.web_context_service import WebContextService
    except Exception as exc:  # noqa: BLE001
        return False, f"cost-control 모듈 임포트 실패: {exc}"

    trivial_cases = [
        ("hi", True),
        ("로이터 오늘 뉴스 알려줘", False),
        ("더", True),
        ("ok ok", True),
        ("https://example.com", False),
        ("Reuters news today", False),
    ]
    for q, expected in trivial_cases:
        got = IntentClassifier._is_trivial_query(q)
        if got != expected:
            return False, f"trivial 분류 불일치 {q!r}: got={got}, expected={expected}"

    expected_key = WebContextService._normalize_search_key("로이터 오늘 뉴스 알려줘")
    variants = [
        "로이터에서 최근 뉴스 알려줘",
        "로이터 오늘 뉴스 알려줘",
        "로이터 오늘 기준 뉴스 알려줘.",
        "거짓말 말고 로이터 뉴스 지금 실시간 기준으로 알려줘",
    ]
    for v in variants:
        key = WebContextService._normalize_search_key(v)
        if key != expected_key:
            return False, f"search-cache 정규화 불일치 {v!r} -> {key!r} (기대 {expected_key!r})"
    return True, (
        f"trivial={len(trivial_cases)} 케이스 + search-cache 키 {expected_key!r} 정규화 정상"
    )


def preflight_bing_decoder() -> tuple[bool, str]:
    """Bing ck/a click-tracker URL 디코더가 올바르게 동작하는지 확인.

    이전 회귀에서 Bing 이 10 링크를 내려줬지만 모두 bing.com/ck/a?... 로
    감싸져 있어 `in "bing.com"` 필터가 전부 걸러버린 사례가 있었다.
    base64 디코드를 거쳐 외부 도메인으로 복원되는지 정적 검사.
    """
    import base64 as _b64
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from src.services.scraper import _unwrap_bing_redirect
    except Exception as exc:  # noqa: BLE001
        return False, f"_unwrap_bing_redirect 임포트 실패: {exc}"

    target = "https://www.reuters.com/world/today"
    enc = _b64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    tracker = f"https://www.bing.com/ck/a?!&&p=abc&u=a1{enc}&ntb=1"
    decoded = _unwrap_bing_redirect(tracker)
    if decoded != target:
        return False, f"ck/a 디코드 실패 (got={decoded!r})"

    if _unwrap_bing_redirect("https://example.com/p") != "https://example.com/p":
        return False, "외부 URL pass-through 실패"
    if _unwrap_bing_redirect("https://www.bing.com/search?q=foo") is not None:
        return False, "bing 내부 비-ck URL 이 None 이 아님"
    return True, "ck/a 디코드 + pass-through + 내부 rejection 정상"


async def preflight_scraper_smoke() -> tuple[bool, str]:
    """오늘/최근 류 시계열 질의에 대해 scraper 가 1개 이상 링크를 추출하는지 확인.

    DDG → Bing 폴백까지 실제 네트워크로 흘려 end-to-end 경로가 빈 리스트로 떨어지지
    않는지 확인한다. 이전에 Bing 폴백이 10 링크를 뽑고도 tracker URL 필터링으로
    0 으로 떨어진 사일런트 회귀가 있었으므로, 최종 반환이 외부 도메인인지까지 본다.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from src.services.scraper import WebScraper
    except Exception as exc:  # noqa: BLE001
        return False, f"scraper 임포트 실패: {exc}"

    scraper = WebScraper()
    try:
        links = await scraper._search_links_duckduckgo("오늘 뉴스", num_results=3)
        source = "ddg"
        if not links:
            links = await scraper._search_links_bing("오늘 뉴스", num_results=3)
            source = "bing"
        if not links:
            return False, "DDG/Bing 모두 0 링크 — 셀렉터 확인 필요"
        # 반환된 링크가 검색엔진 내부 도메인이 아닌지 확인.
        import urllib.parse as _u
        external = [
            x for x in links
            if "bing.com" not in _u.urlparse(x).netloc
            and "duckduckgo.com" not in _u.urlparse(x).netloc
        ]
        if not external:
            return False, f"{source} 가 {len(links)} 링크 반환했으나 전부 엔진 내부 URL"
        return True, f"{source}: {len(external)} 외부 링크 (예: {external[0]})"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


async def main() -> int:
    print("Preflight:")
    iface_fail = preflight_interface_checks()
    if iface_fail:
        for msg in iface_fail:
            print(f"  [iface] 실패 — {msg}")
        return 2
    print("  [iface] analyzer 공용 인터페이스 정상")

    bing_ok, bing_detail = preflight_bing_decoder()
    if bing_ok:
        print(f"  [bing-decoder] {bing_detail}")
    else:
        print(f"  [bing-decoder] 실패 — {bing_detail}")
        return 2

    cost_ok, cost_detail = preflight_web_cost_controls()
    if cost_ok:
        print(f"  [cost-controls] {cost_detail}")
    else:
        print(f"  [cost-controls] 실패 — {cost_detail}")
        return 2

    ok, detail = await preflight_scraper_smoke()
    if ok:
        print(f"  [scraper] {detail}")
    else:
        print(f"  [scraper] 경고 — {detail}")

    async with httpx.AsyncClient(timeout=HYDRA_TIMEOUT) as client:
        gw_ok = await ping_gateway(client)
        srv_ok = await ping_server(client)
        print(f"게이트웨이: {'정상' if gw_ok else '이상'}")
        print(f"서버: {'정상' if srv_ok else '이상'}")
        if not (gw_ok and srv_ok):
            print("프롬프트 검증: 서버 미기동으로 생략")
            return 1

        print("프롬프트 검증:")
        try:
            question = seed_question()
        except Exception as exc:
            print(f"  [seed] 질문 생성 실패 — {exc}")
            return 2
        print(f"  [seed] 질문: {question}")

        try:
            reference, url_count = await fetch_live_reference(client, question)
        except Exception as exc:
            print(f"  [live-ref] 라이브 기준 답변 실패 — {exc}")
            return 2
        print(
            f"  [live-ref] 기준 답변(출처 URL {url_count}개): "
            f"{reference[:200].replace(chr(10),' ')}"
        )
        if url_count == 0:
            print("  [live-ref] 경고: 스크래핑 URL 을 인용하지 않음. 검증 신뢰도 낮음.")

        hints_web_auto = (
            "Answer using the most current information available. "
            "Search the web if needed. "
        )
        hints_web_force = "WEB_SEARCH_REQUIRED. Use up-to-date online information. "

        channels: list[tuple[str, Callable[[], Any], bool]] = [
            (
                "api",
                lambda: hydra_chat(client, question, has_search=False),
                True,
            ),
            (
                "웹 자동 감지",
                lambda: hydra_chat(
                    client,
                    question,
                    has_search=False,
                    system_prompt=(
                        "Auto-detect whether the question requires web info and "
                        "use it if so."
                    ),
                ),
                True,
            ),
            (
                "강제 웹 사용",
                lambda: hydra_chat(client, question, has_search=True),
                True,
            ),
            (
                "openclaw 연동",
                lambda: openclaw_agent(question),
                False,
            ),
            (
                "openclaw 중 웹 자동 감지",
                lambda: openclaw_agent(hints_web_auto + question),
                False,
            ),
            (
                "openclaw 강제 웹 사용",
                lambda: openclaw_agent(hints_web_force + question),
                False,
            ),
        ]

        # 인텐트 false negative 후보 채널(웹을 쓰지 않았어야 할 채널들).
        intent_channels = {"api", "웹 자동 감지"}
        false_negative_hit = False
        overall_ok = True
        for label, fn, is_async in channels:
            if is_async:
                ok, result = await with_retry_async(fn, label)
            else:
                ok, result = with_retry_sync(fn, label)
            if not ok:
                overall_ok = False
                print(f"  {label}: 실패 — {result}")
                continue
            verdict = await cross_validate(client, question, reference, result)
            print(f"  {label}: {verdict}")
            print(f"     └ 답변 요약: {result[:160].replace(chr(10),' ')}")
            if label in intent_channels and verdict.startswith("불일치"):
                false_negative_hit = True

        if false_negative_hit and url_count > 0:
            added = await register_missed_query(client, question)
            if added:
                print(f"  [learn] 웹 키워드 추가: {added}")
            else:
                print("  [learn] 추출된 새 키워드 없음")

        return 0 if overall_ok else 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
