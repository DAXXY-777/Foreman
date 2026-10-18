"""One git worktree per worker, cut from a neutral single-commit copy of the seeded repo.

    wt = WorkTrees(root="work")                       # everything lives under ./work
    wt.make_base(seeded_tree_path)                    # neutral copy: one commit, no history, no hints
    wt.create("w1"); wt.create("w2")                  # each worker gets its own folder and its own branch
    wt.write("w1", "tinydb/utils.py", "...")          # workers can only touch files inside their own folder
    wt.run_tests("w1", ["tests/test_utils.py"])       # runs pytest inside that folder only
    wt.diff("w1")                                     # the worker's patch (this is the patch_ref)
    wt.reset("w1")                                    # back to the starting state between runs

Why a neutral copy: the seeded repo's git history has commit messages naming each planted bug.
Workers must never see that, and they never see the answer key (it is not in these folders at all).
"""
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

SKIP_COPY = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".coverage", "uv.lock", ".venv", "build", "dist"}
GIT_ENV = {"GIT_AUTHOR_NAME": "import", "GIT_AUTHOR_EMAIL": "import@example.com",
           "GIT_COMMITTER_NAME": "import", "GIT_COMMITTER_EMAIL": "import@example.com",
           "GIT_AUTHOR_DATE": "2026-01-01T00:00:00", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00"}


class WorkTreeError(Exception):
    pass


def git(cwd, *args, check=True):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    if check and p.returncode != 0:
        raise WorkTreeError(f"git {' '.join(args)} failed: {p.stderr.strip()[:300]}")
    return p.stdout


_NUM = re.compile(r"^\s*\d+[|:]\s?")


def _forgiving_match(text, old, new):
    """Try to find `old` in `text` despite slips in leading whitespace or copied line numbers. Returns the new text, or
    None. Only succeeds when exactly one place matches."""
    old_lines, new_lines = old.split("\n"), new.split("\n")
    if old_lines and all(_NUM.match(l) for l in old_lines if l.strip()):          # line numbers were copied too
        old_lines = [_NUM.sub("", l) for l in old_lines]
        if all(_NUM.match(l) for l in new_lines if l.strip()):
            new_lines = [_NUM.sub("", l) for l in new_lines]
    while old_lines and not old_lines[-1].strip():
        old_lines.pop()
    if not old_lines or not any(l.strip() for l in old_lines):
        return None
    lines = text.split("\n")
    want = [l.strip() for l in old_lines]
    hits = [i for i in range(len(lines) - len(want) + 1) if [x.strip() for x in lines[i:i + len(want)]] == want]
    if len(hits) != 1:
        return None
    i = hits[0]
    file_indent = len(lines[i]) - len(lines[i].lstrip())
    old_indent = len(old_lines[0]) - len(old_lines[0].lstrip())
    delta = file_indent - old_indent
    out = []
    for l in new_lines:
        if not l.strip():
            out.append(l)
        elif delta >= 0:
            out.append(" " * delta + l)
        else:
            out.append(l[min(-delta, len(l) - len(l.lstrip())):])
    lines[i:i + len(want)] = out
    return "\n".join(lines)


class WorkTrees:
    def __init__(self, root="work"):
        self.root = os.path.abspath(root)
        self.base = os.path.join(self.root, "base")
        self.trees = os.path.join(self.root, "trees")

    # ---- setup ----------------------------------------------------------------------------------------
    def make_base(self, source_tree, force=False):
        """Copy the source tree (no .git) into a fresh repo with ONE commit."""
        if os.path.exists(self.root):
            if not force:
                raise WorkTreeError(f"{self.root} already exists; pass force=True to rebuild it")
            self.cleanup()
        os.makedirs(self.trees)
        shutil.copytree(source_tree, self.base, ignore=lambda d, names: [n for n in names if n in SKIP_COPY])
        git(self.base, "init", "-q", "-b", "main")
        git(self.base, "add", "-A")
        git(self.base, "commit", "-q", "-m", "Initial import")
        return self.base

    def create(self, name):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise WorkTreeError("worktree names are letters, digits, - and _ only")
        path = self.path(name)
        if os.path.exists(path):
            raise WorkTreeError(f"worktree {name} already exists")
        git(self.base, "worktree", "add", "-q", "-b", f"work/{name}", path, "main")
        return path

    def path(self, name):
        return os.path.join(self.trees, name)

    def names(self):
        return sorted(os.listdir(self.trees)) if os.path.isdir(self.trees) else []

    def reset(self, name):
        """Throw away everything the worker changed."""
        git(self.path(name), "reset", "-q", "--hard", "main")
        git(self.path(name), "clean", "-fdq")

    def cleanup(self):
        if os.path.isdir(self.base):
            git(self.base, "worktree", "prune", check=False)
        shutil.rmtree(self.root, ignore_errors=True)

    # ---- the worker's safe file access ------------------------------------------------------------------
    def _safe(self, name, rel):
        top = os.path.realpath(self.path(name))
        full = os.path.realpath(os.path.join(top, rel))
        if full != top and not full.startswith(top + os.sep):
            raise WorkTreeError("that path is outside your worktree")
        parts = os.path.relpath(full, top).split(os.sep)
        if ".git" in parts:
            raise WorkTreeError("the .git folder is not available")
        return full

    def read(self, name, rel, start=1, end=None, max_chars=12000):
        full = self._safe(name, rel)
        if not os.path.isfile(full):
            raise WorkTreeError(f"no such file: {rel}")
        lines = open(full, encoding="utf-8", errors="replace").read().splitlines()
        end = end or len(lines)
        out = "\n".join(f"{i}|{l}" for i, l in enumerate(lines[start - 1:end], start))   # text after the | is the exact line
        return out[:max_chars] + ("\n... (cut off, ask for a smaller range)" if len(out) > max_chars else "")

    def write(self, name, rel, content):
        full = self._safe(name, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return f"wrote {rel} ({len(content)} characters)"

    def replace(self, name, rel, old, new):
        """Swap one piece of text for another. `old` must match exactly one place. Models often copy lines with the
        wrong indent or with the line numbers still attached, so a few harmless slips are forgiven."""
        full = self._safe(name, rel)
        if not os.path.isfile(full):
            raise WorkTreeError(f"no such file: {rel}")
        text = open(full, encoding="utf-8").read()
        n = text.count(old)
        if n > 1:
            raise WorkTreeError(f"the text to replace appears {n} times in {rel}; add a line or two around it so it is unique")
        if n == 1:
            open(full, "w", encoding="utf-8").write(text.replace(old, new))
            return f"changed {rel}"
        fixed = _forgiving_match(text, old, new)
        if fixed is None:
            raise WorkTreeError(f"the text to replace was not found in {rel}. Copy it exactly from read_file (keep the "
                                f"indentation, leave out the line numbers and the | after them), or copy fewer lines")
        open(full, "w", encoding="utf-8").write(fixed)
        return f"changed {rel}"

    def list_dir(self, name, rel="."):
        full = self._safe(name, rel)
        if not os.path.isdir(full):
            raise WorkTreeError(f"not a folder: {rel}")
        items = sorted(e for e in os.listdir(full) if e not in SKIP_COPY and e != ".git")
        return "\n".join(e + ("/" if os.path.isdir(os.path.join(full, e)) else "") for e in items)

    def search(self, name, text, glob="*.py", max_hits=40):
        top = self._safe(name, ".")
        hits = []
        for d, dirs, files in os.walk(top):
            dirs[:] = [x for x in dirs if x not in SKIP_COPY and x != ".git"]
            for f in sorted(files):
                p = os.path.join(d, f)
                if fnmatch.fnmatch(f, glob) or fnmatch.fnmatch(os.path.relpath(p, top), glob):
                    for i, line in enumerate(open(p, encoding="utf-8", errors="replace"), 1):
                        if text in line:
                            hits.append(f"{os.path.relpath(p, top)}:{i}: {line.strip()[:120]}")
                            if len(hits) >= max_hits:
                                return "\n".join(hits) + "\n... (more hits, search for something narrower)"
        return "\n".join(hits) or "no matches"

    # ---- tests and patches -----------------------------------------------------------------------------
    def run_tests(self, name, targets=None, timeout=120):
        """Run pytest inside the worktree. Returns {passed, failed, results: {test_id: 'passed'|'failed'|...},
        failures: {test_id: short reason}, timed_out}."""
        cwd = self.path(name)
        tmp = tempfile.mkdtemp()
        junit = os.path.join(tmp, "junit.xml")
        cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-o", "addopts=", "-q", "--tb=line",
               f"--junitxml={junit}", *(targets or [])]
        timed_out = False
        try:
            subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "PYTHONPATH": cwd, "PYTHONDONTWRITEBYTECODE": "1"})
        except subprocess.TimeoutExpired:
            timed_out = True
        results, failures = {}, {}
        if os.path.exists(junit):
            for tc in ET.parse(junit).getroot().iter("testcase"):
                tid = tc.get("classname", "").replace(".", "/") + ".py::" + tc.get("name")
                tid = re.sub(r"^(tests/\w+)\.py::", r"\1.py::", tid)
                bad = tc.find("failure") if tc.find("failure") is not None else tc.find("error")
                if bad is not None:
                    results[tid] = "failed"
                    failures[tid] = (bad.get("message") or bad.text or "")[:200]
                elif tc.find("skipped") is not None:
                    results[tid] = "skipped"
                else:
                    results[tid] = "passed"
        shutil.rmtree(tmp, ignore_errors=True)
        return {"passed": sum(v == "passed" for v in results.values()),
                "failed": sum(v == "failed" for v in results.values()),
                "results": results, "failures": failures, "timed_out": timed_out}

    def changed_files(self, name):
        git(self.path(name), "add", "-A")
        out = git(self.path(name), "diff", "--cached", "--name-only", "main")
        return [l for l in out.splitlines() if l]

    def revert_paths(self, name, paths):
        """Put the given files back to the starting state (new files are deleted)."""
        top = self.path(name)
        for p in paths:
            tracked = subprocess.run(["git", "cat-file", "-e", f"main:{p}"], cwd=top, capture_output=True).returncode == 0
            if tracked:
                git(top, "checkout", "-q", "main", "--", p)
            else:
                git(top, "rm", "-f", "-q", "--cached", "--ignore-unmatch", "--", p, check=False)
                full = os.path.join(top, p)
                if os.path.isfile(full):
                    os.remove(full)

    def diff(self, name):
        """The worker's whole change as a patch, against the starting commit."""
        git(self.path(name), "add", "-A")
        return git(self.path(name), "diff", "--cached", "main")

    def apply_patch(self, name, patch_text):
        p = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=self.path(name), input=patch_text,
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise WorkTreeError(f"patch does not apply: {p.stderr.strip()[:300]}")
