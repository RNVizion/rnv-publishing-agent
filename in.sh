#!/usr/bin/env bash
# Fix 3: catch up on the remote before giving up on a push.
# One commit, verified before it is pushed. Safe to re-run.
#
#   bash fix_catchup.sh
#
set -euo pipefail

G="\033[32m"; R="\033[31m"; Y="\033[33m"; C="\033[36m"; N="\033[0m"
say() { echo -e "\n${C}== $* ==${N}"; }
ok()  { echo -e "  ${G}ok${N}    $*"; }
skip(){ echo -e "  ${Y}skip${N}  $*"; }
bad() { echo -e "  ${R}stop${N}  $*"; exit 1; }

[ -f server.py ] && [ -d tests ] || bad "run this from the rnv-publishing-agent repo root"
ok "repo root: $(pwd)"

say "sync"
git pull --rebase --autostash
ok "up to date with origin"

say "tooling"
python -m pip install -q -r tests/requirements-dev.txt
python -c "import pytest" 2>/dev/null || bad "pytest not importable after install"
ok "pytest $(python -c 'import pytest; print(pytest.__version__)')"

cat > /tmp/_patch_catchup.py <<'PATCHEOF'
import sys
from pathlib import Path


HELPER_OLD = '''@mcp.tool()
def commit_and_push(slug: str, message: str = "", dry_run: bool = False) -> dict:'''

HELPER_NEW = '''def _push_with_catchup(run) -> dict:
    """Push. If the remote has moved, rebase onto it and push again.

    WHY THIS IS NOT OVER-REACH
      Both repos this chain writes are written by GitHub Actions too. A publish
      pushes the post; then build-feed commits the regenerated index, feed, sitemap
      and robots, and build-og commits the share image, on top of it. So the local
      clone is behind *after every successful publish*, and the next publish's push
      is rejected — not because anything is wrong, but because the pipeline did its
      job. Being behind is the normal resting state here, not an anomaly.

      Which makes it a queue, not a defect, and the project's own rule applies:
      gate a defect, warn about a queue. Waiting does not fix this one, but catching
      up does, and catching up is what a person does without thinking. Refusing
      instead would fail every publish after the first until someone ran git pull by
      hand — a gate that fires on the normal case is a gate people learn to skip.

    WHY IT REACTS RATHER THAN PREDICTS
      It pushes first and only catches up on an actual rejection, rather than
      fetching every time to find out whether it needs to. The remote's answer is
      the fact; a pre-check is a guess about the same fact, one round trip earlier
      and one race condition wider.

    WHY REBASE, NEVER MERGE
      The local side is one publish commit; the remote side is generated output. A
      merge would record a fork that never conceptually happened and leave the
      Actions' commits out of order. Replaying the publish on top keeps the history
      a straight line that reads the way the work actually happened.

    WHY A CONFLICT ABORTS
      A conflict means the two sides changed the same lines, which is not a queue and
      is not fixed by waiting or retrying. It aborts the rebase — restoring the tree,
      including anything --autostash had set aside — and reports. Half-rebased is the
      one state worse than not having tried.

    Bought on 2026-09-14: the corpus push was rejected because the rebuild-corpus
    Action had pushed since the last run. The post was already live by then, so the
    chain ended with the site published and the corpus not updated — the two out of
    step, which is the exact half-done state the ordering everywhere else exists to
    prevent.
    """
    push = run("push")
    if push.returncode == 0:
        return {"ok": True, "caught_up": False}

    err = (push.stderr or "").strip()
    # Only a non-fast-forward is a catch-up situation. Everything else — no
    # credentials, no network, a protected branch — is a real failure, and retrying
    # after a rebase would turn one clear error into two confusing ones.
    behind = any(s in err for s in ("fetch first", "non-fast-forward", "behind its remote"))
    if not behind:
        return {"ok": False, "caught_up": False, "error": err or "git push failed"}

    # Fetch first, and this is not belt-and-braces. A rejected push does NOT update
    # the remote-tracking ref, so origin/main — which is what @{u} resolves to — still
    # points where it did before the rejection. Rebasing onto it without fetching
    # reports "Current branch main is up to date" and changes nothing, after which the
    # retry is rejected for exactly the same reason: a repair that silently no-ops and
    # then fails identically, which reads as the fix not working rather than the fix
    # not having run. Found by running it.
    fetch = run("fetch", "--quiet", "origin")
    if fetch.returncode != 0:
        return {"ok": False, "caught_up": False,
                "error": f"push was rejected and the remote could not be fetched to catch up: "
                         f"{(fetch.stderr or '').strip() or 'fetch failed'}"}

    # --autostash because the working tree legitimately carries other changes here:
    # commit_and_push stages only the post, so a regenerated feed.xml sitting
    # unstaged is normal and must not block the catch-up or be swept into it.
    rebase = run("rebase", "--autostash", "@{u}")
    if rebase.returncode != 0:
        run("rebase", "--abort")
        return {"ok": False, "caught_up": False,
                "error": f"the remote had moved and the catch-up rebase failed, so nothing "
                         f"was pushed: {(rebase.stderr or '').strip() or 'rebase failed'}"}

    again = run("push")
    if again.returncode != 0:
        return {"ok": False, "caught_up": True,
                "error": (again.stderr or "").strip() or "git push failed after catching up"}

    return {"ok": True, "caught_up": True}


@mcp.tool()
def commit_and_push(slug: str, message: str = "", dry_run: bool = False) -> dict:'''

BLOG_OLD = '''    push = _git("push")
    if push.returncode != 0:
        return {"slug": slug, "ok": False, "committed": True, "pushed": False,
                "error": push.stderr.strip() or "git push failed"}
    return {"slug": slug, "ok": True, "committed": True, "pushed": True,
            "message": msg, "files": pending}'''

BLOG_NEW = '''    push = _push_with_catchup(_git)
    if not push["ok"]:
        return {"slug": slug, "ok": False, "committed": True, "pushed": False,
                "error": push["error"]}
    out = {"slug": slug, "ok": True, "committed": True, "pushed": True,
           "message": msg, "files": pending}
    # Present only when it happened, the same way `warnings` is. A field that reads
    # false on almost every run is noise in a trace a human reads, and its absence
    # is already the answer.
    if push["caught_up"]:
        out["caught_up"] = True
    return out'''

CORPUS_OLD = '''    push = _cgit("push")
    if push.returncode != 0:
        return {"slug": slug, "ok": False, "added": True, "pushed": False,
                "error": push.stderr.strip() or "git push failed (corpus repo write permission?)"}
    return {"slug": slug, "ok": True, "added": True, "pushed": True, "url": url,
            "note": "pushed; the rebuild-corpus Action will re-ingest and update the Space"}'''

CORPUS_NEW = '''    push = _push_with_catchup(_cgit)
    if not push["ok"]:
        return {"slug": slug, "ok": False, "added": True, "pushed": False,
                "error": push["error"] + " (corpus repo write permission?)"}
    out = {"slug": slug, "ok": True, "added": True, "pushed": True, "url": url,
           "note": "pushed; the rebuild-corpus Action will re-ingest and update the Space"}
    if push["caught_up"]:
        out["caught_up"] = True
    return out'''

EDITS = [
    (HELPER_OLD, HELPER_NEW, "def _push_with_catchup("),
    (BLOG_OLD, BLOG_NEW, "_push_with_catchup(_git)"),
    (CORPUS_OLD, CORPUS_NEW, "_push_with_catchup(_cgit)"),
]




def main() -> int:
    undo = "--undo" in sys.argv
    p = Path("server.py")
    s = p.read_text(encoding="utf-8")
    out = s
    for old, new, marker in EDITS:
        present = marker in out
        if (present and not undo) or (not present and undo):
            continue
        frm, to = (new, old) if undo else (old, new)
        n = out.count(frm)
        if n != 1:
            sys.exit(f"  stop  server.py: expected exactly one match near {marker!r}, "
                     f"found {n} — file has moved, re-fetch")
        out = out.replace(frm, to)
    if out == s:
        print("  skip  server.py already in that state")
        return 0
    p.write_text(out, encoding="utf-8")
    sites = out.count("_push_with_catchup(") - out.count("def _push_with_catchup(")
    print(f"  ok    server.py {'reverted' if undo else 'patched'} — "
          f"_push_with_catchup at {sites} push site(s)")
    return 0

raise SystemExit(main())
PATCHEOF

say "patch server.py"
python /tmp/_patch_catchup.py
python -c "import ast,pathlib; ast.parse(pathlib.Path('server.py').read_text(encoding='utf-8'))" \
  || bad "server.py no longer parses — do not commit"
ok "server.py parses"

say "pin it with tests"
if grep -q "test_a_publish_catches_up_when_a_workflow_has_pushed" tests/test_chain.py; then
  skip "tests already present"
else
  cat >> tests/test_chain.py <<'TESTEOF'


def _a_workflow_pushes(remote, tmp_path, name: str, message: str) -> None:
    """Commit to the remote from somewhere that is not the chain's clone.

    This is what build-feed and build-og do after every publish: regenerate the
    index, feed, sitemap and share image, and commit them on top of the post. The
    local clone is behind from that moment on, which makes being behind the normal
    resting state between publishes rather than an anomaly.
    """
    work = tmp_path / name
    subprocess.run(["git", "clone", "-q", str(remote), str(work)],
                   check=True, capture_output=True)
    for key, value in (("user.email", "action@example.invalid"),
                       ("user.name", "Workflow"),
                       ("commit.gpgsign", "false")):
        subprocess.run(["git", "config", key, value], cwd=work,
                       check=True, capture_output=True)
    (work / "generated.txt").write_text(message + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=work, check=True, capture_output=True)


def test_a_publish_catches_up_when_a_workflow_has_pushed(blog, blog_remote, corpus,
                                                         site, srv, fake_web, tmp_path):
    """A remote that moved since the last publish does not fail the next one.

    Bought on 2026-09-14. The chain pushed the post, the Actions committed their
    generated output on top, and the next run's push was rejected as non-fast-forward
    — correctly, and uselessly. Refusing here would fail every publish after the
    first until someone ran git pull by hand, and a gate that fires on the normal
    case is a gate people learn to skip.
    """
    from conftest import sitemap_xml

    write_post(blog, "ready", site)
    fake_web[f"{site}/blog/ready/"] = 200
    fake_web[f"{site}/sitemap.xml"] = (200, sitemap_xml(site, "ready"))
    fake_web[f"{site}/assets/og/ready.png"] = 200

    workflow = "chore: rebuild feed, index, sitemap and robots [skip ci]"
    _a_workflow_pushes(blog_remote, tmp_path, "site-workflow", workflow)

    result = srv.publish_post("ready", for_real=True)

    assert result["ok"] is True, result
    subjects = _remote_log(blog_remote)
    assert "Publish: ready" in subjects, "the post never reached the remote"
    assert workflow in subjects, "the catch-up discarded the workflow's commit"
    # Newest first: the publish must sit ON TOP of the workflow's commit, replayed
    # rather than merged beside it.
    assert subjects.index("Publish: ready") < subjects.index(workflow)
    assert not any(s.startswith("Merge ") for s in subjects), \
        "caught up with a merge; the history should stay a straight line"


def test_the_corpus_push_catches_up_too(blog, blog_remote, corpus, site, srv,
                                        fake_web, tmp_path):
    """The corpus repo has the same shape of writer, so it has the same exposure.

    On 2026-09-14 this was the half that actually broke: the post went live and the
    corpus write was rejected, leaving the site published and the corpus not — the
    exact out-of-step state the chain's ordering exists to prevent.
    """
    from conftest import sitemap_xml

    write_post(blog, "ready", site)
    fake_web[f"{site}/blog/ready/"] = 200
    fake_web[f"{site}/sitemap.xml"] = (200, sitemap_xml(site, "ready"))
    fake_web[f"{site}/assets/og/ready.png"] = 200

    rebuild = "corpus: refresh after source change"
    _a_workflow_pushes(tmp_path / "corpus-remote.git", tmp_path, "corpus-workflow", rebuild)

    result = srv.publish_post("ready", for_real=True)

    assert result["ok"] is True, result
    corpus_subjects = _remote_log(tmp_path / "corpus-remote.git")
    assert "corpus: add ready" in corpus_subjects, "the corpus entry never reached the remote"
    assert rebuild in corpus_subjects, "the catch-up discarded the rebuild commit"
TESTEOF
  ok "appended two tests to tests/test_chain.py"
fi

# Two mutations, because there are two ways to get this wrong and only one of them
# is "no fix at all". The second is the bug this very script hit in development:
# the catch-up present but rebasing onto a stale remote-tracking ref, which no-ops
# and then fails identically.
say "mutation 1 — no catch-up at all"
cp server.py /tmp/_srv3.py
python /tmp/_patch_catchup.py --undo >/dev/null
M1=$(python -m pytest tests/test_chain.py -q -k "catches_up or catch" 2>&1 || true)
cp /tmp/_srv3.py server.py
echo "$M1" | grep -q "2 failed" || bad $'mutation 1 did not fail both tests. pytest said:\n'"$M1"
ok "both tests fail without the catch-up"

say "mutation 2 — catch-up without the fetch"
python - <<'MUT'
import pathlib
p = pathlib.Path("server.py"); s = p.read_text(encoding="utf-8")
i = s.index('    fetch = run("fetch", "--quiet", "origin")')
j = s.index('    # --autostash because', i)
p.write_text(s[:i] + s[j:], encoding="utf-8")
MUT
M2=$(python -m pytest tests/test_chain.py -q -k "catches_up or catch" 2>&1 || true)
cp /tmp/_srv3.py server.py
rm -f /tmp/_srv3.py /tmp/_patch_catchup.py
echo "$M2" | grep -q "2 failed" || bad $'mutation 2 did not fail both tests. pytest said:\n'"$M2"
ok "both tests fail when the catch-up rebases onto a stale ref"

say "full suite"
python -m pytest -q || bad "suite is red — nothing committed, nothing pushed"
ok "green"

say "demo unchanged"
python demo/run_demo.py >/tmp/_d.out 2>/dev/null || bad "demo failed"
grep -cE "^ *PASS" /tmp/_d.out | grep -q "^5$" || bad "demo no longer prints 5 PASS lines"
ok "demo: $(wc -l < /tmp/_d.out) lines of stdout, 5 PASS"

if git diff --quiet HEAD -- server.py tests/test_chain.py; then
  skip "already committed"
else
  git add server.py tests/test_chain.py
  git commit -q -m "Catch up on the remote before giving up on a push

Both repos this chain writes are written by GitHub Actions too. A
publish pushes the post; build-feed then commits the regenerated index,
feed, sitemap and robots, and build-og the share image, on top of it. So
the local clone is behind after every successful publish, and the next
publish's push is rejected — not because anything is wrong, but because
the pipeline did its job.

That makes it a queue rather than a defect, and the project's own rule
applies: gate a defect, warn about a queue. Refusing would fail every
publish after the first until someone ran git pull by hand, and a gate
that fires on the normal case is a gate people learn to skip.

On 2026-09-14 it cost a partial success: the post went live and the
corpus push was rejected, leaving the site published and the corpus not.

It pushes first and reacts to the actual rejection rather than fetching
every time to predict one. The fetch before the rebase is load-bearing:
a rejected push does not update the remote-tracking ref, so rebasing
onto @{u} without it reports 'up to date', changes nothing, and fails
again identically. Rebase never merge, since the local side is one
publish and the remote side is generated output. A conflict aborts and
reports — half-rebased is the one state worse than not having tried.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013Ai6mP1x3Rv5n9ygKQgidt"
  ok "committed"
fi

say "push"
git push
ok "pushed"

say "done"
git log --oneline -2
