#!/bin/bash
# Multi-LLM Gateway curl Examples
# This script demonstrates how to test the Gateway API from the terminal.

GATEWAY_URL="http://localhost:8000/v1/chat/completions"

echo "=============================================="
echo "  Multi-LLM Gateway curl Examples"
echo "=============================================="
echo ""

# Example 1: Simple Chat (Auto-routing)
echo "Example 1: Simple Chat (Auto-routing)"
echo "------------------------------------"
curl -s -X POST "$GATEWAY_URL" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [{"role": "user", "content": "Hello!"}],
    "temperature": 0.7
  }' | python3 -m json.tool
echo ""

# Example 2: Web Search (Grounding)
echo "Example 2: Web Search (Grounding)"
echo "--------------------------------"
curl -s -X POST "$GATEWAY_URL" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [{"role": "user", "content": "What is the latest price of Ethereum?"}],
    "has_search": true
  }' | python3 -m json.tool
echo ""

# Example 3: List Available Models
echo "Example 3: List Available Models"
echo "-------------------------------"
curl -s -X GET "http://localhost:8000/v1/models" | python3 -m json.tool
echo ""

# Example 4: Admin Status
echo "Example 4: Admin Status"
echo "----------------------"
curl -s -X GET "http://localhost:8000/v1/admin/status" | python3 -m json.tool
echo ""

echo "=============================================="
echo "  Examples Completed!"
echo "=============================================="
