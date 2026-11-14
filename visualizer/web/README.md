# Hold Short visualizer: web app

The screen controllers look at. A separate system from the tower: it reads everything from the
visualizer API (`/viz/*`, see `docs/v2/CONTRACTS.md`) and sends controller actions to the
tower's `/control/*` API. It never talks to the database.

- **Left**: top-down map drawn from the world data (SVG, no tiles, no keys). Lanes by
  `lane_status` state (colour + pattern + text tag), hold points, objects by kind (shape),
  "Airfield" and "Approach · 7 nm" zooms, airborne list. Next to it: agent requests
  awaiting approval (Approve / Reject), the decision log (executed, pending, refused, rejected,
  lifted; each with rule, authorized_by and approved_by), agent switches and the master switch.
- **Top, only while a stop is in effect**: stops are autonomous by default, so an executed stop is
  an alert the owning controller is told about, not asked about: the exact call, rule, target,
  lane and a **Lift stop** action (`POST /control/revoke`). Hold Short's own outbound call is
  marked in its stream. Only proposals (and stops from a `needs_approval` agent) wait for approval.
- **Right**: one card per radio stream with its transcript and audio. Pick a stream to solo it:
  its new clips play as they arrive; with "All" nothing plays by itself.
- **Bottom**: the timeline. Drag it to see the airfield as it was (history mode: amber frame,
  banner, actions disabled); LIVE returns to now.
- **View as**: All, or one controller: their lanes highlighted, their streams, their requests.

## Run

```bash
cd visualizer/web
npm install
npm run dev -- --port 5180        # http://localhost:5180
```

Without a backend, open **http://localhost:5180/?mock=1**: a scripted runway-incursion scenario
generated in the browser from `src/mock/world_lga.json` (a copy of `docs/v2/world_lga.json`),
with three minutes of history. Mock audio is a placeholder tone per clip.

## Configuration

Copy `.env.example` to `.env` (gitignored). Values are read at build time.

| Variable | Default | Used for |
|---|---|---|
| `VITE_VIZ_API` | `http://localhost:8100` | `/viz/*`, `/viz/audio/{clip_id}` and the `/viz/live` WebSocket (connected directly) |
| `VITE_TOWER_API` | `http://localhost:8000` | `POST /control/approve`, `/control/reject`, `/control/revoke`, `/control/permissions/{agent}` |

## Build

```bash
npm run build        # type-check (tsc) + vite build -> dist/
npm run preview -- --port 18531
```

## Deploy (Firebase Hosting)

`firebase.json` serves `dist/` with an SPA fallback. Firebase rewrites do not carry WebSockets,
so the app connects straight to the API URLs baked in at build time.

```bash
cp .firebaserc.example .firebaserc            # set your project id (gitignored)
VITE_VIZ_API=https://<viz-api-host> VITE_TOWER_API=https://<tower-api-host> npm run build
npx firebase-tools deploy --only hosting
```

The tower's `CORS_ORIGINS` must include the hosting origin.

## Paired check and screenshots

`scripts/run_pair.sh` seeds a scratch database (`holdshort_v2_t<task>_pair`) with
`../api/seed_demo.py`, starts the visualizer API and the built web app on free ports in
18000-18999 (not mock mode), drives the screen with Playwright (`scripts/pair.mjs`), then stops
both and drops the database. The tower API is deliberately absent: actions must fail inline.

```bash
npx playwright install chromium
npm run pair               # the checks
npm run screenshots        # the checks, and retake docs/*.png
```

`docs/` holds the review screenshots: live, inline action error, solo stream, Ground and Local
views, approach zoom, two history views, 1100 px and 480 px windows (all from the seeded API) and
an active conflict (mock mode).

## Layout of the code

```
src/types.ts            contract shapes + the DataSource interface
src/source.ts           HTTP/WebSocket source (VITE_VIZ_API, VITE_TOWER_API); picks mock on ?mock=1
src/mock/               the mock source: scenario, placeholder audio
src/App.tsx             live/history state, audio queue, controller filter
src/components/         MapView, Requests (+ audit trail), Agents, Streams, Timeline
```
