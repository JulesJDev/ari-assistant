#!/usr/bin/env python3
"""
Ari Launcher — GUI Tkinter for starting/stopping the backend server.
Features:
- Auto-detect/create virtualenv
- Install dependencies if needed
- Launch uvicorn server
- Real-time log console
- Open Interface button (opens browser)
- Clean shutdown on window close
"""

import sys
import subprocess
import webbrowser
import threading
import time
from pathlib import Path
from datetime import datetime

try:
    import tkinter as tk
    from tkinter import ttk, scrolledtext, messagebox
except ImportError:
    print("Tkinter not available. Install python3-tk or equivalent.")
    sys.exit(1)

# Paths
BASE_DIR = Path(__file__).parent
VENV_DIR = BASE_DIR / "venv"
REQUIREMENTS_FILE = BASE_DIR / "requirements.txt"
APP_MODULE = "app:app"
HOST = "127.0.0.1"
PORT = 8000

class AriLauncher:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Ari Assistant — Launcher")
        self.root.geometry("850x600")
        self.root.resizable(True, True)

        self.server_process: Optional[subprocess.Popen] = None
        self.running = False
        self.venv_python: Optional[Path] = None

        self._setup_ui()
        self._check_environment()

    def _setup_ui(self):
        """Build the GUI."""
        # Header frame
        header = ttk.Frame(self.root, padding="10")
        header.pack(fill=tk.X)

        ttk.Label(header, text="🎙️ Ari Assistant", font=("Helvetica", 16, "bold")).pack(side=tk.LEFT)
        ttk.Label(header, text="Launcher", font=("Helvetica", 10)).pack(side=tk.LEFT, padx=(10, 0))

        # Control buttons frame
        controls = ttk.Frame(self.root, padding="10")
        controls.pack(fill=tk.X)

        self.btn_start = ttk.Button(controls, text="🚀 Start Server", command=self.start_server, width=20)
        self.btn_start.pack(side=tk.LEFT, padx=(0, 10))

        self.btn_stop = ttk.Button(controls, text="⏹️ Stop Server", command=self.stop_server, width=20, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=(0, 10))

        self.btn_open = ttk.Button(controls, text="🌐 Open Interface", command=self.open_interface, width=20, state=tk.DISABLED)
        self.btn_open.pack(side=tk.LEFT, padx=(0, 10))

        self.btn_install = ttk.Button(controls, text="📦 Install Dependencies", command=self.install_dependencies, width=20)
        self.btn_install.pack(side=tk.LEFT, padx=(0, 10))

        # Status frame
        status_frame = ttk.Frame(self.root, padding="10")
        status_frame.pack(fill=tk.X)

        ttk.Label(status_frame, text="Status:").pack(side=tk.LEFT)
        self.lbl_status = ttk.Label(status_frame, text="⚪ Stopped", foreground="gray")
        self.lbl_status.pack(side=tk.LEFT, padx=(5, 0))

        self.lbl_venv = ttk.Label(status_frame, text="Venv: ?", font=("Consolas", 9))
        self.lbl_venv.pack(side=tk.RIGHT)

        # Log console
        log_frame = ttk.Frame(self.root, padding="10")
        log_frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(log_frame, text="Server Log:").pack(anchor=tk.W)
        self.log_area = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, height=20, font=("Consolas", 9))
        self.log_area.pack(fill=tk.BOTH, expand=True)
        self.log_area.configure(state=tk.DISABLED)

        # Configure tag colors for log levels
        self.log_area.tag_config("INFO", foreground="black")
        self.log_area.tag_config("WARNING", foreground="orange")
        self.log_area.tag_config("ERROR", foreground="red")
        self.log_area.tag_config("DEBUG", foreground="gray")

    def _check_environment(self):
        """Check and report venv/virtualenv status, dependency status."""
        if VENV_DIR.exists():
            # Try to find python executable in venv
            for bin_name in ["python", "python3", "python.exe"]:
                candidate = VENV_DIR / "bin" / bin_name if sys.platform != "win32" else VENV_DIR / "Scripts" / bin_name
                if candidate.exists():
                    self.venv_python = candidate
                    self.lbl_venv.config(text=f"Venv: ✅ {candidate.name}")
                    break
            if not self.venv_python:
                self.lbl_venv.config(text="Venv: ⚠️ Exists but no python?")
                self._log("WARNING", "Venv exists but Python executable not found.\n")
        else:
            self.lbl_venv.config(text="Venv: ❌ Not found")
            self._log("INFO", f"Virtual environment not found at {VENV_DIR}\n")

        # Check requirements.txt timestamp vs venv marker
        self._check_dependency_status()

    def _check_dependency_status(self):
        """Check if dependencies might need installing (simple heuristic)."""
        if not self.venv_python:
            return
        # If no packages installed, or requirements newer than venv, prompt
        # We'll just log a hint
        pip_freeze = subprocess.run([str(self.venv_python), "-m", "pip", "freeze"], capture_output=True, text=True)
        if pip_freeze.returncode != 0 or not pip_freeze.stdout.strip():
            self._log("WARNING", "No packages installed in venv. Click 'Install Dependencies'.\n")
        else:
            self._log("INFO", "Dependencies check: venv appears populated.\n")

    def _log(self, level: str, message: str):
        """Append a log message to the console with timestamp."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted = f"[{timestamp}] [{level}] {message}"
        self.log_area.configure(state=tk.NORMAL)
        self.log_area.insert(tk.END, formatted + "\n", level)
        self.log_area.see(tk.END)
        self.log_area.configure(state=tk.DISABLED)

    def start_server(self):
        """Create venv if needed, install deps, and start uvicorn."""
        if self.running:
            messagebox.showwarning("Already running", "The server is already running.")
            return

        # Ensure venv exists
        if not self.venv_python:
            self._log("INFO", "Creating virtual environment...\n")
            try:
                subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])
                self._log("INFO", "Virtual environment created.\n")
                # Update venv python path
                for bin_name in ["python", "python3", "python.exe"]:
                    candidate = VENV_DIR / "bin" / bin_name if sys.platform != "win32" else VENV_DIR / "Scripts" / bin_name
                    if candidate.exists():
                        self.venv_python = candidate
                        self.lbl_venv.config(text=f"Venv: ✅ {candidate.name}")
                        break
            except subprocess.CalledProcessError as e:
                self._log("ERROR", f"Failed to create venv: {e}\n")
                messagebox.showerror("Error", f"Failed to create virtual environment:\n{e}")
                return

        # Check if we need to install dependencies
        if REQUIREMENTS_FILE.exists():
            self._log("INFO", f"Installing dependencies from {REQUIREMENTS_FILE}...\n")
            try:
                subprocess.check_call([str(self.venv_python), "-m", "pip", "install", "-U", "pip"])
                subprocess.check_call([str(self.venv_python), "-m", "pip", "install", "-r", str(REQUIREMENTS_FILE)])
                self._log("INFO", "Dependencies installed.\n")
            except subprocess.CalledProcessError as e:
                self._log("ERROR", f"Dependency installation failed: {e}\n")
                res = messagebox.askyesno("Install failed", "Dependency installation failed. Continue anyway?")
                if not res:
                    return
        else:
            self._log("WARNING", f"requirements.txt not found at {REQUIREMENTS_FILE}\n")

        # Launch server in separate thread to not block GUI
        self._log("INFO", f"Starting uvicorn server at http://{HOST}:{PORT} ...\n")
        cmd = [
            str(self.venv_python), "-m", "uvicorn", APP_MODULE,
            "--host", HOST, "--port", str(PORT), "--reload"
        ]
        self._log("DEBUG", f"Command: {' '.join(cmd)}\n")

        def run_server():
            try:
                self.server_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=BASE_DIR,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                )
                self.running = True
                # Read output line by line
                for line in iter(self.server_process.stdout.readline, ''):
                    if line:
                        # Parse log level from line (heuristic)
                        level = "INFO"
                        upper = line.upper()
                        if "ERROR" in upper or "TRACEBACK" in upper:
                            level = "ERROR"
                        elif "WARNING" in upper or "WARN" in upper:
                            level = "WARNING"
                        elif "DEBUG" in upper:
                            level = "DEBUG"
                        self._log(level, line.rstrip())
                    else:
                        break
                # Process ended
                self.running = False
                self._log("INFO", "Server process stopped.\n")
            except Exception as e:
                self._log("ERROR", f"Failed to start server: {e}\n")

        threading.Thread(target=run_server, daemon=True).start()

        # Update UI after short delay to allow process to start
        self.root.after(1000, self._update_running_state)

    def _update_running_state(self):
        """Check if process is running and update UI."""
        if self.server_process and self.server_process.poll() is None:
            self.running = True
            self.lbl_status.config(text="🟢 Running", foreground="green")
            self.btn_start.config(state=tk.DISABLED)
            self.btn_stop.config(state=tk.NORMAL)
            self.btn_open.config(state=tk.NORMAL)
        else:
            self.running = False
            self.lbl_status.config(text="⚪ Stopped", foreground="gray")
            self.btn_start.config(state=tk.NORMAL)
            self.btn_stop.config(state=tk.DISABLED)
            self.btn_open.config(state=tk.DISABLED)

    def stop_server(self):
        """Send SIGTERM to server process and wait for clean shutdown."""
        if self.server_process and self.running:
            self._log("INFO", "Stopping server...\n")
            try:
                self.server_process.terminate()
                try:
                    self.server_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._log("WARNING", "Force killing server...\n")
                    self.server_process.kill()
                self._log("INFO", "Server stopped.\n")
            except Exception as e:
                self._log("ERROR", f"Error stopping server: {e}\n")
        self.running = False
        self.lbl_status.config(text="⚪ Stopped", foreground="gray")
        self.btn_start.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)
        self.btn_open.config(state=tk.DISABLED)

    def open_interface(self):
        """Open the browser to the Ari interface."""
        url = f"http://{HOST}:{PORT}"
        self._log("INFO", f"Opening browser: {url}\n")
        webbrowser.open(url)

    def install_dependencies(self):
        """Run pip install -r requirements.txt in venv."""
        if not self.venv_python:
            messagebox.showerror("No venv", "Virtual environment not found. Start server to create it, or create manually.")
            return
        self._log("INFO", f"Installing dependencies from {REQUIREMENTS_FILE}...\n")
        try:
            subprocess.check_call([str(self.venv_python), "-m", "pip", "install", "-U", "pip"])
            subprocess.check_call([str(self.venv_python), "-m", "pip", "install", "-r", str(REQUIREMENTS_FILE)])
            self._log("INFO", "Dependencies installed successfully.\n")
            messagebox.showinfo("Success", "Dependencies installed.")
        except subprocess.CalledProcessError as e:
            self._log("ERROR", f"Failed to install dependencies: {e}\n")
            messagebox.showerror("Error", f"Failed to install dependencies:\n{e}")

def main():
    root = tk.Tk()
    app = AriLauncher(root)

    def on_closing():
        if app.running:
            if messagebox.askokcancel("Quit", "Server is running. Stop and quit?"):
                app.stop_server()
                # Wait a moment for shutdown
                root.after(500, root.destroy)
        else:
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_closing)
    root.mainloop()

if __name__ == "__main__":
    main()
