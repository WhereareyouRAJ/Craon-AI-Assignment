# Stage 1: Builder
FROM python:3.12-alpine AS builder

RUN apk add --no-cache \
    gcc \
    musl-dev \
    libffi-dev \
    openssl-dev \
    cargo
 

RUN addgroup -S appgroup && adduser -S appuser -G appgroup

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir --prefix=/install -r requirements.txt

COPY app/ ./app/


# Stage 2: Runtime
FROM gcr.io/distroless/python3-debian12:nonroot AS runtime

COPY --from=builder /install/lib /usr/local/lib
COPY --from=builder /app /app

WORKDIR /app

ENV PYTHONPATH=/usr/local/lib/python3.12/site-packages
ENV PYTHONDONTWRITEBYTECODE=1

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

ENTRYPOINT ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
