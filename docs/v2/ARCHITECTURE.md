# Hold Short v2: architecture

Three separate systems around one MongoDB Atlas database. Details: `PLAN.md` (design),
`CONTRACTS.md` (data and APIs), `SIMULATOR_CONTRACT.md` (how data enters).

```mermaid
flowchart TB
  SIM["<b>Simulator</b> (separate)<br/>positions, radio clips per stream, weather<br/>normal and incident scenarios"]

  subgraph TOWER["Tower: the main system (one Docker container, Cloud Run)"]
    direction TB
    API["<b>API</b><br/>/ingest (data in)<br/>/control (controller actions)"]
    LIS["<b>Listener</b><br/>speech to text<br/>many streams at once"]
    STA["<b>State</b><br/>objects, clearances<br/>lane status"]
    MEM["<b>Memory</b><br/>small expiring working set<br/>agents search this, not the log"]
    AG["<b>Agents</b><br/>safety (rules), procedures (LLM), weather<br/>they only insert intents"]
    GATE["<b>Gate</b><br/>policy before action:<br/>switch, mode, tier, lane owner"]
    SPK["<b>Speaker</b><br/>speaks executed stops and<br/>approved instructions"]
    API --> LIS --> STA --> AG
    MEM --> AG
    AG -->|intent| GATE -->|decision| SPK
  end

  DB[("<b>MongoDB Atlas</b> holdshort_v2<br/>world: towers, lanes, controllers, streams<br/>live: objects, lane_status, context (TTL)<br/>log (insert-only): transmissions, audio, intents,<br/>decisions, positions, state_events, security_events<br/>validators + change streams + per-agent access")]

  subgraph VIZ["Visualizer: separate system (Cloud Run + Firebase Hosting)"]
    direction TB
    VAPI["<b>Read-only API</b><br/>live feed, time travel"]
    WEB["<b>Web app</b><br/>map of lanes and objects<br/>radio streams with audio, timeline<br/>per-controller view, agent requests"]
    VAPI --> WEB
  end

  CTRL(["<b>Controllers</b><br/>each owns lanes and streams"])

  SIM -->|HTTP /ingest| API
  SPK -.->|what Hold Short said| SIM
  TOWER <--> DB
  DB --> VAPI
  CTRL --> WEB
  WEB -->|approve, reject, revoke,<br/>agent switches| API
```

How a conflict flows:

1. The simulator posts positions once a second and each radio call as an audio clip on its stream.
2. The listener turns clips into transcripts; the state service works out who is cleared onto,
   and who is physically on, each lane.
3. The safety agent sees a conflict and inserts an intent. It cannot act.
4. The gate checks the agent's switch, mode and tier and writes a decision, routed to the
   controller who owns the lane. A critical stop runs at once; anything else waits for that
   controller.
5. The speaker says the executed call on the right frequency. The simulator's traffic obeys.
6. Every step is a document in Atlas, so the visualizer can replay any past moment.
