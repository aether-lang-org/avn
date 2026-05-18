# Codegen audit: 0.144 → 0.158 — why the avn bench still doubles

**Date**: 2026-05-13
**aetherc**: 0.144.0 (last healthy baseline) vs 0.158.0 (current)
**Bench**: `bench/bench_avn.py --no-compress`, 5000 commits

The avn 5000-commit bench, healthy at 0.144 (385s wall, 3.3 GB RSS),
still doubles per-batch on 0.158 despite the team landing Path B's
return-time lift, argument-position drain, cross-fn recursion fix,
and self-assign double-free fix. This document captures the
codegen-level diff between the two versions and identifies what's
actually driving the residual O(N²) growth.

**Summary**: 0.144's apparently-healthy bench was leveraging a
lucky-UAF — the `_heap_X = 0` everywhere at call sites meant the
callee's defer-free ran on the returned buffer, but glibc keeps
freed slots valid for read until they're reused, so the caller's
follow-on read of the "freed" data still worked. 0.158 closes the
UAF correctly (every call site now has `_heap_X = 1` plus a
return-time lift), but the working-set shape of the per-commit
operations is different — more buffers live simultaneously inside
the reclist-build loop, more allocator work per call, more
fragmentation. The team's heap-tracker is now correct; the residual
perf cost is algorithmic (string-builder-in-a-loop is O(N²) memcpy)
plus allocator-free-list overhead.

## Codegen pattern density comparison

| module          | _heap=0 0.144 | _heap=0 0.158 | _heap=1 0.144 | _heap=1 0.158 | lifts | arg-drains |
|-----------------|--------------:|--------------:|--------------:|--------------:|------:|-----------:|
| `avnserver`     |          6939 |          6865 |           406 |           913 |   451 |        164 |
| `client`        |          1821 |          1829 |           198 |           386 |   241 |         71 |
| `repos`         |          2052 |          2061 |           188 |           436 |   316 |         75 |
| `repo_storage`  |          1055 |          1090 |            80 |           167 |    84 |         50 |
| `util`          |            71 |            73 |             0 |             0 |     2 |          0 |
| **total**       |     **11938** |     **11918** |       **872** |      **1902** | **1094** |   **360** |

0.158 emits **~1030 new heap-ownership sites** (`_heap_X = 1`) plus
**1094 lift wrappers** (`aether_uniform_heap_str`) plus **360
arg-drain blocks** (`_ad_NN` GCC statement expressions). Each one
is runtime work per call. The migration also exposed the
previously-hidden `_heap_X = 0` sites as legitimate ownership
transfers — the count there is unchanged (correct call-site
density was the same; the change is in what the wrappers DO at
each site).

## The hot-loop pattern that drives growth

`repo_storage/module.ae:reclist_add` (line 2213) — called per dir
entry in `rebuild_dir`. The body iterates over `body` (the existing
dir blob) appending to `out`:

```aether
reclist_add(body: string, name: string, kind: int, sha: string) -> string {
    found = 0
    out = ""
    it = dir_iter_new(body)
    while dir_iter_next(it) == 1 {
        ename = dir_iter_name(it)
        if string.equals(ename, name) == 1 {
            found = 1
            out = string.concat(out, dir_entry_line(kind, sha, name))
        }
        if string.equals(ename, name) == 0 {
            out = string.concat(out, dir_entry_line(dir_iter_kind(it), dir_iter_sha(it), ename))
        }
    }
    dir_iter_free(it)
    if found == 0 {
        out = string.concat(out, dir_entry_line(kind, sha, name))
    }
    return out
}
```

For a dir of M entries: inner loop runs M times, each iteration
allocates a `dir_entry_line` (~80 B) plus a new `out` buffer of
size O(current_out_size). Total memcpy work per call:
`sum_{i=0..M} 80i = 40M²` bytes.

Outer caller (`rebuild_dir`) invokes this per commit. At commit N
the dir has N entries; rebuild_dir calls reclist_add N times; each
call has M=current size 1..N. Total per-commit memcpy work is
`O(N²)`.

**This algorithmic O(N²) was true at 0.144 too** — the bench was
already paying the same memcpy cost. The difference under 0.158:
the per-call working set is larger, and the allocator free-list
walks are longer.

## What 0.144 was doing (lucky-UAF)

0.144 codegen at the `out = string.concat(out, dir_entry_line(...))`
site:

```c
{ const char* _tmp_old = out;
  out = string_concat(out, dir_entry_line(kind, sha, name));
  if (_heap_out) free((void*)_tmp_old);
  _heap_out = 0; }
```

`_heap_out = 0` means "caller doesn't own this." So:

- The OLD `out` (the one being replaced) was returned by a previous
  `string_concat` call. That call's defer-free **already fired** on
  the buffer when string_concat exited. The buffer is technically
  freed.
- glibc kept the slot valid because nothing else has allocated since.
- The current iteration's `string_concat` reads from the "freed" old
  out as input, writes a new heap allocation, returns. Its defer-free
  fires on the NEW out at exit (which the caller then reads from
  same way).
- Net: each iteration writes a new buffer, glibc reuses the previous
  freed slot for the next allocation, RSS stays low.

This is UAF — `out` is freed in callee's defer-free before caller
reads it — but the timing works out under glibc-on-Linux.

Per-iteration allocator work:
- 1 free (callee's defer-free on previous string_concat return)
- 1 malloc (string_concat's new buffer)
- Free + alloc usually hit the same slot (single-threaded allocator,
  same size class), so very cheap.

## What 0.158 does (correctly)

0.158 codegen at the same site:

```c
{ const char* _tmp_old = out;
  out = ({ const char* _ad_36 = (const char*)(dir_entry_line(dir_iter_kind(it), dir_iter_sha(it), ename));
          const char* _ad_r = string_concat(out, _ad_36);
          free((void*)_ad_36);
          _ad_r; });
  if (_heap_out) free((void*)_tmp_old);
  _heap_out = 1; }
```

- `_ad_36` is the arg-drain temp holding `dir_entry_line`'s heap
  return. Freed after the outer `string_concat` consumes it.
- `_heap_out = 1` — caller now owns the result.
- The reassignment wrapper frees the previous `out` (via
  `_tmp_old`).

Per-iteration allocator work:
- 1 malloc (dir_entry_line return)
- 1 malloc (string_concat new buffer)
- 1 free (dir_entry_line, after string_concat)
- 1 free (previous out, via wrapper)
- Two pairs of free+malloc but with DIFFERENT sizes. Glibc may
  bucket-into different size classes; the previous-out's slot
  isn't directly reusable by the new (larger) string_concat result.

Each iteration the address space grows by ~80 B (the new out is
80 B larger than the previous), and the previous out's slot
becomes a free-list entry of the OLD size. After M iterations
inside reclist_add, the free list has M chunks of sizes 0, 80, 160,
…, 80(M-1). The next outer call's reclist_add will see those
chunks but can only reuse the right-sized ones.

After N outer calls (one per commit), the free list has accumulated
~N² small chunks. Each malloc has to walk a longer free list,
making per-malloc time grow with N. Per-commit time grows as
N (algorithmic) × N (per-malloc cost) = O(N²).

## Experiments

### Glibc malloc trimming

Hypothesis: maybe glibc keeps freed pages around. Test with
aggressive trim threshold:

```sh
MALLOC_TRIM_THRESHOLD_=131072 MALLOC_MMAP_THRESHOLD_=131072 \
  python3 bench/bench_avn.py --no-compress    # 300 commits
```

Result:

| metric                     | default | with TRIM=131072 |
|---------------------------:|--------:|-----------------:|
| RSS at 300 commits         | 1.91 GB |          0.98 GB |
| batch 6 avg per-commit     |  2384ms |           2431ms |
| total 300-commit wall      |    259s |             263s |

**Trim halves RSS but doesn't change wall time.** Confirms the
perf cost is *intra-process free-list walks*, not pages-held-from-
kernel. Reducing fragmentation in the heap structure would help;
trimming pages back to kernel doesn't.

### Per-version trajectory

```
aetherc  batch1  batch5  batch7   notes
0.144     76ms   80ms    86ms    lucky-UAF, healthy
0.149    72ms  1408ms  3861ms    O(N²) — escape-marked clear broke return-escape
0.150    72ms  1429ms       —    O(N²) — classifier-piece-2 didn't reach avn shape
0.156    73ms  1429ms  3831ms    Path B return lift landed; some leak still present
0.157    73ms  1429ms       —    arg-drain fix landed; crashed at #261 (double-free)
0.158    68ms  1417ms  3785ms    self-assign fix landed; bench runs but same O(N²)
```

Each release closes a correctness-and-leak class but the bench
shape doesn't recover the 0.144 lucky-UAF performance. **0.158 is
the first version where the bench is both correct AND
algorithmically honest** about what `rebuild_dir`'s shape costs.

## What would actually fix the bench

This is no longer an Aether-team issue — the heap-tracker is
correct as of 0.158. The remaining cost is avn-side algorithmic.

### Option A — geometric-growth string-builder primitive

Replace `out = string.concat(out, x)` loops with a builder:

```aether
b = builder.new(estimated_cap)
while iter.next() { builder.append(b, dir_entry_line(...)) }
out = builder.finish(b)
```

Builder allocates 2× capacity on overflow → O(log N) reallocs
instead of O(N). Drops reclist_add inner loop from O(N) allocs to
O(log N).

Needs a `std.builder` or `std.strvec` module. Doesn't exist
upstream yet. Filing as an enhancement request makes sense; could
be 200 lines of stdlib C + Aether wrapper.

### Option B — refactor reclist_add to one-pass

Build the dir blob with a single pre-sized allocation (sum up
entry sizes first, then allocate once). Drops memcpy work from
O(N²) to O(N) per call. avn-side change, ~30 lines.

### Option C — peephole: detect `lhs = string.concat(lhs, x)` and grow-in-place

Upstream codegen optimization. Recognize the in-place-append shape
and emit `realloc + memcpy(end, x)` instead of `malloc +
memcpy(both) + free`. Drops alloc count from 2 per iteration to 1
amortised; matches builder semantics without requiring user opt-in.

## Recommendation

**File Option A upstream as a feature request** — `std.builder` or
`std.strvec` is a generally useful primitive, not avn-specific.
The existing `std.bytes.new` / `bytes.finish` pattern is the
building block; what's missing is the amortized-growth wrapper.

**Implement Option B avn-side as a stopgap.** ~30 lines in
`repo_storage/module.ae:reclist_add` / `reclist_sort` to do a
single-pass build. Even with the existing primitives this drops the
hot-loop from O(N²) to O(N) per call and should restore most of
the bench's 0.144 perf.

**Don't pursue Option C** — peephole optimization is fragile across
codegen rewrites, and the explicit builder primitive (Option A) is
the same end state with clearer semantics.

## Cross-references

- `bench/RESULTS.md` — R287 baseline (3.3 GB final RSS, 405s wall).
  0.158 currently exceeds this on both axes.
- `aether/perf_regression_quadratic_commit_path.md` (and the
  `_followup` and Path B docs) — the trail of upstream fixes that
  led to where we are now.
- `aether/CHANGELOG.md` 0.149 → 0.158 entries — the correctness
  fixes that landed; all sound, none of which address the algorithmic
  shape.

## Captured codegen for follow-up

`/tmp/codegen_audit/{repo_storage,repos,avnserver,client,util}_{0144,0158}.c`
hold the side-by-side generated C for the hot-path modules under
both versions. The cleanest comparison is `rebuild_dir_0144.c` vs
`rebuild_dir_0158.c` (264 lines each, ~30 diff sites all showing
the `_heap_X = 0` → `_heap_X = 1` flip).
