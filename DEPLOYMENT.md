[English](DEPLOYMENT.md) | [한국어](DEPLOYMENT.ko.md)

# Deployment Guide: HydraLLM

This document provides instructions for deploying and maintaining **HydraLLM** in various environments.

---

## 💻 Local Development

### 1. Prerequisite Setup

```bash
# Clone the repository
git clone https://github.com/TaewonyNet/HydraLLM.git
cd HydraLLM

# Setup virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies in editable mode
pip install -e .

# Install Playwright browser binaries
playwright install chromium
```

### 2. Configuration

Create a `.env` file based on `.env.example`:

```bash
cp .env.example .env
# Open .env and add your API keys (Gemini, Groq, Cerebras)
```

### 3. Running the Server

```bash
python main.py
# Or with debug mode
python main.py --debug
```

---

## 🐳 Docker Deployment

### 1. Build Image

```bash
docker build -t hydrallm .
```

### 2. Run Container

```bash
docker run -d \
  -p 8000:8000 \
  --env-file .env \
  --name hydrallm-gateway \
  hydrallm
```

### 3. Docker Compose (Recommended)

Create `docker-compose.yml`:

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

## ☸️ Production (Kubernetes)

Ensure you have a secret named `gateway-secrets` containing your API keys.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: hydrallm
spec:
  replicas: 2
  selector:
    matchLabels:
      app: hydrallm
  template:
    metadata:
      labels:
        app: hydrallm
    spec:
      containers:
      - name: hydrallm
        image: taewonynet/hydrallm:latest
        ports:
        - containerPort: 8000
        envFrom:
        - secretRef:
            name: gateway-secrets
        resources:
          requests:
            memory: "512Mi"
            cpu: "500m"
          limits:
            memory: "1Gi"
            cpu: "1000m"
        livenessProbe:
          httpGet:
            path: /
            port: 8000
          initialDelaySeconds: 30
```

---

## 📈 Scaling

- **Horizontal Scaling**: Use multiple replicas behind a load balancer. Since HydraLLM is stateless (DuckDB is local to each instance), each instance will manage its own key pools and local session cache.
- **Vertical Scaling**: Increase memory and CPU if you enable **Context Compression (LLMLingua-2)** or handle many concurrent **Playwright** scraping tasks.

---

## 🔐 Security Considerations

1. **HTTPS**: Always terminate SSL (HTTPS) at your load balancer or reverse proxy (Nginx, Traefik) in production.
2. **API Keys**: Never commit your `.env` file. Use Secret Managers (AWS Secrets Manager, K8s Secrets) for production keys.
3. **Firewall**: Only expose port 8000 (or your configured PORT) to trusted clients or your internal network.

---

## 🛠 Troubleshooting

1. **Key Exhaustion (503)**: Check `/v1/admin/status` to see if all keys are in the `failed` pool. Run `/v1/admin/probe` to force re-validation.
2. **Scraping Failures**: Ensure `playwright` binaries are installed (`playwright install`).
3. **429 Errors**: This is normal for free tier keys. HydraLLM will automatically rotate keys. If you see persistent 429s across all keys, consider increasing the number of keys or the retry delay.

---

## 📄 License

This project is licensed under the **MIT License**.
