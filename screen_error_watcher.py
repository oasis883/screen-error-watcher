"""
Screen Error Watcher
--------------------
Watches all monitors. When the screen visibly changes, it sends a screenshot
to Claude (Anthropic API) to check for an error, crash, warning, or exception.
If one is found, it shows a Windows toast notification with a short suggested fix.

SETUP (Command Prompt / PowerShell):
    pip install mss pillow numpy anthropic win11toast

    setx ANTHROPIC_API_KEY "your-api-key-here"
    (close and reopen your terminal so the environment variable loads)

RUN:
    python screen_error_watcher.py

STOP:
    Ctrl+C in the terminal window

TIP: minimise this terminal while it runs — see "self-monitoring" note in README.
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
CHECK_INTERVAL = 5       # seconds between checking if the screen changed
DIFF_THRESHOLD = 2.5     # % of screen that must change to trigger a Claude check
COOLDOWN = 30            # seconds before re-notifying about a similar error
MAX_WIDTH = 2000         # downscale wide multi-monitor captures for the API
MODEL = "claude-sonnet-4-6"
# ----------------------------------------------------------

api_key = os.environ.get("ANTHROPIC_API_KEY")
if not api_key:
    raise SystemExit(
        'No API key found. Run: setx ANTHROPIC_API_KEY "your-key-here" '
        "then reopen your terminal."
    )

client = Anthropic(api_key=api_key)

# Newer mss releases expose MSS(); older ones use the lowercase factory.
_MSS = getattr(mss, "MSS", None) or mss.mss

PROMPT = (
    "Look at this screenshot of my desktop (it may span multiple monitors). "
    "Is there a visible error message, crash dialog, warning, or exception "
    "on screen? Ignore any lines starting with 'Screen changed', "
    "'Error detected' or 'Watching your screen' - those come from this "
    "monitoring tool itself, not from a real problem. "
    "If a real error is visible, reply in exactly this format: "
    "ERROR: <short description> | FIX: <short suggested fix>. "
    "If no error is visible, reply with exactly: NO_ERROR"
)


def capture_screen():
    """Capture all monitors as one image (monitors[0] = combined virtual screen)."""
    with _MSS() as sct:
        shot = sct.grab(sct.monitors[0])
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def image_diff_percent(img1, img2):
    """Cheap change detection: downscale + grayscale, count changed pixels."""
    a = np.array(img1.resize((200, 120)).convert("L"), dtype=np.int16)
    b = np.array(img2.resize((200, 120)).convert("L"), dtype=np.int16)
    changed = np.sum(np.abs(a - b) > 25)
    return (changed / a.size) * 100


def encode_image(img):
    """JPEG-encode, capping width so multi-monitor captures stay readable for the API."""
    if img.width > MAX_WIDTH:
        ratio = MAX_WIDTH / img.width
        img = img.resize((MAX_WIDTH, int(img.height * ratio)))
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
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": PROMPT},
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

        # Fuzzy duplicate suppression: the model words the same error slightly
        # differently each time, so compare only the start of the description.
        if (last_error_text
                and result[:40] == last_error_text[:40]
                and (time.time() - last_notify_time) < COOLDOWN):
            continue

        last_error_text = result
        last_notify_time = time.time()

        print(f"Error detected: {result}")
        message = result.replace("ERROR:", "").replace("FIX:", "\nFix:").strip()
        notify("Error detected", message[:250])


if __name__ == "__main__":
    main()
