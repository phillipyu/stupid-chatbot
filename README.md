# Hands-On AI Agent Learning Path

---

## Mission 0 — Ping the Model
- **Goal:** Call the OpenAI Chat API once.
- **Outcome:** `python hello.py` prints the model’s reply.

## Mission 1 — Interactive CLI
- **Goal:** Wrap the API call in a loop; stream tokens as they arrive.
- **Outcome:** `python chat.py` becomes a REPL-style assistant.

## Mission 2 — Prompt Experiments
- **Goal:** Store 5 system prompts in `profiles.yml`; choose one at runtime.
- **Outcome:** `python chat.py --profile tutor` feels different from `--profile summarizer`.

## Mission 3 — Tool Calling (Function Hints)
- **Goal:** Expose a JSON-schema function `def_calc(expression)` and let the model decide when to call it.
- **Outcome:** Asking “What’s (1024**2)/17 rounded?” triggers a calculator call.

## Mission 4 — Scratch-Pad Memory
- **Goal:** Append every user/assistant turn to `history.json`; preload on start-up.
- **Outcome:** Bot remembers earlier facts during the same run.

## Mission 5 — Simple Retrieval (Mini-RAG)
- **Goal:** Chunk & embed a small PDF (e.g. HTMX docs) into a local Chroma/SQLite store; retrieve top-k before answering.
- **Outcome:** Asking “What is HTMX _hx-boost_?” returns the exact paragraph with citation.

## Mission 6 — Mini-Agent Loop (ReAct)
- **Goal:** Implement a **Thought → Action → Observation** cycle that can execute Python code (max 3 iterations).
- **Outcome:** “List primes under 100” makes the agent run Python instead of token-guessing.

## Mission 7 — First Deployment
- **Goal:** Dockerise `chat.py`, expose a WebSocket endpoint, deploy to Render or Fly.io.
- **Outcome:** You receive a public URL to share with friends.

## Mission 8 — Logging & Cost Guard
- **Goal:** Decorator logs prompt/response and prints token cost; abort if cost > $0.05.
- **Outcome:** A running tally appears after each request.

## Mission 9 — Polish & Front-End
- **Goal:** Add a simple HTML chat UI (HTMX or React). Auto-scroll and copy-to-clipboard support.
- **Outcome:** A slick demo accessible from your phone.

---

## Suggested Two-Week Schedule

| Day(s) | Mission(s) |
|--------|------------|
| 1      | 0–1 |
| 2      | 2 |
| 3–4    | 3–4 |
| Weekend| 5–6 |
| 8–10   | 7–9 |

---

## After Completing All Missions

- You’ll have working knowledge of the OpenAI API, prompt design, function calling, basic RAG, a ReAct loop, cost tracking, and a deployed demo.
- This foundation is everything you need before diving into multi-agent frameworks or startup prototypes.

Happy hacking! 🚀
