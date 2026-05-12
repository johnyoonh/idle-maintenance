#!/usr/bin/python3
import os
import plistlib
import re
import subprocess

KNOWN_CASK_APP_NAMES = {
    "activitywatch": "ActivityWatch",
    "alt-tab": "AltTab",
    "brave-browser": "Brave Browser",
    "google-chrome": "Google Chrome",
    "google-drive": "Google Drive",
    "iterm2": "iTerm",
    "karabiner-elements": "Karabiner-Elements",
    "lm-studio": "LM Studio",
    "microsoft-edge": "Microsoft Edge",
    "visual-studio-code": "Visual Studio Code",
}

def normalize_app_name(name):
    name = os.path.basename(name)
    if name.endswith(".app"):
        name = name[:-4]
    return re.sub(r"[^a-z0-9]+", "", name.lower())

def load_brewfile_sources(path):
    path = os.path.expanduser(path)
    casks = {}
    mas_apps = {}
    if not os.path.exists(path):
        return casks, mas_apps

    with open(path, "r") as f:
        for line in f:
            cask_match = re.match(r'^\s*cask\s+"([^"]+)"', line)
            if cask_match:
                token = cask_match.group(1)
                app_name = KNOWN_CASK_APP_NAMES.get(token, token.replace("-", " ").title())
                casks[normalize_app_name(app_name)] = {
                    "source": "brew-cask",
                    "token": token,
                    "restore_command": f"brew install --cask {token}",
                }
                continue

            mas_match = re.match(r'^\s*mas\s+"([^"]+)",\s+id:\s+([0-9]+)', line)
            if mas_match:
                name, app_id = mas_match.groups()
                mas_apps[normalize_app_name(name)] = {
                    "source": "mas",
                    "app_id": app_id,
                    "name": name,
                    "restore_command": f"mas install {app_id}",
                }

    return casks, mas_apps

def load_mas_inventory(path):
    path = os.path.expanduser(path)
    mas_apps = {}
    if not os.path.exists(path):
        return mas_apps

    with open(path, "r") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 2 or not parts[0].isdigit():
                continue
            app_id, name = parts[0], parts[1]
            mas_apps[normalize_app_name(name)] = {
                "source": "mas",
                "app_id": app_id,
                "name": name,
                "restore_command": f"mas install {app_id}",
            }
    return mas_apps

def classify_app_restore_source(app_path, providers=None):
    app_key = normalize_app_name(app_path)
    for provider in providers or []:
        if not isinstance(provider, dict):
            continue
        provider_type = provider.get("type")
        provider_path = provider.get("path")
        if not provider_path:
            continue

        if provider_type == "homebrew_bundle":
            casks, mas_apps = load_brewfile_sources(provider_path)
            if app_key in casks:
                return casks[app_key]
            if app_key in mas_apps:
                return mas_apps[app_key]
        elif provider_type == "mas_tsv":
            mas_apps = load_mas_inventory(provider_path)
            if app_key in mas_apps:
                return mas_apps[app_key]

    return {
        "source": "unknown",
        "restore_command": "",
    }

def app_metadata(app_path):
    info_plist = os.path.join(app_path, "Contents", "Info.plist")
    metadata = {
        "bundle_id": "",
        "version": "",
        "short_version": "",
    }
    if os.path.exists(info_plist):
        try:
            with open(info_plist, "rb") as f:
                info = plistlib.load(f)
            metadata["bundle_id"] = str(info.get("CFBundleIdentifier", ""))
            metadata["version"] = str(info.get("CFBundleVersion", ""))
            metadata["short_version"] = str(info.get("CFBundleShortVersionString", ""))
            return metadata
        except Exception:
            pass

    try:
        bundle_id = subprocess.check_output(
            ["mdls", "-raw", "-name", "kMDItemCFBundleIdentifier", app_path],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if bundle_id != "(null)":
            metadata["bundle_id"] = bundle_id
    except Exception:
        pass
    return metadata
