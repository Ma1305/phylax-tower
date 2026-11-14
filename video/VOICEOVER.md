# Phylax Tower: voiceover script

Read this for `vo.wav`. Each block must fit its time window; the captions are cut from the same text
(`video/script.json`, `vo_text`). Total: 59.5 s. Save the recording as `video/assets/vo.wav`, then `npm run render`.

## 1. incident · 0:00 to 0:10 (10 s, 22 words)

> March 2026, LaGuardia. A jet is cleared to land. Two minutes later, a fire truck is cleared to cross the same runway.

## 2. team · 0:10 to 0:14 (4 s, 10 words)

> We're Mani, Ariyan, Sahand and Amirali. This is Phylax Tower.

## 3. what · 0:14 to 0:22 (8 s, 20 words)

> A safety net for control towers. Agents hear every radio call, log it in MongoDB Atlas, and stop conflicting clearances.

## 4. catch · 0:22 to 0:38 (16 s, 34 words)

> Same calls, Safety on. ElevenLabs transcribes each call into Atlas. A change stream fires our rules. No LLM decides. The stop goes to the runway's controller, and Phylax Tower says it on the radio.

## 5. audit · 0:38 to 0:54 (16 s, 33 words)

> Every agent is auditable. Agents can only file intents; anything else is blocked and logged. Switch an agent off, and its next intent is refused. Then rewind the whole event from stored records.

## 6. close · 0:54 to 0:59.5 (5.5 s, 10 words)

> Phylax Tower. The AI can stop traffic, never clear it.

Total 129 words (about 130 words per minute).

If a block runs long, change that scene's `start_s` / `end_s` in `script.json` (see README, Retiming) rather than rushing the read.
