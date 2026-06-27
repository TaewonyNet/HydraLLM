import asyncio
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from src.domain.models import ChatMessage, ChatRequest
from src.services.gateway import Gateway

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AutoModelTest")


@pytest.mark.asyncio
async def test_auto_variants():
    gateway = Gateway()

    model_variants = ["auto", "gemini/auto", "groq/auto", "cerebras/auto"]
    test_queries = [
        "Today's date. Current LLM model name.",
        "Get the 3 latest movies from Letterboxd.",
    ]

    results = []

    for model_hint in model_variants:
        print("\n" + "=" * 50)
        print(f"🚀 Testing Model Variant: {model_hint}")
        print("=" * 50)

        for query in test_queries:
            print(f"\n[Query]: {query}")

            request = ChatRequest(
                model=model_hint,
                messages=[ChatMessage(role="user", content=query)],
                temperature=0.0,
            )

            try:
                response = await gateway.process_request(request)

                content = response.choices[0].message.content

                provider = response.usage.get("gateway_provider", "unknown")
                actual_model = response.usage.get("gateway_model", "unknown")

                print("✅ Success!")
                print(f"   - Provider: {provider}")
                print(f"   - Resolved Model: {actual_model}")
                print(f"   - Response Snippet: {content[:150]}...")

                results.append(
                    {
                        "hint": model_hint,
                        "query": query,
                        "status": "success",
                        "provider": provider,
                        "model": actual_model,
                    }
                )

            except Exception as e:
                print(f"❌ Failed: {e}")
                results.append(
                    {
                        "hint": model_hint,
                        "query": query,
                        "status": "failed",
                        "error": str(e),
                    }
                )

    print("\n" + "=" * 50)
    print("📊 Final Summary of Auto Variants Test")
    print("=" * 50)
    for r in results:
        status_icon = "✅" if r["status"] == "success" else "❌"
        line = f"{status_icon} [{r['hint']}] -> {r.get('provider', 'N/A')}/{r.get('model', 'N/A')}"
        if r["status"] == "failed":
            line += f" (Error: {r['error'][:50]}...)"
        print(line)


if __name__ == "__main__":
    asyncio.run(test_auto_variants())
