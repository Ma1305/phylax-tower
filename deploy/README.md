# Deploying Phylax Tower

Three pieces: the **tower** (every process in one container, `python -m holdshort.serve`), the
**visualizer API** (read-only) and the **visualizer web app** (static files). The database is
MongoDB Atlas; there is no database container anywhere.

| File | What it is |
|---|---|
| `backend.Dockerfile` | Tower image: Python 3.12, uv, ffmpeg; listens on `$PORT` (8000) |
| `viz-api.Dockerfile` | `visualizer/api`, uvicorn on `$PORT` (8100) |
| `viz-web.Dockerfile` | Vite build (`VITE_VIZ_API`, `VITE_TOWER_API` build args) served by nginx on `$PORT` (8080) |
| `docker-compose.yml` | The three services for a local run |
| `docker-compose.smoke.yml` | Override for a smoke run: throwaway database, host ports 18041 / 18141 / 18241 |
| `cloudbuild.yaml` | Builds and pushes the two API images with Cloud Build (no local Docker) |
| `cloudrun.sh` | Builds, pushes and deploys the two APIs to Cloud Run and prints their URLs |
| `firebase.sh` | Builds the web app against those URLs and deploys it to Firebase Hosting |

All images build with the **repo root** as context. `.dockerignore` keeps `.env`, `.git`,
`node_modules` and `logs` out of every image. Both API images run as a non-root user.

## Local run (Docker Compose)

```bash
cp .env.example .env          # set MONGODB_URI; never commit it
docker compose -f deploy/docker-compose.yml up --build -d
# tower     http://localhost:8000/health
# viz-api   http://localhost:8100/viz/health
# web app   http://localhost:5180
docker compose -f deploy/docker-compose.yml down
```

The web app's API URLs are baked in at build time and are what the *browser* calls, so they are
host URLs, not Compose service names. Override them with `VITE_TOWER_API=... VITE_VIZ_API=...`.

## Cloud deploy

```bash
deploy/cloudrun.sh --dry-run     # prints every command and runs none
deploy/cloudrun.sh               # prints TOWER_URL=... and VIZ_API_URL=...
TOWER_URL=... VIZ_API_URL=... deploy/firebase.sh
```

What must exist first:

1. A Google Cloud project with billing, the `gcloud`, `docker` and `firebase` CLIs signed in,
   and the Cloud Run, Artifact Registry and Secret Manager APIs enabled.
2. An Artifact Registry Docker repository (default name `holdshort`, `AR_REPO`).
3. Secret Manager secrets `holdshort-mongodb-uri`, `holdshort-llm-api-key` and
   `holdshort-elevenlabs-api-key` (names overridable with `SECRET_*`), readable by the Cloud Run
   runtime service account.
4. Atlas network access for Cloud Run's outbound address: a static IP through a VPC connector
   and Cloud NAT (pass the flags in `VPC_FLAGS`), or `0.0.0.0/0` for a short demo only.
5. A Firebase project with Hosting enabled.

The defaults in the scripts (`PROJECT_ID`, `REGION`, `FIREBASE_PROJECT`, `SERVICE_ACCOUNT`,
`CORS_ORIGINS`) name the projects used during the build; set those variables for your own.
Other knobs: `TAG`, `DOCKER`, `SKIP_BUILD=1` (images already pushed by Cloud Build),
`MONGODB_DB` (default `holdshort_v2`), `VOICE_ENABLED` (default `false`).

Two things to know before running them as they are:

- `cloudrun.sh` names the services `holdshort-tower` and `holdshort-viz-api`, while
  `firebase.sh` looks up `phylax-tower` and `phylax-viz-api`. Pass `TOWER_URL` and
  `VIZ_API_URL` to `firebase.sh`, as above, and the lookup is skipped.
- `cloudrun.sh` does not set the Vertex AI variables. For Gemini on Vertex AI add
  `VERTEX_PROJECT` (and optionally `LLM_PROVIDER=vertex`) to the tower service and give its
  service account access to Vertex AI. With only the `LLM_API_KEY` secret set, the tower uses
  the Anthropic provider instead (see `holdshort/llm.py`).

## Why Cloud Run and not Cloud Functions

- The tower is not request-scoped. Its gate, listener, state, memory, agents and speaker run as
  change-stream workers the whole time. Cloud Run with `--min-instances 1 --no-cpu-throttling`
  keeps one warm instance with CPU always allocated, and `--max-instances 1` keeps exactly one
  set of workers (two instances would each process every event).
- Both APIs push live updates over WebSockets, which Cloud Run carries for up to the request
  timeout (`--timeout 3600`; clients reconnect).
- Firebase Hosting rewrites do not carry WebSockets, so the web app does not go through a
  rewrite: it is built with the Cloud Run URLs and calls them directly, and `CORS_ORIGINS` on
  the APIs allows the Hosting domains.

## Troubleshooting

`apt-get update` hangs during a build: the Docker bridge network has no outbound access on that
host. The Compose file already builds with `network: host`; with plain Docker use
`docker build --network host ...`. The smoke override also runs the containers on the host
network so they reach Atlas.
