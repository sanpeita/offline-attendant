#!/usr/bin/env python3
"""offline-attendant — one short line, generated on the machine, offline.

The menu imports this module and calls ``Attendant.line(kind)``. That call
always returns a string within ``line_budget_s`` and never raises. If local
inference is unavailable, slow, or produces nothing usable, the fixed
fallback for that kind is returned instead.

Design rules, each enforced below rather than documented and hoped for:

  1. The menu always opens.  ``line()`` has a hard deadline and a fallback.
  2. Never invent a line.    Failure returns the fallback, not a partial or
                             generated-looking guess.
  3. Never re-grab resources. ``ensure_started()`` starts once. No restart,
                             no retry loop, no backoff.
  4. One starter only.       An exclusive flock on the runtime lock file.
  5. Nothing leaves the box. A non-loopback bind address is refused at load.
  6. No user input in the prompt. Fixed template + label + clock, nothing else.

Standard library only. Python 3.11+ (tomllib).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import tomllib
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

try:                      # POSIX only. The module must still import on Windows
    import fcntl           # so that selftest and the test suite can run there.
except ImportError:        # pragma: no cover - platform dependent
    fcntl = None  # type: ignore[assignment]


LOOPBACK = {"127.0.0.1", "::1", "localhost"}

# Tokens are the budget unit because generation speed is the limit, not the
# timeout. 40 tokens at ~9.5 tok/s is 4.2 s, which fits a 5 s line budget; a
# longer answer cannot, so the length is capped instead of the clock.
MAX_CHARS = 80

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS = re.compile(r"\s+")


class ConfigError(Exception):
    """A configuration problem that must be fixed, not worked around."""


@dataclass
class Config:
    engine: Path
    model: Path
    host: str = "127.0.0.1"
    port: int = 8080
    threads: int = 4
    ctx: int = 512          # mandatory on 942 MB: the default fails to start
    n_predict: int = 48
    cold_start_s: float = 30.0
    line_budget_s: float = 5.0
    runtime_dir: Path = Path("runtime")
    lines: dict[str, str] = field(default_factory=dict)
    fallback: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate on every construction path, not just Config.load().

        A config built in code (tests, embedding) must be held to the same
        rules as one read from disk, or the loopback guarantee is decorative.
        """
        if self.host not in LOOPBACK:
            raise ConfigError(
                f"host must be a loopback address ({', '.join(sorted(LOOPBACK))}); "
                f"got {self.host!r}. This program is sold on staying local."
            )
        if not (0 < self.port < 65536):
            raise ConfigError(f"port out of range: {self.port}")
        if not self.lines:
            raise ConfigError("no [lines] entries in config")
        missing = [k for k in self.lines if k not in self.fallback]
        if missing:
            raise ConfigError(f"[lines] without a [fallback] entry: {missing}")
        if self.line_budget_s <= 0:
            raise ConfigError("line_budget_s must be positive")

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "Config":
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)

        return cls(
            engine=Path(os.path.expanduser(str(raw.get("engine", "")))),
            model=Path(os.path.expanduser(str(raw.get("model", "")))),
            host=str(raw.get("host", "127.0.0.1")),
            port=int(raw.get("port", 8080)),
            threads=int(raw.get("threads", 4)),
            ctx=int(raw.get("ctx", 512)),
            n_predict=int(raw.get("n_predict", 48)),
            cold_start_s=float(raw.get("cold_start_s", 30.0)),
            line_budget_s=float(raw.get("line_budget_s", 5.0)),
            runtime_dir=Path(raw.get("runtime_dir", "runtime")),
            lines=dict(raw.get("lines", {})),
            fallback=dict(raw.get("fallback", {})),
        )


def sanitize(text: str, limit: int = MAX_CHARS) -> str:
    """Make model output safe to draw on a fixed-width display."""
    text = _CONTROL.sub("", text)
    text = text.replace("```", " ")
    text = _WS.sub(" ", text).strip()
    # Drop a leading label the model sometimes repeats back at us.
    text = re.sub(r"^(?:応答|回答|assistant|アシスタント)\s*[:：]\s*", "", text)
    if len(text) > limit:
        cut = text[:limit]
        # Prefer a natural break; never leave half a surrogate pair or a comma.
        for sep in ("。", "！", "？", ". ", "、"):
            idx = cut.rfind(sep)
            if idx >= limit // 2:
                cut = cut[: idx + len(sep)]
                break
        text = cut.rstrip()
    return unicodedata.normalize("NFC", text)


class Attendant:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock_fh: Any = None
        self._started = False          # rule 3: start once, then remember
        self._start_failed = False
        self._warned = False

    @classmethod
    def from_config(cls, path: str | os.PathLike[str]) -> "Attendant":
        return cls(Config.load(path))

    # ---------------------------------------------------------------- startup

    def ensure_started(self) -> bool:
        """Start the engine if that has never been attempted. Idempotent.

        Returns True if the engine is believed to be serving. A failure is
        permanent for this process: we do not retry, because retrying is how
        a quiet attendant turns into a machine that grabs its memory back in
        the middle of someone's shift.
        """
        if self._started or self._start_failed:
            return self._started
        self._started = self._guard_and_start()
        if not self._started:
            self._start_failed = True
        return self._started

    def _guard_and_start(self) -> bool:
        # Rule 4: exactly one starter. Anyone else leaves it alone.
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.cfg.runtime_dir / "attendant.lock"
        if fcntl is not None:
            self._lock_fh = open(lock_path, "w")
            try:
                fcntl.flock(self._lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return self._health()
        else:  # pragma: no cover - non-POSIX fallback, best effort only
            # Exclusive create. It cannot detect a stale lock from a crashed
            # process, which is why the product ships on Linux.
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                self._lock_fh = os.fdopen(fd, "w")
            except OSError:
                return self._health()

        if self._health():
            return True
        if not self.cfg.engine.exists():
            self._log(f"engine not found: {self.cfg.engine}")
            return False
        if not self.cfg.model.exists():
            self._log(f"model not found: {self.cfg.model}")
            return False

        argv = [
            str(self.cfg.engine),
            "-m", str(self.cfg.model),
            "--host", self.cfg.host,
            "--port", str(self.cfg.port),
            "-t", str(self.cfg.threads),
            "-c", str(self.cfg.ctx),
            "-n", str(self.cfg.n_predict),
            "--jinja",
        ]
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": os.environ.get("HOME", "/"),
            "LANG": "C.UTF-8",
        }
        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,   # never inherit a caller's stdin
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            self._log(f"engine failed to spawn: {exc}")
            return False

        deadline = time.monotonic() + self.cfg.cold_start_s
        while time.monotonic() < deadline:
            if self._health():
                return True
            if self._proc.poll() is not None:
                self._log(f"engine exited during startup: rc={self._proc.returncode}")
                return False
            time.sleep(0.25)
        self._log(f"engine did not become healthy within {self.cfg.cold_start_s:.0f}s")
        return False

    def _health(self) -> bool:
        try:
            with urllib.request.urlopen(self._url("/health"), timeout=2.0) as rsp:
                return rsp.status == 200
        except (urllib.error.URLError, OSError, TimeoutError):
            return False

    # ------------------------------------------------------------------- line

    def line(self, kind: str) -> str:
        """One line for the menu. Always returns; never raises (rule 1)."""
        fallback = self.cfg.fallback.get(kind) or next(iter(self.cfg.fallback.values()))
        try:
            if not self.ensure_started():
                return self._fall_back(kind, fallback)
            prompt = self._prompt(kind)
            started = time.monotonic()
            text = self._complete(prompt, self.cfg.line_budget_s)
            if time.monotonic() - started > self.cfg.line_budget_s:
                # Answer arrived but too late to be worth showing slowly.
                return self._fall_back(kind, fallback)
            text = sanitize(text)
            return text or self._fall_back(kind, fallback)
        except Exception as exc:  # rule 1: the menu outranks this program
            self._log(f"line({kind!r}) failed: {type(exc).__name__}: {exc}")
            return fallback

    def _prompt(self, kind: str) -> str:
        # Rule 6: no caller-supplied text ever reaches the model.
        instruction = self.cfg.lines.get(kind, "")
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        return (
            f"### 指示:\n{instruction}\n"
            f"現在時刻は {now} です。\n\n"
            f"### 応答:\n"
        )

    def _complete(self, prompt: str, timeout: float) -> str:
        body = json.dumps(
            {
                "prompt": prompt,
                "n_predict": self.cfg.n_predict,
                "temperature": 0.7,
                "top_p": 0.9,
                "stop": ["### 指示:", "\n###", "応答:"],
                "stream": False,
                "cache_prompt": False,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url("/completion"),
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as rsp:
            payload = json.loads(rsp.read().decode("utf-8"))
        content = payload.get("content")
        if not isinstance(content, str):
            raise ValueError("no content in completion response")
        return content

    def _url(self, path: str) -> str:
        host = self.cfg.host
        if host == "localhost":
            host = "127.0.0.1"
        return f"http://{host}:{self.cfg.port}{path}"

    # ------------------------------------------------------------------ misc

    def _fall_back(self, kind: str, fallback: str) -> str:
        # Rule 2: say it plainly, once, and stop. Repeated warnings train
        # people to ignore warnings.
        if not self._warned:
            self._log(f"inference unavailable; serving fixed line for {kind!r}")
            self._warned = True
        return fallback

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        self._started = False

    def close(self) -> None:
        """Release the starter lock. Does not stop an engine it did not start."""
        self.stop()
        if self._lock_fh is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self._lock_fh, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                self._lock_fh.close()
            except OSError:
                pass
            self._lock_fh = None

    def _log(self, message: str) -> None:
        try:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
            with open(self.cfg.runtime_dir / "attendant.log", "a", encoding="utf-8") as fh:
                fh.write(f"{stamp} {message}\n")
        except OSError:
            pass


# ---------------------------------------------------------------------- CLI


def _selftest(path: str) -> int:
    cfg = Config.load(path)
    problems: list[str] = []
    if not cfg.engine.exists():
        problems.append(f"engine missing: {cfg.engine}")
    if not cfg.model.exists():
        problems.append(f"model missing: {cfg.model}")
    else:
        size = cfg.model.stat().st_size
        if size > 400 * 1024 * 1024:
            problems.append(f"model is {size / 2**20:.0f} MiB; this board expects ~157 MiB")
    if cfg.line_budget_s > 8:
        problems.append(
            f"line_budget_s={cfg.line_budget_s}: a menu line should not wait that long"
        )
    for kind, text in cfg.fallback.items():
        if not text.strip():
            problems.append(f"empty fallback for {kind!r}")
        if sanitize(text) != text:
            problems.append(f"fallback for {kind!r} is not display-safe: {text!r}")

    print(f"config      : {path}")
    print(f"bind        : {cfg.host}:{cfg.port} (loopback-only enforced)")
    print(f"ctx         : {cfg.ctx}  threads: {cfg.threads}  n_predict: {cfg.n_predict}")
    print(f"kinds       : {', '.join(sorted(cfg.lines))}")
    for kind in sorted(cfg.fallback):
        print(f"fallback[{kind}] = {cfg.fallback[kind]!r}")
    if problems:
        print("\nPROBLEMS")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nselftest: ok (engine not started)")
    return 0


def main(argv: list[str]) -> int:
    default = Path(__file__).with_name("config.toml")
    if len(argv) < 2:
        print(__doc__.strip().splitlines()[0])
        print("usage: attendant.py <selftest|health|line KIND|stop> [config.toml]")
        return 2

    command = argv[1]
    path = argv[2] if len(argv) > 2 else str(default)
    if not Path(path).exists() and command == "selftest":
        path = str(Path(__file__).with_name("config.example.toml"))

    if command == "selftest":
        return _selftest(path)

    attendant = Attendant.from_config(path)
    if command == "line":
        kind = argv[3] if len(argv) > 3 else "greeting"
        started = time.monotonic()
        text = attendant.line(kind)
        elapsed = time.monotonic() - started
        print(text)
        print(f"# {elapsed:.3f}s", file=sys.stderr)
        return 0
    if command == "health":
        ok = attendant.ensure_started()
        print("serving" if ok else "unavailable")
        return 0 if ok else 1
    if command == "stop":
        attendant.stop()
        print("stopped")
        return 0

    print(f"unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
