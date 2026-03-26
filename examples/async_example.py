#!/usr/bin/env python3
"""
Asynchronous Request Example for Multi-LLM Gateway

This script demonstrates how to send multiple requests concurrently
using aiohttp and asyncio.
"""

import asyncio

import aiohttp

GATEWAY_URL = "http://localhost:8000/v1/chat/completions"


async def send_request(session: aiohttp.ClientSession, prompt: str, example_num: int):
    """Sends a single asynchronous request."""
    print(f"Sending Example {example_num}...")

    payload = {
        "model": "auto",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 200,
    }

    try:
        async with session.post(
            GATEWAY_URL, json=payload, headers={"Content-Type": "application/json"}
        ) as response:
            result = await response.json()

            print(f"✅ Example {example_num} Completed!")
            print(f"   Model: {result['model']}")
            print(f"   Usage: {result['usage']['total_tokens']} tokens")
            print()

            return result

    except Exception as e:
        print(f"❌ Example {example_num} Failed: {e}")
        return None


async def main():
    """Executes multiple requests concurrently."""
    print("=" * 50)
    print("Concurrent Request Testing")
    print("=" * 50)
    print()

    async with aiohttp.ClientSession() as session:
        prompts = [
            "What is FastAPI?",
            "Explain Python async/await.",
            "Who won the last World Cup?",
            "Write a poem about robots.",
            "Compare Groq vs Cerebras.",
        ]

        tasks = [send_request(session, p, i + 1) for i, p in enumerate(prompts)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        print("=" * 50)
        print("Summary:")
        print("=" * 50)
        successful = sum(1 for r in results if r and not isinstance(r, Exception))
        print(f"Successful Requests: {successful}/{len(prompts)}")


if __name__ == "__main__":
    print("🚀 Starting async batch process...\n")
    asyncio.run(main())
    print("\n👋 Done!")
