#!/usr/bin/env python3
"""Sniff MPK Mini — wiggle each knob/pad and see what it sends."""
import mido, sys

port = next((n for n in mido.get_input_names() if 'MPK' in n), None)
if not port:
    sys.exit("MPK Mini not found")

print(f"Listening on: {port}")
print("Wiggle knobs and tap pads. Ctrl+C to stop.\n")

with mido.open_input(port) as inport:
    for msg in inport:
        if msg.type in ('control_change', 'note_on', 'note_off'):
            print(msg)
