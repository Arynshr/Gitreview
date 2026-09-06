"""gitscribe sandbox — manages the local model used by `validation.ai`
(`gitscribe verify`'s AI review pass, and `gitscribe review --sandboxed`'s
local path).

Scope note: this module downloads/tracks *models* (GGUF files) but
deliberately does NOT install llama.cpp itself. That's an asymmetric
decision, not a shortcut:

- llama.cpp is a third-party executable. Installing it automatically would
  mean downloading and *running* a binary this tool can't cryptographically
  verify ahead of time (GitHub doesn't publish official checksums for its
  release assets), and llama.cpp's release asset naming has already
  drifted more than once - code that reverse-engineers it needs ongoing
  upkeep to not silently break. It also already has legitimate,
  well-maintained install paths (Homebrew, distro packages, official
  releases, build-from-source) that are strictly better than anything
  reimplemented here.
- A GGUF model file is inert data, not an executable - a corrupted or
  substituted one produces bad output, not arbitrary code execution the
  way a substituted binary would. That's a meaningfully smaller trust
  expansion, and there's no equivalent "just brew install it" for a
  specific model file, so managing that download is genuine
  gitscribe-specific value.

`gitscribe sandbox init` therefore *detects* llama-server (via PATH) and
fails with an actionable install hint if it's missing, rather than trying
to install it.

Design notes for what remains (model management):

- TOFU integrity: the sha256 of a downloaded model is recorded in
  MANIFEST_PATH on first success and verified against that record on
  every later reuse. This doesn't stop a first-run MITM, but it does
  catch silent corruption/tampering afterward, and it means re-running
  `init` never silently swaps in a different model without telling you.
- Model choice persists in config.yaml's existing `validation.ai.model`
  field (core/config_schema.py) — the single schema this whole app already
  validates against, not a second state file. Only the resolved local
  *file path* is machine-specific, and that's never persisted anywhere:
  it's derived deterministically from the model name
  (MODELS_DIR/<name>.gguf), so config.yaml stays shareable across a team
  while weights stay per-machine.

Known limitations (scoped out deliberately, not overlooked):
- Rewriting config.yaml's `validation.ai.model` uses plain PyYAML, which
  does not preserve comments/formatting on round-trip. `init` prints what
  it changed and this module never touches config.yaml non-interactively
  without --yes, so the team's config.yaml is only rewritten when a human
  asked for it and can review `git diff` afterward.
- Download resume is not implemented (a partial download is discarded and
  restarted) — atomic tmp-file-then-rename is what actually matters here,
  since it's what prevents a partial file from ever being treated as
  valid, and that's covered.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import typer

from gitscribe.cli import app
from gitscribe.config_locator import find_config_path
from gitscribe.validation.config import load_validation_config

try:
    import yaml
except ImportError:  # pragma: no cover - yaml is already a core dependency
    yaml = None


GITSCRIBE_HOME = Path(os.environ.get("GITSCRIBE_HOME", Path.home() / ".cache" / "gitscribe"))
MODELS_DIR = GITSCRIBE_HOME / "models"
MANIFEST_PATH = GITSCRIBE_HOME / "manifest.json"
RUN_DIR = Path(os.environ.get("GITSCRIBE_LLAMA_RUN_DIR", GITSCRIBE_HOME / "llama-server"))
PID_FILE = RUN_DIR / "llama-server.pid"
LOG_DIR = RUN_DIR / "logs"


class SandboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelSpec:
    url: str
    recommended_ctx: int
    approx_size_gb: float


# Curated, not arbitrary: only models actually intended to run through this
# sandbox. Extend this dict to add support for another model - don't accept
# an arbitrary URL from the CLI by default, that reintroduces the exact
# unpinned-supply-chain problem this module exists to avoid.
MODEL_REGISTRY: dict[str, ModelSpec] = {
    "qwen2.5-coder-3b-instruct-q4_k_m": ModelSpec(
        url=(
            "https://huggingface.co/Qwen/Qwen2.5-Coder-3B-Instruct-GGUF/"
            "resolve/main/qwen2.5-coder-3b-instruct-q4_k_m.gguf"
        ),
        recommended_ctx=6000,
        approx_size_gb=2.1,
    ),
    "qwen2.5-coder-1.5b-instruct-q4_k_m": ModelSpec(
        url=(
            "https://huggingface.co/Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF/"
            "resolve/main/qwen2.5-coder-1.5b-instruct-q4_k_m.gguf"
        ),
        recommended_ctx=6000,
        approx_size_gb=1.1,
    ),
}
DEFAULT_MODEL = "qwen2.5-coder-3b-instruct-q4_k_m"


# --- TOFU manifest -----------------------------------------------------------

def _load_manifest() -> dict:
    if not MANIFEST_PATH.is_file():
        return {}
    try:
        return json.loads(MANIFEST_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_manifest(manifest: dict) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, sort_keys=True))


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_or_record(key: str, path: Path) -> None:
    """TOFU: first success records the hash, every later call verifies it."""
    manifest = _load_manifest()
    actual = _sha256_of(path)
    recorded = manifest.get(key)

    if recorded is None:
        manifest[key] = actual
        _save_manifest(manifest)
        return

    if recorded != actual:
        raise SandboxError(
            f"integrity check failed for {key}: expected {recorded[:12]}..., "
            f"got {actual[:12]}... - the cached file at {path} doesn't match "
            "what was recorded on first download. Delete it and re-run "
            "`gitscribe sandbox init` to re-fetch, or investigate before "
            "trusting it."
        )


def _download(url: str, dest: Path, label: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    typer.echo(f"downloading {label} from {url}")

    def _report(block_num: int, block_size: int, total_size: int) -> None:
        if total_size <= 0:
            return
        pct = min(100, block_num * block_size * 100 // total_size)
        print(f"\r  {label}: {pct}%", end="", file=sys.stderr, flush=True)

    try:
        urllib.request.urlretrieve(url, tmp, reporthook=_report)  # noqa: S310 - pinned/registry URLs only
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise SandboxError(f"failed to download {label}: {exc}") from exc
    finally:
        print(file=sys.stderr)

    # Atomic rename: a process killed mid-download leaves only a .part file
    # behind, never a truncated file at the real destination.
    tmp.rename(dest)


# --- llama.cpp detection (install is out of scope, see module docstring) ---

def find_llama_server() -> Path | None:
    """PATH only: llama.cpp is expected to be installed by the user through
    one of its own maintained channels (Homebrew, distro package, official
    release, or building from source) - see _llama_server_install_hint().
    """
    on_path = shutil.which("llama-server")
    return Path(on_path) if on_path else None


def _llama_server_install_hint() -> str:
    system = platform.system().lower()

    if system == "darwin":
        method = "brew install llama.cpp"
    elif system == "linux":
        method = (
            "download a prebuilt binary from "
            "https://github.com/ggml-org/llama.cpp/releases (look for the "
            "ubuntu-x64/arm64 asset matching your CPU) and put llama-server "
            "on your PATH, or build from source"
        )
    else:
        method = "see https://github.com/ggml-org/llama.cpp#building-the-project"

    return (
        "llama-server was not found on PATH. gitscribe does not install "
        f"llama.cpp itself (see this module's docstring for why) - {method}, "
        "then re-run this command."
    )


# --- model install ------------------------------------------------------------

def resolve_model_path(model_name: str) -> Path:
    return MODELS_DIR / f"{model_name}.gguf"


def download_model(model_name: str, force: bool = False) -> Path:
    if model_name not in MODEL_REGISTRY:
        raise SandboxError(
            f"unknown model {model_name!r}. Available: {', '.join(sorted(MODEL_REGISTRY))}"
        )

    spec = MODEL_REGISTRY[model_name]
    dest = resolve_model_path(model_name)

    if dest.is_file() and not force:
        _verify_or_record(f"model:{model_name}", dest)
        typer.echo(f"model already downloaded: {dest}")
        return dest

    typer.echo(f"model {model_name} is ~{spec.approx_size_gb:.1f} GB")
    _download(spec.url, dest, model_name)
    _verify_or_record(f"model:{model_name}", dest)
    return dest


def _current_model_default() -> str:
    try:
        cfg = load_validation_config(find_config_path())
        return cfg.get("ai", {}).get("model") or DEFAULT_MODEL
    except Exception:
        return DEFAULT_MODEL


def _persist_model_choice(model_name: str) -> None:
    if yaml is None:
        raise SandboxError("pyyaml is required to update config.yaml")

    config_path = Path(find_config_path())
    raw = yaml.safe_load(config_path.read_text()) if config_path.is_file() else {}
    raw = raw or {}
    raw.setdefault("validation", {}).setdefault("ai", {})["model"] = model_name

    # NOTE: plain PyYAML does not preserve comments/formatting on
    # round-trip (see module docstring). Review `git diff config.yaml`
    # after this if the file had hand-written comments worth keeping.
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    typer.echo(f"config.yaml: validation.ai.model = {model_name}")


# --- process management (start/stop/status) ----------------------------------

def _pid_alive() -> int | None:
    if not PID_FILE.is_file():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
    except ValueError:
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def _wait_healthy(port: int, timeout_s: int) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:  # noqa: S310
                if resp.status == 200:
                    return True
        except Exception:
            pass
        if _pid_alive() is None:
            return False
        time.sleep(1)
    return False


def start_server(model_name: str | None = None, port: int = 8080, ctx_size: int | None = None) -> None:
    if _pid_alive():
        typer.echo(f"already running (pid {_pid_alive()})")
        return

    cfg = load_validation_config(find_config_path())
    model_name = model_name or cfg.get("ai", {}).get("model") or DEFAULT_MODEL
    ctx_size = ctx_size or MODEL_REGISTRY.get(model_name, ModelSpec("", cfg.get("ai", {}).get("max_context_tokens", 6000), 0)).recommended_ctx

    binary = find_llama_server()
    if binary is None:
        raise SandboxError(_llama_server_install_hint())

    model_path = resolve_model_path(model_name)
    if not model_path.is_file():
        raise SandboxError(f"model not downloaded: {model_path}. Run `gitscribe sandbox init` first.")

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"llama-server-{time.strftime('%Y%m%d')}.log"

    args = [
        str(binary),
        "-m", str(model_path),
        "-c", str(ctx_size),
        "--threads", str(os.cpu_count() or 4),
        "--n-gpu-layers", "0",
        "--host", "127.0.0.1",  # loopback only, always — see validation/ai.py
        "--port", str(port),
    ]

    typer.echo(f"starting: {' '.join(args)}")
    with log_file.open("ab") as log_fh:
        proc = subprocess.Popen(  # noqa: S603
            args,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # detach from this process group
        )
    PID_FILE.write_text(str(proc.pid))

    if _wait_healthy(port, timeout_s=60):
        typer.echo(f"started (pid {proc.pid}), listening on 127.0.0.1:{port}, logs: {log_file}")
    else:
        typer.echo(f"startup failed or timed out — see {log_file}", err=True)
        raise typer.Exit(1)


def stop_server(grace_s: int = 10) -> None:
    pid = _pid_alive()
    if pid is None:
        typer.echo("not running")
        PID_FILE.unlink(missing_ok=True)
        return

    typer.echo(f"stopping pid {pid} (SIGTERM)")
    os.kill(pid, signal.SIGTERM)

    deadline = time.monotonic() + grace_s
    while _pid_alive() and time.monotonic() < deadline:
        time.sleep(1)

    if _pid_alive():
        typer.echo(f"still alive after {grace_s}s, sending SIGKILL")
        os.kill(pid, signal.SIGKILL)

    PID_FILE.unlink(missing_ok=True)
    typer.echo("stopped")


def status_server(port: int = 8080) -> bool:
    pid = _pid_alive()
    if pid is None:
        typer.echo("not running")
        return False

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:  # noqa: S310
            healthy = resp.status == 200
    except Exception:
        healthy = False

    if healthy:
        typer.echo(f"running (pid {pid}), healthy on 127.0.0.1:{port}")
    else:
        typer.echo(f"running (pid {pid}) but NOT responding on /health")

    return healthy


sandbox_app = typer.Typer(help="Install and manage the local llama.cpp sandbox used by `gitscribe verify`.")


@sandbox_app.command("init")
def sandbox_init(
    model: str = typer.Option(None, "--model", help=f"Model to install. One of: {', '.join(sorted(MODEL_REGISTRY))}"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the interactive prompt and config.yaml write confirmation (for CI)."),
    force: bool = typer.Option(False, "--force", help="Re-download the model even if already present."),
) -> None:
    """Check for llama-server (does not install it) and download the chosen model (if needed)."""
    try:
        if find_llama_server() is None:
            typer.echo(_llama_server_install_hint(), err=True)
            raise typer.Exit(1)

        if model is None:
            if yes:
                model = _current_model_default()
            else:
                model = typer.prompt(
                    f"Model ({'/'.join(sorted(MODEL_REGISTRY))})",
                    default=_current_model_default(),
                )

        if model not in MODEL_REGISTRY:
            typer.echo(f"unknown model {model!r}. Available: {', '.join(sorted(MODEL_REGISTRY))}", err=True)
            raise typer.Exit(1)

        download_model(model, force=force)

        if yes or typer.confirm(f"Set validation.ai.model = {model} in config.yaml?", default=True):
            _persist_model_choice(model)

    except SandboxError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@sandbox_app.command("start")
def sandbox_start(
    model: str = typer.Option(None, "--model"),
    port: int = typer.Option(8080, "--port"),
) -> None:
    try:
        start_server(model_name=model, port=port)
    except SandboxError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@sandbox_app.command("stop")
def sandbox_stop() -> None:
    stop_server()


@sandbox_app.command("status")
def sandbox_status(port: int = typer.Option(8080, "--port")) -> None:
    if not status_server(port=port):
        raise typer.Exit(3)


app.add_typer(sandbox_app, name="sandbox")
