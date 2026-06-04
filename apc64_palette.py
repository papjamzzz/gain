#!/usr/bin/env python3
"""
APC64 SysEx color test — tries multiple model IDs and encodings.
"""
import mido, sys, time

def row_col_to_note(row, col):
    return 24 + (7 - row) * 8 + col

def clear(out):
    for note in range(24, 88):
        out.send(mido.Message('note_on', channel=0, note=note, velocity=0))
    time.sleep(0.05)

def sysex_set_pad(out, model_id, pad_index, r, g, b):
    """
    Akai SysEx LED set. All values must be 0-127.
    r/g/b: pass 0-127 (scale your 0-255 values by half before calling).
    """
    r7 = min(r, 127)
    g7 = min(g, 127)
    b7 = min(b, 127)
    out.send(mido.Message('sysex', data=[
        0x47, 0x7F, model_id, 0x30,
        pad_index, r7, g7, b7
    ]))

TRACK_COLORS_7BIT = [
    (0,  106, 85),   # Teal   (#00D4AA scaled)
    (77,  39, 127),  # Purple (#9B4FFF scaled)
    (0,   90, 127),  # Cyan   (#00B4FF scaled)
    (127, 39,  77),  # Pink   (#FF4F9B scaled)
]
TRACK_DIM = 0.4

def paint_grid(out, model_id):
    for track_i, (r, g, b) in enumerate(TRACK_COLORS_7BIT):
        for row_offset in range(2):
            row = track_i * 2 + row_offset
            dim = TRACK_DIM if row_offset == 1 else 1.0
            for col in range(8):
                pad_idx = row * 8 + col
                sysex_set_pad(out, model_id,
                              pad_idx,
                              int(r * dim), int(g * dim), int(b * dim))
    time.sleep(0.1)

MODEL_IDS = [
    (0x40, "0x40 (64 — matches APC64)"),
    (0x4A, "0x4A (74)"),
    (0x49, "0x49 (73 — APC Mini)"),
    (0x7B, "0x7B (123 — original guess)"),
    (0x31, "0x31 (49 — APC Live Mk2)"),
]

def main():
    apc_out = next((n for n in mido.get_output_names() if 'APC64 Custom' in n), None)
    if not apc_out:
        sys.exit("APC64 not found")

    with mido.open_output(apc_out) as out:
        print("=== APC64 SysEx Model ID Scan ===\n")
        print("Each test lights the 8x8 grid with 4 color bands (teal/purple/cyan/pink).")
        print("Tell me the FIRST test number where you see actual colors (not all red).\n")

        for model_id, label in MODEL_IDS:
            clear(out)
            print(f"Testing model ID {label} ...")
            try:
                paint_grid(out, model_id)
            except Exception as e:
                print(f"  ERROR: {e}")
                continue
            answer = input("  Do you see 4 color bands? (y/n): ").strip().lower()
            print()
            if answer == 'y':
                print(f"SUCCESS: model ID is {label}")
                print(f"  Use model_id = {hex(model_id)} in the bridge.")
                break
        else:
            print("No model ID worked. The APC64 may need a different SysEx format.")
            print("Check the APC64 MIDI Reference PDF from Akai's website.")

        clear(out)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nDone.")
