# Gain

**A behavioral mixing board for AI coding agents.**

Same task. Different state. Measurably different output.

---

## What it is

Gain maps physical controls — faders, knobs, buttons — to behavioral parameters that shape how Claude Code thinks and acts. Every move writes to a system prompt in real time. You perform instead of prompt.

Four tracks. Each one controls a different dimension:

| Track | Fader | Knob | Buttons |
|-------|-------|------|---------|
| **Mode** | Intensity | Depth | EXPLORE / BUILD |
| **Confidence** | Certainty | Risk | LIST / DECIDE |
| **Scope** | Scope | Bandwidth | FILE / PROJECT |
| **Voice** | Room | Decay | DIRECT / OPEN |

Set MODE to EXPLORE, stance to LIST. Same task. Set MODE to BUILD, stance to DECIDE. The outputs are measurably different. That's the machine.

---

## Quick start

```bash
# 1. Init state file
ctrl init

# 2. Start the visual console
python3 mvm_ui.py
# → http://127.0.0.1:5570

# 3. (Optional) Start MIDI bridge for Korg nanoKONTROL2
ctrl nano --start

# 4. Run a task
ctrl run "refactor the auth module"
```

---

## The mixer UI

Open `http://127.0.0.1:5570` in your browser (or on an iPad at your local IP).

- **Faders** — drag vertically. Hold Shift for fine mode. Double-click to reset to 0.50.
- **Knobs** — drag vertically. Arc fills to show position. Glow intensity scales with value.
- **Buttons** — click to activate. Click again to deselect. Middle button mutes the track.
- **◉** — per-track power button. Muted tracks contribute nothing to the system prompt.
- **LAUNCH** — runs `ctrl run` with full Claude Code tool access.
- **PREVIEW RUN** — calls Claude API directly (no file access). Shows what the current state produces.

---

## Physical controller

Korg nanoKONTROL2 mapping:

| Control | Parameter |
|---------|-----------|
| Faders 1–4 | Intensity, Certainty, Scope, Room |
| Knobs 1–4 | Depth, Risk, Bandwidth, Decay |
| S buttons | EXPLORE / LIST / FILE / DIRECT |
| M buttons | Mute Track 1 / 2 / 3 / 4 |
| R buttons | BUILD / DECIDE / PROJECT / OPEN |
| PLAY | Replay last task |
| STOP | Kill running process |

---

## Architecture

```
gain/
├── mvm_ui.py        # Mixer UI — Flask + SSE + inline HTML/CSS/JS
├── ctrl             # CLI — reads state.json, builds system prompts, calls claude
├── nano_bridge.py   # MIDI bridge — maps nanoKONTROL2 CC to state.json
├── foot_bridge.py   # Foot controller bridge
└── app.py           # Landing page + waitlist (gain.creativekonsoles.com)
```

State lives at `~/.streamfader/state.json`. All three tools share it.

---

## Requirements

```
flask
anthropic
python-dotenv
mido          # for MIDI bridge
python-rtmidi # for MIDI bridge
```

```bash
pip install -r requirements.txt
```

Set `ANTHROPIC_API_KEY` in `.env` to use PREVIEW RUN and LAUNCH.

---

## The proof

EXPLORE + LIST + Intensity LOW = Claude analyzes, shows options, touches nothing.

BUILD + DECIDE + Intensity HIGH = Claude makes one change, no commentary, done.

Same task. Different state. That's Gain.

---

*A [Creative Konsoles](https://creativekonsoles.com) product.*
