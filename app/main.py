"""
main.py — FastAPI application entry point.
 
Responsibilities:
  • Boot up: connect to MongoDB, register Beanie document models
  • Attach slowapi rate limiter (uses Redis to count requests per IP)
  • Mount the /jobs router
  • Provide a /health endpoint so infra knows the service is alive
  • Expose a /metrics endpoint so Prometheus can scrape app metrics
"""
 
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
 
# Rate limiting
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
 
# Database
from motor.motor_asyncio import AsyncIOMotorClient
from beanie import init_beanie
 
from app.config import settings
from app.models.job import Job
from app.routes.jobs import router as jobs_router
 
# Prometheus — auto-instruments all FastAPI routes and exposes /metrics
# This gives us: request count, request duration, response size — for free
from prometheus_fastapi_instrumentator import Instrumentator
 
 
# ── Rate Limiter ───────────────────────────────────────────────────────────────
# get_remote_address extracts the caller's IP from the request.
# storage_uri tells slowapi to store counters in Redis (survives restarts,
# works across multiple API server instances).
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=settings.redis_url,
)
 
 
# ── Database lifecycle ─────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Code BEFORE yield runs at startup.
    Code AFTER yield runs at shutdown.
    FastAPI calls this automatically.
    """
    # Connect to MongoDB
    client = AsyncIOMotorClient(settings.mongodb_url)
    app.state.mongo_client = client     # store so we can close it on shutdown
 
    # Initialise Beanie with our document models
    # This creates indexes defined in the model classes
    await init_beanie(
        database=client[settings.mongodb_db_name],
        document_models=[Job],
    )
    print(f"✅  Connected to MongoDB: {settings.mongodb_db_name}")
    print(f"✅  Redis broker: {settings.redis_url}")
 
    yield   # ← application runs here
 
    # Shutdown: close the MongoDB connection cleanly
    client.close()
    print("🔌  MongoDB connection closed")
 
 
# ── FastAPI app ────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Job Queue API",
    description=(
        "A FastAPI + Celery + Redis + MongoDB job queue.\n\n"
        "POST /jobs to enqueue work, then poll GET /jobs/{id} for status."
    ),
    version="1.0.0",
    lifespan=lifespan,
)
 
# Attach rate limiter to app state so slowapi can find it
app.state.limiter = limiter
 
# Return 429 Too Many Requests when the limit is hit
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
 
# SlowAPI middleware intercepts every request and checks counters
app.add_middleware(SlowAPIMiddleware)
 
# CORS — allow any origin for local dev (tighten in production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
 
 
# ── Apply rate limit to POST /jobs ─────────────────────────────────────────────
# We override the route in jobs.py here so the limiter instance is available.
# "10/minute" = 10 requests per minute per unique IP.
from app.routes.jobs import create_job
limiter.limit("10/minute")(create_job)
 
 
# ── Routers ────────────────────────────────────────────────────────────────────
app.include_router(jobs_router)
 
# ── Prometheus metrics ─────────────────────────────────────────────────────────
# Instrumentator automatically tracks every HTTP request made to this app:
#   - http_requests_total          (how many requests, by route and status code)
#   - http_request_duration_seconds (how long each request took)
#   - http_response_size_bytes      (response payload sizes)
#
# .instrument(app) hooks into FastAPI middleware to measure every request.
# .expose(app)     creates the GET /metrics endpoint Prometheus will scrape.
Instrumentator().instrument(app).expose(app)
 
 
# ── Health check ───────────────────────────────────────────────────────────────
@app.get("/health", tags=["System"])
async def health():
    """Quick liveness probe — returns 200 if the API is running."""
    return {"status": "ok", "service": "job-queue-api"}
 
 
# ── Root redirect to docs ──────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
async def root():
    return JSONResponse({"message": "Job Queue API — visit /docs for interactive docs"})
