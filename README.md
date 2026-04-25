# Job Queue API

A production-ready background job processing system built with FastAPI, Celery, Redis, and MongoDB. The project includes containerization with Docker, a CI/CD pipeline using GitHub Actions, and a full observability stack using Prometheus, Grafana, Loki, and Flower.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Technology Stack](#technology-stack)
- [Project Structure](#project-structure)
- [API Reference](#api-reference)
- [Job Lifecycle](#job-lifecycle)
- [Retry Behavior](#retry-behavior)
- [Rate Limiting](#rate-limiting)
- [Running Locally Without Docker](#running-locally-without-docker)
- [Running With Docker](#running-with-docker)
- [Production Deployment](#production-deployment)
- [Monitoring](#monitoring)
- [CI/CD Pipeline](#cicd-pipeline)
- [Environment Variables](#environment-variables)
- [Security Practices](#security-practices)

---

## Overview

This system allows you to submit long-running tasks through an HTTP API and track their progress without blocking. When you submit a job, the API returns immediately with a job ID. The work happens in the background. You then poll a second endpoint with that ID to check whether the job is pending, processing, done, or failed.

This pattern is used when a task takes too long to complete inside a single HTTP request, for example processing a large file, sending emails in bulk, or generating a report.

---

## Architecture

```
Client (browser or curl)
        |
        | HTTP request
        v
   FastAPI (port 8000)
        |
        |-- saves job record --> MongoDB
        |-- sends task -------> Redis (queue)
        |
        v
   Celery Worker
        |
        |-- picks up task from Redis
        |-- runs the job (with retries on failure)
        |-- updates job status --> MongoDB
```

The API and the worker are separate processes. The API never waits for the job to finish. Redis acts as the message channel between them.

---

## Technology Stack

| Layer | Technology | Purpose |
|---|---|---|
| API framework | FastAPI | HTTP endpoints and request validation |
| Task queue | Celery | Async background job execution |
| Message broker | Redis | Passes tasks from API to worker |
| Database | MongoDB with Beanie ODM | Stores job state and results |
| Rate limiting | slowapi | Limits POST /jobs to 10 per minute per IP |
| Containerization | Docker and Docker Compose | Runs all services together |
| Metrics | Prometheus and Grafana | Dashboards for request counts, durations, job stats |
| Logs | Loki and Promtail | Centralized log collection across all containers |
| Celery monitor | Flower | Real-time view of tasks and workers |
| CI/CD | GitHub Actions | Automated testing, security scanning, Docker build and push |

---

## Project Structure

```
job-queue-api/
|
|-- app/
|   |-- main.py                  API entry point, rate limiter, startup hooks
|   |-- config.py                Reads settings from the .env file
|   |-- metrics.py               Custom Prometheus metrics (counters, gauges, histograms)
|   |-- models/
|   |   |-- job.py               MongoDB document model for a Job
|   |-- routes/
|   |   |-- jobs.py              The three API endpoints
|   |-- workers/
|       |-- celery_app.py        Celery setup and task logic with retry handling
|
|-- monitoring/
|   |-- prometheus/
|   |   |-- prometheus.yml       Tells Prometheus which services to scrape
|   |-- grafana/
|       |-- provisioning/
|           |-- datasources/
|           |   |-- datasources.yml   Auto-connects Grafana to Prometheus and Loki
|           |-- dashboards/
|               |-- dashboards.yml    Tells Grafana where to find dashboard files
|
|-- .github/
|   |-- workflows/
|       |-- app-ci.yml              CI pipeline: lint, test, security scan, Docker build
|       |-- security-pipeline.yml   Reusable workflow: Docker build, push, sign, SBOM
|
|-- Dockerfile                   Multi-stage build (Alpine builder, distroless runtime)
|-- .dockerignore                Files excluded from the Docker build context
|-- docker-compose.yml           Development environment (all 4 services)
|-- docker-compose.prod.yml      Production overrides (passwords, limits, replicas)
|-- docker-compose.monitoring.yml   Full observability stack (7 services)
|-- requirements.txt             Python dependencies with pinned versions
|-- .env.example                 Template for environment variables
|-- .gitignore                   Prevents secrets and build artifacts from entering git
```

---

## API Reference

### POST /jobs

Creates a new background job and returns immediately with the job record.

Rate limited to 10 requests per minute per IP address. Exceeding the limit returns HTTP 429.

Request body:

```json
{
  "job_type": "csv_parse",
  "payload": {
    "filename": "sales_q1.csv",
    "rows": 1500
  }
}
```

Supported job types:

| job_type | Simulates | Approximate duration |
|---|---|---|
| csv_parse | Parsing a CSV file row by row | 3 seconds |
| send_email | Sending an email via SMTP | 2 seconds |
| report | Generating a PDF report | 5 seconds |

Response (HTTP 202 Accepted):

```json
{
  "id": "66a1f3c7e123456789abcd",
  "job_type": "csv_parse",
  "status": "pending",
  "payload": { "filename": "sales_q1.csv", "rows": 1500 },
  "result": null,
  "error": null,
  "retry_count": 0,
  "celery_task_id": "abc-123-def-456",
  "created_at": "2026-04-25T10:30:00Z",
  "updated_at": "2026-04-25T10:30:00Z"
}
```

The `id` field is the MongoDB document ID. Use this to check job status. Do not use `celery_task_id` for status checks.

---

### GET /jobs/{id}

Returns the current state of a single job.

```
GET /jobs/66a1f3c7e123456789abcd
```

Poll this endpoint every few seconds to track progress. The `status` field changes as the job moves through its lifecycle. When the job succeeds, the `result` field is populated. When it fails permanently, the `error` field contains the reason.

---

### GET /jobs

Returns a paginated list of all jobs, newest first.

Query parameters:

| Parameter | Type | Default | Description |
|---|---|---|---|
| page | integer | 1 | Page number |
| page_size | integer | 10 | Jobs per page, maximum 100 |
| status | string | none | Filter by status: pending, processing, done, failed |
| job_type | string | none | Filter by job type |

Example:

```
GET /jobs?status=failed&page=1&page_size=20
```

Response:

```json
{
  "items": [...],
  "total": 42,
  "page": 1,
  "page_size": 20,
  "total_pages": 3
}
```

---

### GET /health

Returns HTTP 200 if the API is running. Used by Docker and load balancers as a liveness probe.

```json
{ "status": "ok", "service": "job-queue-api" }
```

---

### GET /metrics

Exposes Prometheus metrics. This endpoint is scraped automatically by Prometheus every 15 seconds and is not intended for direct use.

---

## Job Lifecycle

A job passes through four possible states:

```
pending  -->  processing  -->  done
                    |
                    v
                  failed
```

- `pending` — the job has been saved to MongoDB and sent to Redis, but no worker has picked it up yet
- `processing` — a Celery worker is currently running the task, or waiting between retry attempts
- `done` — the task completed successfully, the result is stored in MongoDB
- `failed` — all retry attempts were exhausted, the error message is stored in MongoDB

---

## Retry Behavior

When a task fails, Celery retries it automatically using exponential backoff. The wait time doubles on each attempt.

```
Attempt 1 fails  -->  wait  5 seconds  -->  Attempt 2
Attempt 2 fails  -->  wait 10 seconds  -->  Attempt 3
Attempt 3 fails  -->  wait 20 seconds  -->  Attempt 4
Attempt 4 fails  -->  job marked as failed permanently
```

The simulated tasks fail randomly (csv_parse at 20%, send_email at 15%) so you can observe retries in action during testing. The `retry_count` field on the job record shows how many retries have occurred. The `error` field always shows the most recent failure reason.

---

## Rate Limiting

The `POST /jobs` endpoint is limited to 10 requests per minute per IP address. Counters are stored in Redis, so the limit applies correctly even if multiple API instances are running behind a load balancer.

When the limit is exceeded, the response is:

```
HTTP 429 Too Many Requests
```

The limit resets 60 seconds after the first request in that window.

---

## Running Locally Without Docker

Requirements: Python 3.11 or higher, Redis, and MongoDB installed and running on your machine.

Step 1 — Enter the project directory:

```bash
cd job-queue-api
```

Step 2 — Create and activate a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
```

On Windows:

```bash
venv\Scripts\activate
```

Step 3 — Install dependencies:

```bash
pip install -r requirements.txt
```

Step 4 — Create your environment file:

```bash
cp .env.example .env
```

The default values in `.env` work without changes for local development.

Step 5 — Open four separate terminals and run one command in each:

Terminal 1 — Redis:
```bash
redis-server
```

Terminal 2 — MongoDB:
```bash
mkdir -p data/db && mongod --dbpath ./data/db
```

Terminal 3 — FastAPI:
```bash
uvicorn app.main:app --reload
```

Terminal 4 — Celery worker:
```bash
celery -A app.workers.celery_app.celery_app worker --loglevel=info
```

Open http://localhost:8000/docs in your browser to use the interactive API.

Verify each service is running:

```bash
redis-cli ping                                 # should print: PONG
mongosh --eval "db.adminCommand('ping')"       # should print: { ok: 1 }
curl http://localhost:8000/health              # should print: {"status":"ok",...}
```

---

## Running With Docker

Requirements: Docker Desktop installed.

Build and start all four containers (Redis, MongoDB, API, worker):

```bash
docker compose up --build
```

Open http://localhost:8000/docs in your browser.

Stop everything:

```bash
docker compose down
```

Stop and also delete the MongoDB data volume:

```bash
docker compose down -v
```

---

## Production Deployment

The production configuration adds security hardening on top of the development setup.

Step 1 — Create a `.env` file on the server with real credentials:

```bash
cp .env.example .env
```

Edit `.env` and set strong values for `REDIS_PASSWORD`, `MONGO_ROOT_USER`, and `MONGO_ROOT_PASSWORD`.

Step 2 — Deploy using both compose files:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

What the production configuration adds compared to development:

- Redis and MongoDB require passwords to connect
- Redis and MongoDB ports are not exposed to the host machine
- The container filesystem is read-only, nothing can be written except to `/tmp`
- Containers cannot escalate privileges
- CPU and memory hard limits are set on every container
- The API and worker each run 2 replicas for availability
- Log rotation prevents disk from filling up
- Source code comes from the built image, not a live folder mount

---

## Monitoring

The monitoring stack provides metrics dashboards, log aggregation, and a Celery task monitor. It consists of seven services: Prometheus, Grafana, Flower, Redis Exporter, MongoDB Exporter, Loki, and Promtail.

Start the monitoring stack alongside the application:

```bash
docker compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d
```

Access the tools:

| URL | Tool | What it shows |
|---|---|---|
| http://localhost:3000 | Grafana | Dashboards and charts, login: admin / admin |
| http://localhost:5555 | Flower | Live Celery task monitor |
| http://localhost:9090 | Prometheus | Raw metrics browser |
| http://localhost:8000/metrics | Metrics endpoint | Text metrics the API exposes |

What each monitoring service does:

**Prometheus** visits the `/metrics` endpoint of the API, Redis exporter, MongoDB exporter, and Flower every 15 seconds. It stores all the numbers it collects as time-series data and retains 30 days of history.

**Grafana** connects to Prometheus and Loki as data sources. These connections are configured automatically on startup via the provisioning files, no manual setup is needed. Create panels to visualize request rates, job counts, durations, and error rates.

**Flower** connects to Redis and shows all Celery tasks in real time. You can see which workers are online, which tasks are running, and the full history of completed and failed tasks with their durations and error messages.

**Loki** receives logs from all containers. Promtail reads Docker log files from the host and ships them to Loki. In Grafana you can view and search logs from all containers in one place, and display them alongside metric charts for correlation.

**Redis Exporter** and **MongoDB Exporter** translate the internal stats of each database into Prometheus format so they can be scraped and stored.

Custom application metrics this project exposes:

| Metric | Type | Description |
|---|---|---|
| jobs_created_total | Counter | Total jobs created, broken down by job type |
| jobs_succeeded_total | Counter | Total jobs that completed successfully |
| jobs_failed_total | Counter | Total jobs that failed permanently |
| jobs_by_status | Gauge | Current count of jobs in each status |
| job_duration_seconds | Histogram | How long jobs take to complete |

---

## CI/CD Pipeline

The project uses two GitHub Actions workflow files. The main CI file triggers on code changes and calls the reusable security pipeline after tests pass.

### app-ci.yml

Triggers on every push to the `main` branch when files inside `app/` change.

**Checkout code** — Clones the repository onto the GitHub Actions runner.

**Set up Python 3.11** — Installs Python and enables pip caching so dependency installation is faster on repeated runs.

**Install dependencies** — Upgrades pip, installs the quality and security tools (ruff, pytest, bandit, safety), and installs the application dependencies from `requirements.txt`.

**Lint with Ruff** — Runs Ruff, a fast Python linter, to check for code style violations, unused imports, and common mistakes. The step continues even on failure so all issues are visible at once.

**SAST scan with Bandit** — Runs Bandit, a static analysis security tool, against the Python source code. It looks for issues such as hardcoded credentials, use of dangerous functions, insecure deserialization, and other patterns that indicate security problems in the code itself.

**Dependency audit with Safety** — Runs Safety, which compares every package listed in `requirements.txt` against a public database of known CVEs. If any installed package has a known vulnerability, this step reports it with the CVE ID and severity.

**Run tests with Pytest** — Executes the test suite and reports any failures.

After all steps in `test-and-audit` complete, the workflow calls the reusable security pipeline.

---

### security-pipeline.yml

A reusable workflow called by `app-ci.yml`. It receives the Docker image name and build context as inputs.

**Checkout code** — Clones the repository.

**Set up Docker Buildx for multi-architecture builds** — Configures Docker to build images for two CPU architectures at the same time: `linux/amd64` (standard servers and desktops) and `linux/arm64` (AWS Graviton, Raspberry Pi, Apple M1). This means the same image works on both architectures without building it twice separately.

**Login to Docker Hub** — Authenticates with Docker Hub using credentials stored as GitHub repository secrets. A token is used instead of a password.

**Build and push Docker image** — Builds the image using the multi-stage Dockerfile and pushes it to Docker Hub. The image is tagged with both a human-readable name and the Git commit SHA. Using the commit SHA as a tag means every build is uniquely traceable back to the exact code that produced it.

**Security scan with Anchore** — Runs the Anchore container scanning tool against the newly pushed image. Anchore inspects every layer of the image for known vulnerabilities in OS packages and installed libraries. The pipeline is configured with `severity-cutoff: critical`, meaning it will fail and block the deployment if any critical severity CVE is detected in the image.

**Digital signature with Cosign** — Signs the Docker image using Cosign from the Sigstore project. The signature is stored alongside the image in Docker Hub. It proves that this specific image was built by this pipeline and has not been modified after being pushed. Anyone pulling the image can verify the signature with the corresponding public key to confirm its authenticity.

**Update inventory** — Writes a `release.log` file containing the build timestamp, image name, image digest, and SBOM ID. This creates an audit trail for every build that can be reviewed for compliance.

**Upload release log** — Uploads the `release.log` as a GitHub Actions artifact. It appears in the Actions UI under the workflow run and can be downloaded for audit and compliance records.

---

## Environment Variables

| Variable | Required | Default (development) | Description |
|---|---|---|---|
| REDIS_URL | Yes | redis://localhost:6379/0 | Full Redis connection string |
| MONGODB_URL | Yes | mongodb://localhost:27017 | Full MongoDB connection string |
| MONGODB_DB_NAME | Yes | jobqueue | Name of the MongoDB database |
| APP_ENV | No | development | Set to "production" in prod |
| REDIS_PASSWORD | Production only | none | Redis authentication password |
| MONGO_ROOT_USER | Production only | none | MongoDB root username |
| MONGO_ROOT_PASSWORD | Production only | none | MongoDB root password |

Copy `.env.example` to `.env` and fill in values before running. The `.env` file is listed in `.gitignore` and must never be committed to the repository.

---

## Security Practices

**No root in containers** — The Docker image uses the distroless nonroot base image which runs as user ID 65532. The production compose file additionally sets `no-new-privileges: true` to block any privilege escalation attempt inside the container.

**Distroless runtime image** — The final Docker image contains only the Python interpreter and the application code. There is no shell, no package manager, and no system utilities. An attacker who gains code execution inside the container has no tools available to them.

**Multi-stage Docker build** — Compilation tools such as gcc, pip, and cargo exist only in the intermediate build stage and are never present in the final image. This results in a much smaller image with a significantly reduced attack surface.

**Read-only filesystem in production** — The production compose file sets `read_only: true` on the API and worker containers. No process can write to disk. The only writable area is a small in-memory `/tmp` mount.

**Secrets via environment variables** — No credentials are hardcoded in source code or baked into Docker images. All secrets are passed at runtime through environment variables from a `.env` file that is excluded from version control.

**Image signing with Cosign** — Every Docker image built by the CI pipeline is signed using Cosign. The signature proves the image came from the official pipeline and has not been tampered with after being pushed to the registry.

**Three layers of vulnerability scanning** — Every push runs Bandit for source code security analysis, Safety for CVEs in Python dependencies, and Anchore for vulnerabilities in the built Docker image. Critical findings in the image block the pipeline.

**Rate limiting with Redis-backed counters** — The job creation endpoint is protected against abuse with a 10 requests per minute per IP limit. Counters are stored in Redis so the limit works correctly across multiple API instances.

**Network isolation** — All containers communicate on a private Docker bridge network. In production, Redis and MongoDB have no ports exposed to the host machine. Only the API port is reachable from outside the Docker network.

---
## Screenshots

### App
| Screenshot 1 | Screenshot 2 |
| :---: | :---: |
| ![alt text](Screenshots/App-1.png) | ![alt text](Screenshots/App-2.png) |
| Screenshot 3 | Screenshot 4 |
| ![alt text](Screenshots/App-3.png) | ![alt text](Screenshots/App-4.png) |
| Screenshot 5 | Screenshot 6 |
| ![alt text](Screenshots/App-5.png) | ![alt text](Screenshots/App-6.png) |

### Prometheus 
| Targets |
| :---: |
| ![alt text](Screenshots/Prometheus.png) |

### Grafana
| FlaskAPI Dashboard 1 | FlaskAPI Dashboard 2 |
| :---: | :---: |
| ![alt text](Screenshots/grafana.png) | ![alt text](Screenshots/grafana-2.png) |

### Alerting
| Alerting |
| :---: |
| ![alt text](Screenshots/Alerting.png) |

| Pending | Firing |
| :---: | :---: |
| ![alt text](Screenshots/Pending.png)  | ![text](Screenshots/Firing.png) |

---

# AWS EC2 Deployment Guide
 
## Server Details
 
| Setting | Value |
|---|---|
| OS | Ubuntu Server 24.04 LTS |
| Instance type | t3.medium |
| Storage | 50 GB |
 
---
 
## Architecture
 
Redis and MongoDB run as managed AWS services. Only the API and Celery worker run on EC2.
 
```
EC2 (t3.medium)
  - FastAPI (port 8000)
  - Celery Worker
        |               |
        v               v
  ElastiCache       MongoDB Atlas
  (Redis)           (free tier)
```
 
---
 
## Step 1 — Security Group Rules
 
Open these ports on your EC2 security group:
 
| Port | Source | Purpose |
|---|---|---|
| 22 | My IP only | SSH access |
| 8000 | 0.0.0.0/0 | API |
| 3000 | My IP only | Grafana |
| 5555 | My IP only | Flower |
| 9090 | My IP only | Prometheus |
 
---
 
## Step 2 — Connect to the Server
 
```bash
chmod 400 your-key.pem
ssh -i your-key.pem ubuntu@YOUR_SERVER_IP
```
 
---
 
## Step 3 — Install Docker
 
```bash
sudo apt update
sudo apt install -y ca-certificates curl gnupg
 
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
 
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
 
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
 
sudo usermod -aG docker ubuntu
newgrp docker
```
 
---
 
## Step 4 — Set Up MongoDB Atlas
 
MongoDB runs outside AWS on MongoDB Atlas (free tier).
 
1. Go to https://mongodb.com/atlas and create a free account
2. Create a free M0 cluster — select the same AWS region as your EC2 (for example ap-south-1)
3. Go to **Network Access** and add your EC2 public IP
4. Go to **Database Access** and create a user with a username and strong password
5. Click **Connect** on your cluster, choose "Connect your application", and copy the connection string:
```
mongodb+srv://YOUR_USER:YOUR_PASSWORD@cluster0.abc123.mongodb.net/jobqueue
```
 
---
 
## Step 5 — Set Up ElastiCache (Redis)
 
1. Go to AWS ElastiCache console and click **Create**
2. Select these settings:
| Setting | Value |
|---|---|
| Engine | Redis OSS |
| Deployment | Node-based cluster |
| Configuration | Dev/Test (not Production — saves ~$130/month) |
| Node type | cache.t3.micro |
| Cluster name | jobqueue-redis |
| VPC | Same VPC as your EC2 instance |
 
3. After creation, copy the **Primary Endpoint**:
```
jobqueue-redis.abc123.use1.cache.amazonaws.com:6379
```
 
4. Add an inbound rule to the **ElastiCache security group**:
| Type | Port | Source |
|---|---|---|
| Custom TCP | 6379 | Your EC2 security group ID |
 
This allows your EC2 containers to reach ElastiCache. Without this rule the connection will be refused.
 
---
 
## Step 6 — Deploy the Application
 
```bash
# Clone the repo
git clone https://github.com/YOUR_USERNAME/job-queue-api.git
cd job-queue-api
 
# Create environment file
cp .env.example .env
nano .env
```
 
Fill in the `.env` file with your real connection strings:
 
```bash
REDIS_URL=redis://jobqueue-redis.abc123.use1.cache.amazonaws.com:6379/0
MONGODB_URL=mongodb+srv://YOUR_USER:YOUR_PASSWORD@cluster0.abc123.mongodb.net/jobqueue
MONGODB_DB_NAME=jobqueue
APP_ENV=production
```
 
Then start the containers:
 
```bash
docker compose up --build -d
docker compose ps
```
 
You should see two containers running: `jobqueue-api` and `jobqueue-worker`. Redis and MongoDB are no longer in the compose file — they are external services.
 
Visit `http://YOUR_SERVER_IP:8000/docs` to confirm the API is live.
 
---
 
## Step 7 — Start Monitoring (Optional)
 
```bash
docker compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d
```
 
| URL | Tool |
|---|---|
| http://YOUR_SERVER_IP:3000 | Grafana (login: admin / admin) |
| http://YOUR_SERVER_IP:5555 | Flower |
| http://YOUR_SERVER_IP:9090 | Prometheus |
 
---
 
## Common Commands
 
```bash
docker compose ps                  # check container status
docker compose logs -f api         # stream API logs
docker compose logs -f worker      # stream worker logs
docker compose restart api         # restart one container
docker compose down                # stop everything
```
 
## Redeploy After Code Changes
 
```bash
git pull
docker compose up --build -d
```
 
---
 
## Auto-restart After Server Reboot
 
```bash
sudo systemctl enable docker
```
 
The `restart: unless-stopped` setting in `docker-compose.yml` restarts containers automatically after a reboot.
 
---
 
## Cost
 
| Resource | Cost |
|---|---|
| EC2 t3.medium | ~$30/month |
| 50 GB EBS storage | ~$5/month |
| ElastiCache cache.t3.micro | ~$12/month |
| MongoDB Atlas M0 | Free |
| Total | ~$47/month |

| Running Instance | Running Application on EC2 |
| :---: | :---: |
| ![alt text](<Screenshots/EC2 (2).png>) | ![alt text](Screenshots/EC2.png) |

| Mongodb | ElastiCache |
| :---: | :---: |
| ![alt text](Screenshots/MongoDB.png) |  ![alt text](Screenshots/Redis.png) |


## ThankYou 😊