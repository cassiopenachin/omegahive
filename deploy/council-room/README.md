# Council room deployment (Beastie)

The council room is an OpenClaw gateway that puts council seats into Slack threads. Its first seat is **Astra**: `openai/gpt-6-astra` at high thinking, on OpenClaw's native Codex runtime and signed in with a ChatGPT subscription. Astra works in one shared clone of the hive workspace, which Cassio also edits from the laptop. The room is separate from hive core: it has no spine authority, no hive writer credentials and no task wrappers. It does not touch the notifier, the old gateway or the workers.

Everything here runs as Cassio's user. The configured boundary is the room's own environment, config and credential store. That is **not** isolation from a hostile agent: Codex runs with full access, and its subprocesses see the normal `HOME`, including Cassio's files and Git remotes.

## Layout

| Path | Mode | Holds |
|---|---|---|
| `~/.local/share/omegahive/council-room/` (`$R`) | 0700 | runtime root; the unit's working directory |
| `$R/runtime/node` → `node-v24.22.2` | | room-local Node (OpenClaw needs ≥24.16; the system Node is not used) |
| `$R/runtime/openclaw/` | | npm prefix holding `openclaw@2026.11.3`, exact `package-lock.json` |
| `$R/state/` | 0700 | `OPENCLAW_STATE_DIR`: sessions, plugin installs, agent auth store (`agents/astra/agent/openclaw-agent.sqlite`) |
| `$R/bin/openclaw-room` | 0755 | CLI wrapper: `env -i` with only the room's variables, so no API key from the caller's shell reaches OpenClaw |
| `$R/bin/set-slack-tokens` | 0755 | stores the Slack tokens from hidden prompts |
| `~/.config/omegahive/council-room/openclaw.json` | 0600 | `OPENCLAW_CONFIG_PATH` (repo copy: `openclaw.json`; OpenClaw adds a `meta` block) |
| `~/.config/omegahive/council-room/secrets.json` | 0600, 1 link | file SecretRef provider `room`: `/gateway/token`, `/slack/botToken`, `/slack/appToken` |
| `~/.config/systemd/user/omegahive-council.service` | | the gateway unit (repo: `systemd/omegahive-council.service`) |
| `~/workspaces/hive-council` | | the shared council clone of `~/repos/hive-workspace.git`; Astra's working directory |

The gateway listens on loopback port 18795, using token auth with Tailscale off.

## Install

1. **Roots:** `install -d -m 0700 ~/.local/share/omegahive ~/.local/share/omegahive/council-room{,/runtime,/state,/bin} ~/.config/omegahive/council-room`.
2. **Node:**
   - Download `node-v24.22.2-linux-x64.tar.xz` and `SHASUMS256.txt` from `nodejs.org/dist/v24.22.2/`.
   - Check it with `sha256sum -c --ignore-missing`.
   - Unpack into `$R/runtime/` and `ln -s node-v24.22.2-linux-x64 $R/runtime/node`.
3. **Wrappers:** `install -m 0755 bin/openclaw-room bin/set-slack-tokens $R/bin/`.
4. **OpenClaw:** run `npm install --prefix $R/runtime/openclaw openclaw@2026.11.3` with the room's Node first on `PATH` and with `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and `CODEX_API_KEY` unset.
5. **Config and secrets:**
   - `install -m 0600 openclaw.json ~/.config/omegahive/council-room/`.
   - Create `secrets.json` (0600) as `{"gateway":{"token":"<64 hex from openssl rand -hex 32>"},"slack":{"botToken":"","appToken":""}}`. The file provider refuses a group- or world-readable file, and one with more than one hard link.
6. **Plugins:** `$R/bin/openclaw-room plugins install @openclaw/slack@2026.11.3 --pin`, then the same for `@openclaw/codex@2026.11.3`. Install records are kept in the state database, not in the config.
7. **Clone:** `git clone ~/repos/hive-workspace.git ~/workspaces/hive-council`. Put the seat brief at `projects/omegahive/council/astra.md` if the published workspace does not have it yet.
8. **Unit:** `install -m 0644 systemd/omegahive-council.service ~/.config/systemd/user/`, then `systemctl --user daemon-reload`.
9. Check with `$R/bin/openclaw-room config validate`.

## Credentials (Cassio, in their own terminal)

Tokens never pass through Slack, chat, a command line, the environment or a report.

- **Slack app:**
  1. In api.slack.com/apps, create the app from `slack-app-manifest.json` in the AI Artificery workspace.
  2. Under Basic Information → App-Level Tokens, generate a token with `connections:write`.
  3. Install the app to the workspace.
  4. Run `$R/bin/set-slack-tokens`.
  5. Invite the bot into each room channel.
  6. Add each channel's ID under `channels.slack.channels` in both config copies.

  The app is separate from the notifier's ("OmegaHive Machinist Probe"), which this deployment never touches.
- **ChatGPT login:** run `$R/bin/openclaw-room models auth login --provider openai --method device-code --agent astra --profile-id openai:astra`.
  - This is a fresh device-code login, owned by OpenClaw and stored in Astra's agent auth store.
  - `~/.codex` is untouched, and the two logins share no refresh token.
  - To check it, run `models auth list --agent astra`. It must show `openai:astra [openai/oauth]` and no API-key profile.

## API keys stay out

- The unit has `UnsetEnvironment=OPENAI_API_KEY ANTHROPIC_API_KEY CODEX_API_KEY`.
- `openclaw-room` starts from `env -i`.
- The config sets `env.shellEnv.enabled: false`, and the Codex plugin's `appServer.clearEnv` lists all three names.
- With a subscription profile, OpenClaw also strips `OPENAI_API_KEY` and `CODEX_API_KEY` from the Codex child.
- No `.env` is read: none exists in the working directory or in `$OPENCLAW_STATE_DIR`.
- To verify a running process, print variable **names** only. Never print values:

```
tr '\0' '\n' < /proc/<pid>/environ | cut -d= -f1 | grep -E 'API_KEY|TOKEN|SECRET' || echo none
```

## Backup

The daily `omegahive-bundle.service` (03:15) runs `~/.local/bin/omegahive-git-bundle`, installed from `deploy/git_bundle.sh`.

The drop-in `systemd/omegahive-bundle.service.d/council-room.conf` goes in `~/.config/systemd/user/omegahive-bundle.service.d/`. It sets `OMEGAHIVE_ARCHIVE_SOURCES` to the council clone and to the room config **file**. Each run then also writes `~/omegahive-backups/files-<ts>.tar.gz` (0600, keeping the same 14 as the bundles).

`secrets.json` is deliberately not archived. After a restore, re-enter the tokens with `set-slack-tokens` and generate a new gateway token. The state directory is not archived either: after a restore, Astra needs a fresh login, and sessions start empty, with Slack history refetched.

## Activate

```
systemctl --user enable --now omegahive-council.service
journalctl --user -u omegahive-council.service -f
```

Enable and start only this unit. Lingering is on, so the gateway starts at boot without a login.

## Laptop mount

Cassio edits the council clone from the laptop (macOS 27, Apple Silicon) through sshfs on FUSE-T. FUSE-T needs no kernel extension, so there is no Recovery-mode security change and no reboot. The `.pkg` asks for the admin password once. `<beastie>` is Beastie's tailnet host name, the one the laptop already uses for SSH.

Install once:

```
brew install macos-fuse-t/homebrew-cask/fuse-t
brew install macos-fuse-t/homebrew-cask/sshfs-fuse-t
mkdir -p /Users/cassio/src/SNET/hive-council
```

Mount (repeat after a laptop restart or a Beastie reboot; nothing mounts at boot):

```
sshfs <beastie>:/home/cassio/workspaces/hive-council /Users/cassio/src/SNET/hive-council \
  -o volname=hive-council,reconnect,ServerAliveInterval=15,ServerAliveCountMax=3
```

Unmount with `umount /Users/cassio/src/SNET/hive-council`, or `diskutil unmount force` on the same path if it is busy. If a terminal gets "Operation not permitted" inside the mount, allow it under System Settings → Privacy & Security → Files and Folders → Network Volumes.

A file at `/home/cassio/workspaces/hive-council/<path>` on Beastie is `/Users/cassio/src/SNET/hive-council/<path>` on the laptop: the same bytes, with no copy or sync in between.

## Add a project channel

A project channel is named after its directory: `#<name>` is about `projects/<name>/` in the council clone, as the seat brief states. `#hive-council` covers the whole hive. The gateway answers only in channels listed in its config.

1. Cassio creates `#<name>` in Slack and runs `/invite @Astra` there.
2. Look up the channel ID with Astra's bot token, read from `secrets.json` and sent as a header on standard input (`curl -H @-`), never on the command line. Use `conversations.list?types=public_channel,private_channel`, and check that `is_member` is true.
3. Copy an existing entry under `channels.slack.channels` to the new ID, in both `openclaw.json` (repo) and `~/.config/omegahive/council-room/openclaw.json`. Write the installed copy through a new 0600 file and `mv`, and keep OpenClaw's `meta` block.
4. Run `$R/bin/openclaw-room config validate`, then `systemctl --user restart omegahive-council.service`. Commit the repo copy.

## Roll back

```
systemctl --user disable --now omegahive-council.service
```

Then unmount the laptop path. Keep `$R`, the config, the clone (with its drafts) and the evidence. Do not delete channels or messages, revoke shared credentials, or reset the clone.

To undo the backup extension, remove the drop-in and run `daemon-reload`. A script with no sources behaves exactly like the previous one. The previous live copy was kept as `$R/omegahive-git-bundle.pre-m1.1`.
