# Round spec — delta-encoded directory storage

_Spec'd 2026-05-18. Status: ready to implement (own round). Decided
approach: **delta-chain + sidecar cache** (not skip-deltas — see §10)._

## 1. Problem

avn is a content-addressed Merkle store: every commit, `rebuild_dir`
writes the **whole** directory blob fresh (`rep_write_blob` →
`rep_encode_blob`, which today only does `R`aw / `Z`lib). A directory
with N entries, rewritten N times, costs **O(N²)** cumulative disk.

Measured: 5000-commit bench (`bench/AVN_VS_SVN.md`, 2026-05-17) — avn
repo **855 MB** vs svn **529 MB**; ~326 MB of the gap is cumulative
`f/` directory-blob rewrites (~687 MB raw of `f SHA NAME\n` lines
before zlib). svn's fsfs delta-encodes directory noderevs and pays
O(N); avn should too.

## 2. Goal / non-goals

**Goal:** directory-blob storage O(N²) → O(N). avn repo on the bench
workload drops from 855 MB toward svn's ~530 MB. **No commit-latency
regression** (the sidecar cache keeps the commit hot-path O(1)).

**Non-goals (v1):**
- Skip-deltas / O(log N) arbitrary-rev reads. v1 old-rev reads chain
  O(chain-depth) — acceptable, rare. A later round if needed.
- Delta-encoding *file* blobs — v1 is **directory blobs only** (the
  O(N²) culprit). Files stay raw/zlib.
- `gc` / repack.
- Back-compat with old avn binaries reading new repos (avn has no
  users — `D` reps written by new avn need not be readable by old avn;
  the reverse — new avn reading old `R`/`Z`-only repos — must work and
  does, since decode dispatches on the tag).

## 3. Design overview

A directory blob may be stored as a **delta against its immediate
predecessor** (the previous version of the same directory). Disk
becomes O(N): each commit's dir blob ≈ one `svndiff` op (~80 bytes)
instead of the full N-entry listing.

Naïve delta-chains make reading the latest directory O(chain-depth),
and `rebuild_dir` reads its predecessor *every commit* → that would
trade disk-O(N²) for time-O(N²)+. Fixed by a **sidecar cache**:
`${repo}/.dircache/<sha>` files holding the *full uncompressed content*
of the **current generation** of directory blobs. The commit hot-path
reads the sidecar (O(1)); only cold / old-rev reads chain.

The blob's **identity is unchanged** — still `hash(full content)`.
Only the on-disk *encoding* changes. Content-addressing is preserved.

## 4. Encoding format — the `D` rep

Current rep envelope (`rep_encode_blob`): `<digit>\x01<tag><body>` —
`digit` ∈ {`0`,`1`} (use_zlib), `tag` ∈ {`R`,`Z`}. `rep_encoded_bytes`
= `slice(2,n)` = `<tag><body>`.

New tag **`D`**. A delta rep: `<digit>\x01` + `D` + `<base_sha>` +
`\x01` + `<svndiff bytes>`.
- `digit` byte: set `0` (use_zlib irrelevant for `D`).
- `base_sha`: the predecessor blob's hex hash. Hex never contains
  `\x01`, so `\x01` is an unambiguous separator.
- `<svndiff bytes>`: the raw output of `delta.xdelta_compute` (begins
  with the `SVN\x01` signature — `svndiff_decode_apply` checks it).

**Decode must dispatch on the tag byte (byte 2), not the `digit`.**
Audit `rep_read_decoded` / `rep_encoded_use_zlib` so `D` is handled
before the use_zlib branch.

## 5. Delta module API (already exists, `delta/module.ae` — no change)

- `xdelta_compute(source, source_len, target, target_len) -> (diff, diff_len)`
  — computes the svndiff transforming `source` → `target`.
- `svndiff_decode_apply(diff, diff_len, source, source_len) -> (target, target_len)`
  — applies `diff` to `source`. Returns `("",0)` on a bad signature.

Round-trip already covered by `delta/test_xdelta.ae` /
`delta/test_svndiff.ae`.

## 6. Write path

New `rep_write_blob_delta(repo, data, length, base_sha, base_data, base_len)`:
1. `sha = hash(data)` (same as `rep_write_blob` — content address).
2. dedup check (`rep_cache_has`) — unchanged.
3. `diff, diff_len = delta.xdelta_compute(base_data, base_len, data, length)`.
4. If `diff_len > 0` **and** `diff_len + len(base_sha) + 3` < the
   raw/zlib-encoded size → encode `D` (§4). Else fall back to plain
   `rep_encode_blob` (raw/zlib).
5. Write the `.rep` file; `rep_cache_insert(..., STORAGE_DELTA)`.

`rep_write_blob` (no base) is unchanged. Add `const STORAGE_DELTA = 3`
(next to `STORAGE_RAW=1`, `STORAGE_ZLIB=2`).

## 7. Read path

`rep_read_decoded` (the decoder behind `rep_read_blob`): when the `.rep`
tag is `D`:
1. parse `<base_sha>` (up to the `\x01`), then `<svndiff>`.
2. `base = rep_read_blob(repo, base_sha)` — **recursive**; chains if
   the base is itself `D`.
3. `content, _ = delta.svndiff_decode_apply(svndiff, svndiff_len, base, base_len)`.
4. base missing / `svndiff_decode_apply` returns empty → return `""`
   (graceful — never crash; mirrors the existing "" = missing/empty
   convention).

## 8. `rebuild_dir` + the sidecar cache

`${repo}/.dircache/<sha>` = a file holding the full uncompressed
content of directory blob `<sha>`. Holds only the **current
generation** of directories (bounded — O(current tree size)).

`rebuild_dir(repo, base_dir_sha, prefix, txn)`:
1. **Read base** — instead of `rep_read_blob(repo, base_dir_sha)`:
   try `${repo}/.dircache/<base_dir_sha>`; on hit use it (O(1)); on
   miss fall back to `rep_read_blob` (chains — cold/old, acceptable).
2. Build the new listing `rl` (unchanged logic).
3. **Write the new dir blob:** if `base_dir_sha` is non-empty and its
   content was obtained in step 1 → `rep_write_blob_delta(repo, rl,
   len, base_dir_sha, base_content, base_len)`. Else (root's first
   commit, empty dirs) → plain `rep_write_blob`.
4. Write `${repo}/.dircache/<new_sha>` = `rl` (atomic write).
5. Remove `${repo}/.dircache/<base_dir_sha>` — it is no longer the
   current generation. (Bounds `.dircache/` to the current tree.)

`rebuild_dir` recurses; every dir level does the above against its own
base. The recursion already carries `base_dir_sha` per level.

**Optional simplification:** instead of `rebuild_dir` consulting
`.dircache` directly, make `rep_read_blob` itself check
`${repo}/.dircache/<sha>` first (one `file_exists` stat per read).
Then `rebuild_dir` needs no read-side change — only steps 3–5. Pick
whichever is cleaner during implementation; the stat-per-read cost is
negligible vs the sqlite round-trip `rep_read_blob` already does.

## 9. Correctness / edge cases

- **Cold server** — `.dircache/` empty after restart; the first commit
  per directory pays one O(chain-depth) `rep_read_blob`, then
  re-populates. Acceptable.
- **`.dircache` is a *cache*, never the source of truth.** Keyed by
  content sha; written atomically from the same bytes that hashed to
  `sha`. A delete or miss only costs a chain-walk, never wrong data.
  Consider a debug-only assert `hash(.dircache content) == sha` on
  read.
- **Missing base** (`D` rep whose base `.rep` is gone) — `rep_read_blob`
  returns `""`. `filter_dir` / callers already treat `""` as
  missing/empty. No crash.
- **First version of a directory** — no base → plain raw/zlib.
- **Delta not smaller** — fall back to raw/zlib (step 6.4). Self-tuning.
- **Concurrency** — per-branch commits are serialized
  (`finalise_on_branch_`); `.dircache/<sha>` is content-keyed so
  cross-branch writes don't collide. A shared dir superseded on branch
  A then read on branch B just chains once. Fine for v1.

## 10. Why delta-chain, not skip-deltas

Skip-deltas (delta against a logarithmically-spaced ancestor) give
O(log N) reads *anywhere* without a cache — the proper fsfs scheme.
Rejected for v1: it needs per-directory version-history tracking to
pick the skip base, which avn's content-addressed store doesn't carry.
Delta-chain + the sidecar cache gets O(N) disk and O(1) commit-hot-path
for far less machinery; old-rev reads stay O(depth). If old-rev read
latency ever matters, skip-deltas are the follow-up round.

## 11. Test plan

- `repo_storage` regression test (new — extend `repo_storage/.tests-txn.ae`
  or add one): write blob B2 delta against B1, `rep_read_blob` → assert
  bytes equal B2. A 3-deep chain (B1 full, B2/B3 delta) read of B3.
  A `D` rep with a deleted base → `rep_read_blob` returns `""`.
- `.dircache` hit vs cold-miss both yield identical content.
- `aeb` full suite green (`branch_create`, commit/checkout/status,
  log — all exercise dir reads/writes).
- `bench/bench_avn.py` 5000 commits: repo disk **855 MB → ~530 MB**
  (target: parity with svn); per-commit latency **not worse** than the
  current ~94 ms mean.
- Re-run `bench/leak_probe.py` — `.dircache` files must not leak FDs;
  RSS unaffected.

## 12. Files

- `repo_storage/module.ae` — `STORAGE_DELTA`; `rep_encode_blob` /
  `rep_read_decoded` (the `D` branch); `rep_write_blob_delta`;
  `rebuild_dir` (steps 3–5, and step 1 if not doing the §8 optional
  `rep_read_blob` route); `.dircache` path helpers.
- `repo_storage/.tests-txn.ae` (or a new test driver) — §11 cases.
- `delta/module.ae` — used as-is, **no change**.

## 13. Risk

Core storage-format change — corruption-class blast radius. Mitigate:
land §11's round-trip + chain + missing-base tests *before* wiring
`rebuild_dir`; keep `.dircache` strictly a cache; verify the bench repo
is still fully readable end-to-end (`avn checkout` of HEAD + an old
rev) after the change. Old `R`/`Z`-only repos stay readable (decode
dispatches on tag).
