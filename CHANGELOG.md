# Changelog

## Unreleased

### Fixed

- Timing engine: send times are now guaranteed never to land on `:00`, `:30`,
  or a whole minute. The old code dodged forbidden minutes once, and a single
  jump could land on one again (seed 0, salt `m19` landed on `15:02:00`).
  The dodge and the dedupe steps now loop until the time is clean.
- Timing engine: a message arriving at 07:05 local time no longer sends
  before the 07:15 morning window opens. The night-hold check compared only
  the hour, so 07:00-07:14 slipped through.
- Policy engine: an opt-out now beats escalation for routing. A message like
  "stop texting me, this was terrible" used to route `ESCALATE`, so the
  pipeline drafted a reply and opened a human review for a contact who had
  just opted out. It now routes `SUPPRESS` with no draft and no review. A
  complaint without an opt-out still escalates. The same applies to a
  previously opted-out contact who messages again.
- Pipeline: prompt-injection detection no longer substring-matches the
  classifier's `_flags` string. It parses the `k=v` flags properly.

### Added

- `config/automation.toml`: the operator-editable automation thresholds that
  `pyproject.toml` already pointed at but that did not exist. Loaded with
  `AutomationConfig.from_toml()`. Values match the previous in-code defaults.
- API validation: inbound SMS text must be non-blank and at most 5000 chars;
  `client_id` and `contact_id` must be non-empty. Blank text previously
  reached the pipeline and was classified on empty input.
- CI: GitHub Actions runs the test suite and the demo on Python 3.11 and 3.12.

### Changed

- Quick start now shows macOS/Linux commands first, with Windows PowerShell
  alongside. The old README was PowerShell-only, so a stranger on a Mac or
  Linux box had to translate every step.

## 0.1.0 — 2026-08-27

- Initial prototype: event-sourced conversation state, multi-intent
  classification, deterministic policy and suppression engine, grounded
  context-aware replies, safety validators, human review, timing engine with
  queue invalidation, evaluation loop, FastAPI API, and web dashboard.
  93 tests passing.
