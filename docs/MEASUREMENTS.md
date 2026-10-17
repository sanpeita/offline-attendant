# Measurements

Board: Raspberry Pi 2 Model B Rev 1.1 — armv7l, **942 MB RAM**, 4 cores.
Engine: `llama.cpp` 0.6.0-dev (`-O3 -mfpu=neon-vfpv4 -mfloat-abi=hard`).
Model: llm-jp-3 150M instruct3, **Q8_0 GGUF**, 164,258,048 B (157 MiB).
Generation flags: `-t 4 -c 512 --jinja`.

Conditions are stated for every table. Where something was not measured, it says
so — nothing here is estimated or extrapolated from another machine.

---

## 1. One process per line (the first implementation)

Each line spawned `llama-completion`, loaded the model, generated, exited.

| Metric | Value |
|---|---|
| Generation | **8.72 / 9.62 / 9.68 tok/s** |
| Prompt evaluation | 24.1 – 27.2 tok/s |
| Model load | 658 – 667 ms |
| 24-token line, end to end | **10.22 s** |
| 64-token greeting, end to end | **15.39 s** |
| Peak RSS | ~209 MB |
| Real drop in free memory | **~46 MB** |
| Swap (`pswpin`/`pswpout`) | unchanged |

The number that matters: generation itself is **1.3 – 2.0 s** of those 10–15 s.
The remainder is loading and tearing down the model, every time.

RSS of 209 MB versus a 46 MB real drop is not a contradiction — the weights are
`mmap`ed, so most of that RSS is reclaimable page cache. Swap never moving means
the board is not thrashing.

---

## 2. Resident

`llama-server --host 127.0.0.1 -t 4 -c 512 -n 256 --jinja`, desktop **stopped**.

| Metric | Value |
|---|---|
| Cold start until it accepts requests | **12 s** (paid once) |
| Resident RSS | **~207 MiB** |
| Real drop in free memory | **~54 MB** |
| 24-token line | **3.096 s** |
| 40-token greeting | **4.293 s** |
| Swap | unchanged |

**3.3× faster** than one-shot for the same 24-token line, with identical
hardware and model. The only change is not re-reading the file.

Generation in resident mode measured **9.4 – 9.8 tok/s**. At that rate a 5-second
budget allows roughly 40 tokens, which is why greetings are 40 tokens and
acknowledgements are 24. The *length* is capped, not the clock: a timeout would
turn a slow answer into a missing one.

---

## 3. The admission gate that was wrong

Generation appeared broken for days. The cause was an admission check:
*do not use the model unless at least 512 MiB is free*.

| Condition | MemAvailable | Gate (512 MiB) | Result |
|---|---|---|---|
| Desktop running | 473 – 487 MB | fail | never generated |
| Desktop stopped | 581 – 635 MB | pass | generated |

The gate could not pass while the desktop was up — not occasionally, but
**structurally, every time, by 40–50 MB**.

Lowering the threshold until it passes would be luck, not engineering. What the
measurements actually support:

- Real drop in free memory: **~46 MB**
- Swap: **flat**
- Peak RSS: ~209 MB

So "512 MiB" was never measured on this board; it was inherited. It was replaced
with a floor derived from observation — **256 MiB** (peak RSS + headroom) — and
the fail-closed behaviour was kept: below the floor, no line is generated.

Known separate item: a 512 MiB requirement still exists in the **deployment**
preflight for the surrounding system. It is not the generation floor and was left
alone deliberately, because deployment is a different risk.

---

## 4. Build envelope

| Metric | Value |
|---|---|
| Wall time, completion binary | 70 – 80 min |
| Steps | 73 – 82 (35 – 60 s each) |
| Peak, whole service | 280,526,848 B ≈ **267 MiB** |
| `memory.swap.peak` | 0 |
| `oom_kill` | 0 |
| Slowest single unit | `common/arg.cpp`, ~7 min, `MemAvailable` → 139 MiB |

---

## 5. Device acceptance failures (the expensive ones)

Three read-only preflight runs on the actual hardware failed in a row, all with
the same class of defect: **a rename applied on one side only.**

| # | Defect | Symptom |
|---|---|---|
| 1 | Payload key renamed on writer, reader read old key | key missing at run time; deployment never started |
| 2 | Release-name contract still required the old version string | every run on the device stopped on that line |
| 3 | Identifier group structure changed 3 parts → 2; validator required the removed field | every run stopped on that line |

Local tests missed all three because they asserted that **a value was present**,
not that **a real value is accepted and a wrong one rejected**. Fixing the tests
to pin one real fixture and assert both directions removed the class.

A fourth, environment-only failure: the preflight requires swap not to move for
five seconds. It failed once with 600 MB free — leftover processes from the
*stopped* desktop session touched swapped-out pages and pulled them back in. Not
memory pressure; history. After confirming stillness, the same preflight passed
**87/87**.

Diagnostics note: the device-side bootstrap catches exceptions and returns only
the type, which is a reasonable safety choice that also deletes the message. The
cause was found by replaying the identical byte sequence locally with tracebacks
enabled. Failing safe and failing legibly are separate properties.

---

## 6. Model quality observations (unflattering, therefore included)

- In acceptance testing the model wrote "as of 1:55 PM" when the actual time was
  **17:31**. It states times confidently and wrongly.
- Mitigation in place: a correct clock line is rendered separately whenever the
  model does not state a time, so the screen still tells the truth. The wrong
  time inside the generated sentence remains an open issue.
- 150M parameters is enough for a one-line acknowledgement and not enough for
  facts. The design assumes this rather than fighting it.

---

## 7. Not measured

- Resident operation **with the desktop running**.
- Continuous operation beyond a single session; thermals, throttling, SD-card
  wear.
- Behaviour of a `Q4_K_M` quantization on this board (expected to free ~80 MB;
  **not measured**, so do not quote it as a number).
- Long-term output stability across thousands of generated lines.
