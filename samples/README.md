# 샘플 요청/응답 파일 가이드

이 디렉토리에는 Multi-LLM Gateway API의 다양한 사용 시나리오에 대한 샘플 요청 및 응답 파일이 포함되어 있습니다.

## 파일 목록

| 파일명 | 설명 | 라우팅 대상 |
|--------|------|-------------|
| `simple_request.json` | 간단한 채팅 요청 | Groq (Llama 8B) |
| `long_context_request.json` | 8000+ 토큰의 장문 컨텍스트 | Gemini |
| `multimodal_request.json` | 이미지가 포함된 요청 | Gemini |
| `complex_query_request.json` | 복잡한 분석 요청 | Cerebras (Llama 70B) |

## 사용 방법

1. 해당 JSON 파일의 `request` 부분을 복사합니다
2. API 엔드포인트로 POST 요청을 보냅니다
3. 응답은 `expected_response`와 유사한 형태로 반환됩니다

## 예시 curl 명령어

```bash
# 간단한 요청
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d @samples/simple_request.json

# 응답 저장
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d @samples/simple_request.json \
  -o response.json
```

## 참고 사항

- `expected_routing` 필드는 예상되는 라우팅 결과입니다 (실제 응답에는 포함되지 않음)
- 실제 응답의 `model` 필드는 Gateway가 선택한 실제 모델명이 반환됩니다
- 토큰 수는 요청 내용에 따라 달라질 수 있습니다
