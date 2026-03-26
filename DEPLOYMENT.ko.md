[English](DEPLOYMENT.md) | [한국어](DEPLOYMENT.ko.md)

# 배포 가이드: HydraLLM

이 문서는 다양한 환경에서 **HydraLLM**을 배포하고 유지 관리하기 위한 지침을 제공합니다.

---

## 💻 로컬 개발 환경

### 1. 사전 준비 및 설치

```bash
# 저장소 복제
git clone https://github.com/TaewonyNet/HydraLLM.git
cd HydraLLM

# 가상 환경 설정
python -m venv venv
source venv/bin/activate

# 에디터블 모드로 패키지 및 의존성 설치
pip install -e .

# Playwright 브라우저 바이너리 설치
playwright install chromium
```

### 2. 환경 설정

`.env.example` 파일을 복사하여 `.env` 파일을 생성합니다:

```bash
cp .env.example .env
# .env 파일을 열어 API 키(Gemini, Groq, Cerebras)를 추가하세요.
```

### 3. 서버 실행

```bash
python main.py
# 또는 디버그 모드로 실행
python main.py --debug
```

---

## 🐳 Docker 배포

### 1. 이미지 빌드

```bash
docker build -t hydrallm .
```

### 2. 컨테이너 실행

```bash
docker run -d \
  -p 8000:8000 \
  --env-file .env \
  --name hydrallm-gateway \
  hydrallm
```

### 3. Docker Compose (권장)

`docker-compose.yml` 파일을 작성합니다:

```yaml
version: '3.8'

services:
  hydrallm:
    build: .
    ports:
      - "8000:8000"
    env_file:
      - .env
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/"]
      interval: 30s
      timeout: 10s
      retries: 3
```

---

## 📈 스케일링 (Scaling)

- **수평 스케일링 (Horizontal)**: 로드 밸런서 뒤에 여러 복제본(Replica)을 배치합니다. HydraLLM은 상태 비저장(Stateless) 구조(DuckDB는 각 인스턴스 로컬에 저장됨)이므로, 각 인스턴스는 독자적인 키 풀과 로컬 세션 캐시를 관리합니다.
- **수직 스케일링 (Vertical)**: **컨텍스트 압축(LLMLingua-2)** 기능을 사용하거나 다수의 비동기 **Playwright** 스크래핑 작업을 처리해야 하는 경우 메모리와 CPU를 증설하세요.

---

## 🔐 보안 고려 사항

1. **HTTPS**: 프로덕션 환경에서는 항상 로드 밸런서나 리버스 프록시(Nginx, Traefik 등)에서 SSL(HTTPS)을 적용하세요.
2. **API 키 관리**: `.env` 파일을 절대 커밋하지 마세요. 프로덕션 키는 Secret Manager(AWS Secrets Manager, K8s Secrets 등)를 사용해 관리하는 것이 좋습니다.
3. **방화벽**: 8000번 포트(또는 설정된 포트)를 신뢰할 수 있는 클라이언트나 내부 네트워크에만 노출하세요.

---

## 🛠 문제 해결 (Troubleshooting)

1. **키 소진 (503 Error)**: `/v1/admin/status` 엔드포인트를 호출하여 모든 키가 `failed` 풀에 있는지 확인하세요. `/v1/admin/probe`를 호출하여 강제 재검증을 시도할 수 있습니다.
2. **스크래핑 실패**: `playwright` 바이너리가 정상적으로 설치되었는지 확인하세요 (`playwright install`).
3. **429 에러**: 무료 티어 키 사용 시 발생하는 정상적인 응답입니다. HydraLLM은 자동으로 키를 순환시킵니다. 모든 키에서 지속적으로 429 에러가 발생한다면 키의 개수를 늘리거나 재시도 지연 시간을 조정하세요.

---

## 📄 라이선스
본 프로젝트는 **MIT License**에 따라 배포됩니다.
