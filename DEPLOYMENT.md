# 배포 설정

## 로컬 개발

### 애플리케이션 실행
```bash
# 의존성 설치
pip install -r requirements.txt

# 환경 변수 설정
cp .env.example .env
# .env 파일에 API 키를 입력하세요

# 애플리케이션 실행
python main.py
```

### Docker로 실행
```bash
# Docker 이미지 빌드
docker build -t multi-llm-gateway .

# 컨테이너 실행
docker run -p 8000:8000 -e PORT=8000 multi-llm-gateway
```

## 프로덕션 배포

### Docker Compose
`docker-compose.yml` 파일 생성:
```yaml
version: '3.8'

services:
  gateway:
    build: .
    ports:
      - "8000:8000"
    environment:
      - PORT=8000
      - LOG_LEVEL=INFO
      - MAX_TOKENS_FAST_MODEL=8192
      - GEMINI_KEYS=${GEMINI_KEYS}
      - GROQ_KEYS=${GROQ_KEYS}
      - CEREBRAS_KEYS=${CEREBRAS_KEYS}
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/"]
      interval: 30s
      timeout: 30s
      retries: 3
      start_period: 40s
```

### 환경 변수

#### 필수 변수
```env
# 서버 설정
PORT=8000
LOG_LEVEL=INFO
MAX_TOKENS_FAST_MODEL=8192

# API 키 (쉼표로 구분)
GEMINI_KEYS=your_gemini_key1,your_gemini_key2
GROQ_KEYS=your_groq_key1,your_groq_key2
CEREBRAS_KEYS=your_cerebras_key1,your_cerebras_key2
```

#### 선택 변수
```env
# 보안
SECRET_KEY=your_secret_key

# 성능
WORKER_CLASS=uvicorn.workers.UvicornWorker
WORKER_CONNECTIONS=1000

# 로깅
LOG_FORMAT="%(asctime)s - %(levelname)s - %(message)s"
```

### Kubernetes Deployment

#### Deployment YAML
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: multi-llm-gateway
spec:
  replicas: 2
  selector:
    matchLabels:
      app: multi-llm-gateway
  template:
    metadata:
      labels:
        app: multi-llm-gateway
    spec:
      containers:
      - name: gateway
        image: multi-llm-gateway:latest
        ports:
        - containerPort: 8000
        env:
        - name: PORT
          value: "8000"
        - name: LOG_LEVEL
          value: "INFO"
        - name: GEMINI_KEYS
          valueFrom:
            secretKeyRef:
              name: gateway-secrets
              key: gemini-keys
        - name: GROQ_KEYS
          valueFrom:
            secretKeyRef:
              name: gateway-secrets
              key: groq-keys
        - name: CEREBRAS_KEYS
          valueFrom:
            secretKeyRef:
              name: gateway-secrets
              key: cerebras-keys
        resources:
          requests:
            memory: "256Mi"
            cpu: "250m"
          limits:
            memory: "512Mi"
            cpu: "500m"
        livenessProbe:
          httpGet:
            path: /
            port: 8000
          initialDelaySeconds: 30
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /
            port: 8000
          initialDelaySeconds: 5
          periodSeconds: 5
---
apiVersion: v1
kind: Service
metadata:
  name: multi-llm-gateway-service
spec:
  selector:
    app: multi-llm-gateway
  ports:
    - protocol: TCP
      port: 80
      targetPort: 8000
  type: LoadBalancer
```

### 환경 설정

#### 개발 환경
```bash
# 가상 환경 생성
python -m venv venv
source venv/bin/activate

# 의존성 설치
pip install -r requirements.txt

# 개발 도구 설치
pip install pytest pytest-asyncio pytest-cov mypy black ruff
```

#### 프로덕션 환경
```bash
# 시스템 의존성 설치
apt-get update && apt-get install -y \
    python3.10 \
    python3.10-venv \
    python3-pip \
    curl \
    git

# 저장소 클론
git clone https://github.com/tidesquare/agent-playground.git
cd free_agent

# 프로덕션 환경 설정
cp .env.example .env
# .env 파일에 프로덕션 API 키를 입력하세요

# Docker로 설치 및 실행
# 위 Docker Compose 가이드 따르기
```

### 모니터링 및 로깅

#### 로그 설정
```env
# 로그 레벨: DEBUG, INFO, WARNING, ERROR, CRITICAL
LOG_LEVEL=INFO
LOG_FORMAT="%(asctime)s - %(levelname)s - %(message)s"
LOG_DATEFMT="%Y-%m-%d %H:%M:%S"

# 로그 파일 위치
LOG_FILE=/var/log/gateway.log
```

#### 헬스 체크
```bash
# 애플리케이션 상태 확인
curl http://localhost:8000/

# API 엔드포인트 확인
curl http://localhost:8000/v1/chat/completions \
  -X POST \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "health check"}]}'
```

### 스케일링

#### 수평 스케일링
- 로드 밸런서 뒤에 다중 복제본 사용
- 각 인스턴스는 자체 키 풀 유지
- 인스턴스 간 공유 상태 필요 없음

#### 수직 스케일링
- 요청량에 따라 CPU/메모리 증가
- 키 로테이션 성능 모니터링
- 워커 연결 수 조정

### 보안 고려사항

#### API 키 관리
- 환경 변수 또는 시크릿 매니저에 API 키 저장
- 정기적으로 키 교체
- 환경별로 다른 키 사용

#### 네트워크 보안
- 프로덕션에서 HTTPS 사용
- 레이트 리밋 구현
- 방화벽 규칙으로 접근 제한

#### 애플리케이션 보안
- 모든 입력 데이터 검증
- 파라미터화된 쿼리 사용
- 적절한 에러 처리 구현

### 백업 및 복구

#### 설정 백업
- 환경 변수 파일 백업
- Docker 이미지 백업
- Kubernetes 매니페스트 백업

#### 데이터 백업
- Gateway에 영구 데이터 저장 안함
- API 키는 별도로 백업
- 설정은 버전 관리 필요

### 문제 해결

#### 일반적인 문제

1. **API 키가 작동하지 않음**
   - 키가 올바른 형식인지 확인
   - 제공자 상태 확인
   - 개별 키 테스트

2. **성능 문제**
   - 리소스 사용량 모니터링
   - 키 로테이션 빈도 확인
   - 제공자 응답 시간 검토

3. **연결 오류**
   - 네트워크 연결 확인
   - 방화벽 규칙 확인
   - DNS 해석 테스트

#### 디버그 모드
```bash
# 디버그 로깅으로 실행
LOG_LEVEL=DEBUG python main.py

# 컨테이너 로그 확인
docker logs multi-llm-gateway
```

### 유지보수

#### 정기 작업
- API 사용량 모니터링
- API 키 교체
- 의존성 업데이트
- 에러 로그 검토

#### 업데이트 프로세스
```bash
# 최신 변경사항 가져오기
git pull origin main

# 의존성 업데이트
pip install -r requirements.txt --upgrade

# 애플리케이션 재시작
docker-compose restart gateway
```

## 지원

배포问题时，请检查:
1. 환경 변수가 올바르게 설정되었는지
2. API 키에 적절한 권한이 있는지
3. 네트워크 연결이 작동하는지
4. 리소스 제한이 충분한지

추가 도움이 필요하면 GitHub 저장소에 이슈를 생성해주세요.