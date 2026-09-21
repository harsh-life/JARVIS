# Hypermind : Consumer Product Track

  

**Status:** Execution-ready. Written for new WebDev/PR-Comms contributors joining Track B — self-contained, no external context required.

**Owner:** Harsh (Product), Pushpal (Architecture, Phase 1 carryover)

**Scope:** Phase 2b (build + internal pilot) and the Phase 3 hook point. Production launch to real public users is out of scope until the Decision Gate (Section 12) is met.

  

---

  

## 1. One-Paragraph Summary

  

Track B is the consumer-facing product: a JARVIS-like assistant running on the same 4GB-RAM budget Android phones Phase 1 already proved works, that sees the screen, takes actions through the existing Accessibility/Shizuku execution layer, and gets more useful to each individual user over time through a personalization architecture built on cheap per-user LoRA adapters rather than expensive full fine-tunes. Development happens now, in parallel with Track A — but nothing here ships to real, public end-users until Track A proves it can generate consistent revenue. Track B is being deliberately built *without* domain intelligence (finance, health, education) — that comes later from Track A's research; your job now is to build the product and leave a clean socket for that intelligence to plug into later.

  

## 2. Problem Statement

  

A working on-device execution layer (Phase 1) isn't a product — it's a capability. Track B has to turn "the phone can see and act" into "the phone remembers what I care about, adapts to how I'm feeling right now, and gets better at helping me specifically the more I use it" — without crossing into simulating a relationship, which is both an ethical line and, at the scale this product targets (potentially someone's only AI system access), a real harm if crossed. The problem is building genuine utility-driven personalization while staying strictly on the utility side of that line.

  

## 3. Goals

  

1. Build the Knowledge Vault (static, shared RAG knowledge base) and wire it to the FastAPI Gateway.

2. Build the dual-memory architecture (session-volatile + long-term Mem0), keeping the two strictly separated.

3. Build the Flask dashboard for internal visibility into vault content, memory state, and pipeline health.

4. Build the Phase 3 skill-hook interface — the socket domain verticals will plug into later.

5. Run a small (~10-person) internal technical pilot the moment Track A produces its first validated report — cheap, laptop-hosted, feedback-focused, explicitly not a public launch.

6. Have the investor narrative and pilot documentation ready to deploy the moment Track A's real revenue trend exists.

  

## 4. Explicit Non-Goals

  

- No public launch or real end-user onboarding before the Decision Gate (Section 12) is met.

- No finance, health, or education domain content or logic — that's Phase 3, gated on Track A's research succeeding and being replicated.

- No simulated agent emotions, mood, personality, relationship/trust scoring, or diary content anywhere in the vault, prompts, or UI — see Section 8, this is a hard boundary, not a style call.

- No investor pitching before a 3-month consistent revenue trend exists (Track A's gate, not Track B's, but it governs your PR/Comms timeline directly).

- No full per-user fine-tuning — LoRA adapters only, for cost reasons (Section 9).

  

## 5. Target User

  

People on $100–200 Android devices — the largest smartphone price segment in India, and a population structurally excluded from flagship on-device AI (the RAM bar for on-device assistants has moved *up* to 12GB, not down, widening this gap rather than closing it). During Phase 2b, your actual near-term "user" is a small internal pilot group of ~10 people, not yet the eventual public audience — design and test against that reality, not the eventual scale.

  

---

  

## 6. System Architecture

  

### 6.1 Full Architecture Diagram

  

```

**Gateway note:** Track B's endpoints run as modular FastAPI routers within the same Phase 1 Gateway instance Track A uses — a monolith, not a separate deployment. Splitting into microservices is overkill for Phase 2a/2b; revisit only in Phase 3 if actual load demands it.

  

DEVICE (existing from Phase 1 — Accessibility Service, Shizuku, on-device router)

│ JSON payload over HTTPS (Cloudflare Tunnel, same pattern as Phase 1)

▼

┌───────────────────────────────────────────────────────────────────┐

│ FASTAPI GATEWAY │

│ │

│ ┌─────────────────────────┐ ┌─────────────────────────────────┐│

│ │ VOLATILE ACTIVE-STATE │ │ KNOWLEDGE VAULT QUERY LAYER ││

│ │ JSON (in-memory only) │ │ GET /query?domain=X&question=... ││

│ │ - current conversation │ │ → searches ChromaDB ││

│ │ - detected user state │ │ → returns top-K markdown chunks ││

│ │ - immediate task context │ └─────────────┬─────────────────────┘│

│ └───────────┬───────────────┘ │ │

│ │ (session ends) │ │

│ ▼ ▼ │

│ ┌─────────────────────────┐ ┌─────────────────────────────────┐│

│ │ MEM0 (persistent) │ │ CHROMADB (vector index) ││

│ │ - task history │ │ - indexes the Git-based ││

│ │ - stated preferences │ │ Knowledge Vault (markdown) ││

│ │ - past requests │ │ - static, shared across users ││

│ │ (vector DB + SQLite) │ └─────────────────────────────────┘│

│ └───────────────────────────┘ │

│ │

│ ┌─────────────────────────────────────────────────────────────────┐│

│ │ PHASE 3 SKILL HOOK (dormant until Phase 3) ││

│ │ POST /skills/register — vertical models register here later ││

│ │ Currently: empty manifest, interface defined, nothing plugged in ││

│ └─────────────────────────────────────────────────────────────────┘│

└───────────────────────────────┬───────────────────────────────────┘

▼

┌─────────────────────────┐

│ FLASK DASHBOARD │

│ (internal-facing) │

│ - vault content inspector │

│ - memory state viewer │

│ - pipeline health checks │

└─────────────────────────┘

```

  

### 6.2 The Knowledge Vault (Static, Shared)

  

- **Storage:** Git repository, markdown files, organized by domain folder (folder structure to be defined once Phase 3 domains are locked — leave placeholder structure now).

- **Indexing:** ChromaDB, semantic search over the markdown content.

- **Access pattern:** `GET /query?domain={domain}&question={question}` → returns top-K relevant chunks as JSON (VaultQueryResponse, Section 7) — this is standard RAG, not a novel build.

- **What goes in it now:** general product knowledge, tone/style guides (bounded per Section 8), general task-assistance patterns. **Nothing domain-specific (finance/health/education) yet** — that's explicitly Phase 3.

  

### 6.3 Dual-Memory Architecture — Why the Separation Matters

  

| Layer | Contents | Tech | Lifespan | Why separate |

|---|---|---|---|---|

| Volatile Active-State JSON | Current conversation, detected user state (valence/arousal/stress), immediate task context | In-memory | Session only | Keeps the router fast — nothing here needs to survive a restart or bloat the long-term store |

| Long-Term Mem0 | Persistent task history, stated preferences, past requests | Mem0 (vector DB + SQLite) | Across sessions | This is what makes the assistant "remember you" over time, without carrying session noise forward |

| Knowledge Vault (6.2) | Static, shared domain knowledge | ChromaDB | Permanent, shared across all users | Never mixed with per-user memory — static knowledge shouldn't pollute the per-user vector space, and per-user data should never leak into shared knowledge |

  

Collapsing any two of these three layers to save build time is the single most likely way this architecture quietly degrades — don't do it, even under time pressure.

  

### 6.4 Phase 3 Skill Hook — Build the Socket, Not the Content

  

The single most important non-obvious deliverable in this document. Track B is intentionally shipping without finance/health/education intelligence. Your job is the interface, not the content:

  

- `POST /skills/register` — accepts a `SkillManifest` (Section 7) describing a new domain vertical's base model endpoint, allowed action types, and safety constraints.

- Until Phase 3, this endpoint exists and is tested against a mock manifest, but nothing real is registered.

- This mirrors exactly how Phase 1 built dormant `/store_memory` and `/schedule_task` endpoints for Track A before Track A existed — same pattern, one phase further down the line.

  

---

  

## 7. Data Contracts

  

**ActiveStateJSON** (volatile, session-only):

```json

{

"session_id": "uuid",

"conversation_turns": ["..."],

"detected_user_state": {"valence": 0.0, "arousal": 0.0, "stress": 0.0},

"immediate_task_context": {"intent": "string", "relevant_screen_data": {}}

}

```

  

**Mem0Fact** (long-term, persistent):

```json

{

"user_id": "uuid",

"fact_type": "preference | past_request | stated_goal",

"content": "string, task-relevant only — never relationship/emotional content",

"timestamp": "ISO8601",

"source_session_id": "uuid"

}

```

  

**VaultQueryRequest / VaultQueryResponse:**

```json

// Request

{"domain": "string", "question": "string", "top_k": 5}

  

// Response

{

"results": [

{"chunk": "markdown text", "source_file": "vault/path.md", "relevance_score": 0.0}

]

}

```

  

**SkillManifest** (Phase 3 hook, dormant until then):

```json

{

"vertical": "finance | health | education",

"base_model_endpoint": "string",

"allowed_action_types": ["read-only", "suggest", "human-confirm-required"],

"safety_constraints": {

"autonomous_actions_permitted": false,

"requires_human_confirmation_for": ["any transaction", "any diagnosis-adjacent statement"]

},

"registered_at": "ISO8601"

}

```

  

---

  

## 8. What Track B Uses vs. Explicitly Does NOT Use

  

### 8.1 Uses (Locked)

  

| Module | Purpose |

|---|---|

| Mem0 (task-facts only) | Preferences, past requests, stated goals — no relationship data, ever |

| User-State Detection | Reads the *user's* voice/text cues (stress, urgency, confusion) to adapt response style — shorter when stressed, more detail when confused |

| Task-Linked Proactivity | Follows up on reminders, deadlines, stated goals — never unprompted |

| Proactive Scheduler | APScheduler, same 2 AM-style automation pattern as Track A, applied to task reminders |

| LoRA Adapters (Phase 3, future) | Cheap (~$0.05–0.10/run, ~5MB) per-user personalization on shared vertical base models |

  

### 8.2 Explicitly Does NOT Use — Read This Section Fully Before Writing Any Vault Content or Prompt

  

| Excluded | Why |

|---|---|

| Agent mood state (8-state "mood machine") | Simulates the *agent's own* feelings — not a utility function |

| Agent personality (Big Five-style traits) | Defines a character — not needed for task completion |

| Relationship/trust scoring | Gates warmth/familiarity based on interaction history — creates dependency, the opposite of what a free, wide-reach utility should do |

| Diary / "I was thinking about you" | Unprompted emotional contact implying an inner life |

| Any first-person statement of feeling ("I missed you," "I feel sad," "I care about you") | Banned from every vault file and system prompt, no exceptions, no clever reframing |

  

**The test to apply when unsure:** does this describe the assistant reading *the user's* state, or does it describe the assistant *having* a state of its own? Only the first is in scope, ever.

  

**Why this is weighted so heavily in this document:** Track B may be the only AI system some of its eventual users have access to, running on the cheapest phones in the largest price segment in the market. Simulated emotional attachment at that scale is a real, material harm — not a hypothetical one. This is why it's Locked rather than a preference, and why it's restated fully here rather than just referenced.

  

---

  

## 9. Personalization Strategy — LoRA, Not Full Fine-Tuning

  

**The technical reason "one fine-tuned model per user" doesn't work at scale:** even a modest per-user fine-tuning run costs real, recurring compute — multiplied across thousands of users, with no natural ceiling, and it has to repeat every time meaningful new data accumulates.

  

**The fix:** one shared, fully fine-tuned base model per Phase 3 vertical (finance, health, education) — trained once, holds the real domain expertise. On top of that, each user gets a small, cheap **LoRA (Low-Rank Adaptation) adapter** — a lightweight additional weight set, a few MB, trained/updated incrementally on that user's own interaction history. Standard, mature open-source practice (Hugging Face's PEFT library). This is what makes "personalized to me specifically" actually affordable at real scale — full fine-tunes run ~$10–20 and ~4GB storage per user; LoRA adapters run ~$0.05–0.10 and ~5MB. This is a Phase 3 concern architecturally, but Track B's memory schema (Section 7) needs to be built now in a way that doesn't require rework when LoRA training starts pulling from it.

  

---

  

## 10. Team & Ramp Path

  

Lower-risk than Track A — no live execution against external systems is involved in Track B's work. The CLA/SHA still has to be signed before repo access, same as every contributor on the project, but there's no equivalent shadow-period requirement here; you can start building immediately once onboarded.

  

**PR/Comms specifically:**

- Draft the investor narrative now — but do not pitch until Track A shows a 3-month consistent revenue trend (or 3+ confirmed bounties across different targets). One bounty is a data point, not a trend; pitching off it too early reads as inexperience and burns a first impression that doesn't come back.

- Build a community waitlist for Track B now — no downside.

- Document the internal pilot (Section 12) as it happens — this becomes real material for the eventual pitch.

  

---

  

## 11. Budget Breakdown

  

| Item | Cost | Notes |

|---|---|---|

| Development (Months 1–6, pre-pilot) | ₹0 direct infra cost | Founder/contributor time only — architecture, vault content, dashboard build |

| Internal Technical Pilot (10 users, laptop-hosted) | ~₹1,400/month (~$16.50) | 10 users × ~$1.65/user/month unit cost, reusing Phase 1's Cloudflare Tunnel pattern — trivially cheap |

| Production pilot infra (post-Decision-Gate) | ₹8,000–16,000/month (~$100–200) at 50–100 users | Real VPS hosting, moved off the laptop |

| Public launch infra (later, post-Decision-Gate) | Scales linearly at ~$1.65/active user/month | Funded by investment or Track A revenue by this point |

  

---

  

## 12. Deployment Gates — Two Separate Gates, Do Not Blur Them

  

- **Internal Technical Pilot Gate:** fires the moment Track A produces its first validated report (paid or not). Triggers the ~10-person, laptop-hosted internal pilot (Section 11). This is dev-testing and feedback collection — not a launch, and success here says nothing about production readiness.

- **Production/Decision Gate:** Track A generates ₹1L/month for 3 consecutive months. Only this gate authorizes moving Track B toward real hosting and real public users. A good Technical Pilot result is not evidence this gate should move — the two measure completely different things (pilot mechanics working vs. a real, repeatable business).

  

---

  

## 13. Risk Mitigation (Ordered by Severity)

  

| Priority | Risk | Impact | Mitigation | Owner |

|---|---|---|---|---|

| 1 | Simulated-emotion content leaks into vault or prompts | CRITICAL — real user harm at scale, violates a locked boundary | Section 8.2 applied literally to every vault commit; review before merge | Whoever reviews vault PRs |

| 2 | Internal Technical Pilot success gets treated as production readiness | HIGH — undermines the whole revenue-validation discipline this project runs on | Section 12's two-gate framing stated explicitly wherever pilot results are discussed | Harsh |

| 3 | Dual-memory layers collapsed to save build time | MEDIUM — degrades personalization quality and router speed over time | **Concrete code review rule:** the Knowledge Vault's ChromaDB instance and the Mem0 ChromaDB instance must use distinct collection names (`hypermind_vault` vs. `hypermind_memories`) and must be initialized as separate client objects — never the same variable or the same collection name in any file. Any PR that points both at the same collection, or imports one client instance into a file that should use the other, is a merge-blocking issue, not a style note. | Backend contributor |

| 4 | Investor pitch attempted before 3-month revenue trend | HIGH — burns investor relationships | PR/Comms explicitly briefed on Section 10's timing rule | Harsh + PR/Comms |

| 5 | Phase 3 skill hook built as an afterthought, requiring rework later | MEDIUM — repeats the exact mistake Phase 1's Track A hooks were designed to avoid | Section 6.4 treated as a first-class deliverable, not a placeholder comment | Backend contributor |

  

---

  

## 14. Implementation Timeline (First 8 Weeks)

  

| Week | Focus |

|---|---|

| 1 | Repo setup, CLA/SHA signed, Knowledge Vault folder structure (placeholder domains), Git repo initialized |

| 2–3 | FastAPI Gateway skeleton, ChromaDB integration, `/query` endpoint working against initial vault content |

| 3–4 | Dual-memory architecture: Active-State JSON (in-memory) and Mem0 wiring, kept strictly separate |

| 4–5 | Flask dashboard skeleton, connected to the Gateway for internal visibility |

| 5–6 | Phase 3 skill-hook endpoint (`/skills/register`) built and tested against a mock manifest |

| 6–7 | Internal Technical Pilot readiness — laptop hosting, Cloudflare Tunnel, 10-person recruitment prepped, waiting on Track A's first validated report to trigger |

| 7–8 | If Track A's Gate has fired: run the internal pilot, collect feedback. If not: continue refining vault content and dashboard, document everything for the eventual pilot |

  

---

  

## 15. Appendix A — Mem0 Configuration (Required, Not Optional)

  

**This must be applied before Mem0 is called anywhere in Track B.** Mem0 defaults to OpenAI for both LLM and embedding calls if not explicitly configured — silently doing so will blow through budget without warning, and it's the same shared config Track A uses (one Mem0 instance, one config, per the Gateway monolith note above). Shared file: `mem0_config.py`, imported wherever the long-term Mem0 layer (Section 6.3) is written to or read from — specifically, the session-close hook that persists facts from Active-State JSON into Mem0.

  

```python

import os

from mem0 import AsyncMemory

from mem0.configs.base import MemoryConfig

  

config = MemoryConfig(

llm={

"provider": "litellm",

"config": {

"model": "deepseek/deepseek-chat",

"api_key": os.getenv("DEEPSEEK_API_KEY")

}

},

embedder={

"provider": "huggingface",

"config": {

"model": "BAAI/bge-small-en-v1.5"

}

},

vector_store={

"provider": "chroma",

"config": {

"collection_name": "hypermind_memories",

"path": "./mem0_storage"

}

}

)

  

memory = AsyncMemory(config=config)

```

  

Verified against current Mem0 documentation before inclusion here — `AsyncMemory`, `MemoryConfig`, and all three providers (`litellm`, `huggingface` embedder, `chroma` vector store) are confirmed real and correctly structured.

  

**Note on the Knowledge Vault vs. Mem0 distinction (Section 6.3):** this config is for the dynamic, per-user Mem0 layer only. The static Knowledge Vault (Section 6.2) uses its own separate ChromaDB collection — do not point the vault's indexing at `hypermind_memories`; keep the collection names distinct so per-user data never mixes into shared vault content.

  

## 16. Glossary

  

- **RAG (Retrieval-Augmented Generation):** looking up relevant text chunks and injecting them into a prompt instead of relying on the model's memorized knowledge.

- **Mem0:** the dynamic, per-user, long-term memory layer — distinct from the static Knowledge Vault and the volatile session state.

- **LoRA (Low-Rank Adaptation):** cheap, small-weight personalization layered on top of a shared base model — used instead of full per-user fine-tuning for cost reasons.

- **Technical Pilot Gate vs. Production/Decision Gate:** the two separate triggers in Section 12 — do not conflate them.

- **Skill Hook:** the dormant interface (Section 6.4) that Phase 3 domain verticals will register into later.

- **`offensive-claude`:** a swappable system-prompt manifest used on the Track A side (included here for reference since both tracks share the Gateway). It configures the cloud LLM for offensive security reasoning without touching pipeline code. Track B does not use it — mentioned here only so contributors don't search for it and find something unrelated.

  

---

  

## 17. What Success Looks Like & How to Use the Tools

  

### 17.1 The Finished Product

  

Track B is successful when a person with a $120 Android phone sideloads the APK, uses it across a week, and at the end of that week it is measurably more useful to them than it was on day one — because it remembered something they told it, adapted its communication style to how they were feeling, and completed a real task (checked a bill, set a reminder, read a screen) without them having to re-explain their context each time. The personalization is in the usefulness, not in simulated warmth. A reviewer reading the interaction logs should see a smarter assistant, not a friendlier one.

  

The internal pilot's job is to surface whether this actually happens, or whether the architecture only looks like it should work in theory. One of three things comes out of the pilot: it works and feedback says why, it doesn't work and feedback says where it breaks, or it works mechanically but the experience feels cold/confusing/friction-heavy — which is the most useful outcome because it's the hardest to predict from architecture alone.

  

### 17.2 The Lego Philosophy — How to Think About the Tools

  

You are assembling components, not building a platform. Every layer in this stack handles one job so you don't have to. Here is how the pieces fit:

  

**Knowledge Vault (Git + FastAPI + ChromaDB — `hypermind_vault` collection):** the static, shared knowledge base. You write and organize markdown files in a Git repo. FastAPI exposes a `/query` endpoint. ChromaDB indexes the markdown and returns semantically relevant chunks when queried. You do not write a search engine — ChromaDB already is one. Your job is writing good markdown and wiring the FastAPI route correctly.

  

**Session memory (in-process Python dict or FastAPI app-state):** the volatile, short-lived layer. It exists only while a conversation is happening. When the session closes, the relevant facts are extracted and handed to Mem0. You don't persist this layer — it lives in RAM only. The moment you find yourself writing this to disk or a DB, stop: that's Mem0's job, not this layer's.

  

**Long-term memory (Mem0 + ChromaDB — `hypermind_memories` collection, Appendix A config):** the persistent, per-user layer. After every session, a structured fact-extraction step reads the session's `ActiveStateJSON` and decides what's worth keeping (stated preferences, recurring tasks, stated goals). Those facts go into Mem0 via `await memory.add(...)`. On the next session, `await memory.search(...)` retrieves what's relevant to the current context. Your job is writing the fact-extraction step and calling the Mem0 API correctly — not building a database.

  

**FastAPI Gateway (monolith, modular routers):** the single entry point for everything — device requests, vault queries, memory reads/writes, and the Phase 3 skill hook. Each concern is a separate FastAPI router (`APIRouter`), mounted on the same app instance. Don't build separate services; just add a router. This keeps deployment simple (one process, one port, one Cloudflare Tunnel) while keeping the code organized.

  

**Flask Dashboard (internal-facing):** a minimal web UI for you and the founders to inspect the vault's content, query Mem0 for a specific user's stored facts, and check pipeline health. This is a dev tool — don't over-invest in it. A few `GET` routes and a basic HTML template is enough for the internal pilot.

  

**Cloudflare Tunnel (`cloudflared`):** makes the laptop-hosted Gateway reachable from the outside world without port-forwarding or a public IP. One command to start, one config file. The 10-person pilot runs through this; it has no cost and no deployment complexity. When the Production/Decision Gate fires and you move to a real VPS, the Tunnel goes away and you use a normal public IP instead — the architecture doesn't change, just the hosting.

  

**Proactive Scheduler (APScheduler):** task-linked reminders only. If a user said "remind me to pay rent on the 1st," APScheduler fires the reminder. If no user-given reason exists, the scheduler does nothing. One function call to register a job, one to cancel it. Don't build a cron wrapper.

  

**Phase 3 Skill Hook (`/skills/register`):** a dormant endpoint that exists now so Phase 3 doesn't require a Gateway rebuild. A domain vertical (finance, health, education) will register here later, providing its base model endpoint and its safety constraints. For now, build the endpoint, test it against a mock manifest, and leave it empty. The point is that the socket exists — not that anything is plugged into it yet.

  

**The rule for adding something new:** if a tool or library you want to use isn't already in one of the layers above, ask whether an existing layer already solves the problem before adding a new dependency. The stack was kept small deliberately — each addition increases the surface area that has to be maintained, explained to new contributors, and verified working before the pilot.