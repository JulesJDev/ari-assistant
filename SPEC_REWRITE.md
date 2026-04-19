# Ari Assistant — Complete Rewrite Specification

## 1. Goals
Rewrite “Ari” from scratch: a local, francophone, low-latency voice AI assistant with a tavern RPG theme. Runs entirely locally (no cloud infrastructure), uses OpenRouter as LLM provider, and provides a fully anarchic dev mode (no authentication required in production but 2FA for remote connections). The system must be production-ready, well-architected, and easy to deploy (single-file frontend, monolith backend).

## 2. Stack Cible
- **Backend**: FastAPI + uvicorn, Python 3.12+, 100% async native.
- **Frontend**: Single `index.html` (no bundler) — HTML + CSS + vanilla JS inline.
- **TTS**: edge-tts (Microsoft, remote) + Piper (local, subprocess, optional).
- **LLM**: OpenRouter only, streaming SSE.
- **Memory**: JSON files on disk (no database).
- **Config**: python-dotenv (`.env`).
- **Backend dependencies**: `fastapi`, `uvicorn[standard]`, `httpx`, `edge-tts`, `python-dotenv`, `ddgs`, `tavily-python`, `python-multipart`.

## 3. Architecture Overview
```
[index.html — SPA monolithique]
      ↕️ WebSocket ws://127.0.0.1:8000/ws
      ↕️ HTTP    http://127.0.0.1:8000/api/*
[FastAPI app.py]
      → OpenRouter API (streaming SSE)
      → Edge TTS / Piper TTS
      → Tavily / DDGS / DuckDuckGo (web search, 4-level fallback)
      ↓
[Disque local : memory_profiles/{user}/{profile}/]
```

## 4. Project Structure
```
project/
├── app.py                  # Backend FastAPI complet
├── index.html              # Frontend SPA monolithique
├── launcher.py             # GUI Tkinter de lancement (optionnel)
├── requirements.txt         # Sans versions épinglées
├── .env.example            # Template de config
└── memory_profiles/        # Créé automatiquement au runtime
    └── {user_id}/
        └── {profile_id}/
            ├── memory.json           # Tours bruts (max 80)
            ├── memory_fast.json      # Préférences compressées
            ├── memory_secondary.json # Faits épisodiques
            ├── llm_usage.json        # Suivi coûts
            ├── avatar_config.json    # Config avatar persistée
            └── history/              # Sessions de conversation
                └── {session_id}.json
```

## 5. Backend — `app.py` Full Specification

### 5.1 Principles
- Split logic into *modules within the same file* via clear classes/functions — monolith voluntarily for simple deployment.
- **Always atomic JSON writes**: use `tmp` + `os.replace`.
- All payloads WebSocket modelled with **Pydantic**.
- Use **`httpx.AsyncClient`** exclusively; no blocking sync client in async code.
- For sync third-party libraries (`ddgs`), wrap with `asyncio.to_thread()`.
- Use **lifespan pattern** (no deprecated `@app.on_event`).
- CORS restricted to localhost in production.
- WebSocket **heartbeat** every 25 seconds.
- Never log API keys — mask with `***` if needed.
- **Anti zip-slip** for Live2D uploads.
- No placeholders; final code production-ready.

### 5.2 Configuration & Environment
Load via `load_dotenv()`. Variables:

| Variable | Default | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | (required) | OpenRouter API key |
| `OPENROUTER_MODEL` | `google/gemma-3-27b-it:free` | Primary model |
| `OPENROUTER_FALLBACK_MODELS` | `meta-llama/llama-3.2-3b-instruct:free,mistralai/mistral-7b-instruct:free` | Comma-separated |
| `OPENROUTER_MAX_MODEL_TRIES` | `6` | Max rotation attempts |
| `EDGE_VOICE` | `fr-FR-DeniseNeural` | Edge TTS voice |
| `EDGE_RATE` | `+25%` | Rate modifier |
| `EDGE_PITCH` | `+0Hz` | Pitch modifier |
| `EDGE_VOLUME` | `+0%` | Volume modifier |
| `EDGE_TTS_TIMEOUT` | `18` | Seconds |
| `MEMORY_MAX_TURNS` | `80` | Max raw memory turns |
| `MEMORY_PROMPT_TURNS` | `6` | Turns injected in prompt |
| `MEMORY_OPTIMIZE_INTERVAL_SEC` | `14400` (4h) | Optimizer interval |
| `DEFAULT_USER_ID` | `default` | |
| `DEFAULT_PROFILE_ID` | `default` | |
| `AI_NAME` | `Ari` | Assistant name |
| `WEB_SEARCH_PROVIDER` | `auto` | `auto` \| `tavily` \| `ddgs` \| `duckduckgo` |
| `TAVILY_API_KEY` | (optional) | Tavily API key |
| `PIPER_BIN` | (optional) | Piper executable path |
| `PIPER_MODEL_PATH` | (optional) | Piper model path |
| `REASONING_BAIL_MAX_CHARS` | `8192` | Max reasoning block size |
| `INTERNET_ENABLED_DEFAULT` | `true` | Default internet toggle |
| `PORT` | `8000` | Server port (use 8080 on local network) |

Implementation: a `Config` class reads these with `os.getenv`.

### 5.3 Pydantic Models
All messages and data structures must be modelled.

**WebSocket payloads:**
```python
class WsMsgGetConfig(BaseModel):
    type: Literal["get_config"]

class WsMsgSetConfig(BaseModel):
    type: Literal["set_config"]
    user_id: str
    profile_id: str
    ai_name: str
    model: str
    system_prompt: str
    edge_voice: str
    edge_rate: str
    edge_pitch: str
    edge_volume: str
    tts_engine: Literal["edge", "piper", "auto", "off"]
    temperature: float
    max_tokens: int
    internet_enabled: bool
    avatar_config: dict
    # … extensible

class WsMsgUserText(BaseModel):
    type: Literal["user_text"]
    text: str
    silent: bool = False
    profile: str = "default"
    user_id: Optional[str] = None  # debug/override
```

**Internal data models:**
```python
class MemoryTurn(BaseModel):
    ts: str                # ISO 8601
    user: str
    assistant: str

class MemoryFile(BaseModel):
    turns: list[MemoryTurn] = []
    summary: str = ""

class MemoryFastFile(BaseModel):
    items: list[str] = []
    meta: dict = {}

class MemorySecondaryFile(BaseModel):
    items: list[str] = []
    meta: dict = {}

class LlmUsageBucket(BaseModel):
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_usd: float = 0.0

class LlmUsageFile(BaseModel):
    version: int = 1
    chat: dict[str, LlmUsageBucket] = {"free": LlmUsageBucket(), "paid": LlmUsageBucket()}

class LibraryItem(BaseModel):
    name: str
    path: str
    type: str  # "model", "background", "animation"

class AnimationCatalog(BaseModel):
    id: str
    name: str
    preview_url: Optional[str] = None
    tags: list[str] = []
```

**2FA models:**
```python
class WsAuth2FABegin(BaseModel):
    type: Literal["auth_2fa_begin"]
    user_id: str

class WsAuth2FAVerify(BaseModel):
    type: Literal["auth_2fa_verify"]
    user_id: str
    code: str
```

### 5.4 Atomic Write Helper
```python
import os, json, tempfile
from pathlib import Path

def atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
```

### 5.5 Memory Manager
In-memory caches:
```python
_memory_cache: dict[str, MemoryFile] = {}      # key "user/profile"
_fast_cache: dict[str, MemoryFastFile] = {}
_secondary_cache: dict[str, MemorySecondaryFile] = {}
```

`MemoryManager` class with methods:
- `load_memory(user_id, profile_id, layer)` — returns dict, uses cache.
- `save_memory(user_id, profile_id, layer, data)` — atomic write + update cache.
- `append_message(user_id, profile_id, role, content)` — adds turn, trims to `MEMORY_MAX_TURNS`.
- `build_context(user_id, profile_id, max_tokens=2000)` — builds LLM context list: system prompt + preferences from fast layer + episodic facts from secondary (last 5) + recent raw turns (last 20). Each as `{"role": ..., "content": ...}`.
- `compress_memory(user_id, profile_id)` — simple heuristic: extract likes/dislikes from user messages into fast; add milestone facts into secondary. In production, replace with a small LLM (llama-3.2-3b) to compress.

### 5.6 LLM Streamer
Class `LLMStreamer`:
- `__init__`: creates `httpx.AsyncClient(timeout=30.0)`, sets `self.models = [CONFIG.OPENROUTER_MODEL] + CONFIG.OPENROUTER_FALLBACK_MODELS.split(",")`, sets usage file path.
- `stream_llm(messages, model_override=None, on_chunk=None) -> AsyncGenerator[str, None]`:
  - Prepare headers: `Authorization: Bearer <key>`, `Content-Type: application/json`, `HTTP-Referer`, `X-Title`.
  - Iterate models in order, up to `OPENROUTER_MAX_MODEL_TRIES`.
  - For each model, POST to `https://openrouter.ai/api/v1/chat/completions` with stream=True, max_tokens=1024, temperature=0.7.
  - Parse SSE: each line `data: {...}`. Extract `delta.content` and `delta.reasoning`.
  - **Reasoning block handling**: accumulate `reasoning_buffer`. If it exceeds `REASONING_BAIL_MAX_CHARS`, yield `"[RAISONNEMENT TROP LONG - INTERRUPTION]"` and break.
  - **Tag filtering**: filter `<think>`, `<reasoning>`, `<scratchpad>`, `<analysis>` via `_filter_tags`.
  - Yield filtered `content` tokens; also call `on_chunk` if provided.
  - After stream finishes, record usage via `_record_usage(model, total_tokens)`.
  - On any exception, try next model. If all fail, raise.
- `_filter_tags(text: str) -> str`: regex remove tags.
- `_record_usage(model, tokens)`: atomic update of `llm_usage.json` daily buckets.

### 5.7 TTS Worker
Queue-based worker.
- `TTSWorker.__init__`: `self.queue = asyncio.Queue()`, `self.running=False`, `self.task=None`.
- `start()`: set running, create `asyncio.create_task(self._worker_loop())`.
- `stop()`: cancel task.
- `enqueue(text, websocket)`: `await self.queue.put({"text": text, "websocket": websocket})`.
- `_worker_loop`: `while self.running: item = await wait_for(queue, 1.0); await _synthesize(item)`
- `_synthesize(text, websocket)`:
  - **Edge TTS**: try:
    ```python
    from edge_tts import Communicate
    communicate = Communicate(text, CONFIG.EDGE_VOICE, rate=CONFIG.EDGE_RATE, pitch=CONFIG.EDGE_PITCH, volume=CONFIG.EDGE_VOLUME)
    audio_chunks = []
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_chunks.append(chunk["data"])
    if audio_chunks:
        b64 = base64.b64encode(b"".join(audio_chunks)).decode()
        await websocket.send_json({"type": "tts_audio", "data": b64})
        return
    ```
  - **Piper fallback** if edge fails and `PIPER_BIN`/`PIPER_MODEL_PATH` set:
    ```python
    proc = await asyncio.create_subprocess_exec(
        CONFIG.PIPER_BIN, "--model", CONFIG.PIPER_MODEL_PATH,
        "--output_file", "/tmp/tts_output.wav",
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    await proc.communicate(text.encode())
    if proc.returncode == 0:
        data = Path("/tmp/tts_output.wav").read_bytes()
        b64 = base64.b64encode(data).decode()
        await websocket.send_json({"type":"tts_audio","data":b64})
    ```
- **Segmentation** (`split_into_tts_segments`):
  - Split on punctuation: `.`, `!`, `?`, `;` *only if* followed by whitespace.
  - Do NOT split commas inside numbers (`12,5`) or abbreviations (`M.`, `Dr.`, `etc.`).
  - If no split point and segment > 80 chars, split on comma as last resort.
  - Returns list of segments.

### 5.8 Web Search (4-level fallback)
Class `WebSearcher`:
- `async search(query) -> str` with timeout 8 seconds.
  - **Level 1** — Tavily API (if `TAVILY_API_KEY`):
    ```python
    resp = await client.post("https://api.tavily.com/search", json={"api_key": key, "query": query, "max_results":3, "include_answer":True})
    return resp.json().get("answer","")
    ```
  - **Level 2** — `duckduckgo-search` library (sync, use `asyncio.to_thread`).
  - **Level 3** — HTML scrape: `GET https://html.duckduckgo.com/html/?q=query`, parse first `<a class="result__a">` text.
  - **Level 4** — Instant Answer API: `GET https://api.duckduckgo.com/?q=query&format=json&no_html=1`; return `Answer` or `Abstract`.
- Return first non-empty result.

### 5.9 2FA (Remote-Only)
- Store: `_2fa_codes: dict[user_id, {"code": str, "expires": datetime, "attempts": int}]`.
- **Trigger**: Only for connections where client IP is not loopback (`127.0.0.1` or `::1`). Local connections skip.
- **Flow**:
  1. Client sends `{"type":"auth_2fa_begin","user_id":"..."}`.
  2. Server generates 4‑digit code (`secrets.randbelow(9000)+1000`), stores with 120s expiry, attempts=0. Prints code to server console (for local admin help).
  3. Server sends `{"type":"2fa_challenge","data":{"user_id":..., "message":"Code sent"}}`. Frontend shows 4 buttons, one correct (random order).
  4. Client sends `{"type":"auth_2fa_verify","user_id":"...","code":1234}`.
  5. Server validates: exists, not expired, attempts<3, code matches.
        - Success: mark `session_state.authenticated = True`, delete code.
        - Failure: increment attempts, send error with remaining attempts.
  6. If authenticated, allow `user_text` messages; else ignore/error.
- **Rate limiting**: max 3 attempts; then delete code.

### 5.10 HTTP Routes
| Method | Path | Description |
|---|---|---|
| `GET /` | Serve `index.html` with no‑cache headers |
| `GET /assets/{path}` | Static files |
| `GET /api/library` | List models/backgrounds/live2d from `assets/` |
| `GET /api/animation-catalog` | Animations metadata |
| `POST /api/upload/animation` | Upload animation file |
| `POST /api/upload/background` | Upload background |
| `POST /api/upload/model` | Upload 3D model |
| `POST /api/upload/live2d` | Upload Live2D pack (zip‑slip protected) |
| `GET /api/conversations/{user}/{profile}` | List session files |
| `GET /api/conversations/{user}/{profile}/{session_id}` | Read session JSON |
| `GET /api/usage/{user_id}/{profile_id}` | LLM usage stats |

### 5.11 WebSocket (`/ws`)
**ConnectionManager**: tracks `active_connections: dict[client_id, WebSocket]`, sends heartbeat every 25s.

**SessionState** (per connection):
- `client_id: str`
- `user_id: str` (from query param or anonymous `anon-{client_id}`)
- `profile_id: str` (default "default")
- Config fields: `ai_name`, `model`, `system_prompt`, `tts_*`, `internet_enabled`, `avatar_config`
- `authenticated: bool` (for 2FA)
- `heartbeat_task`

**Message handlers** (use `match msg_type:`):
- `"get_config"` → `handle_get_config`: send current config.
- `"set_config"` → `handle_set_config`: update session fields, optionally persist avatar_config.
- `"user_text"` → `handle_user_text` (full pipeline below).
- `"auth_2fa_begin"` → `handle_2fa_begin`.
- `"auth_2fa_verify"` → `handle_2fa_verify`.
- unknown → error.

**`handle_user_text` pipeline**:
1. Send `{"type":"state","state":"thinking"}`.
2. Build memory context via `memory_mgr.build_context`.
3. Decide web search: `needs_web = session.internet_enabled and any(kw in text.lower() for kw in ["recherche","cherche","actualité","news","quoi de neuf"])`.
4. Parallel fetch: `web_ctx, secondary_ctx = await asyncio.gather(fetch_web_context(text) if needs_web else "", memory_mgr.load_memory(..., "secondary"), return_exceptions=True)`.
5. If `needs_web` and result, send interim chunk `"Je consulte le web..."`.
6. Build LLM messages: system prompt + context + secondary facts + web snippet + user message.
7. Create `tts_queue = asyncio.Queue()`. Start `tts_task = asyncio.create_task(tts_worker(tts_queue, websocket, session))`.
8. Stream LLM:
   - `full_response = ""`, `pending_segment = ""`
   - `async for token in llm_streamer.stream_llm(messages, session.model, ...)`
     - Append to `full_response` and `pending_segment`.
     - Send `assistant_chunk` to WS.
     - `segments = split_into_tts_segments(pending_segment)`; if `len(segments)>1`, enqueue all but last into TTS queue, keep last as `pending_segment`.
9. After loop: enqueue last segment if any.
10. Send `None` to TTS queue to signal end, await `tts_task`.
11. Detect emotion via `detect_emotion(full_response)` (simple keyword rules; optional async LLM fallback non-blocking).
12. Persist turn: `memory_mgr.append_message(user_id, profile, "user", text)` and assistant.
13. Gather usage stats `await get_usage_stats(session)`.
14. Send `assistant_done` with emotion and usage; set state `"idle"`.

**Other notes**:
- Prompt leak detection: run regex patterns on every chunk *and* final response before forwarding to client. Patterns: `r"règle prioritaire"`, `r"tu dois l'appeler par"`, `r"system says"`, `r"the instruction says"` (case‑insensitive). If leak detected, replace chunk with `[FILTRÉ]` and log.
- If LLM fails after all fallbacks, send error and fallback response `"Désolé, je ne peux pas répondre pour le moment."`

### 5.12 Background Tasks
- **Memory optimizer**: `async def memory_optimizer_loop()`: `while True: await asyncio.sleep(CONFIG.MEMORY_OPTIMIZE_INTERVAL_SEC); for each user/profile: await memory_mgr.compress_memory(...)`.
- **2FA cleanup**: optional periodic task to purge expired codes; can also be done on‑demand during verify.

### 5.13 Security Checklist
- ⚠️ Never log full API keys (mask: `key[:8] + "***"`).
- ⚠️ Zip‑slip protection when extracting Live2D archives.
- ⚠️ Rate limit 2FA (max 3 attempts).
- ⚠️ CORS middleware restricted to `http://127.0.0.1:8000`, `http://localhost:8000`, methods `GET,POST`.
- ⚠️ Prompt‑leak patterns filtered server‑side.
- ⚠️ Use `secrets` module for random codes.

### 5.14 `lifespan` Manager
```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(memory_optimizer_loop())
    yield
    # on shutdown: cancel background tasks, close httpx clients
```

### 5.15 Startup
`app = FastAPI(lifespan=lifespan)`
Add middleware, mount static, include routes.

---

## 6. Frontend — `index.html` Full Specification

### 6.1 General Structure
- Single HTML file.
- CSS in `<style>` block (dark parchment tavern theme).
- JS in `<script>` block (ES2022 classes).
- External libraries via CDN (no bundler):
  - Three.js r163
  - `@pixiv/three-vrm` (for VRM avatars)
  - PixiJS v7
  - `pixi-live2d-display` (Cubism 4)
  - (Optional) howler.js for audio? We'll use HTML5 Audio.

### 6.2 Configuration
```javascript
const CONFIG = {
  WS_URL: `ws://${location.host}/ws`,
  RECONNECT_DELAY_MS: 2000,
  AUDIO_MAX_QUEUE: 20,
  STORAGE_KEYS: {
    USERS: "assistant_users_v3",
    ACTIVE_USER: "assistant_active_user_v1",
    PROFILES: userId => `assistant_profiles_v2_${userId}`,
    UI_CONFIG: userId => `assistant_ui_config_v2_${userId}`,
  },
};
```

### 6.3 WebSocket Manager (`WsManager`)
- `connect()`: create WS, set `onmessage` → dispatch to registered handlers by `msg.type`.
- `send(type, payload)`: `ws.send_json({type, ...payload})`.
- `onMessage(type, handler)`: register.
- `reconnect()`: scheduled back‑off.
- On `__connected` event, re‑send config.

### 6.4 Audio Queue (`AudioQueue`)
- Internal queue `Array<base64Audio>`.
- `enqueue(base64Audio)`: push; if queue was empty, `_playNext()`.
- `_playNext()`: create `Audio` object with `src = "data:audio/wav;base64,"+data`; on `ended` play next.
- `clear()`: stop current and empty queue.

### 6.5 Auth Manager (`AuthManager`) — 2FA Remote
- **Properties**: `currentUser`, `profiles`, `authenticated`.
- `login(userId, pin)` / `register(...)` — *not used* in this rewrite (optional local PIN). Instead, for remote connections, flow:
  - `request2FA(userId)`: send `auth_2fa_begin`; store pending user.
  - `verify2FA(code)`: send `auth_2fa_verify`; on success, set `authenticated=true`.
- `isLoggedIn()`: true if localhost or `authenticated`.
- On startup, check `localStorage` for trusted device; auto‑login if found.
- Store trusted devices in `localStorage` (encrypted optional).

### 6.6 Chat Manager (`ChatManager`)
- DOM references: chat container.
- `addUserBubble(text)`: append right‑aligned bubble.
- `appendAiChunk(token)`: append to current AI bubble (or create new), streaming style.
- `finalizeAiBubble(emotion)`: finalize bubble, add emotion class.
- `clear()`: empty chat.

### 6.7 Avatar Manager (`AvatarManager`)
- Modes: `"3d"` (Three.js VRM), `"pixel"` (PixiJS sprite), `"2d"` (Live2D).
- Methods: `setMode(mode)`, `loadModel(url)`, `setEmotion(emotion)`, `startLipSync(audioAnalyser)`.
- 3D: use Three.js scene with VRM loader; emotion via blend shapes.
- Live2D: via pixi‑live2d‑display; set expression by name.
- Lip sync: simple audio‑frequency amplitude mapped to mouth open value.

### 6.8 Voice Input (`VoiceInput`)
- Use `webkitSpeechRecognition` if available.
- `start()`: begin listening; `onresult` → emit text.
- `stop()`: stop recognition.
- Continuous mode optional.

### 6.9 Settings Panel (`SettingsPanel`)
- Modal UI with sliders/selects for all config fields.
- `loadConfig(config)` populates controls.
- `getConfig()` returns current values.
- On change → send `set_config` WS.

### 6.10 Usage Dock (`UsageDock`)
- Shows token usage (free/paid split).
- `update(usageStats)` refreshes numbers.
- `setMode(mode)` toggles free/paid view.

### 6.11 UI / CSS — Tavern RPG Theme
- Dark background (#1a1510) with parchment texture noise.
- Chat bubbles: user `#d4a574` (warm), AI `#4a6fa5` (mystic blue).
- Thinking state: pulsing glow on avatar.
- Speaking state: subtle voice wave animation.
- Font: “MedievalSharp” or similar Google Font.
- Buttons: metallic gradient, hover effects.
- Responsive: mobile‑friendly layout (flex/grid).

### 6.12 Initialization Flow
- `init()`:
  - Load config from localStorage or defaults.
  - Instantiate managers.
  - `WsManager.connect()`.
  - On `__connected`, send `get_config`.
  - On `config` response, update UI.
  - If `!AuthManager.isLoggedIn()`, show 2FA PIN screen; else `ChatManager` visible.

---

## 7. Launcher — `launcher.py` (Tkinter GUI)
- Detect/create virtualenv:
  ```python
  VENV = Path(__file__).parent / "venv"
  if not (VENV / "bin/python3").exists():
      subprocess.check_call([sys.executable, "-m", "venv", str(VENV)])
  ```
- Ensure dependencies: if `requirements.txt` newer than venv, run `pip install -r requirements.txt`.
- Start server: `subprocess.Popen([str(VENV/"bin/python"), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8080", "--reload"], ...)`
- Capture stdout/stderr → display in scrolling Text widget.
- “Open Interface” button → `webbrowser.open("http://127.0.0.1:8080")`.
- Handle window close: terminate uvicorn process gracefully (send SIGTERM, wait, kill if needed).

---

## 8. Development Order (recommended)
1. Backend — models, memory, LLM, TTS, search, 2FA, routes, WS.
2. Frontend — skeleton, WS client, chat, avatar, voice, settings.
3. Launcher — GUI, venv handling.

---

## 9. Testing Data (no API key)
- Add endpoint `/api/test/echo` that streams back a canned response.
- Prepopulate `memory_profiles/test/default/memory.json` with a few turns for UI preview.

---

## 10. Anti‑Patterns (Never Do)
❌ Do NOT use `httpx.Client` (sync) inside async code.  
❌ Do NOT write JSON directly — always atomic write.  
❌ Do NOT hard‑code API keys or log them.  
❌ Do NOT use `@app.on_event("startup")` (deprecated).  
❌ Do NOT use `asyncio.sleep()` for resource waiting (use proper primitives).  
❌ Do NOT split TTS segments on commas inside numbers (e.g., “12,5”).  
❌ Do NOT store session state in global variables; keep in `SessionState`.  
❌ Do NOT skip CORS middleware.  
❌ Do NOT use `random.randint` for security codes — always `secrets`.

---

## 11. Additional Notes
- All file I/O should be `await`‑compatible where possible, but memory files are small so sync `Path` ops okay inside async (wrap in `to_thread` if heavy).
- The optimizer loop runs every 4h; on first run it may create fast/secondary summaries.
- Frontend must work offline (after initial load) except for TTS/LLM/WebSearch which need network.
- Support both localhost and LAN (port 8080) — ensure CORS includes LAN IP? For development, allow `http://127.0.0.1:8080` only; for LAN access you may adjust.
- The server should be able to run without `.env` (uses defaults) but LLM will fail without `OPENROUTER_API_KEY`.
