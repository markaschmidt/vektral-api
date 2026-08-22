FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV API_PORT=8788
ENV FIREBASE_STUB_MODE=true
EXPOSE 8788

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8788"]
