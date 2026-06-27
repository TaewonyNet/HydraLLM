"""SessionManager 성능 벤치마크 (수동 실행).

SQLite(WAL) 기반 세션 저장소의 쓰기 처리량·동시성 안전성·읽기 지연·compaction 비용을
실제 임시 DB 로 측정한다. 결정적 단위 테스트가 아니라 성능 관찰용 스크립트다.

실행:
    python scripts/bench_session_manager.py [--writes N] [--concurrent M] [--quick]
"""
import argparse
import asyncio
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.services.session_manager import SessionManager  # noqa: E402


def _new_manager() -> tuple[SessionManager, str]:
    db_path = os.path.join(tempfile.gettempdir(), f"bench_session_{uuid4().hex[:8]}.sqlite")
    return SessionManager(db_path=db_path), db_path


def _cleanup(db_path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = db_path + suffix
        if os.path.exists(p):
            os.unlink(p)


def _fmt(label: str, value: str) -> None:
    print(f"  {label:<38} {value}")


async def bench_sequential_writes(n: int) -> None:
    sm, db_path = _new_manager()
    try:
        sid = await sm.create_session()
        body = "메시지 본문 " + "x" * 200
        durations: list[float] = []
        t0 = time.perf_counter()
        for i in range(n):
            s = time.perf_counter()
            await sm.save_message(sid, "user" if i % 2 == 0 else "assistant", f"{i} {body}")
            durations.append((time.perf_counter() - s) * 1000)
        total = time.perf_counter() - t0
        print(f"\n[1] 순차 save_message x{n}")
        _fmt("총 시간", f"{total:.3f}s")
        _fmt("처리량", f"{n / total:.0f} ops/s")
        _fmt("평균/op", f"{statistics.mean(durations):.2f} ms")
        _fmt("p50 / p95 / max", f"{statistics.median(durations):.2f} / "
             f"{sorted(durations)[int(len(durations) * 0.95)]:.2f} / {max(durations):.2f} ms")
    finally:
        sm.close()
        _cleanup(db_path)


async def bench_concurrent_writes(m: int, sessions: int) -> None:
    sm, db_path = _new_manager()
    try:
        sids = [await sm.create_session() for _ in range(sessions)]
        body = "동시 쓰기 " + "y" * 200
        t0 = time.perf_counter()
        results = await asyncio.gather(
            *[sm.save_message(sids[i % sessions], "user", f"{i} {body}") for i in range(m)],
            return_exceptions=True,
        )
        total = time.perf_counter() - t0
        errors = [r for r in results if isinstance(r, Exception)]
        print(f"\n[2] 동시 save_message x{m} ({sessions} 세션, WAL/per-conn)")
        _fmt("총 시간", f"{total:.3f}s")
        _fmt("처리량", f"{m / total:.0f} ops/s")
        _fmt("오류(예: database is locked)", f"{len(errors)}")
        if errors:
            _fmt("첫 오류", repr(errors[0])[:80])
    finally:
        sm.close()
        _cleanup(db_path)


async def bench_read_latency(sizes: list[int]) -> None:
    sm, db_path = _new_manager()
    try:
        sid = await sm.create_session()
        body = "읽기 지연 측정 " + "z" * 300
        print("\n[3] load_context / load_messages_with_parts 읽기 지연(히스토리 크기별)")
        written = 0
        for size in sizes:
            while written < size:
                await sm.save_message(sid, "user" if written % 2 == 0 else "assistant",
                                      f"{written} {body}")
                written += 1
            # load_context
            samples = []
            for _ in range(5):
                s = time.perf_counter()
                await sm.load_context(sid)
                samples.append((time.perf_counter() - s) * 1000)
            lc = statistics.mean(samples)
            samples = []
            for _ in range(5):
                s = time.perf_counter()
                await sm.load_messages_with_parts(sid)
                samples.append((time.perf_counter() - s) * 1000)
            lm = statistics.mean(samples)
            _fmt(f"size={size:>5}", f"load_context {lc:.2f} ms | with_parts {lm:.2f} ms")
    finally:
        sm.close()
        _cleanup(db_path)


async def bench_overflow_and_compaction(msgs: int) -> None:
    sm, db_path = _new_manager()
    try:
        sid = await sm.create_session()
        # 큰 메시지로 overflow 유발
        for i in range(msgs):
            await sm.save_message(sid, "user" if i % 2 == 0 else "assistant",
                                  f"q{i} " + "w" * 2000)
        s = time.perf_counter()
        overflow = sm.is_overflow(sid)
        est_ms = (time.perf_counter() - s) * 1000

        mock_compressor = MagicMock()
        mock_compressor.compress.return_value = "summary"
        s = time.perf_counter()
        await sm.compact(sid, mock_compressor)
        compact_ms = (time.perf_counter() - s) * 1000

        ctx_after = await sm.load_context(sid)
        print(f"\n[4] overflow 감지 + compaction (메시지 {msgs}개)")
        _fmt("is_overflow", f"{overflow} (추정 {est_ms:.2f} ms)")
        _fmt("compact() 시간", f"{compact_ms:.2f} ms")
        _fmt("compaction 후 context 메시지 수", f"{len(ctx_after)}")
    finally:
        sm.close()
        _cleanup(db_path)


async def main() -> None:
    parser = argparse.ArgumentParser(description="SessionManager 성능 벤치마크")
    parser.add_argument("--writes", type=int, default=1000)
    parser.add_argument("--concurrent", type=int, default=300)
    parser.add_argument("--quick", action="store_true", help="작은 규모로 빠르게")
    args = parser.parse_args()

    writes = 200 if args.quick else args.writes
    concurrent = 100 if args.quick else args.concurrent

    print("=" * 60)
    print(" SessionManager 성능 벤치마크 (SQLite WAL, 임시 DB)")
    print("=" * 60)
    await bench_sequential_writes(writes)
    await bench_concurrent_writes(concurrent, sessions=8)
    await bench_read_latency([100, 500, 1000] if not args.quick else [50, 200])
    await bench_overflow_and_compaction(60 if not args.quick else 30)
    print("\n완료.")


if __name__ == "__main__":
    asyncio.run(main())
