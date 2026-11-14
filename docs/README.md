# Documentation

Start with the top-level [`README.md`](../README.md). The design documents are in `v2/`: the
system in this repository is the second version built on the day, hence the name.

| Document | What it covers |
|---|---|
| [`v2/ARCHITECTURE.md`](v2/ARCHITECTURE.md) | One diagram, how a conflict flows through the system, and the controller's screen |
| [`v2/PLAN.md`](v2/PLAN.md) | The design: the three systems, the design decisions and why, the processes, hosting |
| [`v2/CONTRACTS.md`](v2/CONTRACTS.md) | Every collection shape and enum, the gate's decision table and reason codes, the `/control/*` and `/viz/*` APIs |
| [`v2/SIMULATOR_CONTRACT.md`](v2/SIMULATOR_CONTRACT.md) | The `/ingest/*` API: how positions, radio clips and weather enter the tower, and how traffic hears what the tower said |
| [`v2/world_lga.json`](v2/world_lga.json) | The seeded airport: a schematic of LaGuardia with its lanes, hold points, controllers and radio streams |
| [`../TESTING.md`](../TESTING.md) | Running the stack and clicking through an incident; the automated checks |

Screenshots of the web app are in [`v2/screens/`](v2/screens/): the
[live view with a stop in effect](v2/screens/live-stop-in-effect.png),
[highlights playback in history mode](v2/screens/history-highlights.png), the
[settings panel](v2/screens/settings.png) and the [audit log](v2/screens/audit-log.png). They
show the app's built-in scripted demo data (`?mock=1`).

Identifiers still say `holdshort`: Phylax Tower was called Hold Short during the build, and the
package, the database (`holdshort_v2`) and the station name (`HOLDSHORT`) kept that name.
