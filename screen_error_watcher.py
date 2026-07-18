"""
Screen Error Watcher (Overlay Edition + Chat + Pause) — Mission control theme
-----------------------------------------------------------------------------
A small always-on-top window sits in the corner of your screen.
It watches ONLY the monitor the overlay is currently on — drag the
window to another screen and it watches that one instead.

When the screen visibly changes, a screenshot is sent to Claude to
check for an error, crash dialog, warning, or exception. If one is
found, the error and a suggested fix appear right in the overlay —
and the watcher AUTO-PAUSES so the result stays on screen while you
read it, ask follow-ups, and work through the fix. Click the play
button (▶) in the title bar to resume watching.

CHAT: the "ask a follow-up..." field at the bottom lets you ask Claude
follow-up questions about the last detected error (e.g. "explain the
fix in more detail" or "that didn't work, what else can I try?").
Claude answers with full context: the screenshot from the moment the
error was detected, the original error/fix, and the chat so far.
Chat works while paused — it uses the saved context, not the live screen.

THEME: all colors live in the THEME dict below — tweak or swap the
whole palette without touching the UI code.

SETUP:
    pip install mss pillow numpy anthropic
    setx ANTHROPIC_API_KEY "your-key-here"   (then reopen the terminal)

RUN:
    python screen_error_watcher.py        (with terminal)
    pythonw screen_error_watcher.py       (no terminal window)

STOP:
    Click the X on the overlay, or Ctrl+C in the terminal.
"""

import base64
import ctypes
import io
import os
import queue
import threading
import time
import uuid
from pathlib import Path

import customtkinter as ctk
import mss
import numpy as np
from anthropic import Anthropic
from PIL import Image, ImageGrab


# ============================================================
# OPTIONAL FEATURES
# ============================================================

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False


try:
    import pytesseract

    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False


# ============================================================
# SETTINGS
# ============================================================

CHECK_INTERVAL = 2
DIFF_THRESHOLD = 0.5
COOLDOWN = 30

MODEL = "claude-sonnet-4-6"
CHAT_MAX_TOKENS = 700

WIN_W = 520
WIN_H = 620

MAX_ATTACHMENTS = 6

# OCR works only when pytesseract and the Tesseract application
# are installed. The images are still sent to Claude without OCR.
ENABLE_OCR = True

TITLE_TEXT = "SCREEN ERROR WATCHER"
PLACEHOLDER_TEXT = "ask a follow-up..."

THEME = {
    "bg": "#0b141d",
    "bar": "#05090e",
    "panel": "#101c27",
    "title": "#f5f7fa",
    "ok": "#9db4c7",
    "error": "#ff625b",
    "detail": "#e1e8ee",
    "warn": "#71889b",
    "question": "#f5f7fa",
    "input_bg": "#172432",
    "input_fg": "#edf2f6",
    "input_border": "#31475a",
    "placeholder": "#8296a8",
    "user_bubble": "#183247",
    "assistant_bubble": "#121f2a",
}


# ============================================================
# API SETUP
# ============================================================

api_key = os.environ.get("ANTHROPIC_API_KEY")

if not api_key:
    raise SystemExit(
        "No Anthropic API key was found.\n\n"
        "Run this command in PowerShell:\n\n"
        'setx ANTHROPIC_API_KEY "your-api-key"\n\n'
        "Then close and reopen PowerShell."
    )

client = Anthropic(api_key=api_key)


# ============================================================
# SHARED STATE
# ============================================================

ui_queue = queue.Queue()

stop_flag = threading.Event()
pause_flag = threading.Event()

overlay_pos = {
    "x": 100,
    "y": 100,
}

chat_lock = threading.Lock()

chat_context = {
    "screenshot_b64": None,
    "error_text": None,
    "history": [],
}


# ============================================================
# SCREEN CAPTURE
# ============================================================

def capture_screen():
    """Capture the monitor that currently contains the overlay."""

    with mss.mss() as screenshot_tool:
        x = overlay_pos["x"]
        y = overlay_pos["y"]

        monitor = screenshot_tool.monitors[1]

        for current_monitor in screenshot_tool.monitors[1:]:
            left = current_monitor["left"]
            top = current_monitor["top"]
            right = left + current_monitor["width"]
            bottom = top + current_monitor["height"]

            if left <= x < right and top <= y < bottom:
                monitor = current_monitor
                break

        screenshot = screenshot_tool.grab(monitor)

        return Image.frombytes(
            "RGB",
            screenshot.size,
            screenshot.bgra,
            "raw",
            "BGRX",
        )


def image_diff_percent(first_image, second_image):
    """Calculate how much the screen has visibly changed."""

    first_array = np.array(
        first_image.resize((400, 240)).convert("L"),
        dtype=np.int16,
    )

    second_array = np.array(
        second_image.resize((400, 240)).convert("L"),
        dtype=np.int16,
    )

    changed_pixels = np.abs(
        first_array - second_array
    ) > 15

    return (
        np.sum(changed_pixels)
        / changed_pixels.size
    ) * 100


def encode_image(image, quality=92):
    """Compress and encode an image for the Claude API."""

    image = image.convert("RGB")

    if image.width > 3000:
        ratio = 3000 / image.width

        image = image.resize(
            (
                3000,
                int(image.height * ratio),
            )
        )

    image_buffer = io.BytesIO()

    image.save(
        image_buffer,
        format="JPEG",
        quality=quality,
    )

    return base64.b64encode(
        image_buffer.getvalue()
    ).decode("utf-8")


# ============================================================
# EXTRA CONTEXT
# ============================================================

def active_window_title():
    """Return the active Windows application title."""

    if os.name != "nt":
        return ""

    try:
        window_handle = (
            ctypes.windll.user32.GetForegroundWindow()
        )

        title_length = (
            ctypes.windll.user32.GetWindowTextLengthW(
                window_handle
            )
        )

        title_buffer = ctypes.create_unicode_buffer(
            title_length + 1
        )

        ctypes.windll.user32.GetWindowTextW(
            window_handle,
            title_buffer,
            title_length + 1,
        )

        return title_buffer.value.strip()

    except Exception:
        return ""


def image_ocr(image):
    """Optionally extract text from an attached screenshot."""

    if not ENABLE_OCR:
        return ""

    if not OCR_AVAILABLE:
        return ""

    try:
        text = pytesseract.image_to_string(image)

        return text.strip()[:4000]

    except Exception:
        return ""


# ============================================================
# AUTOMATIC ERROR DETECTION
# ============================================================

def ask_claude(screenshot_b64):
    """
    Detect any visible technical error, warning, syntax issue,
    validation problem, failed state, or abnormal UI indicator.
    """

    response = client.messages.create(
        model=MODEL,
        max_tokens=220,
        system=(
            "You are an extremely strict visual software error detector. "
            "Inspect every visible part of the screenshot carefully, including "
            "small icons, editor gutters, status bars, notification areas, "
            "underlines, badges, tooltips, terminal text, query editors, forms, "
            "dialogs, browser pages, and application panels.\n\n"

            "Treat all of the following as errors or problems:\n"
            "- red X icons or gutter markers\n"
            "- red, orange, yellow, or blue squiggly underlines\n"
            "- syntax errors in code, SQL, PowerShell, terminals, or scripts\n"
            "- failed commands or failed queries\n"
            "- warnings, exceptions, crashes, timeouts, and connection failures\n"
            "- invalid fields, missing values, disabled actions, and validation messages\n"
            "- spelling or grammar errors visibly marked by an application\n"
            "- broken layouts, missing images, loading failures, and blank error states\n"
            "- authentication, permission, network, database, file, and application errors\n"
            "- any visible abnormal state, even if the indicator is very small\n\n"

            "Do not require an error popup. A small red marker, underline, icon, "
            "badge, or abnormal status is enough to report a problem.\n\n"

            "Ignore only the SCREEN ERROR WATCHER overlay itself.\n\n"

            "Reply in exactly one of these formats:\n"
            "ERROR: <clear visible problem, maximum 18 words> | "
            "FIX: <practical next step, maximum 25 words>\n"
            "or\n"
            "NO_ERROR\n\n"

            "Use NO_ERROR only when you are highly confident there is no visible "
            "problem anywhere in the screenshot."
        ),
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": screenshot_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "Examine the entire screenshot at high attention. "
                            "Look for errors of any size, including tiny editor markers, "
                            "red X icons, squiggly underlines, warning colors, invalid input, "
                            "failed commands, failed queries, exceptions, disabled actions, "
                            "status-bar warnings, broken UI elements, and subtle error indicators. "
                            "Do not ignore a problem merely because no dialog is open. "
                            "Return only ERROR: ... | FIX: ... or NO_ERROR."
                        ),
                    },
                ],
            }
        ],
    )

    return response.content[0].text.strip()


# ============================================================
# FOLLOW-UP REQUEST BUILDING
# ============================================================

def build_followup_turn(
    question,
    attachments,
    window_title,
    clipboard_text,
):
    """Build one multimodal Claude conversation turn."""

    with chat_lock:
        original_screenshot = (
            chat_context["screenshot_b64"]
        )

        error_text = chat_context["error_text"]

        history = list(
            chat_context["history"]
        )

    content = []

    # Include the original automatically detected screenshot
    # only in the first follow-up message.
    if not history and original_screenshot:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": original_screenshot,
                },
            }
        )

    # Include every newly pasted or dropped screenshot.
    for attachment in attachments:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": attachment["b64"],
                },
            }
        )

    context_parts = []

    if not history and error_text:
        context_parts.append(
            "Previously detected error:\n"
            f"{error_text}"
        )

    if window_title:
        context_parts.append(
            "Active window title:\n"
            f"{window_title}"
        )

    if clipboard_text:
        context_parts.append(
            "Clipboard text:\n"
            f"{clipboard_text[:3000]}"
        )

    for index, attachment in enumerate(
        attachments,
        start=1,
    ):
        if attachment.get("ocr"):
            context_parts.append(
                f"OCR from attached image {index}:\n"
                f"{attachment['ocr'][:2500]}"
            )

    context_parts.append(
        "User question:\n"
        f"{question or 'Please analyse the attached screenshot.'}"
    )

    content.append(
        {
            "type": "text",
            "text": "\n\n".join(
                context_parts
            ),
        }
    )

    user_turn = {
        "role": "user",
        "content": content,
    }

    return history, user_turn


def ask_claude_followup(
    question,
    attachments,
    window_title,
    clipboard_text,
):
    """Send a multimodal follow-up question to Claude."""

    history, user_turn = build_followup_turn(
        question,
        attachments,
        window_title,
        clipboard_text,
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=CHAT_MAX_TOKENS,
        system=(
            "You are a concise IT troubleshooting assistant "
            "inside a desktop overlay. Use the screenshots, "
            "active window title, clipboard text, OCR, detected "
            "error, and conversation history when provided. "
            "Compare previous and new screenshots when relevant. "
            "Give practical steps, exact commands, and exact menu "
            "paths. Use plain text and short paragraphs."
        ),
        messages=history + [user_turn],
    )

    answer = response.content[0].text.strip()

    with chat_lock:
        chat_context["history"].append(
            user_turn
        )

        chat_context["history"].append(
            {
                "role": "assistant",
                "content": answer,
            }
        )

    return answer


def chat_worker(
    question,
    attachments,
    window_title,
    clipboard_text,
):
    """Run Claude in a background thread."""

    try:
        answer = ask_claude_followup(
            question,
            attachments,
            window_title,
            clipboard_text,
        )

        ui_queue.put(
            (
                "answer",
                answer,
            )
        )

    except Exception as error:
        ui_queue.put(
            (
                "answer",
                f"API error: {error}",
            )
        )


# ============================================================
# AUTOMATIC WATCHER
# ============================================================

def watcher_loop():
    """Watch the screen and pause when an error is found."""

    try:
        previous_image = capture_screen()

    except Exception as error:
        ui_queue.put(
            (
                "status",
                f"Screen capture error: {error}",
            )
        )

        return

    last_error_text = None
    last_notification_time = 0.0

    while not stop_flag.is_set():
        time.sleep(CHECK_INTERVAL)

        if pause_flag.is_set():
            try:
                previous_image = capture_screen()

            except Exception as error:
                ui_queue.put(
                    (
                        "status",
                        f"Screen capture error: {error}",
                    )
                )

            continue

        try:
            current_image = capture_screen()

        except Exception as error:
            ui_queue.put(
                (
                    "status",
                    f"Screen capture error: {error}",
                )
            )

            continue

        difference = image_diff_percent(
            previous_image,
            current_image,
        )

        previous_image = current_image

        if difference < DIFF_THRESHOLD:
            continue

        ui_queue.put(
            (
                "status",
                f"Screen changed ({difference:.0f}%) — asking Claude...",
            )
        )

        screenshot_b64 = encode_image(
            current_image
        )

        try:
            result = ask_claude(
                screenshot_b64
            )

        except Exception as error:
            ui_queue.put(
                (
                    "status",
                    f"API error: {error}",
                )
            )

            continue

        if result.startswith("NO_ERROR"):
            ui_queue.put(
                (
                    "status",
                    "Watching... no error found",
                )
            )

            continue

        is_duplicate = (
            last_error_text
            and result[:40]
            == last_error_text[:40]
            and time.time()
            - last_notification_time
            < COOLDOWN
        )

        if is_duplicate:
            continue

        last_error_text = result
        last_notification_time = time.time()

        with chat_lock:
            chat_context["screenshot_b64"] = (
                screenshot_b64
            )

            chat_context["error_text"] = result

            chat_context["history"] = []

        # Preserve the original behaviour.
        pause_flag.set()

        error_part = result
        fix_part = ""

        if "| FIX:" in result:
            error_part, fix_part = result.split(
                "| FIX:",
                1,
            )

        error_part = error_part.replace(
            "ERROR:",
            "",
        ).strip()

        fix_part = fix_part.strip()

        ui_queue.put(
            (
                "error",
                (
                    error_part,
                    fix_part,
                ),
            )
        )


# ============================================================
# DRAG-AND-DROP ROOT
# ============================================================

if DND_AVAILABLE:

    class AppRoot(
        ctk.CTk,
        TkinterDnD.DnDWrapper,
    ):
        def __init__(self):
            ctk.CTk.__init__(self)

            self.TkdndVersion = (
                TkinterDnD._require(self)
            )

else:

    class AppRoot(ctk.CTk):
        pass


# ============================================================
# INTERFACE
# ============================================================

ctk.set_appearance_mode("dark")


class Overlay:
    def __init__(self):
        self.root = AppRoot()

        self.root.overrideredirect(True)

        self.root.attributes(
            "-topmost",
            True,
        )

        self.root.attributes(
            "-alpha",
            0.98,
        )

        self.root.configure(
            fg_color=THEME["bg"],
        )

        self.root.geometry(
            f"{WIN_W}x{WIN_H}+100+100"
        )

        self.root.minsize(
            WIN_W,
            WIN_H,
        )

        self.chat_busy = False

        self._drag_x = 0
        self._drag_y = 0

        self.placeholder_on = True

        self.attachments = []

        self.attachment_widgets = {}

        # Prevent CustomTkinter images from being garbage-collected.
        self.preview_refs = []

        self.shell = ctk.CTkFrame(
            self.root,
            fg_color=THEME["bg"],
            corner_radius=16,
            border_width=1,
            border_color="#263949",
        )

        self.shell.pack(
            fill="both",
            expand=True,
        )

        self.shell.pack_propagate(False)

        self.build_header()
        self.build_status()
        self.build_input()
        self.build_attachment_strip()
        self.build_conversation()

        # Ctrl+V first checks whether the clipboard contains an image.
        self.root.bind_all(
            "<Control-v>",
            self.handle_paste,
            add="+",
        )

        self.root.bind_all(
            "<Control-V>",
            self.handle_paste,
            add="+",
        )

        if DND_AVAILABLE:
            self.shell.drop_target_register(
                DND_FILES
            )

            self.shell.dnd_bind(
                "<<Drop>>",
                self.handle_drop,
            )

        self.add_message(
            "assistant",
            (
                "Watching your screen. Paste a screenshot with "
                "Ctrl+V, drag image files here, use the + button, "
                "or wait for an error to be detected automatically."
            ),
        )

        self.root.after(
            300,
            self.poll_queue,
        )

    # ========================================================
    # HEADER
    # ========================================================

    def build_header(self):
        self.header = ctk.CTkFrame(
            self.shell,
            height=46,
            corner_radius=14,
            fg_color=THEME["bar"],
        )

        self.header.pack(
            side="top",
            fill="x",
        )

        self.header.pack_propagate(False)

        self.title_area = ctk.CTkFrame(
            self.header,
            fg_color="transparent",
        )

        self.title_area.pack(
            side="left",
            fill="both",
            expand=True,
            padx=(14, 0),
        )

        self.search_icon = ctk.CTkLabel(
            self.title_area,
            text="⌕",
            width=25,
            text_color=THEME["title"],
            font=ctk.CTkFont(
                family="Segoe UI Symbol",
                size=22,
            ),
        )

        self.search_icon.pack(
            side="left",
            pady=8,
        )

        self.title_label = ctk.CTkLabel(
            self.title_area,
            text=TITLE_TEXT,
            text_color=THEME["title"],
            font=ctk.CTkFont(
                family="Segoe UI",
                size=13,
                weight="bold",
            ),
        )

        self.title_label.pack(
            side="left",
            padx=(4, 0),
            pady=8,
        )

        controls = ctk.CTkFrame(
            self.header,
            fg_color="transparent",
        )

        controls.pack(
            side="right",
            padx=(0, 9),
        )

        self.close_btn = ctk.CTkButton(
            controls,
            text="×",
            width=42,
            height=30,
            corner_radius=15,
            fg_color="transparent",
            hover_color="#2d1519",
            border_width=1,
            border_color=THEME["input_border"],
            text_color=THEME["error"],
            font=ctk.CTkFont(
                family="Segoe UI",
                size=18,
            ),
            command=self.close,
        )

        self.close_btn.pack(
            side="right",
            padx=(4, 0),
            pady=8,
        )

        self.pause_btn = ctk.CTkButton(
            controls,
            text="⏸",
            width=42,
            height=30,
            corner_radius=15,
            fg_color="transparent",
            hover_color="#152738",
            border_width=1,
            border_color=THEME["input_border"],
            text_color=THEME["ok"],
            font=ctk.CTkFont(
                family="Segoe UI Symbol",
                size=13,
            ),
            command=self.toggle_pause,
        )

        self.pause_btn.pack(
            side="right",
            pady=8,
        )

        for widget in (
            self.header,
            self.title_area,
            self.search_icon,
            self.title_label,
        ):
            widget.bind(
                "<Button-1>",
                self.start_drag,
            )

            widget.bind(
                "<B1-Motion>",
                self.drag,
            )

    # ========================================================
    # STATUS
    # ========================================================

    def build_status(self):
        self.status = ctk.CTkLabel(
            self.shell,
            text="Watching your screen...",
            text_color=THEME["ok"],
            anchor="w",
            justify="left",
            font=ctk.CTkFont(
                family="Segoe UI",
                size=12,
                weight="bold",
            ),
        )

        self.status.pack(
            fill="x",
            padx=16,
            pady=(10, 4),
        )

    # ========================================================
    # INPUT
    # ========================================================

    def build_input(self):
        self.input_bar = ctk.CTkFrame(
            self.shell,
            height=104,
            corner_radius=0,
            fg_color=THEME["bar"],
        )

        self.input_bar.pack(
            side="bottom",
            fill="x",
        )

        self.input_bar.pack_propagate(False)

        self.attach_btn = ctk.CTkButton(
            self.input_bar,
            text="+",
            width=40,
            height=40,
            corner_radius=20,
            fg_color="transparent",
            hover_color="#172b3d",
            border_width=1,
            border_color=THEME["input_border"],
            text_color=THEME["question"],
            font=ctk.CTkFont(
                size=22,
            ),
            command=self.choose_images,
        )

        self.attach_btn.pack(
            side="left",
            padx=(12, 7),
            pady=12,
        )

        self.input_box = ctk.CTkTextbox(
            self.input_bar,
            height=76,
            corner_radius=11,
            border_width=1,
            border_color=THEME["input_border"],
            fg_color=THEME["input_bg"],
            text_color=THEME["placeholder"],
            wrap="word",
            activate_scrollbars=True,
            font=ctk.CTkFont(
                family="Segoe UI",
                size=13,
            ),
        )

        self.input_box.pack(
            side="left",
            fill="both",
            expand=True,
            padx=(0, 7),
            pady=12,
        )

        self.input_box.insert(
            "1.0",
            PLACEHOLDER_TEXT,
        )

        self.input_box.bind(
            "<FocusIn>",
            self.clear_placeholder,
        )

        self.input_box.bind(
            "<FocusOut>",
            self.restore_placeholder,
        )

        self.input_box.bind(
            "<Return>",
            self.handle_enter,
        )

        self.send_btn = ctk.CTkButton(
            self.input_bar,
            text="➤",
            width=46,
            height=46,
            corner_radius=23,
            fg_color="transparent",
            hover_color="#172b3d",
            border_width=1,
            border_color=THEME["input_border"],
            text_color=THEME["question"],
            font=ctk.CTkFont(
                family="Segoe UI Symbol",
                size=20,
            ),
            command=self.send_question,
        )

        self.send_btn.pack(
            side="right",
            padx=(0, 12),
            pady=12,
        )

    # ========================================================
    # ATTACHMENT STRIP
    # ========================================================

    def build_attachment_strip(self):
        self.attachment_strip = (
            ctk.CTkScrollableFrame(
                self.shell,
                orientation="horizontal",
                height=92,
                fg_color=THEME["panel"],
                corner_radius=10,
                scrollbar_button_color="#243747",
                scrollbar_button_hover_color="#314a5e",
            )
        )

        self.attachment_strip.pack(
            fill="x",
            padx=12,
            pady=(0, 7),
        )

        # Hide until an attachment is added.
        self.attachment_strip.pack_forget()

    # ========================================================
    # CONVERSATION
    # ========================================================

    def build_conversation(self):
        self.conversation = (
            ctk.CTkScrollableFrame(
                self.shell,
                fg_color="transparent",
                corner_radius=0,
                scrollbar_button_color="#243747",
                scrollbar_button_hover_color="#314a5e",
            )
        )

        self.conversation.pack(
            fill="both",
            expand=True,
            padx=10,
            pady=(0, 6),
        )

        self.conversation.grid_columnconfigure(
            0,
            weight=1,
        )

    def add_message(
        self,
        role,
        text,
        images=None,
    ):
        """Add a message bubble to the conversation."""

        row = ctk.CTkFrame(
            self.conversation,
            fg_color="transparent",
        )

        row.grid(
            sticky="ew",
            padx=3,
            pady=5,
        )

        row.grid_columnconfigure(
            0,
            weight=1,
        )

        is_user = role == "user"

        bubble = ctk.CTkFrame(
            row,
            fg_color=(
                THEME["user_bubble"]
                if is_user
                else THEME["assistant_bubble"]
            ),
            corner_radius=12,
            border_width=1,
            border_color="#294052",
        )

        bubble.grid(
            row=0,
            column=0,
            sticky="e" if is_user else "w",
            padx=(
                (70, 0)
                if is_user
                else (0, 70)
            ),
        )

        role_label = ctk.CTkLabel(
            bubble,
            text=(
                "You"
                if is_user
                else "Claude"
            ),
            text_color=THEME["ok"],
            font=ctk.CTkFont(
                size=10,
                weight="bold",
            ),
            anchor="w",
        )

        role_label.pack(
            fill="x",
            padx=10,
            pady=(7, 0),
        )

        if images:
            image_row = ctk.CTkFrame(
                bubble,
                fg_color="transparent",
            )

            image_row.pack(
                fill="x",
                padx=8,
                pady=(6, 0),
            )

            for image in images:
                thumbnail = image.copy()

                thumbnail.thumbnail(
                    (110, 70)
                )

                ctk_image = ctk.CTkImage(
                    light_image=thumbnail,
                    dark_image=thumbnail,
                    size=thumbnail.size,
                )

                self.preview_refs.append(
                    ctk_image
                )

                image_button = ctk.CTkButton(
                    image_row,
                    text="",
                    image=ctk_image,
                    width=thumbnail.width,
                    height=thumbnail.height,
                    fg_color="transparent",
                    hover_color="#22384a",
                    command=lambda selected_image=image.copy(): (
                        self.open_preview(
                            selected_image
                        )
                    ),
                )

                image_button.pack(
                    side="left",
                    padx=3,
                )

        message_label = ctk.CTkLabel(
            bubble,
            text=text,
            wraplength=360,
            justify="left",
            anchor="w",
            text_color=THEME["detail"],
            font=ctk.CTkFont(
                family="Segoe UI",
                size=12,
            ),
        )

        message_label.pack(
            fill="x",
            padx=10,
            pady=(5, 9),
        )

        self.root.after(
            50,
            self.scroll_conversation_bottom,
        )

    def scroll_conversation_bottom(self):
        try:
            self.conversation._parent_canvas.yview_moveto(
                1.0
            )
        except Exception:
            pass

    # ========================================================
    # PLACEHOLDER
    # ========================================================

    def clear_placeholder(self, _event=None):
        if self.placeholder_on:
            self.input_box.delete(
                "1.0",
                "end",
            )

            self.input_box.configure(
                text_color=THEME["input_fg"],
            )

            self.placeholder_on = False

    def restore_placeholder(self, _event=None):
        current_text = self.input_box.get(
            "1.0",
            "end-1c",
        ).strip()

        if not current_text:
            self.input_box.delete(
                "1.0",
                "end",
            )

            self.input_box.insert(
                "1.0",
                PLACEHOLDER_TEXT,
            )

            self.input_box.configure(
                text_color=THEME["placeholder"],
            )

            self.placeholder_on = True

    # ========================================================
    # KEYBOARD
    # ========================================================

    def handle_enter(self, event):
        # Shift+Enter inserts a normal new line.
        if event.state & 0x0001:
            return None

        self.send_question()

        return "break"

    # ========================================================
    # CLIPBOARD
    # ========================================================

    def clipboard_text(self):
        try:
            value = self.root.clipboard_get()

            return value.strip()[:3000]

        except Exception:
            return ""

    def handle_paste(self, _event=None):
        """
        Paste an image when the clipboard contains a screenshot.

        Normal text pasting continues to work when the clipboard
        does not contain an image.
        """

        try:
            clipboard_content = (
                ImageGrab.grabclipboard()
            )

        except Exception:
            return None

        if isinstance(
            clipboard_content,
            Image.Image,
        ):
            self.add_attachment(
                clipboard_content.copy(),
                name=(
                    f"clipboard-"
                    f"{len(self.attachments) + 1}.png"
                ),
            )

            return "break"

        # Windows may return a list of copied files.
        if isinstance(
            clipboard_content,
            list,
        ):
            added_image = False

            for item in clipboard_content:
                path = Path(item)

                if path.suffix.lower() in {
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".bmp",
                    ".webp",
                }:
                    self.add_image_file(
                        path
                    )

                    added_image = True

            if added_image:
                return "break"

        # Returning None allows normal text paste.
        return None

    # ========================================================
    # DRAG AND DROP
    # ========================================================

    def handle_drop(self, event):
        try:
            paths = self.root.tk.splitlist(
                event.data
            )

        except Exception:
            paths = [event.data]

        for raw_path in paths:
            path = Path(
                str(raw_path).strip("{}")
            )

            if path.suffix.lower() in {
                ".png",
                ".jpg",
                ".jpeg",
                ".bmp",
                ".webp",
            }:
                self.add_image_file(
                    path
                )

    # ========================================================
    # FILE PICKER
    # ========================================================

    def choose_images(self):
        from tkinter import filedialog

        paths = filedialog.askopenfilenames(
            title="Attach screenshots",
            filetypes=[
                (
                    "Image files",
                    "*.png *.jpg *.jpeg *.bmp *.webp",
                ),
                (
                    "All files",
                    "*.*",
                ),
            ],
        )

        for raw_path in paths:
            self.add_image_file(
                Path(raw_path)
            )

    # ========================================================
    # ATTACHMENTS
    # ========================================================

    def add_image_file(self, path):
        try:
            with Image.open(path) as image:
                self.add_attachment(
                    image.copy(),
                    path.name,
                )

        except Exception as error:
            self.status.configure(
                text=(
                    f"Could not attach "
                    f"{path.name}: {error}"
                ),
                text_color=THEME["error"],
            )

    def add_attachment(
        self,
        image,
        name,
    ):
        if len(self.attachments) >= MAX_ATTACHMENTS:
            self.status.configure(
                text=(
                    f"Maximum {MAX_ATTACHMENTS} "
                    "images per message."
                ),
                text_color=THEME["warn"],
            )

            return

        image = image.convert("RGB")

        attachment_id = uuid.uuid4().hex

        attachment = {
            "id": attachment_id,
            "name": name,
            "image": image,
            "b64": encode_image(image),
            "ocr": image_ocr(image),
        }

        self.attachments.append(
            attachment
        )

        self.render_attachment(
            attachment
        )

        self.attachment_strip.pack(
            fill="x",
            padx=12,
            pady=(0, 7),
        )

        self.status.configure(
            text=(
                f"{len(self.attachments)} "
                "screenshot(s) attached"
            ),
            text_color=THEME["ok"],
        )

    def render_attachment(
        self,
        attachment,
    ):
        card = ctk.CTkFrame(
            self.attachment_strip,
            width=130,
            height=70,
            fg_color="#142330",
            corner_radius=9,
            border_width=1,
            border_color=THEME["input_border"],
        )

        card.pack(
            side="left",
            padx=4,
            pady=2,
        )

        card.pack_propagate(False)

        thumbnail = attachment[
            "image"
        ].copy()

        thumbnail.thumbnail(
            (82, 52)
        )

        ctk_image = ctk.CTkImage(
            light_image=thumbnail,
            dark_image=thumbnail,
            size=thumbnail.size,
        )

        self.preview_refs.append(
            ctk_image
        )

        preview_btn = ctk.CTkButton(
            card,
            text="",
            image=ctk_image,
            width=88,
            height=58,
            fg_color="transparent",
            hover_color="#21384a",
            command=lambda selected_image=attachment[
                "image"
            ].copy(): self.open_preview(
                selected_image
            ),
        )

        preview_btn.pack(
            side="left",
            padx=(5, 1),
            pady=5,
        )

        remove_btn = ctk.CTkButton(
            card,
            text="×",
            width=25,
            height=25,
            corner_radius=12,
            fg_color="transparent",
            hover_color="#361a1d",
            text_color=THEME["error"],
            command=lambda attachment_id=attachment[
                "id"
            ]: self.remove_attachment(
                attachment_id
            ),
        )

        remove_btn.pack(
            side="right",
            padx=(1, 5),
            pady=5,
        )

        self.attachment_widgets[
            attachment["id"]
        ] = card

    def remove_attachment(
        self,
        attachment_id,
    ):
        self.attachments = [
            item
            for item in self.attachments
            if item["id"] != attachment_id
        ]

        attachment_widget = (
            self.attachment_widgets.pop(
                attachment_id,
                None,
            )
        )

        if attachment_widget:
            attachment_widget.destroy()

        if not self.attachments:
            self.attachment_strip.pack_forget()

            self.status.configure(
                text=(
                    "Watching your screen..."
                    if not pause_flag.is_set()
                    else "Paused — screen not being watched"
                ),
                text_color=(
                    THEME["ok"]
                    if not pause_flag.is_set()
                    else THEME["warn"]
                ),
            )

    # ========================================================
    # IMAGE PREVIEW
    # ========================================================

    def open_preview(self, image):
        preview = ctk.CTkToplevel(
            self.root
        )

        preview.title(
            "Screenshot preview"
        )

        preview.attributes(
            "-topmost",
            True,
        )

        preview.geometry(
            "900x650"
        )

        display_image = image.copy()

        display_image.thumbnail(
            (860, 590)
        )

        ctk_image = ctk.CTkImage(
            light_image=display_image,
            dark_image=display_image,
            size=display_image.size,
        )

        self.preview_refs.append(
            ctk_image
        )

        image_label = ctk.CTkLabel(
            preview,
            text="",
            image=ctk_image,
        )

        image_label.pack(
            fill="both",
            expand=True,
            padx=15,
            pady=15,
        )

    # ========================================================
    # SENDING
    # ========================================================

    def get_question(self):
        if self.placeholder_on:
            return ""

        return self.input_box.get(
            "1.0",
            "end-1c",
        ).strip()

    def send_question(self):
        question = self.get_question()

        if self.chat_busy:
            return

        if not question and not self.attachments:
            return

        with chat_lock:
            has_original_context = (
                chat_context["error_text"]
                is not None
            )

        if (
            not has_original_context
            and not self.attachments
        ):
            self.status.configure(
                text=(
                    "Attach a screenshot or wait for "
                    "an error to be detected."
                ),
                text_color=THEME["warn"],
            )

            return

        attachment_snapshot = [
            {
                "name": item["name"],
                "image": item["image"].copy(),
                "b64": item["b64"],
                "ocr": item["ocr"],
            }
            for item in self.attachments
        ]

        images_for_bubble = [
            item["image"]
            for item in attachment_snapshot
        ]

        self.add_message(
            "user",
            (
                question
                or "Please analyse these screenshots."
            ),
            images=images_for_bubble,
        )

        self.input_box.delete(
            "1.0",
            "end",
        )

        self.placeholder_on = False

        self.restore_placeholder()

        # Remove the current attachment cards after copying
        # their content for the request.
        attachment_ids = [
            item["id"]
            for item in self.attachments
        ]

        for attachment_id in attachment_ids:
            self.remove_attachment(
                attachment_id
            )

        self.chat_busy = True

        self.send_btn.configure(
            state="disabled",
            text="•••",
        )

        self.status.configure(
            text="Claude is analysing...",
            text_color=THEME["ok"],
        )

        threading.Thread(
            target=chat_worker,
            args=(
                question,
                attachment_snapshot,
                active_window_title(),
                self.clipboard_text(),
            ),
            daemon=True,
        ).start()

    # ========================================================
    # WINDOW DRAGGING
    # ========================================================

    def start_drag(self, event):
        self._drag_x = (
            event.x_root
            - self.root.winfo_x()
        )

        self._drag_y = (
            event.y_root
            - self.root.winfo_y()
        )

    def drag(self, event):
        self.root.geometry(
            f"+{event.x_root - self._drag_x}"
            f"+{event.y_root - self._drag_y}"
        )

    # ========================================================
    # PAUSE AND RESUME
    # ========================================================

    def toggle_pause(self):
        if pause_flag.is_set():
            pause_flag.clear()

            self.pause_btn.configure(
                text="⏸",
                text_color=THEME["ok"],
            )

            self.status.configure(
                text="Watching your screen...",
                text_color=THEME["ok"],
            )

        else:
            pause_flag.set()

            self.pause_btn.configure(
                text="▶",
                text_color=THEME["title"],
            )

            self.status.configure(
                text="Paused — screen not being watched",
                text_color=THEME["warn"],
            )

    # ========================================================
    # QUEUE
    # ========================================================

    def poll_queue(self):
        overlay_pos["x"] = (
            self.root.winfo_x()
            + 10
        )

        overlay_pos["y"] = (
            self.root.winfo_y()
            + 10
        )

        try:
            while True:
                kind, payload = (
                    ui_queue.get_nowait()
                )

                if kind == "status":
                    self.status.configure(
                        text=payload,
                        text_color=THEME["ok"],
                    )

                elif kind == "error":
                    error_text, fix_text = payload

                    self.pause_btn.configure(
                        text="▶",
                        text_color=THEME["title"],
                    )

                    self.status.configure(
                        text=(
                            "Error detected — "
                            "monitoring paused"
                        ),
                        text_color=THEME["error"],
                    )

                    message = (
                        f"⚠ {error_text}"
                    )

                    if fix_text:
                        message += (
                            f"\n\nFix: {fix_text}"
                        )

                    message += (
                        "\n\nPress ▶ to resume watching."
                    )

                    self.add_message(
                        "assistant",
                        message,
                    )

                elif kind == "answer":
                    self.chat_busy = False

                    self.send_btn.configure(
                        state="normal",
                        text="➤",
                    )

                    self.status.configure(
                        text=(
                            "Ready for another follow-up"
                        ),
                        text_color=THEME["ok"],
                    )

                    self.add_message(
                        "assistant",
                        payload,
                    )

        except queue.Empty:
            pass

        self.root.after(
            300,
            self.poll_queue,
        )

    # ========================================================
    # CLOSE
    # ========================================================

    def close(self):
        stop_flag.set()

        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    try:
        print(
            "Starting Screen Error Watcher..."
        )

        app = Overlay()

        watcher_thread = threading.Thread(
            target=watcher_loop,
            daemon=True,
        )

        watcher_thread.start()

        print(
            "Screen Error Watcher is running."
        )

        if not DND_AVAILABLE:
            print(
                "Drag and drop is disabled. Install tkinterdnd2 "
                "to enable it."
            )

        if not OCR_AVAILABLE:
            print(
                "OCR is disabled. Install pytesseract and "
                "Tesseract OCR to enable it."
            )

        app.run()

    except Exception:
        import traceback

        print(
            "\nAPPLICATION ERROR:\n"
        )

        traceback.print_exc()

        input(
            "\nPress Enter to close..."
        )