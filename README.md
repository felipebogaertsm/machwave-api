# machwave-api

FastAPI backend for the machwave web platform.

## Architecture

```mermaid
flowchart TD
    User(["👤 User"])

    subgraph Vercel["Vercel"]
        FE["Next.js Frontend"]
    end

    subgraph FirebaseAuth["Firebase Auth"]
        FA["Identity &amp; JWT Issuance"]
    end

    subgraph GCP["Google Cloud Platform"]
        subgraph CloudRunService["Cloud Run — API Service"]
            API["machwave-api\n(FastAPI)"]
        end

        subgraph CloudRunJobs["Cloud Run Jobs — Simulation Worker"]
            Worker["Simulation Worker"]
            Lib["machwave lib\n(physics engine)"]
            Worker -- "InternalBallisticsSimulation.run()" --> Lib
        end

        GCS[("Cloud Storage (GCS)\nmotor configs · sim configs\nstatus · results")]
        GCR["Container Registry (GCR)\nAPI image · Worker image"]
    end

    subgraph CICD["CI / CD"]
        GHR["GitHub Release"]
    end

    User -->|"browse"| FE
    FE -->|"sign-in"| FA
    FA -->|"JWT ID token"| FE
    FE -->|"REST + Bearer JWT"| API
    API -->|"verify JWT"| FA
    API <-->|"motor &amp; sim CRUD"| GCS
    API -->|"trigger execution"| CloudRunJobs
    Worker <-->|"read config · write results"| GCS

    GHR -->|"build &amp; push images"| GCR
    GCR -->|"deploy"| CloudRunService
    GCR -->|"update job image"| CloudRunJobs
```

Users authenticate through Firebase. Every API request carries a Firebase ID token verified by the backend. Simulation jobs are dispatched as Cloud Run Job executions; the worker runs the `machwave` physics engine, writes results to GCS, and the API reads them back on demand.

## Main components

| Path | Purpose |
|---|---|
| `app/routers/motors.py` | Motor CRUD — stores configs as JSON in GCS |
| `app/routers/simulations.py` | Trigger + poll simulations via Cloud Run Jobs |
| `app/routers/usage.py` | Per-user account, quotas, admin limit overrides |
| `app/routers/propellants.py` | Read-only propellant catalogue |
| `app/credits/estimator.py` | Pre-run cost estimation engine |
| `app/repositories/account.py` | Per-user balance + caps + monthly grant rollover |
| `app/repositories/cost.py` | Per-simulation cost ledger |
| `app/schemas/` | Pydantic v2 request/response models |
| `app/storage/gcs.py` | Async GCS helpers |
| `app/auth/firebase.py` | Firebase ID token verification |
| `app/worker/run.py` | Cloud Run Jobs entry point |

## Credit system

Every simulation consumes **machwave tokens** (1 token ≈ 1 integration step). Each user has a per-user account at `users/{uid}/account.json` with their own caps and a usage counter — config supplies the defaults; admins can override per user via `PUT /admin/users/{uid}/limits`.

### Monthly limit

`tokens_used` starts at 0 and grows with each simulation. When it would exceed `monthly_token_limit` (default `10_000`, ≈ $0.10 USD) the next submit is rejected with **402**. The counter resets to 0 at the start of each calendar month (UTC). Admins bypass all checks.

### Pre-charge / post-charge flow

Submission and worker reconciliation are decoupled:

1. `POST /simulations` → estimator computes `estimated_tokens` from the motor + `d_t`. The estimate is added to `tokens_used` up front and saved to `users/{uid}/simulations/{sid}/cost.json`. If `tokens_used + estimated > monthly_token_limit`, the request returns **402**.
2. Worker runs, then computes `actual_tokens = len(motor_state.t)`. Depending on the delta:
   - Actual < estimate → subtract the difference from `tokens_used` (refund).
   - Actual > estimate → add the overage to `tokens_used` (best-effort if it would exceed the limit).
3. Failure subtracts the entire pre-charge from `tokens_used`. Deleting a simulation likewise refunds whatever was charged.

The estimator (`app/credits/estimator.py`) is intentionally a single function. Today it uses simple heuristics (web ÷ nominal burn rate for solids, propellant mass ÷ nominal flow for liquids); refine it without touching routers, the worker, or storage.

### Quota enforcement

- `account.motor_limit` (default 10) — checked on `POST /motors`.
- `account.simulation_limit` (default 10) — checked on `POST /simulations`. This is a *stored* count cap, not a rate limit.
- Both are config-defaulted, per-user mutable.

### Inspecting state

| Endpoint | Purpose |
|---|---|
| `GET /me/account` | Full account: caps, limit, usage, period |
| `GET /me/usage` | Counts vs caps + usage summary (the frontend's quota panel) |
| `POST /simulations/estimate` | Dry-run cost — does not create or charge |
| `GET /simulations/{id}/cost` | Per-sim estimated/actual breakdown |
| `GET /admin/users/{uid}/account` | Admin view of any user's account |
| `PUT /admin/users/{uid}/limits` | Admin override of `motor_limit`, `simulation_limit`, `monthly_token_limit` |

## Local development

Local is the only non-prod environment for the API. It points at a separate Firebase project (`machwave-dev`) and GCS bucket (`machwave-data-dev`) so day-to-day work doesn't touch prod users or data.

### Prerequisites

- Docker + Docker Compose
- A GCP service account key for `machwave-api-dev@machwave.iam.gserviceaccount.com` with roles: `Storage Object Admin` on `machwave-data-dev`, `firebaseauth.admin` on `machwave-dev`
- A `.env` file (copy `.env.example` and fill in values)
- Place the service account key at `./sa-key.json` (gitignored)

### Run

```bash
cp .env.example .env
# edit .env with your project values
make run
```

API is available at `http://localhost:8000`. Interactive docs at `http://localhost:8000/docs`.

When `ENV=local`, simulation requests spawn the worker as a subprocess inside the API container instead of submitting a Cloud Run Job. Status and results are still written to the (dev) GCS bucket exactly like in prod.

To run the worker manually for one simulation:

```bash
docker compose run --env SIM_ID=<id> --env USER_ID=<uid> worker
```

## Deployment

Only one Cloud Run environment exists: **prod**. Local development uses the dev Firebase project and dev GCS bucket for isolation, but there is no dev Cloud Run service.

Prod is triggered by publishing a GitHub release. The workflow runs tests, then calls `make deploy-prod`, which delegates to [`scripts/deploy.sh`](scripts/deploy.sh). The same `make` target works locally once authenticated:

```bash
gcloud auth login
gcloud auth configure-docker

make deploy-prod TAG=v1.2.3      # explicit tag required
```

GitHub secrets `WIF_PROVIDER` and `WIF_SERVICE_ACCOUNT` are stored on the `prod` GitHub Environment. Runtime config lives in [`deploy/prod/`](deploy/prod/).


## Commands

```bash
make install-dev               # install dependencies (dev)
make test                      # run tests
make check                     # format check + lint
make format                    # auto-format
make run                       # start local API with docker compose
make stop                      # stop
make deploy-prod TAG=v1.2.3    # deploy a tagged release to prod
```
