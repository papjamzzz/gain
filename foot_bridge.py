#!/usr/bin/env python3
"""
Mode Via MIDI — Foot controller bridge
Supports HID (keyboard/mouse events) and BLE MIDI
"""

import json
import sys
import time
import argparse
import threading
from pathlib import Path

STATE_FILE  = Path.home() / ".streamfader" / "state.json"
CONFIG_FILE = Path.home() / ".streamfader" / "foot_config.json"

# ── Profiles ──────────────────────────────────────────────────────────────────
# Each switch loads a complete behavioral state, not just a mode label.

PROFILES = {
    "EXPLORE": {"mode": "EXPLORE", "intensity": 0.2, "depth": 0.8},
    "FIX":     {"mode": "FIX",     "intensity": 0.8, "depth": 0.5},
    "BUILD":   {"mode": "BUILD",   "intensity": 0.7, "depth": 0.6},
    "PAUSE":   {"mode": "EXPLORE", "intensity": 0.0, "depth": 0.3},
}

# Default config — populated after running --scan and confirming key names
DEFAULT_CONFIG = {
    "type": "hid",
    "key_map": {
        # Edit these values to match what --scan reports for each pedal
        "Key.page_up":   "EXPLORE",
        "Key.page_down": "FIX",
        "Key.up":        "BUILD",
        "Key.down":      "PAUSE",
    },
}


# ── State file ────────────────────────────────────────────────────────────────

def write_state(profile_name: str) -> None:
    profile = PROFILES[profile_name]
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(profile, indent=2) + "\n")
    tmp.replace(STATE_FILE)


def read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2) + "\n")


# ── Display ───────────────────────────────────────────────────────────────────

def _bar(v: float, width: int = 12) -> str:
    filled = round(v * width)
    return "█" * filled + "░" * (width - filled)


def print_status(profile_name: str, trigger: str = "") -> None:
    p = PROFILES[profile_name]
    t = f"  ← {trigger}" if trigger else ""
    print(
        f"\r  [{profile_name:<7}]  "
        f"I {_bar(p['intensity'])} {p['intensity']:.1f}  "
        f"D {_bar(p['depth'])} {p['depth']:.1f}"
        f"{t:<30}",
        end="",
        flush=True,
    )
    print()


def print_header(mode: str) -> None:
    print("┌──────────────────────────────────────────────┐")
    print("│    MODE VIA MIDI  ·  Foot Controller Bridge  │")
    print(f"│    Mode: {mode:<37}│")
    print("└──────────────────────────────────────────────┘")
    print()


# ── HID bridge ────────────────────────────────────────────────────────────────

def run_hid_bridge(cfg: dict) -> None:
    try:
        from pynput import keyboard
    except ImportError:
        sys.exit("[foot] pynput not installed. Run: pip3 install pynput")

    key_map = cfg.get("key_map", {})
    if not key_map:
        sys.exit("[foot] No key_map in config. Run: mvm foot --scan")

    print_header("HID")
    print("  Listening for foot controller input...")
    print("  Ctrl+C to stop\n")

    # Print current state
    state = read_state()
    if state.get("mode"):
        print(f"  Current state: {state['mode']}  I:{state.get('intensity',0):.1f}  D:{state.get('depth',0):.1f}\n")

    def on_press(key):
        key_str = str(key).replace("'", "")
        if key_str in key_map:
            profile_name = key_map[key_str]
            write_state(profile_name)
            print_status(profile_name, trigger=key_str)

    with keyboard.Listener(on_press=on_press, suppress=False) as listener:
        try:
            listener.join()
        except KeyboardInterrupt:
            print("\n\n[foot] Stopped.")


# ── MIDI bridge ───────────────────────────────────────────────────────────────

def run_midi_bridge(cfg: dict) -> None:
    try:
        import mido
    except ImportError:
        sys.exit("[foot] mido not installed. Run: pip3 install mido python-rtmidi")

    ports = mido.get_input_names()
    target = next(
        (p for p in ports if any(
            kw in p.lower() for kw in ["foot", "mwave", "chocolate", "footctrl"]
        )),
        None,
    )

    if not target:
        print("[foot] No foot controller found in MIDI ports.")
        print("  Available ports:", ports or ["(none)"])
        print("  Try: Audio MIDI Setup → Window → Show MIDI Studio → Bluetooth")
        sys.exit(1)

    midi_map = cfg.get("midi_map", {})
    print_header("MIDI")
    print(f"  Port: {target}")
    print("  Listening... Ctrl+C to stop\n")

    with mido.open_input(target) as port:
        try:
            for msg in port:
                key = f"{msg.type}:{msg.note if hasattr(msg,'note') else msg.control}"
                if key in midi_map:
                    profile_name = midi_map[key]
                    write_state(profile_name)
                    print_status(profile_name, trigger=key)
        except KeyboardInterrupt:
            print("\n\n[foot] Stopped.")


# ── Scan mode ─────────────────────────────────────────────────────────────────

def run_scan() -> None:
    print("┌──────────────────────────────────────────────┐")
    print("│    MODE VIA MIDI  ·  Scan Mode               │")
    print("└──────────────────────────────────────────────┘")
    print()
    print("  Stomp each pedal. Note the key name printed.")
    print("  Then run: mvm foot --config to set the mapping.")
    print("  Ctrl+C to stop.\n")

    # Check MIDI ports first
    try:
        import mido
        ports = mido.get_input_names()
        if ports:
            print(f"  MIDI ports found: {ports}")
        else:
            print("  No MIDI ports found — device is likely HID (keyboard mode)")
    except Exception:
        print("  mido not available for MIDI port check")

    print()
    print("  Watching for keystrokes...\n")

    try:
        from pynput import keyboard

        seen = {}

        def on_press(key):
            key_str = str(key).replace("'", "")
            if key_str not in seen:
                seen[key_str] = True
                print(f'  PRESS  →  "{key_str}"')

        def on_release(key):
            key_str = str(key).replace("'", "")
            print(f'  RELEASE→  "{key_str}"')

        with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
            listener.join()

    except KeyboardInterrupt:
        print("\n\n  Scan complete.")
        if seen:
            print("\n  Keys detected:")
            for k in seen:
                print(f'    "{k}"')
            print()
            print("  Edit ~/.streamfader/foot_config.json key_map with these values.")
            print("  Then run: mvm foot --start")


# ── Config wizard ─────────────────────────────────────────────────────────────

def run_config_wizard() -> None:
    try:
        from pynput import keyboard
    except ImportError:
        sys.exit("[foot] pynput not installed. Run: pip3 install pynput")

    print("┌──────────────────────────────────────────────┐")
    print("│    MODE VIA MIDI  ·  Config Wizard           │")
    print("└──────────────────────────────────────────────┘")
    print()

    actions = ["EXPLORE", "FIX", "BUILD", "PAUSE"]
    key_map = {}

    for action in actions:
        print(f"  Stomp the pedal you want for  [{action}]  ...")

        captured = threading.Event()
        result = {}

        def on_press(key, action=action):
            key_str = str(key).replace("'", "")
            # Ignore modifier keys
            if key_str in ("Key.ctrl_l", "Key.ctrl_r", "Key.shift", "Key.cmd"):
                return
            result["key"] = key_str
            captured.set()
            return False  # stop listener

        with keyboard.Listener(on_press=on_press) as listener:
            captured.wait(timeout=15)
            listener.stop()

        if "key" not in result:
            print(f"  No input — skipping {action}")
            continue

        key_map[result["key"]] = action
        print(f'  Mapped  "{result["key"]}"  →  {action}\n')
        time.sleep(0.3)

    cfg = {"type": "hid", "key_map": key_map}
    save_config(cfg)
    print(f"  Saved to: {CONFIG_FILE}")
    print()
    print("  Final mapping:")
    for k, v in key_map.items():
        print(f'    "{k}"  →  {v}')
    print()
    print("  Run  mvm foot --start  to begin.")


# ── Entry ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mvm foot",
        description="Mode Via MIDI — foot controller bridge",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--start",  action="store_true", help="Start the bridge (default)")
    group.add_argument("--scan",   action="store_true", help="Print raw input from controller")
    group.add_argument("--config", action="store_true", help="Interactive mapping wizard")
    args = parser.parse_args()

    if args.scan:
        run_scan()
        return

    if args.config:
        run_config_wizard()
        return

    # Default: start bridge
    cfg = load_config()
    bridge_type = cfg.get("type", "hid")

    if bridge_type == "midi":
        run_midi_bridge(cfg)
    else:
        run_hid_bridge(cfg)


if __name__ == "__main__":
    main()
