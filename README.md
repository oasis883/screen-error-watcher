# Screen Error Watcher

A lightweight Windows tool that watches your screen and sends you a toast notification — with an AI-suggested fix — the moment an error, crash dialog, or exception appears anywhere on any monitor.

I built this as a personal IT support aid: when an error pops up mid-task, the usual routine is read it, copy it, search it, and dig through results. This tool skips all of that — the diagnosis and a suggested fix appear in the corner of the screen within seconds, before you've even reached for the browser. Fewer clicks, faster fixes.

## How it works

```
![How it works](flow.png)

```

Design decisions:

- **Two-stage detection keeps costs low.** A pixel-diff runs locally every cycle for free; the paid vision API call only fires when the screen actually changed. Screenshots are JPEG-compressed and width-capped before upload.
- **Duplicate suppression is fuzzy, not exact.** The model describes the same error with slightly different wording each time, so exact-match dedup fails. Comparing the first 40 characters within a cooldown window stops notification spam.
- **Structured output contract.** The prompt forces a strict `ERROR: ... | FIX: ...` or `NO_ERROR` reply format, so the response can be parsed reliably without any fragile text analysis.

## Lessons learned the hard way

**The tool detected itself.** Early on, a deprecation warning appeared in the tool's own terminal — the watcher screenshotted it, printed "Error detected...", which changed the screen, which triggered another check, which found the same text still on screen. An alert loop, in miniature — the same class of problem as alert storms in real monitoring platforms.

Fixed two ways: the prompt now tells the model to ignore the watcher's own output lines, and the terminal runs minimised. It was a useful, concrete reminder of why production monitoring systems need self-exclusion rules and alert deduplication.

**Multi-monitor capture is not the default.** `mss` treats `monitors[1]` as the primary display only; `monitors[0]` is the combined virtual screen across all monitors. The first version silently ignored my second monitor — errors there were invisible to the tool. The fix also meant re-tuning the change threshold (the same dialog is a smaller *percentage* of a bigger screen) and capping image width so terminal text stays readable for the vision model.

## Setup

Windows 10/11, Python 3.9+.

```
pip install mss pillow numpy anthropic win11toast

setx ANTHROPIC_API_KEY "your-api-key-here"
```

Close and reopen your terminal after `setx`, then:

```
python screen_error_watcher.py
```

Stop with Ctrl+C. The API key is read from an environment variable — it is never hard-coded or committed.

## Test it

With the watcher running (terminal minimised), open another terminal on any monitor and type a broken command like `git m-`. Within a few seconds you should get a toast describing the error and suggesting a fix.

## Tunables

| Setting | Default | What it does |
|---|---|---|
| `CHECK_INTERVAL` | 5s | how often the screen diff runs |
| `DIFF_THRESHOLD` | 2.5% | how much change triggers an API check |
| `COOLDOWN` | 30s | suppress repeat alerts for similar errors |
| `MAX_WIDTH` | 2000px | downscale cap for multi-monitor captures |

## Costs & privacy

Each API check costs a fraction of a cent, but they add up — the change-detection stage exists to keep calls rare. Screenshots are sent to the Anthropic API for analysis and are not stored by this tool; be mindful of running it while sensitive information is on screen.

## License

MIT
