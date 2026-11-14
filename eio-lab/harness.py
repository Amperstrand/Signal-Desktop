#!/usr/bin/env python3
"""EIO dialog-loop harness for Signal-Desktop dev builds.

Launches electron under a sacrificial PTY (fds only, no controlling tty —
replicates `signal-desktop &` + terminal close), drains startup, orphans the
slave by closing the master, fires a deterministic log trigger, then counts
instrumented events (handleError/dialog/exit) until the window closes.
"""
import json, os, selectors, shutil, signal, subprocess, sys, time

REPO = os.path.expanduser("~/src/Signal-Desktop")
BASE = "/tmp/opencode/signal-eio/runs"
SETTLE = int(os.environ.get("SETTLE", "30"))
OBSERVE = int(os.environ.get("OBSERVE", "45"))

label = sys.argv[1] if len(sys.argv) > 1 else "run"
run_dir = os.path.join(BASE, f"{label}-{int(time.time())}")
os.makedirs(run_dir, exist_ok=True)
events = os.path.join(run_dir, "events.jsonl")
trigger = os.path.join(run_dir, "trigger")
if os.path.exists(trigger):
    os.unlink(trigger)
open(os.path.join(run_dir, "host-sanity.jsonl"), "w").write(
    json.dumps({"t": time.time(), "kind": "host-writer-ok"}) + "\n")

master, slave = os.openpty()
pid = os.fork()
if pid == 0:  # child
    os.setsid()  # own session; PTY is NOT the controlling tty (no SIGHUP on close)
    os.dup2(slave, 0); os.dup2(slave, 1); os.dup2(slave, 2)
    if slave > 2: os.close(slave)
    os.close(master)
    env = dict(os.environ)
    env.update({
        "SIGNAL_EIO_INSTR": "1",
        "SIGNAL_EIO_EVENTS": events,
        "SIGNAL_EIO_TRIGGER": trigger,
        "DISPLAY": ":0",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "ELECTRON_DISABLE_SANDBOX": "1",
        "ELECTRON_NO_ATTACH_CONSOLE": "1",
    })
    env.pop("WAYLAND_DISPLAY", None)  # force X11/XWayland path
    os.chdir(REPO)
    electron = os.path.join(REPO, "node_modules", ".bin", "electron")
    os.execve(electron, [electron, ".", "--storage-profile=eio",
                         "--ozone-platform=x11"], env)

os.close(slave)
t0 = time.time()

# Phase A: drain the PTY master until the instrumentation reports ready
# (event loop alive + watcher armed). Cold first-run profiles spend minutes
# in synchronous DB setup; a fixed timer misses the window.
sel = selectors.DefaultSelector()
sel.register(master, selectors.EVENT_READ)
master_log = open(os.path.join(run_dir, "master.log"), "wb")
saw_output = False
ready_at = None
READY_TIMEOUT = int(os.environ.get("READY_TIMEOUT", "150"))
while time.time() - t0 < READY_TIMEOUT:
    for key, _ in sel.select(0.5):
        try:
            chunk = os.read(master, 65536)
        except OSError:
            chunk = b""
        if chunk:
            saw_output = True
            master_log.write(chunk)
    if os.path.exists(events):
        try:
            with open(events) as f:
                if "instr-ready" in f.read():
                    ready_at = round(time.time() - t0, 1)
                    break
        except (FileNotFoundError, PermissionError):
            pass
if ready_at is not None:
    # grace drain while the app finishes settling. Long on purpose: closing
    # the PTY mid-startup-write triggers a Chromium-level abort (observed
    # 2026-11-13) instead of the Node-level EIO path the incident exercises,
    # and the real incident closed the terminal hours after launch.
    tend = time.time() + int(os.environ.get("STARTUP_GRACE", "30"))
    while time.time() < tend:
        for key, _ in sel.select(0.5):
            try:
                chunk = os.read(master, 65536)
            except OSError:
                chunk = b""
            if chunk:
                master_log.write(chunk)
master_log.close()

alive_after_settle = True
try:
    os.kill(pid, 0)
except ProcessLookupError:
    alive_after_settle = False

# Phase B: orphan the slave. No ctty => no SIGHUP; subsequent slave writes
# return EIO. Then fire the deterministic trigger.
os.close(master)
time.sleep(2)
open(trigger, "w").close()

counts = {}
trigger_seen = False
t1 = time.time()
exited_early = None
env_probe = []
probed_env = False
while time.time() - t1 < OBSERVE:
    if not probed_env:
        # verify SIGNAL_EIO_* actually reached the electron processes
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open(f"/proc/{d}/environ", "rb") as f:
                    envblob = f.read()
                with open(f"/proc/{d}/cmdline", "rb") as f:
                    cmd = f.read().replace(b"\0", b" ").decode(errors="replace")
                hits = [l for l in envblob.split(b"\0")
                        if l.startswith(b"SIGNAL_EIO")]
                if "electron" in cmd or hits:
                    env_probe.append({"pid": d, "cmd": cmd[:80],
                                      "signal_eio_vars": [h.decode() for h in hits]})
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
        probed_env = True
    if os.path.exists(events):
        with open(events) as f:
            lines = [l for l in f.read().splitlines() if l.strip()]
        counts = {}
        for l in lines:
            try:
                k = json.loads(l).get("kind", "?")
            except json.JSONDecodeError:
                k = "?"
            counts[k] = counts.get(k, 0) + 1
        trigger_seen = counts.get("trigger-fired", 0) > 0
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        exited_early = round(time.time() - t1, 1)
        # give the 'exit' instrumentation a moment to flush
        time.sleep(1)
        break
    time.sleep(1)

# Teardown: kill the whole process group (dialog included).
try:
    os.killpg(pid, signal.SIGKILL)
except (ProcessLookupError, PermissionError):
    pass
try:
    os.waitpid(pid, 0)
except ChildProcessError:
    pass

summary = {
    "label": label,
    "ready_after_s": ready_at,
    "saw_stdout_output": saw_output,
    "alive_after_settle": alive_after_settle,
    "alive_at_end": exited_early is None,
    "exited_seconds_after_trigger": exited_early,
    "trigger_fired": trigger_seen,
    "event_counts": counts,
    "env_probe": env_probe[:6],
    "run_dir": os.path.basename(run_dir),
}
out = os.path.join(run_dir, "summary.json")
with open(out, "w") as f:
    json.dump(summary, f, indent=2)
print(json.dumps(summary))
