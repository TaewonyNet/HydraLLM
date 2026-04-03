# 🛠 HydraLLM Troubleshooting Guide

[English](TROUBLESHOOTING.md) | [한국어](TROUBLESHOOTING.ko.md)

This document records solved problems and patterns to help AI agents and developers diagnose issues using logs.

---

## 🔍 Log-Based Diagnosis Workflow

When a "Problem Occurred" report is received:
1. **Identify Session/Request ID**: Grep the logs for the session ID provided by the user. Look for the `[req_...]` pattern.
2. **Trace Lifecycle**: Search for all logs sharing the same Request ID to see the full flow:
   - Analysis -> Web Fetch -> Routing -> Provider Selection -> Model Response.
3. **Check Usage Field**: Verify `gateway_provider` and `gateway_key_index` in the response metadata to see exactly which resource failed.

---

## ✅ Solved Issues & Patterns

### 1. TypeError: cannot unpack non-iterable NoneType object
- **Symptom**: 500 Internal Server Error, `TypeError` in `gateway.py` during `_process_with_retries` unpacking.
- **Root Cause**: The retry loop in `_process_with_retries` finished without returning a value or raising an exception (usually due to a failed last-attempt fallback), causing it to implicitly return `None`.
- **Solution**: Ensure `raise last_exception` is present outside the retry loop. Verify all fallback paths return a valid `(ChatResponse, list[dict])` tuple.

### 2. 404 Model Not Found (Local Agents)
- **Symptom**: `Agent: ollama (Model: llama3) - Error: 404 Not Found`.
- **Root Cause**: Hardcoded model names (like `llama3`) in the adapter defaults that don't exist on the local machine.
- **Solution**: 
  - Use **Runtime Discovery**: Call `adapter.discover_models()` at execution time.
  - **Generic Mapping**: Map generic model hints (e.g., `ollama`, `auto`) to the first available model found in the discovered list.

### 3. 429 Quota Exceeded (Gemini/Groq)
- **Symptom**: `Rate limit exceeded: 429`.
- **Root Cause**: Free tier limits reached.
- **Solution**: 
  - **Strict Recovery**: Do not mark a 429-failed key as "recovered" until a probe actually succeeds.
  - **Provider Fallback**: If all keys for Provider A are 429, immediately switch to Provider B.
  - **Local Fallback**: If all external providers fail, use local Ollama/OpenCode.

---

## 📈 Log Optimization Tips
- Every log entry MUST include `[req_...]` for correlation.
- Use `DEBUG` level for full request/response payloads if `settings.debug` is enabled.
- Avoid JSON logs for human readability, but keep the structure consistent for script parsing.

---
*Last Updated: 2026-03-30*
