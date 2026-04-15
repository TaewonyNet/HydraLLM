import asyncio
import logging

from src.domain.models import ChatMessage, ChatRequest
from src.services.gateway import Gateway

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AutoModelTest")


async def test_auto_models_functionality():
    gateway = Gateway()

    test_scenarios = [
        {
            "name": "Date and Model Identification",
            "query": "Today's date. Current LLM model name.",
            "expected_keywords": [
                "2026",
                "gemini",
                "groq",
                "llama",
                "flash",
            ],  # 2026 is current year in env
        },
        {
            "name": "Web Retrieval - Letterboxd",
            "query": "Get the 3 latest movies from Letterboxd.",
            "expected_keywords": ["movie", "Letterboxd", "2024", "2025", "2026"],
        },
    ]

    print("\n🚀 Starting AUTO Model Functionality Tests...")

    for scenario in test_scenarios:
        print(f"\n[Scenario: {scenario['name']}]")
        print(f"Query: {scenario['query']}")

        request = ChatRequest(
            model="auto",
            messages=[ChatMessage(role="user", content=scenario["query"])],
            temperature=0.0,
        )

        try:
            response = await gateway.process_request(request)
            content = response.choices[0].message.content
            usage = response.usage or {}

            provider = usage.get("gateway_provider", "unknown")
            model = usage.get("gateway_model", "unknown")

            print(f"✅ Response from {provider} ({model}):")
            print(f"--- CONTENT PREVIEW ---\n{content[:300]}...\n---")

            # 기본적인 정상 동작 확인
            assert len(content) > 10, "Response content too short"
            assert "gateway_provider" in usage, "Missing gateway_provider in usage"

            # 키워드 검증 (상대적임)
            found_keywords = [
                k for k in scenario["expected_keywords"] if k.lower() in content.lower()
            ]
            if found_keywords:
                print(f"✨ Found expected context: {found_keywords}")
            else:
                print("⚠️ Warning: No specific expected keywords found in content.")

        except Exception as e:
            print(f"❌ Scenario FAILED: {e}")
            raise e


if __name__ == "__main__":
    asyncio.run(test_auto_models_functionality())
