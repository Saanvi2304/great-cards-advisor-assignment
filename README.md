# Great.cards Credit Card Advisor

A web chatbot that helps Indian users choose credit cards based on their spending, rewards, fees and eligibility. It is built with **Python, FastAPI and HTML templates**. **Gemini** works out what the user wants and picks a tool, and the **live Great.cards MCP server** supplies the card data.

---

## 1. Setup

### Prerequisites
- Windows with Python 3.13 (the project's `venv` was created with 3.13.5)
- A Gemini API key
- The Great.cards MCP server URL

### Steps

1. **Open a terminal in the project folder**
   ```powershell
   cd D:\great-cards
   ```

2. **Activate the existing virtual environment**
   ```powershell
   .\venv\Scripts\Activate.ps1
   ```
   If you are in Git Bash, run `source venv/Scripts/activate` instead.

3. **Install the dependencies.** They are already installed in `venv`, so this step is only needed on a fresh environment.
   ```powershell
   pip install -r requirements.txt
   ```

4. **Configure the environment.** Copy `.env.example` to `.env` (if you haven't already) and fill in the real values:
   ```env
   MCP_URL="<your Great.cards MCP server URL>"
   LLM_PROVIDER=gemini
   LLM_MODEL=gemini-2.5-flash-lite
   GEMINI_API_KEY="<your Gemini API key>"
   LLM_TIMEOUT_SECONDS=30
   ```
   - `.env` is listed in `.gitignore`, so never commit it.
   - Values still set to the `PASTE YOUR ...` placeholders count as missing. In that case the page shows a "not configured" banner instead of crashing.

5. **Start the server**
   ```powershell
   uvicorn app.main:app --reload
   ```

6. **Open the chatbot** at http://127.0.0.1:8000

7. **Check the logs** in `logs/`. The folder is created automatically on first startup: `logs/app.log` holds the application log, and `logs/sessions/` holds one transcript file per chat session.

   > **Note on `logs_failure_example/`:** The `logs_failure_example/` directory contains sample logs from failure testing to illustrate how logs will look after running the app. It has nothing to do with the running of the app and is just an example.

### Useful endpoints
| Endpoint | Method | Purpose |
|---|---|---|
| `/` | GET | Chat UI |
| `/api/chat` | POST | `{"message": "..."}` → `{"reply", "kind", "tools_used", "notices"}` |
| `/api/reset` | POST | Clears the current conversation (closes its session log) |
| `/health` | GET | `{"status": "ok", "configured": true/false}` (never shows secrets) |

> Conversations are stored in memory, so run a **single worker process**.

---

## 2. Chatbot pipeline

Every user message passes through these stages in order. Each stage can answer early, so the later and more expensive stages (Gemini, MCP) only run when they are needed.

```
User message
  │
  ├─1─ Input cleaning & validation        (length 1–1000, Unicode normalised, control chars removed)
  ├─2─ Sensitive-input redaction          (local, before anything leaves the server or is logged)
  ├─3─ Local intent handling              (greetings / help / thanks / bye → instant reply, no LLM/MCP)
  ├─4─ Input guardrails                   (injection, fraud, tax-only, obvious off-topic → local reply)
  ├─5─ Gemini interprets & selects a tool (live MCP schemas + ask_clarification)
  ├─6─ Argument validation                (live JSON Schema + "no guessed values" grounding rules)
  ├─7─ Live Great.cards MCP call          (one lazily opened session per turn)
  │      └── results go back to Gemini; up to 4 tool calls per turn
  ├─8─ Grounded answer from Gemini
  ├─9─ Output guardrails                  (₹ figure verification, approval & tax filters, disclaimers)
  └─10─ Session transcript                (turn appended to logs/sessions/<session>.log)
  │
Reply shown in the web UI (with "Source" tags and privacy notices)
```

### Stage details

1. **Input cleaning and validation:** `schemas.py` rejects empty messages and messages over 1000 characters. `pipeline.py` normalises Unicode and removes control characters.

2. **Sensitive-input redaction** (`safety/redaction.py`)
   - **Detected and redacted:** card numbers (checked with the Luhn checksum), CVV, OTP, PIN, passwords, bank account numbers, email, UPI ID, phone, Aadhaar, PAN, passport and date of birth.
   - **Placeholders:** each value is replaced with a token such as `[REDACTED CARD NUMBER]`.
   - **Where redacted text is used:** only the redacted text goes to Gemini, is stored in history or is written to logs.
   - **Not redacted on purpose:** pincodes and income, because eligibility checks need them.
   - **Mostly-secret messages:** if a message is mostly secrets, the bot replies locally with a safety warning.
   - **Notice to the user:** the user is shown which kinds of data were removed.

3. **Local intent handling** (`chat/intents.py`): messages that are entirely "Hi", "Hello", "help", "thanks", "bye" or similar get canned replies. No Gemini or MCP call is made.

4. **Input guardrails** (`safety/guardrails.py`), enforced in code:
   - **Prompt injection** ("ignore previous instructions", "reveal your prompt"): refused.
   - **Fraud** (stolen cards, fake salary slips, bypassing KYC): refused.
   - **Tax-only questions:** redirected to a tax professional.
   - **Obvious off-topic requests** (jokes, Python code) with no credit-card words: a short redirect.
   - **Flags for Gemini:** questions about approval odds, and tax mentions mixed into card questions, are passed on as flags so Gemini adds the right constraints.

5. **Gemini selects a tool** (`llm/gemini.py`, `llm/prompts.py`)
   - **Declarations:** Gemini receives function declarations built from the live MCP tool schemas, filtered by `tools/registry.py`.
   - **Clarification tool:** a local `ask_clarification` tool lets Gemini ask a question instead of guessing.
   - **Advisor playbook:** the `get_advisor_context` playbook is fetched once from the server and included in the system prompt as reference data.
   - **Off-topic messages** that reach Gemini get a one-line redirect, with no tool call.

6. **Argument validation** (`tools/registry.py`)
   - **Parameter checks:** unknown or hidden parameters are rejected, and harmless type slips are fixed (for example `"5000"` becomes `5000`).
   - **Schema validation:** arguments are validated with `jsonschema` against the server's real input schema.
   - **Grounding rules:**
     - `check_eligibility` requires a pincode that the user actually typed, and an income the user stated.
     - `recommend_cards` checks **each** spend value separately. It must match a number the user wrote in the **current request**, either as written or converted monthly↔annual (×12 or ÷12). Lounge-visit fields can also be converted from per-month or per-year counts to per-quarter.
     - The current request is the current message, plus any earlier messages that the bot answered with a clarifying question.
     - Amounts from older messages, or from the bot's own earlier answers, are rejected. Gemini then asks the user to give or confirm them.
   - **On failure:** the error goes back to Gemini, which corrects the arguments or asks the user.

7. **Live MCP call** (`tools/client.py`)
   - **Connection:** one MCP session per chat turn, opened only when a tool is actually needed and always closed afterwards.
   - **Transport:** streamable HTTP, or SSE if the URL ends in `/sse`.
   - **Timeouts and errors:** timeouts are applied, and connection errors are converted into friendly messages.
   - **Caching:** the tool catalog (live schemas) is cached for 30 minutes.

8. **Grounded answer:** Gemini writes a conversational answer from the tool results only.
   - **Card facts need a tool:** if a draft names a bank or co-brand card, or states a ₹ amount or a percentage, and no MCP tool ran this turn, Gemini is re-prompted once. That retry *must* call a tool or `ask_clarification` (function-calling mode `ANY`).
   - **Fallback:** if the final answer still states card facts without any tool result, it is replaced with a short message offering to look the card up.

9. **Output guardrails** (`safety/grounding.py`, `safety/guardrails.py`)
   - **Unsupported figures:** every ₹ amount must match a number from **this turn's** MCP results, or a number the user wrote in the current request.
     - Tool figures must match as returned (up to 1% for display rounding), with no ×12 or ÷12 conversion. The user's own figures may also be converted monthly↔annual.
     - Both ends of a range are checked ("₹7,000–8,000", "₹1,000 to ₹1,500").
     - Hedged amounts ("around ₹7,000", "typically ₹1,000") must match exactly, without the 1% tolerance.
     - If an amount doesn't match, Gemini is asked once to rewrite the answer.
     - Anything still unsupported is replaced with `[amount not verified]`.
   - **Approval promises:** sentences that promise approval (e.g. "guaranteed approval") are removed.
   - **Tax advice:** sentences that give tax advice are removed.
   - **Disclaimers:** an approval or tax disclaimer is added when relevant.
   - **Final redaction:** the final text is redacted once more as a last defence.

10. **Session transcript** (`chat/transcript.py`): the turn is appended to the session's log file. This covers the redacted user message, the endpoint, Gemini and MCP calls, and the final answer. See [Logging](#logging).

### Conversation state
- Each browser gets an opaque, HttpOnly `gc_session` cookie.
- History is stored per session, in redacted form only, and capped at 12 messages.
- Figures from earlier turns' tool results are **not** carried over, so each answer can quote only that turn's tool results and the user's current request.
- While the bot is waiting for an answer to a clarifying question, the session keeps the text of the open request, so the user's earlier figures still count after they reply.
- A per-session lock prevents overlapping turns.
- Sessions expire after 1 hour, and the store is capped at 1000 sessions.

### Logging
All logs go into the project's `logs/` folder. The app creates the folder on startup if it doesn't exist, and `logs/` is listed in `.gitignore`, so logs are never committed.

> **Note on `logs_failure_example/`:** The `logs_failure_example/` directory contains logs from failure testing and is just an example of how logs will look after running the app. It has nothing to do with the running of the app and is strictly an example.

```
logs/
├── app.log                                   application log (rotates at 5 MB, keeps 3 backups)
└── sessions/
    ├── session_20261015-213000_ab12cd34ef56_4821.log
    └── session_20261015-214512_9f3e01c2aa47_1307.log
```

**1. Session transcripts (`logs/sessions/`)**

Every chat session gets its own log file, created on the session's first message. Each turn records:
- turn number, timestamp, the **HTTP endpoint** used (e.g. `POST /api/chat`) and the response time
- the **user's message** (after redaction, so card numbers, CVV, OTP, Aadhaar and similar never reach disk)
- which kinds of sensitive data were redacted, and whether a local guardrail blocked the message
- **Gemini API** usage: the number of `generateContent` calls and the model name
- every **MCP tool call**, with its arguments and status: `ok`, `error returned by tool`, `rejected: schema validation failed`, `rejected: values not provided by the user` or `failed: MCP server unavailable`
- the **chatbot's answer**, its kind (`local`, `clarification`, `answer`, `blocked` or `error`) and any privacy notices

Clicking **New chat** (`POST /api/reset`) writes a closing entry to the current file, and the next message starts a new file.

**File names**
- Format: `session_<start time>_<session tag>_<suffix>.log`.
- **Session tag:** a short one-way hash of the session cookie. The cookie value itself is never written to disk.
- **Suffix:** keeps two conversations started in the same second in separate files.

Example entry:
```
---- Turn 2 | 2026-10-15 21:31:02 | POST /api/chat | 3184 ms ----
USER:
    I spend 8000 on Amazon and 3000 on Swiggy every month
Gemini API: 2 generateContent call(s) [gemini-2.5-flash-lite]
MCP tools:
    - recommend_cards {"amazon_spends": 8000, "online_food_ordering": 3000} -> ok
ASSISTANT [answer]:
    ...
```

**2. Application log (`logs/app.log`)**

This file mirrors the console log: startup, tool catalog loading, warnings and errors.
- It records metadata only: message length, redaction categories, tool names and argument *field names*.
- A logging filter redacts personal data and masks the API key and MCP URL in every record (console and file), including tracebacks.
- Transport libraries are kept at WARNING level so request URLs are not logged.

---

## 3. Folder structure

```
great-cards/
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── schemas.py
│   ├── logging_config.py
│   ├── chat/
│   │   ├── __init__.py
│   │   ├── intents.py
│   │   ├── pipeline.py
│   │   ├── session.py
│   │   └── transcript.py
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── gemini.py
│   │   └── prompts.py
│   ├── safety/
│   │   ├── __init__.py
│   │   ├── redaction.py
│   │   ├── guardrails.py
│   │   └── grounding.py
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── client.py
│   │   └── registry.py
│   ├── templates/
│   │   └── index.html
│   └── static/
│       ├── css/styles.css
│       └── js/chat.js
├── data/
│   └── mcp_tools.json
├── logs/                     (created on startup, git-ignored)
│   ├── app.log
│   └── sessions/
│       └── session_<time>_<tag>_<suffix>.log
├── logs_failure_example/     (example failure testing logs; not used at runtime)
├── venv/
├── .env
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

### Root
| Path | Responsibility |
|---|---|
| `app/` | All application code (Python package). |
| `data/mcp_tools.json` | Offline snapshot of the MCP tool descriptions and schemas. **Reference only:** the app always uses the live schemas from the server at runtime. |
| `logs/` | Runtime logs, created automatically on startup and git-ignored: `app.log` (application log) and `sessions/` (one transcript file per chat session). See [Logging](#logging). |
| `logs_failure_example/` | Contains logs from failure testing as an example of how logs will look after running the app. It has nothing to do with the running of the app and is just an example. |
| `venv/` | The existing Python virtual environment. |
| `.env` | Your real configuration and secrets (never committed or logged). |
| `.env.example` | Template listing the required environment variables. |
| `.gitignore` | Keeps `.env`, `venv/`, `logs/` and Python caches out of git. |
| `inline_text.md` | Log of the development conversation used to build this project. |
| `requirements.txt` | Python dependencies (FastAPI, Uvicorn, Jinja2, python-dotenv, Pydantic, httpx, mcp, google-genai, jsonschema). |

### `app/` (top level)
| File | Responsibility |
|---|---|
| `main.py` | Creates the FastAPI app. On startup it creates `logs/` if it's missing, sets up logging and the session transcript logger, and builds the Gemini client, the MCP gateway (and starts loading the tool catalog in the background), the session store and the pipeline; on shutdown it closes them. Also defines the routes (`/`, `/api/chat`, `/api/reset`, `/health`), the session cookie, security headers (CSP and others) and the validation-error handler. |
| `config.py` | Loads `.env` without overriding real environment variables, treats unfilled placeholders as missing, and lists missing variables by name only. Also holds the fixed limits (message length, tool steps, history size, session TTL, MCP timeout) and the log paths (`LOG_DIR`, `SESSION_LOG_DIR`, `APP_LOG_FILE`). |
| `schemas.py` | Pydantic models for the chat request and response. |
| `logging_config.py` | Logging setup: console output plus a rotating `logs/app.log`, and `RedactingFilter`, which removes personal data and secrets from every log record. |

### `app/chat/`: conversation flow
| File | Responsibility |
|---|---|
| `pipeline.py` | `ChatPipeline`, which runs the full flow for one turn (stages 1–9 above): redaction, local intents, guardrails, the Gemini tool loop, argument validation, MCP execution, figure grounding and output guardrails. |
| `intents.py` | Regex-based small talk (greetings, help, thanks, bye) answered locally. |
| `session.py` | `Conversation` (redacted history, the open request carried across clarifying questions, lock, turn count, transcript file path) and `SessionStore` (expiry, size cap, eviction of least-recently-used sessions). |
| `transcript.py` | `TurnTrace` (collects the endpoint, Gemini calls, MCP calls and guardrail outcome for one turn) and `TranscriptLogger` (creates a log file per chat session and appends each turn and the reset event, writing off the event loop). |

### `app/llm/`: Gemini integration
| File | Responsibility |
|---|---|
| `gemini.py` | Async `GeminiClient` (timeout from `LLM_TIMEOUT_SECONDS`, friendly error mapping). Converts MCP JSON Schemas into Gemini function declarations and provides helpers for messages and function calls. |
| `prompts.py` | System prompt (scope, tool-selection guide, clarification rules, safety rules, style), the `ask_clarification` declaration, the prompt that asks Gemini to rewrite answers with unsupported figures, and the prompt that requires a tool call when a draft states card facts without one. |

### `app/safety/`: privacy and guardrails
| File | Responsibility |
|---|---|
| `redaction.py` | Detects and redacts sensitive data; flags critical secrets (card number, CVV, OTP, PIN, password). |
| `guardrails.py` | `check_input` (injection, fraud, tax, off-topic, approval questions) and `enforce_output` (removes approval promises and tax advice, adds disclaimers). |
| `grounding.py` | Extracts numbers from evidence and checks or removes ₹ figures in answers that the evidence doesn't support. It checks both ends of ranges and is stricter with hedged estimates. |

### `app/tools/`: Great.cards MCP integration
| File | Responsibility |
|---|---|
| `client.py` | `McpToolGateway`: opens MCP sessions (streamable HTTP or SSE), loads and caches the live tool catalog and advisor context, and runs tool calls with timeouts through a per-turn `ToolConnection`. |
| `registry.py` | Lists which tools Gemini may use and which parameters are hidden, builds the schema shown to Gemini, cleans arguments, validates them with `jsonschema` against the live schema, and checks that values came from the user. |

### `app/templates/` and `app/static/`: web UI
| File | Responsibility |
|---|---|
| `templates/index.html` | Chat page: header with "New chat", welcome message, suggestion chips, message box, privacy footnote and "not configured" banner. |
| `static/css/styles.css` | Clean, responsive styling (chat bubbles, tables, tags, notices, typing indicator). |
| `static/js/chat.js` | Sends messages, shows a typing indicator, renders safe minimal markdown (everything escaped first: bold, lists, tables, links), shows "Source" tags and privacy notices, and handles reset. |

### Exposed MCP tools
| Tool | Used for |
|---|---|
| `recommend_cards` | Personalised ranking from the user's stated monthly spends |
| `list_cards` | Browsing and filtering (category, lifetime free, network, fee range) |
| `get_card_details` | Deep dive into one card |
| `compare_cards` | Side-by-side comparison of 2–3 cards |
| `check_eligibility` | Cards the user can apply for (pincode, income, employment) |
| `generate_content_brief` | "Best cards for a category", "is a card worth the fee", bank rankings, upgrade paths |
| `whatsapp_resolve_card_or_category` | Turning a card name the user typed into the exact card alias |

`get_advisor_context` is loaded internally once. Diagnostic and duplicate tools are not exposed to Gemini.
