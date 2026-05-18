#!/usr/bin/env python3
# RSS-leak probe — smallest-repro driver for avnserver's per-commit RSS
# ramp (see bench/AVN_VS_SVN.md: ~537 KB/commit compress).
#
# Fires a SMALL number of commits (default 60) at an avnserver that is
# optionally wrapped in valgrind, so the linear ramp is reproduced in
# ~30 MB of heap growth with full allocation stacks — no need for the
# 5000-commit run.
#
#   --commits N      number of commits to fire (default 60)
#   --text-size N    bytes per file (default 102400). Shrink to decompose
#                    the per-commit leak into a file-size-proportional
#                    part and a fixed part.
#   --tool X         none | massif | memcheck   (default none)
#   --no-compress    run server with AVN_NO_COMPRESS=1
#
# Outputs (this dir):
#   leak-{tool}.massif / leak-{tool}_memcheck.log   valgrind artifact
#   leak_probe_rss.csv                              1Hz server RSS
#   leak_probe_server.log                           server stdout/stderr

import argparse, os, random, re, string, subprocess, sys, time
from pathlib import Path

SHA_RE   = re.compile(r"\(sha: ([a-f0-9]{40})\)")
PORT     = 9993
REPO     = "leak"
ROOT     = Path(__file__).resolve().parent.parent
SERVER   = ROOT / "target/avnserver/bin/avnserver"
ADMIN    = ROOT / "target/avnadmin/bin/avnadmin"
AVN      = ROOT / "target/avn/bin/avn"
OUT      = Path(__file__).resolve().parent
REPO_DIR = Path.home() / "leak_probe_repo"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commits", type=int, default=60)
    ap.add_argument("--text-size", type=int, default=100 * 1024)
    ap.add_argument("--tool", choices=["none", "massif", "memcheck"], default="none")
    ap.add_argument("--no-compress", action="store_true")
    args = ap.parse_args()

    for b in (SERVER, ADMIN, AVN):
        if not b.exists():
            print(f"missing binary: {b}", file=sys.stderr); return 2

    if REPO_DIR.exists():
        subprocess.run(["rm", "-rf", str(REPO_DIR)], check=True)
    subprocess.run([str(ADMIN), "create", str(REPO_DIR)], check=True)

    massif_out  = OUT / "leak.massif"
    memcheck_lg = OUT / "leak_memcheck.log"
    srv_cmd = [str(SERVER), REPO, str(REPO_DIR), str(PORT)]
    if args.tool == "massif":
        massif_out.unlink(missing_ok=True)
        srv_cmd = ["valgrind", "--tool=massif",
                   f"--massif-out-file={massif_out}",
                   "--detailed-freq=1", "--max-snapshots=200",
                   "--time-unit=ms"] + srv_cmd
    elif args.tool == "memcheck":
        srv_cmd = ["valgrind", "--tool=memcheck", "--leak-check=full",
                   "--show-leak-kinds=all", "--num-callers=30",
                   "--track-origins=no", f"--log-file={memcheck_lg}"] + srv_cmd

    env = os.environ.copy()
    if args.no_compress:
        env["AVN_NO_COMPRESS"] = "1"

    srv_log = open(OUT / "leak_probe_server.log", "w")
    print(f"starting server (tool={args.tool}): {' '.join(srv_cmd)}")
    server = subprocess.Popen(srv_cmd, stdout=srv_log, stderr=subprocess.STDOUT, env=env)

    # valgrind startup is slow — poll the port instead of a fixed sleep.
    url = f"http://127.0.0.1:{PORT}/{REPO}"
    deadline = time.time() + 180
    while time.time() < deadline:
        if server.poll() is not None:
            print("server died on startup; see leak_probe_server.log", file=sys.stderr)
            return 1
        probe = subprocess.run([str(AVN), "info", url], capture_output=True)
        if probe.returncode == 0:
            break
        time.sleep(1)
    else:
        print("server never came up", file=sys.stderr); server.kill(); return 1
    print(f"server up after {time.time() - (deadline - 180):.0f}s")

    rss_csv = OUT / "leak_probe_rss.csv"
    poller = subprocess.Popen(["/bin/bash", "-c",
        f"echo 't_unix,rss_kb' > {rss_csv}; "
        f"while [ -r /proc/{server.pid}/status ]; do "
        f"  r=$(awk '/^VmRSS:/{{print $2}}' /proc/{server.pid}/status 2>/dev/null); "
        f"  [ -n \"$r\" ] && echo \"$(date +%s),$r\" >> {rss_csv}; sleep 1; done"])

    rng = random.Random(0xA1)
    alphabet = string.ascii_letters + string.digits + " \n"
    last_sha = ""
    t0 = time.time()
    try:
        for i in range(1, args.commits + 1):
            content = "".join(rng.choices(alphabet, k=args.text_size))
            argv = [str(AVN), "commit", url, "--author", "leak",
                    "--log", f"r{i}", "--add-file", f"f/{i}.txt={content}"]
            if last_sha:
                argv += ["--parent-sha", last_sha]
            r = subprocess.run(argv, capture_output=True, timeout=300)
            if r.returncode != 0:
                print(f"COMMIT FAIL i={i}: {(r.stderr or r.stdout)[-300:]!r}", file=sys.stderr)
                break
            m = SHA_RE.search((r.stdout or b"").decode("utf-8", "replace"))
            if not m:
                print(f"NO SHA i={i}: {r.stdout[:200]!r}", file=sys.stderr); break
            last_sha = m.group(1)
            if i % 10 == 0:
                print(f"  commit {i}/{args.commits}  ({time.time()-t0:.0f}s)")
    finally:
        print("stopping server (valgrind writes its report on exit)")
        server.terminate()
        try:
            server.wait(timeout=120)
        except subprocess.TimeoutExpired:
            server.kill(); server.wait()
        srv_log.close()
        try:
            poller.wait(timeout=3)
        except subprocess.TimeoutExpired:
            poller.kill()

    if args.tool == "massif":
        print(f"massif report: {massif_out}  — run: ms_print {massif_out}")
    elif args.tool == "memcheck":
        print(f"memcheck log: {memcheck_lg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
