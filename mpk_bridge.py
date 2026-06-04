#!/usr/bin/env python3
"""
Gain — MPK Mini 3 bridge (MPC mode)

Knobs K1–K8  (CC 70–77):  intensity, depth, certainty, risk,
                            scope, bandwidth, room, decay
Pads  1–4    (note 36–39): T1–T4 mute toggle
Pads  5–8    (note 40–43): cycle mode/stance/filter/voice
"""

import mido, json, sys, time
from pathlib import Path

STATE_FILE = Path.home() / ".streamfader" / "state.json"

KNOB_MAP = {
    70: "intensity",
    71: "depth",
    72: "certainty",
    73: "risk",
    74: "scope",
    75: "bandwidth",
    76: "room",
    77: "decay",
}

MUTE_PADS = {36: "t1_on", 37: "t2_on", 38: "t3_on", 39: "t4_on"}

MODE_PADS = {
    40: ("mode",    ["EXPLORE", "BUILD"]),
    41: ("stance",  ["LIST",    "DECIDE"]),
    42: ("filter",  ["FILE",    "PROJECT", "MODULE"]),
    43: ("voice",   ["DIRECT",  "OPEN",    "STUDIO"]),
}

def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {
            "intensity": 0.5, "depth": 0.5,
            "certainty": 0.5, "risk":  0.5,
            "scope":     0.5, "bandwidth": 0.5,
            "room":      0.5, "decay": 0.5,
            "mode": "EXPLORE", "stance": "LIST",
            "filter": "FILE",  "voice": "DIRECT",
            "t1_on": True, "t2_on": True, "t3_on": True, "t4_on": True,
        }

def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)

def main():
    port_name = next((n for n in mido.get_input_names() if "MPK" in n), None)
    if not port_name:
        sys.exit("[mpk] MPK Mini not found")

    print(f"[mpk] Connected: {port_name}")
    print("[mpk] K1–K8: intensity/depth/certainty/risk/scope/bandwidth/room/decay")
    print("[mpk] Pads 1–4: mute T1–T4  |  Pads 5–8: cycle mode/stance/filter/voice")
    print("[mpk] Ctrl+C to stop.\n")

    with mido.open_input(port_name) as inport:
        for msg in inport:
            state = load_state()
            if msg.type == "control_change" and msg.control in KNOB_MAP:
                field = KNOB_MAP[msg.control]
                state[field] = round(msg.value / 127, 3)
                save_state(state)
                print(f"  {field} → {state[field]:.2f}")

            elif msg.type == "note_on" and msg.velocity > 0:
                if msg.note in MUTE_PADS:
                    key = MUTE_PADS[msg.note]
                    state[key] = not state.get(key, True)
                    save_state(state)
                    track = msg.note - 35
                    print(f"  T{track} {'UNMUTED' if state[key] else 'MUTED'}")

                elif msg.note in MODE_PADS:
                    key, options = MODE_PADS[msg.note]
                    current = state.get(key, options[0])
                    next_val = options[(options.index(current) + 1) % len(options)] \
                               if current in options else options[0]
                    state[key] = next_val
                    save_state(state)
                    print(f"  {key} → {next_val}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[mpk] Stopped.")
