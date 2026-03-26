#!/usr/bin/env python3
"""
Python SDK Example for Multi-LLM Gateway

This script demonstrates how to interact with the Multi-LLM Gateway
using the standard OpenAI Python library.
"""

from openai import OpenAI

GATEWAY_BASE_URL = "http://localhost:8000/v1"

client = OpenAI(api_key="sk-unused", base_url=GATEWAY_BASE_URL)


def simple_chat():
    """Simple chat completion with automatic model selection."""
    print("=" * 50)
    print("Example 1: Simple Chat (Auto-routing)")
    print("=" * 50)

    response = client.chat.completions.create(
        model="auto",
        messages=[
            {
                "role": "user",
                "content": "What are the benefits of using a multi-LLM gateway?",
            }
        ],
        temperature=0.7,
        max_tokens=500,
    )

    print(f"Executed Model: {response.model}")
    print(f"Token Usage: {response.usage.total_tokens}")
    print(f"Response: {response.choices[0].message.content[:200]}...")
    print()


def web_search_example():
    """Example using web search (grounding) capability."""
    print("=" * 50)
    print("Example 2: Web Search (Grounding)")
    print("=" * 50)

    response = client.chat.completions.create(
        model="auto",
        messages=[
            {
                "role": "user",
                "content": "What is the current price of Bitcoin?",
            }
        ],
        extra_body={"has_search": True},
    )

    print(f"Executed Model: {response.model}")
    print(f"Response: {response.choices[0].message.content[:200]}...")
    print()


def local_agent_example():
    """Example using local agent engines (Ollama, OpenCode, OpenClaw)."""
    print("=" * 50)
    print("Example 3: Local Agent Integration")
    print("=" * 50)

    try:
        response = client.chat.completions.create(
            model="opencode",
            messages=[
                {
                    "role": "user",
                    "content": "Write a python script to list all files in a directory.",
                }
            ],
        )

        print(f"Agent Model: {response.model}")
        print(f"Response: {response.choices[0].message.content[:200]}...")
    except Exception as e:
        print(f"Error (Agent might not be running): {e}")
    print()


def list_models_example():
    """Example retrieving all available models from the gateway."""
    print("=" * 50)
    print("Example 4: Listing Available Models")
    print("=" * 50)

    models = client.models.list()

    print(f"Found {len(models.data)} models.")
    for m in models.data[:10]:
        print(f"- {m.id} (Owned by: {getattr(m, 'owned_by', 'unknown')})")
    print("...")
    print()


if __name__ == "__main__":
    print("\n🚀 Multi-LLM Gateway Python SDK Examples")
    print("Make sure the server is running (python main.py)\n")

    try:
        simple_chat()
        web_search_example()
        local_agent_example()
        list_models_example()
    except Exception as e:
        print(f"Connection Error: {e}")
        print("Is the gateway server running at http://localhost:8000?")

    print("=" * 50)
    print("All examples completed!")
    print("=" * 50)

    print("Example 1: Simple Chat (Auto-routing)")
    print("=" * 50)

    # Using model="auto" lets the gateway pick the best model
    response = client.chat.completions.create(
        model="auto",
        messages=[
            {
                "role": "user",
                "content": "What are the benefits of using a multi-LLM gateway?",
            }
        ],
        temperature=0.7,
        max_tokens=500,
    )

    print(f"Executed Model: {response.model}")
    print(f"Token Usage: {response.usage.total_tokens}")
    print(f"Response: {response.choices[0].message.content[:200]}...")
    print()


def web_search_example():
    """Example using web search (grounding) capability."""
    print("=" * 50)
    print("Example 2: Web Search (Grounding)")
    print("=" * 50)

    # Enable web search via extra_body
    response = client.chat.completions.create(
        model="auto",
        messages=[
            {
                "role": "user",
                "content": "What is the current price of Bitcoin?",
            }
        ],
        extra_body={"has_search": True},
    )

    print(f"Executed Model: {response.model}")
    print(f"Response: {response.choices[0].message.content[:200]}...")
    print()


def local_agent_example():
    """Example using local agent engines (Ollama, OpenCode, OpenClaw)."""
    print("=" * 50)
    print("Example 3: Local Agent Integration")
    print("=" * 50)

    try:
        # You can target local agents directly or via their sub-models
        response = client.chat.completions.create(
            model="opencode",
            messages=[
                {
                    "role": "user",
                    "content": "Write a python script to list all files in a directory.",
                }
            ],
        )

        print(f"Agent Model: {response.model}")
        print(f"Response: {response.choices[0].message.content[:200]}...")
    except Exception as e:
        print(f"Error (Agent might not be running): {e}")
    print()


def list_models_example():
    """Example retrieving all available models from the gateway."""
    print("=" * 50)
    print("Example 4: Listing Available Models")
    print("=" * 50)

    # The models endpoint follows the OpenAI standard
    models = client.models.list()

    print(f"Found {len(models.data)} models.")
    for m in models.data[:10]:  # Print first 10
        print(f"- {m.id} (Owned by: {getattr(m, 'owned_by', 'unknown')})")
    print("...")
    print()


if __name__ == "__main__":
    print("\n🚀 Multi-LLM Gateway Python SDK Examples")
    print("Make sure the server is running (python main.py)\n")

    try:
        simple_chat()
        web_search_example()
        local_agent_example()
        list_models_example()
    except Exception as e:
        print(f"Connection Error: {e}")
        print("Is the gateway server running at http://localhost:8000?")

    print("=" * 50)
    print("All examples completed!")
    print("=" * 50)
