# Deploying to the Gardyn Pi

The Pi tracks a **`deploy` branch**, not `main`, and every deploy hash-verifies
the artifacts it installed. This document is the promotion policy: what moves
`deploy` forward, who decides, and how to go back.

```
./bin/deploy.sh --check      # read-only. Safe on the live host at any time.
./bin/deploy.sh              # fast-forward, install, verify
./bin/deploy.sh --rollback-to <sha>
```

---

## Why a branch rather than `main`

`main` collects work from several directions. A pull from it to fix the health
sampler also advances `mqtt.py`, which is the grow-light and pump controller,
with plants on the end of it. T-479 deployed by copying files onto the host
specifically to avoid that coupling, and it worked - but it left the checkout
describing only its own pointer and saying nothing true about what was running.
Disproving that state cost a whole session (T-485), which found the checkout
clean and four commits behind while every deployed artifact matched something
else entirely.

The objection to pulling was never really about pulling. It was about not
choosing what ships. That is a branching problem, so `deploy` fixes it and the
checkout stays honest: `git log` on the Pi describes what is deployed, because
the branch is what is deployed.

## What `deploy` is

A branch on `origin` that only ever moves **forward, by fast-forward, to a
commit somebody has decided to ship**. It is a subset of `main`'s history, never
a parallel line of development. Nothing is committed to it directly.

Two rules keep it from quietly becoming a second `main`:

1. **`deploy` only ever points at a commit that is already on `main`.** If a fix
   has not been merged, it is not promotable. A hotfix committed straight to
   `deploy` would be a change that only the Pi has ever seen.
2. **Promotion is a deliberate act by the maintainer, not a consequence of
   merging.** Nothing automatic advances it. Merging to `main` is "this is
   correct"; promoting to `deploy` is "this should run on the plants now".

## Promoting

The decision is the repository owner's. There is no automation, and adding some
would remove the only step in this process that thinks about the plants.

Before promoting, satisfy all of:

- [ ] The commit is on `origin/main`.
- [ ] Everything between the current `deploy` and the candidate has been
      **reviewed**. This is the standing rule for anything that reaches a live
      host, and the deploy branch is the only place it can actually be enforced.
      `git log --oneline deploy..<candidate>` is the list to have read.
- [ ] The test suite is green at the candidate.
- [ ] You have read the diff for anything touching `mqtt.py`, `config.py` or
      `services/etc/systemd/system/` - a change to those restarts the grow-light
      controller.
- [ ] If `bin/gardyn-netwatch.py` or either `gardyn-netwatch` unit changed, read
      **Ordering** below first. That is the one component whose bad version can
      take the host away permanently.

Then:

```bash
git fetch origin
git branch -f deploy <candidate-sha>      # or: git push origin <candidate-sha>:deploy
git push origin deploy
```

and on the Pi:

```bash
cd /home/gardyn/garden-of-eden
./bin/deploy.sh --restart-on-code-change
```

`--restart-on-code-change` is the usual flag, because most releases change only
Python. Without it, a release that touches no unit file restarts nothing and the
installer correctly refuses to call that a success.

Record the promotion on the ticket that motivated it: the sha, the date, and the
sha it replaced. That last one is the rollback target, and it is much easier to
write down now than to reconstruct later.

## Rolling back

**There is no physical recovery path for this host.** Nobody is going to pull
the SD card, so rollback has to work over the network, from a shell, with no
console. It does:

```bash
cd /home/gardyn/garden-of-eden
./bin/deploy.sh --rollback-to <previous-sha>
```

That resets the checkout, reinstalls the units from the older commit, restarts
`mqtt.service` (forced, because rolling back Python alone changes no unit file
and nothing else would), and verifies the result. The objects it needs are
already local from the last fetch, so a rollback does not depend on GitHub being
reachable, on the maintainer being awake, or on anyone force-pushing.

The rollback leaves the checkout **diverged from `origin/deploy`**, which is
deliberate: the next ordinary `./bin/deploy.sh` will refuse to fast-forward and
say so, rather than silently pulling the bad commit back in. Clear it by moving
`deploy` on the remote to the commit that should actually be running.

If the rollback itself is what fails, the fallback is one level down and still
remote:

```bash
cd /home/gardyn/garden-of-eden
git reset --hard <sha>
./bin/install-systemd-units.sh --restart-on-code-change   # it sudoes internally
./bin/verify-deployed-artifacts.sh
```

## Ordering, and what can cut off access

The host is reachable over Wi-Fi and nothing else. Ranked by what a bad deploy
of each would do:

| Artifact | If it breaks | Remotely recoverable? |
|---|---|---|
| `bin/gardyn-netwatch.py`, `gardyn-netwatch.{service,timer}` | Runs as root, reconnects Wi-Fi, and can **reboot the host**. A reboot loop ends the story. | **Only if the loop leaves a window**, and only because netwatch's own consecutive-reboot cap in `/var/lib/gardyn-netwatch` stops it. That cap is part of what a deploy can change. |
| `mqtt.py`, `config.py`, `mqtt.service` | Grow light and pump stop responding. Plants suffer; ssh does not. | Yes |
| `bin/gardyn-health-log.py`, its unit and timer | Monitoring goes quiet, and Uptime Kuma turns that into a page. | Yes |
| `bin/setup.sh` | Not part of a deploy. See below. | n/a |

So: **a deploy that changes the watchdog is gated.** `bin/deploy.sh` refuses it
unless `--netwatch-change-ok` is passed, and refuses *before* the merge, so the
tree is untouched. The sequence to use instead:

```bash
sudo systemctl disable --now gardyn-netwatch.timer     # disarm first
./bin/deploy.sh --netwatch-change-ok --restart-on-code-change
sudo systemd-analyze verify /etc/systemd/system/gardyn-netwatch.service
sudo systemctl start gardyn-netwatch.service           # one run, watch it
journalctl -t gardyn-netwatch -n 20
sudo systemctl enable --now gardyn-netwatch.timer      # re-arm only when happy
```

Disarming first means a bad version gets exactly one supervised run instead of
one every two minutes unattended.

The step in that sequence where a mid-step failure costs access is
`systemctl start gardyn-netwatch.service` - it is a real run of the real
watchdog, and a version whose escalation logic is wrong can order a reboot from
it. Everything before that point is inert.

## `bin/setup.sh` is not a deploy tool

It is the first-run provisioning script. It runs `apt update` and `apt install`,
rebuilds the venv, rewrites `/boot/config.txt` and `/etc/modules`, calls
`raspi-config`, adds group memberships and offers a reboot. T-477 made it stop
generating `mqtt.service` over its own tracked source and call the installer
instead, so it forwards arguments correctly now - but that fixed the unit half,
not the fact that it provisions a machine. Running it to ship a Python change
puts an apt transaction and a venv rebuild in front of a grow light.

Use `./bin/deploy.sh`.

## What the verification actually checks

`bin/verify-deployed-artifacts.sh` compares two classes of thing against a
commit, and the split is the point:

- **The checkout.** Git can see these, so git is the instrument: a content diff
  of the working tree against the revision, covering every tracked file with no
  list to maintain.
- **The installed copies** under `/etc/systemd/system/`. Git cannot see these at
  all. Each is hashed and compared to the blob at the same revision.

The second class is the one that motivated the ticket. Those units are copies;
they can be edited in place, half-written by an interrupted run, masked with
`systemctl mask`, or left behind by a rollback, and every one of those states
reports a clean `git status`.

It is read-only, unprivileged, runs no `sudo`, and restarts nothing, so
`--check` is safe on the live host whenever you want an answer.

It fails closed. Exit `0` means every artifact matched, `1` means something
differs, and **`2` means the check could not be carried out** - which is not a
milder version of `0`. A verifier that cannot report a mismatch is worse than no
verifier, because it reads as assurance.

### What it cannot tell you

Whether the **running process** is executing the code on disk. Files cannot
answer that. `bin/install-systemd-units.sh` records the revision it last
restarted `mqtt.service` at, in `.gardyn-source-revision` beside the units, and
the verifier reports that comparison but never writes the file - it has one
writer, and two would be worse than none.

A consequence worth knowing: that file did not exist before T-491's installer
ran for the first time. On its first run the installer takes the current
revision as a baseline and says so. That baseline is an assumption, not a
measurement, and it is only sound if `mqtt.py` has not moved since the service
was last restarted.

## First-time setup: pointing the Pi at `deploy`

Done once. The whole point of the sequence below is that **step 3 changes
nothing on the host** — the branch is created where the Pi already is, so the
repoint moves no file, restarts no service, and cannot fail in a way that costs
access. Everything risky happens afterwards, on a host that is already tracking
the branch and can therefore be rolled back.

### 1. Ask the Pi where it is (read-only)

```bash
cd /home/gardyn/garden-of-eden
git rev-parse HEAD
git status --porcelain          # expect no output
git rev-parse --abbrev-ref HEAD
```

The sha from the first command is the branch point. Do not assume it.

### 2. Check the installed units before touching anything

`bin/verify-deployed-artifacts.sh` is not on the host yet — it arrives with the
first promotion — so do this by hand, on the Pi:

```bash
cd /home/gardyn/garden-of-eden
for u in services/etc/systemd/system/*; do
  n=$(basename "$u")
  a=$(git show "HEAD:$u" | sha256sum | cut -d' ' -f1)
  b=$(sha256sum "/etc/systemd/system/$n" 2>/dev/null | cut -d' ' -f1)
  if [ "$a" = "$b" ]; then echo "OK   $n"; else echo "DIFF $n repo=$a deployed=$b"; fi
done
```

Read the output for one specific thing before believing it: **any hash printed
as `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` is the
SHA-256 of the empty string**, which means `git show` printed nothing and that
line is meaningless rather than reassuring. A pipeline exits with the status of
its last command, so a failed `git show` here is silent. Five `OK` lines with no
`e3b0c442…` anywhere is the result you want.

### 3. Create the branch there, and repoint

On the workstation, using the sha from step 1:

```bash
git fetch origin
git branch -f deploy <sha-from-step-1>
git push origin deploy
```

On the Pi:

```bash
cd /home/gardyn/garden-of-eden
git fetch origin
git checkout -B deploy origin/deploy
git status --porcelain                          # expect no output
git rev-list --left-right --count origin/deploy...HEAD   # expect: 0	0
```

`git status` clean and `0	0` together mean the repoint was a no-op: same commit,
same files, nothing to pull, nothing local. If either disagrees, stop — the
branch was created at the wrong commit, and the fix is to move `deploy` rather
than to move the Pi.

### 4. Ship the deploy tooling itself, as the first real promotion

Only now does anything change. Promote a commit that contains `bin/deploy.sh`
and `bin/verify-deployed-artifacts.sh`, then on the Pi:

```bash
cd /home/gardyn/garden-of-eden
git pull --ff-only                       # deploy.sh does not exist here yet
./bin/deploy.sh --check                  # read-only: what is on the host now?
./bin/deploy.sh --no-pull --restart-on-code-change
```

After that first run, `./bin/deploy.sh` is the whole procedure.

Expect the installer to warn once that no revision was recorded for
`mqtt.service` and that it is taking the current one as a baseline. That is
correct and only happens once — `.gardyn-source-revision` did not exist before
T-491's installer ran for the first time. Note what it means, though: the
baseline is an **assumption** that the running service was started from the code
now on disk, not a measurement. If there is any doubt, the
`--restart-on-code-change` above settles it by restarting and recording in one
step.

## Relationship to `.gardyn-source-revision`

The revision check and the deploy branch do not fight, because the check
compares **commit shas** and knows nothing about branch names. Two consequences:

- Creating `deploy` at exactly the commit the Pi is already on makes the repoint
  a no-op. `HEAD` does not move, the recorded revision stays equal to it, and
  the check stays quiet. That is why the branch must start where the host
  actually is.
- Pointing the Pi at a `deploy` that is anywhere else *is* a code change, and the
  next installer run will correctly report the service as stale. That is not a
  bug in either mechanism; it is the check doing its job on an undeclared deploy.
