"""Filesystem traversal with provably excluded subtrees pruned before descent."""
import fnmatch
import os
from pathlib import Path


def context_paths(root: Path, blocked_globs, *, policy_root: Path | None = None):
    """Yield descendants without opening explicitly blocked ``directory/**`` trees.

    Only full relative-path patterns ending in /** prove that every descendant
    is excluded. A blocked directory name alone does not establish that fact:
    retain existing per-file policy checks, and never follow directory symlinks.
    """
    policy_root = policy_root or root
    subtree_patterns = tuple(pattern for pattern in blocked_globs if pattern.endswith("/**"))
    for directory, directories, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in directories + files:
            yield parent / name
        directories[:] = [name for name in directories if not any(
            fnmatch.fnmatchcase((parent / name).relative_to(policy_root).as_posix() + "/", pattern)
            for pattern in subtree_patterns
        )]
