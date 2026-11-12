"""whisper-translate-realtime: live English captions for any program's audio.

Captures one program's audio (cs2.exe by default), translates speech to
English on the GPU with Whisper, and shows it in a click-through overlay and
in this console.

    run.bat                                        CS2 voice chat (the default)
    run.bat --process Microsoft.Media.Player.exe   whatever the Media Player app plays
    run.bat --language auto                        detect the spoken language per line
    run.bat --show-original                        also show the original text under each line
    run.bat --no-overlay                           console only (e.g. on a second monitor)
    run.bat --help                                 all options
"""

from __future__ import annotations

import argparse
import ctypes
import os
import queue
import re
import signal
import sys
import threading
import time
from datetime import datetime
from typing import Optional

import numpy as np

SR = 16000

# Phrases Whisper is known to invent on silence/noise (it was trained on
# subtitled video). Matched case-insensitively, punctuation stripped.
HALLUCINATION_PHRASES = (
    "thank you for watching", "thanks for watching", "thank you for your attention",
    "subtitles by", "subtitled by", "subtitles were", "amara org", "subscribe",
    "dimatorzok", "see you in the next video", "see you next time", "to be continued",
    "продолжение следует", "субтитр", "редактор", "корректор", "спасибо за просмотр", "подписывайтесь",
)
# Short outputs that are real sometimes but usually invented on noise; only
# dropped when Whisper itself is unsure there was speech.
SUSPICIOUS_SHORT = {"you", "thank you", "thank you very much", "thanks", "bye", "bye bye"}


# --------------------------------------------------------------------------- setup


def add_cuda_dlls() -> None:
    """Make cuBLAS from the nvidia-cublas-cu12 pip package loadable.
    (cuDNN already ships inside the ctranslate2 wheel on Windows.)"""
    try:
        import nvidia.cublas  # type: ignore
    except ImportError:
        return
    for base in nvidia.cublas.__path__:
        bin_dir = os.path.join(base, "bin")
        if os.path.isdir(bin_dir):
            os.add_dll_directory(bin_dir)
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Live English captions for any program's audio (default: CS2 voice chat).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    src = p.add_argument_group("audio source")
    src.add_argument("--process", default="cs2.exe", help="program whose audio to translate")
    src.add_argument("--pid", type=int, help="capture this process ID instead of --process")
    src.add_argument("--test-file", help="translate an audio file instead (played in real time)")

    tr = p.add_argument_group("translation")
    tr.add_argument("--language", default="ru", help='spoken language, or "auto" to detect per line')
    tr.add_argument("--model", default="large-v3",
                    help="Whisper model name (large-v3, medium, ...) or a folder holding a "
                         "CTranslate2/faster-whisper model you downloaded yourself")
    tr.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    tr.add_argument("--compute-type", default="float16", help="float16, int8_float16, int8 (cpu)")
    tr.add_argument("--beam-size", type=int, default=5)
    tr.add_argument("--show-original", action="store_true", help="also show the original-language text")
    tr.add_argument("--no-partials", dest="partials", action="store_false",
                    help="only show finished lines (less GPU use while someone is talking)")
    tr.add_argument("--silence", type=float, default=0.6, help="seconds of silence that end a line")
    tr.add_argument("--max-line", type=float, default=10.0, help="force a line out after this many seconds")
    tr.add_argument("--prompt", help="optional context/vocabulary hint passed to Whisper")

    ui = p.add_argument_group("display")
    ui.add_argument("--no-overlay", dest="overlay", action="store_false", help="console output only")
    ui.add_argument("--position", choices=["bottom", "top"], default="bottom")
    ui.add_argument("--offset", type=float, default=0.16, help="distance from that screen edge, as a fraction of height")
    ui.add_argument("--width", type=float, default=0.6, help="overlay width as a fraction of screen width")
    ui.add_argument("--font-size", type=int, default=22)
    ui.add_argument("--lines", type=int, default=3, help="max finished lines on screen")
    ui.add_argument("--hold", type=float, default=8.0, help="seconds each line stays on screen")
    ui.add_argument("--log", help="append translations to this text file")
    # Testing aids
    p.add_argument("--opaque", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--exit-after", type=float, help=argparse.SUPPRESS)
    return p.parse_args(argv)


# --------------------------------------------------------------------------- audio sources


class FileFeeder(threading.Thread):
    """Plays an audio file into the pipeline at real-time speed (for testing)."""

    def __init__(self, path: str, on_audio, on_status):
        super().__init__(daemon=True, name="file-feeder")
        self.path, self.on_audio, self.on_status = path, on_audio, on_status
        self.stop_event = threading.Event()
        self.error = None

    def stop(self):
        self.stop_event.set()

    def run(self):
        from faster_whisper import decode_audio

        try:
            audio = decode_audio(self.path, sampling_rate=SR)
        except Exception as exc:
            self.error = exc
            self.on_status(f"Could not read test file: {exc}")
            return
        self.on_status(f"Playing test file {os.path.basename(self.path)} ({len(audio) / SR:.1f}s)")
        step = int(0.05 * SR)
        for i in range(0, len(audio), step):
            if self.stop_event.is_set():
                return
            self.on_audio(audio[i:i + step])
            time.sleep(0.05)
        self.on_status("Test file finished")


# --------------------------------------------------------------------------- translation


def _looks_hallucinated(text: str, seg) -> bool:
    norm = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()
    if not norm:
        return True
    if any(p in norm for p in HALLUCINATION_PHRASES):
        return True
    if norm in SUSPICIOUS_SHORT and (seg.no_speech_prob > 0.2 or seg.avg_logprob < -0.6):
        return True
    return seg.no_speech_prob > 0.85


class Translator(threading.Thread):
    """Cuts the incoming audio into utterances with a voice detector and
    translates each one with Whisper. Emits events onto `events`:
        ("status", text) ("ready",) ("partial", text) ("final", english, original) ("error", text)
    """

    def __init__(self, args, audio_q: "queue.Queue[np.ndarray]", events: "queue.Queue[tuple]"):
        super().__init__(daemon=True, name="translator")
        self.args, self.audio_q, self.events = args, audio_q, events
        self.stop_event = threading.Event()
        self.language = None if args.language.lower() == "auto" else args.language
        self.silence_samples = int(args.silence * SR)
        self.max_samples = int(args.max_line * SR)
        self.pad = int(0.2 * SR)
        self.partial_shown = False
        self.last_partial_at = 0.0

    def stop(self):
        self.stop_event.set()

    def run(self):
        try:
            self._load()
            self._loop()
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            if "cublas" in msg.lower() or "cudnn" in msg.lower():
                msg += "\n  -> NVIDIA libraries missing. Run setup.bat again."
            elif self.args.device == "cuda" and "cuda" in msg.lower():
                msg += "\n  -> GPU problem. You can try: run.bat --device cpu --model small --compute-type int8"
            self.events.put(("error", msg))

    def _load(self):
        from faster_whisper import WhisperModel
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        self._get_speech = get_speech_timestamps
        self.vad_options = VadOptions(threshold=0.5, min_speech_duration_ms=200,
                                      min_silence_duration_ms=300, speech_pad_ms=0)
        local = os.path.isdir(self.args.model)
        self.events.put(("status", f"Loading Whisper {self.args.model} on {self.args.device}"
                                   + ("…" if local else " (downloads it first if not cached)…")))
        t = time.monotonic()
        self.model = WhisperModel(self.args.model, device=self.args.device, compute_type=self.args.compute_type)
        self._whisper(np.zeros(SR, np.float32), "translate", final=False)  # warm-up
        self.events.put(("status", f"Model ready in {time.monotonic() - t:.0f}s"))
        self.events.put(("ready",))

    def _loop(self):
        buf = np.zeros(0, np.float32)
        last_step = 0.0
        while not self.stop_event.is_set():
            try:
                parts = [self.audio_q.get(timeout=0.1)]
                while True:
                    try:
                        parts.append(self.audio_q.get_nowait())
                    except queue.Empty:
                        break
                buf = np.concatenate([buf, *parts])
            except queue.Empty:
                # The program stopped sending audio (Windows sends nothing when
                # an app is silent), so treat the gap as silence.
                if not len(buf):
                    continue
                buf = np.concatenate([buf, np.zeros(int(0.1 * SR), np.float32)])
            now = time.monotonic()
            if now - last_step >= 0.2:
                last_step = now
                buf = self._step(buf, now)

    def _step(self, buf: np.ndarray, now: float) -> np.ndarray:
        speech = self._get_speech(buf, vad_options=self.vad_options, sampling_rate=SR)
        if not speech:
            self._clear_partial()
            return buf[-int(0.5 * SR):]

        start = max(0, speech[0]["start"] - self.pad)
        end = speech[-1]["end"]

        if len(buf) - end >= self.silence_samples:  # speaker paused: line is done
            cut = min(len(buf), end + self.pad)
            self._final(buf[start:cut])
            return buf[cut:]

        if len(buf) - start >= self.max_samples:  # long monologue: cut at the last pause
            if len(speech) > 1:
                cut = min(speech[-2]["end"] + self.pad, speech[-1]["start"])
            else:
                cut = len(buf)
            self._final(buf[start:cut])
            return buf[cut:]

        if self.args.partials and now - self.last_partial_at >= 0.8 and end - start >= int(0.8 * SR):
            text = self._whisper(buf[start:], "translate", final=False)
            if text:
                self.events.put(("partial", text))
                self.partial_shown = True
            self.last_partial_at = time.monotonic()
        return buf

    def _final(self, audio: np.ndarray):
        self.last_partial_at = time.monotonic()
        english = self._whisper(audio, "translate", final=True) if len(audio) >= int(0.3 * SR) else ""
        if english:
            original = self._whisper(audio, "transcribe", final=True) if self.args.show_original else ""
            self.events.put(("final", english, original))
            self.partial_shown = False
        else:
            self._clear_partial()

    def _clear_partial(self):
        if self.partial_shown:
            self.events.put(("partial", ""))
            self.partial_shown = False

    def _whisper(self, audio: np.ndarray, task: str, final: bool) -> str:
        segments, _ = self.model.transcribe(
            audio,
            language=self.language,
            task=task,
            beam_size=self.args.beam_size if final else 1,
            temperature=[0.0, 0.2, 0.4] if final else 0.0,
            condition_on_previous_text=False,
            without_timestamps=True,
            vad_filter=False,
            initial_prompt=self.args.prompt,
        )
        texts = [s.text.strip() for s in segments if not _looks_hallucinated(s.text, s)]
        return " ".join(t for t in texts if t)


# --------------------------------------------------------------------------- output


class Console:
    """Prints and optionally logs events. Shared by overlay and console modes."""

    def __init__(self, log_path: Optional[str]):
        self.log = open(log_path, "a", encoding="utf-8") if log_path else None

    def handle(self, ev: tuple) -> None:
        kind = ev[0]
        if kind == "status":
            print(f"• {ev[1]}", flush=True)
        elif kind == "error":
            print(f"ERROR: {ev[1]}", file=sys.stderr, flush=True)
        elif kind == "final":
            stamp = datetime.now().strftime("%H:%M:%S")
            lines = [f"[{stamp}] {ev[1]}"] + ([f"           {ev[2]}"] if ev[2] else [])
            print("\n".join(lines), flush=True)
            if self.log:
                self.log.write("\n".join(lines) + "\n")
                self.log.flush()


def _enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware: crisp text, real pixels
    except (AttributeError, OSError):
        pass


def _make_click_through(hwnd: int) -> None:
    user32 = ctypes.windll.user32
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    GWL_EXSTYLE = -20
    WS_EX_TRANSPARENT, WS_EX_LAYERED, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x20, 0x80000, 0x80, 0x08000000
    style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE,
                             style | WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)


class Overlay:
    """Borderless, transparent, always-on-top, click-through caption window."""

    KEY = "#010203"  # this exact colour is rendered as fully transparent

    def __init__(self, args, events: "queue.Queue[tuple]", on_event, stop_flag: threading.Event):
        import tkinter as tk
        import tkinter.font as tkfont

        _enable_dpi_awareness()
        self.args, self.events, self.on_event, self.stop_flag = args, events, on_event, stop_flag
        self.root = root = tk.Tk()
        root.title("Whisper Translate")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        bg = "#303030" if args.opaque else self.KEY
        root.configure(bg=bg)
        if not args.opaque:
            root.attributes("-transparentcolor", self.KEY)

        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        self.w, self.h = int(sw * args.width), int(sh * 0.3)
        x = (sw - self.w) // 2
        y = sh - int(sh * args.offset) - self.h if args.position == "bottom" else int(sh * args.offset)
        root.geometry(f"{self.w}x{self.h}+{x}+{y}")
        self.geometry = (x, y, self.w, self.h)

        self.canvas = tk.Canvas(root, width=self.w, height=self.h, bg=bg, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.font = tkfont.Font(family="Segoe UI Semibold", size=args.font_size)
        self.partial_font = tkfont.Font(family="Segoe UI Semibold", size=args.font_size, slant="italic")
        self.small_font = tkfont.Font(family="Segoe UI", size=max(10, int(args.font_size * 0.7)))
        self.outline = max(2, args.font_size // 10)

        self.lines: list = []  # (expires_at, english, original)
        self.partial = ""
        self.status = ("", 0.0)
        self.next_expiry_check = 0.0
        self.next_topmost = 0.0

        root.update_idletasks()
        user32 = ctypes.windll.user32
        user32.GetParent.argtypes = [ctypes.c_void_p]
        user32.GetParent.restype = ctypes.c_void_p
        self.hwnd = user32.GetParent(root.winfo_id())
        _make_click_through(self.hwnd)

    def run(self):
        self.root.after(50, self._poll)
        self.root.mainloop()

    def _poll(self):
        changed = False
        now = time.monotonic()
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            self.on_event(ev)
            changed |= self._apply(ev, now)
        if now >= self.next_expiry_check:
            self.next_expiry_check = now + 0.5
            changed = True
        if now >= self.next_topmost:  # games sometimes push themselves above us
            self.next_topmost = now + 2.0
            self.root.attributes("-topmost", True)
        if changed:
            self._render(now)
        if self.stop_flag.is_set():
            self.root.destroy()
            return
        self.root.after(50, self._poll)

    def _apply(self, ev: tuple, now: float) -> bool:
        kind = ev[0]
        if kind == "final":
            self.lines.append((now + self.args.hold, ev[1], ev[2]))
            self.partial = ""
        elif kind == "partial":
            self.partial = ev[1]
        elif kind in ("status", "error"):
            self.status = (ev[1].splitlines()[0], now + (30 if kind == "error" else 4))
        else:
            return False
        return True

    def _render(self, now: float):
        c = self.canvas
        c.delete("all")
        self.lines = [ln for ln in self.lines if ln[0] > now][-self.args.lines:]
        blocks = []
        if self.status[0] and self.status[1] > now:
            blocks.append((self.status[0], self.small_font, "#9fd3ff"))
        for _, english, original in self.lines:
            blocks.append((english, self.font, "#ffffff"))
            if original:
                blocks.append((original, self.small_font, "#c8c8c8"))
        if self.partial:
            blocks.append((self.partial, self.partial_font, "#e8e2a8"))

        x = self.w // 2
        if self.args.position == "bottom":
            y = self.h - 8
            for text, font, fill in reversed(blocks):
                y -= self._text(x, y, text, font, fill, "s") + 6
        else:
            y = 8
            for text, font, fill in blocks:
                y += self._text(x, y, text, font, fill, "n") + 6

    def _text(self, x, y, text, font, fill, anchor) -> int:
        t = self.outline
        kw = dict(text=text, font=font, width=self.w - 40, anchor=anchor, justify="center")
        for dx, dy in ((-t, 0), (t, 0), (0, -t), (0, t), (-t, -t), (t, t), (-t, t), (t, -t)):
            self.canvas.create_text(x + dx, y + dy, fill="#000000", **kw)
        item = self.canvas.create_text(x, y, fill=fill, **kw)
        x0, y0, x1, y1 = self.canvas.bbox(item)
        return y1 - y0


# --------------------------------------------------------------------------- main


def main(argv=None) -> int:
    args = parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    add_cuda_dlls()

    audio_q: "queue.Queue[np.ndarray]" = queue.Queue()
    events: "queue.Queue[tuple]" = queue.Queue()
    stop_flag = threading.Event()
    console = Console(args.log)

    def status(msg: str):
        events.put(("status", msg))

    if args.test_file:
        source = FileFeeder(args.test_file, audio_q.put, status)
        label = args.test_file
    else:
        from process_audio import ProcessAudioCapture

        source = ProcessAudioCapture(on_audio=audio_q.put, process_name=None if args.pid else args.process,
                                     pid=args.pid, on_status=status)
        label = f"PID {args.pid}" if args.pid else args.process

    translator = Translator(args, audio_q, events)
    print(f"whisper-translate-realtime: {label} -> English  |  model {args.model} ({args.device})  |  "
          f"language {args.language}  |  Ctrl+C to quit", flush=True)

    signal.signal(signal.SIGINT, lambda *_: stop_flag.set())
    if args.exit_after:
        threading.Timer(args.exit_after, stop_flag.set).start()

    translator.start()
    # Start capturing once the model is ready so early audio isn't queued up.
    started_source = False

    def on_event(ev: tuple):
        nonlocal started_source
        if ev[0] == "ready" and not started_source:
            started_source = True
            source.start()
        elif ev[0] == "error" and not started_source:
            stop_flag.set()  # model failed to load; nothing useful left to do
        console.handle(ev)

    if args.overlay:
        overlay = Overlay(args, events, on_event, stop_flag)
        if args.opaque:
            print(f"overlay geometry {overlay.geometry}", flush=True)
        overlay.run()
    else:
        while not stop_flag.is_set():
            try:
                ev = events.get(timeout=0.2)
            except queue.Empty:
                continue
            on_event(ev)

    source.stop()
    translator.stop()
    # Drain anything still queued so the console/log is complete.
    while True:
        try:
            console.handle(events.get_nowait())
        except queue.Empty:
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
