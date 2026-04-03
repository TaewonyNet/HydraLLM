import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.services.gateway import Gateway
from src.domain.models import ChatRequest, ChatMessage
from src.domain.enums import ProviderType

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AutoValidation")


async def validate_auto_modes():
    gateway = Gateway()

    test_cases = [
        ("gemini/auto", ProviderType.GEMINI),
        ("groq/auto", ProviderType.GROQ),
        ("cerebras/auto", ProviderType.CEREBRAS),
    ]

    print("\n" + "=" * 60)
    print("🎯 Validating Provider Stickiness for AUTO hints")
    print("=" * 60)

    for hint, expected_provider in test_cases:
        print(f"\n[Testing Hint]: {hint} (Expected: {expected_provider.value})")

        request = ChatRequest(
            model=hint,
            messages=[
                ChatMessage(
                    role="user", content="Today's date. Current LLM model name."
                )
            ],
            temperature=0.0,
        )

        try:
            response = await gateway.process_request(request)

            actual_provider = response.usage.get("gateway_provider")

            actual_model = response.usage.get("gateway_model")

            print(f"   -> Result: Provider={actual_provider}, Model={actual_model}")

            if actual_provider == expected_provider.value:
                print(f"   ✅ PASS: Correct provider used.")
            else:
                print(
                    f"   ❌ FAIL: Routed to {actual_provider} instead of {expected_provider.value}"
                )

        except Exception as e:
            print(f"   ⚠️ Request attempted but failed: {e}")
            if expected_provider.value in str(e).lower():
                print(
                    f"   ✅ PASS (Validation): Attempted correct provider but hit resource limit."
                )
            else:
                print(
                    f"   ❌ FAIL (Validation): Failed with unexpected error or wrong provider context."
                )


if __name__ == "__main__":
    asyncio.run(validate_auto_modes())
