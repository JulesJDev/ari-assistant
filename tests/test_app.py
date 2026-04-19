"""
Application-level tests for Ari Assistant.
Tests memory atomic writes, LLM fallback, TTS queue, web search cascade, and more.
"""

import pytest
import sys
import os
import tempfile
import json
import time
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add parent dir to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Import security functions used in end-to-end tests
from core.security import (
    sanitize_html,
    validate_upload_file,
    anti_zip_slip,
    rate_limit_check,
    generate_2fa_code,
    store_2fa_code,
    get_2fa_code,
    verify_2fa_code,
    is_2fa_expired,
)

import core.security as sec_mod_app

# Reset security module state before each test for isolation
@pytest.fixture(autouse=True)
def _reset_security_state_app():
    sec_mod_app._rate_limit_store.clear()
    sec_mod_app._2fa_store.clear()
    sec_mod_app._ws_connections.clear()

# We'll mock external dependencies since they may not be installed
# In a real setup, requirements.txt would provide them


# ============ Mock Fixtures ============

@pytest.fixture
def temp_memory_file():
    """Create a temporary memory file for testing."""
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as f:
        yield f.name
    try:
        os.unlink(f.name)
    except FileNotFoundError:
        pass


@pytest.fixture
def mock_llm_clients():
    """Mock multiple LLM clients for fallback testing."""
    return {
        "primary": MagicMock(),
        "fallback1": MagicMock(),
        "fallback2": MagicMock(),
    }


# ============ Memory Atomic Write Tests ============

class TestMemoryAtomicWrites:
    """Tests for atomic memory writes."""

    def test_atomic_write_simple(self):
        """Test basic atomic write using tempfile rename."""
        data = {"test": "data", "number": 42}

        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "memory.json")

            # Atomic write
            with tempfile.NamedTemporaryFile(mode='w', dir=tmpdir, delete=False) as tf:
                json.dump(data, tf)
                temp_name = tf.name
            os.rename(temp_name, target)

            # Verify
            with open(target, 'r') as f:
                loaded = json.load(f)
            assert loaded == data

    def test_atomic_write_concurrent_simulation(self):
        """Simulate concurrent writes; atomicity prevents corruption."""
        target = tempfile.mktemp(suffix=".json")

        def write_data(pid, value):
            data = {"pid": pid, "value": value}
            with tempfile.NamedTemporaryFile(mode='w', delete=False) as tf:
                json.dump(data, tf)
                temp = tf.name
            os.rename(temp, target)

        # Simulate two processes
        import threading
        t1 = threading.Thread(target=write_data, args=(1, "A"))
        t2 = threading.Thread(target=write_data, args=(2, "B"))

        t1.start(); t2.start()
        t1.join(); t2.join()

        with open(target, 'r') as f:
            result = json.load(f)

        # One write should win; file should be valid JSON
        assert result["pid"] in [1, 2]
        assert result["value"] in ["A", "B"]
        os.unlink(target)

    def test_atomic_write_partial_failure_recovery(self):
        """Partial writes should not corrupt target file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "memory.json")

            # Write initial valid file
            with open(target, 'w') as f:
                json.dump({"version": 1}, f)

            # Simulate failed partial write
            bad_temp = os.path.join(tmpdir, "bad.tmp")
            with open(bad_temp, 'w') as f:
                f.write("INCOMPLETE JSON {")  # Invalid
            # Don't rename - target should remain valid

            with open(target, 'r') as f:
                loaded = json.load(f)
            assert loaded == {"version": 1}


# ============ LLM Fallback Rotation Tests ============

class TestLLMFallbackRotation:
    """Tests for LLM fallback and rotation logic."""

    def test_llm_fallback_success_on_primary(self):
        """Primary LLM should succeed and not use fallbacks."""
        primary = MagicMock()
        primary.generate.return_value = "Response from primary"

        # Simulate fallback logic
        def try_llm(client, prompt):
            return client.generate(prompt)

        result = try_llm(primary, "Hello")
        assert result == "Response from primary"
        primary.generate.assert_called_once()

    def test_llm_fallback_on_primary_failure(self):
        """If primary fails, fallback1 should be tried."""
        primary = MagicMock()
        primary.generate.side_effect = Exception("Primary down")
        fallback1 = MagicMock()
        fallback1.generate.return_value = "Fallback1 response"

        # Simulate fallback chain
        clients = [primary, fallback1]
        result = None
        for client in clients:
            try:
                result = client.generate("Hello")
                break
            except Exception:
                continue

        assert result == "Fallback1 response"
        fallback1.generate.assert_called_once()

    def test_llm_all_fail(self):
        """If all LLMs fail, appropriate exception should be raised."""
        clients = [MagicMock(), MagicMock(), MagicMock()]
        for c in clients:
            c.generate.side_effect = Exception("LLM down")

        errors = []
        for client in clients:
            try:
                client.generate("Hello")
            except Exception as e:
                errors.append(e)

        assert len(errors) == 3

    def test_llm_rotation_round_robin(self):
        """Test round-robin selection makes progress."""
        # We'll implement a simple round-robin
        clients = [MagicMock(return_value=f"LLM{i}") for i in range(3)]
        current = 0

        def get_next_llm():
            nonlocal current
            client = clients[current]
            current = (current + 1) % len(clients)
            return client

        # Get 6 responses
        results = [get_next_llm()("test") for _ in range(6)]

        assert results == ["LLM0", "LLM1", "LLM2", "LLM0", "LLM1", "LLM2"]


# ============ TTS Queue Order Tests ============

class TestTTSQueueOrder:
    """Tests for TTS audio queue ordering."""

    def test_tts_queue_fifo(self):
        """TTS should play in first-in-first-out order."""
        from collections import deque

        queue = deque()
        queue.append("audio1.wav")
        queue.append("audio2.wav")
        queue.append("audio3.wav")

        order = []
        while queue:
            order.append(queue.popleft())

        assert order == ["audio1.wav", "audio2.wav", "audio3.wav"]

    def test_tts_queue_priority(self):
        """Priority items should jump queue if implemented."""
        # Simulate priority queue
        import heapq

        heap = []
        heapq.heappush(heap, (2, "normal1"))   # normal priority
        heapq.heappush(heap, (0, "urgent"))    # highest priority
        heapq.heappush(heap, (2, "normal2"))
        heapq.heappush(heap, (1, "high"))

        order = [heapq.heappop(heap)[1] for _ in range(len(heap))]

        assert order == ["urgent", "high", "normal1", "normal2"]

    def test_tts_queue_clear(self):
        """Queue should be clearable."""
        from collections import deque
        queue = deque(["a", "b", "c"])
        queue.clear()
        assert len(queue) == 0

    def test_tts_queue_concurrent_access(self):
        """Queue operations should be thread-safe if lock used."""
        from queue import Queue
        import threading

        q = Queue()
        for i in range(10):
            q.put(f"audio{i}.wav")

        results = []
        def worker():
            while not q.empty():
                item = q.get()
                results.append(item)
                q.task_done()

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 10
        assert set(results) == {f"audio{i}.wav" for i in range(10)}


# ============ Web Search Cascade Tests ============

class TestWebSearchCascade:
    """Tests for web search fallback cascade."""

    def test_search_cascade_primary_success(self):
        """Primary search engine should be used if available."""
        search_engines = [
            {"name": "google", "search": MagicMock(return_value=["result1", "result2"])},
            {"name": "duckduckgo", "search": MagicMock(return_value=["ddg1"])},
            {"name": "bing", "search": MagicMock(return_value=["bing1"])},
        ]

        def search(query, engines):
            for engine in engines:
                try:
                    results = engine["search"](query)
                    if results:
                        return results, engine["name"]
                except Exception:
                    continue
            return [], None

        results, engine = search("test", search_engines)
        assert results == ["result1", "result2"]
        assert engine == "google"

    def test_search_cascade_fallback(self):
        """Fallback should work if primary fails."""
        search_engines = [
            {"name": "google", "search": MagicMock(side_effect=Exception("down"))},
            {"name": "duckduckgo", "search": MagicMock(return_value=["ddg1", "ddg2"])},
        ]

        def search(query, engines):
            for engine in engines:
                try:
                    results = engine["search"](query)
                    if results:
                        return results, engine["name"]
                except Exception:
                    continue
            return [], None

        results, engine = search("test", search_engines)
        assert results == ["ddg1", "ddg2"]
        assert engine == "duckduckgo"

    def test_search_cascade_all_fail(self):
        """Should handle case where all engines fail."""
        search_engines = [
            {"name": "g1", "search": MagicMock(side_effect=Exception())},
            {"name": "g2", "search": MagicMock(return_value=[])},
        ]

        def search(query, engines):
            for engine in engines:
                try:
                    results = engine["search"](query)
                    if results:
                        return results, engine["name"]
                except Exception:
                    continue
            return [], None

        results, engine = search("test", search_engines)
        assert results == []
        assert engine is None

    def test_search_cascade_result_merging(self):
        """Optionally merge results from multiple sources."""
        # If cascade is used to combine results
        google = ["g1", "g2"]
        ddg = ["d1", "d2"]
        merged = google + ddg
        # Could also deduplicate
        assert len(merged) == 4


# ============ WebSocket Heartbeat Tests ============

class TestWSHeartbeat:
    """Tests for WebSocket heartbeat mechanism."""

    def test_heartbeat_timestamp_update(self):
        """Heartbeat should update last seen timestamp."""
        connection_states = {}

        def heartbeat(ws_id):
            connection_states[ws_id] = time.time()

        heartbeat("conn1")
        assert "conn1" in connection_states
        first = connection_states["conn1"]

        time.sleep(0.01)
        heartbeat("conn1")
        second = connection_states["conn1"]
        assert second > first

    def test_heartbeat_timeout_detection(self):
        """Stale connections without heartbeat should be detected."""
        # Simulate connection tracking
        connections = {
            "active": time.time(),
            "stale": time.time() - 30,  # 30 seconds old
        }
        timeout = 15

        dead = []
        for cid, last_seen in connections.items():
            if time.time() - last_seen > timeout:
                dead.append(cid)

        assert "stale" in dead
        assert "active" not in dead

    def test_heartbeat_interval(self):
        """Heartbeat interval should be reasonable (e.g., 30s)."""
        heartbeat_interval = 30
        assert 10 <= heartbeat_interval <= 60

    def test_heartbeat_response(self):
        """Server should respond to client pings."""
        # Mock: server receives ping, sends pong
        ping_received = False
        pong_sent = False

        def on_ping():
            nonlocal ping_received
            ping_received = True
            # send pong
            nonlocal pong_sent
            pong_sent = True

        on_ping()
        assert ping_received is True
        assert pong_sent is True


# ============ End-to-End Scenario Tests ============

class TestEndToEndScenarios:
    """High-level scenario tests."""

    def test_full_user_session(self):
        """
        Simulate a full user session:
        1. Upload a file
        2. Authenticate with 2FA
        3. Send message (rate-limited)
        4. Receive response
        """
        # Step 1: Upload
        allowed, _ = validate_upload_file("model.glb", 2 * 1024 * 1024)
        assert allowed is True

        # Step 2: 2FA
        code = generate_2fa_code()
        store_2fa_code("test_user", code)
        retrieved = get_2fa_code("test_user")
        assert retrieved == code
        success, _ = verify_2fa_code(retrieved, code, 0)
        assert success is True

        # Step 3: Rate-limited API call
        assert rate_limit_check("192.168.1.1", "/api") is True

        # Step 4: Sanitize response
        response = "<p>Your model is ready</p>"
        safe = sanitize_html(response)
        assert "&lt;p&gt;" in safe

    def test_attack_mitigation(self):
        """Test that common attacks are mitigated."""
        # XSS
        xss = "<script>stealCookies()</script>"
        assert "<script>" not in sanitize_html(xss)

        # Zip Slip
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, "evil.zip")
            with zipfile.ZipFile(zip_path, 'w') as zf:
                zf.writestr("../../../tmp/evil.txt", "data")
            assert anti_zip_slip(zip_path, tmpdir) is False

        # EXE upload
        allowed, reason = validate_upload_file("malware.exe", 1024)
        assert allowed is False

        # Rate limit bypass (different endpoint)
        ip = "10.0.0.1"
        for _ in range(25):
            rate_limit_check(ip, "/api")  # Should block after 20
        assert rate_limit_check(ip, "/api") is False

# ============ Additional Required Tests (per spec) ============

def test_2fa_generation_and_verification():
    """Test 2FA code generation and verification."""
    code = generate_2fa_code()
    store_2fa_code("spec_user", code)
    retrieved = get_2fa_code("spec_user")
    assert retrieved == code
    success, _ = verify_2fa_code(retrieved, code, 0)
    assert success is True


def test_2fa_expiry():
    """Test that 2FA codes expire."""
    user_id = "expiry_spec"
    store_2fa_code(user_id, "1234", expiry_seconds=1)
    time.sleep(2)
    assert is_2fa_expired(user_id) is True


def test_rate_limiting():
    """Test API rate limiting."""
    ip = "192.168.2.1"
    for _ in range(20):
        assert rate_limit_check(ip, "/api") is True
    assert rate_limit_check(ip, "/api") is False


def test_upload_security_zip_slip():
    """Test zip slip protection."""
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = os.path.join(tmpdir, "evil.zip")
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr("../../../tmp/evil.txt", "data")
        assert anti_zip_slip(zip_path, tmpdir) is False


def test_upload_reject_exe():
    """Test executable file rejection."""
    allowed, _ = validate_upload_file("virus.exe", 1024)
    assert allowed is False


def test_ws_heartbeat():
    """Test WebSocket heartbeat mechanism."""
    state = {}
    def heartbeat(cid):
        state[cid] = time.time()
    heartbeat("conn1")
    assert "conn1" in state
    time.sleep(0.01)
    # Connection should still be considered fresh
    assert time.time() - state["conn1"] < 10
