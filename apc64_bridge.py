#!/usr/bin/env python3
"""
Gain — APC64 Bridge
Maps APC64 faders → Gain state, lights pad grid in track colors.
Run: python3 apc64_bridge.py
"""

import mido
import json
import time
import sys
from pathlib import Path

STATE_FILE = Path.home() / ".streamfader" / "state.json"
PORT_NAME  = "APC64 Custom (APC64)"

# ── Fader → state key mapping (CC → state field) ─────────────────────────────
FADER_MAP = {
    16: "intensity",   # T1 Effort
    17: "depth",       # T1 Thinking Time
    18: "certainty",   # T2 Confidence
    19: "risk",        # T2 Boldness
    20: "scope",       # T3 Zoom Level
    21: "bandwidth",   # T3 Context Size
    22: "room",        # T4 Verbosity
    23: "decay",       # T4 Memory Persistence
}

# ── Pad grid layout ────────────────────────────────────────────────────────────
# APC64 8x8 grid, notes 24–87
# Row 0 (top) = note 80–87, Row 7 (bottom) = note 24–31
# We assign 2 rows per track

def note_to_row_col(note):
    """Return (row, col) for a pad note. Row 0 = top."""
    idx = note - 24
    row = 7 - (idx // 8)
    col = idx % 8
    return row, col

def row_col_to_note(row, col):
    """Return note number for a pad position."""
    idx = (7 - row) * 8 + col
    return 24 + idx

# Track colors (RGB for APC64 SysEx, or use velocity for color in custom mode)
# APC64 Custom mode: note_on velocity sets color index
# Standard Akai color table approximations:
TRACK_COLORS = {
    1: 37,   # Teal/Cyan
    2: 81,   # Purple
    3: 33,   # Cyan/Aqua
    4: 79,   # Blue
}

TRACK_MUTED_COLOR = 0   # Off
TRACK_DIM_COLORS = {
    1: 17,
    2: 49,
    3: 15,
    4: 47,
}

# Which rows belong to which track (row 0 = top)
TRACK_ROWS = {
    1: [0, 1],
    2: [2, 3],
    3: [4, 5],
    4: [6, 7],
}

# ── State ─────────────────────────────────────────────────────────────────────

def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {
            "intensity": 0.5, "depth": 0.5,
            "certainty": 0.5, "risk": 0.5,
            "scope": 0.5, "bandwidth": 0.5,
            "room": 0.5, "decay": 0.5,
            "t1_on": True, "t2_on": True, "t3_on": True, "t4_on": True,
        }

def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)

# ── LED control ───────────────────────────────────────────────────────────────

def light_pad(out_port, note, color_vel):
    """Light a pad with a given color velocity."""
    out_port.send(mido.Message('note_on', channel=0, note=note, velocity=color_vel))

def light_all_pads(out_port, state):
    """Light the full 8x8 grid based on current state."""
    for track, rows in TRACK_ROWS.items():
        muted = not state.get(f"t{track}_on", True)
        bright = TRACK_COLORS[track]
        dim    = TRACK_DIM_COLORS[track]
        for row in rows:
            for col in range(8):
                note = row_col_to_note(row, col)
                if muted:
                    color = TRACK_MUTED_COLOR
                else:
                    # First row of track = bright, second row = dim
                    color = bright if row == rows[0] else dim
                light_pad(out_port, note, color)
    time.sleep(0.01)

def pulse_all(out_port, state):
    """Flash all pads bright then settle — run animation."""
    # Flash white
    for note in range(24, 88):
        out_port.send(mido.Message('note_on', channel=0, note=note, velocity=3))
    time.sleep(0.12)
    for note in range(24, 88):
        out_port.send(mido.Message('note_on', channel=0, note=note, velocity=119))
    time.sleep(0.12)
    # Settle back to track colors
    light_all_pads(out_port, state)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Find ports
    in_names  = mido.get_input_names()
    out_names = mido.get_output_names()

    apc_in  = next((n for n in in_names  if 'APC64 Custom' in n), None)
    apc_out = next((n for n in out_names if 'APC64 Custom' in n), None)

    if not apc_in or not apc_out:
        sys.exit(f"[apc64] APC64 not found.\nAvailable: {in_names}")

    print(f"[apc64] Connected: {apc_in}")

    state = load_state()

    with mido.open_input(apc_in) as inport, mido.open_output(apc_out) as outport:
        # Light up the grid on connect
        light_all_pads(outport, state)
        print("[apc64] Grid lit. Faders and pads active.")
        print("[apc64] Ctrl+C to stop.\n")

        last_state_check = time.time()

        while True:
            # Poll incoming MIDI
            for msg in inport.iter_pending():
                if msg.type == 'control_change' and msg.control in FADER_MAP:
                    field = FADER_MAP[msg.control]
                    value = round(msg.value / 127, 3)
                    state[field] = value
                    save_state(state)
                    track = (msg.control - 16) // 2 + 1
                    print(f"  T{track} {field} → {value:.2f}")

                elif msg.type == 'note_on' and msg.velocity > 0:
                    row, col = note_to_row_col(msg.note)
                    # Determine which track this pad belongs to
                    for track, rows in TRACK_ROWS.items():
                        if row in rows:
                            # Toggle mute for this track
                            key = f"t{track}_on"
                            state[key] = not state.get(key, True)
                            save_state(state)
                            muted = not state[key]
                            print(f"  T{track} {'MUTED' if muted else 'UNMUTED'}")
                            light_all_pads(outport, state)
                            break

            # Re-sync grid every 2 seconds in case state changed from UI/nanoKONTROL
            if time.time() - last_state_check > 2.0:
                new_state = load_state()
                if new_state != state:
                    state = new_state
                    light_all_pads(outport, state)
                last_state_check = time.time()

            time.sleep(0.005)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[apc64] Stopped.")
