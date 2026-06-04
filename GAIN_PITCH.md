# GAIN
### The Behavioral Control Layer for AI

---

## The Problem

Every developer using Claude, GPT, or any large language model today faces the same invisible problem:

**You cannot control how an AI thinks. You can only control what you ask it.**

The same prompt, sent twice, can return a 400-word architectural analysis or a single sentence. A tool that gives you a research session when you need to ship. A tool that executes when you need to think. There is no dial. There is no control surface. There is no way to tune the machine.

Until now.

---

## The Product

**Gain is a behavioral mixing board for AI.**

Four tracks. Faders. Knobs. Buttons. A physical controller. A software interface. Every parameter maps directly to how Claude thinks — not what it thinks about, but *how*.

- **Track 1 — MODE:** Are you exploring or building? Thinking or shipping?
- **Track 2 — CONFIDENCE:** How certain should Claude be? Should it commit or present options?
- **Track 3 — SCOPE:** How wide should Claude look? One file or the whole project?
- **Track 4 — VOICE:** How much room should the response take up? Compressed or open?

Move a fader. Claude changes. Measurably. Repeatably. Every time.

---

## The Proof

*Same model. Same prompt. One button press difference.*

**Test parameters:**
- Model: Claude Sonnet 4.6
- Prompt: *"Whats a good suggestion for a suggestion engine?"*
- Intensity: 0.72 on both runs
- Tracks 2, 3, 4 muted — one variable isolated

---

### Run 1 — EXPLORE Mode

> *"Clarifying question first: Suggestion engine for what — user behavior in Gain, content recommendations in one of your apps, or something new?*
>
> *Assuming general-purpose for your stack, here are the real options:*
>
> *Lightweight (no infra, fits Flask)*
> *TF-IDF + cosine similarity — Pure Python, no server needed. Great for text-based suggestions. scikit-learn handles it in ~20 lines.*
>
> *BM25 — Better than TF-IDF for short text. Fast, zero dependencies beyond the lib.*
>
> *Claude-powered (fits your setup)*
> *Embedding similarity via Claude API — Send items → get embeddings → find nearest neighbors. Semantic understanding, not just keyword match. Costs tokens but fits your existing Anthropic usage.*
>
> *If you need real-time / scale: Redis with sorted sets..."*

**Behavior:** Multi-option analysis. Trade-offs presented. Asks clarifying questions. Thinks out loud. Does not commit.

---

### Run 2 — BUILD Mode

> *"Depends on the use case. What are you suggesting — content, products, actions, search completions?"*
>
> *[User responds: "build a suggestion engine for my project"]*
>
> *[Claude begins implementation immediately]*

**Behavior:** One clarifying question for minimum viable context. Then executes. No options. No trade-offs. No thinking out loud. Just work.

---

## The Contrast

| | EXPLORE | BUILD |
|---|---|---|
| Response style | Multi-option analysis | Direct execution |
| Questions asked | Multiple clarifying | One, then done |
| Output length | ~200 words | Action |
| Alternatives presented | 4 approaches | 0 |
| Ends with | Open question | Code |

Same model. Same prompt. Same intensity setting. **One button.**

---

## Why This Matters

Developers spend hours fighting AI behavior. Too verbose. Too cautious. Too exploratory when you need to ship. Too decisive when you need to think. The workaround today is rewriting your prompt. Again. And again.

Gain makes behavioral control physical, repeatable, and immediate.

- **Save presets.** EXPLORE, BUILD, DECIDE, LIST — save any combination and load it in one click.
- **Hardware control.** A Korg nanoKONTROL2 maps directly to every parameter. No mouse. No keyboard. Just faders.
- **Compare engine.** Run the same prompt through two presets side by side. Scored on 6 metrics. Plain-English diagnosis. See exactly what changed and why.
- **Claude Code integration.** Every prompt you type in terminal inherits the current Gain state. The fader changes are live.

---

## The Opportunity

Every developer using AI tools today is flying blind on behavior. They optimize prompts but have no instrument panel. No way to save a behavioral state. No way to compare outputs systematically. No way to dial in the cognitive mode they need for the task in front of them.

Gain is the instrument panel.

**Target users:** AI-forward developers, prompt engineers, researchers, power users of Claude Code and similar tools.

**Waitlist:** gain.creativekonsoles.com

---

## What's Next

- [ ] Stripe billing tiers (free / base / foundational)
- [ ] Model selector dial — switch models via hardware
- [ ] Multi-model compare — same preset, different models
- [ ] Team presets — share behavioral states across a team

---

*Built by Jeremiah Smith — Creative Konsoles*
*gain.creativekonsoles.com*
