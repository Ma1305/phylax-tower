# Phylax Tower: voiceover script

Read this for `vo.wav`. Each block must fit its time window; the captions are cut from the same text
(`video/script.json`, `vo_text`).
Total: 59.5 s. Record one continuous take from 0:00, save as `video/assets/vo.wav`, then `npm run render`.

## 1. incident · 0:00 to 0:10 (10 s, 28 words)

> March 2026. LaGuardia, runway four. A jet is cleared to land. Two minutes later, a fire truck is cleared to cross the same runway. Two clearances. One runway.

## 2. team · 0:10 to 0:14 (4 s, 13 words)

> We're Mani, Ariyan, Sahand, Amirali. We built Phylax Tower.

## 3. what · 0:14 to 0:22 (8 s, 26 words)

> Phylax Tower is a safety net for control towers. Agents listen to every radio stream, log each call in MongoDB Atlas, and stop clearances that conflict.

## 4. catch · 0:22 to 0:38 (16 s, 43 words)

> Same calls, Safety on. ElevenLabs cleans and transcribes each transmission into Atlas. A change stream fires our rule engine: deterministic, no LLM in the decision. The stop goes to the controller who owns the runway, and Phylax Tower says it on the radio.

## 5. audit · 0:38 to 0:54 (16 s, 40 words)

> And every agent is auditable. Agents can only file intents; three layers block anything else, and every attempt is logged. Switch an agent off and its next intent is refused. Then rewind: the whole event, rebuilt from stored records alone.

## 6. close · 0:54 to 0:59.5 (5.5 s, 10 words)

> Phylax Tower. The AI can stop traffic, never clear it.

Total 160 words (about 161 words per minute).

If a block runs long, change that scene's `start_s` / `end_s` in `script.json` (see README, Retiming) rather than rushing the read.
