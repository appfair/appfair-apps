"""Compare package payloads across declared Day identities, without executing app code.

Only host packaging metadata is normalized. Native libraries, DEX, permissions, components,
non-icon assets and other resources still have to match. Tools must be present when a container
needs decoding: silently excluding a whole resource table or asset catalog would hide code/data.
"""
import hashlib
import json
import plistlib
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path


def identity(metadata, target):
    project = metadata["project"]
    return {**project, **project.get("resolved", {}).get(target, {})}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def run_tool(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise ValueError(f"{args[0]} failed: {result.stderr.strip()[:500]}")
    return result.stdout


def signing_file(path):
    parts = path.split("/")
    if "_CodeSignature" in parts or parts[-1] in {"embedded.mobileprovision", "CodeResources"}:
        return True
    # META-INF also contains runtime resources: only omit JAR signing records.
    return bool(re.fullmatch(r"META-INF/(?:MANIFEST\.MF|[^/]+\.(?:RSA|DSA|EC|SF))", path, re.I))


def normalize_plist(data, app):
    info = plistlib.loads(data)
    # The build machine's own OS build, stamped by Xcode. Two runners of the same image generation
    # carry different values, and it says nothing about the app. The DT* keys stay, because a
    # different Xcode or SDK is a real difference between the two builds.
    info.pop("BuildMachineOSBuild", None)
    for key, field in [("CFBundleIdentifier", "id"), ("CFBundleShortVersionString", "version"),
                       ("CFBundleVersion", "build")]:
        if str(info.get(key, "")) != str(app[field]):
            raise ValueError(f"{key}: package has {info.get(key)!r}, manifest declares {app[field]!r}")
        info[key] = f"<{field}>"
    # The executable is named after the app, and CFBundleName is its product name, so both are
    # replaced by their role. What the executable contains is still compared, under the key the
    # payload gives it.
    executable = str(info.get("CFBundleExecutable", ""))
    if executable:
        info["CFBundleExecutable"] = "<executable>"
        if info.get("CFBundleName") == executable:
            info["CFBundleName"] = "<name>"
    # Keep undeclared/custom labels and URLs visible; only the declared identity is replaced.
    for key in ["CFBundleDisplayName", "CFBundleName"]:
        if info.get(key) == app.get("title"):
            info[key] = "<title>"
    for url in info.get("CFBundleURLTypes", []):
        if url.get("CFBundleURLName") == app["id"]:
            url["CFBundleURLName"] = "<id>"
        url["CFBundleURLSchemes"] = [
            "<scheme>" if scheme == app.get("scheme") else scheme
            for scheme in url.get("CFBundleURLSchemes", [])
        ]
    return canonical(info)


def normalize_android_manifest(text, app):
    # aapt2 decodes both binary and protobuf XML. Check identity before normalizing it.
    fields = [(r'\bpackage="([^\"]+)"', "id"),
              (r':versionName\([^)]*\)="([^\"]+)"', "version"),
              (r':versionCode\([^)]*\)=([^ ]+)', "build")]
    for pattern, field in fields:
        match = re.search(pattern, text)
        if not match or match[1] != str(app[field]):
            raise ValueError(f"Android {field} does not match the declared identity {app[field]!r}")
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith("T: "):
            # Whitespace from source XML is not a manifest declaration.
            if re.fullmatch(r"\s*T: '(?:\\[nrt]|\s)*'", line):
                continue
        line = re.sub(r" \(line=\d+\)", "", line)
        if re.search(r":version(?:Code|Name)\(", line):
            line = line.split("=", 1)[0] + "=<version>"
        else:
            # Package-qualified permissions/provider authorities change with applicationId.
            line = line.replace('"' + app["id"] + '"', '"<id>"')
            line = line.replace('"' + app["id"] + '.', '"<id>.')
            for attr, field in [("label", "title"), ("scheme", "scheme")]:
                if re.search(rf":{attr}\(", line) and app.get(field):
                    line = line.replace('"' + app[field] + '"', f'"<{field}>"')
        lines.append(line)
    return "\n".join(lines).encode()


def normalize_assets(data):
    tool = shutil.which("assetutil")
    if not tool:
        raise ValueError("assetutil is required to compare iOS asset catalogs; use a macOS runner")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "Assets.car"
        path.write_bytes(data)
        entries = json.loads(run_tool([tool, "--info", str(path)]))
    retained = []
    for entry in entries:
        if "AssetType" not in entry:
            continue  # archive header, including build timestamp and authoring-tool version
        if entry.get("Name") == "AppIcon":
            continue  # Day's generated launcher icon, intentionally overlaid by the flavor
        retained.append({k: v for k, v in entry.items() if k not in {"SizeOnDisk", "NameIdentifier"}})
    return canonical(sorted(retained, key=lambda e: canonical(e)))


def payload(path, metadata=None, target=None, aapt2=None):
    """Return canonical file digests plus a list of explicitly excluded/normalized paths."""
    app = identity(metadata, target) if metadata else None
    result, normalized = {}, []
    with zipfile.ZipFile(path) as package, tempfile.TemporaryDirectory() as tmp:
        names = [n for n in package.namelist() if not n.endswith("/")]
        if len(names) != len(set(names)):
            raise ValueError(f"{path}: duplicate archive entries")
        # aapt2 understands a proto APK, while bundles put each module's manifest under manifest/.
        # Repack only the two metadata files, in memory/on disk as data, never extracting paths.
        modules = {}
        if app and target == "android-mdc":
            if not aapt2:
                raise ValueError("aapt2 is required to compare Android metadata")
            for name in names:
                if name.endswith("/manifest/AndroidManifest.xml"):
                    module = name.split("/", 1)[0]
                    apk = Path(tmp) / f"module-{len(modules)}.apk"
                    with zipfile.ZipFile(apk, "w") as out:
                        out.writestr("AndroidManifest.xml", package.read(name))
                        resources = f"{module}/resources.pb"
                        if resources in names:
                            out.writestr("resources.pb", package.read(resources))
                    modules[module] = apk
            if not modules:
                raise ValueError("Android comparison expects an AAB containing module manifests")
        roots = [n.rsplit("/", 1)[0] + "/" for n in names
                 if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", n)]
        if app and target == "ios-uikit" and len(roots) != 1:
            raise ValueError("iOS comparison expects exactly one main app")
        # The bundle and its executable are named after the app, so both sides are keyed by role:
        # the bundle as Payload/App.app and the executable as <executable> inside it. Their
        # contents are still compared.
        executable = ""
        if app and target == "ios-uikit":
            executable = str(plistlib.loads(package.read(roots[0] + "Info.plist"))
                             .get("CFBundleExecutable", ""))
        for name in names:
            if signing_file(name) or name.startswith("BUNDLE-METADATA/com.android.tools.build.debugsymbols/"):
                normalized.append(name)
                continue
            key = name
            if app and target == "ios-uikit" and name.startswith(roots[0]):
                inside = name[len(roots[0]):]
                if executable and inside == executable:
                    inside = "<executable>"
                key = "Payload/App.app/" + inside
            data = package.read(name)
            if app and target == "ios-uikit":
                if name == roots[0] + "Info.plist":
                    data = normalize_plist(data, app)
                    normalized.append(name)
                elif name == roots[0] + "Assets.car":
                    data = normalize_assets(data)
                    normalized.append(name)
                elif re.fullmatch(re.escape(roots[0]) + r"AppIcon[^/]*\.png", name):
                    normalized.append(name)
                    continue
            elif app and target == "android-mdc":
                module = name.split("/", 1)[0]
                if module in modules and name == f"{module}/manifest/AndroidManifest.xml":
                    text = run_tool([aapt2, "dump", "xmltree", str(modules[module]),
                                     "--file", "AndroidManifest.xml"])
                    data = normalize_android_manifest(text, app)
                    normalized.append(name)
                elif module in modules and name == f"{module}/resources.pb":
                    text = run_tool([aapt2, "dump", "resources", str(modules[module])])
                    text = text.replace(f"Package name={app['id']} ", "Package name=<id> ")
                    data = text.encode()
                    normalized.append(name)
                elif re.fullmatch(r"base/res/(?:mipmap|drawable)[^/]*/ic_launcher(?:_background|_foreground|_monochrome|_round)?\.(?:png|webp|xml)", name):
                    normalized.append(name)
                    continue
            result[key] = digest(data)
    return result, normalized
