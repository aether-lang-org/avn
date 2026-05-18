#!/usr/bin/env python3
# Bench classic Subversion: 5,000 commits, 100K random text each — the
# apples-to-apples counterpart to bench_avn.py.
#
# Fairness design (see bench/PATH_B_AUDIT.md and the avn-vs-svn discussion):
#   - avn bench  = `avn commit <url> --add-file`  : one fresh client
#     process, one remote commit, NO working copy, server-side CAS.
#   - svn analog = `svnmucc put`                  : one fresh client
#     process, one remote commit, NO working copy. A WC-based
#     `svn add` / `svn commit` would be unfair both ways (WC bookkeeping
#     svn pays and avn does not), so svnmucc is the right tool.
#
# Transport: svnserve in `-d -T --foreground` mode — daemon, THREADED
# (single process, not fork-per-connection), staying in the foreground
# so the Popen pid IS the server. That single long-lived process is
# what the 1Hz RSS poller samples, giving an RSS curve directly
# comparable to avnserver's (bench-{tag}_rss.csv from bench_avn.py).
#
# Same 100K random-text files at f/{i}.txt, same seed (0xA1), same
# batches of 50, same timings-CSV columns as bench_avn.py — diff the
# two CSVs directly.
#
# Output files (in this script's directory):
#   bench-svn.log           wall-clock progress log
#   bench-svn_timings.csv   batch_n, commits, batch_secs, avg_commit_secs,
#                           total_secs, repo_size_bytes
#   bench-svn_rss.csv       t_unix, rss_kb, vsz_kb  (1Hz svnserve sampler)
#   bench-svn_server.log    svnserve stderr/stdout

import argparse
import os
import random
import string
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_REPO_DIR = Path.home() / "svn_bench_repo"
PORT         = 9991                       # avn bench uses 9990
HOST         = "127.0.0.1"
TOTAL        = 5000
BATCH_SIZE   = 50
TEXT_SIZE    = 100 * 1024
AUTHOR       = "bench"

OUT_DIR      = Path(__file__).resolve().parent
LOG_FILE     = OUT_DIR / "bench-svn.log"
TIMINGS_FILE = OUT_DIR / "bench-svn_timings.csv"
SERVER_LOG   = OUT_DIR / "bench-svn_server.log"
RSS_FILE     = OUT_DIR / "bench-svn_rss.csv"


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    sys.stdout.write(line)
    sys.stdout.flush()
    with open(LOG_FILE, "a") as f:
        f.write(line)


def random_text(n: int, rng: random.Random) -> str:
    # Identical generator + seed to bench_avn.py, so the two benches
    # store byte-for-byte the same content stream.
    alphabet = string.ascii_letters + string.digits + " \n"
    return "".join(rng.choices(alphabet, k=n))


def repo_bytes(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=DEFAULT_REPO_DIR,
        help=f"Repo storage path (default: {DEFAULT_REPO_DIR}).",
    )
    args = parser.parse_args()
    REPO_DIR = args.repo_dir

    for tool in ("svnadmin", "svnserve", "svnmucc"):
        if subprocess.run(["which", tool], capture_output=True).returncode != 0:
            print(f"tool missing: {tool}", file=sys.stderr)
            return 2

    if not REPO_DIR.parent.exists():
        print(f"mount missing: {REPO_DIR.parent}", file=sys.stderr)
        return 2

    LOG_FILE.write_text("")
    SERVER_LOG.write_text("")
    with open(TIMINGS_FILE, "w") as tf:
        tf.write("batch_n,commits,batch_secs,avg_commit_secs,total_secs,repo_size_bytes\n")
        tf.flush()

    if REPO_DIR.exists():
        log(f"removing existing {REPO_DIR}")
        subprocess.run(["rm", "-rf", str(REPO_DIR)], check=True)

    log(f"creating fsfs repo at {REPO_DIR}")
    subprocess.run(["svnadmin", "create", str(REPO_DIR)], check=True)

    # Allow anonymous commits so svnmucc needs no passwd/auth round-trip.
    # Fresh `svnadmin create` ships svnserve.conf with anon-access=read;
    # bump it to write. --username on svnmucc still tags the author.
    (REPO_DIR / "conf" / "svnserve.conf").write_text(
        "[general]\nanon-access = write\n"
    )

    URL = f"svn://{HOST}:{PORT}"

    log(f"starting svnserve (-d -T --foreground) on port {PORT}")
    srv_log = open(SERVER_LOG, "w")
    server = subprocess.Popen(
        ["svnserve", "-d", "-T", "--foreground",
         "-r", str(REPO_DIR),
         "--listen-host", HOST, "--listen-port", str(PORT)],
        stdout=srv_log,
        stderr=subprocess.STDOUT,
    )
    time.sleep(2)
    if server.poll() is not None:
        srv_log.close()
        log(f"server died on startup; see {SERVER_LOG}")
        return 1

    # 1Hz RSS sampler — identical shape to bench_avn.py's poller so the
    # RSS CSVs line up column-for-column.
    poller_cmd = (
        f"echo 't_unix,rss_kb,vsz_kb' > {RSS_FILE}; "
        f"while [ -r /proc/{server.pid}/status ]; do "
        f"  rss=$(awk '/^VmRSS:/ {{print $2}}' /proc/{server.pid}/status 2>/dev/null); "
        f"  vsz=$(awk '/^VmSize:/ {{print $2}}' /proc/{server.pid}/status 2>/dev/null); "
        f"  if [ -n \"$rss\" ]; then echo \"$(date +%s),$rss,$vsz\" >> {RSS_FILE}; fi; "
        f"  sleep 1; "
        f"done"
    )
    poller = subprocess.Popen(["/bin/bash", "-c", poller_cmd])

    # Untimed setup: create the f/ directory once (rev 1). svnmucc `put`
    # does not create intermediate dirs; avn creates them implicitly, so
    # folding this single mkdir into setup keeps the timed loop a clean
    # run of 5000 identical `put` commits.
    mk = subprocess.run(
        ["svnmucc", "--non-interactive", "--no-auth-cache",
         "--username", AUTHOR, "-m", "init f/", "mkdir", f"{URL}/f"],
        capture_output=True, timeout=30,
    )
    if mk.returncode != 0:
        log(f"mkdir f/ failed: {(mk.stderr or mk.stdout)[-300:]!r}")
        server.terminate()
        return 1

    content_file = OUT_DIR / "bench-svn_content.tmp"

    rng = random.Random(0xA1)
    wall_start  = time.time()
    batch_start = wall_start
    last_i      = 0

    try:
        log(f"starting {TOTAL} commits × {TEXT_SIZE}B random text, batches of {BATCH_SIZE}")
        for i in range(1, TOTAL + 1):
            content_file.write_text(random_text(TEXT_SIZE, rng))
            argv = [
                "svnmucc", "--non-interactive", "--no-auth-cache",
                "--username", AUTHOR,
                "-m", f"r{i}: random",
                "put", str(content_file), f"{URL}/f/{i}.txt",
            ]
            r = subprocess.run(argv, capture_output=True, timeout=120)
            if r.returncode != 0:
                tail = (r.stderr or r.stdout or b"")[-300:]
                log(f"COMMIT FAIL at i={i}: rc={r.returncode}: {tail!r}")
                break
            last_i = i

            if i % BATCH_SIZE == 0:
                now         = time.time()
                batch_secs  = now - batch_start
                total_secs  = now - wall_start
                avg         = batch_secs / BATCH_SIZE
                size        = repo_bytes(REPO_DIR)
                batch_n     = i // BATCH_SIZE
                with open(TIMINGS_FILE, "a") as tf:
                    tf.write(
                        f"{batch_n},{i},{batch_secs:.2f},{avg:.4f},"
                        f"{total_secs:.2f},{size}\n"
                    )
                    tf.flush()
                log(
                    f"batch {batch_n}: {i}/{TOTAL} commits, "
                    f"batch={batch_secs:.1f}s, avg={avg*1000:.0f}ms, "
                    f"repo={size // (1024 * 1024)}MB, total={total_secs:.0f}s"
                )
                batch_start = now

        elapsed = time.time() - wall_start
        log(f"DONE. {last_i} commits in {elapsed:.0f}s "
            f"({last_i / elapsed:.1f} commits/sec mean)")

    finally:
        log("stopping svnserve")
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        srv_log.close()
        try:
            poller.wait(timeout=3)
        except subprocess.TimeoutExpired:
            poller.kill()
            poller.wait()
        try:
            content_file.unlink()
        except OSError:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
