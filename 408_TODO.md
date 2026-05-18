# 408_TODO — avn opportunities from aether PR #408

PR #408 (`feat/issues-384-391-392-396-397`, merged as commit
`a44ed78a` in the aether repo, ~23 commits authored by `nicolasmd87`,
shipped across aetherc 0.134.0–0.135.0) introduced eight features.
Three of them map directly onto known avn pain points; two are
nice-to-haves; three aren't a fit. This file is the working list.

Order below is the recommended rollout. Each item carries a target
round, the avn files touched, the from→to shape, and acceptance
criteria.

---

## [ ] 1. Migrate handler return codes to structured `(value, kind, message)` returns *(highest-value)*

**Aether feature:** [#392 structured-error pilot](../aether/CHANGELOG.md)
shipped in 0.135.0. Common shape: `(value, int, string)` where the
int is one of `KIND_OK` / `KIND_PARSE_ERROR` / `KIND_INVALID_INPUT` /
`KIND_FORBIDDEN` / `KIND_OUT_OF_DATE` / `KIND_OUT_OF_MEMORY` /
`KIND_NOT_FOUND` / etc. Kinds overlap deliberately with `std.fs` and
`std.json` so callers can switch on either surface.

**Today's avn:** signed-int return codes -1..-13 from
`avnserver/commit_parse.ae:99-450`'s `commit_from_body`. The HTTP
handler at `avnserver/handler_commit.ae:74-133` has 13 separate
`if rv == -N → svnserver_respond_error(...)` branches. Same anti-
pattern in:

- `avnserver/handler_path_put.ae`
- `avnserver/handler_path_delete.ae`
- `avnserver/handler_branch_create.ae`
- `avnserver/handler_copy.ae`
- `repo_storage/commit_finalise.ae:353-358` (`commit_finalise -> int`,
  `-1` / `-12` overloaded)
- `repo_storage/commit_finalise.ae:372-412` (`branch_create -> int`,
  `-1` for "name empty" / "base empty" / "globs empty" / "name exists"
  / "base missing" — five distinct conditions all collapsed into one
  return value)

**Migration:**

```aether
// before
export commit_from_body(req: ptr, res: ptr, repo: string) -> int { ... }

// after
export commit_from_body(req: ptr, res: ptr, repo: string) -> (int, int, string) { ... }
//                                                            ^    ^    ^
//                                                            |    |    error message (empty when KIND_OK)
//                                                            |    KIND_*
//                                                            new_rev or -1
```

`avnserver/handler_commit.ae` collapses to one switch over kind plus
one default `svnserver_respond_error(res, status_for_kind(kind), msg)`.

**Acceptance:**

- [ ] Every `*_parse.ae` and `repo_storage/commit_finalise.ae` export
  returning `int`-with-negative-error-codes migrated to `(int, int,
  string)`.
- [ ] Every `handler_*.ae` switches on `KIND_*` instead of literal
  `-N`.
- [ ] `tests/integration/` (the test-rest-put driver, server tests)
  green.
- [ ] No new client breakage — the wire shape (HTTP status code) for
  each kind matches what the existing -N→status mapping produces.
- [ ] `avn commit` CLI no longer string-matches "out of date" /
  "parent_sha mismatch" — switches on response shape instead.

**Estimated round:** R288 or R289. Big diff (~300-500 lines across
~15 files), high client-visible win.

**Cross-ref:**
[`docs/stdlib-module-pattern.md` in aether](../aether/docs/stdlib-module-pattern.md)
documents the canonical `(value, kind, message)` shape — copy the
constants list as-is.

---

## [ ] 2. Adopt `requires` / `ensures` contracts on invariant-heavy paths *(low cost, big readability/safety)*

**Aether feature:** [#348 Eiffel-style runtime contracts](../aether/CHANGELOG.md)
shipped in 0.135.0. Const-fold elision for trivially-true predicates
(`requires true`, `ensures 1 > 0`, etc.) makes documentation-only
clauses zero-cost. `--no-contracts` aetherc flag for prod builds.

**Today's avn:** invariants documented as comments. E.g.,
`commit_finalise.ae:354-355`'s "Returns the new rev number on
success, -1 on failure" or `rebuild_dir`'s "Returns the new dir
blob's sha (empty string if nothing survived at all)".

**Concrete sites worth adding clauses to** (priority within the
item):

- [ ] `commit_finalise.ae:256` `finalise_on_branch_`:
  `requires base_rev >= 0` and `ensures result == -1 || result == -12 || result > 0`
- [ ] `commit_finalise.ae:353` `commit_finalise` (export, public-API):
  same shape as above; couple this with item 1's migration so the
  ensure clause references KIND values once they're available
- [ ] `repo_storage/rebuild.ae:268` `rebuild_dir`:
  `ensures string.length(result) == 40 || string.length(result) == 0`
- [ ] `commit_finalise.ae:96` `load_rev_root_sha1_`:
  `requires rev >= 0; ensures string.length(result) == 0 || string.length(result) == 40`
- [ ] `commit_parse.ae:75` `txn_add_b64_`:
  `ensures result == 0 || result == -1`

**Acceptance:**

- [ ] Five sites above carry `requires`/`ensures` clauses.
- [ ] All clauses that fire in steady state (i.e. always-true) are
  classified as const-fold elisions in the generated C — verify by
  greping `*_generated.c` for `precondition elided` / `postcondition
  elided` comments alongside the function.
- [ ] Default `make` / `aeb` build still ships contract checks (catch
  regressions during the port); a `--no-contracts` flag is exercised
  in a prod-mode build target.

**Estimated round:** R290 or alongside R288 (item 1 above) since both
touch the same export signatures. Small diff (~50 lines), high
documentation/safety value.

**Cross-ref:**
[`docs/language-reference.md` in aether](../aether/docs/language-reference.md)
section on contracts; `examples/basics/contracts.ae` for syntax.

---

## [ ] 3. `std.json.parse_strict` for richer parse-error diagnostics *(small mechanical)*

**Aether feature:** [#392 std.json structured-error pilot](../aether/CHANGELOG.md)
shipped in 0.135.0. New `parse_strict(json_str) -> (ptr, int, string)`
plus `last_error_line` / `last_error_col` accessors.

**Today's avn:** `avnserver/commit_parse.ae:104`:

```aether
root, err = json.parse(body)
if string.length(err) > 0 { return -2 }
```

The client gets HTTP 400 "malformed JSON" — no location, no
diagnostic. For a 100 KB body that's frustrating.

**Migration (depends on item 1 landing first to avoid double rework):**

```aether
root, kind, msg = json.parse_strict(body)
if kind != json.KIND_OK {
    line = json.last_error_line()
    col  = json.last_error_col()
    return -1, KIND_PARSE_ERROR, "${msg} at line ${line}, col ${col}"
}
```

**Acceptance:**

- [ ] Single migration site in `commit_parse.ae:104`.
- [ ] Client-visible error string carries line + col on malformed JSON.
- [ ] No regression in the happy path (parse_strict is opt-in and
  additive per #392's contract).

**Estimated round:** R291 (after item 1 lands). Tiny diff (~5 lines).

**Cross-ref:**
[`docs/stdlib-reference.md` in aether](../aether/docs/stdlib-reference.md)
section on std.json.

---

## [ ] 4. Enable `HARDEN=1` on CI *(low cost, catches latent C-side bugs)*

**Aether feature:** [#396 opt-in build hardening](../aether/CHANGELOG.md)
shipped in 0.135.0. `HARDEN=1 make ...` → `-fstack-protector-all`
+ `-D_FORTIFY_SOURCE=2` + `-Wformat -Wformat-security`. ~3-5% runtime
overhead, off by default in release builds.

**Today's avn:** all-default `make` / `aeb` builds. C-side shims
(b64 decoder, sha hasher, txn machinery, repos shim) and any future
Aether-runtime allocator changes are not exercised under the
hardened build — a regression that introduces an unchecked memcpy
over a fixed buffer wouldn't trip a red check.

**Migration:**

- [ ] Add a CI matrix entry that runs `HARDEN=1 aeb avnserver
  avnadmin avn && HARDEN=1 aeb` (run the full test suite under the
  hardened flags).
- [ ] Document in avn's CONTRIBUTING (if/when one exists) that PRs
  touching C in `repo_storage/`, `avnserver/`, `repos/`, etc. should
  pass under HARDEN=1 locally.

**Acceptance:**

- [ ] CI green under HARDEN=1 on Linux/gcc.
- [ ] Doc note on the policy.

**Estimated round:** R292 or any spare cycle. Probably ~30 minutes
of CI YAML + a doc paragraph.

**Cross-ref:**
[`docs/build-system.md` in aether](../aether/docs/build-system.md).

---

## [ ] 5. Park: `fs.copy` / `fs.move` / `fs.realpath` / `fs.chmod` for future branch-creation work

**Aether feature:** [#391 std.fs completeness bundle](../aether/CHANGELOG.md)
shipped in 0.135.0. Per-platform best-primitive: Linux
`copy_file_range(2)` (reflinks on btrfs/XFS) → `sendfile(2)` → 8 MiB
read/write fallback; macOS `fcopyfile(COPYFILE_DATA)` (APFS clone on
same-volume); Windows `CopyFileExW`. Returns the structured
`(bytes, kind, message)` triple from #392.

**Today's avn:** file ops in `repo_storage/rep_store.ae` and
`util/io.ae` are atomic-writes + read-binary. None of the new
primitives replace existing avn code 1:1.

**When this becomes useful:** `repo_storage/commit_finalise.ae:372`
`branch_create` currently uses `filter_dir` + `rep_write_blob`
(builds a filtered subtree blob in-process). If branch creation ever
grows to a "snapshot the whole tree" operation, `fs.copy` with
reflinks would do it in O(log) time instead of O(content). Same for
`avnadmin dump` / `dump_load` — a reflink would replace the
serialise-then-deserialise round trip.

**Acceptance:** none — this is a "remember it exists" item. Revisit
when `branch_create` or `avnadmin` grows new operations.

---

## Not a fit (recorded so we don't relitigate)

- **`std.http.script_gateway`** (#384). avnserver *is* the HTTP
  server; it doesn't host other scripts as plugins. Could become
  relevant if avn ever supports per-repo Aether-scripted hooks
  (post-commit, pre-branch-create), but that's a feature decision,
  not an aether-side gift to take.

- **`--emit=both` / `ae lib-info` / `aether_lib_meta()`** (#403).
  Embed-as-library isn't on avn's roadmap. Could matter if avn ever
  wants to be linked into other tools (e.g. an IDE extension calling
  into the avn server in-process). Park.

- **Kind-tagged `AetherValue*` + safe deep-free** (the v1-out-of-scope
  closer in 0.135.0). Only relevant if avn exposes an embedded-host
  C ABI to consumers. It doesn't.

- **`ci-riscv64`** (#397). Wait until someone actually wants to deploy
  avn on RISC-V. Architectural-portability bug surfacing is a real
  win for aether but doesn't change avn's deployment story.

---

## Coupling and ordering

- Items 1 and 2 should land together if possible — both touch every
  handler / commit-finalise export signature, and the contract
  clauses can reference KIND values added by item 1.
- Item 3 depends on item 1 (the call-site already returns a tuple
  by then).
- Item 4 is independent and parallelisable.
- Item 5 requires no avn-side work right now.

Suggested round assignments:

| round | content |
|---:|---|
| R288 | Item 1 (structured-error returns) + Item 2 (contracts on the same exports) |
| R291 | Item 3 (parse_strict) |
| R292 | Item 4 (HARDEN=1 CI) |
| later | Item 5 (when branch_create or avnadmin grows) |

Round numbers are aspirational — actual rollout depends on whatever
else is queued. Items 1+2 together are the meaningful migration; the
rest are polish.
