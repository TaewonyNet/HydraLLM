import asyncio
import os
import sys
import time
from pathlib import Path
from datetime import datetime

# Add src to path
src_path = str(Path(__file__).parent.parent.parent / "src")
if src_path not in sys.path:
    sys.path.insert(0, src_path)

from domain.models import ChatMessage, ChatRequest
from services.gateway import Gateway


async def test_date_aware_priority():
    print("\n" + "=" * 60)
    print("      TESTING DATE-AWARE CONTEXT PRIORITY      ")
    print("=" * 60)

    gateway = Gateway()

    # Simulate two conflicting news with different dates
    # Source 1: Old news
    # Source 2: Latest news
    fake_web_text = (
        "--- SOURCE: https://old-news.com ---\n"
        "[PUBLISHED_DATE: 2024-01-01]\n"
        "Fact: The population of Mars is 0.\n\n"
        "--- SOURCE: https://new-news.com ---\n"
        "[PUBLISHED_DATE: 2026-04-15]\n"
        "Fact: The population of Mars has reached 1,000 as of early 2026.\n"
    )

    request = ChatRequest(
        model="gemini-2.5-flash",
        messages=[
            ChatMessage(
                role="user",
                content="What is the current population of Mars according to the latest data?",
            )
        ],
        session_id="date_test_session",
    )

    # Mock enrichment
    async def mock_enrich(req):
        return [], fake_web_text

    gateway.web_context.enrich_request = mock_enrich

    print("Today is:", datetime.now().strftime("%Y-%m-%d"))
    print("Sending request with conflicting dates...")

    try:
        response = await gateway.process_request(request)
        content = response.choices[0].message.content
        print(f"\nModel Response: {content}")

        if "1,000" in content and "2026" in content:
            print(
                "\n✅ SUCCESS: Model correctly identified and prioritized the 2026 data!"
            )
        elif "0" in content:
            print("\n❌ FAILURE: Model picked the outdated 2024 data.")
        else:
            print("\n⚠️ AMBIGUOUS: Check model response content.")

    except Exception as e:
        print(f"❌ ERROR: {e}")


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    asyncio.run(test_date_aware_priority())
