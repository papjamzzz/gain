# Gain — Re-Entry File
*Claude: read this before touching anything.*

---

## What This Is
Gain is a behavioral mixing board for AI. Four tracks control different dimensions of how Claude thinks: Mode, Confidence, Scope, Voice. Faders, knobs, and buttons write to a system prompt. Same prompt, different state — measurably different output.

**The core feature is Compare** — run the same prompt through two saved presets, get scored output on 6 metrics (Adherence, Depth, Clarity, Efficiency, Confidence, Token Efficiency) from a reasoning model, plus raw reply length and token counts. The scoring model writes a plain-English behavioral diagnosis. This is the product.

This repo is everything: landing page (SaaS), mixer UI, CLI tool, MIDI bridge.

## Re-Entry Phrase
> "Re-entry: gain"

## Current Status
🔨 Active — Compare engine live, preset system working, nanoKONTROL2 wired, scoring + stats database building

## Stack
- Python + Flask
- Mixer UI: port 5570 (mvm_ui.py), binds 0.0.0.0
- Landing page: port 5567 (app.py), binds 127.0.0.1
- Dark theme, Inter + Abril Fatface fonts
- CSS variables, no external frameworks

## File Structure
```
gain/
├── app.py              # Landing page + waitlist Flask app (port 5567)
├── mvm_ui.py           # Mixer UI Flask app (port 5570) — THE MAIN FILE
├── ctrl                # CLI tool — reads state.json, builds system prompts, calls claude
├── nano_bridge.py      # Korg nanoKONTROL2 MIDI bridge (PRIMARY hardware controller)
├── mpk_bridge.py       # MPK Mini 3 bridge (secondary, no LED feedback)
├── foot_bridge.py      # Foot controller bridge
├── templates/
│   └── index.html      # Landing page HTML
├── static/
├── data/
│   └── waitlist.json   # Email waitlist signups
├── requirements.txt
├── Makefile
├── launch.command
├── .env
└── .env.example
```

## How to Run
```bash
# Mixer UI (auto-starts via LaunchAgent com.papjamzzz.gain)
python3 /Users/miahsm1/gain/mvm_ui.py

# nanoKONTROL2 bridge (auto-starts via LaunchAgent com.papjamzzz.gain.nano)
python3 /Users/miahsm1/gain/nano_bridge.py --start

# Landing page
cd ~/gain && make run

# Run a task via CLI
ctrl run "your task here"
```

Mixer UI: http://127.0.0.1:5570
iPad: http://192.168.1.3:5570
Landing page: http://127.0.0.1:5567
Production: gain.creativekonsoles.com

## Shared State
`~/.streamfader/state.json` — shared between mixer UI, MIDI bridge, and CLI.
`~/.streamfader/presets/` — saved preset files (one JSON per preset).
`~/.streamfader/comparisons.json` — comparison run log, builds stats database over time.

## Key Infrastructure
- `ctrl` symlink: `/opt/homebrew/bin/ctrl` → `/Users/miahsm1/gain/ctrl`
- LaunchAgent: `com.papjamzzz.gain` — auto-starts mixer UI on boot
- LaunchAgent: `com.papjamzzz.gain.nano` — auto-starts nanoKONTROL2 bridge on boot
- Logs: `~/.streamfader/control.log` (mixer), `~/.streamfader/nano.log` (MIDI bridge)
- State resets to neutral (all 0.5, no buttons) on every server start by design

## GitHub
- Repo: papjamzzz/gain
- Push: `make m="your message" push`

## Track Layout
| Track | Fader | Knob | Buttons |
|-------|-------|------|---------|
| Track 1 — MODE | Effort (intensity) | Thinking Time (depth) | EXPLORE / MUTE / BUILD |
| Track 2 — CONFIDENCE | Confidence (certainty) | Boldness (risk) | LIST / MUTE / DECIDE |
| Track 3 — SCOPE | Zoom Level (scope) | Context Size (bandwidth) | FILE / MUTE / PROJECT |
| Track 4 — VOICE | Verbosity (room) | Memory Persistence (decay) | DIRECT / MUTE / OPEN |

## nanoKONTROL2 Mapping (PRIMARY — wins over MPK)
- Faders 1–4: Intensity, Certainty, Scope, Room
- Knobs 1–4: Depth, Risk, Bandwidth, Decay
- S buttons: EXPLORE / LIST / FILE / DIRECT
- M buttons: Mute T1 / T2 / T3 / T4
- R buttons: BUILD / DECIDE / PROJECT / OPEN
- PLAY: replay last task | STOP: kill running process
- LED feedback: fully wired via output port

## Compare Engine
- Route: POST /compare — SSE stream, runs prompt against two presets sequentially
- Scoring: claude-sonnet-4-6 scores 6 metrics (0-100) with winner + summary
- Metrics: Adherence, Depth, Clarity, Efficiency, Confidence, Token Efficiency
- Token counts: pulled from API usage data (hard numbers, not estimated)
- Reply length: word count from output text
- Stats log: every run appends to ~/.streamfader/comparisons.json
- Stats route: GET /compare/stats — per-preset averages across all past runs

## Roadmap
### Done
- [x] Landing page with waitlist
- [x] Mixer UI — all four tracks, muting, physical controller
- [x] CLI tool (ctrl) — system prompt builder, claude integration
- [x] nanoKONTROL2 bridge with LED feedback (PRIMARY controller)
- [x] Preset save/load system (accessible from main UI and Compare panel)
- [x] Compare engine — A/B preset comparison with 6-metric AI scoring
- [x] Token Efficiency metric + raw token/word count stats
- [x] Scoring stats database (local JSON, accumulates over time)
- [x] Onboarding rewritten around Compare + presets + scoring
- [x] GAIN all-caps, 72px header
- [x] State resets to neutral on every server start
- [x] Screen-recording optimized typography
- [x] Supabase auth (login/signup) + cloud state/presets
- [x] Stripe billing — webhook, subscriber DB, plan gate on Compare (free: 5, base: 100/mo, foundational: unlimited)

### Next
- [ ] Stripe billing tier wiring
- [ ] Model selector dial

## Last Session
2026-06-03 — Two major sessions. Liquid tank faders across main + prototype (full-width, fill from bottom, glowing surface line per track color). Button contrast fixed globally. Prototype page (/proto) built with all main screen controls ported. Gain hook wired into Claude Code terminal via UserPromptSubmit + CLAUDE.md injection — faders now control Claude Code behavior live (proven: LIST stopped implementation, DECIDE wrote one line and stopped). nanoKONTROL2 transport remapped: PLAY=Enter keystroke, STOP=SIGINT direct to claude process (works mid-response), RECORD=/clear. iPad fully optimized: touch targets, safe area insets, full-width panels, overscroll:none. COMPARE button lit as featured element (breathing glow pulse, backlit hardware look). 4K/HD media queries added with knob/type scaling. Expand/collapse button restored. Theme toggle moved to settings panel only.
