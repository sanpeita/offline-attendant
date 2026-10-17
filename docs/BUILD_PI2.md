# Building `llama.cpp` on a Raspberry Pi 2 (armv7l, 942 MB)

Target: **Raspberry Pi 2 Model B Rev 1.1** — 32-bit ARM, 942 MB RAM, 4 cores.
Produces `bin/llama-server` (needed by `attendant.py`) and `bin/llama-completion`
(useful for one-shot testing).

Every number and every failure below was observed on this board. Where a value
was not measured it is marked unverified rather than estimated.

---

## 1. What the three walls are

**1. You cannot build in parallel.** `ninja` with no explicit job count uses
`nproc + 2 = 6` on this board. A single `-O3` `cc1plus` peaks at roughly 300 MB.
One translation unit (`common/arg.cpp`) took about seven minutes alone and drove
`MemAvailable` down to 139 MiB.

**2. 32-bit ARM has no inline 64-bit atomics.** The executable link fails with
`undefined reference to __atomic_load_8` / `__atomic_store_8` /
`__atomic_fetch_add_8`. Shared libraries link fine because undefined symbols are
permitted there, so the failure appears only at the very last step and looks
unrelated to anything you changed.

**3. Per-process memory accounting may not exist.** On this kernel,
`/proc/<pid>/status` has **no `VmRSS` / `VmHWM` rows at all** for tasks without a
memory map. A fail-closed gate built on those fields cannot distinguish
"unmeasurable" from "over budget", and refuses to proceed — the build runs
**zero steps**.

---

## 2. Build

```bash
# 1) -latomic must end up at the END of the link line.
#    -DCMAKE_EXE_LINKER_FLAGS=-latomic places it before the objects, where
#    Debian's default --as-needed discards it.
cmake -DCMAKE_CXX_STANDARD_LIBRARIES=-latomic .

# 2) One job at a time, detached so an SSH drop cannot kill a 75-minute build.
#    MemoryHigh is a SOFT cap. MemoryMax invites the OOM killer; do not use it.
sudo systemd-run --unit=llama-build --collect --no-block \
  --property=MemoryAccounting=yes --property=MemoryHigh=650M \
  --property=RuntimeMaxSec=10800 \
  ninja -j1 bin/llama-server bin/llama-completion
```

**Cost:** full build of the completion binary was **70–80 minutes** across
73–82 steps, 35–60 s per step. The heavy files are `src/llama-*.cpp` and
`common/arg.cpp`. Incremental restart is cheap; do not start over.

**Resource envelope (measured, whole build):**

| Metric | Value |
|---|---|
| Peak (service total) | 280,526,848 B ≈ **267 MiB** |
| `memory.swap.peak` | 0 |
| `oom_kill` | 0 |
| `MemAvailable` before → after | 485,752 kB → 530,052 kB |

---

## 3. Traps, in the order you will hit them

### `cmake: error while loading shared libraries: librhash.so.1`

If `cmake` comes from a vendored/relocated tree (a `.deb` extracted somewhere
non-standard), `ninja`'s post-processing step calls `cmake -E
cmake_symlink_library`, and that binary needs `<cmake-root>/usr/lib/<triplet>/`
on `LD_LIBRARY_PATH`. Without it, **only the symlink step fails** and the build
stops at, for example, `[9/82]`.

### `undefined reference to __atomic_load_8`

See wall 2. Only `-DCMAKE_CXX_STANDARD_LIBRARIES=-latomic` works. This puts the
flag at the end of the link line, after the objects.

### OOM with a bare `ninja`

See wall 1. `-j1`, always.

### `this custom template is not supported, try using --jinja`

Japanese instruct models ship a Jinja chat template. `--jinja` is required.
Note that some of these templates **do not support a system role** — passing one
is silently ignored (`Removing system prompt due to template not supporting
system role`), so fold your instruction into the user turn.

### `cannot find -lllama-server-impl` / `-lllama-cli-impl`

The unified `bin/llama` target is only built when the server is enabled. If you
only need one-shot generation, build `bin/llama-completion` and skip it.

### The measurement gate stops the build at zero steps

See wall 3. Move the safety valve to cgroup v2 (`MemoryHigh`, soft) and let the
build run detached. Before relaxing a threshold, ask whether the *instrument*
can be removed from the critical path instead.

On a board whose `cmdline.txt` contains `cgroup_disable=memory`, remove it (or
add `cgroup_enable=memory`) and reboot once. Verify with `cat
/sys/fs/cgroup/cgroup.controllers` containing `memory`.

---

## 4. Running it

**Resident (what the product uses):**

```bash
./bin/llama-server -m MODEL.gguf --host 127.0.0.1 --port 8080 \
  -t 4 -c 512 -n 256 --jinja
```

- **`-c 512` must be explicit.** Without it the server fails to start with a
  message about fitting parameters into available device memory.
- Bind to loopback only. This is a product requirement, not a preference.

**One-shot (for testing without the server):**

```bash
./bin/llama-completion -m MODEL.gguf -no-cnv \
  -p '### 指示:\n<input>\n\n### 応答:\n' \
  -n 64 -t 4 -c 512 --no-warmup --no-display-prompt   # stdin=/dev/null
```

- Do not pass `-p` together with interactive mode: stdout gets contaminated with
  lines like `> EOF by user`.
- **Do not let the child inherit stdin.** If the caller pre-reads stdin (a
  Python `input()`, for instance), the child sees EOF immediately. It works in a
  terminal and fails in a pipe — and therefore cannot be tested.

---

## 5. Reproduction checklist

- [ ] `-j1` explicit (a bare `ninja` runs 6 jobs and OOMs)
- [ ] `LD_LIBRARY_PATH` includes the vendored cmake's `usr/lib/<triplet>`
- [ ] configured with `-DCMAKE_CXX_STANDARD_LIBRARIES=-latomic` (not `EXE_LINKER_FLAGS`)
- [ ] long build inside a transient unit with `MemoryHigh` (never `MemoryMax`)
- [ ] `--jinja` at run time for a Jinja-template model
- [ ] `-c 512` explicit on the server
- [ ] `stdin=DEVNULL`, fixed argv, minimal env when spawning from an app
- [ ] no gate depends solely on `/proc/<pid>/status` RSS fields

---

## 6. Hardware and model (verified)

| Item | Value |
|---|---|
| Board | Raspberry Pi 2 Model B Rev 1.1 |
| Arch | armv7l, 32-bit |
| RAM / Swap | 942 MB / 942 MB |
| CPU features | NEON 1, ARM_FMA 1, LLAMAFILE 1, REPACK 1 |
| Engine | `llama.cpp` 0.6.0-dev, GNU 14.2.0 armv7l |
| Model | `llm-jp-3-150m-instruct3` Q8_0 GGUF, 164,258,048 B |
| Model SHA-256 | `6d3f0838dd2568ecf962a337e7302c8cfdf732312598f926f328d41594b82032` |

Unverified: why the unified `bin/llama` link did not pick up `libatomic`
(only `llama-completion` / `llama-server` were validated here); thermals and
throughput under continuous load.
