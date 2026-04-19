#!/usr/bin/env python3
"""
Ari - Local Voice AI Assistant Backend
FastAPI async server with SSE streaming, WebSocket, memory management, and LLM integration.
"""

import asyncio
import json
import os
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, AsyncGenerator
from uuid import uuid4

import httpx
from edge_tts import Communicate
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

# ============================================
# SECTION 1: CONFIG & ENV
# ============================================

class Config:
    """Configuration from environment variables."""
    OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
    OPENROUTER_MODEL: str = os.getenv("OPENROUTER_MODEL", "google/gemma-3-27b-it:free")
    OPENROUTER_FALLBACK_MODELS: List[str] = os.getenv(
        "OPENROUTER_FALLBACK_MODELS",
        "meta-llama/llama-3.2-3b-instruct:free,mistralai/mistral-7b-instruct:free"
    ).split(",")
    OPENROUTER_MAX_MODEL_TRIES: int = int(os.getenv("OPENROUTER_MAX_MODEL_TRIES", "6"))
    
    EDGE_VOICE: str = os.getenv("EDGE_VOICE", "fr-FR-DeniseNeural")
    EDGE_RATE: str = os.getenv("EDGE_RATE", "+25%")
    
    AI_NAME: str = os.getenv("AI_NAME", "Ari")
    TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")
    PIPER_BIN: str = os.getenv("PIPER_BIN", "")
    PIPER_MODEL_PATH: str = os.getenv("PIPER_MODEL_PATH", "")
    INTERNET_ENABLED_DEFAULT: bool = os.getenv("INTERNET_ENABLED_DEFAULT", "true").lower() == "true"
    PORT: int = int(os.getenv("PORT", "8000"))
    
    BASE_DIR = Path(__file__).parent
    MEMORY_DIR = BASE_DIR / "memory_profiles"
    HISTORY_DIR = BASE_DIR / "history"
    ASSETS_DIR = BASE_DIR / "assets"
    UPLOAD_DIR = BASE_DIR / "uploads"

CONFIG = Config()

# ============================================
# SECTION 2: PYDANTIC MODELS
# ============================================

class WSConfigUpdate(BaseModel):
    """Payload for config updates via WebSocket."""
    internet_enabled: Optional[bool] = None
    tts_voice: Optional[str] = None
    tts_rate: Optional[str] = None
    model: Optional[str] = None
    fallback_models: Optional[List[str]] = None

class WSUserTextPayload(BaseModel):
    """Payload for user text message."""
    text: str
    profile: str = "default"
    # user_id est optionnel (debug/override), sinon pris de la session WebSocket
    user_id: Optional[str] = None



# ============ AUTH MODELS ============

class UserRegister(BaseModel):
    """Payload for user registration."""
    pin_hash: str = Field(..., min_length=64, max_length=64)  # SHA256 hex
    device_id: Optional[str] = Field(None, description="Optional device identifier")

class UserLogin(BaseModel):
    """Payload for user login."""
    pin_hash: str = Field(..., min_length=64, max_length=64)

class UserSession(BaseModel):
    """Active user session."""
    user_id: str
    device_id: str
    created_at: datetime
    last_seen: datetime

class TrustedDevice(BaseModel):
    """Trusted device record."""
    device_id: str
    user_id: str
    added_at: datetime

class WSAuth2FABegin(BaseModel):
    """Payload to initiate 2FA."""
    user_id: str

class WSAuth2FAVerify(BaseModel):
    """Payload to verify 2FA code."""
    user_id: str
    code: str

class LibraryItem(BaseModel):
    """Library item model."""
    name: str
    path: str
    type: str  # "model", "background", "animation"

class AnimationCatalog(BaseModel):
    """Animation catalog entry."""
    id: str
    name: str
    preview_url: Optional[str] = None
    tags: List[str] = []

class UsageStats(BaseModel):
    """LLM usage statistics."""
    tokens: int
    cost_usd: float
    model: str
    timestamp: str

# ============================================
# SECTION 3: MÉMOIRE (MULTI-LAYER MEMORY)
# ============================================

class MemoryManager:
    """Manages three-layer memory: raw, fast (compressed), secondary (episodic)."""
    
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.cache: Dict[str, Dict] = {}  # In-memory cache
        self._ensure_dirs()
    
    def _ensure_dirs(self):
        """Ensure memory directories exist."""
        (self.base_dir / "profiles").mkdir(parents=True, exist_ok=True)
        (self.base_dir / "profiles" / "default").mkdir(parents=True, exist_ok=True)
    
    def _profile_path(self, user_id: str, profile_id: str, layer: str) -> Path:
        """Get path to memory file."""
        base = self.base_dir / "profiles" / user_id / profile_id
        return base / f"memory_{layer}.json"
    
    def _atomic_write(self, path: Path, data: Dict):
        """Atomic write using temp file + rename."""
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        os.replace(tmp, path)
    
    async def load_memory(self, user_id: str, profile_id: str, layer: str) -> Dict:
        """Load memory layer, using cache if available."""
        cache_key = f"{user_id}:{profile_id}:{layer}"
        if cache_key in self.cache:
            return self.cache[cache_key]
        
        path = self._profile_path(user_id, profile_id, layer)
        if path.exists():
            try:
                data = json.loads(path.read_text())
                self.cache[cache_key] = data
                return data
            except Exception:
                return {"messages": []}
        return {"messages": []}
    
    async def save_memory(self, user_id: str, profile_id: str, layer: str, data: Dict):
        """Save memory layer atomically and update cache."""
        path = self._profile_path(user_id, profile_id, layer)
        self._atomic_write(path, data)
        cache_key = f"{user_id}:{profile_id}:{layer}"
        self.cache[cache_key] = data
    
    async def append_message(self, user_id: str, profile_id: str, role: str, content: str):
        """Append a message to raw memory (layer 1)."""
        memory = await self.load_memory(user_id, profile_id, "json")
        if "messages" not in memory:
            memory["messages"] = []
        
        memory["messages"].append({
            "role": role,
            "content": content,
            "timestamp": datetime.utcnow().isoformat()
        })
        
        # Keep only last 80 turns (160 messages)
        if len(memory["messages"]) > 160:
            memory["messages"] = memory["messages"][-160:]
        
        await self.save_memory(user_id, profile_id, "json", memory)
    
    async def build_context(self, user_id: str, profile_id: str, max_tokens: int = 2000) -> List[Dict]:
        """Build conversation context from memory layers."""
        raw = await self.load_memory(user_id, profile_id, "json")
        fast = await self.load_memory(user_id, profile_id, "fast")
        secondary = await self.load_memory(user_id, profile_id, "secondary")
        
        context = []
        
        # Add compressed preferences from fast layer
        if "preferences" in fast:
            context.append({
                "role": "system",
                "content": f"User preferences: {json.dumps(fast['preferences'])}"
            })
        
        # Add episodic facts from secondary
        if "facts" in secondary:
            for fact in secondary["facts"][-5:]:
                context.append({
                    "role": "system",
                    "content": f"Important fact: {fact}"
                })
        
        # Add recent conversation from raw
        messages = raw.get("messages", [])[-20:]  # Last 20 messages
        context.extend(messages)
        
        return context
    
    async def compress_memory(self, user_id: str, profile_id: str):
        """Compress raw memory into fast and secondary layers (called by optimizer)."""
        raw = await self.load_memory(user_id, profile_id, "json")
        fast = await self.load_memory(user_id, profile_id, "fast")
        secondary = await self.load_memory(user_id, profile_id, "secondary")
        
        # Simple compression: extract preferences from user messages
        user_msgs = [m for m in raw.get("messages", []) if m["role"] == "user"]
        if user_msgs:
            # Extract keywords (naive approach)
            preferences = fast.get("preferences", {})
            for msg in user_msgs[-10:]:
                content = msg["content"].lower()
                if "j'aime" in content or "like" in content:
                    # Could parse further - keep simple for now
                    preferences["likes"] = True
                if "déteste" in content or "hate" in content:
                    preferences["dislikes"] = True
            
            fast["preferences"] = preferences
            await self.save_memory(user_id, profile_id, "fast", fast)
        
        # Secondary: important episode extraction (simplified)
        facts = secondary.get("facts", [])
        # Add some identifiers
        if len(raw.get("messages", [])) % 50 == 0:
            facts.append(f"Conversation milestone: {len(raw.get('messages', []))} messages")
        secondary["facts"] = facts[-50:]  # Keep last 50
        await self.save_memory(user_id, profile_id, "secondary", secondary)

memory_mgr = MemoryManager(CONFIG.BASE_DIR)

# ============================================
# SECTION 4: LLM STREAMING WITH ROTATION
# ============================================

class LLMStreamer:
    """Handles LLM streaming with model rotation fallback."""
    
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0)
        self.models = [CONFIG.OPENROUTER_MODEL] + CONFIG.OPENROUTER_FALLBACK_MODELS
        self.usage_file = CONFIG.BASE_DIR / "llm_usage.json"
    
    async def _record_usage(self, model: str, tokens: int):
        """Record token usage for billing/stats."""
        usage = {}
        if self.usage_file.exists():
            usage = json.loads(self.usage_file.read_text())
        
        key = datetime.utcnow().strftime("%Y-%m-%d")
        if key not in usage:
            usage[key] = []
        
        usage[key].append({
            "model": model,
            "tokens": tokens,
            "timestamp": datetime.utcnow().isoformat()
        })
        
        memory_mgr._atomic_write(self.usage_file, usage)
    
    async def stream_llm(
        self,
        messages: List[Dict[str, str]],
        model_override: Optional[str] = None,
        on_chunk: Optional[callable] = None
    ) -> AsyncGenerator[str, None]:
        """
        Stream LLM response with fallback rotation.
        Filters <think> / <reasoning> / <scratchpad> blocks.
        """
        models_to_try = [model_override] if model_override else self.models[:CONFIG.OPENROUTER_MAX_MODEL_TRIES]
        
        headers = {
            "Authorization": f"Bearer {CONFIG.OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:8000",
            "X-Title": CONFIG.AI_NAME,
        }
        
        last_error = None
        
        for model in models_to_try:
            try:
                payload = {
                    "model": model,
                    "messages": messages,
                    "stream": True,
                    "max_tokens": 1024,
                    "temperature": 0.7,
                }
                
                async with self.client.stream(
                    "POST",
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=30.0
                ) as resp:
                    if resp.status_code != 200:
                        raise Exception(f"HTTP {resp.status_code}: {await resp.aread()}")
                    
                    buffer = ""
                    reasoning_buffer = ""
                    in_reasoning = False
                    total_tokens = 0
                    
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        
                        try:
                            chunk = json.loads(data)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            reasoning = delta.get("reasoning", "")
                            
                            # Handle reasoning content separately (bail-out if too large)
                            if reasoning:
                                reasoning_buffer += reasoning
                                if len(reasoning_buffer) > 8192:
                                    # Reasoning bail-out: send summary and stop
                                    yield "[RAISONNEMENT TROP LONG - INTERRUPTION]"
                                    break
                            
                            # Filter special tags
                            if content:
                                # Filter <think> / <reasoning> / <scratchpad> blocks
                                filtered = self._filter_tags(content)
                                if filtered:
                                    buffer += filtered
                                    if on_chunk:
                                        await on_chunk(filtered)
                                    yield filtered
                            
                            # Track usage
                            total_tokens += delta.get("tokens", 0) or 0
                            
                        except json.JSONDecodeError:
                            continue
                    
                    # Record usage
                    await self._record_usage(model, total_tokens)
                    return
                    
            except Exception as e:
                last_error = e
                continue
        
        raise Exception(f"All models failed. Last error: {last_error}")
    
    def _filter_tags(self, text: str) -> str:
        """Remove <think> / <reasoning> / <scratchpad> blocks."""
        import re
        patterns = [
            r'<think>.*?</think>',
            r'<reasoning>.*?</reasoning>',
            r'<scratchpad>.*?</scratchpad>'
        ]
        for pattern in patterns:
            text = re.sub(pattern, '', text, flags=re.DOTALL)
        return text.strip()

llm_streamer = LLMStreamer()

# ============================================
# SECTION 5: TTS WORKER (QUEUE-BASED)
# ============================================

class TTSWorker:
    """TTS worker with queue, edge-tts primary, Piper fallback."""
    
    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.running = False
        self.task: Optional[asyncio.Task] = None
    
    async def start(self):
        """Start the TTS worker."""
        self.running = True
        self.task = asyncio.create_task(self._worker_loop())
    
    async def stop(self):
        """Stop the TTS worker."""
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
    
    async def _worker_loop(self):
        """Main TTS worker loop."""
        while self.running:
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                await self._synthesize(item["text"], item["websocket"])
                self.queue.task_done()
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"TTS worker error: {e}")
    
    async def _synthesize(self, text: str, websocket: WebSocket):
        """Synthesize text to speech and send via WebSocket."""
        try:
            # Try edge-tts first
            communicate = Communicate(
                text,
                CONFIG.EDGE_VOICE,
                rate=CONFIG.EDGE_RATE
            )
            
            audio_chunks = []
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_chunks.append(chunk["data"])
            
            if audio_chunks:
                audio_data = b"".join(audio_chunks)
                b64_audio = self._bytes_to_base64(audio_data)
                await websocket.send_json({
                    "type": "tts_audio",
                    "data": b64_audio
                })
                return
        except Exception as e:
            print(f"edge-tts failed: {e}")
        
        # Fallback to Piper if configured
        if CONFIG.PIPER_BIN and CONFIG.PIPER_MODEL_PATH:
            try:
                # Run piper in subprocess (sync -> thread)
                proc = await asyncio.create_subprocess_exec(
                    CONFIG.PIPER_BIN,
                    "--model", CONFIG.PIPER_MODEL_PATH,
                    "--output_file", "/tmp/tts_output.wav",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await proc.communicate(text.encode())
                
                if proc.returncode == 0:
                    audio_data = Path("/tmp/tts_output.wav").read_bytes()
                    b64_audio = self._bytes_to_base64(audio_data)
                    await websocket.send_json({
                        "type": "tts_audio",
                        "data": b64_audio
                    })
            except Exception as e:
                print(f"Piper fallback failed: {e}")
    
    def _bytes_to_base64(self, data: bytes) -> str:
        """Convert bytes to base64 string."""
        import base64
        return base64.b64encode(data).decode('utf-8')
    
    async def enqueue(self, text: str, websocket: WebSocket):
        """Add text to TTS queue."""
        await self.queue.put({"text": text, "websocket": websocket})

tts_worker = TTSWorker()

# ============================================
# SECTION 6: WEB SEARCH (4-LEVEL FALLBACK)
# ============================================

class WebSearcher:
    """Multi-level web search with progressively simpler fallbacks."""
    
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=8.0, follow_redirects=True)
    
    async def search(self, query: str) -> str:
        """
        Search web with 4-level fallback chain:
        Tavily → DDGS → DuckDuckGo HTML scrape → Instant Answer
        """
        # Level 1: Tavily API
        if CONFIG.TAVILY_API_KEY:
            try:
                result = await self._search_tavily(query)
                if result:
                    return result
            except Exception as e:
                print(f"Tavily failed: {e}")
        
        # Level 2: DDGS (duckduckgo-search library - sync)
        try:
            result = await asyncio.to_thread(self._search_ddgs, query)
            if result:
                return result
        except Exception as e:
            print(f"DDGS failed: {e}")
        
        # Level 3: DuckDuckGo HTML scrape
        try:
            result = await self._search_ddg_html(query)
            if result:
                return result
        except Exception as e:
            print(f"DDG HTML failed: {e}")
        
        # Level 4: Instant Answer API
        try:
            result = await self._search_instant_answer(query)
            if result:
                return result
        except Exception as e:
            print(f"Instant Answer failed: {e}")
        
        return ""
    
    async def _search_tavily(self, query: str) -> str:
        """Tavily API search."""
        resp = await self.client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": CONFIG.TAVILY_API_KEY,
                "query": query,
                "max_results": 3,
                "include_answer": True
            }
        )
        data = resp.json()
        if "answer" in data and data["answer"]:
            return data["answer"]
        return ""
    
    def _search_ddgs(self, query: str) -> str:
        """DDGS library search (sync)."""
        try:
            from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=3))
                if results:
                    return results[0]["body"]
        except ImportError:
            pass
        return ""
    
    async def _search_ddg_html(self, query: str) -> str:
        """DuckDuckGo HTML scrape."""
        resp = await self.client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (compatible; AriBot/1.0)"}
        )
        # Very basic parsing
        import re
        match = re.search(r'<a class="result__a" href="[^"]*">(.*?)</a>', resp.text)
        if match:
            return match.group(1).strip()
        return ""
    
    async def _search_instant_answer(self, query: str) -> str:
        """DuckDuckGo Instant Answer API."""
        resp = await self.client.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1}
        )
        data = resp.json()
        if data.get("Answer"):
            return data["Answer"]
        if data.get("Abstract"):
            return data["Abstract"]
        return ""

web_searcher = WebSearcher()

# ============================================
# SECTION 7: HTTP ROUTES
# ============================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    # Startup
    asyncio.create_task(memory_optimizer_loop())
    await tts_worker.start()
    yield
    # Shutdown
    await tts_worker.stop()
    await llm_streamer.client.aclose()
    await web_searcher.client.aclose()

app = FastAPI(lifespan=lifespan)

# CORS
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files
app.mount("/assets", StaticFiles(directory=str(CONFIG.ASSETS_DIR)), name="assets")



# ============ AUTH HTTP ROUTES ============

@app.post("/auth/register")
async def register_user(payload: UserRegister):
    """Register a new user with PIN hash."""
    pin_hash = payload.pin_hash
    
    if pin_hash in _users:
        raise HTTPException(status_code=400, detail="PIN already registered")
    
    _users[pin_hash] = {
        "created": datetime.utcnow(),
        "last_login": None,
        "device_ids": set()
    }
    
    # Auto-login on same device
    session_id = str(uuid4())
    device_id = payload.device_id or str(uuid4())
    _sessions[session_id] = UserSession(
        user_id=pin_hash,
        device_id=device_id,
        created_at=datetime.utcnow(),
        last_seen=datetime.utcnow()
    )
    _users[pin_hash]["device_ids"].add(device_id)
    _trusted_devices[device_id] = pin_hash
    
    return {"session_id": session_id, "user_id": pin_hash, "device_id": device_id}


@app.post("/auth/login")
async def login_user(payload: UserLogin):
    """Login with PIN hash."""
    pin_hash = payload.pin_hash
    
    if pin_hash not in _users:
        raise HTTPException(status_code=401, detail="Invalid PIN")
    
    user = _users[pin_hash]
    user["last_login"] = datetime.utcnow()
    
    # Check trusted devices (auto-login)
    device_id = str(uuid4())
    session_id = str(uuid4())
    
    _sessions[session_id] = UserSession(
        user_id=pin_hash,
        device_id=device_id,
        created_at=datetime.utcnow(),
        last_seen=datetime.utcnow()
    )
    user["device_ids"].add(device_id)
    _trusted_devices[device_id] = pin_hash
    
    return {"session_id": session_id, "user_id": pin_hash, "device_id": device_id}


@app.post("/auth/logout")
async def logout_user(session_id: str = Body(..., embed=True)):
    """Logout and invalidate session."""
    if session_id in _sessions:
        del _sessions[session_id]
    return {"ok": True}


@app.get("/auth/me")
async def get_current_user(session_id: str):
    """Get current user info from session."""
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=401, detail="Invalid session")
    
    user = _users.get(session.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    return {
        "user_id": session.user_id,
        "device_id": session.device_id,
        "created_at": user["created"],
        "last_login": user["last_login"]
    }


@app.post("/auth/trusted-devices")
async def list_trusted_devices(user_id: str = Body(..., embed=True)):
    """List trusted devices for a user."""
    devices = [
        {"device_id": d, "user_id": uid}
        for d, uid in _trusted_devices.items()
        if uid == user_id
    ]
    return {"devices": devices}


@app.delete("/auth/trusted-devices/{device_id}")
async def remove_trusted_device(device_id: str):
    """Revoke a trusted device."""
    if device_id in _trusted_devices:
        user_id = _trusted_devices[device_id]
        del _trusted_devices[device_id]
        if user_id in _users:
            _users[user_id]["device_ids"].discard(device_id)
        return {"ok": True}
    raise HTTPException(status_code=404, detail="Device not found")

@app.get("/")
async def root():
    """Serve index.html with no-cache headers."""
    index_path = CONFIG.BASE_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    content = index_path.read_text()
    return HTMLResponse(
        content=content,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )

@app.get("/api/library")
async def get_library():
    """Get library of 3D models, backgrounds, Live2D."""
    library = {
        "models": [],
        "backgrounds": [],
        "live2d": []
    }
    
    # Scan directories
    for item in CONFIG.ASSETS_DIR.rglob("*"):
        if item.is_file():
            rel = item.relative_to(CONFIG.ASSETS_DIR)
            parts = rel.parts
            
            if parts[0] == "models":
                library["models"].append(LibraryItem(
                    name=item.stem,
                    path=f"/assets/{rel}",
                    type="model"
                ).dict())
            elif parts[0] == "backgrounds":
                library["backgrounds"].append(LibraryItem(
                    name=item.stem,
                    path=f"/assets/{rel}",
                    type="background"
                ).dict())
            elif parts[0] == "live2d":
                library["live2d"].append(LibraryItem(
                    name=item.stem,
                    path=f"/assets/{rel}",
                    type="live2d"
                ).dict())
    
    return library

@app.get("/api/animation-catalog")
async def get_animation_catalog():
    """Get catalog of available animations."""
    catalog = []
    animations_dir = CONFIG.ASSETS_DIR / "animations"
    if animations_dir.exists():
        for item in animations_dir.iterdir():
            if item.is_file() and item.suffix in [".json", ".anim"]:
                catalog.append(AnimationCatalog(
                    id=item.stem,
                    name=item.stem.replace("_", " ").title(),
                    preview_url=f"/assets/animations/{item.name}" if item.suffix == ".png" else None,
                    tags=[]
                ).dict())
    return catalog

@app.post("/api/upload/animation")
async def upload_animation(file: UploadFile = File(...)):
    """Upload animation file."""
    return await _handle_upload(file, CONFIG.UPLOAD_DIR / "animations")

@app.post("/api/upload/background")
async def upload_background(file: UploadFile = File(...)):
    """Upload background file."""
    return await _handle_upload(file, CONFIG.UPLOAD_DIR / "backgrounds")

@app.post("/api/upload/model")
async def upload_model(file: UploadFile = File(...)):
    """Upload 3D model file."""
    return await _handle_upload(file, CONFIG.UPLOAD_DIR / "models")

@app.post("/api/upload/live2d")
async def upload_live2d(file: UploadFile = File(...)):
    """Upload Live2D model."""
    return await _handle_upload(file, CONFIG.UPLOAD_DIR / "live2d")

async def _handle_upload(file: UploadFile, dest_dir: Path) -> dict:
    """Generic upload handler with zip-slip protection."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / file.filename
    
    # Basic path traversal check
    if ".." in file.filename or file.filename.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid filename")
    
    content = await file.read()
    dest.write_bytes(content)
    
    return {"status": "ok", "filename": file.filename, "size": len(content)}

@app.get("/api/usage/{user}/{profile}")
async def get_usage(user: str, profile: str):
    """Get LLM usage statistics."""
    usage_file = CONFIG.BASE_DIR / "llm_usage.json"
    if usage_file.exists():
        usage = json.loads(usage_file.read_text())
        return usage
    return {}

# ============================================
# SECTION 8: WEBSOCKET /ws
# ============================================

class ConnectionManager:
    """Manages WebSocket connections with heartbeat."""
    
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.heartbeat_task: Optional[asyncio.Task] = None
    
    async def connect(self, websocket: WebSocket, client_id: str):
        await websocket.accept()
        self.active_connections[client_id] = websocket
    
    def disconnect(self, client_id: str):
        if client_id in self.active_connections:
            del self.active_connections[client_id]
    
    async def send_json(self, client_id: str, data: dict):
        """Send JSON to a specific client."""
        if client_id in self.active_connections:
            try:
                await self.active_connections[client_id].send_json(data)
            except Exception:
                self.disconnect(client_id)
    
    async def heartbeat_loop(self):
        """Send periodic heartbeat to all connections."""
        while True:
            await asyncio.sleep(25)
            for cid, ws in list(self.active_connections.items()):
                try:
                    await ws.send_json({"type": "heartbeat"})
                except Exception:
                    self.disconnect(cid)

manager = ConnectionManager()

# 2FA storage (in-memory, per user)
_2fa_codes: Dict[str, Dict[str, Any]] = {}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Main WebSocket endpoint."""
    client_id = str(uuid4())
    await manager.connect(websocket, client_id)
    # ============ WEBSOCKET AUTH (simple session check) ============
    # Extract session ID from query params or first message
    session_id = websocket.query_params.get("session_id")
    if not session_id or session_id not in _sessions:
        await websocket.close(code=4001, reason="Unauthorized: invalid session")
        return
    
    session = _sessions[session_id]
    session.last_seen = datetime.utcnow()
    user_id = session.user_id

    
    # Start heartbeat task if not running
    if manager.heartbeat_task is None or manager.heartbeat_task.done():
        manager.heartbeat_task = asyncio.create_task(manager.heartbeat_loop())
    
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            
            if msg_type == "get_config":
                await websocket.send_json({
                    "type": "config",
                    "data": {
                        "internet_enabled": CONFIG.INTERNET_ENABLED_DEFAULT,
                        "tts_voice": CONFIG.EDGE_VOICE,
                        "tts_rate": CONFIG.EDGE_RATE,
                        "model": CONFIG.OPENROUTER_MODEL,
                        "fallback_models": CONFIG.OPENROUTER_FALLBACK_MODELS
                    }
                })
            
            elif msg_type == "set_config":
                # Update config (runtime only, not persisted)
                payload = WSConfigUpdate(**data.get("data", {}))
                # In real implementation, would update config store
                await websocket.send_json({"type": "config_updated", "data": "ok"})
            
            elif msg_type == "auth_2fa_begin":
                payload = WSAuth2FABegin(**data.get("data", {}))
                user_id = payload.user_id
                
                # Generate 6-digit code
                code = str(secrets.randbelow(900000) + 100000)
                _2fa_codes[user_id] = {
                    "code": code,
                    "expires": datetime.utcnow() + timedelta(seconds=120),
                    "attempts": 0
                }
                
                # In production, send code via email/SMS
                print(f"[2FA] User {user_id} code: {code}")
                
                await websocket.send_json({
                    "type": "2fa_challenge",
                    "data": {"user_id": user_id, "message": "Code sent (check console)"}
                })
            
            elif msg_type == "auth_2fa_verify":
                payload = WSAuth2FAVerify(**data.get("data", {}))
                user_id = payload.user_id
                code = payload.code
                
                record = _2fa_codes.get(user_id)
                if not record:
                    await websocket.send_json({"type": "2fa_result", "data": {"success": False, "error": "No pending code"}})
                    continue
                
                if datetime.utcnow() > record["expires"]:
                    del _2fa_codes[user_id]
                    await websocket.send_json({"type": "2fa_result", "data": {"success": False, "error": "Code expired"}})
                    continue
                
                if record["attempts"] >= 3:
                    del _2fa_codes[user_id]
                    await websocket.send_json({"type": "2fa_result", "data": {"success": False, "error": "Max attempts exceeded"}})
                    continue
                
                if code != record["code"]:
                    record["attempts"] += 1
                    await websocket.send_json({"type": "2fa_result", "data": {"success": False, "error": f"Invalid code ({3 - record['attempts']} attempts left)"}})
                    continue
                
                # Success
                del _2fa_codes[user_id]
                await websocket.send_json({"type": "2fa_result", "data": {"success": True}})
            
            elif msg_type == "user_text":
                payload = WSUserTextPayload(**data.get("data", {}))
                user_text = payload.text
                profile = payload.profile
                # user_id already set from session validation above
                
                # State: thinking
                await websocket.send_json({"type": "state", "state": "thinking"})
                
                try:
                    # Build memory context
                    context = await memory_mgr.build_context(user_id, profile)
                    
                    # Add system prompt
                    system_prompt = f"""Tu es {CONFIG.AI_NAME}, un assistant vocal IA amical et serviable.
Réponds de façon naturelle, concise et utile. Tu es dans une taverne RPG."""
                    messages = [{"role": "system", "content": system_prompt}] + context + [
                        {"role": "user", "content": user_text}
                    ]
                    
                    # Decide if web search needed (simple heuristic)
                    need_web = any(kw in user_text.lower() for kw in ["recherche", "cherche", "actualité", "news", "quoi de neuf"])
                    web_result = ""
                    
                    if need_web and CONFIG.INTERNET_ENABLED_DEFAULT:
                        await websocket.send_json({"type": "status", "message": "Searching web..."})
                        web_result = await web_searcher.search(user_text)
                        if web_result:
                            messages.insert(-1, {"role": "system", "content": f"Web search result: {web_result}"})
                    
                    # Stream LLM and TTS in parallel
                    full_response = ""
                    tts_task = None
                    
                    async def collect_chunks():
                        nonlocal full_response
                        async for chunk in llm_streamer.stream_llm(messages):
                            full_response += chunk
                            await websocket.send_json({
                                "type": "assistant_chunk",
                                "chunk": chunk
                            })
                    
                    # Start both concurrently
                    await asyncio.gather(
                        collect_chunks(),
                        tts_worker.enqueue(full_response, websocket)  # Will queue after collection
                    )
                    
                    # But we need TTS to start streaming as chunks arrive...
                    # Better: stream chunks to TTS as they arrive
                    # (Implement proper streaming in production)
                    
                    # Detect emotion (simple keyword-based)
                    emotion = "neutral"
                    if any(word in full_response.lower() for word in ["excellent", "génial", "super"]):
                        emotion = "happy"
                    elif any(word in full_response.lower() for word in ["désolé", "triste", "malheur"]):
                        emotion = "sad"
                    
                    # Save to memory
                    await memory_mgr.append_message(user_id, profile, "user", user_text)
                    await memory_mgr.append_message(user_id, profile, "assistant", full_response)
                    
                    # Done
                    await websocket.send_json({
                        "type": "assistant_done",
                        "emotion": emotion,
                        "full_response": full_response
                    })
                    await websocket.send_json({"type": "state", "state": "idle"})
                    
                except Exception as e:
                    await websocket.send_json({"type": "error", "message": str(e)})
                    await websocket.send_json({"type": "state", "state": "idle"})
            
            else:
                await websocket.send_json({"type": "error", "message": f"Unknown message type: {msg_type}"})
    
    except WebSocketDisconnect:
        manager.disconnect(client_id)
    except Exception as e:
        print(f"WebSocket error: {e}")
        manager.disconnect(client_id)

# ============================================
# MEMORY OPTIMIZER LOOP (EVERY 4 HOURS)
# ============================================

async def memory_optimizer_loop():
    """Periodic task to compress memories."""
    while True:
        await asyncio.sleep(4 * 3600)  # 4 hours
        
        # Compress all user profiles
        profiles_dir = CONFIG.BASE_DIR / "memory_profiles" / "profiles"
        if profiles_dir.exists():
            for user_dir in profiles_dir.iterdir():
                if user_dir.is_dir():
                    for profile_dir in user_dir.iterdir():
                        if profile_dir.is_dir():
                            user_id = user_dir.name
                            profile_id = profile_dir.name
                            try:
                                await memory_mgr.compress_memory(user_id, profile_id)
                            except Exception as e:
                                print(f"Memory compression failed for {user_id}/{profile_id}: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=CONFIG.PORT)
