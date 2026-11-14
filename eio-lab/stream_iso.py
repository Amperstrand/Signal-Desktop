#!/usr/bin/env python3
"""Stream-isolation diagnostic: which stdio stream's death kills Signal?

variantA: stdout=PTY (dies at close), stderr=file (lives)
variantB: stdout=file (lives), stderr=PTY (dies at close)
control : both on PTY (baseline, both die)
Measures electron-main survival precisely (direct pid watch, not wrapper).
"""
import glob, json, os, selectors, signal, sys, time

REPO = os.path.expanduser("~/src/Signal-Desktop")
variant = sys.argv[1] if len(sys.argv) > 1 else "control"
BASE = "/tmp/opencode/signal-eio/runs"
run_dir = os.path.join(BASE, f"iso-{variant}-{int(time.time())}")
os.makedirs(run_dir, exist_ok=True)
events = os.path.join(run_dir, "events.jsonl")
trigger = os.path.join(run_dir, "trigger")
redir_file = os.path.join(run_dir, "surviving-stream.log")
for p in (events, trigger, redir_file):
    if os.path.exists(p):
        os.unlink(p)

master, slave = os.openpty()
pid = os.fork()
if pid == 0:
    os.setsid()
    os.dup2(slave, 0)                      # stdin always PTY
    redir = os.open(redir_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    if variant == "A":   os.dup2(slave, 1); os.dup2(redir, 2)   # stdout dies, stderr lives
    elif variant == "B": os.dup2(redir, 1); os.dup2(slave, 2)   # stdout lives, stderr dies
    elif variant == "C":
        pass  # handled below
    else:                os.dup2(slave, 1); os.dup2(slave, 2)   # both die (baseline)
    if slave > 2: os.close(slave)
    os.close(master)
    env = dict(os.environ)
    env.update({
        "SIGNAL_EIO_INSTR": "1", "SIGNAL_EIO_EVENTS": events,
        "SIGNAL_EIO_TRIGGER": trigger, "DISPLAY": ":0",
        "XDG_RUNTIME_DIR": "/run/user/1000", "ELECTRON_DISABLE_SANDBOX": "1",
        "ELECTRON_ENABLE_LOGGING": "1",
    })
    env.pop("WAYLAND_DISPLAY", None)
    os.chdir(REPO)
    e = os.path.join(REPO, "node_modules", ".bin", "electron")
    os.execve(e, [e, ".", "--storage-profile=eio", "--ozone-platform=x11"], env)
os.close(slave)

# find the electron MAIN pid (not the node wrapper)
def find_main():
    for proc in glob.glob('/proc/[0-9]*'):
        try:
            cmd = open(f'{proc}/cmdline', 'rb').read().replace(b'\0', b' ').decode()
            if 'storage-profile=eio' in cmd and '--type=' not in cmd:
                return int(proc.split('/')[-1])
        except OSError:
            continue
    return None

t0 = time.time()
main_pid = None
sel = selectors.DefaultSelector()
sel.register(master, selectors.EVENT_READ)
ml = open(os.path.join(run_dir, "master.log"), "wb")
deadline = t0 + 40
while time.time() < deadline:
    for k, _ in sel.select(0.4):
        try: chunk = os.read(master, 65536)
        except OSError: chunk = b""
        if chunk: ml.write(chunk)
    main_pid = main_pid or find_main()
    ready = os.path.exists(events) and 'instr-ready' in open(events, errors='ignore').read()
    if main_pid and ready: break
ml.close()

def alive(p):
    try: os.kill(p, 0); return True
    except (ProcessLookupError, TypeError): return False

close_t = time.time()
os.close(master)
time.sleep(2)
open(trigger, 'w').close()

# poll the MAIN pid at 150ms; capture last-known state
last_state = ""
death_at = None
while time.time() - close_t < 40:
    if main_pid and alive(main_pid):
        try:
            st = open(f'/proc/{main_pid}/stat').read().rsplit(')', 1)[1].split()
            last_state = f"state={st[0]}"
        except OSError: last_state = "gone"
    else:
        death_at = round(time.time() - close_t, 2)
        break
    time.sleep(0.15)

# grace: maybe events arrive late
time.sleep(2)
ev = {}
if os.path.exists(events):
    for l in open(events, errors='ignore'):
        try: k = json.loads(l)['kind']; ev[k] = ev.get(k, 0) + 1
        except Exception: pass

survivor_tail = ""
if os.path.exists(redir_file):
    survivor_tail = open(redir_file, errors='ignore').read()[-600:]

print(json.dumps({
    "variant": variant, "main_pid": main_pid, "ready_ok": bool(main_pid),
    "main_died_s_after_close": death_at if death_at else ("alive_40s" if main_pid else "?"),
    "last_state": last_state, "events": ev,
    "surviving_stream_tail": survivor_tail.splitlines()[-4:],
}, indent=1)[:2200])

try: os.killpg(pid, signal.SIGKILL)
except Exception: pass
