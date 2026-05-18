# avn vs. classic Subversion — 5000-commit bench

**Date**: 2026-05-17 (re-run after the heap-leak fixes — supersedes the
2026-05-15 run).
**Workload**: 5000 commits, each adding one 100 KB random-text file at
`f/{i}.txt`. Identical content stream (seed `0xA1`) for both systems.
**Harnesses**: `bench/bench_avn.py` (avn), `bench/bench_svn.py` (svn).
**Toolchain**: aetherc 0.168 + the `map_put_raw` fix; avn built with the
strbuilder refactor and the `string_new_with_length` / `string_substring_n`
heap-source fixes.

## Methodology — apples-to-apples

| | avn | svn |
|---|---|---|
| client | `avn commit <url> --add-file` — fresh process/commit, no working copy | `svnmucc put` — fresh process/commit, no working copy |
| transport | HTTP loopback, port 9990 | `svn://` loopback, port 9991 |
| server | `avnserver` — one persistent process | `svnserve -d -T --foreground` — one persistent **threaded** process |
| RSS sampling | 1 Hz poll of server `/proc/<pid>/status` | identical 1 Hz poll |

A WC-based `svn add` / `svn commit` was deliberately *not* used — it
would charge svn for working-copy bookkeeping that avn never does.
`svnmucc` is the true no-WC, one-remote-commit-per-process analog.
`svnserve -T` keeps svn to a single threaded process so its RSS curve
is directly comparable to avnserver's.

## Results

### Latency — tracks ✅

| | per-commit (first → last batch) | total wall | commits/sec |
|---|---|---|---|
| svn | 47 → 61 ms | 254 s | 19.7 |
| avn compress | 70 → 114 ms | 469 s | 10.7 |

Same shape — both flat with a mild rise, no quadratic tail. avn is
~1.8× svn's per-commit cost: modestly slower, same algorithmic class.

### Server RAM — still diverges, but 3.2× better than before ⚠️

| server | RSS start → end | shape |
|---|---|---|
| svnserve | 8.5 MB → ~30 MB (max 31 MB) | **plateaus** — bounded |
| avnserver compress | 5.2 MB → **804 MB** | linear ramp, ~160 KB/commit, still climbing at commit 5000 |

The previous run had avnserver at **2.57 GB** (~537 KB/commit) — driven
by a chain of compiler heap-ownership bugs (functions returning
`bytes.finish` / `string_new_with_length` / `string_substring_n` values
were never freed). Those are now all fixed upstream; the per-commit
accrual dropped 537 → ~160 KB/commit and the endpoint 2.57 GB → 804 MB.

But it is **still a ramp, not a plateau** — and the residual is *not* a
heap leak. valgrind massif shows avnserver's live heap flat at ~2 MB
over 100 commits; memcheck shows only ~3.8 KB/commit definitely-lost.
The 804 MB is process RSS bloat — glibc malloc arena retention and/or
mmap'd repo pages staying resident as avnserver churns ~100 KB blobs
across its thread pool. Closing the remaining gap to svn's 31 MB is an
allocator/mmap-tuning problem (`malloc_trim`, `MALLOC_ARENA_MAX`,
`munmap` audit), not a leak hunt.

### Disk — avn larger here 🔄

| | repo on disk (5000 commits, `du -sh`) |
|---|---|
| svn | 529 MB |
| avn compress | 855 MB |

Both store ~500 MB of incompressible random file content. avn pays an
extra ~350 MB because it **rewrites the whole `f/` directory blob every
commit** — commit N's directory holds N entries, and the cumulative
cost of those rewrites is O(N²) (~687 MB raw of `f SHA NAME` lines,
before compression). svn's fsfs shares / skip-deltas directory
structure across revisions and does not re-store it. (The 2026-05-15
run reported avn *smaller* than svn; that comparison is superseded —
current `du` measurement is the figure to trust.)

## Verdict

Latency tracks (svn ~1.8× faster, same shape). Disk: avn is larger on
this workload, due to full per-commit directory-blob rewrites.

**Server RAM is still the one real divergence** — but the picture has
changed. The old 2.57 GB figure was a genuine heap leak and is fixed;
avn now ends a 5000-commit run at 804 MB. The remaining 5 MB → 804 MB
ramp is allocator/mmap RSS retention, not leaked memory. svn still sets
the target — a commit server belongs in tens of MB — and avn is not yet
there, but the gap is now a tuning problem, not a correctness bug.
