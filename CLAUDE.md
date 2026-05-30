# Gain — Re-Entry File
*Claude: read this before touching anything.*

---

## What This Is
Gain is a behavioral mixing board for AI coding agents. Four tracks, each controlling a different dimension of how Claude thinks: Mode, Confidence, Scope, Voice. Faders, knobs, and buttons write to a system prompt in real time. Same task, different state — measurably different output.

This repo is everything: landing page (SaaS), mixer UI, CLI tool, MIDI bridge.

## Re-Entry Phrase
> "Re-entry: gain"

## Current Status
🔨 Active — landing page live, mixer UI rebranded, SaaS auth next

## Stack
- Python + Flask
- Mixer UI: port 5570 (mvm_ui.py), binds 0.0.0.0
- Landing page: port 5567 (app.py), binds 127.0.0.1
- Dark theme, Inter + Abril Fatface + JetBrains Mono fonts
- CSS variables, no external frameworks

## File Structure
```
gain/
├── app.py              # Landing page + waitlist Flask app (port 5567)
├── mvm_ui.py           # Mixer UI Flask app (port 5570)
├── ctrl                # CLI tool — reads state.json, calls claude --print
├── nano_bridge.py      # Korg nanoKONTROL2 MIDI bridge
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

# Landing page
cd ~/gain && make run

# MIDI bridge
ctrl nano --start

# Run a task
ctrl run "your task here"
```

Mixer UI: http://127.0.0.1:5570
iPad: http://192.168.1.3:5570
Landing page: http://127.0.0.1:5567
Production: gain.creativekonsoles.com

## Shared State
`~/.streamfader/state.json` — shared between mixer UI, MIDI bridge, and CLI.

## Key Infrastructure
- `ctrl` symlink: `/opt/homebrew/bin/ctrl` → `/Users/miahsm1/gain/ctrl`
- LaunchAgent: `com.papjamzzz.gain` — auto-starts mixer on boot
- Old `~/control` repo: archived at papjamzzz/control on GitHub

## GitHub
- Repo: papjamzzz/gain
- Push: `make m="your message" push`

## Track Layout
| Track | Fader | Knob | Buttons |
|-------|-------|------|---------|
| Track 1 — MODE | Intensity | Depth | EXPLORE / MUTE / BUILD |
| Track 2 — CONF | Certainty | Risk | LIST / MUTE / DECIDE |
| Track 3 — SCOPE | Scope | Bandwidth | FILE / MUTE / PROJECT |
| Track 4 — VOICE | Room | Decay | DIRECT / MUTE / OPEN |

## nanoKONTROL2 Mapping
- Faders 1–4: Intensity, Certainty, Scope, Room
- Knobs 1–4: Depth, Risk, Bandwidth, Decay
- S buttons: EXPLORE / LIST / FILE / DIRECT
- M buttons: Mute T1 / T2 / T3 / T4
- R buttons: BUILD / DECIDE / PROJECT / OPEN
- PLAY: replay last task | STOP: kill running process

## Roadmap
### Done
- [x] Landing page with waitlist
- [x] Mixer UI (mvm_ui.py) — all four tracks, muting, physical controller
- [x] CLI tool (ctrl) — reads state.json, builds system prompts, calls claude
- [x] MIDI bridge (nano_bridge.py)
- [x] Rebranded from Control → Gain
- [x] Live prompts removed, output window expanded

### Next
- [ ] Clerk auth (login/signup)
- [ ] Supabase (cloud state + presets)
- [ ] Stripe billing tier wiring
- [ ] Preset save/load system
- [ ] Model selector dial

## Last Session
2026-05-30 — Repo merged (control → gain). UI polish: live prompts removed, output expanded to JetBrains Mono readout, knob depth improved, palette brightened, Track 1–4 labels, bank headers removed, iPad optimizations, ? button resized. Landing page built with waitlist. Clerk auth is next.
