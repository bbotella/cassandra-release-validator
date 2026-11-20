# Signing release artifacts from the release container

The release container builds and packages artifacts on Linux, but your signing key stays on your Mac.
The container talks to your Mac's gpg-agent through its restricted *extra* socket, which supports
signing but refuses key export and import.

## How it works

```text
container                              podman VM (gvproxy)                  your Mac
gpg / rpmsign / debsign / gradle
   │ ~/.gnupg/S.gpg-agent (Unix)
   └─ socat UNIX-LISTEN → TCP host.containers.internal:<port> ─────► relay on 127.0.0.1:<port>
                                                                       └─► ~/.gnupg/S.gpg-agent.extra
```

- `cut-release` starts the relay (a few lines of Python, `crv/release/signing.py`) only for the steps
  that sign, on a random port bound to `127.0.0.1`, and stops it right after.
- The container imports only your **public** key. Every signature is made by the agent on your Mac,
  and a passphrase is asked by your Mac's pinentry.

## Why not SSH forwarding or a shared socket

Both were tried (spike, 2026-11-20, podman 6.2 applehv):

- Forwarding the socket into the VM with `ssh -R` needs SSH into the VM. Key login to this VM is
  refused, even for `podman machine ssh`, while podman itself works through its API socket.
- A Unix socket in a directory shared into the VM can't be connected to: virtiofs answers
  "Operation not supported".

The TCP relay worked:

- container gpg 2.2 talked to the Mac's agent 2.5.24;
- detached (`--armor --detach-sign`) and clear-signed signatures verified on the Mac;
- `rpmsign --addsign` produced an RPM that `rpm -K` reports as `Header V4 RSA/SHA512 Signature: OK`.

These are the same gpg operations Gradle's signing, `debsign`, `reprepro` and `rpmsign` use. `debsign`
itself and Gradle signing are exercised by the rehearsal runs.

## The trade-off

While a signing step runs, any process on your Mac could connect to the relay port and ask the agent
to sign. The extra socket still refuses key export. With a passphrase-protected key, the agent asks
in a pinentry dialog before using it (or uses its cache). Keep `default-cache-ttl` short if that
concerns you.

## What a real release needs on this Mac

- **Your release key** (the one in KEYS) in your keyring here, or on a smartcard the agent can use.
  `preflight` checks that the key given with `--signing-key` (or `$gpg_key`) has a secret part here.
- **`pinentry-mac`**, so the agent can ask for the passphrase while no terminal is involved:

  ```bash
  brew install pinentry-mac
  echo "pinentry-program $(brew --prefix)/bin/pinentry-mac" >> ~/.gnupg/gpg-agent.conf
  gpgconf --kill gpg-agent
  ```

- **`svn`**, for the dist steps (`brew install subversion`). Authenticate once yourself so svn caches the
  credentials in the macOS keychain. `cut-release preflight` prints the command.

`cut-release preflight` checks all three.
