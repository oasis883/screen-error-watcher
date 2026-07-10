"""
Screen Error Watcher (Overlay Edition)
--------------------------------------
A small always-on-top window sits in the corner of your screen,
watches for changes, asks Claude if an error is visible, and
shows the suggested fix right in the overlay.

SETUP:
    pip install mss pillow numpy anthropic

RUN:
    python screen_error_watcher.py

STOP:
    Click the ✕ on the overlay, or Ctrl+C in the terminal.
"""

import io
import os
import time
import base64
import threading
import queue

import numpy as np
from PIL import Image
import mss
from anthropic import Anthropic
import tkinter as tk

# ---------------- Settings ----------------
CHECK_INTERVAL = 2
DIFF_THRESHOLD = 5.0
COOLDOWN = 30
MODEL = "claude-haiku-4-5-20251001"   # cheaper than Sonnet — better for a $5 budget
WIN_W, WIN_H = 340, 170
# -------------------------------------------

api_key = os.environ.get("ANTHROPIC_API_KEY")
if not api_key:
    raise SystemExit("No API key found. Run: setx ANTHROPIC_API_KEY \"your-key-here\"")

client = Anthropic(api_key=api_key)
ui_queue = queue.Queue()
stop_flag = threading.Event()


def capture_screen():
    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[0])
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def image_diff_percent(img1, img2):
    a = np.array(img1.resize((200, 120)).convert("L"), dtype=np.int16)
    b = np.array(img2.resize((200, 120)).convert("L"), dtype=np.int16)
    return (np.sum(np.abs(a - b) > 25) / a.size) * 100


def encode_image(img):
    if img.width > 2000:
        ratio = 2000 / img.width
        img = img.resize((2000, int(img.height * ratio)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def ask_claude(img):
    b64 = encode_image(img)
    response = client.messages.create(
        model=MODEL,
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": (
                    "Look at this screenshot. Is there a visible error message, "
                    "crash dialog, warning, or exception on screen? "
                    "If yes, reply in exactly this format: "
                    "ERROR: <short description> | FIX: <short suggested fix>. "
                    "If no error is visible, reply with exactly: NO_ERROR"
                )},
            ],
        }],
    )
    return response.content[0].text.strip()


def watcher_loop():
    prev_img = capture_screen()
    last_error_text = None
    last_notify_time = 0.0

    while not stop_flag.is_set():
        time.sleep(CHECK_INTERVAL)
        current_img = capture_screen()
        diff = image_diff_percent(prev_img, current_img)
        prev_img = current_img

        if diff < DIFF_THRESHOLD:
            continue

        ui_queue.put(("status", f"Screen changed ({diff:.0f}%) — asking Claude..."))
        try:
            result = ask_claude(current_img)
        except Exception as e:
            ui_queue.put(("status", f"API error: {e}"))
            continue

        if result.startswith("NO_ERROR"):
            ui_queue.put(("status", "Watching... (no error found)"))
            continue

        if last_error_text and result[:40] == last_error_text[:40] and (time.time() - last_notify_time) < COOLDOWN:
            continue

        last_error_text = result
        last_notify_time = time.time()

        error_part, fix_part = result, ""
        if "| FIX:" in result:
            error_part, fix_part = result.split("| FIX:", 1)
        error_part = error_part.replace("ERROR:", "").strip()
        ui_queue.put(("error", (error_part, fix_part.strip())))


# ---------------- Overlay window ----------------
class Overlay:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)          # frameless
        self.root.attributes("-topmost", True)    # always on top
        self.root.attributes("-alpha", 0.92)
        self.root.configure(bg="#1e1e2e")

        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"{WIN_W}x{WIN_H}+{sw - WIN_W - 20}+{sh - WIN_H - 60}")

        # Title bar (draggable)
        bar = tk.Frame(self.root, bg="#11111b", height=26)
        bar.pack(fill="x")
        tk.Label(bar, text="🔍 Screen Error Watcher", bg="#11111b",
                 fg="#cdd6f4", font=("Segoe UI", 9, "bold")).pack(side="left", padx=8)
        tk.Button(bar, text="✕", bg="#11111b", fg="#f38ba8", bd=0,
                  command=self.close).pack(side="right", padx=6)
        bar.bind("<Button-1>", self.start_drag)
        bar.bind("<B1-Motion>", self.drag)

        self.status = tk.Label(self.root, text="Watching your screen...",
                               bg="#1e1e2e", fg="#a6e3a1",
                               font=("Segoe UI", 9), wraplength=WIN_W - 20,
                               justify="left", anchor="w")
        self.status.pack(fill="x", padx=10, pady=(6, 2))

        self.detail = tk.Label(self.root, text="", bg="#1e1e2e", fg="#cdd6f4",
                               font=("Segoe UI", 9), wraplength=WIN_W - 20,
                               justify="left", anchor="nw")
        self.detail.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        self.root.after(300, self.poll_queue)

    def start_drag(self, e):
        self._dx, self._dy = e.x, e.y

    def drag(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def poll_queue(self):
        try:
            while True:
                kind, payload = ui_queue.get_nowait()
                if kind == "status":
                    self.status.config(text=payload, fg="#a6e3a1")
                    self.detail.config(text="")
                elif kind == "error":
                    error_text, fix_text = payload
                    self.status.config(text=f"⚠ {error_text}", fg="#f38ba8")
                    self.detail.config(text=f"Fix: {fix_text}" if fix_text else "")
        except queue.Empty:
            pass
        self.root.after(300, self.poll_queue)

    def close(self):
        stop_flag.set()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    threading.Thread(target=watcher_loop, daemon=True).start()
    Overlay().run()