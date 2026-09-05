# Implementation guide: charts, voice, and decks in a chat app

**Audience: an AI coding agent adding these features to an existing chatbot.**

This is a spec plus a list of traps. Everything below was built and verified end
to end; the "Traps" sections are failures that actually happened, not
hypotheticals. Read § 1 and § 2 before writing any code — the wire format is the
one decision everything else depends on.

Three features:

1. **Charts rendered inline in chat** — real interactive components, not images
2. **Text-to-speech and speech-to-text**
3. **PowerPoint (.pptx) export** with native, editable charts

Two deployment targets are covered. Build **§ 3–6** for a hosted API. If you are
targeting a local model (Ollama / llama.cpp / LM Studio), you **must also** read
**§ 7** — a naive port of § 3 to a local 4B model produced 206 seconds of prose
and no chart.

---

## 1. The core design decision

**The model emits fenced code blocks. The client parses them out of the message
and swaps in React components.**

````
Revenue climbed through 2024, dipping slightly in Q3.

```chart
{ "type": "bar", "title": "...", "xKey": "quarter", "series": [...], "data": [...] }
```

Costs grew steadily, widening the margin in Q4.
````

Everything good follows from this:

- **Streaming works.** Blocks are parsed incrementally out of the token stream.
- **Client and server decouple completely.** When I later replaced a hosted API
  with a local model, the parser, chart renderer, deck preview, and pptx builder
  needed **zero changes** — only spec *production* changed.
- **Charts and prose interleave** naturally in one message.
- **Failure is containable.** A malformed block degrades to a code block instead
  of taking down the message.

Do not invent a side-channel (JSON envelope, tool-call metadata, separate
endpoint). It couples your transport to your renderer and breaks streaming.

---

## 2. Data contracts

Define these once and share them between server and client.

```ts
export type ChartType = "bar" | "line" | "area" | "pie" | "scatter";

export interface ChartSpec {
  type: ChartType;
  title: string;
  xKey: string;                              // key in each row for the x axis
  series: { key: string; label?: string }[];
  data: Record<string, string | number>[];
  xLabel?: string;
  yLabel?: string;
  stacked?: boolean;
}

export interface DeckSpec {
  title: string;
  subtitle?: string;
  slides: {
    title: string;
    bullets?: string[];
    notes?: string;                          // speaker notes
    chart?: ChartSpec;                       // charts embed in slides
  }[];
}
```

### Validate before rendering — always

Model output is untrusted input. Write real predicates, not casts:

```ts
export function isChartSpec(v: unknown): v is ChartSpec {
  if (!v || typeof v !== "object") return false;
  const c = v as Partial<ChartSpec>;
  if (!["bar","line","area","pie","scatter"].includes(c.type as string)) return false;
  if (typeof c.xKey !== "string" || !c.xKey) return false;
  if (!Array.isArray(c.series) || c.series.length === 0) return false;
  if (!c.series.every(s => s && typeof s.key === "string" && s.key)) return false;
  if (!Array.isArray(c.data) || c.data.length === 0) return false;
  if (!c.data.every(row => row && typeof row === "object")) return false;

  // TRAP: xKey must actually exist in the rows. A model emitted
  // xKey:"Language" while keying every row on "x". It passed every other
  // check and rendered a chart with a blank category axis.
  if (!c.data.some(row => c.xKey! in row)) return false;

  // At least one series must resolve to a number somewhere, else it's blank.
  return c.series.some(s => c.data!.some(r => Number.isFinite(Number(r[s.key]))));
}
```

`isDeckSpec` is analogous: non-empty `title`, non-empty `slides`, every slide
has a `title`. When a *slide's* chart fails validation, drop that chart and keep
the deck — one bad chart must not void eight good slides.

---

## 3. Feature 1 — charts in chat

### 3a. Teach the model the format

Add to your system prompt. Be explicit about the row/series distinction; it is
the single most common thing models get wrong.

```
When a point is better made visually, emit a fenced block tagged `chart`
whose body is ONLY JSON:

```chart
{
  "type": "bar" | "line" | "area" | "pie" | "scatter",
  "title": "Short descriptive title",
  "xKey": "month",
  "series": [{ "key": "revenue", "label": "Revenue" }],
  "data": [{ "month": "Jan", "revenue": 120 }]
}
```

Rules:
- Each item the user lists is a ROW in "data", not a series. Usually there is
  exactly ONE series: the measured value.
- Every series "key" must appear in every row, and "xKey" must too.
- Numbers unquoted. No trailing commas. No comments. 2-40 rows.
- "pie" and "scatter" take exactly ONE series.
- "line"/"area" only when the x axis is ordered (time, sequence).
- Never two measures of wildly different scale in one chart — emit two charts.
- Always write a sentence naming what the chart shows. Never leave it bare.
- Chart the user's numbers exactly. If inventing figures, say they're illustrative.
```

If a model still confuses rows and series, replace the rules with a **worked
example** — small models follow shape far better than prose:

```
For "Python 41, JavaScript 38, Java 30" the answer is:
{"type":"bar","title":"Language usage","xKey":"language",
 "series":[{"key":"usage","label":"Usage (%)"}],
 "data":[{"language":"Python","usage":41},{"language":"JavaScript","usage":38},
         {"language":"Java","usage":30}]}
```

### 3b. The streaming-safe parser — the critical component

This runs on **every streamed token**. It must never leak half-arrived JSON into
the transcript. Copy this logic:

```ts
export type Segment =
  | { kind: "text";    text: string }
  | { kind: "chart";   spec: ChartSpec }
  | { kind: "deck";    spec: DeckSpec }
  | { kind: "pending"; label: "chart" | "deck" };   // fence open, not yet closed

const CLOSED = /```(chart|deck)[^\S\n]*\n([\s\S]*?)```/g;
const OPEN_TAIL = /```(chart|deck)[^\S\n]*\n?(?![\s\S]*```)/;

export function parseSegments(raw: string): Segment[] {
  const segments: Segment[] = [];
  let cursor = 0;

  CLOSED.lastIndex = 0;
  for (let m = CLOSED.exec(raw); m; m = CLOSED.exec(raw)) {
    pushText(segments, raw.slice(cursor, m.index));
    cursor = m.index + m[0].length;

    const [, label, bodyText] = m;
    let parsed: unknown = null;
    try { parsed = JSON.parse(bodyText.trim()); } catch { parsed = null; }

    if (label === "chart" && isChartSpec(parsed)) {
      segments.push({ kind: "chart", spec: parsed });
    } else if (label === "deck" && isDeckSpec(parsed)) {
      segments.push({ kind: "deck", spec: normalizeDeck(parsed) });
    } else {
      // Malformed: show it rather than lose it.
      pushText(segments, "```json\n" + bodyText.trim() + "\n```");
    }
  }

  const tail = raw.slice(cursor);
  const open = OPEN_TAIL.exec(tail);
  if (open) {
    pushText(segments, tail.slice(0, open.index));
    segments.push({ kind: "pending", label: open[1] as "chart" | "deck" });
  } else {
    pushText(segments, tail);
  }
  return segments;
}

function pushText(segs: Segment[], text: string) {
  if (text.trim()) segs.push({ kind: "text", text });
}
```

The `pending` segment is what makes this feel good: render a *"Building chart…"*
placeholder while the JSON streams in. Without it the user watches raw JSON
scroll past.

**Test it by replaying every prefix.** This is the test that matters:

```js
for (let i = 1; i <= sample.length; i++) {
  for (const seg of parseSegments(sample.slice(0, i))) {
    if (seg.kind === "text" && /"xKey"|"series"/.test(seg.text)) {
      throw new Error(`raw spec leaked at prefix length ${i}`);
    }
  }
}
```

### 3c. Rendering

Render each segment: `text` → markdown, `chart` → chart component, `deck` →
deck component, `pending` → spinner. **Memoize the message component** — a
streaming reply re-renders the whole transcript on every token, and without
memoization every settled chart re-renders too.

Any charting library works. With **Recharts** specifically, see § 3d.

Give every chart:
- a **title**
- a **legend** when ≥ 2 series, and **none** for a single series (the title names it)
- a **tooltip**
- a **table-view toggle** (see the colour note below)
- CSV export (cheap, and users want it)

### 3d. Traps — charts

**Recharts sorts the legend alphabetically by default.** `itemSorter` defaults
to `'value'`, so a legend reads "Costs, Revenue" while the bars are drawn
"Revenue, Costs". Pass `itemSorter={null}` on `<Legend>`, and a matching
comparator on `<Tooltip>`. In Recharts 3, `payload` is *omitted* from Legend's
prop type — you cannot pass it; use `itemSorter` plus the chart children.

**Never colour legend or axis text with the series colour.** Labels wear normal
text colour; the little swatch beside them carries the identity. Coloured label
text fails contrast and looks amateurish.

**Recharts line dots render hollow.** They read as white blobs on a dark
surface. Set `dot={{ fill: seriesColor, stroke: surfaceColor, strokeWidth: 2 }}`.

**Colours must be chosen, not inverted, for dark mode.** Pick separate hexes per
theme. Charting libraries need real colour strings, not CSS variables, so
resolve the theme in JS (watch both `prefers-color-scheme` and your theme
attribute via `MutationObserver`).

A palette validated for colour-vision deficiency, in fixed order — assign by
slot, never cycle:

| Slot | Light | Dark |
|---|---|---|
| 1 blue | `#2a78d6` | `#3987e5` |
| 2 orange | `#eb6834` | `#d95926` |
| 3 aqua | `#1baf7a` | `#199e70` |
| 4 yellow | `#eda100` | `#c98500` |
| 5 magenta | `#e87ba4` | `#d55181` |
| 6 green | `#008300` | `#008300` |
| 7 violet | `#4a3aa7` | `#9085e9` |
| 8 red | `#e34948` | `#e66767` |

Three light-mode hues fall under 3:1 against a light surface. That obligates
"relief": ship the **table-view toggle** so identity never depends on colour
alone. For **pie**, don't use categorical hues at all — slices encode magnitude,
so use one blue ramp light→dark.

**Never a dual-axis chart.** Two y-scales is the single most common charting
mistake. Two measures of different scale → two charts.

---

## 4. Feature 2 — text-to-speech

Two options. Pick per your constraints.

### 4a. Browser speech (free, offline, zero setup)

`window.speechSynthesis` drives the OS voices — SAPI on Windows, AVSpeech on
macOS. No network, no key, no model. Robotic but instant.

```ts
const utterance = new SpeechSynthesisUtterance(text);
utterance.voice = chosenVoice;   // from speechSynthesis.getVoices()
window.speechSynthesis.speak(utterance);
```

Traps:
- **`getVoices()` returns `[]` on first call in Chrome.** Listen for the
  `voiceschanged` event and re-read.
- **Filter to `voice.localService`** if offline behaviour matters — some voices
  are cloud-backed.
- `cancel()` fires `onerror` with `"interrupted"`/`"canceled"`. Don't surface
  those as errors.

### 4b. Hosted TTS

POST text, get audio back, play it via `new Audio(URL.createObjectURL(blob))`.
Keep the key server-side behind your own route.

Traps:
- **Voice names are provider-specific and undocumented in the obvious place.**
  Hit the API once and read the error — it usually lists the valid set. Validate
  the requested voice server-side and fall back rather than forwarding an
  unknown name and returning a dead 400.
- **Some models are gated behind a one-time terms acceptance** and return a 400
  until a human clicks accept in a console. Detect that specific error and fall
  back to browser speech, so the feature works on a fresh clone.

### 4c. What to actually speak

Do **not** read the raw message — it contains JSON. Reduce the parsed segments:

```ts
segments.map(seg =>
  seg.kind === "text"  ? stripMarkdown(seg.text)
: seg.kind === "chart" ? `Chart: ${seg.spec.title}.`
: seg.kind === "deck"  ? `Presentation: ${seg.spec.title}, ${seg.spec.slides.length} slides.`
: ""
).filter(Boolean).join(" ")
```

`stripMarkdown` should remove code fences, links, emphasis, headings, and table
pipes. Otherwise the voice reads asterisks aloud.

---

## 5. Feature 2b — speech-to-text

**Do not use the Web Speech `SpeechRecognition` API** unless you have verified
it is acceptable: Chrome implements it by streaming audio to Google's servers.
It is not local, despite living in the browser.

Use `MediaRecorder` → POST the blob → Whisper (hosted or local).

```ts
const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
const mimeType = ["audio/webm", "audio/mp4", "audio/ogg"]
  .find(t => MediaRecorder.isTypeSupported(t));
const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
```

Traps:
- **Safari cannot record webm.** Feature-detect as above.
- **Preserve the file extension** when uploading — Whisper picks its demuxer
  from the filename. Send `speech.webm` / `speech.m4a` / `speech.ogg` to match.
- **Always `stream.getTracks().forEach(t => t.stop())`** in `onstop`, including
  on the error path, or the mic indicator stays on.
- **Reject tiny blobs** (< ~1.2 KB) with "too short" rather than paying for a
  request that returns nothing.
- Put the transcript **in the composer for review**, don't auto-send it.

---

## 6. Feature 3 — PowerPoint export

Use **pptxgenjs**. It runs in the browser, so no server round-trip. It is ~1 MB
— import it lazily inside the click handler, not at module scope.

```ts
const { default: PptxGenJS } = await import("pptxgenjs");
const pptx = new PptxGenJS();
pptx.layout = "LAYOUT_16x9";

const slide = pptx.addSlide();
slide.addText(title, { x: 0.6, y: 0.45, w: 8.8, h: 0.7, fontSize: 26, bold: true });
slide.addText(
  bullets.map(text => ({ text, options: { bullet: { code: "2022" }, breakLine: true } })),
  { x: 0.7, y: 1.5, w: 8.6, h: 3.4, fontSize: 18, lineSpacingMultiple: 1.3 }
);
if (notes) slide.addNotes(notes);

await pptx.writeFile({ fileName: "deck.pptx" });
```

**Emit native charts, not images** — `slide.addChart(...)` produces a real
editable PowerPoint chart. This is the difference between a deliverable and a
screenshot.

Traps:
- **Scatter takes a different data shape** — the first entry is the X values:
  `[{name:"X-Axis", values:[...]}, {name:"Series", values:[...]}]`. Every other
  type uses `[{name, labels, values}]`.
- **Colours are bare hex, no `#`.**
- **Layout is inches**, 16:9 is 10 × 5.63.
- Give bullets ~half width when a chart shares the slide.
- Coerce every value with `Number.isFinite(n) ? n : 0` — one `null` corrupts the
  chart XML.

Also render an **in-chat preview** with slide navigation, so the user sees the
deck before downloading.

---

## 7. If you are targeting a LOCAL model — read this

Everything above assumes a capable hosted model. **A local 3–4B model will fail
§ 3a outright.** Measured, asking one in prose to emit a chart spec:

| | Freeform prompt | Schema-constrained |
|---|---|---|
| Wall time | **206 s** | **15 s** |
| Throughput | 5.3 tok/s | 13.4 tok/s |
| Prompt cost | 715 tok / 38 s | 69 tok / 1.5 s |
| Result | prose only, **no chart** | valid chart |

**14× faster, and the difference between working and not.** Restructure into
three phases:

### Phase 1 — classify intent with keywords, not the model

Asked to classify directly, a 4B model got **3 of 4 wrong** — it labelled an
explicit `"Bar chart: Python 41, ..."` request as `"none"`, and a
`"Build a 6-slide deck"` request as `"chart"`.

```ts
const DECK_RE  = /\b(deck|slides?|presentation|present|powerpoint|ppt|pptx|pitch)\b/i;
const CHART_RE = /\b(chart|graph|plot|histogram|scatter|visuali[sz]e|diagram)\b/i;
const IMPLICIT = /\b(compare|breakdown|trend|over time|distribution|by (?:region|month|quarter|year))\b/i;

export function keywordIntent(msg: string): "chart" | "deck" | null {
  if (DECK_RE.test(msg))  return "deck";        // deck wins: "deck with charts" is a deck
  if (CHART_RE.test(msg)) return "chart";
  if (IMPLICIT.test(msg) && (msg.match(/\b\d+([.,]\d+)?\b/g)?.length ?? 0) >= 3) return "chart";
  return null;                                   // only now ask the model
}
```

Instant, exact, and a wrong model answer now costs a *missing* chart rather than
a wrong one.

### Phase 2 — generate prose under a schema too

Attach a JSON schema (`format` in Ollama, GBNF in llama.cpp) even for plain
prose: `{"answer": "string"}`. Constrained decoding makes reasoning-leak
physically impossible.

To keep streaming, stream the JSON *string value* out incrementally: scan for
`"answer"\s*:\s*"`, then emit characters — handling `\n`, `\"`, `\\`, `\uXXXX` —
until the closing quote. ~40 lines, and you keep live output while guaranteeing
clean text.

### Phase 3 — generate the spec under a schema

Same schema as § 2. **Open the fence before you start decoding:**

```ts
send("\n\n```chart\n");          // client now shows "Building chart…"
const raw = await complete({ format: CHART_SCHEMA, ... });
// validate, then send the JSON and the closing fence
```

If validation fails, close the fence with `{}` and append an apology. Leaving it
open strands the spinner forever.

The response body stays plain markdown with fences — **so § 3b–3d, § 4, § 6 are
completely unchanged.** That is the payoff of § 1.

### Traps — local models

**Reasoning models leak their thinking.** Qwen3 ignores both `think: false` and
`/no_think`, streaming reasoning into normal `content` terminated by a bare
`</think>` **with no opening tag** — so neither the provider's `thinking` field
nor a `<think>`-anchored regex catches it. Defend twice: generate under a schema
(prevents it), and strip anything before the last `</think>` (catches residue).
Note a truncated budget yields *no* closing tag at all — which is exactly why
the schema, not the stripper, is the real fix.

**Prompt evaluation is expensive on CPU** (~40 tok/s). A 715-token system prompt
costs ~38 s *before the first output token*. Keep local prompts under ~100
tokens and trim history aggressively (last ~4 turns). Strip prior charts/decks
from history — they are huge and add nothing.

**Set generation ceilings per phase** (prose ~320, chart ~700, deck ~2200) and
expect roughly: question 25 s, chart 35 s, deck 100 s on CPU. Tell the user;
silence reads as a hang.

---

## 8. Cross-cutting traps

**Environment variable precedence.** Next.js (and most frameworks) let real
process env vars override `.env.local`. A stale machine-wide `GROQ_API_KEY` from
another project silently won, and the app authenticated as the wrong account —
invisible for hours because most endpoints aren't account-gated. Use a
project-specific name (`MYAPP_API_KEY`) with the generic one as fallback.

**Hosted providers count `max_tokens` against your rate limit.** A 8192-token
ceiling on an 8000 TPM tier fails *every* request before generating anything.

**`next/font/google` downloads fonts at BUILD time.** Fine normally; it makes
the build impossible offline. Use a system font stack or `next/font/local`.

**A theme script in `<head>` causes hydration warnings.** Stamping `data-theme`
before React hydrates makes server and client markup differ by design — add
`suppressHydrationWarning` to `<html>`.

**Auto-scroll must yield to the user.** Only scroll when they're already near
the bottom (within ~120 px), or reading an earlier chart yanks them away
mid-stream.

**Support abort.** Long generations need a stop button —
`AbortController`, and propagate the signal to the upstream request so the
model actually stops.

---

## 9. Verify it

Don't declare done on a green build. These are the checks that caught real bugs:

1. **Prefix-replay the parser** (§ 3b) — proves no raw JSON ever leaks.
2. **Malformed spec** → degrades to a code block, message survives.
3. **Schema violations rejected** — series key absent from rows; unknown chart
   type; `xKey` missing from rows.
4. **Build a .pptx from a real generated deck**, then assert it's a valid zip
   (`PK` magic) containing `ppt/slides/slide1.xml` and `ppt/charts/chart1.xml`.
   Exercise *every* chart type, not just the one the model happened to pick.
5. **Round-trip the voice features**: synthesize a known sentence, transcribe
   it, assert the text matches. Decode the audio and check amplitude — a valid
   WAV header proves nothing about whether it's silence.
6. **Screenshot the UI in both themes** and look at it. This is what caught the
   reversed legend, the coloured legend text, and the hollow dots — none of
   which any type check or unit test would ever surface.