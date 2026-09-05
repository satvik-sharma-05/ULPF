# Synthesizer walkthrough — Voltrix Edge

`voltrix.py` is **not a shipped detector**, and is deliberately not registered.
"Voltrix Edge" is a fictional appliance invented to measure what
`batch/synth_parser.py` can and cannot do on a format that exists nowhere in
the codebase. Registering a detector for a vendor that does not exist would be
misleading in a handover, so it lives here instead.

Reproduce the measurement:

    python -m batch.synth_parser --file docs/examples/novel_source.log \
        --name voltrix_edge --only-unmatched

What was measured (2026-09-04):

| Step | Result |
|---|---|
| The 53 registered detectors, before | 6/6 lines to `generic_fallback` — no hostname, no severity, 1 attribute |
| Synthesizer runtime | 662 ms |
| Draft quality | correct regex for the largest shape, 83.3% of input, 18 fields typed |
| What it could not infer | `hostname: None` with a TODO; fields named `number2`/`field7`; the pipe-delimited date split into six separate integers |
| After human completion (`voltrix.py`) | 6/6 lines, both shapes, ISO timestamp, mapped severity, 10 attributes |

The conclusion the numbers support: **the synthesizer infers shape in under a
second; a human still assigns meaning.** That is the honest claim, and it is
narrower than "automatic parser generation".
