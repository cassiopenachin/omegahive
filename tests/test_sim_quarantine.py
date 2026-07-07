"""§8 sim quarantine: the substrate imports none of omegahive.sim.

Run in a subprocess so imports made by other tests in this session can't contaminate
sys.modules and mask a real leak.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap


def test_substrate_does_not_import_sim():
    script = textwrap.dedent(
        """
        import sys
        import omegahive.events
        import omegahive.gateway
        import omegahive.board
        import omegahive.port
        import omegahive.clock
        import omegahive.db
        import omegahive.config
        leaked = sorted(m for m in sys.modules if m.startswith("omegahive.sim"))
        assert not leaked, f"substrate leaked sim imports: {leaked}"
        """
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
