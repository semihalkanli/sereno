"""Ephemeral Docker execution without host mounts or credential forwarding."""

import json
import subprocess
import uuid

from sereno.context_eval.schema import validate_path

# JSON on stdin keeps payloads out of shell syntax. All filesystem operations stay in the container.
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
        return p.read_text()
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
        p.write_text(request['text'])
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


class DockerEnvironment:
    def __init__(self, image_id: str, wall_seconds: int):
        self.name = f"sereno-context-eval-{uuid.uuid4().hex[:12]}"
        self.closed = False
        self.action_env: dict[str, str] = {}
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
        except Exception:
            self.close()
            raise

    def execute(self, command: str, timeout: int = 300) -> dict:
        try:
            env_args = [part for key, value in self.action_env.items() for part in ("-e", f"{key}={value}")]
            result = subprocess.run(
                ["docker", "exec", "-w", "/app", *env_args, self.name, "bash", "-lc", command],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            return {"output": result.stdout, "returncode": result.returncode, "exception_info": ""}
        except subprocess.TimeoutExpired as error:
            output = error.stdout or b""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            # docker exec timeout does not kill its in-container process: invalidate and close the session.
            self.close()
            raise RuntimeError(
                f"action timed out; container stopped; partial output has {len(output)} characters"
            ) from error

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

    def collect_patch(self, base_commit: str) -> str:
        if not all(c in "0123456789abcdef" for c in base_commit) or len(base_commit) != 40:
            raise ValueError("base commit must be a full hexadecimal commit hash")
        result = self.execute(
            f"git -c safe.directory=/app add -A && git -c safe.directory=/app diff --cached --binary {base_commit}"
        )
        if result["returncode"]:
            raise RuntimeError("patch collection failed")
        return result["output"]

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            subprocess.run(["docker", "rm", "-f", self.name], capture_output=True, timeout=30, check=True)
