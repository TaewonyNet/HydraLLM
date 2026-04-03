import google.generativeai as genai
import os
from dotenv import load_dotenv

load_dotenv()


def discover_gemini_models():
    keys = os.getenv("GEMINI_KEYS", "").split(",")
    if not keys or not keys[0]:
        print("No Gemini keys found in .env")
        return

    key = keys[0].strip()
    genai.configure(api_key=key)

    print(f"Using key: {key[:8]}...")
    print("\n--- Available Gemini Models ---")
    try:
        for m in genai.list_models():
            if "generateContent" in m.supported_generation_methods:
                print(f"ID: {m.name} | Display: {m.display_name}")
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    discover_gemini_models()
