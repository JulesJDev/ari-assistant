import os
import json
import asyncio
import secrets
import base64
import subprocess
import re
import io
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Literal, Dict, Any, List, AsyncGenerator
from collections import defaultdict

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

# ============================================================================
# CONFIGURATION
# ============================================================================

class Config:
    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
    OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemma-3-27b-it:free")
    OPENROUTER_FALLBACK_MODELS = os.getenv("OPENROUTER_FALLBACK_MODELS", "meta-llama/llama-3.2-3b-instruct:free,mistralai/mistral-7b-instruct:free")
    OPENROUTER_MAX_MODEL_TRIES = int(os.getenv("OPENROUTER_MAX_MODEL_TRIES", "6"))
    EDGE_VOICE = os.getenv("EDGE_VOICE", "fr-FR-DeniseNeural")
    EDGE_RATE = os.getenv("EDGE_RATE", "+25%")
    EDGE_PITCH = os.getenv("EDGE_PITCH", "+0Hz")
    EDGE_VOLUME = os.getenv("EDGE_VOLUME", "+0%")
    EDGE_TTS_TIMEOUT = int(os.getenv("EDGE_TTS_TIMEOUT", "18"))
    MEMORY_MAX_TURNS = int(os.getenv("MEMORY_MAX_TURNS", "80"))
    MEMORY_PROMPT_TURNS = int(os.getenv("MEMORY_PROMPT_TURNS", "6"))
    MEMORY_OPTIMIZE_INTERVAL_SEC = int(os.getenv("MEMORY_OPTIMIZE_INTERVAL_SEC", "14400"))
    DEFAULT_USER_ID = os.getenv("DEFAULT_USER_ID", "default")
    DEFAULT_PROFILE_ID = os.getenv("DEFAULT_PROFILE_ID", "default")
    AI_NAME = os.getenv("AI_NAME", "Ari")
    WEB_SEARCH_PROVIDER = os.getenv("WEB_SEARCH_PROVIDER", "auto")
    TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
    PIPER_BIN = os.getenv("PIPER_BIN", "")
    PIPER_MODEL_PATH = os.getenv("PIPER_MODEL_PATH", "")
    REASONING_BAIL_MAX_CHARS = int(os.getenv("REASONING_BAIL_MAX_CHARS", "8192"))
    INTERNET_ENABLED_DEFAULT = os.getenv("INTERNET_ENABLED_DEFAULT", "true").lower() == "true"
    PORT = int(os.getenv("PORT", "8000"))
    HTTP_REFERER = os.getenv("HTTP_REFERER", "http://localhost:8000")
    APP_TITLE = os.getenv("APP_TITLE", "Ari Assistant")

CONFIG = Config()

# ============================================================================
# ATOMIC WRITE HELPER
# ============================================================================

def atomic_write_json(path: Path, data: dict) -> None:
    """Atomically write JSON data to disk using tmp + rename."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)

# ============================================================================
# PYDANTIC MODELS
# ============================================================================

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

class WsMsgUserText(BaseModel):
    type: Literal["user_text"]
    text: str
    silent: bool = False
    profile: str = "default"
    user_id: Optional[str] = None

class WsAuth2FABegin(BaseModel):
    type: Literal["auth_2fa_begin"]
    user_id: str

class WsAuth2FAVerify(BaseModel):
    type: Literal["auth_2fa_verify"]
    user_id: str
    code: str

class MemoryTurn(BaseModel):
    ts: str
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
    type: str

class AnimationCatalog(BaseModel):
    id: str
    name: str
    preview_url: Optional[str] = None
    tags: list[str] = []

# ============================================================================
# MEMORY MANAGER
# ============================================================================

class MemoryManager:
    def __init__(self, base_path: Path = Path("memory_profiles")):
        self.base_path = base_path
        self.base_path.mkdir(exist_ok=True)
        self._memory_cache: dict[str, MemoryFile] = {}
        self._fast_cache: dict[str, MemoryFastFile] = {}
        self._secondary_cache: dict[str, MemorySecondaryFile] = {}

    def _profile_path(self, user_id: str, profile_id: str, layer: str) -> Path:
        return self.base_path / user_id / profile_id / f"memory_{layer}.json"

    def load_memory(self, user_id: str, profile_id: str, layer: str) -> dict:
        key = f"{user_id}/{profile_id}/{layer}"
        cache = {
            "raw": self._memory_cache,
            "fast": self._fast_cache,
            "secondary": self._secondary_cache
        }.get(layer)

        if cache is not None and key in cache:
            return cache[key].dict()

        path = self._profile_path(user_id, profile_id, layer)
        if not path.exists():
            return {"turns": [], "summary": ""} if layer == "raw" else {"items": [], "meta": {}}

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if layer == "raw":
                obj = MemoryFile(**data)
                self._memory_cache[key] = obj
            elif layer == "fast":
                obj = MemoryFastFile(**data)
                self._fast_cache[key] = obj
            elif layer == "secondary":
                obj = MemorySecondaryFile(**data)
                self._secondary_cache[key] = obj
            return obj.dict()
        except Exception as e:
            print(f"Memory load error {path}: {e}")
            return {"turns": [], "summary": ""} if layer == "raw" else {"items": [], "meta": {}}

    def save_memory(self, user_id: str, profile_id: str, layer: str, data: dict) -> None:
        key = f"{user_id}/{profile_id}/{layer}"
        path = self._profile_path(user_id, profile_id, layer)
        path.parent.mkdir(parents=True, exist_ok=True)

        try:
            if layer == "raw":
                obj = MemoryFile(**data)
                self._memory_cache[key] = obj
            elif layer == "fast":
                obj = MemoryFastFile(**data)
                self._fast_cache[key] = obj
            elif layer == "secondary":
                obj = MemorySecondaryFile(**data)
                self._secondary_cache[key] = obj

            atomic_write_json(path, obj.dict())
        except Exception as e:
            print(f"Memory save error {path}: {e}")

    def append_message(self, user_id: str, profile_id: str, role: str, content: str) -> None:
        raw = self.load_memory(user_id, profile_id, "raw")
        turn = MemoryTurn(
            ts=datetime.utcnow().isoformat() + "Z",
            user=role if role == "user" else "",
            assistant=content if role == "assistant" else ""
        )
        if role == "user":
            raw.setdefault("turns", []).append({"ts": turn.ts, "user": content, "assistant": ""})
        else:
            if raw["turns"]:
                raw["turns"][-1]["assistant"] = content
            else:
                raw["turns"].append({"ts": turn.ts, "user": "", "assistant": content})

        if len(raw["turns"]) > CONFIG.MEMORY_MAX_TURNS:
            raw["turns"] = raw["turns"][-CONFIG.MEMORY_MAX_TURNS:]

        self.save_memory(user_id, profile_id, "raw", raw)

    def build_context(self, user_id: str, profile_id: str, max_tokens: int = 2000) -> List[Dict[str, str]]:
        """Build LLM context from memory layers."""
        config_path = self._profile_path(user_id, profile_id, "config")
        system_prompt = CONFIG.AI_NAME + " : tu es une assistante vocale francophone, tavernière RPG."
        if config_path.exists():
            try:
                cfg = json.loads(config_path.read_text(encoding="utf-8"))
                system_prompt = cfg.get("system_prompt", system_prompt)
            except:
                pass

        context = [{"role": "system", "content": system_prompt}]

        fast = self.load_memory(user_id, profile_id, "fast")
        if fast["items"]:
            context.append({"role": "system", "content": "Préférences utilisateur :\n" + "\n".join(f"• {item}" for item in fast["items"][:5])})

        secondary = self.load_memory(user_id, profile_id, "secondary")
        if secondary["items"]:
            context.append({"role": "system", "content": "Faits notables :\n" + "\n".join(f"• {item}" for item in secondary["items"][-5:])})

        raw = self.load_memory(user_id, profile_id, "raw")
        recent_turns = raw.get("turns", [])[-CONFIG.MEMORY_PROMPT_TURNS*2:]
        for turn in recent_turns:
            if turn.get("user"):
                context.append({"role": "user", "content": turn["user"]})
            if turn.get("assistant"):
                context.append({"role": "assistant", "content": turn["assistant"]})

        return context

    def compress_memory(self, user_id: str, profile_id: str) -> None:
        """Heuristic compression: extract preferences and episodic facts."""
        raw = self.load_memory(user_id, profile_id, "raw")
        fast = self.load_memory(user_id, profile_id, "fast")
        secondary = self.load_memory(user_id, profile_id, "secondary")

        likes = fast.get("items", [])
        dislikes = []
        facts = secondary.get("items", [])

        for turn in raw.get("turns", []):
            user_text = turn.get("user", "").lower()
            assistant_text = turn.get("assistant", "")

            if any(word in user_text for word in ["j'aime", "j'adore", "aime bien"]):
                likes.append(user_text[:100])
            if any(word in user_text for word in ["je déteste", "je n'aime pas"]):
                dislikes.append(user_text[:100])

            if any(word in assistant_text.lower() for word in ["souviens-toi", "important", "retenir"]):
                facts.append(assistant_text[:150])

        fast["items"] = list(set(likes + dislikes))[-20:]
        secondary["items"] = list(set(facts))[-20:]

        self.save_memory(user_id, profile_id, "fast", fast)
        self.save_memory(user_id, profile_id, "secondary", secondary)

memory_mgr = MemoryManager()

# ============================================================================
# LLM STREAMER
# ============================================================================

class LLMStreamer:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0)
        self.models = [CONFIG.OPENROUTER_MODEL] + CONFIG.OPENROUTER_FALLBACK_MODELS.split(",")
        self.usage_path = Path("memory_profiles") / "llm_usage.json"
        self.usage_path.parent.mkdir(exist_ok=True)

    def _filter_tags(self, text: str) -> str:
        """Remove reasoning/analysis tags."""
        text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<reasoning>.*?</reasoning>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<scratchpad>.*?</scratchpad>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<analysis>.*?</analysis>', '', text, flags=re.DOTALL | re.IGNORECASE)
        return text.strip()

    async def _record_usage(self, model: str, prompt_tokens: int, completion_tokens: int) -> None:
        """Record usage statistics atomically."""
        try:
            if self.usage_path.exists():
                data = json.loads(self.usage_path.read_text(encoding="utf-8"))
            else:
                data = {"version": 1, "chat": {"free": {}, "paid": {}}}

            today = datetime.utcnow().strftime("%Y-%m-%d")
            bucket_type = "free" if ":free" in model else "paid"

            if today not in data["chat"][bucket_type]:
                data["chat"][bucket_type][today] = {
                    "requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "estimated_usd": 0.0
                }

            data["chat"][bucket_type][today]["requests"] += 1
            data["chat"][bucket_type][today]["prompt_tokens"] += prompt_tokens
            data["chat"][bucket_type][today]["completion_tokens"] += completion_tokens

            atomic_write_json(self.usage_path, data)
        except Exception as e:
            print(f"Usage record error: {e}")

    async def stream_llm(self, messages: List[Dict[str, str]], model_override: Optional[str] = None,
                        on_chunk: Optional[callable] = None) -> AsyncGenerator[str, None]:
        """Stream LLM response with fallback models and reasoning bail-out."""
        models = [model_override] if model_override else self.models
        last_error = None

        for model in models[:CONFIG.OPENROUTER_MAX_MODEL_TRIES]:
            try:
                headers = {
                    "Authorization": f"Bearer {CONFIG.OPENROUTER_API_KEY[:8]}***",
                    "Content-Type": "application/json",
                    "HTTP-Referer": CONFIG.HTTP_REFERER,
                    "X-Title": CONFIG.APP_TITLE,
                }
                payload = {
                    "model": model,
                    "messages": messages,
                    "stream": True,
                    "max_tokens": 1024,
                    "temperature": 0.7,
                }

                async with self.client.stream("POST", "https://openrouter.ai/api/v1/chat/completions",
                                            headers=headers, json=payload, timeout=30.0) as resp:
                    if resp.status_code != 200:
                        last_error = f"HTTP {resp.status_code}"
                        continue

                    reasoning_buffer = []
                    in_reasoning = False
                    full_response = ""

                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:].strip()
                        if data == "[DONE]":
                            break

                        try:
                            chunk = json.loads(data)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})

                            content = delta.get("content", "")
                            reasoning = delta.get("reasoning", "")

                            if reasoning:
                                reasoning_buffer.append(reasoning)
                                combined_reasoning = "".join(reasoning_buffer)
                                if len(combined_reasoning) > CONFIG.REASONING_BAIL_MAX_CHARS:
                                    yield "[RAISONNEMENT TROP LONG - INTERRUPTION]"
                                    reasoning_buffer = []
                                    in_reasoning = False
                                    break

                            if content:
                                filtered = self._filter_tags(content)
                                if filtered:
                                    full_response += filtered
                                    if on_chunk:
                                        await on_chunk(filtered)
                                    yield filtered

                        except json.JSONDecodeError:
                            continue

                prompt_tokens = resp.headers.get("X-Openrouter-Prompt-Tokens", "0")
                completion_tokens = resp.headers.get("X-Openrouter-Completion-Tokens", "0")
                await self._record_usage(model, int(prompt_tokens), int(completion_tokens))
                return

            except Exception as e:
                last_error = str(e)
                print(f"LLM error with model {model}: {e}")
                continue

        raise Exception(f"All models failed. Last error: {last_error}")

llm_streamer = LLMStreamer()

# ============================================================================
# TTS WORKER
# ============================================================================

class TTSWorker:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.running = False
        self.task = None

    async def start(self):
        self.running = True
        self.task = asyncio.create_task(self._worker_loop())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def enqueue(self, text: str, websocket: WebSocket):
        await self.queue.put({"text": text, "websocket": websocket})

    def split_into_tts_segments(self, text: str) -> List[str]:
        """Segment text for TTS, avoiding numeric/abbreviation splits."""
        if not text:
            return []

        sentences = re.split(r'(?<=[.!?;])\s+', text.strip())
        if len(sentences) <= 1:
            return [text] if text else []

        segments = []
        current = sentences[0]

        for sentence in sentences[1:]:
            if len(current) + len(sentence) < 100:
                current += " " + sentence
            else:
                if current:
                    segments.append(current.strip())
                current = sentence

        if current:
            segments.append(current.strip())

        result = []
        for seg in segments:
            if len(seg) <= 250:
                result.append(seg)
            else:
                commas = [m.end() for m in re.finditer(r',\s+(?=[A-ZÀ-Ú])', seg)]
                if commas and commas[-1] > 50:
                    last_good = commas[-1]
                    before = seg[:last_good-1].strip()
                    after = seg[last_good:].strip()
                    if before:
                        result.append(before)
                    if after:
                        result.append(after)
                else:
                    words = seg.split()
                    mid = len(words) // 2
                    result.append(" ".join(words[:mid]).strip())
                    result.append(" ".join(words[mid:]).strip())

        return [s for s in result if s]

    async def _synthesize(self, text: str, websocket: WebSocket) -> None:
        """Synthesize speech using Edge TTS with Piper fallback."""
        if not text.strip():
            return

        audio_data = None

        try:
            from edge_tts import Communicate
            communicate = Communicate(
                text,
                CONFIG.EDGE_VOICE,
                rate=CONFIG.EDGE_RATE,
                pitch=CONFIG.EDGE_PITCH,
                volume=CONFIG.EDGE_VOLUME
            )
            chunks = []
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    chunks.append(chunk["data"])
            if chunks:
                audio_data = b"".join(chunks)
        except Exception as e:
            print(f"Edge TTS error: {e}")

        if not audio_data and CONFIG.PIPER_BIN and CONFIG.PIPER_MODEL_PATH:
            try:
                proc = await asyncio.create_subprocess_exec(
                    CONFIG.PIPER_BIN, "--model", CONFIG.PIPER_MODEL_PATH,
                    "--output_file", "/tmp/tts_output.wav",
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                await proc.communicate(input=text.encode())
                if proc.returncode == 0:
                    audio_data = Path("/tmp/tts_output.wav").read_bytes()
            except Exception as e:
                print(f"Piper TTS error: {e}")

        if audio_data:
            try:
                b64 = base64.b64encode(audio_data).decode()
                await websocket.send_json({"type": "tts_audio", "data": b64})
            except Exception as e:
                print(f"TTS send error: {e}")

    async def _worker_loop(self):
        while self.running:
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                try:
                    await self._synthesize(item["text"], item["websocket"])
                finally:
                    self.queue.task_done()
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"TTS worker error: {e}")

tts_worker = TTSWorker()

# ============================================================================
# WEB SEARCHER (4-level fallback)
# ============================================================================

class WebSearcher:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=8.0)

    async def search(self, query: str) -> str:
        providers = []

        if CONFIG.WEB_SEARCH_PROVIDER == "tavily" or CONFIG.WEB_SEARCH_PROVIDER == "auto":
            if CONFIG.TAVILY_API_KEY:
                providers.append(self._search_tavily)

        if CONFIG.WEB_SEARCH_PROVIDER in ["ddgs", "auto"]:
            providers.append(self._search_ddgs)

        if CONFIG.WEB_SEARCH_PROVIDER in ["duckduckgo", "auto"]:
            providers.append(self._search_html_scrape)

        if CONFIG.WEB_SEARCH_PROVIDER in ["instant", "auto"]:
            providers.append(self._search_instant)

        for provider in providers:
            try:
                result = await provider(query)
                if result and result.strip():
                    return result.strip()[:2000]
            except Exception as e:
                print(f"Search provider error: {e}")
                continue

        return ""

    async def _search_tavily(self, query: str) -> str:
        if not CONFIG.TAVILY_API_KEY:
            return ""
        resp = await self.client.post(
            "https://api.tavily.com/search",
            json={"api_key": CONFIG.TAVILY_API_KEY, "query": query, "max_results": 3, "include_answer": True}
        )
        if resp.status_code == 200:
            return resp.json().get("answer", "")

    async def _search_ddgs(self, query: str) -> str:
        try:
            from duckduckgo_search import DDGS
            results = await asyncio.to_thread(lambda: list(DDGS().text(query, max_results=3)))
            if results:
                return results[0].get("body", "")
        except ImportError:
            print("duckduckgo-search not installed")
        return ""

    async def _search_html_scrape(self, query: str) -> str:
        try:
            resp = await self.client.get("https://html.duckduckgo.com/html/", params={"q": query}, timeout=5.0)
            if resp.status_code == 200:
                match = re.search(r'<a class="[^"]*?result__a[^"]*?"[^>]*?>(.*?)</a>', resp.text)
                if match:
                    return re.sub(r'<[^>]+>', '', match.group(1))[:500]
        except:
            pass
        return ""

    async def _search_instant(self, query: str) -> str:
        try:
            resp = await self.client.get("https://api.duckduckgo.com/", params={
                "q": query, "format": "json", "no_html": 1, "no_redirect": 1
            })
            if resp.status_code == 200:
                data = resp.json()
                return data.get("Answer") or data.get("Abstract", "")[:500]
        except:
            pass
        return ""

web_searcher = WebSearcher()

# ============================================================================
# 2FA MANAGER
# ============================================================================

class TwoFAManager:
    def __init__(self):
        self.codes: Dict[str, Dict[str, Any]] = {}

    def begin(self, user_id: str) -> str:
        code = str(secrets.randbelow(9000) + 1000)
        expires = datetime.utcnow() + timedelta(seconds=120)
        self.codes[user_id] = {"code": code, "expires": expires, "attempts": 0}
        print(f"[2FA] Code for {user_id}: {code}")
        return code

    def verify(self, user_id: str, code: str) -> bool:
        if user_id not in self.codes:
            return False

        entry = self.codes[user_id]
        if entry["attempts"] >= 3:
            del self.codes[user_id]
            return False

        if datetime.utcnow() > entry["expires"]:
            del self.codes[user_id]
            return False

        if code != entry["code"]:
            entry["attempts"] += 1
            if entry["attempts"] >= 3:
                del self.codes[user_id]
            return False

        del self.codes[user_id]
        return True

    def is_pending(self, user_id: str) -> bool:
        if user_id not in self.codes:
            return False
        entry = self.codes[user_id]
        if datetime.utcnow() > entry["expires"]:
            del self.codes[user_id]
            return False
        return True

twofa_mgr = TwoFAManager()

# ============================================================================
# CONNECTION MANAGER & SESSION STATE
# ============================================================================

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.heartbeat_interval = 25

    async def connect(self, client_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[client_id] = websocket
        asyncio.create_task(self._heartbeat(client_id))

    def disconnect(self, client_id: str):
        if client_id in self.active_connections:
            del self.active_connections[client_id]

    async def send_json(self, client_id: str, data: dict):
        ws = self.active_connections.get(client_id)
        if ws:
            try:
                await ws.send_json(data)
            except:
                pass

    async def _heartbeat(self, client_id: str):
        while client_id in self.active_connections:
            await asyncio.sleep(self.heartbeat_interval)
            try:
                await self.send_json(client_id, {"type": "heartbeat"})
            except:
                break

manager = ConnectionManager()

class SessionState:
    def __init__(self, client_id: str, user_id: str, profile_id: str):
        self.client_id = client_id
        self.user_id = user_id
        self.profile_id = profile_id
        self.authenticated = False
        self.config_path = memory_mgr._profile_path(user_id, profile_id, "config")
        self._load_config()

    def _load_config(self):
        default = {
            "ai_name": CONFIG.AI_NAME,
            "model": CONFIG.OPENROUTER_MODEL,
            "system_prompt": "",
            "edge_voice": CONFIG.EDGE_VOICE,
            "edge_rate": CONFIG.EDGE_RATE,
            "edge_pitch": CONFIG.EDGE_PITCH,
            "edge_volume": CONFIG.EDGE_VOLUME,
            "tts_engine": "auto",
            "temperature": 0.7,
            "max_tokens": 1024,
            "internet_enabled": CONFIG.INTERNET_ENABLED_DEFAULT,
            "avatar_config": {}
        }
        if self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
                default.update(data)
            except:
                pass
        self.ai_name = default["ai_name"]
        self.model = default["model"]
        self.system_prompt = default["system_prompt"] or f"Tu es {self.ai_name}, une assistante vocale francophone."
        self.edge_voice = default["edge_voice"]
        self.edge_rate = default["edge_rate"]
        self.edge_pitch = default["edge_pitch"]
        self.edge_volume = default["edge_volume"]
        self.tts_engine = default["tts_engine"]
        self.temperature = default["temperature"]
        self.max_tokens = default["max_tokens"]
        self.internet_enabled = default["internet_enabled"]
        self.avatar_config = default["avatar_config"]

    def save_config(self):
        data = {
            "ai_name": self.ai_name,
            "model": self.model,
            "system_prompt": self.system_prompt,
            "edge_voice": self.edge_voice,
            "edge_rate": self.edge_rate,
            "edge_pitch": self.edge_pitch,
            "edge_volume": self.edge_volume,
            "tts_engine": self.tts_engine,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "internet_enabled": self.internet_enabled,
            "avatar_config": self.avatar_config
        }
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.config_path, data)

sessions: Dict[str, SessionState] = {}

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def is_localhost(request: Request) -> bool:
    client_host = request.client.host if request.client else ""
    return client_host in ("127.0.0.1", "::1", "localhost")

def sanitize_filename(filename: str) -> str:
    return Path(filename).name

def is_zip_safe(base_dir: Path, target_path: Path) -> bool:
    try:
        resolved = target_path.resolve()
        resolved_base = base_dir.resolve()
        return resolved.is_relative_to(resolved_base)
    except:
        return False

def detect_emotion(text: str) -> str:
    text_lower = text.lower()
    if any(word in text_lower for word in ["content", "joyeu", "heureux", "excellente", "parfait"]):
        return "happy"
    if any(word in text_lower for word in ["triste", "désolé", "excuse", "malheureusement"]):
        return "sad"
    if any(word in text_lower for word in ["colère", "énervé", "fâché"]):
        return "angry"
    if any(word in text_lower for word in ["surpris", "ah bon", "vraiment"]):
        return "surprised"
    return "neutral"

LEAK_PATTERNS = [
    re.compile(r"règle prioritaire", re.IGNORECASE),
    re.compile(r"tu dois l'appeler par", re.IGNORECASE),
    re.compile(r"\bsystem says\b", re.IGNORECASE),
    re.compile(r"\bthe instruction says\b", re.IGNORECASE),
]

def check_prompt_leak(text: str) -> bool:
    return any(p.search(text) for p in LEAK_PATTERNS)

# ============================================================================
# FASTAPI APP SETUP
# ============================================================================

app = FastAPI(title=CONFIG.APP_TITLE)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    allow_credentials=True,
)

# ============================================================================
# BACKGROUND TASKS
# ============================================================================

async def memory_optimizer_loop():
    """Periodically compress memory for all users."""
    await asyncio.sleep(60)
    while True:
        try:
            base = Path("memory_profiles")
            if base.exists():
                for user_dir in base.iterdir():
                    if user_dir.is_dir():
                        for profile_dir in user_dir.iterdir():
                            if profile_dir.is_dir():
                                try:
                                    memory_mgr.compress_memory(user_dir.name, profile_dir.name)
                                except Exception as e:
                                    print(f"Optimize error {user_dir.name}/{profile_dir.name}: {e}")
        except Exception as e:
            print(f"Optimizer loop error: {e}")
        await asyncio.sleep(CONFIG.MEMORY_OPTIMIZE_INTERVAL_SEC)

# ============================================================================
# LIFESPAN
# ============================================================================

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(memory_optimizer_loop())

@app.on_event("shutdown")
async def shutdown_event():
    await llm_streamer.client.aclose()
    await tts_worker.stop()
    await web_searcher.client.aclose()

# ============================================================================
# HTTP ROUTES
# ============================================================================

@app.get("/")
async def root():
    return FileResponse("index.html", headers={"Cache-Control": "no-cache"})

@app.get("/assets/{path:path}")
async def assets(path: str):
    asset_path = Path("assets") / path
    if asset_path.exists():
        return FileResponse(asset_path)
    raise HTTPException(404)

@app.get("/api/library")
async def get_library():
    lib = {"models": [], "backgrounds": [], "live2d": []}
    assets_dir = Path("assets")
    if assets_dir.exists():
        for type_dir in ["models", "backgrounds", "live2d"]:
            dir_path = assets_dir / type_dir
            if dir_path.exists():
                for file in dir_dir.iterdir():
                    if file.is_file():
                        lib[type_dir].append({
                            "name": file.name,
                            "path": f"/assets/{type_dir}/{file.name}",
                            "type": type_dir[:-1] if type_dir.endswith('s') else type_dir
                        })
    return lib

@app.get("/api/animation-catalog")
async def get_animation_catalog():
    catalog_file = Path("assets") / "animation_catalog.json"
    if catalog_file.exists():
        try:
            return json.loads(catalog_file.read_text(encoding="utf-8"))
        except:
            pass
    return {"animations": []}

@app.post("/api/upload/animation")
async def upload_animation(request: Request):
    form = await request.form()
    file = form.get("file")
    if not file:
        raise HTTPException(400, "Missing file")
    dest = Path("assets") / "animations" / sanitize_filename(file.filename)
    dest.parent.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    dest.write_bytes(content)
    return {"status": "ok", "path": f"/assets/animations/{dest.name}"}

@app.post("/api/upload/background")
async def upload_background(request: Request):
    form = await request.form()
    file = form.get("file")
    if not file:
        raise HTTPException(400)
    dest = Path("assets") / "backgrounds" / sanitize_filename(file.filename)
    dest.parent.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    dest.write_bytes(content)
    return {"status": "ok"}

@app.post("/api/upload/model")
async def upload_model(request: Request):
    form = await request.form()
    file = form.get("file")
    if not file:
        raise HTTPException(400)
    dest = Path("assets") / "models" / sanitize_filename(file.filename)
    dest.parent.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    dest.write_bytes(content)
    return {"status": "ok"}

@app.post("/api/upload/live2d")
async def upload_live2d(request: Request):
    form = await request.form()
    file = form.get("file")
    if not file or not file.filename.endswith(".zip"):
        raise HTTPException(400, "Only zip files allowed")
    dest_dir = Path("assets") / "live2d" / sanitize_filename(file.filename).replace(".zip", "")
    dest_dir.mkdir(parents=True, exist_ok=True)

    import zipfile
    content = await file.read()
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for member in zf.infolist():
            member_path = dest_dir / member.filename
            if not is_zip_safe(dest_dir, member_path):
                raise HTTPException(400, "Zip slip detected")
            zf.extract(member, dest_dir)

    return {"status": "ok"}

@app.get("/api/conversations/{user}/{profile}")
async def list_conversations(user: str, profile: str):
    history_dir = Path("memory_profiles") / user / profile / "history"
    if not history_dir.exists():
        return []
    sessions = []
    for file in history_dir.glob("*.json"):
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
            sessions.append({"id": file.stem, "title": data.get("title", "Session"), "ts": data.get("ts", "")})
        except:
            pass
    return sorted(sessions, key=lambda x: x.get("ts", ""), reverse=True)

@app.get("/api/conversations/{user}/{profile}/{session_id}")
async def get_conversation(user: str, profile: str, session_id: str):
    path = Path("memory_profiles") / user / profile / "history" / f"{session_id}.json"
    if not path.exists():
        raise HTTPException(404)
    return json.loads(path.read_text(encoding="utf-8"))

@app.get("/api/usage/{user_id}/{profile_id}")
async def get_usage(user_id: str, profile_id: str):
    usage_file = Path("memory_profiles") / "llm_usage.json"
    if not usage_file.exists():
        return {"free": {}, "paid": {}}
    return json.loads(usage_file.read_text(encoding="utf-8"))

# ============================================================================
# WEBSOCKET ENDPOINT
# ============================================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    client_id = secrets.token_urlsafe(8)
    query_params = dict(websocket.query_params)
    user_id = query_params.get("user_id", CONFIG.DEFAULT_USER_ID)
    profile_id = query_params.get("profile_id", CONFIG.DEFAULT_PROFILE_ID)
    remote_addr = websocket.client.host if websocket.client else ""

    is_remote = remote_addr not in ("127.0.0.1", "::1", "localhost")

    await manager.connect(client_id, websocket)
    session = SessionState(client_id, user_id, profile_id)

    if is_remote and not session.authenticated:
        pending_auth = True
    else:
        pending_auth = False
        session.authenticated = True

    sessions[client_id] = session

    try:
        await manager.send_json(client_id, {"type": "connected", "client_id": client_id, "requires_2fa": pending_auth})

        while True:
            try:
                data = await websocket.receive_json()
                msg_type = data.get("type")

                if msg_type == "get_config":
                    await manager.send_json(client_id, {
                        "type": "config",
                        "data": {
                            "user_id": session.user_id,
                            "profile_id": session.profile_id,
                            "ai_name": session.ai_name,
                            "model": session.model,
                            "system_prompt": session.system_prompt,
                            "edge_voice": session.edge_voice,
                            "edge_rate": session.edge_rate,
                            "edge_pitch": session.edge_pitch,
                            "edge_volume": session.edge_volume,
                            "tts_engine": session.tts_engine,
                            "temperature": session.temperature,
                            "max_tokens": session.max_tokens,
                            "internet_enabled": session.internet_enabled,
                            "avatar_config": session.avatar_config,
                        }
                    })

                elif msg_type == "set_config":
                    cfg = data.get("data", {})
                    session.user_id = cfg.get("user_id", session.user_id)
                    session.profile_id = cfg.get("profile_id", session.profile_id)
                    session.ai_name = cfg.get("ai_name", session.ai_name)
                    session.model = cfg.get("model", session.model)
                    session.system_prompt = cfg.get("system_prompt", session.system_prompt)
                    session.edge_voice = cfg.get("edge_voice", session.edge_voice)
                    session.edge_rate = cfg.get("edge_rate", session.edge_rate)
                    session.edge_pitch = cfg.get("edge_pitch", session.edge_pitch)
                    session.edge_volume = cfg.get("edge_volume", session.edge_volume)
                    session.tts_engine = cfg.get("tts_engine", session.tts_engine)
                    session.temperature = cfg.get("temperature", session.temperature)
                    session.max_tokens = cfg.get("max_tokens", session.max_tokens)
                    session.internet_enabled = cfg.get("internet_enabled", session.internet_enabled)
                    session.avatar_config = cfg.get("avatar_config", session.avatar_config)
                    session.save_config()
                    await manager.send_json(client_id, {"type": "config_updated"})

                elif msg_type == "auth_2fa_begin":
                    user_id_auth = data.get("user_id")
                    if not user_id_auth:
                        await manager.send_json(client_id, {"type": "error", "message": "user_id required"})
                        continue
                    code = twofa_mgr.begin(user_id_auth)
                    await manager.send_json(client_id, {
                        "type": "2fa_challenge",
                        "data": {"user_id": user_id_auth, "message": "Code généré (pour test: dans console)"}
                    })

                elif msg_type == "auth_2fa_verify":
                    user_id_verify = data.get("user_id")
                    code = data.get("code", "")
                    if twofa_mgr.verify(user_id_verify, code):
                        session.authenticated = True
                        session.user_id = user_id_verify
                        session.profile_id = CONFIG.DEFAULT_PROFILE_ID
                        await manager.send_json(client_id, {"type": "auth_success"})
                    else:
                        await manager.send_json(client_id, {"type": "auth_failure", "message": "Code invalide ou expiré"})

                elif msg_type == "user_text":
                    if not session.authenticated:
                        await manager.send_json(client_id, {"type": "error", "message": "Authentification requise"})
                        continue

                    text = data.get("text", "").strip()
                    if not text:
                        continue

                    silent = data.get("silent", False)
                    profile = data.get("profile", session.profile_id)
                    user_id = data.get("user_id") or session.user_id

                    try:
                        await manager.send_json(client_id, {"type": "state", "state": "thinking"})

                        web_ctx_task = None
                        secondary_task = asyncio.create_task(
                            memory_mgr.load_memory(user_id, profile, "secondary")
                        )

                        needs_web = session.internet_enabled and any(
                            kw in text.lower() for kw in ["recherche", "cherche", "actualité", "news", "quoi de neuf"]
                        )
                        if needs_web:
                            web_ctx_task = asyncio.create_task(web_searcher.search(text))

                        await asyncio.sleep(0)

                        secondary_ctx = await secondary_task
                        web_ctx = await web_ctx_task if web_ctx_task else ""

                        if needs_web and web_ctx:
                            await manager.send_json(client_id, {"type": "assistant_chunk", "text": "Je consulte le web..."})
                            await asyncio.sleep(0.1)

                        context = memory_mgr.build_context(user_id, profile)
                        if web_ctx:
                            context.append({"role": "system", "content": f"Résultat web :\n{web_ctx}"})
                        context.append({"role": "user", "content": text})

                        tts_queue = asyncio.Queue()
                        await tts_worker.start()

                        full_response = ""
                        pending_segment = ""
                        tts_task = asyncio.create_task(tts_worker._worker_loop())

                        async def on_chunk(chunk: str):
                            await manager.send_json(client_id, {"type": "assistant_chunk", "text": chunk})

                        try:
                            async for token in llm_streamer.stream_llm(
                                context,
                                model_override=session.model if session.model else None,
                                on_chunk=on_chunk
                            ):
                                if check_prompt_leak(token):
                                    await manager.send_json(client_id, {"type": "assistant_chunk", "text": "[FILTRÉ]"})
                                    continue

                                full_response += token
                                pending_segment += token

                                segments = tts_worker.split_into_tts_segments(pending_segment)
                                if len(segments) > 1:
                                    for seg in segments[:-1]:
                                        await tts_queue.put({"text": seg, "websocket": websocket})
                                    pending_segment = segments[-1]

                                await manager.send_json(client_id, {"type": "assistant_chunk", "text": token})
                                await asyncio.sleep(0)

                        except Exception as e:
                            print(f"LLM stream error: {e}")
                            await manager.send_json(client_id, {"type": "error", "message": "Erreur LLM"})

                        if pending_segment.strip():
                            await tts_queue.put({"text": pending_segment, "websocket": websocket})

                        await tts_queue.put(None)
                        await tts_task

                        if not full_response.strip():
                            full_response = "Désolé, je ne peux pas répondre pour le moment."
                            await manager.send_json(client_id, {"type": "assistant_chunk", "text": full_response})

                        memory_mgr.append_message(user_id, profile, "user", text)
                        memory_mgr.append_message(user_id, profile, "assistant", full_response)

                        emotion = detect_emotion(full_response)

                        usage_file = Path("memory_profiles") / "llm_usage.json"
                        usage_stats = {}
                        if usage_file.exists():
                            try:
                                usage_stats = json.loads(usage_file.read_text(encoding="utf-8"))
                            except:
                                pass

                        await manager.send_json(client_id, {
                            "type": "assistant_done",
                            "emotion": emotion,
                            "usage": usage_stats
                        })
                        await manager.send_json(client_id, {"type": "state", "state": "idle"})

                    except Exception as e:
                        print(f"Handle user_text error: {e}")
                        await manager.send_json(client_id, {"type": "error", "message": str(e)})

                else:
                    await manager.send_json(client_id, {"type": "error", "message": f"Unknown message type: {msg_type}"})

            except WebSocketDisconnect:
                break
            except Exception as e:
                print(f"WebSocket loop error: {e}")
                break

    except Exception as e:
        print(f"WebSocket connection error: {e}")
    finally:
        manager.disconnect(client_id)
        if client_id in sessions:
            del sessions[client_id]

# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=CONFIG.PORT)
