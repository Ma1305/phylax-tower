# visualizer: the controller's screen

A separate system from the tower. It imports nothing from `holdshort/` and never writes to the
database.

| Folder | What it is |
|---|---|
| [`api/`](api/) | Read-only FastAPI service over the stored records: the live picture, a WebSocket of changes, and any past moment rebuilt from the logs (`/viz/*`) |
| [`web/`](web/) | React and Vite app: airfield map, radio streams with audio, timeline with time travel and highlights, the "Needs you" panel, agent settings and the audit log |

Controller actions (approve, reject, lift a stop, agent switches, the audit question) do not go
through the visualizer API: the web app sends them straight to the tower's `/control/*` API.

```bash
scripts/run_all.sh                                    # from the repo root: tower, this API and the web app
cd visualizer/web && npm ci && npm run dev -- --port 5180   # the web app alone; add ?mock=1 for scripted data
```

Each folder has its own README with routes, settings and checks.
