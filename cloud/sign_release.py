#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sign a Kindle self-update release (Ed25519) and write the manifest.

Muse-side only — Ken's Mac is never involved in signing:
  * Private key lives ONLY on the Muse VM at
    ~/.config/kindle-signing/ed25519_private.key (mode 0600). It is never
    committed to the repo, never uploaded to Oracle, never sent anywhere,
    and this script never prints it.
  * The public key is baked into kindle/f1dash.py (SIGNING_PUBLIC_KEY_HEX).
    This script refuses to sign if the key on disk does not match the
    baked-in public key — signing with the wrong key would publish a
    release the Kindle rejects (bad_signature) on every cycle.
  * Output is the manifest the Kindle fetches: f1dash.version.json with
    sha256 + signature (base64 over the raw file bytes) + version/commit
    + key fingerprint. The Kindle verifies the signature BEFORE the sha
    check (see _check_self_update in f1dash.py).

Usage (from the repo root, as deploy.sh does):
    python3 cloud/sign_release.py --in kindle/f1dash.py \
        --out /tmp/f1dash.version.json [--version <git rev>]

Lost-key warning: if the private key file is lost, no future release can
be signed — the Kindle will refuse every unsigned/new-key release with
bad_signature. Recovery is a manual reinstall with a new public key baked
into the code (scp from the home LAN, same as the first install). Back up
~/.config/kindle-signing/ somewhere safe, offline, outside this VM.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ed25519  # noqa: E402  (pure-python, stdlib only)

DEFAULT_KEY = os.path.expanduser("~/.config/kindle-signing/ed25519_private.key")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git_rev():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
            stderr=subprocess.DEVNULL, timeout=10).decode().strip()
    except Exception:
        return "unknown"


def main():
    ap = argparse.ArgumentParser(description="Sign a Kindle self-update release.")
    ap.add_argument("--in", dest="src", default=os.path.join(REPO_ROOT, "kindle", "f1dash.py"))
    ap.add_argument("--out", dest="out", required=True,
                    help="where to write the manifest JSON (e.g. /tmp/f1dash.version.json)")
    ap.add_argument("--version", default=None, help="release version (default: git HEAD)")
    ap.add_argument("--key", default=os.environ.get("KINDLE_SIGNING_KEY", DEFAULT_KEY))
    args = ap.parse_args()

    if not os.path.exists(args.key):
        print("ERROR: signing key not found at %s" % args.key, file=sys.stderr)
        print("Releases are signed on the Muse VM only — the private key lives "
              "there and nowhere else. Do not run the publishing step from a "
              "Mac; it has no key and must not get one.", file=sys.stderr)
        return 1
    try:
        seed = bytes.fromhex(open(args.key).read().strip())
    except Exception as exc:
        print("ERROR: cannot read signing key: %r" % exc, file=sys.stderr)
        return 1
    if len(seed) != 32:
        print("ERROR: signing key is not a 32-byte seed", file=sys.stderr)
        return 1

    with open(args.src, "rb") as f:
        blob = f.read()
    src_text = blob.decode("utf-8", "replace")
    m = re.search(r'SIGNING_PUBLIC_KEY_HEX\s*=\s*"([0-9a-fA-F]{64})"', src_text)
    if not m:
        print("ERROR: no SIGNING_PUBLIC_KEY_HEX in %s" % args.src, file=sys.stderr)
        return 1
    baked_pubkey = bytes.fromhex(m.group(1))
    pubkey = ed25519.publickey(seed)
    if pubkey != baked_pubkey:
        print("ERROR: signing key does not match the public key baked into %s" % args.src,
              file=sys.stderr)
        print("Refusing to sign — a release signed with this key would be "
              "rejected by the Kindle as bad_signature.", file=sys.stderr)
        return 1

    sha = hashlib.sha256(blob).hexdigest()
    sig = ed25519.signature(blob, seed, pubkey)
    fingerprint = hashlib.sha256(pubkey).hexdigest()[:16]
    manifest = {
        "sha256": sha,
        "signature": base64.b64encode(sig).decode("ascii"),
        "signature_alg": "ed25519",
        "key_fingerprint": fingerprint,
        "version": args.version or _git_rev(),
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S+08:00",
                                 time.gmtime(time.time() + 8 * 3600)),
    }
    with open(args.out, "w") as f:
        json.dump(manifest, f)
    # Never print the private key — only the fingerprint and hashes.
    print("signed: sha256=%s... version=%s fingerprint=%s" % (sha[:12], manifest["version"], fingerprint))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
