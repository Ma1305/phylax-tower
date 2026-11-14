# Hold Short visualizer: web app

The screen controllers look at. A separate system from the tower: it reads everything from the
visualizer API (`/viz/*`, see `docs/v2/CONTRACTS.md`) and sends controller actions to the
tower's `/control/*` API. It never talks to the database.

- **Left**: top-down map drawn from the world data (SVG, no tiles, no keys). Lanes by
  `lane_status` state (colour + pattern + text tag), hold points, objects by kind (shape),
  "Airfield" and "Approach · 7 nm" zooms, airborne list. Next to it: agent requests
  (Approve / Reject), the audit trail, agent switches and the master switch.
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
| `VITE_TOWER_API` | `http://localhost:8000` | `POST /control/approve`, `/control/reject`, `/control/permissions/{agent}` |

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

## Screenshots

`docs/` holds review screenshots taken from mock mode. To retake them:

```bash
npx playwright install chromium
npm run build && npm run preview -- --port 18531 &
npm run screenshots        # or: node scripts/screenshots.mjs http://localhost:5180
```

## Layout of the code

```
src/types.ts            contract shapes + the DataSource interface
src/source.ts           HTTP/WebSocket source (VITE_VIZ_API, VITE_TOWER_API); picks mock on ?mock=1
src/mock/               the mock source: scenario, placeholder audio
src/App.tsx             live/history state, audio queue, controller filter
src/components/         MapView, Requests (+ audit trail), Agents, Streams, Timeline
```
