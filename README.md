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
| `app/routers/propellants.py` | Read-only propellant catalogue |
| `app/schemas/` | Pydantic v2 request/response models |
| `app/storage/gcs.py` | Async GCS helpers |
| `app/auth/firebase.py` | Firebase ID token verification |
| `app/worker/run.py` | Cloud Run Jobs entry point |

## Local development

### Prerequisites

- Docker + Docker Compose
- A GCP service account key with roles: `Storage Object Admin`, `Firebase Auth` read, `Cloud Run Jobs` invoker
- A `.env` file (copy `.env.example` and fill in values)
- Place the service account key at `./sa-key.json` (gitignored)

### Run

```bash
cp .env.example .env
# edit .env with your project values
make up
```

API is available at `http://localhost:8000`. Interactive docs at `http://localhost:8000/docs`.

To run the worker manually:

```bash
docker compose run --env SIM_ID=<id> --env USER_ID=<uid> worker
```

## Deployment

Two environments live in the same `machwave` GCP project, distinguished by a `-dev` / `-prod` suffix on every resource (Cloud Run service, worker job, GCS bucket, runtime SA, GCR image). Each environment has its own Firebase project so user pools are isolated:

| Env | Trigger | Firebase project | Image tag |
|---|---|---|---|
| dev | push to `main` | `machwave-dev` | git SHA |
| prod | GitHub release published | `machwave-76f5f` | release tag |

The workflow runs tests, then calls `make deploy-dev` or `make deploy-prod`, which delegate to [`scripts/deploy.sh`](scripts/deploy.sh). The same `make` targets work locally once you've authenticated:

```bash
gcloud auth login
gcloud auth configure-docker

make deploy-dev                  # uses current git SHA as the image tag
make deploy-prod TAG=v1.2.3      # explicit tag required
```

Per-environment GitHub secrets (`WIF_PROVIDER`, `WIF_SERVICE_ACCOUNT`) are stored on the `dev` and `prod` GitHub Environments. Per-environment runtime config lives in [`deploy/dev/`](deploy/dev/) and [`deploy/prod/`](deploy/prod/).

## Commands

```bash
make install-dev               # install dependencies (dev)
make test                      # run tests
make check                     # format check + lint
make format                    # auto-format
make up                        # start local API with docker compose
make down                      # stop
make deploy-dev                # deploy current branch to dev
make deploy-prod TAG=v1.2.3    # deploy a tagged release to prod
```
