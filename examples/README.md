# API 사용 예제

이 디렉토리에는 Multi-LLM Gateway API의 다양한 사용 방법을 보여주는 예제 코드가 포함되어 있습니다.

## 예제 파일

### 1. Python SDK 예제 (`python_sdk_example.py`)

OpenAI Python 라이브러리(v1.0.0+)를 사용하여 Gateway에 요청을 보내는 방법을 보여줍니다.

**실행 방법:**
```bash
cd /home/tide/project/py/github.com/tidesquare/agent-playground/free_agent
pip install "openai>=1.0.0"
python examples/python_sdk_example.py
```

**포함된 예제:**
- 간단한 채팅 요청 (자동 모델 선택: `model="auto"`)
- 대화 형태 (시스템 프롬프트 포함)
- 로컬 에이전트 엔진 연동 (Ollama)
- 코드 생성 (고지능 모델 자동 선택)
- 데이터 분석 요청

### 2. curl 예제 (`curl_examples.sh`)

터미널에서 직접 curl 명령어로 API를 테스트하는 방법을 보여줍니다. `model="auto"`를 통한 지능형 라우팅과 로컬 에이전트 엔진 연동을 포함합니다.

**실행 방법:**
```bash
cd /home/tide/project/py/github.com/tidesquare/agent-playground/free_agent
chmod +x examples/curl_examples.sh
./examples/curl_examples.sh
```

**포함된 예제:**
- 간단한 채팅
- 시스템 프롬프트 포함 요청
- 긴 텍스트 분석
- 코드 생성
- 구조화된 JSON 응답 요청

### 3. 비동기 요청 예제 (`async_example.py`)

aiohttp를 사용하여 동시에 여러 요청을 보내는 방법을 보여줍니다.

**실행 방법:**
```bash
cd /home/tide/project/py/github.com/tidesquare/agent-playground/free_agent
pip install aiohttp
python examples/async_example.py
```

**포함된 기능:**
- 비동기 HTTP 세션 관리
- 동시 요청 처리
- 에러 처리
- 결과 집계

## 빠른 시작

### 1. Gateway 서버 실행

```bash
# 터미널 1에서 서버 실행
cd /home/tide/project/py/github.com/tidesquare/agent-playground/free_agent
python main.py
```

### 2. 예제 실행

```bash
# 터미널 2에서 예제 실행
cd /home/tide/project/py/github.com/tidesquare/agent-playground/free_agent
python examples/python_sdk_example.py
```

## 주의사항

- Gateway 서버가 실행 중인지 확인하세요
- `.env` 파일에 API 키가 설정되어 있어야 합니다
- 예제 코드는 로컬호스트(8000 포트)를 기준으로 작성되었습니다
- 실제 API 키가 필요합니다 (Gemini, Groq, Cerebras 중 하나 이상)
