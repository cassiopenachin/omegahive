# OmegaHive — Beastie Remote Access Spec

**Status:** v1. Covers remote access for Beastie (deployment #0) during and after a 5-week unattended absence. Governed by [omegahive_deployment_spec.md](omegahive_deployment_spec.md) §4 (secrets), §5 (recovery, environment-change rule), §7 (host-facts).

## 1. Decision

**Tailscale**, running as a system-wide daemon on Beastie, carrying plain SSH (not Tailscale SSH) and future web-UI traffic over the tailnet only.

Rationale: Beastie is NAT'd with no inbound, so the access tool must either hole-punch out or require a self-run public endpoint. Tailscale hole-punches, opens zero inbound ports, and is already field-proven in this circle with a keys-only policy. Rejected alternatives: **plain self-managed WireGuard** — no control-plane dependency, but Beastie's NAT means the operator would have to build and babysit rendezvous/relay infrastructure remotely, which is more absence-risk than it removes. **ZeroTier/NetBird** — same architecture as Tailscale, no advantage, no precedent here. **cloudflared/SSH-over-tunnel** — puts a third-party edge in the path of the one channel (SSH) that must stay minimally dependent; rejected for the recovery path specifically. **Dumb VPS jump-host** — fully self-owned but is itself new inbound-facing infrastructure to provision and patch during a trip, for no benefit over Tailscale's inbound-free posture.

## 2. Setup on Beastie

- Install: `sudo dnf install tailscale` (Fedora repo) or the upstream install script; enable and start: `sudo systemctl enable --now tailscaled`.
- Auth: `sudo tailscale up --ssh=false --hostname=beastie`. Authenticate via the operator's Tailscale account (browser flow or a one-time auth key — see §5 for handling).
- **Key/auth expiry — the critical item.** Tailscale's default 180-day key expiry is longer than the 5-week trip, so default behavior is already safe; do not rely on defaults alone. In the admin console, mark Beastie's device as **"Key expiry disabled"** before departure. Tradeoff: disabling expiry on a headless node is standard operator practice for unattended machines, but it means a stolen/cloned node key stays valid indefinitely — mitigate by pairing it with the ACL restriction in this section (Beastie only exposed to the operator's own device tags, nothing else) and revoking the node from the admin console immediately if compromise is suspected. Do not use a tagged-device service policy here; that's for fleet automation, not a single human-owned workstation, and adds indirection with no benefit at this scale.
- MagicDNS: enable at the tailnet level (admin console → DNS). Beastie becomes reachable as `beastie.<tailnet-name>.ts.net` — no static IP tracking needed.
- **SSH: keep plain sshd over the tailnet; do not enable Tailscale SSH.** Rationale: the deployment spec's §5 recovery path must be a human-only, agent-free, minimally-dependent OOB channel. Tailscale SSH routes auth/session decisions through Tailscale's control plane and ACL engine at connection time; plain sshd with keys-only auth depends only on the tailnet's data plane (already required for any access) and the host's own `authorized_keys`. Fewer moving parts in the one path that must work when everything else has failed. Configure: `PasswordAuthentication no`, `PubkeyAuthentication yes`, restrict `AllowUsers` to the deploy user; sshd continues to bind all interfaces (or explicitly `ListenAddress` on the tailnet IP + loopback) — never expose 22 on the LAN-facing/WAN-facing NIC beyond what's already non-routable.
- **ACL sketch** (Tailscale admin console, `acls.json` or the UI editor):
  ```
  {
    "acls": [
      {"action": "accept", "src": ["group:operator"], "dst": ["tag:beastie:22"]},
      {"action": "accept", "src": ["group:operator"], "dst": ["tag:beastie:8443"]}
    ],
    "groups": {"group:operator": ["operator@example.com"]},
    "tagOwners": {"tag:beastie": ["operator@example.com"]}
  }
  ```
  Tag Beastie `tag:beastie` at `tailscale up` time (`--advertise-tags=tag:beastie`, tag owner pre-declared in the ACL). Port 8443 is a placeholder for the future web UI — update to the real port when it ships. No other device, port, or subnet route is granted.

## 3. What does NOT change

- Postgres stays bound to loopback (`127.0.0.1`) only — the tailnet interface is not a Postgres listener under any circumstance covered by this spec.
- No compose service binds to the tailnet interface without a separate, explicit decision and a compose-profile change; the web UI, when it lands, is reverse-proxied or port-forwarded deliberately, never auto-exposed by virtue of the tailnet existing.
- The tailnet is **transport, not trust**. Reaching Beastie's IP over the tailnet grants network path only; sshd still requires a registered public key. No trust decision is delegated to Tailscale ACLs alone.

## 4. Failure modes + mitigations during the absence

| Failure | Mitigation |
|---|---|
| `tailscaled` crashes | systemd unit ships with `Restart=always`; add a user-level watchdog timer (`systemctl status tailscaled` check every 15 min, log-only — no auto-remediation beyond systemd's own restart) |
| Tailscale control-plane outage | Realistic options, in order of practicality: (1) do nothing — control-plane outages block *new* device auth/coordination changes, not already-established peer connectivity, so existing SSH access likely survives; (2) keep a second LAN device (e.g. a Raspberry Pi or spare laptop already on the tailnet) as a backdoor — SSH to it, then SSH from it to Beastie over the LAN if the tailnet itself degrades; (3) a direct WireGuard peer as true break-glass is available but not pre-provisioned for this trip — documented as an option, not built, given low probability and the LAN-backdoor device covering the same gap more cheaply |
| Tailscale auth-key/node-key expiry | Disabled per §2; verify in admin console before departure |
| Beastie reboot (planned or crash) | `sudo loginctl enable-linger <deployuser>` so user-level systemd units (compose stack, backup timers) start without login; `tailscaled` and `sshd` are system services, already boot-enabled |
| Power loss | Enable "restore last power state to ON" in BIOS/UEFI so Beastie repowers after an outage. Wake-on-LAN is not usable remotely (no LAN-local device to send the magic packet) — **flagged as residual risk**: a power loss with BIOS misconfigured, or a hardware fault, is not remotely recoverable this trip. No mitigation beyond the BIOS setting and accepting the risk. |

## 5. Deployment-spec compliance

- **Host-facts table** (deployments record) gets: tailnet name, Beastie's node name (`beastie`), and a pointer to the ACL policy (e.g. the `acls.json` commit/version in the Tailscale admin console) — never keys, never the node key, never the auth key.
- **Environment-change rule (§5):** the tailnet interface appearing on Beastie is a network-position change. Re-run the structural security checks before agents resume or continue: tier-routing fact and the credential-scope scan (every container's environment vs `secrets-manifest.yaml`). Record the re-run in the deployments record alongside the host-facts update.
- **Secrets handling:** the Tailscale auth key (if used instead of interactive browser login) is one-time-use — consumed at `tailscale up`, then irrelevant. Post-auth, the durable credential is the node's local Tailscale state (`/var/lib/tailscale/`, root-owned, mode 0700 by default), which is device state analogous to an SSH host key, **not** a deployment secret. It does not go in `~/.config/omegahive/secrets/`, is not named in `secrets-manifest.yaml`, and is not covered by the credential-scope scan (it's host-level, not container-level). SSH access still relies solely on the operator's personal SSH keypair — `authorized_keys` on Beastie, private key stays with the operator, never stored on Beastie or in any repo.

## 6. Test checklist before departure

From a phone hotspot (off-LAN, to prove no LAN-dependence):

- [ ] `ssh <deployuser>@beastie.<tailnet-name>.ts.net` succeeds with key auth; password auth confirmed rejected
- [ ] `git push`/`git pull` against a repo hosted on Beastie succeeds over the SSH transport above
- [ ] Reach the web-UI port (or a `python -m http.server 8443` placeholder) at `beastie.<tailnet-name>.ts.net:8443`
- [ ] Confirm Postgres is **not** reachable over the tailnet: `nc -zv beastie.<tailnet-name>.ts.net 5432` fails/times out
- [ ] Reboot Beastie remotely (`sudo reboot` over SSH); confirm tailnet reconnects, sshd comes back, compose stack (linger-enabled) restarts, backup timers are active (`systemctl --user list-timers`)
- [ ] Kill `tailscaled` (`sudo systemctl kill tailscaled`) and confirm systemd restarts it within the unit's restart window; confirm SSH session survives or reconnects
