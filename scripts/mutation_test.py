"""Mutation-test the suite: prove the tests fail when the code is wrong.

A passing test proves nothing unless it fails when the behaviour it pins is
broken. This tool generates single-operator mutations of the source (swapped
comparisons, ``and``/``or`` swaps, dropped ``not``, flipped bool constants),
restricted to lines the suite actually executes, and reports every mutant that
NO test kills.

Subcommands:

``sweep``
    Mutate every executed site — optionally only lines changed since a git ref
    (``--since origin/main``) — and score the suite against them in parallel
    throwaway worktrees. ``--check`` fails on survivors that the recorded
    baseline (``scripts/mutation_baseline.json``) does not excuse.

``verify``
    Apply hand-written line-level mutations one at a time, serially, and
    require the named tests to fail. The spec is a JSON list of objects with
    keys ``file`` (basename under ``src/codex_dobby_mcp``), ``line`` (1-based),
    ``new`` (replacement source line), and ``label``. Serial and
    restore-after-each by design: a batch loop lets state leak between mutants,
    and a false "killed" is worse than useless.

Both subcommands score the EXACT current state of the working tree, including
uncommitted and untracked files, by committing a throwaway snapshot (without
touching the real index) and checking it out into temporary git worktrees. The
real checkout is never modified, and every worktree is removed on exit, even on
SIGTERM.

Exit codes: 0 clean; 1 surviving mutants the baseline does not excuse
(``sweep --check``) or unkilled spec mutations (``verify``); 2 the environment
or the unmutated baseline run is broken, so no mutation result would mean
anything.

Run through the project environment so the venv's pytest and coverage are used:

    uv sync --group mutation
    uv run python scripts/mutation_test.py sweep --since origin/main --check
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from threading import Lock

REPO = Path(__file__).resolve().parents[1]
PACKAGE_DIR = Path("src/codex_dobby_mcp")
EXCLUDED_FILES = {"__init__.py", "__main__.py", "_version.py"}
DEFAULT_BASELINE = REPO / "scripts" / "mutation_baseline.json"
SUITE_TIMEOUT_SECONDS = 600

CMP_SWAP: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class Site:
    """One mutable location, identified by its index in the shared visit order."""

    index: int
    lineno: int
    end_lineno: int
    op: str


@dataclass(frozen=True)
class Mutant:
    """A planned mutation plus the source-line text used for baseline matching."""

    file: str
    index: int
    line: int
    op: str
    text: str


class MutantVisitor(ast.NodeTransformer):
    """One traversal that both enumerates mutation sites and applies exactly one.

    Enumeration and application MUST share a single visit order. An earlier
    version used a separate collector that visited parents before children while
    the transformer visited children first, so index N named one site during
    collection and mutated a different one at run time. Keeping a single class,
    where record-only mode is simply ``target is None``, makes that divergence
    impossible.
    """

    def __init__(self, target: int | None = None) -> None:
        self.target = target
        self.sites: list[Site] = []

    def _site(self, node: ast.expr, op: str) -> bool:
        index = len(self.sites)
        end = node.end_lineno if node.end_lineno is not None else node.lineno
        self.sites.append(Site(index=index, lineno=node.lineno, end_lineno=end, op=op))
        return self.target == index

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        for i, op in enumerate(node.ops):
            swapped = CMP_SWAP.get(type(op))
            if swapped is not None and self._site(node, f"{type(op).__name__}->{swapped.__name__}"):
                node.ops[i] = swapped()
        return node

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        self.generic_visit(node)
        name = type(node.op).__name__
        if self._site(node, f"{name}->{'Or' if name == 'And' else 'And'}"):
            node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
        return node

    def visit_UnaryOp(self, node: ast.UnaryOp) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.op, ast.Not) and self._site(node, "drop not"):
            return node.operand
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if isinstance(node.value, bool) and self._site(node, f"{node.value}->{not node.value}"):
            return ast.Constant(value=not node.value)
        return node


def _git(*args: str, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        print(f"git {' '.join(args)} failed:\n{proc.stderr.strip()}", file=sys.stderr)
        raise SystemExit(2)
    return proc.stdout.strip()


def snapshot_tree(workdir: Path) -> str:
    """Commit the current working-tree state as a throwaway dangling commit.

    ``git worktree add`` can only check out a commit, and a checkout of HEAD is
    NOT the same as what is in the editor: three full sweeps were once silently
    graded against an old test suite because the new tests were uncommitted, and
    the error was only caught when a "corrected" run returned byte-identical
    numbers. So the harness scores the working tree itself — tracked
    modifications AND untracked files, captured through a temporary index so the
    user's real index is never touched — and says so out loud. The snapshot
    commit is deliberately left dangling; ``git gc`` collects it eventually.
    """
    head = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain")
    if not status:
        print(f"Scoring: clean checkout at HEAD {head[:12]}", flush=True)
        return head
    tmp_index = workdir / "snapshot-index"
    env = {**os.environ, "GIT_INDEX_FILE": str(tmp_index)}
    _git("read-tree", "HEAD", env=env)
    _git("add", "-A", env=env)
    tree = _git("write-tree", env=env)
    snapshot = _git(
        # -c identity so this works on CI runners with no git user configured.
        "-c", "user.name=mutation-test", "-c", "user.email=mutation-test@localhost",
        "commit-tree", tree, "-p", head, "-m", "mutation-testing snapshot of the working tree (never merge)",
    )
    changes = len(status.splitlines())
    print(
        f"Scoring: HEAD {head[:12]} plus {changes} uncommitted/untracked change(s), "
        f"snapshotted as {snapshot[:12]}",
        flush=True,
    )
    return snapshot


class WorktreePool:
    """Detached worktrees of the snapshot commit, each leased to one task at a time.

    Leasing goes through a queue rather than assigning ``wt[task_index % N]``:
    ``ThreadPoolExecutor`` hands tasks to whichever thread is free, so
    index-based assignment let two concurrent mutants share a tree — one task's
    restore overwrote the other's mutation mid-run and pytest scored pristine
    code as a surviving mutant (15 false survivors in one full sweep).
    """

    def __init__(self, workdir: Path, commit: str) -> None:
        self._workdir = workdir
        self._commit = commit
        self._free: queue.Queue[Path] = queue.Queue()
        self.paths: list[Path] = []

    def create(self, count: int) -> None:
        for i in range(count):
            wt = self._workdir / f"wt{i}"
            _git("worktree", "add", "--detach", "-q", str(wt), self._commit)
            self.paths.append(wt)
            self._free.put(wt)

    def lease(self) -> Path:
        return self._free.get()

    def release(self, wt: Path) -> None:
        self._free.put(wt)

    def destroy(self) -> None:
        """Unregister every worktree from the repo; must never leave state behind."""
        for wt in self.paths:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt)],
                cwd=REPO,
                capture_output=True,
            )
        subprocess.run(["git", "worktree", "prune"], cwd=REPO, capture_output=True)
        self.paths.clear()


def _test_env() -> dict[str, str]:
    """Environment for every pytest/coverage subprocess.

    ``PYTHONDONTWRITEBYTECODE=1`` is load-bearing, not hygiene: mutants are
    written, tested, and reverted in the same worktree, and an ``==``/``!=``
    style mutation produces a file with the SAME byte length. When that happens
    within the same mtime second, CPython's (mtime, size) pyc validation reuses
    the PREVIOUS source's bytecode, silently scoring mutants against the wrong
    code in both directions — proven concretely by a restored, correct source
    that still failed its tests.
    """
    return {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}


def _run_pytest(
    wt: Path, extra_args: list[str], timeout: int = SUITE_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    """Run pytest in a worktree with the guards every run needs.

    ``--basetemp`` is load-bearing under concurrency: pytest otherwise allocates
    ``/tmp/pytest-of-<user>/pytest-<N>`` by scanning for the highest N and
    PRUNES old numbered directories as it goes. With several mutants running at
    once, one worker deletes or collides with a directory another is actively
    using, so ``tmp_path`` files vanish mid-run — false survivors AND false
    kills. A fixed per-worktree basetemp disables the numbering and pruning
    entirely, and also keeps ``verify`` (single worktree) consistent with any
    other pytest run happening on the machine.
    """
    return subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "--basetemp", str(wt / ".pytest-basetemp"), *extra_args,
        ],
        cwd=wt,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_test_env(),
    )


def coverage_baseline(wt: Path) -> dict[str, set[int]]:
    """Run the UNMUTATED suite once under coverage; exit 2 if it fails.

    Two guards in one pass:

    - Clean baseline. A mutation "kills" a test only if the test passes WITHOUT
      it. Without this gate a simply-broken test scores as killing every mutant
      — the exact failure mode mutation testing exists to catch. Adding the gate
      immediately exposed one genuinely broken test.
    - Executed lines. Mutants are only generated on lines this same run
      executed, so results never depend on code the suite cannot reach (plain
      coverage already reports unexecuted lines; mutation testing adds nothing
      there).
    """
    probe = subprocess.run([sys.executable, "-c", "import coverage"], capture_output=True)
    if probe.returncode != 0:
        print("The 'coverage' package is missing. Run: uv sync --group mutation", file=sys.stderr)
        raise SystemExit(2)
    try:
        proc = subprocess.run(
            [
                sys.executable, "-m", "coverage", "run", "--source", str(PACKAGE_DIR),
                "-m", "pytest", "-q", "-p", "no:cacheprovider",
                "--basetemp", str(wt / ".pytest-basetemp"),
            ],
            cwd=wt,
            capture_output=True,
            text=True,
            timeout=SUITE_TIMEOUT_SECONDS,
            env=_test_env(),
        )
    except subprocess.TimeoutExpired:
        print("BASELINE TIMED OUT on unmutated source — results would be meaningless.", file=sys.stderr)
        raise SystemExit(2) from None
    if proc.returncode != 0:
        print("BASELINE FAILS on unmutated source — results would be meaningless:", file=sys.stderr)
        print("\n".join(proc.stdout.strip().splitlines()[-15:]), file=sys.stderr)
        raise SystemExit(2)
    summary = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    print(f"baseline clean: {summary}", flush=True)
    cov_json = wt / "coverage-executed.json"
    subprocess.run(
        [sys.executable, "-m", "coverage", "json", "-o", str(cov_json)],
        cwd=wt,
        capture_output=True,
        text=True,
        check=True,
        env=_test_env(),
    )
    data = json.loads(cov_json.read_text())
    return {Path(name).name: set(info["executed_lines"]) for name, info in data["files"].items()}


def changed_lines(since_ref: str, snapshot: str) -> dict[str, set[int]]:
    """Post-image lines added or modified since ``merge-base(since_ref, snapshot)``.

    "Changed lines" for mutation testing means: line numbers IN THE TREE BEING
    SCORED that a diff hunk added or rewrote. A mutation site qualifies when any
    line of its expression's ``[lineno, end_lineno]`` span is in that set, so
    editing the middle of a multi-line condition still mutates the whole
    condition. Deleted lines have no post-image and nothing to mutate.

    What this deliberately does NOT chase: behavioural drift in UNchanged lines
    caused by a changed one (an edited helper making a distant guard dead, a
    weakened fixture). That cannot be attributed to a diff without
    whole-program analysis; the scheduled full sweep exists to catch exactly
    that class over time. Three-dot (merge-base) semantics so commits that
    merely advanced the base branch are never counted as this change's lines.
    """
    diff = _git("diff", "--unified=0", f"{since_ref}...{snapshot}", "--", str(PACKAGE_DIR))
    out: dict[str, set[int]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            current = Path(target[2:]).name if target.startswith("b/") else None
        elif current is not None and (match := HUNK_RE.match(line)):
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            if count:
                out.setdefault(current, set()).update(range(start, start + count))
    return out


def build_plan(
    snapshot_wt: Path,
    executed: dict[str, set[int]],
    changed: dict[str, set[int]] | None,
) -> tuple[list[Mutant], dict[str, str]]:
    """Enumerate mutants on executed (and, if limited, changed) lines.

    Sources are read once from the snapshot worktree and cached; every later
    restore writes this cached pristine text rather than re-reading a file that
    a failed earlier restore could have left mutated.
    """
    plan: list[Mutant] = []
    sources: dict[str, str] = {}
    for path in sorted((snapshot_wt / PACKAGE_DIR).glob("*.py")):
        if path.name in EXCLUDED_FILES:
            continue
        source = path.read_text()
        sources[path.name] = source
        recorder = MutantVisitor(target=None)
        recorder.visit(ast.parse(source))
        live = executed.get(path.name, set())
        allowed = None if changed is None else changed.get(path.name, set())
        source_lines = source.splitlines()
        for site in recorder.sites:
            if site.lineno not in live:
                continue
            if allowed is not None and not any(
                lineno in allowed for lineno in range(site.lineno, site.end_lineno + 1)
            ):
                continue
            plan.append(
                Mutant(
                    file=path.name,
                    index=site.index,
                    line=site.lineno,
                    op=site.op,
                    text=source_lines[site.lineno - 1].strip(),
                )
            )
    return plan, sources


def _execute_mutant(wt: Path, mutant: Mutant, pristine: str) -> dict[str, object]:
    tree = ast.parse(pristine)
    mutated = MutantVisitor(target=mutant.index).visit(tree)
    ast.fix_missing_locations(mutated)
    code = ast.unparse(mutated)
    # A mutant that does not change the code would read as a survivor and
    # silently inflate the failure count. Compare against the unparsed pristine
    # form because unparse normalises formatting.
    if code == ast.unparse(ast.parse(pristine)):
        return {**asdict(mutant), "status": "noop", "detail": "mutation changed nothing"}
    target = wt / PACKAGE_DIR / mutant.file
    target.write_text(code)
    try:
        try:
            proc = _run_pytest(wt, ["-x"])
        except subprocess.TimeoutExpired:
            # A mutant that hangs the suite was caught by it: a timeout is a kill.
            return {**asdict(mutant), "status": "killed", "detail": "timeout (killed)"}
        if proc.returncode == 0:
            return {**asdict(mutant), "status": "survived", "detail": "SURVIVED"}
        detail = next(
            (
                line.strip()
                for line in proc.stdout.splitlines()
                if line.startswith("FAILED") or line.startswith("ERROR")
            ),
            "",
        )
        return {**asdict(mutant), "status": "killed", "detail": detail}
    finally:
        # Restore from the cached pristine text, never from the worktree file,
        # so one bad restore can never poison later mutants in this tree.
        target.write_text(pristine)


def run_mutant(pool: WorktreePool, mutant: Mutant, pristine: str) -> dict[str, object]:
    wt = pool.lease()
    try:
        result = _execute_mutant(wt, mutant, pristine)
        if result["status"] == "survived":
            # A kill is positive evidence: a test failed. A survivor is an
            # absence of evidence, and any flake in the run looks exactly like
            # one. Confirm every survivor with a second full run — survivors
            # are the minority, so this costs little.
            again = _execute_mutant(wt, mutant, pristine)
            if again["status"] != "survived":
                return {**again, "detail": f"flaky: survived once, then {again['status']}"}
        return result
    finally:
        pool.release(wt)


def _survivor_key(entry: dict[str, object]) -> tuple[object, object, object]:
    return (entry["file"], entry["op"], entry["text"])


def _check_baseline(
    baseline_path: Path, survived: list[dict[str, object]], killed: list[dict[str, object]]
) -> int:
    """Compare survivors to the recorded baseline; only NEW survivors fail.

    The matching key is (file, operator, stripped source-line text) as a
    multiset — NOT line numbers, which shift on every edit. Two accepted
    consequences: a line-for-line copy of an existing weak line borrows its
    excuse until the next full-sweep refresh, and editing a weak line changes
    its text, which correctly voids the excuse — a touched line must get a
    killing test or a known-equivalent entry (TODO.md) plus a baseline refresh.
    """
    try:
        data = json.loads(baseline_path.read_text())
        recorded_survivors = list(data["survivors"])
        recorded_stamp = f"recorded {data['recorded']} at {str(data['commit'])[:12]}"
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"Cannot read baseline {baseline_path}: {exc}", file=sys.stderr)
        return 2
    matchable = Counter(_survivor_key(s) for s in recorded_survivors)
    improvable = Counter(matchable)
    new: list[dict[str, object]] = []
    matched = 0
    for entry in survived:
        key = _survivor_key(entry)
        if matchable[key] > 0:
            matchable[key] -= 1
            matched += 1
        else:
            new.append(entry)
    improved = 0
    for entry in killed:
        key = _survivor_key(entry)
        if improvable[key] > 0:
            improvable[key] -= 1
            improved += 1
    print(f"\nBaseline check ({baseline_path.name}, {recorded_stamp}):", flush=True)
    print(f"  {matched} survivor(s) matched the recorded baseline", flush=True)
    if improved:
        print(
            f"  {improved} recorded survivor(s) were killed in this run — refresh the baseline "
            "after a full sweep",
            flush=True,
        )
    if new:
        print(f"  {len(new)} NEW survivor(s) not excused by the baseline:", flush=True)
        for entry in sorted(new, key=lambda e: (str(e["file"]), int(str(e["line"])))):
            print(f"    {entry['file']}:{entry['line']}  {str(entry['op']):<14} | {entry['text']}", flush=True)
        print(
            "  Kill each with a test — or, if provably equivalent, document the proof in TODO.md\n"
            "  ('Known-equivalent mutants') and refresh the baseline with a full sweep\n"
            "  (uv run python scripts/mutation_test.py sweep --update-baseline).",
            flush=True,
        )
        return 1
    print("  no new survivors", flush=True)
    return 0


def _write_baseline(baseline_path: Path, snapshot: str, results: list[dict[str, object]]) -> None:
    killed = [r for r in results if r["status"] == "killed"]
    survived = [r for r in results if r["status"] == "survived"]
    per_file: dict[str, dict[str, int]] = {}
    for entry in results:
        counts = per_file.setdefault(str(entry["file"]), {"killed": 0, "survived": 0})
        if entry["status"] in counts:
            counts[str(entry["status"])] += 1
    scored = len(killed) + len(survived)
    baseline = {
        "recorded": date.today().isoformat(),
        "commit": snapshot,
        "tool": "scripts/mutation_test.py",
        "note": (
            "Full-sweep baseline. Survivors listed here are expected and do not fail "
            "the CI mutation lane; the subset proven semantically unkillable is "
            "documented in TODO.md under 'Known-equivalent mutants'. Refresh with: "
            "uv run python scripts/mutation_test.py sweep --update-baseline"
        ),
        "mutants": len(results),
        "killed": len(killed),
        "survived": len(survived),
        "score_percent": round(100 * len(killed) / scored, 1) if scored else 100.0,
        "per_file": {name: per_file[name] for name in sorted(per_file)},
        "survivors": [
            {"file": r["file"], "line": r["line"], "op": r["op"], "text": r["text"]}
            for r in sorted(survived, key=lambda r: (str(r["file"]), int(str(r["line"])), str(r["op"])))
        ],
    }
    baseline_path.write_text(json.dumps(baseline, indent=1) + "\n")


def cmd_sweep(args: argparse.Namespace) -> int:
    if args.update_baseline and args.since:
        # Bug-class guard: a partial run silently recorded as the full-sweep
        # baseline would claim coverage it does not have, exactly the
        # wrong-tree-state failure this tool is loud about everywhere else.
        print("--update-baseline requires a full sweep; refusing to record a --since run.", file=sys.stderr)
        return 2
    owns_workdir = args.workdir is None
    workdir = Path(tempfile.mkdtemp(prefix="dobby-mutation-")) if owns_workdir else args.workdir
    workdir.mkdir(parents=True, exist_ok=True)
    snapshot = snapshot_tree(workdir)
    pool = WorktreePool(workdir=workdir, commit=snapshot)
    started = time.time()
    try:
        pool.create(args.workers)
        print(f"{args.workers} worktree(s) ready under {workdir}", flush=True)
        executed = coverage_baseline(pool.paths[0])
        changed = changed_lines(args.since, snapshot) if args.since else None
        if changed is not None:
            total = sum(len(lines) for lines in changed.values())
            print(f"--since {args.since}: {total} changed line(s) in {len(changed)} source file(s)", flush=True)
        plan, sources = build_plan(pool.paths[0], executed, changed)
        scope = "changed, executed lines" if changed is not None else "executed lines"
        print(f"{len(plan)} mutant(s) on {scope}", flush=True)
        for name, count in sorted(Counter(m.file for m in plan).items()):
            print(f"  {name:24} {count}", flush=True)
        results: list[dict[str, object]] = []
        if plan:
            progress_lock = Lock()
            total_planned = len(plan)

            def task(mutant: Mutant) -> None:
                result = run_mutant(pool, mutant, sources[mutant.file])
                with progress_lock:
                    results.append(result)
                    done = len(results)
                    if result["status"] == "survived":
                        print(
                            f"[{done}/{total_planned}] SURVIVED  "
                            f"{result['file']}:{result['line']}  {result['op']}",
                            flush=True,
                        )
                    elif done % 25 == 0 or done == total_planned:
                        survivors = sum(1 for r in results if r["status"] == "survived")
                        print(
                            f"[{done}/{total_planned}] {time.time() - started:.0f}s elapsed, "
                            f"{survivors} survivor(s) so far",
                            flush=True,
                        )

            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                for _ in executor.map(task, plan):
                    pass
        survived = [r for r in results if r["status"] == "survived"]
        killed = [r for r in results if r["status"] == "killed"]
        noop = [r for r in results if r["status"] == "noop"]
        scored = len(killed) + len(survived)
        score = 100 * len(killed) / scored if scored else 100.0
        print(
            f"\n=== {len(killed)} killed, {len(survived)} survived "
            f"({score:.1f}% mutation score) in {time.time() - started:.0f}s ===",
            flush=True,
        )
        if noop:
            print(f"({len(noop)} no-op mutant(s) excluded from the score)", flush=True)
        by_file: dict[str, list[dict[str, object]]] = {}
        for entry in survived:
            by_file.setdefault(str(entry["file"]), []).append(entry)
        for name, rows in sorted(by_file.items(), key=lambda kv: -len(kv[1])):
            print(f"\n--- {name}: {len(rows)} survivor(s) ---", flush=True)
            for entry in sorted(rows, key=lambda e: int(str(e["line"]))):
                print(f"  line {entry['line']:<5} {str(entry['op']):<14} | {entry['text']}", flush=True)
        if args.json:
            args.json.write_text(json.dumps(results, indent=1) + "\n")
            print(f"\nfull results written to {args.json}", flush=True)
        if args.update_baseline:
            _write_baseline(args.baseline, snapshot, results)
            print(f"baseline refreshed at {args.baseline}", flush=True)
        if args.check:
            return _check_baseline(args.baseline, survived, killed)
        return 0
    finally:
        pool.destroy()
        if owns_workdir:
            shutil.rmtree(workdir, ignore_errors=True)


def _spec_targeting_errors(mutations: list[dict], backups: dict[str, str]) -> list[str]:
    """Reject a spec whose line numbers do not point where it claims.

    ``verify`` replaces a numbered line outright, so an off-by-two spec silently
    rewrites a comment, an import, or a line in a different function. That can
    still make the suite fail, which prints KILLED for a mutation that was never
    applied — a false kill, the worst possible output from a tool whose entire
    purpose is proving tests discriminate. (Observed: a spec aimed at line 160
    instead of 158 overwrote a comment with ``DEFAULT_DANGER = True`` and
    reported a kill that the intended mutation had nothing to do with.)

    Two checks, both cheap and both fatal:

    * ``old``, when the spec supplies it, must match the targeted line's
      stripped text. This is the direct assertion and specs should carry it.
    * The targeted line must not be blank or a comment. Every operator this
      tool models rewrites an expression, so a comment target is always a
      mis-aimed spec, even when the author omitted ``old``.
    """
    errors: list[str] = []
    for mutation in mutations:
        label = mutation.get("label", "<unlabelled>")
        source = backups[mutation["file"]].splitlines()
        index = mutation["line"] - 1
        if not 0 <= index < len(source):
            errors.append(
                f"{label}: {mutation['file']}:{mutation['line']} is outside the file "
                f"(1-{len(source)})"
            )
            continue
        actual = source[index].strip()
        expected = mutation.get("old")
        if expected is not None and actual != expected.strip():
            errors.append(
                f"{label}: {mutation['file']}:{mutation['line']} is {actual!r}, "
                f"but the spec expected {expected.strip()!r}"
            )
        elif not actual or actual.startswith("#"):
            errors.append(
                f"{label}: {mutation['file']}:{mutation['line']} is a comment or blank "
                f"line ({actual!r}); mutation operators only rewrite expressions, so "
                f"this line number is wrong"
            )
    return errors


def cmd_verify(args: argparse.Namespace) -> int:
    mutations = json.loads(Path(args.spec).read_text())
    pytest_args = list(args.pytest_args)
    if pytest_args and pytest_args[0] == "--":
        pytest_args = pytest_args[1:]
    owns_workdir = args.workdir is None
    workdir = Path(tempfile.mkdtemp(prefix="dobby-mutation-")) if owns_workdir else args.workdir
    workdir.mkdir(parents=True, exist_ok=True)
    snapshot = snapshot_tree(workdir)
    pool = WorktreePool(workdir=workdir, commit=snapshot)
    try:
        pool.create(1)
        wt = pool.paths[0]
        backups: dict[str, str] = {}
        for mutation in mutations:
            name = mutation["file"]
            if name not in backups:
                backups[name] = (wt / PACKAGE_DIR / name).read_text()
        # Checked before the baseline runs, because a spec that points at the
        # wrong line does not test what it claims: a mutation landing on an
        # unrelated line can still break the suite and print KILLED for a
        # mutation that was never applied. A false kill is the exact outcome
        # this tool exists to prevent, so a mis-aimed spec must never produce a
        # green result — and there is no reason to spend a full suite run
        # discovering that.
        spec_errors = _spec_targeting_errors(mutations, backups)
        if spec_errors:
            print("SPEC DOES NOT MATCH THE SOURCE — results would be meaningless:", file=sys.stderr)
            for error in spec_errors:
                print(f"  {error}", file=sys.stderr)
            return 2
        # A mutation "kills" a test only if the test passes WITHOUT it. Without
        # this gate a simply-broken test scores as a kill for every mutation —
        # the exact failure mode this whole exercise exists to catch.
        try:
            baseline = _run_pytest(wt, pytest_args)
        except subprocess.TimeoutExpired:
            print("BASELINE TIMED OUT on unmutated source — results would be meaningless.", file=sys.stderr)
            return 2
        if baseline.returncode != 0:
            print("BASELINE FAILS on unmutated source — results would be meaningless:", file=sys.stderr)
            print("\n".join(baseline.stdout.strip().splitlines()[-6:]), file=sys.stderr)
            return 2
        print(f"baseline clean: {baseline.stdout.strip().splitlines()[-1]}\n", flush=True)
        failures: list[str] = []
        applied = 0
        for mutation in mutations:
            lines = backups[mutation["file"]].splitlines(keepends=True)
            i = mutation["line"] - 1
            indent = len(lines[i]) - len(lines[i].lstrip())
            if lines[i].strip() == mutation["new"].strip():
                # A replacement identical to the original would read as a
                # survivor of a mutation that never happened.
                print(f"  SKIP (no-op)  {mutation['label']}", flush=True)
                continue
            lines[i] = " " * indent + mutation["new"].strip() + "\n"
            target = wt / PACKAGE_DIR / mutation["file"]
            target.write_text("".join(lines))
            try:
                try:
                    proc = _run_pytest(wt, pytest_args)
                    was_killed = proc.returncode != 0
                    tail = [line for line in proc.stdout.strip().splitlines() if line][-1:]
                    summary = tail[0] if tail else ""
                except subprocess.TimeoutExpired:
                    was_killed, summary = True, "timeout (killed)"
            finally:
                target.write_text(backups[mutation["file"]])
            applied += 1
            status = "KILLED " if was_killed else "SURVIVED"
            if not was_killed:
                failures.append(mutation["label"])
            print(f"  {status}  {mutation['label']:<52} {summary}", flush=True)
        print(flush=True)
        if failures:
            print(f"{len(failures)} mutation(s) NOT killed:", flush=True)
            for label in failures:
                print(f"   - {label}", flush=True)
            return 1
        print(f"all {applied} applied mutation(s) killed", flush=True)
        return 0
    finally:
        pool.destroy()
        if owns_workdir:
            shutil.rmtree(workdir, ignore_errors=True)


def _handle_sigterm(_signum: int, _frame: object) -> None:
    # Convert SIGTERM (e.g. a cancelled CI job) into SystemExit so the finally
    # blocks unregister every worktree instead of leaving them in the repo.
    raise SystemExit(143)


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGTERM, _handle_sigterm)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sweep = sub.add_parser("sweep", help="mutate executed (optionally changed) lines and score the suite")
    sweep.add_argument("--since", metavar="REF", help="mutate only lines changed since merge-base(REF, working tree)")
    sweep.add_argument("--workers", type=int, default=max(2, min(8, os.cpu_count() or 2)))
    sweep.add_argument("--check", action="store_true", help="exit 1 on survivors the baseline does not excuse")
    sweep.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    sweep.add_argument("--update-baseline", action="store_true", help="record this full sweep as the new baseline")
    sweep.add_argument("--json", type=Path, metavar="PATH", help="write full per-mutant results as JSON")
    sweep.add_argument("--workdir", type=Path, help="directory for throwaway worktrees (default: fresh temp dir)")
    sweep.set_defaults(func=cmd_sweep)

    verify = sub.add_parser("verify", help="apply spec mutations one at a time; the named tests must fail")
    verify.add_argument("--workdir", type=Path, help="directory for the throwaway worktree (default: fresh temp dir)")
    verify.add_argument("spec", help="JSON list of {file, line, new, label} mutations")
    verify.add_argument("pytest_args", nargs=argparse.REMAINDER, help="arguments passed to pytest")
    verify.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
