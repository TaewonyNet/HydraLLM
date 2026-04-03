import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.domain.models import ChatMessage, ChatRequest
from src.services.gateway import Gateway

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("RealtimeTest")


async def test_realtime_capability():
    gateway = Gateway()

    # 1. 오늘 날짜 및 최신 뉴스 관련 쿼리
    # 'auto' 모델을 사용하며, 검색이 필요한 질문을 던짐
    today_str = datetime.now().strftime("%Y-%m-%d")
    query = f"오늘({today_str})의 주요 뉴스 3가지만 요약해줘."

    print(f"\n[Test Query]: {query}")

    request = ChatRequest(
        model="auto",
        messages=[ChatMessage(role="user", content=query)],
        temperature=0.0,
    )

    try:
        # Gateway 프로세스 실행 (내부에서 analyzer -> scraper -> model 순으로 동작)
        response = await gateway.process_request(request)

        content = response.choices[0].message.content
        print("\n=== Response ===")
        print(content)
        print("================")

        # 2. 검증 포인트
        # - 응답에 'REAL-TIME' 또는 'web' 관련 언급이 있는지 (시스템 프롬프트 영향)
        # - 날짜 정보가 포함되어 있는지
        # - 검색 결과가 비어있지 않은지 (로그 확인 병행)

        has_realtime_data = any(
            keyword in content.lower()
            for keyword in ["오늘", "뉴스", "소식", "보도", "날짜"]
        )

        if has_realtime_data:
            print("\n✅ SUCCESS: Model seems to be using real-time information.")
        else:
            print("\n⚠️ WARNING: Response might be based on pre-trained knowledge only.")

        # 로그 파일에서 검색 트리거 확인
        log_content = Path("gateway.log").read_text()
        if "🔍 Performing web search" in log_content:
            print("✅ SUCCESS: Web search was explicitly triggered in logs.")
        else:
            print("❌ FAILURE: Web search was NOT triggered in logs.")

    except Exception as e:
        print(f"\n💥 ERROR during test: {e}")


if __name__ == "__main__":
    asyncio.run(test_realtime_capability())
