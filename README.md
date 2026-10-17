# offline-attendant

A front-desk attendant that runs on a 2015-vintage Raspberry Pi 2 (32-bit ARM, **942 MB RAM**) with **no internet connection and no subscription**.

It does not chat. It sits inside a small menu and, at fixed moments, says one short line — a greeting, an acknowledgement, a closing line — generated locally by a 150M-parameter open-weight model through `llama.cpp`.

```
menu ──import──> attendant.line("greeting")
                     │
                     ├─ ok      → one generated line (≈3.1 s)
                     └─ failure → the fixed fallback line, silently
```

## Who pays for this

Small workshops, studios, maker spaces, reception desks. Not people who want a chatbot: people who want a screen near the door that feels **attended** rather than blank, who have one unreliable Wi-Fi router, and who do not want the words spoken around their front desk retained on someone else's server.

The commercial argument is the constraint itself. A cloud assistant at the front desk is a monthly bill, a network dependency, and a third-party retention policy. This is a one-time hardware cost and a box that keeps working when the router dies.

## Why open source is load-bearing here

- **Zero marginal cost.** Each line is a few seconds of one CPU core. Metered per call, a greeter becomes a subscription — exactly what this customer is avoiding.
- **The hardware is the reason you need the whole stack.** On 942 MB you must control quantization, context size (512, and it must be *explicit*), thread count, and above all **whether the model stays loaded**. That last one was a 3.3× difference. None of it is an API parameter.
- **Privacy is structural.** Loopback bind only; no outbound port exists to accidentally enable.
- **Legacy hardware is a market.** Closed stacks tell owners of 32-bit boards to buy new hardware. `llama.cpp` builds on armv7 if you know about `-j1` and `-latomic`.

## Requirements

- Raspberry Pi 2 Model B (armv7l, 942 MB) — other small boards should work but are untested
- `llama.cpp` built with `bin/llama-server` (see [`docs/BUILD_PI2.md`](docs/BUILD_PI2.md))
- An open-weight GGUF model. Tested with `llm-jp-3-150m-instruct3` **Q8_0** (157 MiB)
- Python 3.11+ (standard library only)

## Install

```bash
git clone https://github.com/sanpeita/offline-attendant.git
cd offline-attendant
cp config.example.toml config.toml
$EDITOR config.toml          # engine path, model path, the lines you want it to say
python3 attendant.py selftest
```

`selftest` never starts the engine. It checks the config, the model file and the output sanitiser, and prints exactly what the menu will show when inference is unavailable.

## Use

From the menu, in-process (recommended — see "Why not shell out" below):

```python
from attendant import Attendant

attendant = Attendant.from_config("config.toml")
attendant.ensure_started()          # no-op after the first call; never retries
print(attendant.line("greeting"))   # always returns a string
```

From a shell, for debugging:

```bash
python3 attendant.py line greeting
python3 attendant.py health
python3 attendant.py stop
```

## The contract this program keeps

These are not features. They are the promises the product is sold on, and each one is enforced in code:

| Promise | How |
|---|---|
| The menu always opens | `line()` never raises and never blocks longer than `line_budget_s` |
| Never invent a line when inference is down | failures return `fallback[kind]`, and that is logged at most once |
| Never silently re-grab resources | `ensure_started()` starts **once**; there is no restart, no retry, no backoff loop |
| Only one place starts the engine | an exclusive `flock` on `runtime/attendant.lock` |
| Nothing leaves the machine | the bind address is validated as loopback; non-loopback config is refused at load |
| The model never sees user input | the prompt is built from a fixed template + a label + the clock, nothing else |
| Output cannot corrupt the screen | control characters stripped, whitespace collapsed, length capped |

## Measured on the target board

Full tables with conditions in [`docs/MEASUREMENTS.md`](docs/MEASUREMENTS.md).

| | One process per line | Resident |
|---|---|---|
| 24-token line | 10.22 s | **3.096 s** |
| 40-token greeting | 15.39 s | **4.293 s** |
| Generation | 8.72–9.68 tok/s | 9.4–9.8 tok/s |
| RSS | ~209 MB peak | ~207 MiB |
| Real drop in free memory | ~46 MB | ~54 MB |
| Swap activity | none | none |
| Cold start | paid every time | 12 s, once |

Generation itself is only 1.3–2.0 s. Everything else in the one-shot number is loading and tearing down the model. The `-c 512` flag is mandatory: without it the server fails to start on this board.

## Honest limits

- A 150M model is **not smart**. Short lines only; it will state facts wrongly. In acceptance testing it wrote "as of 1:55 PM" when it was 17:31. Always render a real clock separately.
- The **first line after boot is never generated**: a 12-second cold start does not fit a 5-second line budget, so it falls back by design. The second line onward is real inference.
- Resident operation **with the desktop running** is unverified. All resident numbers above were taken with the desktop stopped.
- Long-run thermals and stability are **unmeasured**.
- **No auto-restart, on purpose.** If the resident process dies, the attendant goes quiet rather than silently taking resources back. Quiet and honest beats alive and lying.

## Why not shell out to the model

Calling `llama-completion` as a subprocess per line works, and was the first implementation. It has two traps that cost real time:

1. **The child inherits stdin and reads EOF immediately** if the caller has already buffered stdin (a Python `input()`, for example). It works in a TTY and fails in a pipe, which means it also cannot be tested.
2. Ten seconds of model loading per line is not a latency problem, it is a product defect.

So: import in-process, and if you do spawn anything, `stdin=DEVNULL` with fixed argv and a minimal environment.

## Prior work

The build knowledge used here — the `-j1` requirement, the `-latomic` link-order trap, `--jinja` for the Japanese template, the 12-second cold start, the 3.096-second resident line — comes from my own private lab notes from earlier in October 2026, where `llama.cpp` was first brought up on this specific board and resident mode was measured against one-process-per-request.

This repository was written during the Hacktoberfest 2026 challenge window (October 17–18, 2026). **No code was copied from those notes**; what crossed over is measurements and lessons, reproduced in [`docs/`](docs/) with their conditions attached. The prior work was never published, so nothing here derives from a third party's project.

## Credits

- [`llama.cpp`](https://github.com/ggml-org/llama.cpp) — MIT, unmodified
- **llm-jp-3** 150M instruct (Q8_0 GGUF, 157 MiB) — open-weight Japanese model, unmodified

## License

MIT — see [`LICENSE`](LICENSE).
