#!/bin/bash
# Gain Companion — floating window for Ableton
open -a "Google Chrome" --args \
  --app=http://127.0.0.1:5570/companion \
  --window-size=380,620 \
  --window-position=1500,100 \
  --disable-extensions \
  --no-first-run
