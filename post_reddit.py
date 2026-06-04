#!/usr/bin/env python3
"""Open pre-filled Reddit submit pages in your browser. Just click Submit."""

import webbrowser
import urllib.parse
import time

POSTS = [
    {
        "subreddit": "SideProject",
        "title": "I built a mixing board that controls how Claude thinks — 4 faders for Mode, Confidence, Scope, and Voice. MIDI-mapped to a nanoKONTROL2.",
        "body": """Been building this for a few months. It's called Gain.

The idea: a mixing board metaphor for controlling AI agent behavior. Same task, different board state — measurably different output.

Four tracks:
- Mode — Explore vs Build (how it approaches the task)
- Confidence — Hedge vs Commit (how certain it sounds)
- Scope — File vs Project (how wide it looks)
- Voice — Direct vs Open (how it communicates)

Each track has a fader, a knob, and mode buttons. Everything writes to a system prompt in real time.

It's MIDI-mapped to a Korg nanoKONTROL2 so you can physically ride the faders while the agent runs.

Also includes a CLI tool (ctrl run "your task") and a Flask UI that runs on iPad over LAN.

Landing page + waitlist: https://gain.creativekonsoles.com

Would love feedback from anyone deep in AI coding workflows."""
    },
    {
        "subreddit": "ClaudeAI",
        "title": "Built a physical mixing board to control Claude's behavior in real time — faders for Mode, Confidence, Scope, Voice",
        "body": """Called it Gain. It's a behavioral mixer for Claude Code sessions.

The core idea: instead of rewriting system prompts manually, you ride faders. Four tracks control four dimensions of how Claude thinks:

- Mode fader: Explore ↔ Build
- Confidence fader: Hedge ↔ Commit
- Scope fader: Single File ↔ Whole Project
- Voice fader: Open ↔ Direct

Each position writes a different system prompt live. You can feel the difference in output within a few messages.

It's MIDI-mapped to a Korg nanoKONTROL2 — physical faders, physical knobs. Also has a browser UI that works on iPad over LAN.

CLI: ctrl run "your task" — reads the board state, builds the prompt, calls claude.

Waitlist is open: https://gain.creativekonsoles.com"""
    },
    {
        "subreddit": "artificial",
        "title": "Gain — a mixing board interface for controlling AI agent behavior in real time (Mode, Confidence, Scope, Voice faders)",
        "body": """Sharing a side project: Gain is a behavioral mixer for AI coding agents.

Instead of manually tuning system prompts, you set four faders:
- Mode: Explore vs Build
- Confidence: Hedge vs Commit
- Scope: File vs Project
- Voice: Direct vs Open

Everything writes to a live system prompt. I've been running it with a Korg nanoKONTROL2 — physical faders mapped to each track.

Same task at different board states produces genuinely different outputs. It's made debugging AI behavior much more intuitive.

Also has a CLI and Flask UI (iPad-compatible over LAN).

https://gain.creativekonsoles.com — waitlist open if you want early access."""
    },
]

def main():
    print("Opening pre-filled Reddit posts in your browser.")
    print("For each one: check it looks right, then click Post.\n")

    for i, post in enumerate(POSTS, 1):
        url = (
            f"https://www.reddit.com/r/{post['subreddit']}/submit"
            f"?selftext=true"
            f"&title={urllib.parse.quote(post['title'])}"
            f"&text={urllib.parse.quote(post['body'])}"
        )
        print(f"[{i}/3] Opening r/{post['subreddit']}...")
        webbrowser.open(url)
        if i < len(POSTS):
            input("      Submit that post, then press Enter for the next one...")

    print("\nAll 3 opened. Done.")

if __name__ == "__main__":
    main()
