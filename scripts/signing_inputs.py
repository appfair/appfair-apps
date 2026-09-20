"""Adapt the signing stage's inputs, whichever secret carries them.

The organization holds some of its keys as bundles — a Java properties file with the keystore's
alias and passwords, a fastlane key file with the App Store Connect id, issuer and key — and the
queue also takes each field on its own. This reads whichever is present, with an explicit field
winning over the bundle that duplicates it, and hands the signing steps one set of names.

No secret value is returned as an action output or printed except through GitHub's masking
commands. Decoded private keys are written under RUNNER_TEMP, readable only by this job.
"""
import base64
import json
import os
import re
import sys
from pathlib import Path


def decoded(value):
    return base64.b64decode(re.sub(r"\s+", "", value), validate=True)


def properties(text):
    """The Java properties format the base64 keystore-properties secret is written in."""
    def unescape(value):
        def replacement(match):
            if match[1]:
                return chr(int(match[1], 16))
            return {"n": "\n", "r": "\r", "t": "\t", "f": "\f"}.get(match[2], match[2])
        return re.sub(r"\\(?:u([0-9a-fA-F]{4})|(.))", replacement, value)

    result = {}
    pending = ""
    for raw in text.splitlines():
        line = pending + raw.lstrip()
        if (len(line) - len(line.rstrip("\\"))) % 2:
            pending = line[:-1]
            continue
        pending = ""
        if not line or line.startswith(("#", "!")):
            continue
        match = re.fullmatch(r"((?:\\.|[^=:\s])+)(?:\s*[:=]\s*|\s+)(.*)", line)
        if match:
            result[unescape(match[1])] = unescape(match[2])
    return result


def prepare(values, directory):
    """Return environment entries, writing any decoded private files with mode 0600."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    result = {}

    def private_file(name, data):
        path = directory / name
        # Set the mode on creation, not after a potentially readable write.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
        return str(path)

    props = {}
    if values.get("KEYSTORE_PROPERTIES_B64") and not all(
        values.get(k) for k in ["KEY_ALIAS", "STORE_PASS", "KEY_PASS"]
    ):
        props = properties(decoded(values["KEYSTORE_PROPERTIES_B64"]).decode("latin-1"))
    for input_name, property_name, output in [
        ("KEY_ALIAS", "keyAlias", "APPFAIR_KEY_ALIAS"),
        ("STORE_PASS", "storePassword", "DAY_SIGN_STORE_PASS"),
        ("KEY_PASS", "keyPassword", "DAY_SIGN_KEY_PASS"),
    ]:
        result[output] = values.get(input_name) or props.get(property_name, "")

    if values.get("PLAY_KEY"):
        text = values["PLAY_KEY"].strip()
        data = text.encode() if text.startswith("{") else decoded(text)
        if not isinstance(json.loads(data), dict):
            raise ValueError("invalid Play JSON key")
        result["APPFAIR_PLAY_KEY_PATH"] = private_file("play.json", data)

    api = {}
    if values.get("ASC_API_KEY_B64") and not all(
        values.get(k) for k in ["ASC_KEY_ID", "ASC_ISSUER", "ASC_KEY_B64"]
    ):
        api = json.loads(decoded(values["ASC_API_KEY_B64"]))
    result["APPFAIR_ASC_KEY_ID"] = values.get("ASC_KEY_ID") or api.get("key_id", "")
    result["APPFAIR_ASC_ISSUER"] = values.get("ASC_ISSUER") or api.get("issuer_id", "")
    key = values.get("ASC_KEY_B64")
    if key:
        data = decoded(key)
    elif api.get("key"):
        data = api["key"].encode()
        if api.get("is_key_content_base64"):
            data = decoded(api["key"])
    else:
        data = None
    if data:
        if b"-----BEGIN PRIVATE KEY-----" not in data:
            raise ValueError("invalid App Store Connect private key")
        result["APPFAIR_ASC_KEY_PATH"] = private_file("asc.p8", data)
    return result


def main():
    try:
        values = prepare(os.environ, Path(os.environ["RUNNER_TEMP"]) / "appfair-signing")
        # GitHub's multiline environment syntax also supports passwords with reserved characters.
        import uuid
        with open(os.environ["GITHUB_ENV"], "a") as env:
            for name, value in values.items():
                for line in value.splitlines():
                    escaped = line.replace("%", "%25").replace("\r", "%0D")
                    print(f"::add-mask::{escaped}")
                marker = f"APPFAIR_{uuid.uuid4().hex}"
                env.write(f"{name}<<{marker}\n{value}\n{marker}\n")
        return 0
    except Exception:
        # Do not interpolate a parsing error: it can contain part of the secret being parsed.
        print("::error::invalid signing input: check the documented JSON/base64/properties formats")
        return 1


if __name__ == "__main__":
    sys.exit(main())
