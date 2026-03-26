import asyncio

import google.generativeai as genai

from src.core.config import settings


async def probe_my_key():
    api_keys = settings.gemini_keys
    if isinstance(api_keys, str):
        api_keys = [k.strip() for k in api_keys.split(",") if k.strip()]

    if not api_keys:
        print("No keys found in .env")
        return

    # Use the first key for detailed probing
    key = api_keys[0]
    print(f"--- Probing Key: {key[:8]}... ---")
    genai.configure(api_key=key)

    available_models = []
    for m in genai.list_models():
        if "generateContent" in m.supported_generation_methods:
            available_models.append(m.name.replace("models/", ""))

    print(f"Found {len(available_models)} candidate models. Testing actual access...")

    results = {"free": [], "paid_only": [], "error": []}

    for model_name in available_models:
        print(f"Testing {model_name}...", end=" ", flush=True)
        try:
            model = genai.GenerativeModel(model_name)
            # Try a very small generation
            await model.generate_content_async(
                "hi", generation_config={"max_output_tokens": 1}
            )
            print("✅ [FREE/AVAILABLE]")
            results["free"].append(model_name)
        except Exception as e:
            if "limit: 0" in str(e) or "429" in str(e):
                print("❌ [PAID ONLY/NO QUOTA]")
                results["paid_only"].append(model_name)
            else:
                print(f"⚠️ [OTHER ERROR: {str(e)[:50]}...]")
                results["error"].append(model_name)

    print("\n--- PROBE SUMMARY ---")
    print(f"FREE MODELS (Usable now): {', '.join(results['free'])}")
    print(f"PAID MODELS (Need billing): {', '.join(results['paid_only'])}")


if __name__ == "__main__":
    asyncio.run(probe_my_key())
