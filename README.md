# whisper-translate-realtime

Live English captions for whatever a program on your PC is saying, translated while people talk. It was built for Russian voice chat in Counter-Strike 2, but it works with any app (Discord, a browser, the Media Player app…) and any of the ~99 languages Whisper understands. Everything runs locally on your GPU with [faster-whisper](https://github.com/SYSTRAN/faster-whisper), and no audio leaves your PC.

- **One app only.** It captures a single program's audio (`cs2.exe` by default), so music and other apps don't get mixed in. It uses Windows' per-app audio capture, the same API as OBS's "Application Audio Capture". It finds the program by name in the Windows process list and never opens, reads, hooks or injects into it.
- **Near real time.** A voice detector splits speech into lines. Each line is translated to English with Whisper `large-v3` about a second after the speaker pauses, and a live preview (yellow italic) shows while they're still talking.
- **Overlay.** Captions appear in a transparent, click-through, always-on-top window that never takes focus, and also in the console.

## Requirements

- Windows 10 version 2004 or newer, or Windows 11
- An NVIDIA GPU. `large-v3` needs about 4–5 GB of video memory (tested on an RTX 5080). Without a GPU, use `--device cpu` with a smaller model.
- Python 3.10 or newer (tested with 3.14) with the `py` launcher

## Setup

1. Clone or download this repository to a short path such as `C:\whisper-translate-realtime`. pip fails on very long paths unless Windows long-path support is on.
2. Run `setup.bat`. It creates `.venv` in the folder and installs the Python packages (about 1 GB).
3. The first run downloads the Whisper model (about 3 GB) to `%USERPROFILE%\.cache\huggingface`, unless you point `--model` at a model you already have.

## Use with CS2

1. In CS2, set **Video → Display Mode → Fullscreen Windowed**. Exclusive fullscreen draws over every other window, including the overlay.
2. Double-click `run.bat`. You can start it before or after CS2: it waits for `cs2.exe` to start. If you restart CS2, restart this too.
3. Press **Ctrl+C** in the console window to quit.

## Other apps

Pass the program's `.exe` name, as shown in Task Manager's Details tab, with `--process`. For example:

- `run.bat --process Microsoft.Media.Player.exe` captions whatever plays in the Windows Media Player app, which is handy for testing with a video.
- `run.bat --process discord.exe` captions Discord calls.
- `run.bat --process chrome.exe` captions Chrome.

When several processes share the name, as with Chrome and Discord, it uses the first one Windows lists, which is normally the main process. Audio from that process's child processes is included.

## Other languages

Whisper translates from about 99 languages, but only into English.

- `--language ru` is the default. `uk`, `pl`, `pt`, `tr`, `es`, `de` and the other Whisper language codes work the same way. Naming the language is the most reliable option when you know it.
- `--language auto` detects the language of each line, which is useful in mixed lobbies. It often guesses wrong on short callouts like "rush B".

Quality depends on the language. Widely spoken languages come out about as well as Russian, and less common ones noticeably worse.

## Using your own model

`--model` takes a Whisper model name (downloaded on first use) or a folder you downloaded yourself:

```
run.bat --model "D:\models\faster-whisper-large-v3"
```

The folder must hold a **CTranslate2 (faster-whisper)** model, meaning `model.bin`, `config.json`, `tokenizer.json` and `vocabulary.*`, for example `Systran/faster-whisper-large-v3`.

- Hugging Face Transformers checkpoints (`model.safetensors`), which is how most fine-tuned models are shared, must be converted first with `ct2-transformers-converter`. See the faster-whisper README.
- OpenAI `.pt` files and whisper.cpp (GGML) files won't load.
- Avoid `large-v3-turbo`. It was trained without translation data and tends to output the original language instead of English. Models fine-tuned only to transcribe one language can have the same problem.

## Options

| Option | What it does |
| --- | --- |
| `--process NAME.exe` | Program whose audio to translate (default `cs2.exe`) |
| `--language CODE` | Spoken language (default `ru`), or `auto` to detect each line |
| `--show-original` | Also shows the original-language text under each English line |
| `--no-partials` | Skips the live preview, so the GPU only works when a line finishes |
| `--no-overlay` | Console only, e.g. on a second monitor |
| `--font-size 26`, `--lines 4`, `--hold 10` | Text size, lines kept on screen, seconds each line stays |
| `--position top --offset 0.1` | Puts the captions near the top of the screen |
| `--log chat.txt` | Saves every translated line to a file |
| `--model`, `--device`, `--compute-type` | Which model to use, and on what hardware and precision |
| `--test-file clip.wav` | Translates an audio file instead of a running program |

Run `run.bat --help` for the full list.

## Tips

- **FPS:** Whisper shares the GPU with your game. If you notice stutter, try `--no-partials`, then `--compute-type int8_float16`, then `--model medium`.
- **Game sounds:** the voice detector filters out gunfire and footsteps, and the script drops lines Whisper is known to invent from noise (e.g. "Thank you for watching"). Turning the in-game music down still helps.
- **Slang and callouts** are translated literally, so expect some odd lines. `--prompt "Counter-Strike callouts: A site, B site, mid, rush, plant, defuse, eco"` can help, but it sometimes makes Whisper repeat the prompt during silence.

## How it works

`process_audio.py` looks the program up by name in the Windows process list once, then uses the Windows process-loopback API to get a copy of its audio (including its child processes) at 16 kHz. `whisper_translate.py` runs the Silero voice detector (bundled with faster-whisper) on it every 0.2 s. When a speaker pauses for 0.6 s (`--silence`), or has talked for 10 s without a pause (`--max-line`), that stretch of audio goes to Whisper's translate-to-English mode. The result is filtered and shown.

## Files

| File | Purpose |
| --- | --- |
| `whisper_translate.py` | Main program: voice detection, translation, overlay |
| `process_audio.py` | Captures one program's audio through Windows process loopback |
| `run.bat` | Starts it, passing any options through |
| `setup.bat` | Creates `.venv` and installs the packages in `requirements.txt` |
