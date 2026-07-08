"""Real capture backend against the OmegaClaw fork container (slice-2 Phase B).

STATUS: scaffold. The Phase-A harness (runner, record writer, validator) is complete and
exercised via `StubCaptureBackend`; this real backend is deferred pending a scope decision,
because driving the fork's Test provider is a host-side integration sub-project, not a thin
adapter. The fork-internal mechanics are pinned (from reading
`/home/cassio/src/SNET/OmegaClaw-Core` @ 38ebd6d), recorded here so the build is turnkey:

Boot (base image `localhost/omegaclaw-base`), one ephemeral container per rep:
  podman run --add-host=host.docker.internal:host-gateway \
    -e TEST_SERVER_IP=<host-gateway-ip> \
    -v <capture-dir>:/PeTTa/repos/OmegaClaw-Core/memory \
    <image> commchannel=test provider=Test embeddingprovider=Local \
    securityPolicyPath=/PeTTa/repos/OmegaClaw-Core/profile/policy.yaml \
    TEST_SERVER_IP=<host-gateway-ip>
  (ENTRYPOINT entrypoint.sh; runs as nobody under WORKDIR /PeTTa; GATEWAY_URL defaults set.)

Drive (the Test provider/channel are RPC *clients* dialing OUT to a host controller):
  - Host controller listens on 9765 (LlmMockController) and 9766 (CommMockServer) —
    classes in the fork's Autotests/mock/{llm,comm,rpc}.py (length-prefixed JSON/TCP).
  - Per turn: set_answer(make_prompt(run_id, task), <canned command line>) then
    send_message(prompt). The agent drains one user message per loop iteration.
  - With `-p Test`, the model reply is whatever we scripted (this proves plumbing, not a
    model); real-model v0a runs (slice 3) use a real provider + the mock *channel* only.

Capture:
  - stdout via `podman logs`: with Test provider the reply appears as
    `[LlmMockAgent] Mock answers: <text>` (real providers emit `[LLM_RAW] ts=… raw=<repr>`).
  - memory/history.metta from the mounted volume (blocks: `<ts>\nHUMAN_MESSAGE: …\n<resp>\n`).
  - Parse trace: replay each raw line through the container's own parser via
      podman exec … python3 -c '<add src to path>; import helper;
                                print(helper.balance_parentheses(<raw>))'
    (module at /PeTTa/repos/OmegaClaw-Core/src/helper.py; balance_parentheses is standalone
    Python; sread is reached via the `(metta "…")` skill).
  - Stock command heads (base image, for command-recognition / v0a scenario recasting):
      append-file episodes metta pin query read-file remember search send shell
      tavily-search technical-analysis write-file   (src/helper.py LLM_COMMANDS)
  - image_id via `podman image inspect --format {{.Id}}`; wall-clock telemetry; teardown.

Port the `dc()` / `DOCKER_HOST` rootless-Podman idiom from scripts/deploy_checks.sh.
"""

from __future__ import annotations

from .capture import CaptureResult
from .loader import LoadedScenario

_DEFER = (
    "ForkContainerCaptureBackend is a Phase-B scaffold. The Phase-A harness + record "
    "machinery are complete and usable via `--backend stub`. Building the real backend "
    "means standing up a host-side RPC mock controller (ports 9765/9766) that the fork "
    "container dials into — a scoped integration task; see this module's docstring."
)


class ForkContainerCaptureBackend:
    """Boots the fork base image with the Test provider and captures real artifacts.
    Deferred; see module docstring for the pinned build recipe."""

    def __init__(self, image_ref: str = "localhost/omegaclaw-base:0.1") -> None:
        self.image_ref = image_ref
        self.image_id = ""

    def capture(self, loaded: LoadedScenario, model: str, rep: int) -> CaptureResult:
        raise NotImplementedError(_DEFER)
