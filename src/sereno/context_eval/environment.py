"""Ephemeral Docker execution without host mounts or credential forwarding."""

import json
import shlex
import subprocess
import time
import uuid

from sereno.context_eval.schema import validate_path

# JSON on stdin keeps payloads out of shell syntax. All filesystem operations stay in the container.
# Bytes are decoded and written without newline translation, so CRLF files round-trip unchanged.
BRIDGE = r"""
import json, os, pathlib, sys
request = json.load(sys.stdin)
def safe(raw):
    p = pathlib.Path(raw)
    if not (raw.startswith('/app/') or raw.startswith('/memories/')):
        raise ValueError('path outside experiment roots')
    if '..' in p.parts or '.git' in p.parts or str(p) != raw:
        raise ValueError('noncanonical or reserved path')
    if str(p.resolve()) != raw:
        raise ValueError('symlink paths are unsupported')
    return p
def text(p):
    try:
        return p.read_bytes().decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError(f'non-UTF-8 file {p}') from None
op = request['op']
if op == 'read':
    p = safe(request['path'])
    result = text(p) if p.is_file() else None
elif op == 'write':
    p = safe(request['path'])
    if request['text'] is None:
        p.unlink(missing_ok=True)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(request['text'], encoding='utf-8', newline='')
    result = None
elif op == 'memory':
    root = pathlib.Path('/memories')
    result, size = {}, 0
    if root.is_symlink():
        raise ValueError('symlink memory root')
    if root.exists():
        for directory, dirs, files in os.walk(root):
            for name in dirs + files:
                if pathlib.Path(directory, name).is_symlink():
                    raise ValueError('symlink memory entry')
            for name in sorted(files):
                p = safe(str(pathlib.Path(directory, name)))
                size += p.stat().st_size
                if size > request['max_bytes'] or len(result) >= request['max_files']:
                    raise ValueError('memory snapshot exceeds limits')
                result[str(p)] = text(p)
else:
    raise ValueError('unknown bridge operation')
print(json.dumps(result, ensure_ascii=False))
"""


GIT = "git -c safe.directory=/app -c core.hooksPath=/dev/null"
IDENTITY = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_COMMITTER_DATE",
)


def commit_planted(env, paths: list[str]) -> dict:
    """Fold planted files into HEAD so `git status` stays clean and `git log -1` differs only in its hash.

    Runs inside the environment through env.execute. Ignored paths are never force-added; they stay ignored.
    """

    def run(command: str) -> str:
        result = env.execute(command)
        if result["returncode"]:
            raise RuntimeError(f"planting commit failed: {result['output'].strip()[-300:]}")
        return result["output"]

    base = run(f"{GIT} rev-parse HEAD").strip()
    placement = {}
    for path in paths:
        code = env.execute(f"{GIT} check-ignore -q -- {shlex.quote(path)}")["returncode"]
        if code not in (0, 1):
            raise RuntimeError(f"planting commit failed: cannot check whether {path} is ignored")
        placement[path] = "ignored" if code == 0 else "committed"
        if placement[path] == "committed":
            run(f"{GIT} add -- {shlex.quote(path)}")
    head, moved = base, []
    if "committed" in placement.values():
        fields = run(f"{GIT} log -1 --date=raw --format=%an%n%ae%n%ad%n%cn%n%ce%n%cd HEAD").split("\n")[:6]
        identity = " ".join(f"{key}={shlex.quote(value)}" for key, value in zip(IDENTITY, fields, strict=True))
        run(f"{identity} {GIT} commit --quiet --amend --no-edit --no-verify --no-gpg-sign --cleanup=verbatim")
        head = run(f"{GIT} rev-parse HEAD").strip()
        # Other branches and remote-tracking refs at the base would show the branch as diverged; tags stay put.
        refs = run(f"{GIT} for-each-ref --points-at {base} --format='%(refname) %(symref)' refs/heads refs/remotes")
        moved = [line.split()[0] for line in refs.splitlines() if len(line.split()) == 1]
        for ref in moved:
            run(f"{GIT} update-ref {shlex.quote(ref)} {head} {base}")
    return {
        "base_commit": base,
        "planted_head": head,
        "paths": placement,
        "moved_refs": moved,
        "status": run(f"{GIT} status --porcelain"),
    }


DEFAULT_ACTION_TIMEOUT = 300
KILLED = 128 + 9


def action_result(command: str, output: str, returncode: int, timed_out_after: int | None = None) -> dict:
    """An action result shaped like mini-swe's DockerEnvironment, including its timeout observation."""
    if timed_out_after is None:
        return {"output": output, "returncode": returncode, "exception_info": ""}
    message = f"Command '{command}' timed out after {timed_out_after} seconds"
    return {
        "output": output,
        "returncode": -1,
        "exception_info": f"An error occurred while executing the command: {message}",
        "extra": {"exception_type": "TimeoutExpired", "exception": message},
    }


class DockerEnvironment:
    def __init__(self, image_id: str, wall_seconds: int):
        self.name = f"sereno-context-eval-{uuid.uuid4().hex[:12]}"
        self.closed = False
        self.action_env: dict[str, str] = {}
        self.action_timeout = DEFAULT_ACTION_TIMEOUT
        self.has_timeout = False
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                self.name,
                "--network",
                "none",
                "--cpus",
                "2",
                "--memory",
                "8g",
                "-w",
                "/app",
                image_id,
                "sleep",
                str(wall_seconds + 3600),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        try:
            self._bridge({"op": "memory", "max_files": 100, "max_bytes": 1_000_000})
            # Without coreutils timeout, an overlong action hits the host timeout and invalidates the session.
            self.has_timeout = self.execute("command -v timeout")["returncode"] == 0
        except Exception:
            self.close()
            raise

    def _exec(self, command: str, **capture) -> subprocess.CompletedProcess:
        """Run a bash command in /app under the action limit; `capture` sets the subprocess output handling."""
        limit = self.action_timeout
        env_args = [part for key, value in self.action_env.items() for part in ("-e", f"{key}={value}")]
        # coreutils timeout kills the whole in-container process group, so a timed-out action cannot keep running.
        killer = ["timeout", "-s", "KILL", str(limit)] if self.has_timeout else []
        try:
            return subprocess.run(
                ["docker", "exec", "-w", "/app", *env_args, self.name, *killer, "bash", "-lc", command],
                timeout=limit + 60,
                **capture,
            )
        except subprocess.TimeoutExpired as error:
            output = error.stdout or b""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            # docker exec timeout does not kill its in-container process: invalidate and close the session.
            self.close()
            raise RuntimeError(
                f"action timed out; container stopped; partial output has {len(output)} characters"
            ) from error

    def execute(self, command: str) -> dict:
        limit = self.action_timeout
        started = time.monotonic()
        result = self._exec(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace"
        )
        timed_out = self.has_timeout and result.returncode == KILLED and time.monotonic() - started >= limit
        return action_result(command, result.stdout, result.returncode, limit if timed_out else None)

    def _bridge(self, request: dict):
        try:
            result = subprocess.run(
                ["docker", "exec", "-i", self.name, "python3", "-c", BRIDGE],
                input=json.dumps(request),
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
        except subprocess.CalledProcessError as error:
            # The bridge's last stderr line names the rejected path or limit; keep it in result.json.
            reason = (error.stderr or "").strip().splitlines()[-1:] or [f"exit status {error.returncode}"]
            raise RuntimeError(f"bridge {request['op']} failed: {reason[0]}") from error
        return json.loads(result.stdout)

    def read(self, path: str) -> str | None:
        validate_path(path, "/memories" if path.startswith("/memories/") else "/app")
        return self._bridge({"op": "read", "path": path})

    def write(self, path: str, text: str | None) -> None:
        validate_path(path, "/memories" if path.startswith("/memories/") else "/app")
        self._bridge({"op": "write", "path": path, "text": text})

    def snapshot_memory(self, max_files: int, max_bytes: int) -> dict[str, str]:
        return self._bridge({"op": "memory", "max_files": max_files, "max_bytes": max_bytes})

    def collect_patch(self, base_commit: str) -> bytes:
        """The working tree against the base as `git diff --binary` bytes: CR and non-UTF-8 bytes stay exact."""
        if not all(c in "0123456789abcdef" for c in base_commit) or len(base_commit) != 40:
            raise ValueError("base commit must be a full hexadecimal commit hash")
        # Bytes with stderr apart: git's line-ending warnings never enter the patch.
        result = self._exec(f"{GIT} add -A && {GIT} diff --cached --binary {base_commit}", capture_output=True)
        if result.returncode:
            reason = result.stderr.decode("utf-8", errors="replace").strip()[-300:]
            raise RuntimeError(f"patch collection failed: {reason or f'exit status {result.returncode}'}")
        return result.stdout

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            subprocess.run(["docker", "rm", "-f", self.name], capture_output=True, timeout=30, check=True)
