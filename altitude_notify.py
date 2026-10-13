import glob
import os
import subprocess
import time

import requests


def load_env(path):
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())


load_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Config
DRY_RUN = False
IS_WINDOWS = os.name == "nt"

LOG_DIR = os.environ.get("IVAO_LOG_DIR")
if not LOG_DIR:
    if IS_WINDOWS:
        LOG_DIR = os.path.join(
            os.environ["APPDATA"],
            "Microsoft Flight Simulator",
            "packages",
            "IVAO",
            "PilotCore",
        )
    else:
        LOG_DIR = os.path.dirname(os.path.abspath(__file__))

POLL_SECONDS = 1.0
SNAPSHOT_INTERVAL = 15
TEMP_DIR = os.environ.get("TEMP", os.environ.get("TMP", ".")) if IS_WINDOWS else "/tmp"
SNAPSHOT_COPY = os.path.join(TEMP_DIR, "ivao_live.log")
PS_HELPER_PATH = os.path.join(TEMP_DIR, "ivao_snapshot.ps1")

PS_HELPER = r"""
param([string]$SrcDir, [string]$Dest)
$ErrorActionPreference = "Stop"
$name = (Get-ChildItem -LiteralPath $SrcDir -Filter 'ivao_pilot_core_msfs_*.log' |
         Sort-Object Name | Select-Object -Last 1).Name
if (-not $name) { exit 2 }
$r = ([wmiclass]'Win32_ShadowCopy').Create('C:\','ClientAccessible')
if ($r.ReturnValue -ne 0) { exit 1 }
$s = Get-WmiObject Win32_ShadowCopy | Where-Object { $_.ID -eq $r.ShadowID }
try {
    $rel = $SrcDir.Substring(2)
    Copy-Item -LiteralPath "$($s.DeviceObject)$rel\$name" -Destination $Dest -Force
    Write-Output $name
} finally {
    $s.Delete()
}
"""


def write_ps_helper():
    with open(PS_HELPER_PATH, "w", encoding="utf-8") as f:
        f.write(PS_HELPER)


def refresh_snapshot():
    res = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            PS_HELPER_PATH,
            LOG_DIR,
            SNAPSHOT_COPY,
        ],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        print("snapshot failed:", res.returncode, res.stderr.strip())
        return None
    lines = res.stdout.strip().splitlines()
    return lines[-1] if lines else None


def get_source():
    if IS_WINDOWS:
        name = refresh_snapshot()
        if name is None:
            return None, None
        return SNAPSHOT_COPY, name
    files = glob.glob(os.path.join(LOG_DIR, "ivao_pilot_core_msfs_*.log"))
    if not files:
        return None, None
    path = max(files)
    return path, os.path.basename(path)


def find_callsign(path):
    with open(path, "rb") as f:
        for raw in f:
            line = raw.decode("ascii", "replace")
            if "Received IVAO Credentials:" in line:
                return (
                    line.split("Received IVAO Credentials:")[1].split(" - ")[0].strip()
                )
    return None


def should_forward(line, callsign):
    if "Displaying text" in line:
        return False
    if "PRIV_MSG" in line and f"to {callsign}:" in line:
        return True
    if "FREQ_MSG" in line and f"{callsign}," in line:
        return True
    return False


def send_telegram(text):
    if DRY_RUN:
        print("[DRY_RUN]", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10
        )
        if not r.ok:
            print("Telegram error:", r.status_code, r.text)
    except Exception as e:
        print("Send failed:", e)


def read_new(path, pos):
    try:
        with open(path, "rb") as f:
            f.seek(pos)
            data = f.read()
            return data.decode("ascii", "replace"), f.tell()
    except OSError:
        return "", pos


def is_admin():
    if not IS_WINDOWS:
        return True
    try:
        import ctypes

        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def follow():
    src, name = get_source()
    if src is None:
        print("No log file found in", LOG_DIR)
        return
    callsign = find_callsign(src)
    print(f"Monitoring as {callsign} ({name})")
    send_telegram(f"Monitoring as {callsign}")

    pos = os.path.getsize(src)
    buffer = ""

    while True:
        if IS_WINDOWS:
            time.sleep(SNAPSHOT_INTERVAL)
            src, name2 = get_source()
            if src is None:
                print("snapshot unavailable, retrying...")
                continue
            if name2 != name:
                name = name2
                callsign = find_callsign(src)
                pos = os.path.getsize(src)
                buffer = ""
                print(f"Switched to {name} (now {callsign})")
        else:
            time.sleep(POLL_SECONDS)

        chunk, pos = read_new(src, pos)
        if not chunk:
            continue
        buffer += chunk
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")
            if should_forward(line, callsign):
                print("->", line)
                send_telegram(line)


def main():
    if not DRY_RUN and not TELEGRAM_TOKEN:
        print("No TELEGRAM_TOKEN set - copy .env.example to .env and fill it in.")
        return
    if IS_WINDOWS and not is_admin():
        print("WARNING: not running as Administrator - snapshot creation will fail.")
    try:
        if IS_WINDOWS:
            write_ps_helper()
        follow()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
