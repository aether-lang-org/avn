# Current plan — avn perf / RSS work

_Updated 2026-05-17._

## Headline

The **heap-string leak is fixed.** It was the filed problem and the
driver of the old 2.57 GB RSS ramp. Verified: massif heap is flat at
~2 MB over 100 commits (was a 20 MB+ ramp); leak_probe memcheck shows
8.4 MB → 94 KB lost over 25 commits.

But the 5000-commit bench still shows RSS climbing to **~800 MB** — and
that is **not** a heap leak (see below). It's a separate problem.

**avn-vs-svn re-run 2026-05-17** (`bench/AVN_VS_SVN.md` refreshed):
avn 469 s / svn 254 s (latency ~1.8×, same shape); avn RSS
5 MB → 804 MB ramp / svn 8.5 → 31 MB plateau; disk avn 855 MB > svn
529 MB — avn now *larger*, from O(N²) full per-commit directory-blob
rewrites (a workload-specific architectural cost, newly noted).

## Toolchain

aetherc **0.168** (`c75fa3a`) — all four bugs from
`aether/new_string_len_something.md` fixed and verified (the four
`aether/repro-*.ae` all pass). aeb at `de945a8`.

## What's fixed and verified

- **4 Aether bugs** (`new_string_len_something.md` §1–§4): heap-source
  sweep (`string_substring_n`, `string_from_*`), two-closure heap
  tracker, closure-captured libc-name, nested-decl-in-closure typecheck.
- **Heap leak**: massif flat ~2 MB / 100 commits; memcheck 94 KB lost /
  25 commits (~3.8 KB/commit, diffuse small allocations — no systemic
  leak left).
- **5000-commit bench**: 463 s, 10.8 commits/s, per-commit latency flat
  ~100 ms. Builds clean against 0.168.
- **aeb toolchain-aware caching** (`13e562b`) — link cache + regen pass
  both rebuild correctly on a toolchain change. Verified.

## Open — RSS still ramps to ~800 MB, but it is NOT a heap leak

5000-commit bench RSS: 5 MB → 804 MB, a steady ramp (~160 KB/commit),
no plateau. svn plateaus at 31 MB. (Was 2.57 GB / ~537 KB/commit
before the heap-leak fixes — 3.2× better, but still a ramp.)

Evidence it is not a heap leak:
- massif: avnserver's live heap is flat at ~2 MB over 100 commits.
- memcheck: only ~3.8 KB/commit definitely-lost; "still reachable"
  ~174 KB total at 25 commits (not ramping).
- rep-cache.db / merges.db total ~3.9 MB — not the source.

So the 868 MB is process RSS bloat from **mmap and/or glibc malloc
arena retention** — avnserver churns ~100 KB blobs every commit across
a thread pool; freed memory is not returned to the OS, and/or repo
files are mmap'd and stay resident. This is a distinct investigation:
allocator/mmap tuning, not a leak hunt. Candidate angles:
`malloc_trim` / `MALLOC_ARENA_MAX` / `mallopt`, checking whether file
reads go through `mmap` without `munmap`, sqlite mmap settings.

## Open — disk O(N²): delta-encoded directory storage (SPEC'D, next round)

5000-commit bench disk: avn 855 MB > svn 529 MB. Cause: `rebuild_dir`
rewrites the whole directory blob every commit (`rep_encode_blob` does
raw/zlib only) → O(N²). Fix = delta-encode directory reps — what svn
fsfs does; avn's `delta/` svndiff machinery already exists, just
unwired. Implementation-ready spec: **`delta-dir-storage-round.md`** —
delta-chain + a `.dircache/` sidecar (O(N) disk, commit hot-path O(1),
old-rev reads chain). Deferred to a dedicated round — it's a core
storage-format change with corruption-class blast radius.

## RESOLVED — the `bash -c` / driver-tests-fail bug

Root cause: avn had **dot-prefixed its `aether.driver_test` driver
*source* files** (`avn/.test_*_driver.ae` etc.). aeb's full-tree scan
treats every dot-prefixed `.ae` as a build-graph target, so the
orchestrator ran each driver's `main()` (an aeocha test) directly,
in-process, with no fixture → `os.getenv("AVN_BIN")` NULL →
`string.concat(...,NULL)` NULL command → `bash -c` with no arg.
Proven via `_sorted.txt` (driver files listed as nodes), ASan-clean
(not memory), strace (`["/bin/bash","-c"]`, cmd absent from call #1).

Fix (avn-side, done): renamed all 30 `*/.test_*_driver.ae` →
`*/test_*_driver.ae` and updated the `driver("…")` refs in the
`.tests-*.ae` files. `cd avn && aeb` → 0 `bash -c` errors, driver
tests run with fixtures: **324 passing / 32 failing**. No aeb code
change needed (aeb Claude may add a defensive guard — their call).
Filed resolution: `aeb/bash-issue.md`.

## RESOLVED — `double free or corruption` (aetherc codegen bug, fixed)

Root cause: aetherc rewrote an explicit `map_put_raw(map, key, value)`
→ `map_put_string_owned` whenever `value` was a string-typed *bare
identifier* — including a literal-only one (`is_heap_string_expr` on a
bare identifier answers "heap-tracked name?", true for any assigned
string var). The owned variant made `map_free` `free()` a `.rodata`
literal. Filed `aether/map-put-raw-rewritten-to-owned.md` (+ 9-line
repro); the Aether team fixed it — the owned-routing now resolves a
bare identifier *structurally* (`body_assigns_var_from_heap`) so a
literal-only var stays on the non-owning path. (Note: my proposed fix
— gate on the runtime `_heap_<value>` flag — would NOT have worked;
that flag reads stale for a value escaped into a container call. The
structural resolution is the correct one.)

**Full suite after the fix: 345 passing / 1 failing**, 0 double-frees,
0 `bash -c` errors. No avn source change was needed.

## Open — 1 residual: `branch_create` (D) — `feat/src` empty

`avn branch create feat --include 'src/**' --include README` → the
`feat` branch's `src/` is empty; `src/main.c` is dropped. Confirmed
genuine, not a display bug: `/rev/4/cat/src/main.c` → `not found`.

**Investigation (narrowed, not finished):**
- A standalone harness calling `repo_storage.branch_create` directly,
  with `filter_dir` instrumented, was built (hand-linked past avn's
  `repos`↔`repo_storage` extern cycle). Its trace **proves
  `filter_dir` matches `src/main.c`** — `[FILT] file match=1`. So the
  glob matcher / `path_join_rel` / `dir_entry_*` are all fine; the
  drop happens in `filter_dir`'s **post-match path** (`include=1` →
  `dir_entry_line` → `body` `string.concat` → `rep_write_blob` → the
  parent's `src` entry).
- Couldn't trace the post-match path: the hand-linked harness's
  `--allow-multiple-definition` link resolves `filter_dir` to the
  `--emit=lib` `module_generated.c` copy (no post-match prints), not
  the instrumented one. Forcing the harness's own copy needs
  whole-programming the full `repos`↔`repo_storage` cycle into one TU.
- Server-side tracing is dead: `println` from `--emit=lib`-compiled
  `repo_storage` produces nothing.
- Suspected: a heap-ownership quirk in `--emit=lib` codegen on the
  `dir_entry_line(...)` result or `body` — consistent with the
  session's other `--emit=lib` findings — but unproven.

Pre-existing (reproduces with the strbuilder refactor stashed). 1 of
346. Finishing needs a harness that whole-programs the avn module
graph so the instrumented `filter_dir` actually runs.

## Artifacts

- `bench/bench_avn.py`, `bench/bench_svn.py`, `bench/leak_probe.py`,
  `bench/AVN_VS_SVN.md`
- `aether/new_string_len_something.md` + 4 `repro-*.ae` — all fixed
- prior filings (all landed): `heap-ownership-return-propagation.md`,
  `string-new-with-length-heap-annotation.md`,
  `tuple-destructure-in-closure-scope.md`
- `repo_storage/module.ae` — strbuilder refactor (uncommitted); R313 reverted
