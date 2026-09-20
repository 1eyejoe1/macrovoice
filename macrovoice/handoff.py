"""Direct VoiceInk-to-macrowhisper delivery.

The normal bridge publishes a Superwhisper-shaped folder into macrowhisper's
watch root.  That remains the default.  Direct handoff is for the complementary
case where macrowhisper must keep watching a real Superwhisper directory: it
hands the generated ``meta.json`` to macrowhisper's public CLI instead.

The generated file first enters macrovoice's durable spool.  It is removed only
after ``macrowhisper --run-auto --meta`` exits successfully, so a missing binary,
timeout, or failed invocation leaves the dictation recoverable for a later
``--drain-only`` run.
"""

import subprocess
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .publisher import Publisher

__all__ = ["DEFAULT_TIMEOUT_S", "DirectHandoff"]

# VoiceInk stops a Custom Command after 10 seconds.  The publisher's six-second
# drain budget leaves enough room for staging and logging while still preventing
# the runner from being killed in the middle of an unbounded subprocess wait.
DEFAULT_TIMEOUT_S = 6.0


def _run(args: List[str], timeout: float):
    """Run macrowhisper without retaining its output or the dictated text."""
    completed = subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=timeout,
        check=False,
    )
    return completed.returncode, completed.stdout


class DirectHandoff:
    """Deliver spooled files through ``macrowhisper --run-auto --meta``.

    ``runner`` is injectable so command construction and recovery behaviour are
    testable without a real macrowhisper install.
    """

    def __init__(
        self,
        binary: str = "macrowhisper",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        runner: Optional[Callable[[List[str], float], Tuple[int, str]]] = None,
    ) -> None:
        self.binary = binary
        self.timeout_s = timeout_s
        self._runner = runner or _run
        self.last_error: Optional[str] = None

    def _deliver(self, meta_path: Path, remaining_s: float) -> bool:
        # A non-positive timeout is not useful to subprocess and means the
        # publisher's bounded drain window is already over.
        timeout = min(self.timeout_s, remaining_s)
        if timeout <= 0:
            self.last_error = "direct handoff ran out of delivery time"
            return False

        try:
            result, output = self._runner(
                [self.binary, "--run-auto", "--meta", str(meta_path.resolve())], timeout
            )
        except subprocess.TimeoutExpired:
            self.last_error = "macrowhisper direct handoff timed out"
            return False
        except OSError:
            # Keep the log deliberately free of subprocess output: it may echo
            # user content, while this error tells the user what they can act on.
            self.last_error = "could not start macrowhisper for direct handoff"
            return False
        except Exception:
            self.last_error = "macrowhisper direct handoff failed before completion"
            return False

        # The CLI exits zero even when its daemon is unavailable or an action
        # fails. Only its explicit execution response acknowledges delivery.
        if result == 0 and any(
            line.startswith("Executed ") and (
                line.endswith(" via trigger resolution")
                or line.endswith(" via active action fallback")
            ) for line in output.splitlines()
        ):
            return True
        self.last_error = "macrowhisper did not acknowledge direct execution (exit %s)" % result
        return False

    def drain(self, publisher: Publisher) -> List[Path]:
        """Attempt every queued transcript until one cannot be delivered.

        ``Publisher`` owns the spool and its cross-process drain lock.  This
        deliberately shares that lock with watch-folder delivery so switching a
        configuration cannot make two VoiceInk commands run the same queued file.
        """
        self.last_error = None
        return publisher.drain_direct(self._deliver)
