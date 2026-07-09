"""
Screen Error Watcher
--------------------
Watches your screen. When something visibly changes, it sends a screenshot
to Claude to check for an error/crash/warning dialog. If one is found,
it shows a Windows toast notification with a short suggested fix.

SETUP (run these in Command Prompt / PowerShell):
    pip install mss pillow numpy anthropic win11toast

    setx ANTHROPIC_API_KEY "your-api-key-here"
    (then close and reopen your terminal so the env var loads)

RUN:
    python screen_error_watcher.py

STOP:
    Ctrl+C in the terminal window
"""

import io
import os
import time
import base64

import numpy as np
from PIL import Image
import mss
from anthropic import Anthropic
from win11toast import notify

# ---------------- Settings you can tweak ----------------
CHECK_INTERVAL = 2       # seconds between checking if the screen changed
DIFF_THRESHOLD = 5.0     # % of screen that must change to trigger a Claude check
COOLDOWN = 30            # seconds before re-notifying about a very similar error
MODEL = "claude-sonnet-4-6"
# ----------------------------------------------------------

api_key = os.environ.get("ANTHROPIC_API_KEY")
if not api_key:
    raise SystemExit(
        "No API key found. Run: setx ANTHROPIC_API_KEY \"your-key-here\" "
        "then reopen your terminal."
    )

client = Anthropic(api_key=api_key)


def capture_screen():
    with mss.mss() as sct:
        monitor = sct.monitors[0]  # all monitors combined
        shot = sct.grab(monitor)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def image_diff_percent(img1, img2):
    # Downscale + grayscale for a cheap, fast comparison
    a = np.array(img1.resize((200, 120)).convert("L"), dtype=np.int16)
    b = np.array(img2.resize((200, 120)).convert("L"), dtype=np.int16)
    changed = np.sum(np.abs(a - b) > 25)
    return (changed / a.size) * 100


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


def main():
    print("Watching your screen for errors... (Ctrl+C to stop)")
    prev_img = capture_screen()
    last_error_text = None
    last_notify_time = 0.0

    while True:
        time.sleep(CHECK_INTERVAL)
        current_img = capture_screen()
        diff = image_diff_percent(prev_img, current_img)
        prev_img = current_img

        if diff < DIFF_THRESHOLD:
            continue

        print(f"Screen changed ({diff:.1f}%), checking with Claude...")
        try:
            result = ask_claude(current_img)
        except Exception as e:
            print(f"API error: {e}")
            continue

        if result.startswith("NO_ERROR"):
            continue

        if result == last_error_text and (time.time() - last_notify_time) < COOLDOWN:
            continue  # avoid spamming the same notification

        if last_error_text and result[:40] == last_error_text[:40] and (time.time() - last_notify_time) < COOLDOWN:
            continue  # avoid spamming the same notification

        print(f"Error detected: {result}")
        message = result.replace("ERROR:", "").replace("FIX:", "\nFix:").strip()
        notify("Error detected", message[:250])


if __name__ == "__main__":
    main()
