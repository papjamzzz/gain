#!/usr/bin/env python3
"""Test MPK Mini 3 pad LED feedback."""
import mido, sys, time

out = next((n for n in mido.get_output_names() if "MPK" in n), None)
if not out:
    sys.exit("MPK Mini not found")

print(f"Output port: {out}")
print("Lighting pads 1-8 one at a time...")

with mido.open_output(out) as port:
    # Light each pad briefly
    for note in range(36, 44):
        port.send(mido.Message('note_on', channel=9, note=note, velocity=127))
        print(f"  note {note} → lit")
        time.sleep(0.4)

    time.sleep(0.5)
    print("All on at once...")
    for note in range(36, 44):
        port.send(mido.Message('note_on', channel=9, note=note, velocity=127))
    time.sleep(1.5)

    print("Off...")
    for note in range(36, 44):
        port.send(mido.Message('note_on', channel=9, note=note, velocity=0))

print("Done. Did pads light up?")
