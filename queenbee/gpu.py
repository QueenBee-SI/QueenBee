"""Cooperative workload client and terminal launcher: python -m queenbee.gpu.

Use AdaptiveGPU at safe batch boundaries. A terminal wrapper cannot rewrite
an arbitrary training loop or enforce a driver-level VRAM quota.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import deque

from .agents.host_client import TOKEN_ENV, HostClient, HostUnavailable


def _split_windows(text):
    """Split a command line the way a Windows program sees it (quotes group,
    backslashes stay literal). shlex's POSIX rules would eat the backslashes
    of every Windows path."""
    parts, cur, quoted, started = [], [], False, False
    for ch in text:
        if ch == '"':
            quoted, started = not quoted, True
        elif ch.isspace() and not quoted:
            if started:
                parts.append("".join(cur))
                cur, started = [], False
        else:
            cur.append(ch)
            started = True
    if started:
        parts.append("".join(cur))
    return parts


def normalise_command(command, cwd="."):
    """Validate a workload command before it is submitted.

    Agents regularly pass the whole command as one quoted string
    (`-- "python train.py --epochs 3"`), which then reaches CreateProcess as a
    program name with spaces in it and dies at once -- after the agent has
    already reported success. Such a string is split into its arguments, and
    a program that cannot be found is refused here, where the agent still
    sees the error.
    """
    command = [str(part) for part in command]
    if len(command) == 1 and " " in command[0].strip() and not os.path.exists(command[0]):
        command = _split_windows(command[0])
    if not command:
        raise ValueError("a GPU workload command is required")
    exe = command[0]
    local = exe if os.path.isabs(exe) else os.path.join(cwd, exe)
    if not (os.path.exists(local) or shutil.which(exe)):
        raise ValueError(
            f"workload program not found: {exe!r}. Pass the command as separate "
            "arguments after --, starting with an absolute interpreter path.")
    return command


def launch_argv(command, *, name="GPU workload", gpu=0, role="secondary",
                max_batch=8, port=7877, owner=None):
    """Build the validated launcher argv used by the UI and documentation.

    `owner` is the agent terminal that submitted the workload. It rides in the
    worker's own argv, like the role, so every window can read which chat the
    job belongs to from the process itself.
    """
    command = [str(part) for part in command]
    name = str(name).strip() or "GPU workload"
    gpu = int(gpu)
    max_batch = int(max_batch)
    port = int(port)
    if not command:
        raise ValueError("a GPU workload command is required")
    if role not in {"primary", "secondary"}:
        raise ValueError("role must be primary or secondary")
    if gpu < 0:
        raise ValueError("GPU index must be nonnegative")
    if not 1 <= max_batch <= 65536:
        raise ValueError("max batch must be between 1 and 65536")
    if not 1 <= port <= 65535:
        raise ValueError("host port must be between 1 and 65535")
    if owner is not None and not re.fullmatch(r"term-\d+", str(owner)):
        raise ValueError("owner must be a terminal id")
    return [
        sys.executable, "-m", "queenbee.gpu", "--port", str(port), "run",
        "--cooperative", "--name", name, "--gpu", str(gpu),
        "--role", role, "--max-batch", str(max_batch),
        *(["--owner", str(owner)] if owner else []), "--", *command,
    ]


# Flags whose value changes how much VRAM a run holds. Every other flag's
# value (learning rate, seed, run tag, epochs, swept settings ...) does not,
# and is left out, so the runs of one sweep share one signature.
_SHAPE_FLAGS = ("batch", "bs", "micro", "accum", "precision", "amp", "fp16", "bf16", "half",
                "model", "arch", "backbone", "dataset", "data", "img", "size", "resolution",
                "width", "depth", "hidden", "layers", "seq", "context", "crop", "workers")


def workload_signature(command, cwd=None):
    """A stable id for what a command runs, not for what it is called: the
    hash of the script it runs plus the values of the VRAM-shaping flags."""
    cwd = cwd or os.getcwd()
    script = next((a for a in command if str(a).endswith(".py")
                   and os.path.isfile(os.path.join(cwd, a))), None)
    h = hashlib.sha256()
    if script:
        with open(os.path.join(cwd, script), "rb") as fh:
            h.update(fh.read())
    else:
        h.update(" ".join(str(a) for a in command[:1]).encode())
    parts, i, args = [], 0, [str(a) for a in command]
    while i < len(args):
        flag, eq, value = args[i].partition("=")
        if flag.startswith("-") and any(k in flag.lower().lstrip("-") for k in _SHAPE_FLAGS):
            if not eq and i + 1 < len(args) and not args[i + 1].startswith("-"):
                value, i = args[i + 1], i + 1
            parts.append(f"{flag}={value}")
        i += 1
    h.update(";".join(sorted(parts)).encode())
    return h.hexdigest()[:16]


def console_python(path=None):
    """This interpreter, but the console one: pythonw.exe (what a windowed
    app such as the Mother window runs under) has no stdout or stderr, so a
    workload started with it dies at its first progress bar with no output."""
    path = path or sys.executable
    head, name = os.path.split(path)
    if name.lower() == "pythonw.exe" and os.path.isfile(os.path.join(head, "python.exe")):
        return os.path.join(head, "python.exe")
    return path


def submit_argv(command, *, cwd=".", project_root=None, name="GPU workload", gpu=0,
                role="secondary", max_batch=8, port=7877, ui_port=7878):
    """Build a command that hands the workload to QueenBee's persistent host."""
    launch_argv(command, name=name, gpu=gpu, role=role,
                max_batch=max_batch, port=port)
    ui_port = int(ui_port)
    if not 1 <= ui_port <= 65535:
        raise ValueError("UI port must be between 1 and 65535")
    result = [
        console_python(), "-m", "queenbee.gpu", "--port", str(int(port)), "submit",
        "--ui-port", str(ui_port), "--cwd", str(cwd),
    ]
    if project_root is not None:
        result += ["--project-root", str(project_root)]
    return result + [
        "--name", str(name),
        "--gpu", str(int(gpu)), "--role", role,
        "--max-batch", str(int(max_batch)), "--", *[str(part) for part in command],
    ]


def project_contains(project_root, cwd):
    """True when cwd is the selected project or one of its descendants."""
    root = os.path.normcase(os.path.realpath(os.path.abspath(project_root)))
    target = os.path.normcase(os.path.realpath(os.path.abspath(cwd)))
    try:
        return os.path.commonpath([root, target]) == root
    except ValueError:
        return False


def agent_task_prompt(task, *, name="GPU workload", gpu=0, role="secondary",
                      max_batch=8, port=7877, project_root=None):
    """Policy prompt for an agent that discovers and launches the work itself."""
    task = str(task).strip()
    # Reuse the launch validation without guessing a workload command yet.
    sample = submit_argv(["<workload command>"], cwd=".", project_root=project_root,
                         name=name, gpu=gpu, role=role, max_batch=max_batch, port=port)
    prefix = subprocess.list2cmdline(sample[:-1])
    role_text = (
        "This is the primary workload and may run unrestricted. It must still register "
        "through AdaptiveGPU so QueenBee can display and supervise it."
        if role == "primary" else
        "This is a secondary workload. It must yield at safe work-unit boundaries, "
        "release GPU allocations before acknowledging wait, and resume safely. "
        "Existing primary workloads are expected: never stop, replace, or ask whether "
        "to stop them. Do not ask the user to inspect GPU capacity or choose whether "
        "there is room. After safe adaptation, always submit the workload; QueenBee is "
        "the sole admission authority and will either run it, reduce its work unit, "
        "or keep it unloaded and waiting until capacity is available."
    )
    goal = (
        f"User goal: {task}" if task else
        "The user will provide the goal in the next interactive message. Do not inspect, edit, or launch anything until that message arrives."
    )
    return f"""You are responsible for the next GPU task end to end.

{goal}

QueenBee policy: role={role}, GPU={int(gpu)}, maximum work unit/batch={int(max_batch)}.
{role_text}

Work autonomously, but do not assume the workload type, model, dataset, engine,
or output from files left by an earlier run. First translate the user's actual
goal into a concrete project-local command and verify that the discovered script,
inputs and outputs match that goal. An existing training script is not evidence
that the requested task is training or that it uses the same model/data. Ask only
when the goal itself is materially ambiguous; do not ask for a script name when
the correct script is discoverable from a clear goal. The selected project root is
{project_root or 'the current working directory'}.
Keep discovery, edits and workload launch inside that project root. Never switch
to a sibling project or submit with a different project as --cwd merely because
it has a similar script or dataset. If the selected project lacks the requested
assets, report that concrete project-local blocker instead of starting duplicate
work elsewhere. Before launching GPU work, integrate a QueenBee cooperative
adapter at safe boundaries. For Python use queenbee.gpu.AdaptiveGPU. A safe work unit
may be a training micro-batch, inference request, frame, tile, render pass,
simulation step, compilation unit, or another workload-appropriate boundary.
Preserve the workload's semantics, progress/output integrity, restart safety and
idempotency; training-specific batch/checkpoint rules apply only to training.
Use a concise truthful --name derived from the actual workload; never reuse a
previous model or dataset name unless this run really uses it. Then launch through:

  {prefix} <workload command>

Use the project's own interpreter for <workload command>. Do not merely run the
original command outside the launcher. If this workload cannot be adapted safely,
explain the concrete blocker instead of pretending it is controlled. Avoid
overwriting an existing checkpoint unless the user's goal clearly requires it.
Do not use the agent's temporary/background-task feature for the workload; the
submit command hands it to QueenBee's persistent host. After submission, monitor
initial progress and report the QueenBee terminal id, command and log path."""


# Exit status of a job that saved its state and left because QueenBee asked it to
# (EX_TEMPFAIL). `queenbee.gpu run` restarts such a job when the GPU allows.
PREEMPTED_EXIT = 75


class Preempted(SystemExit):
    """Raised by `before_batch` after the job saved its state for preemption.

    A SystemExit, so an application that does not catch it exits with
    PREEMPTED_EXIT after its `with`/`finally` blocks ran -- no extra code
    is needed beyond the `save` callback.
    """

    def __init__(self) -> None:
        super().__init__(PREEMPTED_EXIT)


class AdaptiveGPU:
    """Cooperative GPU access for one workload.

    Resumable jobs pass `save`: a callable that writes everything the job needs
    to continue (model, optimizer, schedule, RNG, position in the data) and
    returns only once it is durably on disk. The job's own start-up code must
    load that state when it exists. QueenBee then preempts the job instead of
    holding it in memory: at the next safe boundary `before_batch` calls
    `save`, releases the GPU and raises `Preempted`.

    `restore` is the inverse of `release`: it moves the model and optimizer
    state back to the GPU. When given, `before_batch` calls it before letting
    a released job run again, so a script need not remember to reload.
    """

    def __init__(self, name=None, *, gpu=None, role=None, max_batch=None,
                 release=None, trim=None, client=None, sleep=time.sleep, save=None,
                 restore=None):
        self.role = role or os.environ.get("QUEENBEE_GPU_ROLE", "secondary")
        self.gpu = int(gpu if gpu is not None else os.environ.get("QUEENBEE_GPU_INDEX", "0"))
        self.max_batch = int(max_batch if max_batch is not None else os.environ.get("QUEENBEE_GPU_BATCH", "8"))
        if self.role == "secondary" and not callable(release):
            raise ValueError("secondary jobs require a release callback that frees GPU allocations")
        if save is not None and not callable(save):
            raise ValueError("save must be a callable that writes the job's state")
        if restore is not None and not callable(restore):
            raise ValueError("restore must be a callable that moves the job back to the GPU")
        self.release = release
        self.restore = restore
        self.trim = trim
        self.save = save
        self.previous_batch = 0
        self.sleep = sleep
        self.client = client or HostClient(port=int(os.environ.get("QUEENBEE_GPU_PORT", "7877")), autostart=False)
        result = self.client._call("gpu_register", name or os.environ.get("QUEENBEE_GPU_NAME", "Python workload"),
                                   os.getpid(), gpu=self.gpu, role=self.role, max_batch=self.max_batch,
                                   resumable=save is not None,
                                   launch_name=os.environ.get("QUEENBEE_GPU_NAME"),
                                   signature=os.environ.get("QUEENBEE_GPU_SIGNATURE"))
        self.key, self.token = result["id"], result["token"]
        self.ack = -1
        self.released = False
        self.closed = False

    def _unload(self):
        if not self.released:
            self.release()  # Do not acknowledge until unloading actually succeeded.
            self.released = True

    def before_batch(self):
        """Return permitted microbatch size; wait/unload on pressure or host loss.

        The caller loads/reloads its model lazily AFTER this method returns.
        Calling it after loading a model defeats initial admission control.
        """
        if self.role == "primary":
            # Primary progress must not depend on the host being available.
            try:
                d = self.client._call("gpu_heartbeat", self.key, self.token, ack=self.ack)
                self.ack = d["revision"]
            except HostUnavailable:
                pass
            return self.max_batch
        while True:
            try:
                d = self.client._call("gpu_heartbeat", self.key, self.token,
                                      ack=self.ack, released=self.released)
            except HostUnavailable as exc:
                self._unload()
                # Fail closed instead of silently starting a replacement host
                # that has forgotten running jobs and control acknowledgements.
                raise RuntimeError("QueenBee host unavailable; secondary memory released. Reconnect before continuing.") from exc
            if d["action"] == "preempt" and self.save is not None:
                # Order matters: state is on disk before memory is released,
                # and released before QueenBee is told, so an acknowledged
                # preemption never loses work or holds VRAM.
                self.save()
                self._unload()
                self.ack = d["revision"]
                try:
                    self.client._call("gpu_heartbeat", self.key, self.token,
                                      ack=self.ack, released=True)
                except HostUnavailable:
                    pass
                self.close()
                raise Preempted()
            if d["action"] in ("wait", "preempt"):
                self._unload()
                self.ack = d["revision"]
                self.sleep(0.5)
                continue
            self.ack = d["revision"]
            if d["batch"] < self.previous_batch:
                # Smaller batches alone do not return allocator-cached VRAM.
                # Trim after prior GPU tensors are gone, or unload as fallback.
                if self.trim:
                    self.trim()
                else:
                    self._unload()
            self.previous_batch = d["batch"]
            if self.released and self.restore is not None:
                self.restore()
            self.released = False
            if d["delay_s"]:
                self.sleep(d["delay_s"])
            return d["batch"]

    def report_oom(self):
        """Call after leaving an OOM exception handler; no automatic step retry.

        Training steps can mutate weights before failing. The application must
        decide whether rollback/retry is valid, including optimizer state.
        """
        if self.role == "primary":
            raise RuntimeError("Primary OOM must be handled by the training application")
        self._unload()
        d = self.client._call("gpu_heartbeat", self.key, self.token,
                              ack=self.ack, released=True, oom=True)
        self.ack = d["revision"]

    def batches(self, iterable):
        """Yield variable-sized CPU-input batches. Do not pre-load inputs to GPU."""
        pending = deque()
        source = iter(iterable)
        exhausted = False
        while True:
            if not pending and not exhausted:
                try:
                    pending.append(next(source))
                except StopIteration:
                    exhausted = True
            if not pending:
                return
            size = self.before_batch()
            while len(pending) < size and not exhausted:
                try:
                    pending.append(next(source))
                except StopIteration:
                    exhausted = True
            yield [pending.popleft() for _ in range(min(size, len(pending)))]

    def close(self):
        if self.closed:
            return
        if self.role == "secondary":
            self._unload()
        try:
            self.client._call("gpu_unregister", self.key, self.token)
        except HostUnavailable:
            pass
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run a QueenBee-aware Python job with adaptive GPU control")
    parser.add_argument("--port", type=int, default=7877)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    run = sub.add_parser("run")
    run.add_argument("--name", default="Terminal workload")
    run.add_argument("--gpu", type=int, default=0)
    run.add_argument("--role", choices=("primary", "secondary"), default="secondary")
    run.add_argument("--max-batch", type=int, default=8)
    # Read by QueenBee from the process's command line; the worker ignores it.
    run.add_argument("--owner", help=argparse.SUPPRESS)
    run.add_argument("--cooperative", action="store_true", help="Confirm the script uses AdaptiveGPU")
    run.add_argument("argv", nargs=argparse.REMAINDER)
    submit = sub.add_parser("submit", help="launch a cooperative workload in a persistent QueenBee terminal")
    submit.add_argument("--ui-port", type=int, default=7878)
    submit.add_argument("--cwd", default=".")
    submit.add_argument("--project-root")
    submit.add_argument("--name", default="GPU workload")
    submit.add_argument("--gpu", type=int, default=0)
    submit.add_argument("--role", choices=("primary", "secondary"), default="secondary")
    submit.add_argument("--max-batch", type=int, default=8)
    submit.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    client = HostClient(args.port, autostart=False)
    if args.command == "status":
        print(json.dumps(client._call("gpu_state"), indent=2))
        return 0
    if args.command == "submit":
        command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        try:
            command = normalise_command(command, os.path.abspath(args.cwd))
            # Validation is shared with direct launches; the UI server performs
            # it again at the trust boundary before opening the terminal.
            launch_argv(command, name=args.name, gpu=args.gpu, role=args.role,
                        max_batch=args.max_batch, port=args.port)
            if args.project_root and not project_contains(args.project_root, args.cwd):
                raise ValueError(
                    "workload cwd is outside the selected project; choose that project explicitly"
                )
            payload = json.dumps({
                "cwd": os.path.abspath(args.cwd),
                "argv": command,
                "gpu_job": {"name": args.name, "gpu": args.gpu,
                            "role": args.role, "max_batch": args.max_batch},
                # Lets QueenBee find the agent terminal this runs inside, so the
                # job is shown under the chat that submitted it rather than
                # guessed from the project folder.
                "submitter_pid": os.getpid(),
            }).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{args.ui_port}/api/terminal/open",
                data=payload, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=15) as response:
                result = json.loads(response.read() or b"{}")
        except (ValueError, OSError, urllib.error.URLError) as exc:
            parser.error(str(exc))
        if not result.get("ok"):
            parser.error(result.get("error") or "QueenBee did not accept the workload")
        print(json.dumps(result))
        return 0
    if not args.cooperative:
        parser.error("Use --cooperative only for scripts integrated with queenbee.gpu.AdaptiveGPU; arbitrary commands are not capped")
    command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if not command or args.gpu < 0 or not 1 <= args.max_batch <= 65536:
        parser.error("Provide a command, nonnegative GPU index and max-batch 1..65536")
    info = client.ping()
    if not info or info.get("version", 0) < 4:
        parser.error("Start an updated QueenBee host first. Do not stop a host with live training jobs")
    state = client._call("gpu_state")
    device = next((g for g in state["devices"] if g["index"] == args.gpu), None)
    if not state["telemetry_fresh"] or not device or not str(device.get("uuid", "")).startswith("GPU-"):
        parser.error("Selected GPU has no fresh telemetry/UUID; wait for host measurement")
    env = dict(os.environ, QUEENBEE_GPU_NAME=args.name, QUEENBEE_GPU_INDEX=str(args.gpu),
               QUEENBEE_GPU_ROLE=args.role, QUEENBEE_GPU_BATCH=str(args.max_batch), QUEENBEE_GPU_PORT=str(args.port),
               QUEENBEE_GPU_SIGNATURE=workload_signature(command),
               CUDA_DEVICE_ORDER="PCI_BUS_ID", CUDA_VISIBLE_DEVICES=device["uuid"])
    # The workload's AdaptiveGPU must reach the host, and the project's own
    # interpreter may not see the state directory this one wrote the token to
    # (a Microsoft Store Python redirects it). Only this child gets it; the
    # host itself never holds it in its environment, so agents don't inherit it.
    token = client.token()
    if token:
        env[TOKEN_ENV] = token
    # Device visibility is not a memory quota. SDK callbacks perform adaptation.
    runs = 0
    while True:
        runs += 1
        code = subprocess.call(command, env=env)
        if code != PREEMPTED_EXIT:
            return code
        # The job saved its state and left for a higher-priority one. Start the
        # same command again once this GPU can take it; the job's own start-up
        # code picks up from its saved state.
        print(f"[queenbee] {args.name}: preempted after saving its state; "
              "waiting for the GPU to resume", flush=True)
        while True:
            time.sleep(2)
            try:
                verdict = client._call("gpu_can_resume", args.gpu, name=env["QUEENBEE_GPU_SIGNATURE"])
            except HostUnavailable:
                continue
            if verdict.get("ok"):
                break
        print(f"[queenbee] {args.name}: resuming (start {runs + 1})", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
