# Deploying Phylax Tower

Three pieces: the **tower** (`phylax-tower`, every process in one container via
`python -m holdshort.serve`), the **visualizer API** (`phylax-viz-api`, read-only) and the
**visualizer web app** (static, Firebase Hosting). The database is MongoDB Atlas; there is no
database container anywhere.

| File | What |
|---|---|
| `backend.Dockerfile` | tower image: python 3.12 + uv + ffmpeg, listens on `$PORT` (8000) |
| `viz-api.Dockerfile` | `visualizer/api`, uvicorn on `$PORT` (8100) |
| `viz-web.Dockerfile` | Vite build (`VITE_VIZ_API`, `VITE_TOWER_API` build args) served by nginx with SPA fallback on `$PORT` (8080) |
| `docker-compose.yml` | the three services for a local run (builds use `network: host`) |
| `docker-compose.smoke.yml` | override for a smoke run: throwaway database, host ports 18041 / 18141 / 18241 |
| `cloudbuild.yaml` | builds and pushes the tower and viz-api images with Cloud Build (no local Docker) |
| `cloudrun.sh` | builds, pushes and deploys the two APIs to Cloud Run, prints their URLs |
| `firebase.sh` | builds the web app against those URLs and runs `firebase deploy --only hosting` |

All images build with the **repo root** as context. `.dockerignore` keeps `.env`, `.git`,
`node_modules`, `logs` and `tools/fixtures/audio` out of every image.

## Local run (docker compose)

```bash
cp .env.example .env          # fill MONGODB_URI etc.; never commit it
sudo docker compose -f deploy/docker-compose.yml up --build -d
# tower     http://localhost:8000/health
# viz-api   http://localhost:8100/viz/health
# viz-web   http://localhost:5180
sudo docker compose -f deploy/docker-compose.yml down
```

The web app's API URLs are baked in at build time and are what the *browser* calls, so they are
host URLs (`http://localhost:8000`, `http://localhost:8100`), not compose service names. Override
with `VITE_TOWER_API=... VITE_VIZ_API=... docker compose ... build viz-web`.

## Smoke run (what was verified)

```bash
uv run python scripts/setup_atlas.py --db holdshort_v2_t41_docker      # throwaway database
sudo SMOKE_DB=holdshort_v2_t41_docker docker compose -f deploy/docker-compose.yml \
     -f deploy/docker-compose.smoke.yml up --build -d
curl localhost:18041/health          # routers: control, ingest, outbound
curl localhost:18141/viz/health      # and /viz/world?tower_id=LGA
curl localhost:18241/                # index.html; the bundle calls :18041 / :18141 (build args)
# POST one /ingest/positions batch and one /ingest/transcripts to :18041, then see the object
# and the transmission in :18141/viz/snapshot?tower_id=LGA and /viz/streams/LGA_TWR/clips.
sudo docker compose -p holdshort-smoke -f deploy/docker-compose.yml -f deploy/docker-compose.smoke.yml down
# then drop the database holdshort_v2_t41_docker
```

The override uses `network_mode: host` and sets `PORT`, so it also proves both API images
listen on `$PORT`. Both API images run as a non-root user (uid 10001) and contain no `.env`,
`.git`, `node_modules` or `tools/fixtures/audio`.

## Cloud deploy

Defaults (each overridable by the environment variable in brackets): project
`ambient-handler-460806-t7` (`PROJECT_ID`), region `us-east4` (`REGION`), runtime service account
`holdshort-run@ambient-handler-460806-t7.iam.gserviceaccount.com` (`SERVICE_ACCOUNT`; set it
empty to use the default compute account), Artifact Registry repository
`us-east4-docker.pkg.dev/ambient-handler-460806-t7/holdshort` (`REGISTRY`, or `AR_REPO` for the
last part), secrets `holdshort-mongodb-uri`, `holdshort-llm-api-key`,
`holdshort-elevenlabs-api-key`, Firebase project `hackathon-d0bf5` (`FIREBASE_PROJECT`), and
`CORS_ORIGINS=https://hackathon-d0bf5.web.app,https://hackathon-d0bf5.firebaseapp.com`.

### What must exist first

1. A Google Cloud project with billing, and the CLIs: `gcloud`, `docker`, `node`/`npm`,
   `firebase` (`npm i -g firebase-tools`), each signed in (`gcloud auth login`,
   `firebase login`).
2. APIs enabled: `gcloud services enable run.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com`.
3. An Artifact Registry docker repository (default name `holdshort`, override with `AR_REPO`):
   ```bash
   gcloud artifacts repositories create holdshort --repository-format docker --location "$REGION"
   gcloud auth configure-docker "$REGION-docker.pkg.dev"
   ```
4. Secret Manager secrets (names overridable with `SECRET_MONGODB_URI`, `SECRET_LLM_API_KEY`,
   `SECRET_ELEVENLABS_API_KEY`):
   ```bash
   printf %s "$MONGODB_URI"        | gcloud secrets create holdshort-mongodb-uri --data-file=-
   printf %s "$LLM_API_KEY"        | gcloud secrets create holdshort-llm-api-key --data-file=-
   printf %s "$ELEVENLABS_API_KEY" | gcloud secrets create holdshort-elevenlabs-api-key --data-file=-
   ```
   and grant the Cloud Run runtime service account
   (`holdshort-run@ambient-handler-460806-t7.iam.gserviceaccount.com`, or `SERVICE_ACCOUNT`)
   `roles/secretmanager.secretAccessor` on them.
5. **Atlas network access** for the Cloud Run egress address. Cloud Run has no fixed outbound IP
   by default. Either give it one (Direct VPC egress or a Serverless VPC connector, routed through
   a Cloud NAT with a reserved static IP; pass the flags in `VPC_FLAGS`, for example
   `VPC_FLAGS="--network=default --subnet=default --vpc-egress=all-traffic"`) and add that IP to
   the Atlas IP Access List, or, for a short-lived demo only, allow `0.0.0.0/0` in Atlas.
6. The Firebase project `hackathon-d0bf5` with Hosting enabled (the web app is served from
   https://hackathon-d0bf5.web.app; it calls the Cloud Run URLs directly).

### Steps

```bash
deploy/cloudrun.sh --dry-run     # read the commands first (defaults above)
deploy/cloudrun.sh               # prints TOWER_URL=... and VIZ_API_URL=...
deploy/firebase.sh --dry-run
deploy/firebase.sh               # builds visualizer/web with those URLs, deploys Hosting
```

`--dry-run` prints every command and runs none of them. Other knobs (environment variables):
`TAG`, `DOCKER` (e.g. `"sudo docker"`), `SKIP_BUILD=1` (images already pushed by Cloud Build),
`MONGODB_DB` (default `holdshort_v2`), `VOICE_ENABLED` (default `false`), `VPC_FLAGS`;
`firebase.sh` also takes `TOWER_URL` / `VIZ_API_URL` to skip the lookup.

### Without Docker: Cloud Build

```bash
TAG=$(git rev-parse --short HEAD)
gcloud builds submit --config deploy/cloudbuild.yaml --project ambient-handler-460806-t7 \
  --region us-east4 --substitutions _TAG=$TAG .          # from the repo root
SKIP_BUILD=1 TAG=$TAG deploy/cloudrun.sh
```
Substitutions: `_REGISTRY`, `_TOWER_IMAGE` (`tower`), `_VIZ_API_IMAGE` (`viz-api`), `_TAG`.

Tower: `--min-instances 1 --max-instances 1 --no-cpu-throttling --timeout 3600 --session-affinity`,
secrets `MONGODB_URI`, `LLM_API_KEY`, `ELEVENLABS_API_KEY` from Secret Manager, plain variables
`MONGODB_DB`, `VOICE_ENABLED`, `CORS_ORIGINS`. Viz API: `--timeout 3600`, `MONGODB_URI` from
Secret Manager.

## Why Cloud Run and not Cloud Functions

- The tower is not request-scoped. `holdshort.serve` keeps the gate, listener, state, memory,
  agents and speaker running as change-stream workers on Atlas the whole time. Cloud Functions
  only run while handling a request and are scaled to zero / CPU-throttled between requests, so
  those workers would stop. Cloud Run with `--min-instances 1 --no-cpu-throttling` keeps one warm
  instance with CPU always allocated; `--max-instances 1` keeps exactly one set of workers (two
  instances would each process every event).
- Both APIs push live updates over WebSockets. Cloud Run supports WebSockets for up to the
  request timeout (`--timeout 3600`, clients reconnect), and `--session-affinity` keeps a client
  on the same instance. Functions do not serve long-lived WebSockets.
- Firebase Hosting rewrites to Cloud Run do not carry WebSockets, so the web app does **not** go
  through a Hosting rewrite: it is built with `VITE_TOWER_API` and `VITE_VIZ_API` set to the Cloud
  Run URLs and calls them directly (CORS allows the Hosting domains via `CORS_ORIGINS`).

Nothing in this directory deploys on its own; run the scripts yourself.

## Troubleshooting

- `apt-get update` hangs during a build: the docker bridge network has no outbound access on
  this host (seen on the shared hackathon VM). The compose file already builds with
  `network: host`; with plain docker use `docker build --network host ...`. The smoke override
  also runs the containers with `network_mode: host` so they reach Atlas.
