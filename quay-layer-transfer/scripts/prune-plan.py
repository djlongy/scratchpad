#!/usr/bin/env python3
"""Low side: decide which blobs the far side may delete, and forget them here.

The dedupe ledger records a digest when it is SENT, because with no return path
there is nothing that could confirm receipt. That makes the ledger and the far
side's content store a single coupled system: delete a blob over there while the
ledger still says "sent" and it is never re-sent, so that image can never be
rebuilt. Pruning the far store on its own is therefore unsafe at any speed.

The way out is to notice that the LOW side already holds every fact needed to
decide. It authored the desired state, and its export layout contains every blob
that desired state references. So:

    prunable = (everything the ledger has ever sent) - (everything still wanted)

and the low side can clear those ledger entries itself, in the same breath as it
tells the far side to drop them.

**Ordering is the safety property.** The ledger is cleared HERE, before the
transfer leaves. If the transfer is then lost, the low side has merely forgotten
that it once sent those blobs: the next transfer that needs them sends them
again. The reverse order — far side deletes first, ledger cleared later — leaves
a window where the content is gone and the ledger still claims it crossed, which
is exactly the unrecoverable state this design exists to avoid.

In BIDIRECTIONAL mode none of that applies, because the record of what the far
side holds is published by the far side itself (have/blobs.txt). Pass that file
instead of a Redis command: the arithmetic is the same, there is nothing to
forget locally, and a blob pruned over there simply reappears in the next
published list and is exported again. Pruning is safe there at any speed.

  usage: prune-plan.py OCI_LAYOUT_DIR REDIS_CLI_CMD...
         prune-plan.py OCI_LAYOUT_DIR HAVE_FILE
         prints the prunable digests, one per line, having removed them from the
         ledger when the ledger is what was given. Prints nothing when there is
         nothing to prune, or when the have file does not exist yet.

Env:
  PRUNE_MAX_PCT   refuse if more than this share of the ledger would go (default 60)
  PRUNE_FORCE=1   override that refusal
"""
import os
import subprocess
import sys
from pathlib import Path

MAX_PCT = int(os.environ.get("PRUNE_MAX_PCT", "60"))
FORCE = os.environ.get("PRUNE_FORCE") == "1"


def redis(cmd, *args):
    out = subprocess.run(cmd + list(args), capture_output=True, text=True, check=True)
    return out.stdout


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    layout, redis_cli = sys.argv[1], sys.argv[2:]

    blobs = Path(layout) / "blobs" / "sha256"
    wanted = {p.name for p in blobs.iterdir() if p.is_file()} if blobs.is_dir() else set()
    if not wanted:
        sys.exit("prune-plan: the export layout holds no blobs; refusing to treat "
                 "that as 'nothing is wanted'")

    # One argument that names a file is the far side's published digest list; anything
    # else is a Redis CLI command. The two carry the same information in opposite
    # directions: what the low side believes it sent, or what the high side says it has.
    have = Path(redis_cli[0]) if len(redis_cli) == 1 else None
    if have is not None:
        if not have.exists():
            return                       # first transfer: the far side has published nothing
        keys = {k.strip().removeprefix("sha256:") for k in have.read_text().splitlines() if k.strip()}
    else:
        keys = {k.strip() for k in redis(redis_cli, "--scan").splitlines() if k.strip()}

    prunable = sorted(keys - wanted)
    if not prunable:
        return

    if not FORCE and len(prunable) * 100 > len(keys) * MAX_PCT:
        sys.exit(f"prune-plan: {len(prunable)} of {len(keys)} recorded blobs would be "
                 f"pruned (> {MAX_PCT}%). Refusing. Set PRUNE_FORCE=1 to override.")

    if have is None:
        # Forget first, announce second. A lost transfer must only ever cost a re-send.
        for i in range(0, len(prunable), 200):
            redis(redis_cli, "DEL", *prunable[i:i + 200])

    print("\n".join(prunable))


if __name__ == "__main__":
    main()
