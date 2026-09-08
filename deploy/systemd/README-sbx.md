# The sandbox runtime, and the credential that is not in a file

Every route whose `runner.executable` is `sbx` builds a microVM with `sbx create` — ask
`hive-routes` which ones those are rather than trusting a count here. Two host facts make that fragile in ways nothing else here is,
and both cost a full outage on 2026-09-08.

## 1. Supervise the daemon

`sbx daemon start` was a hand-run process. Install `sbx-daemon.service` so it survives a
reboot, and enable lingering so it starts without a login session:

```
systemctl --user enable --now sbx-daemon.service
loginctl enable-linger "$USER"          # once per host
```

`loginctl enable-linger` is the half that is easy to miss: without it a user unit stops
when the last session closes, which on a box reached only over SSH means the sandboxed
routes work while you are logged in and not otherwise.

## 2. The Docker Hub credential, and the keyring that can hijack it

`sbx` stores its Docker Hub session through the OS Secret Service **when it detects one**.
When it detects none it falls back to a file, protected by permissions rather than a
password, and says so:

```
No keychain detected - this secret will be stored on disk, protected by file permissions
```

That fallback is what a headless host wants, and the trap is that a keyring can appear and
take precedence at any moment. `gnome-keyring-daemon` is D-Bus-activatable: any process
that asks for a secret starts it, and it then stays resident under `systemd --user`. Once
it is up, sbx detects a keychain — and on a headless host that keyring's `login`
collection is locked, with nothing to unlock it. Then:

- `sbx login` completes the browser step and fails at **saving** the credential;
- nothing lands on disk, so nothing can be refreshed later;
- every subsequent `sbx` call tries the refresh, takes a cross-process lock and never
  releases it — `could not acquire docker hub refresh lock` on *every* command, including
  ones needing no authentication;
- and because a resident daemon offers a prompt nobody can answer, calls **hang** rather
  than erroring. With no daemon resident the same locked write fails immediately instead.

This cost a full outage on 2026-09-08: all five sandboxed routes unlaunchable, with the
CLI wedged rather than complaining. The recovery was to stop the resident daemon and log
in again, at which point sbx found no keychain and wrote the credential to disk.

Diagnose the keyring directly:

```
printf x | timeout 10 secret-tool store --label=probe probe probe; echo "exit=$?"
# exit 0 -> usable      exit 1 -> locked, failing fast      exit 124 -> locked, hanging
pgrep -a gnome-keyring    # resident? then sbx will prefer it over the file
```

**Operationally:** on a headless host, keep the credential on the file path. Routine
launches never touch it, so a resident keyring daemon is harmless day to day — but the next
`sbx login`, whenever that session expires, will hang if one is resident. Stop it first.
A host that would rather have the keyring must unlock it at boot instead; either way,
record the choice in this host's row in `docs/deployments/`, and rely on
`deploy_checks.sh` check 9 to say when it stops being true.
