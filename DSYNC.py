#!/usr/bin/env python3
"""
DSYNC  —  Dead Simple Sync
A git helper that works with GitHub, Codeberg, Forgejo, Gitea,
GitLab, self-hosted forges — anything git speaks to.

Usage:
    python DSYNC.py [command] [--remote <name>] [--all-remotes]

Commands:
    init      Initialize a new repo
    commit    Stage, commit, push
    pull      Pull with conflict resolution
    remote    Add / remove / list / switch remotes
    status    Pretty repo status overview
    log       Recent commit log
    stash     Stash or pop changes
    branch    Create, switch, delete branches
    tag       Create or list tags
    config    Show / edit per-repo DSYNC config
    sync      commit + pull + push in one shot

Flags:
    --remote <name>   Target a specific remote
    --all-remotes     Push/pull to every configured remote
    --help            This message
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# ─────────────────────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────────────────────

DSYNC_CONFIG_FILE = ".dsync.json"
VERSION = "1.0.0"

# ─────────────────────────────────────────────────────────────
#  Core helpers
# ─────────────────────────────────────────────────────────────

def run(cmd, capture=False, cwd=None):
    if capture:
        return subprocess.run(cmd, shell=True,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,
                              text=True, cwd=cwd)
    return subprocess.run(cmd, shell=True, cwd=cwd)

def check_program(name):
    return shutil.which(name) is not None

def safe_input(prompt, default=None):
    try:
        v = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        v = ""
    if v == "" and default is not None:
        return default
    return v

def in_git_repo():
    return run("git rev-parse --is-inside-work-tree", capture=True).returncode == 0

def repo_root():
    r = run("git rev-parse --show-toplevel", capture=True)
    return r.stdout.strip() if r.returncode == 0 else os.getcwd()

def current_branch():
    r = run("git rev-parse --abbrev-ref HEAD", capture=True)
    return r.stdout.strip() or "main"

def banner(title):
    print(f"\n── DSYNC  {title} ──\n")

def ok(msg):   print(f"✔  {msg}")
def warn(msg): print(f"⚠  {msg}")
def err(msg):  print(f"✖  {msg}")
def info(msg): print(f"   {msg}")

# ─────────────────────────────────────────────────────────────
#  Per-repo config  (.dsync.json at repo root)
# ─────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "version": VERSION,
    "default_remote": "origin",
    "ssh_remotes": [],        # list of remote names that prefer SSH
    "https_remotes": [],      # list of remote names that prefer HTTPS
    "push_to_all": False,     # push to every remote by default
    "default_branch": "main",
    "sign_commits": False,
    "notes": ""
}

def config_path():
    return os.path.join(repo_root(), DSYNC_CONFIG_FILE)

def load_config():
    p = config_path()
    if os.path.exists(p):
        try:
            with open(p) as f:
                data = json.load(f)
            # fill in any missing keys from defaults
            for k, v in DEFAULT_CONFIG.items():
                data.setdefault(k, v)
            return data
        except Exception:
            warn("Could not parse .dsync.json — using defaults.")
    return dict(DEFAULT_CONFIG)

def save_config(cfg):
    p = config_path()
    with open(p, "w") as f:
        json.dump(cfg, f, indent=2)
    ok(f"Config saved to {p}")

def remote_mode(cfg, remote_name):
    """Return 'ssh' or 'https' for a given remote, based on config."""
    if remote_name in cfg.get("ssh_remotes", []):
        return "ssh"
    if remote_name in cfg.get("https_remotes", []):
        return "https"
    # auto-detect from current URL
    url = get_remote_url(remote_name)
    if url and url.startswith("git@"):
        return "ssh"
    return "https"

# ─────────────────────────────────────────────────────────────
#  URL helpers  (forge-agnostic)
# ─────────────────────────────────────────────────────────────

def https_to_ssh(url: str) -> str:
    """
    https://github.com/user/repo.git   -> git@github.com:user/repo.git
    https://codeberg.org/user/repo     -> git@codeberg.org:user/repo
    https://forge.example.com/u/r.git  -> git@forge.example.com:u/r.git
    """
    m = re.match(r"https?://([^/]+)/(.+)", url)
    if m:
        return f"git@{m.group(1)}:{m.group(2)}"
    return url

def ssh_to_https(url: str) -> str:
    """
    git@github.com:user/repo.git       -> https://github.com/user/repo.git
    git@codeberg.org:user/repo         -> https://codeberg.org/user/repo
    """
    m = re.match(r"git@([^:]+):(.+)", url)
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    return url

def normalise_url(url: str, mode: str) -> str:
    if mode == "ssh":
        return https_to_ssh(url)
    return ssh_to_https(url)

def get_remote_url(name="origin"):
    r = run(f"git remote get-url {name}", capture=True)
    return r.stdout.strip() if r.returncode == 0 else None

def list_remotes():
    r = run("git remote", capture=True)
    return [x for x in r.stdout.strip().splitlines() if x] if r.returncode == 0 else []

def ensure_remote_protocol(cfg, remote_name):
    """Convert the remote's URL to the configured protocol if needed."""
    url = get_remote_url(remote_name)
    if not url:
        return
    mode = remote_mode(cfg, remote_name)
    desired = normalise_url(url, mode)
    if desired != url:
        info(f"Switching {remote_name} to {mode.upper()}: {desired}")
        run(f"git remote set-url {remote_name} {desired}")

# ─────────────────────────────────────────────────────────────
#  Conflict resolution
# ─────────────────────────────────────────────────────────────

def get_unmerged_files():
    r = run("git diff --name-only --diff-filter=U", capture=True)
    return [f for f in r.stdout.strip().splitlines() if f]

def resolve_conflicts():
    unmerged = get_unmerged_files()
    if not unmerged:
        return True

    err("Unmerged (conflicted) files:")
    for f in unmerged:
        info(f"- {f}")

    print("""
Options:
  [1] Abort merge/rebase — reset to clean state
  [2] Keep OUR version for all files
  [3] Keep THEIR version for all files
  [4] Open each file in $EDITOR manually
  [q] Quit — fix manually""")

    choice = safe_input("\nChoice [1/2/3/4/q]: ", "q").lower()

    if choice == "1":
        if run("git rev-parse -q --verify MERGE_HEAD", capture=True).returncode == 0:
            run("git merge --abort")
        else:
            run("git rebase --abort")
        ok("Aborted. Tree is clean.")
        return False

    elif choice in ("2", "3"):
        side = "ours" if choice == "2" else "theirs"
        for f in unmerged:
            run(f'git checkout --{side} -- "{f}"')
            run(f'git add "{f}"')
        msg = safe_input("Commit message: ", f"Resolve conflicts ({side})")
        run(f'git commit -m "{msg}"')
        ok(f"Resolved using {side} and committed.")
        return True

    elif choice == "4":
        editor = os.environ.get("EDITOR", "nano")
        for f in unmerged:
            print(f"\nOpening: {f}")
            subprocess.run([editor, f])
            if safe_input(f"Mark '{f}' resolved? [Y/n] ", "y").lower() not in ("n", "no"):
                run(f'git add "{f}"')
        if get_unmerged_files():
            warn("Still unresolved files. Re-run DSYNC.")
            return False
        msg = safe_input("Commit message: ", "Resolve merge conflicts")
        run(f'git commit -m "{msg}"')
        ok("Conflicts resolved and committed.")
        return True

    else:
        print("Exiting. Fix manually then re-run DSYNC.")
        sys.exit(0)

# ─────────────────────────────────────────────────────────────
#  Commands
# ─────────────────────────────────────────────────────────────

def cmd_init(args):
    banner("Init")
    cwd = os.getcwd()
    print(f"Directory : {cwd}")
    print(f"Repo name : {os.path.basename(os.path.abspath(cwd))}\n")

    if safe_input("Initialize git repo here? [Y/n] ", "y").lower() in ("n", "no"):
        print("Aborted."); return

    run("git init")

    branch = safe_input("Default branch name [main]: ", "main")
    run(f"git branch -m {branch}")

    if safe_input("Stage all files (git add .)? [Y/n] ", "y").lower() not in ("n", "no"):
        run("git add .")

    dirty = run("git status --porcelain", capture=True).stdout.strip()
    if dirty or safe_input("Create initial commit? [Y/n] ", "y").lower() not in ("n", "no"):
        msg = safe_input("Commit message [Initial commit]: ", "Initial commit")
        sign = ""
        run(f'git commit {sign}-m "{msg}"')

    # Set up remotes
    print("\nAdd remotes (leave URL blank to stop):")
    cfg = load_config()
    cfg["default_branch"] = branch
    remote_count = 0
    while True:
        rname = safe_input(f"Remote name [{'origin' if remote_count == 0 else 'blank to stop'}]: ",
                           "origin" if remote_count == 0 else "")
        if not rname:
            break
        rurl = safe_input(f"URL for '{rname}': ", "")
        if not rurl:
            break
        mode = safe_input("Protocol for this remote [https/ssh] (default: https): ", "https").lower()
        if mode not in ("ssh", "https"):
            mode = "https"
        final_url = normalise_url(rurl, mode)
        if final_url != rurl:
            info(f"Converted to {mode.upper()}: {final_url}")
        existing = run(f"git remote get-url {rname}", capture=True)
        if existing.returncode == 0:
            run(f"git remote set-url {rname} {final_url}")
        else:
            run(f"git remote add {rname} {final_url}")
        if mode == "ssh":
            cfg["ssh_remotes"].append(rname)
        else:
            cfg["https_remotes"].append(rname)
        if remote_count == 0:
            cfg["default_remote"] = rname
        ok(f"Remote '{rname}' added.")
        remote_count += 1

    if remote_count > 1:
        push_all = safe_input("Push to ALL remotes by default? [y/N] ", "n").lower() in ("y", "yes")
        cfg["push_to_all"] = push_all

    save_config(cfg)

    remotes = list_remotes()
    if remotes and safe_input(f"Push to remote(s) now? [y/N] ", "n").lower() in ("y", "yes"):
        targets = remotes if cfg["push_to_all"] else [cfg["default_remote"]]
        for r in targets:
            ensure_remote_protocol(cfg, r)
            res = run(f"git push -u {r} {branch}")
            if res.returncode != 0:
                warn(f"Push to '{r}' failed — check auth / remote setup.")

    ok(f"Repo initialized on branch '{branch}'.")


def cmd_commit(args):
    if not in_git_repo():
        err("Not inside a Git repository."); return

    banner("Commit")
    cfg = load_config()

    if safe_input("Stage all changes (git add .)? [Y/n] ", "y").lower() not in ("n", "no"):
        run("git add .")

    dirty = run("git status --porcelain", capture=True).stdout.strip()
    if not dirty:
        warn("Working tree clean — nothing to commit.")
        if safe_input("Create empty commit? [y/N] ", "n").lower() not in ("y", "yes"):
            return

    msg = None
    while not msg:
        msg = safe_input("Commit message: ", None)
        if not msg:
            warn("Commit message cannot be empty.")

    sign_flag = "-S " if cfg.get("sign_commits") else ""
    res = run(f'git commit {sign_flag}-m "{msg}"')
    if res.returncode != 0:
        warn("Commit may have failed."); return

    if safe_input("Push? [y/N] ", "n").lower() not in ("y", "yes"):
        ok("Committed (not pushed)."); return

    _do_push(cfg, args)


def _do_push(cfg, args, branch=None):
    branch = branch or current_branch()
    remotes = list_remotes()
    if not remotes:
        warn("No remotes configured. Add one with: DSYNC remote"); return

    # Decide which remotes to push to
    if "--all-remotes" in args or cfg.get("push_to_all"):
        targets = remotes
    elif "--remote" in args:
        idx = args.index("--remote")
        targets = [args[idx + 1]] if idx + 1 < len(args) else [cfg["default_remote"]]
    else:
        targets = [cfg["default_remote"]]

    for r in targets:
        ensure_remote_protocol(cfg, r)
        url = get_remote_url(r)
        info(f"Pushing to '{r}' ({url}) ...")
        res = run(f"git push -u {r} {branch}")
        if res.returncode != 0:
            warn(f"Push to '{r}' failed.")
        else:
            ok(f"Pushed to '{r}'.")


def cmd_pull(args):
    if not in_git_repo():
        err("Not inside a Git repository."); return

    banner("Pull")
    cfg = load_config()
    branch = current_branch()
    print(f"Branch: {branch}\n")

    # Resolve any pre-existing conflicts first
    if get_unmerged_files():
        err("Unmerged files detected — resolve before pulling.")
        resolved = resolve_conflicts()
        if not resolved and get_unmerged_files():
            err("Still unresolved. Cannot pull."); return

    status = run("git status --porcelain", capture=True).stdout.strip()
    if status:
        warn("You have uncommitted changes.")
        if safe_input("Continue anyway? [y/N] ", "n").lower() not in ("y", "yes"):
            print("Aborted."); return

    # Pick remote
    remote = cfg["default_remote"]
    if "--remote" in args:
        idx = args.index("--remote")
        if idx + 1 < len(args):
            remote = args[idx + 1]

    ensure_remote_protocol(cfg, remote)

    print(f"-> Fetching from '{remote}'...")
    run(f"git fetch {remote}")

    log = run(f"git log HEAD..{remote}/{branch} --oneline", capture=True).stdout
    if log.strip():
        print("\nIncoming commits:")
        print(log)
    else:
        ok("Already up to date."); return

    mode = safe_input("Merge or rebase? [m/r] (default: m) ", "m").lower()
    if mode == "r":
        res = run(f"git pull --rebase {remote} {branch}")
    else:
        res = run(f"git pull --no-rebase --allow-unrelated-histories {remote} {branch}")

    if res.returncode != 0:
        warn("Pull failed. Re-run DSYNC to resolve new conflicts.")
    else:
        ok("Synced!")


def cmd_remote(args):
    if not in_git_repo():
        err("Not inside a Git repository."); return

    banner("Remote Manager")
    cfg = load_config()

    remotes = list_remotes()
    if remotes:
        print("Configured remotes:")
        for r in remotes:
            url = get_remote_url(r)
            mode = remote_mode(cfg, r).upper()
            default = " (default)" if r == cfg["default_remote"] else ""
            print(f"  {r:<12} [{mode}]  {url}{default}")
    else:
        print("No remotes configured.\n")

    print("""
Options:
  [1] Add a remote
  [2] Remove a remote
  [3] Change URL of a remote
  [4] Switch a remote between SSH / HTTPS
  [5] Set default remote
  [6] Push to a specific remote now
  [q] Back""")

    choice = safe_input("\nChoice: ", "q").lower()

    if choice == "1":
        name = safe_input("Remote name: ", "")
        if not name: return
        url = safe_input("URL: ", "")
        if not url: return
        mode = safe_input("Protocol [https/ssh]: ", "https").lower()
        if mode not in ("ssh", "https"): mode = "https"
        final = normalise_url(url, mode)
        if final != url: info(f"Converted: {final}")
        run(f"git remote add {name} {final}")
        if mode == "ssh" and name not in cfg["ssh_remotes"]:
            cfg["ssh_remotes"].append(name)
        elif mode == "https" and name not in cfg["https_remotes"]:
            cfg["https_remotes"].append(name)
        save_config(cfg)
        ok(f"Remote '{name}' added.")

    elif choice == "2":
        name = safe_input("Remote name to remove: ", "")
        if name and safe_input(f"Remove '{name}'? [y/N] ", "n").lower() in ("y", "yes"):
            run(f"git remote remove {name}")
            cfg["ssh_remotes"] = [x for x in cfg["ssh_remotes"] if x != name]
            cfg["https_remotes"] = [x for x in cfg["https_remotes"] if x != name]
            save_config(cfg)
            ok(f"Removed '{name}'.")

    elif choice == "3":
        name = safe_input("Remote name: ", "")
        url = safe_input("New URL: ", "")
        if name and url:
            mode = remote_mode(cfg, name)
            final = normalise_url(url, mode)
            run(f"git remote set-url {name} {final}")
            ok(f"Updated '{name}' -> {final}")

    elif choice == "4":
        name = safe_input("Remote name: ", cfg["default_remote"])
        url = get_remote_url(name)
        if not url: warn(f"Remote '{name}' not found."); return
        current_mode = remote_mode(cfg, name)
        new_mode = "https" if current_mode == "ssh" else "ssh"
        new_url = normalise_url(url, new_mode)
        print(f"  {current_mode.upper()} -> {new_mode.upper()}: {new_url}")
        if safe_input("Confirm? [Y/n] ", "y").lower() not in ("n", "no"):
            run(f"git remote set-url {name} {new_url}")
            # update config lists
            cfg["ssh_remotes"] = [x for x in cfg["ssh_remotes"] if x != name]
            cfg["https_remotes"] = [x for x in cfg["https_remotes"] if x != name]
            if new_mode == "ssh":
                cfg["ssh_remotes"].append(name)
            else:
                cfg["https_remotes"].append(name)
            save_config(cfg)
            ok(f"Switched '{name}' to {new_mode.upper()}.")

    elif choice == "5":
        if not remotes: warn("No remotes to set."); return
        print("Available:", ", ".join(remotes))
        name = safe_input("Set default remote: ", remotes[0])
        if name in remotes:
            cfg["default_remote"] = name
            save_config(cfg)
            ok(f"Default remote set to '{name}'.")
        else:
            warn(f"'{name}' is not a configured remote.")

    elif choice == "6":
        name = safe_input("Push to remote: ", cfg["default_remote"])
        branch = current_branch()
        ensure_remote_protocol(cfg, name)
        run(f"git push -u {name} {branch}")


def cmd_status(args):
    if not in_git_repo():
        err("Not inside a Git repository."); return

    banner("Status")
    branch = current_branch()
    cfg = load_config()

    # Ahead/behind
    remote = cfg["default_remote"]
    run(f"git fetch {remote} --quiet")
    ahead  = run(f"git rev-list --count {remote}/{branch}..HEAD", capture=True).stdout.strip()
    behind = run(f"git rev-list --count HEAD..{remote}/{branch}", capture=True).stdout.strip()

    print(f"Branch  : {branch}")
    print(f"Remote  : {remote} ({get_remote_url(remote)})")
    try:
        print(f"Ahead   : {int(ahead)} commit(s)")
        print(f"Behind  : {int(behind)} commit(s)")
    except ValueError:
        pass
    print()
    run("git status -sb")


def cmd_log(args):
    if not in_git_repo():
        err("Not inside a Git repository."); return

    banner("Log")
    n = "15"
    for a in args:
        if a.startswith("-n") and a[2:].isdigit():
            n = a[2:]
    run(f"git log --oneline --graph --decorate -n {n}")


def cmd_stash(args):
    if not in_git_repo():
        err("Not inside a Git repository."); return

    banner("Stash")
    print("""
  [1] Stash current changes
  [2] Pop latest stash
  [3] List stashes
  [4] Drop latest stash
  [q] Back""")

    choice = safe_input("\nChoice: ", "q").lower()
    if choice == "1":
        msg = safe_input("Stash message (optional): ", "")
        if msg:
            run(f'git stash push -m "{msg}"')
        else:
            run("git stash push")
        ok("Changes stashed.")
    elif choice == "2":
        run("git stash pop")
    elif choice == "3":
        run("git stash list")
    elif choice == "4":
        if safe_input("Drop latest stash? [y/N] ", "n").lower() in ("y", "yes"):
            run("git stash drop")
            ok("Dropped.")


def cmd_branch(args):
    if not in_git_repo():
        err("Not inside a Git repository."); return

    banner("Branch")
    print("Current branches:")
    run("git branch -a")
    print("""
  [1] Create new branch
  [2] Switch branch
  [3] Create + switch
  [4] Delete branch
  [5] Rename current branch
  [q] Back""")

    choice = safe_input("\nChoice: ", "q").lower()
    if choice == "1":
        name = safe_input("Branch name: ", "")
        if name: run(f"git branch {name}"); ok(f"Branch '{name}' created.")
    elif choice == "2":
        name = safe_input("Branch to switch to: ", "")
        if name: run(f"git switch {name}")
    elif choice == "3":
        name = safe_input("New branch name: ", "")
        if name: run(f"git switch -c {name}"); ok(f"Switched to new branch '{name}'.")
    elif choice == "4":
        name = safe_input("Branch to delete: ", "")
        force = safe_input("Force delete? [y/N] ", "n").lower() in ("y", "yes")
        flag = "-D" if force else "-d"
        if name: run(f"git branch {flag} {name}")
    elif choice == "5":
        name = safe_input("New name for current branch: ", "")
        if name: run(f"git branch -m {name}"); ok(f"Renamed to '{name}'.")


def cmd_tag(args):
    if not in_git_repo():
        err("Not inside a Git repository."); return

    banner("Tags")
    run("git tag -l")
    print("""
  [1] Create lightweight tag
  [2] Create annotated tag
  [3] Delete tag
  [4] Push tags to remote
  [q] Back""")

    choice = safe_input("\nChoice: ", "q").lower()
    cfg = load_config()

    if choice == "1":
        name = safe_input("Tag name: ", "")
        if name: run(f"git tag {name}"); ok(f"Tag '{name}' created.")
    elif choice == "2":
        name = safe_input("Tag name: ", "")
        msg  = safe_input("Tag message: ", name)
        if name: run(f'git tag -a {name} -m "{msg}"'); ok(f"Annotated tag '{name}' created.")
    elif choice == "3":
        name = safe_input("Tag to delete: ", "")
        if name: run(f"git tag -d {name}")
    elif choice == "4":
        remote = safe_input(f"Push tags to remote [{cfg['default_remote']}]: ", cfg["default_remote"])
        run(f"git push {remote} --tags")


def cmd_config_cmd(args):
    if not in_git_repo():
        err("Not inside a Git repository."); return

    banner("DSYNC Config")
    cfg = load_config()
    print(json.dumps(cfg, indent=2))
    print("""
  [1] Toggle push-to-all remotes
  [2] Toggle GPG commit signing
  [3] Change default remote
  [4] Change default branch
  [5] Add a note
  [r] Reset to defaults
  [q] Back""")

    choice = safe_input("\nChoice: ", "q").lower()
    if choice == "1":
        cfg["push_to_all"] = not cfg["push_to_all"]
        ok(f"push_to_all = {cfg['push_to_all']}")
        save_config(cfg)
    elif choice == "2":
        cfg["sign_commits"] = not cfg["sign_commits"]
        ok(f"sign_commits = {cfg['sign_commits']}")
        save_config(cfg)
    elif choice == "3":
        remotes = list_remotes()
        if remotes:
            print("Available:", ", ".join(remotes))
            name = safe_input("Default remote: ", cfg["default_remote"])
            if name in remotes:
                cfg["default_remote"] = name
                save_config(cfg)
    elif choice == "4":
        b = safe_input("Default branch: ", cfg["default_branch"])
        cfg["default_branch"] = b
        save_config(cfg)
    elif choice == "5":
        note = safe_input("Note: ", "")
        cfg["notes"] = note
        save_config(cfg)
    elif choice == "r":
        if safe_input("Reset config to defaults? [y/N] ", "n").lower() in ("y", "yes"):
            save_config(dict(DEFAULT_CONFIG))


def cmd_sync(args):
    """One-shot: stage → commit → pull → push."""
    if not in_git_repo():
        err("Not inside a Git repository."); return

    banner("Sync  (commit → pull → push)")
    cfg = load_config()
    branch = current_branch()

    # Stage
    if safe_input("Stage all changes (git add .)? [Y/n] ", "y").lower() not in ("n", "no"):
        run("git add .")

    dirty = run("git status --porcelain", capture=True).stdout.strip()
    if dirty:
        msg = None
        while not msg:
            msg = safe_input("Commit message: ", None)
            if not msg: warn("Cannot be empty.")
        sign_flag = "-S " if cfg.get("sign_commits") else ""
        res = run(f'git commit {sign_flag}-m "{msg}"')
        if res.returncode != 0:
            warn("Commit failed."); return
    else:
        ok("Nothing new to commit — skipping commit step.")

    # Pull first to avoid push rejection
    remote = cfg["default_remote"]
    if "--remote" in args:
        idx = args.index("--remote")
        if idx + 1 < len(args):
            remote = args[idx + 1]

    if get_unmerged_files():
        err("Unmerged files — resolve before syncing.")
        resolve_conflicts()
        if get_unmerged_files(): return

    ensure_remote_protocol(cfg, remote)
    print(f"\n-> Pulling from '{remote}'...")
    run(f"git fetch {remote}")
    behind = run(f"git rev-list --count HEAD..{remote}/{branch}", capture=True).stdout.strip()
    try:
        if int(behind) > 0:
            mode = safe_input("Remote has new commits. Merge or rebase? [m/r] ", "m").lower()
            if mode == "r":
                run(f"git pull --rebase {remote} {branch}")
            else:
                run(f"git pull --no-rebase --allow-unrelated-histories {remote} {branch}")
            if get_unmerged_files():
                warn("New conflicts after pull — resolve then re-run DSYNC sync.")
                resolve_conflicts()
                return
    except ValueError:
        pass

    # Push
    _do_push(cfg, args, branch=branch)
    ok("Sync complete.")

# ─────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────

COMMANDS = {
    "init":    (cmd_init,       "Initialize a new repo"),
    "commit":  (cmd_commit,     "Stage, commit, and push"),
    "pull":    (cmd_pull,       "Pull with conflict resolution"),
    "remote":  (cmd_remote,     "Add / remove / manage remotes"),
    "status":  (cmd_status,     "Pretty repo status overview"),
    "log":     (cmd_log,        "Recent commit log"),
    "stash":   (cmd_stash,      "Stash or pop changes"),
    "branch":  (cmd_branch,     "Create, switch, delete branches"),
    "tag":     (cmd_tag,        "Create or list tags"),
    "config":  (cmd_config_cmd, "View / edit DSYNC config for this repo"),
    "sync":    (cmd_sync,       "Stage + commit + pull + push in one shot"),
}

def print_help():
    print(f"\nDSYNC v{VERSION}  —  Dead Simple Sync\n")
    print("Works with GitHub, Codeberg, Forgejo, Gitea, GitLab,")
    print("or any self-hosted forge.\n")
    print("Usage:")
    print("  python DSYNC.py [command] [--remote <name>] [--all-remotes]\n")
    print("Commands:")
    for k, (_, desc) in COMMANDS.items():
        print(f"  {k:<10} {desc}")
    print("""
Flags:
  --remote <name>   Target a specific remote
  --all-remotes     Push/pull to every configured remote
  --help            This message

Per-repo config is stored in .dsync.json at the repo root.
Add .dsync.json to .gitignore if you don't want to commit it,
or commit it to share settings with collaborators.
""")

def interactive_menu():
    print(f"\n  DSYNC v{VERSION}  —  Dead Simple Sync\n")
    keys = list(COMMANDS.keys())
    for i, (k, (_, desc)) in enumerate(COMMANDS.items(), 1):
        print(f"  [{i:>2}]  {k:<10}  {desc}")
    print("  [ q]  quit\n")
    choice = safe_input("Choice: ", "q").lower()
    if choice == "q":
        print("Bye!"); return None
    if choice.isdigit() and 1 <= int(choice) <= len(keys):
        return keys[int(choice) - 1]
    if choice in COMMANDS:
        return choice
    warn(f"Unknown option: {choice}")
    return None

def main():
    if not check_program("git"):
        err("git is not installed or not in PATH.")
        sys.exit(1)

    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print_help(); return

    positional = [a for a in args if not a.startswith("-")]

    if positional:
        cmd_key = positional[0].lower()
        cmd_args = args  # pass all args including flags
        if cmd_key not in COMMANDS:
            err(f"Unknown command: {cmd_key}")
            print_help(); sys.exit(1)
        COMMANDS[cmd_key][0](cmd_args)
    else:
        cmd_key = interactive_menu()
        if cmd_key:
            COMMANDS[cmd_key][0](args)

if __name__ == "__main__":
    main()
