# Council room deployment (Beastie)

The council room is an OpenClaw gateway that puts council seats into Slack threads. It has two seats, from two vendors:

- **Astra:** `openai/gpt-6-astra` at high thinking, on OpenClaw's native Codex runtime, signed in with a ChatGPT subscription.
- **Fable:** `anthropic/claude-fable-5-2` at high thinking, on OpenClaw's Anthropic provider, with a Claude subscription setup token.

Each seat has its own Slack app and its own auth profile. Both work in one shared clone of the hive workspace, which Cassio also edits from the laptop. The room is separate from hive core: it has no spine authority, no hive writer credentials and no task wrappers. It does not touch the notifier, the old gateway or the workers.

Everything here runs as Cassio's user. The configured boundary is the room's own environment, config and credential store. That is **not** isolation from a hostile agent: Codex runs with full access, and its subprocesses see the normal `HOME`, including Cassio's files and Git remotes.

## Layout

| Path | Mode | Holds |
|---|---|---|
| `~/.local/share/omegahive/council-room/` (`$R`) | 0700 | runtime root; the unit's working directory |
| `$R/runtime/node` → `node-v24.22.2` | | room-local Node (OpenClaw needs ≥24.16; the system Node is not used) |
| `$R/runtime/openclaw/` | | npm prefix holding `openclaw@2026.11.3`, exact `package-lock.json` |
| `$R/state/` | 0700 | `OPENCLAW_STATE_DIR`: sessions, plugin installs, one auth store per seat (`agents/<seat>/agent/openclaw-agent.sqlite`) |
| `$R/state/agents/<seat>/workspace/` | | the seat's long-term memory (`MEMORY.md`, `memory/*.md`); not the record, and not backed up |
| `$R/bin/openclaw-room` | 0755 | CLI wrapper: `env -i` with only the room's variables, so no API key from the caller's shell reaches OpenClaw |
| `$R/bin/set-slack-tokens` | 0755 | stores one seat's Slack tokens from hidden prompts (`set-slack-tokens [astra\|fable]`) |
| `~/.config/omegahive/council-room/openclaw.json` | 0600 | `OPENCLAW_CONFIG_PATH` (repo copy: `openclaw.json`; OpenClaw adds a `meta` block) |
| `~/.config/omegahive/council-room/secrets.json` | 0600, 1 link | file SecretRef provider `room`: `/gateway/token`; Astra's `/slack/botToken`, `/slack/appToken`; Fable's `/slack/fable/botToken`, `/slack/fable/appToken` |
| `~/.config/systemd/user/omegahive-council.service` | | the gateway unit (repo: `systemd/omegahive-council.service`) |
| `~/workspaces/hive-council` | | the shared council clone of `~/repos/hive-workspace.git`; both seats' working directory |

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
6. **Plugins:** `$R/bin/openclaw-room plugins install @openclaw/slack@2026.11.3 --pin`, then the same for `@openclaw/codex@2026.11.3`. Install records are kept in the state database, not in the config. The Anthropic provider is bundled with OpenClaw; the config enables it.
7. **Clone:**
   - `git clone ~/repos/hive-workspace.git ~/workspaces/hive-council`.
   - Add `._*` and `.DS_Store` to the clone's `.git/info/exclude`. A save from the laptop mount writes macOS metadata files next to the edited file, and those must never be committed.
   - The seat briefs are `projects/omegahive/council/astra.md` and `fable.md`. Put a copy there only if the published workspace does not have it yet, and check its hash against the source.
8. **Unit:** `install -m 0644 systemd/omegahive-council.service ~/.config/systemd/user/`, then `systemctl --user daemon-reload`.
9. Check with `$R/bin/openclaw-room config validate`.

## Credentials (Cassio, in their own terminal)

Tokens never pass through Slack, chat, a command line, the environment or a report.

- **Slack apps, one per seat:**
  1. In api.slack.com/apps, create the app from its manifest in the AI Artificery workspace: `slack-app-manifest.json` (Astra) or `slack-app-manifest-fable.json` (Fable).
  2. Under Basic Information → App-Level Tokens, generate a token with `connections:write`.
  3. Install the app to the workspace.
  4. Run `$R/bin/set-slack-tokens astra` or `$R/bin/set-slack-tokens fable`.
  5. Invite the bot into every room channel. Both seats are in all of them.

  The apps are separate from the notifier's ("OmegaHive Machinist Probe"), which this deployment never touches.
- **ChatGPT login:** run `$R/bin/openclaw-room models auth login --provider openai --method device-code --agent astra --profile-id openai:astra`.
  - This is a fresh device-code login, owned by OpenClaw and stored in Astra's agent auth store.
  - `~/.codex` is untouched, and the two logins share no refresh token.
  - To check it, run `models auth list --agent astra`. It must show `openai:astra [openai/oauth]` and no API-key profile.
- **Claude setup token (Fable):** this step needs the installed config to include the `fable` agent.
  1. On any machine signed in to Claude Code with the Claude subscription, run `claude setup-token`. It prints a long-lived `sk-ant-oat01-…` token.
  2. On Beastie, run `$R/bin/openclaw-room models auth paste-token --provider anthropic --agent fable --profile-id anthropic:fable` and paste the token at its prompt.
  - The token lives only in Fable's agent auth store. Other Claude logins on the host, including `~/.claude`, are untouched.
  - To check it, run `models auth list --agent fable`. It must show `anthropic:fable` as a token profile and no API-key profile.
  - The token expires and can be revoked. If Fable starts failing with 401s, create a new token and paste it again.

## API keys stay out

These rules cover both seats.

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

`secrets.json` is deliberately not archived. After a restore, re-enter both seats' tokens with `set-slack-tokens` and generate a new gateway token. The state directory is not archived either. After a restore:

- Astra needs a fresh login, and Fable a fresh setup token.
- Sessions start empty, and Slack history is refetched.
- Long-term memory starts empty. It is a convenience, and the record lives in the workspace.

## Speaking rules

Each seat has a Slack account, `channels.slack.accounts.astra` or `.fable`. Each account lists every room channel under `channels`, and a binding sends the account's messages to the agent of the same name. A channel has one **author** seat and one **reviewer** seat. The seat briefs carry the same table.

| Channel | ID | Author | Reviewer |
|---|---|---|---|
| `#agi-maze-poc` | `C0CGP5HA34Z` | Fable | Astra |
| `#pln-benchmarks` | `C0CG5SZGH3R` | Astra | Fable |
| `#omegahive` | `C0CHG32UGUT` | Astra | Fable |
| `#hive-council` | `C0CHFKHH89W` | Astra | Fable |

Seat user IDs: Astra `U0CGK1J7S5C`, Fable `U0CJGLKQFG8`; Cassio is `U0C2206GLEA`.

These rules are meant to be tuned as the room is used. Each behaviour has one key:

| Behaviour | Key | Set to |
|---|---|---|
| The author answers unaddressed messages | `accounts.<author>.channels.<id>.requireMention` | `false` (reviewer: `true`) |
| A message mentioning only the other seat does not wake the author | `accounts.<author>.channels.<id>.ignoreOtherMentions` | `true` |
| The other seat's bot messages wake a seat only when they mention it | `channels.slack.allowBots`, and per channel | `"mentions"` |
| Which bot or human may wake a seat in a channel | `accounts.<seat>.channels.<id>.users` | Cassio and the other seat |
| A seat that spoke in a thread is not woken by later unaddressed messages | `channels.slack.implicitMentions` | all three flags `false` |
| Runaway bot exchanges stop | `channels.slack.botLoopProtection` | 8 messages per 600 s, then 600 s cooldown |

To change who authors a channel, swap that channel's `requireMention` and `ignoreOtherMentions` between the two accounts, then update the tables in both seat briefs. Mention rules, sender lists and bot admission apply to new messages without reconnecting Slack. Adding or removing a channel or an account restarts the Slack connection inside the running gateway. Edit both config copies, as under "Add a project channel", run `config validate`, and commit the repo copy.

## Activate

```
systemctl --user enable --now omegahive-council.service
journalctl --user -u omegahive-council.service -f
```

Enable and start only this unit. Lingering is on, so the gateway starts at boot without a login.

**Adding the second seat to a running room.** Fable's Slack tokens must already be stored with `set-slack-tokens fable`.

1. Save the running config: `cp -p ~/.config/omegahive/council-room/openclaw.json ~/.config/omegahive/council-room/openclaw.json.pre-m1.2`. Seat rollback restores this file.
2. Install the repo's `openclaw.json` with the installed copy's `meta` block, through a new 0600 file and `mv`.
3. Run `$R/bin/openclaw-room config validate`.
4. Store Fable's setup token, as described under Credentials.
5. Run `systemctl --user restart omegahive-council.service`, then check `$R/bin/openclaw-room agents list --bindings` and `models status --agent fable`.

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

A project channel is named after its directory: `#<name>` is about `projects/<name>/` in the council clone, as the seat briefs state. `#omegahive` covers building the hive, and `#hive-council` hive-wide judgments. The gateway answers only in channels listed in its config, and every new channel needs an explicit author.

1. Cassio creates `#<name>` in Slack, runs `/invite @Astra` and `/invite @Fable` there, and names the author.
2. Look up the channel ID with a seat's bot token, read from `secrets.json` and sent as a header on standard input (`curl -H @-`), never on the command line. Use `conversations.list?types=public_channel,private_channel`, and check that `is_member` is true.
3. Under each account's `channels`, copy an existing entry in which that seat has the same role, author or reviewer, to the new ID. Do this in both `openclaw.json` (repo) and `~/.config/omegahive/council-room/openclaw.json`. Write the installed copy through a new 0600 file and `mv`, and keep OpenClaw's `meta` block.
4. Add the channel to the roles table above and in both seat briefs.
5. Run `$R/bin/openclaw-room config validate`, then `systemctl --user restart omegahive-council.service`. Commit the repo copy.

## Roll back

**Fable only (the normal seat rollback).** This leaves the room running as the Astra-only room:

1. Remove Fable's auth profile while the `fable` agent is still configured: `$R/bin/openclaw-room models auth logout --agent fable anthropic:fable`. A failed route-A profile is removed the same way; it is never kept as a fallback.
2. Install the saved Astra-only config, `~/.config/omegahive/council-room/openclaw.json.pre-m1.2`, through a new 0600 file and `mv`.
3. Restart the unit.

Astra's Slack account, channels and login are unchanged throughout. Keep Fable's Slack app, its tokens in `secrets.json`, its state directory, the drafts and the evidence. Do not delete channels.

**The whole room:**

```
systemctl --user disable --now omegahive-council.service
```

Then unmount the laptop path. Keep `$R`, the config, the clone (with its drafts) and the evidence. Do not delete channels or messages, revoke shared credentials, or reset the clone.

To undo the backup extension, remove the drop-in and run `daemon-reload`. A script with no sources behaves exactly like the previous one. The previous live copy was kept as `$R/omegahive-git-bundle.pre-m1.1`.
