#!/usr/bin/env python3
"""HydraLLM 관리 대시보드 CLI.

웹 대시보드(/ui admin)의 기능을 터미널에서 제공한다. 표준 라이브러리만 사용.

서브커맨드:
  dashboard  통계·제공자별 사용량·키 쿼터·최근 오류·스크래핑 (기본)
  quota      키별 쿼터(분/일 사용량·한도·리셋·쿨다운·probe)
  sessions   활성 세션 목록
  logs       시스템 로그
  settings   런타임 설정

공통: --url, --admin-key
dashboard/quota: --watch N(주기 갱신), --probe(활성 키 실측, 외부 사용 반영·쿼터 소모)

사용 예:
  python scripts/quota_cli.py                       # dashboard 1회
  python scripts/quota_cli.py dashboard --watch 10
  python scripts/quota_cli.py quota --probe
  python scripts/quota_cli.py logs --limit 50
  python scripts/quota_cli.py sessions
  python scripts/quota_cli.py --url http://host:8000 --admin-key KEY settings
"""
import argparse
import json
import time
import urllib.error
import urllib.request


def fetch(url, path, method="GET", admin_key=None):
    req = urllib.request.Request(url + path, method=method)
    if admin_key:
        req.add_header("X-Admin-Key", admin_key)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _get(url, path, admin_key):
    try:
        return fetch(url, path, admin_key=admin_key)
    except urllib.error.HTTPError as e:
        print(f"  {path} 실패 {e.code}: ADMIN_API_KEY 설정 시 --admin-key 필요")
    except Exception as e:  # noqa: BLE001
        print(f"  {path} 연결 실패({url}): {e}")
    return None


def bar(pct, width=12):
    fill = int(width * min(100, max(0, pct)) / 100)
    return "#" * fill + "." * (width - fill)


def fmt_dur(sec):
    sec = int(sec or 0)
    if sec <= 0:
        return "-"
    h, m = sec // 3600, (sec % 3600) // 60
    return f"{h}h{m}m" if h else f"{m}m{sec % 60}s"


def render_quota(status):
    ks = (status or {}).get("key_statistics", {})
    out = []
    for prov, data in ks.items():
        keys = data.get("keys", [])
        if not keys:
            continue
        out.append(f"\n[{prov.upper()}] active {data.get('active')}/{data.get('total')}")
        out.append(
            f"  {'#':>3} {'stat':<7} {'tier':<12} {'daily(used/limit)':<26} "
            f"{'min':<7} {'reset':<7} {'cool':<7} {'probe':<6} flag"
        )
        for k in keys:
            q = k.get("quota", {}) or {}
            pct = q.get("day_pct", 0)
            daily = f"{q.get('day_used', 0)}/{q.get('day_limit', 0)} {bar(pct)} {pct}%"
            mn = f"{q.get('minute_used', 0)}/{q.get('minute_limit', 0)}"
            lp = q.get("last_probe") or {}
            flags = ("X403" if q.get("is_forbidden") else "") + (
                " !quota" if q.get("is_quota_limit") else ""
            )
            out.append(
                f"  {'#' + str(k['index'] + 1):>3} {k['status']:<7} "
                f"{str(k['tier'])[:12]:<12} {daily:<26} {mn:<7} "
                f"{fmt_dur(q.get('day_reset_in_sec')):<7} "
                f"{fmt_dur(q.get('cooldown_remaining_sec')):<7} "
                f"{lp.get('status', '-'):<6} {flags}"
            )
    return "\n".join(out) if out else "(키 없음)"


def render_dashboard(dash, status):
    if not dash:
        return "(대시보드 데이터 없음)"
    out = ["=== 통계 ==="]
    tok = dash.get("total_tokens", 0)
    req = dash.get("total_requests", 0)
    sc = dash.get("scraping") or {}
    total = sc.get("total", 0)
    success = total - sc.get("fails", 0)
    rate = round(100 * success / total) if total else 0
    out.append(f"  누적 토큰 {tok:,} | 총 요청 {req:,} | "
               f"스크래핑 {rate}% ({success}/{total}, hits {sc.get('hits', 0)})")

    providers = dash.get("providers") or []
    if providers:
        out.append("\n=== 제공자별 사용량 ===")
        for u in providers:
            out.append(
                f"  {u.get('provider', '?')}/{u.get('model', '?')}: "
                f"prompt {u.get('prompt', 0):,} + completion {u.get('completion', 0):,} "
                f"= {u.get('total', 0):,} ({u.get('count', 0)}회)"
            )

    out.append("\n=== 키 헬스 / 쿼터 ===")
    out.append(render_quota(status))

    logs = dash.get("recent_logs") or []
    errors = [x for x in logs if x.get("level") == "ERROR"]
    out.append("\n=== 최근 오류 ===")
    if errors:
        for e in errors[:10]:
            out.append(f"  [{e.get('category', '?')}] {str(e.get('message', ''))[:90]}")
    else:
        out.append("  (오류 없음)")

    scr = dash.get("recent_scraping") or []
    out.append("\n=== 최근 웹 스크래핑 ===")
    if scr:
        for s in scr[:10]:
            tgt = (s.get("query") and f"[검색] {s['query']}") or s.get("url", "")
            out.append(
                f"  {s.get('status', '?'):<10} {str(tgt)[:60]:<60} "
                f"{s.get('chars_count', 0):,}자 {s.get('latency_ms', 0)}ms"
            )
    else:
        out.append("  (기록 없음)")
    return "\n".join(out)


def render_sessions(sessions):
    rows = sessions if isinstance(sessions, list) else (sessions or {}).get("sessions", [])
    if not rows:
        return "(세션 없음)"
    out = [f"=== 활성 세션 {len(rows)}개 ===",
           f"  {'session_id':<24} {'title':<30} {'msgs':>5}  updated"]
    for s in rows:
        out.append(
            f"  {str(s.get('session_id', s.get('id', '?')))[:24]:<24} "
            f"{str(s.get('title', '-'))[:30]:<30} "
            f"{s.get('message_count', 0):>5}  {s.get('updated_at', '-')}"
        )
    return "\n".join(out)


def render_logs(logs, limit):
    rows = logs if isinstance(logs, list) else (logs or {}).get("logs", [])
    if not rows:
        return "(로그 없음)"
    out = [f"=== 최근 로그 {min(len(rows), limit)}개 ==="]
    for x in rows[:limit]:
        out.append(
            f"  {str(x.get('level', '?')):<7} [{x.get('category', '?')}] "
            f"{str(x.get('message', ''))[:100]}"
        )
    return "\n".join(out)


def render_settings(settings):
    if not settings:
        return "(설정 없음)"
    out = ["=== 런타임 설정 ==="]
    for k, v in settings.items():
        sv = str(v)
        kl = k.lower()
        # 민감값만 마스킹(max_tokens 등 오탐 방지 위해 접미사/명시 패턴 사용).
        if (
            kl.endswith(("_key", "_keys", "_token", "_secret"))
            or "api_key" in kl
            or "password" in kl
        ):
            sv = "***"
        out.append(f"  {k}: {sv[:80]}")
    return "\n".join(out)


def run(args):
    cmd = args.cmd or "dashboard"
    probe = getattr(args, "probe", False)
    if probe and cmd in ("dashboard", "quota"):
        try:
            pr = fetch(args.url, "/v1/admin/probe?active=true", "POST", args.admin_key)
            print(f"[probe] 활성 키 실측: {pr.get('active_probe')}")
        except Exception as e:  # noqa: BLE001
            print(f"[probe] 실패: {e}")

    if cmd == "dashboard":
        dash = _get(args.url, "/v1/admin/dashboard", args.admin_key)
        status = _get(args.url, "/v1/admin/status", args.admin_key)
        print(render_dashboard(dash, status))
    elif cmd == "quota":
        print(render_quota(_get(args.url, "/v1/admin/status", args.admin_key)))
    elif cmd == "sessions":
        print(render_sessions(_get(args.url, "/v1/admin/sessions", args.admin_key)))
    elif cmd == "logs":
        lim = getattr(args, "limit", 50)
        print(render_logs(_get(args.url, f"/v1/admin/logs?limit={lim}", args.admin_key), lim))
    elif cmd == "settings":
        print(render_settings(_get(args.url, "/v1/admin/settings", args.admin_key)))


def main():
    ap = argparse.ArgumentParser(description="HydraLLM 관리 대시보드 CLI")
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--admin-key", default=None, help="ADMIN_API_KEY 설정 시 필요")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("dashboard", "quota"):
        sp = sub.add_parser(name)
        sp.add_argument("--watch", type=int, default=0, help="N초마다 갱신")
        sp.add_argument("--probe", action="store_true", help="활성 키 실측(쿼터 소모)")
    sub.add_parser("sessions")
    lg = sub.add_parser("logs")
    lg.add_argument("--limit", type=int, default=50)
    sub.add_parser("settings")
    args = ap.parse_args()

    watch = getattr(args, "watch", 0)
    if watch and watch > 0:
        try:
            while True:
                print("\033[2J\033[H", end="")
                print(f"HydraLLM CLI 대시보드 [{args.cmd or 'dashboard'}] — "
                      f"{time.strftime('%Y-%m-%d %H:%M:%S')} (갱신 {watch}s, Ctrl+C 종료)")
                run(args)
                time.sleep(watch)
        except KeyboardInterrupt:
            print("\n종료")
    else:
        run(args)


if __name__ == "__main__":
    main()
