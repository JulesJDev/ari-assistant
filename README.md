# Ari Assistant 🎙️

A local, francophone, low-latency voice AI assistant with a tavern RPG theme. Runs entirely locally using OpenRouter as LLM provider.

## Quick Start (Launcher)

1. Run the launcher GUI:
   ```bash
   python3 launcher.py
   ```

2. In the launcher window:
   - Click **"🚀 Start Server"** (creates virtualenv and installs dependencies automatically)
   - Click **"🌐 Open Interface"** to open the web UI in your browser

3. To stop: click **"⏹️ Stop Server"** or close the window.

## Manual Setup

If you prefer command line:

```bash
# 1. Create virtualenv
python3 -m venv venv

# 2. Activate virtualenv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env and add your OPENROUTER_API_KEY

# 5. Start server
uvicorn app:app --host 127.0.0.1 --port 8000 --reload

# 6. Open http://127.0.0.1:8000 in your browser
```

## Configuration

All configuration is done via environment variables (`.env` file). See `.env.example` for all available options. Key settings:

- `OPENROUTER_API_KEY`: Required. Get from https://openrouter.ai
- `OPENROUTER_MODEL`: Default model (e.g., `google/gemma-3-27b-it:free`)
- `EDGE_VOICE`: Microsoft Edge TTS voice (e.g., `fr-FR-DeniseNeural`)
- `PORT`: Server port (default 8000)

## Architecture

```
[index.html] ↔ WebSocket ↔ [FastAPI backend]
        ↳ OpenRouter (LLM streaming)
        ↳ Edge TTS / Piper (TTS)
        ↳ Tavily / DuckDuckGo (web search)
        ↳ JSON files (memory, no database)
```

See `SPEC_REWRITE.md` for full technical specification.

## Features

- ✅ 100% async FastAPI backend
- ✅ WebSocket real-time chat with SSE-style streaming
- ✅ Streaming TTS (Edge TTS with Piper fallback)
- ✅ 3-layer memory system (raw, compressed, episodic)
- ✅ Web search with 4-level fallback
- ✅ 2FA authentication for remote connections
- ✅ Single-page frontend (no build step)
- ✅ Tkinter launcher with live logs

## Memory Profiles

User memory is stored in `memory_profiles/{user_id}/{profile_id}/`:
- `memory.json` — raw conversation turns (rotating)
- `memory_fast.json` — compressed preferences
- `memory_secondary.json` — episodic facts

## Notes

- Internet connection required for OpenRouter API and Edge TTS.
- For local-only mode (no OpenRouter), a local LLM integration via Ollama is not included in this rewrite (TODO).
- Piper TTS is optional; install separately from https://github.com/rhasspy/piper.

## License

MIT
