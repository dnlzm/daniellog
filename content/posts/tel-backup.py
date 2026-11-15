#!/usr/bin/env python3
"""
Pixel-Backup per adb: kopiert die komplette SD-Karte (/sdcard) mit mehreren
parallelen Threads auf ein Ziel und vergleicht danach Handy und Backup.

Beispiele:
    python3 pixel_backup.py                      # Backup + Vergleich
    python3 pixel_backup.py --verify-only        # nur Vergleich
    python3 pixel_backup.py --deep               # Vergleich zusätzlich per MD5
    python3 pixel_backup.py --exclude 'Android/data/*' --exclude '*/.thumbnails/*'

Wiederaufnahme: Dateien, die am Ziel schon mit gleicher Größe vorliegen,
werden übersprungen. Nach einem Abbruch das Skript also einfach erneut starten.
"""

import argparse
import fnmatch
import hashlib
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DEFAULT_DEST = "~/tel-backup"
DEFAULT_SRC = "/sdcard"
DEFAULT_REPORT = str(Path.home() / "tel-backup-report.txt")
FAT32_LIMIT = 4 * 1024**3 - 1


def die(msg):
    print(f"FEHLER: {msg}", file=sys.stderr)
    sys.exit(2)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def nfc(s):
    return unicodedata.normalize("NFC", s)


# --------------------------------------------------------------------------- #
# adb / Handy
# --------------------------------------------------------------------------- #

def pick_device(serial):
    if shutil.which("adb") is None:
        die("adb nicht gefunden (brew install android-platform-tools)")
    out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
    devices = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    if serial:
        if serial not in devices:
            die(f"Gerät {serial} nicht verbunden/autorisiert. Gefunden: {devices or 'keines'}")
        return serial
    if not devices:
        die("kein autorisiertes Gerät gefunden (Handy entsperren, Debugging-Dialog bestätigen)")
    if len(devices) > 1:
        die(f"mehrere Geräte verbunden {devices}, bitte --serial angeben")
    return devices[0]


def adb_base(serial):
    return ["adb", "-s", serial]


def excluded(rel, patterns):
    return any(fnmatch.fnmatch(rel, p) for p in patterns)


def list_phone(adb, src, patterns):
    """Liefert {relativer_pfad: größe} aller Dateien auf dem Handy."""
    remote = f"cd {shlex.quote(src.rstrip('/'))}/ && find . -type f -exec stat -c '%s %n' {{}} + 2>/dev/null"
    r = subprocess.run(adb + ["exec-out", remote], capture_output=True)
    text = r.stdout.decode("utf-8", errors="surrogateescape")
    files = {}
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if not line:
            continue
        size_s, _, name = line.partition(" ")
        if not size_s.isdigit():
            continue
        rel = name[2:] if name.startswith("./") else name
        if excluded(rel, patterns):
            continue
        files[rel] = int(size_s)
    if not files:
        die("Dateiliste vom Handy ist leer (Handy entsperrt? Pfad korrekt?)")
    return files


def phone_md5(adb, src, patterns):
    remote = f"cd {shlex.quote(src.rstrip('/'))}/ && find . -type f -exec md5sum {{}} + 2>/dev/null"
    r = subprocess.run(adb + ["exec-out", remote], capture_output=True)
    text = r.stdout.decode("utf-8", errors="surrogateescape")
    sums = {}
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if len(line) < 35:
            continue
        digest, name = line[:32], line[34:]
        rel = name[2:] if name.startswith("./") else name
        if not excluded(rel, patterns):
            sums[rel] = digest
    return sums


# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #

def pull_one(adb, src, dest, rel, size, retries):
    local = dest / rel
    tmp = local.with_name(local.name + ".part")
    try:
        if local.exists() and local.stat().st_size == size:
            return ("skip", rel, 0, "")
        local.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return ("fail", rel, 0, str(e))

    remote = f"{src.rstrip('/')}/{rel}"
    err = ""
    for attempt in range(retries + 1):
        try:
            r = subprocess.run(
                adb + ["pull", "-a", remote, str(tmp)],
                capture_output=True, text=True, errors="replace",
                timeout=60 + size / 1e6,
            )
            if r.returncode == 0 and tmp.exists() and tmp.stat().st_size == size:
                os.replace(tmp, local)
                return ("ok", rel, size, "")
            err = (r.stderr or r.stdout).strip()[-300:] or "Größe stimmt nicht"
        except subprocess.TimeoutExpired:
            err = "Timeout"
        except OSError as e:
            err = str(e)
        time.sleep(1 + attempt * 2)
    try:
        tmp.unlink()
    except OSError:
        pass
    return ("fail", rel, 0, err)


def run_backup(adb, src, dest, files, threads, retries):
    total_files = len(files)
    total_bytes = sum(files.values())
    print(f"Handy: {total_files} Dateien, {human(total_bytes)}")

    # Große Dateien zuerst: bessere Auslastung der Threads
    todo = sorted(files.items(), key=lambda kv: -kv[1])

    stats = Counter()
    failed = []
    t0 = time.time()
    last_print = 0.0
    lock = threading.Lock()

    def progress(force=False):
        nonlocal last_print
        now = time.time()
        if not force and now - last_print < 2:
            return
        last_print = now
        elapsed = max(now - t0, 0.001)
        done = stats["ok"] + stats["skip"] + stats["fail"]
        rate = stats["bytes"] / elapsed / 1024**2
        print(
            f"\r{done}/{total_files} Dateien | kopiert {human(stats['bytes'])} "
            f"| {rate:5.1f} MB/s | übersprungen {stats['skip']} | Fehler {stats['fail']}   ",
            end="", flush=True,
        )

    ex = ThreadPoolExecutor(max_workers=threads)
    try:
        futs = [ex.submit(pull_one, adb, src, dest, rel, size, retries) for rel, size in todo]
        for fut in as_completed(futs):
            status, rel, nbytes, err = fut.result()
            with lock:
                stats[status] += 1
                stats["bytes"] += nbytes
                if status == "fail":
                    failed.append((rel, err))
                    print(f"\nFEHLER bei {rel}: {err}")
                progress()
        progress(force=True)
        print()
    except KeyboardInterrupt:
        print("\nAbbruch durch Benutzer, warte auf laufende Transfers ...")
        ex.shutdown(wait=False, cancel_futures=True)
        sys.exit(130)
    finally:
        ex.shutdown(wait=True)

    elapsed = time.time() - t0
    print(
        f"Backup fertig in {elapsed:.0f}s: {stats['ok']} kopiert, "
        f"{stats['skip']} übersprungen, {stats['fail']} Fehler"
    )
    return failed


# --------------------------------------------------------------------------- #
# Vergleich
# --------------------------------------------------------------------------- #

def scan_local(dest, patterns):
    """{nfc(relativer_pfad): (echter_pfad, größe)}"""
    local = {}
    for root, _, names in os.walk(dest):
        for name in names:
            p = Path(root) / name
            rel = p.relative_to(dest).as_posix()
            if excluded(rel, patterns):
                continue
            try:
                local[nfc(rel)] = (p, p.stat().st_size)
            except OSError:
                pass
    return local


def md5_local(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run_verify(adb, src, dest, patterns, deep, threads, report_path):
    print("\n=== Vergleich Handy <-> Backup ===")
    phone = {nfc(k): v for k, v in list_phone(adb, src, patterns).items()}
    local = scan_local(dest, patterns)

    leftovers = sorted(k for k in local if k.endswith(".part"))
    for k in leftovers:
        del local[k]

    missing = sorted(set(phone) - set(local))
    extra = sorted(set(local) - set(phone))
    size_diff = sorted(k for k in phone if k in local and local[k][1] != phone[k])

    # Groß-/Kleinschreibung-Kollisionen (macOS-Volumes unterscheiden meist nicht)
    groups = defaultdict(list)
    for k in phone:
        groups[k.lower()].append(k)
    collisions = sorted(v for v in groups.values() if len(v) > 1)

    # Zählung pro Top-Level-Ordner
    def top(k):
        return k.split("/", 1)[0] if "/" in k else "(Wurzel)"

    cp, cl = Counter(top(k) for k in phone), Counter(top(k) for k in local)
    print(f"\n{'Ordner':<20}{'Handy':>10}{'Backup':>10}{'Differenz':>11}")
    for folder in sorted(set(cp) | set(cl)):
        print(f"{folder:<20}{cp[folder]:>10}{cl[folder]:>10}{cp[folder] - cl[folder]:>11}")
    print(f"{'GESAMT':<20}{len(phone):>10}{len(local):>10}{len(phone) - len(local):>11}")
    print(f"\nGröße Handy:  {human(sum(phone.values()))}")
    print(f"Größe Backup: {human(sum(s for _, s in local.values()))}")

    hash_diff = []
    if deep:
        print("\nMD5-Prüfung (kann dauern) ...")
        psums = {nfc(k): v for k, v in phone_md5(adb, src, patterns).items()}
        common = [k for k in phone if k in local and k in psums and local[k][1] == phone[k]]
        done = 0
        with ThreadPoolExecutor(max_workers=threads) as ex:
            futs = {ex.submit(md5_local, local[k][0]): k for k in common}
            for fut in as_completed(futs):
                k = futs[fut]
                done += 1
                if fut.result() != psums[k]:
                    hash_diff.append(k)
                if done % 200 == 0:
                    print(f"\r{done}/{len(common)} geprüft", end="", flush=True)
        print(f"\r{len(common)}/{len(common)} geprüft")
        hash_diff.sort()

    problems = len(missing) + len(size_diff) + len(hash_diff)
    big = [k for k in missing if phone[k] > FAT32_LIMIT]

    lines = [
        f"Fehlend im Backup:        {len(missing)}",
        f"Größe abweichend:         {len(size_diff)}",
        f"MD5 abweichend:           {len(hash_diff)}" if deep else "MD5-Prüfung:             nicht durchgeführt (--deep)",
        f"Nur im Backup (extra):    {len(extra)}",
        f"Übrige .part-Dateien:     {len(leftovers)}",
        f"Namenskollisionen (Case): {len(collisions)}",
    ]
    print("\n" + "\n".join(lines))
    if big:
        print(f"Hinweis: {len(big)} fehlende Dateien sind > 4 GB (FAT32-Limit?). Ziel mit exFAT/APFS formatiert?")

    with open(report_path, "w", encoding="utf-8", errors="replace") as f:
        f.write("\n".join(lines) + "\n")
        for title, items in (
            ("FEHLEND", missing), ("GRÖSSE ABWEICHEND", size_diff), ("MD5 ABWEICHEND", hash_diff),
            ("EXTRA IM BACKUP", extra), ("PART-DATEIEN", leftovers),
        ):
            f.write(f"\n--- {title} ({len(items)}) ---\n")
            f.writelines(f"{k}\n" for k in items)
        f.write(f"\n--- NAMENSKOLLISIONEN ({len(collisions)}) ---\n")
        f.writelines(" | ".join(g) + "\n" for g in collisions)
    print(f"Bericht: {report_path}")

    if problems == 0:
        print("\nOK: Alle Dateien vorhanden, Größen" + (" und MD5-Summen" if deep else "") + " stimmen überein.")
    else:
        print(f"\nACHTUNG: {problems} Abweichungen. Skript erneut starten, fehlende Dateien werden nachgeholt.")
        for k in missing[:10]:
            print(f"  fehlt: {k}")
        if len(missing) > 10:
            print(f"  ... und {len(missing) - 10} weitere (siehe Bericht)")
    return problems


def check_dest(dest):
    parts = dest.parts
    if len(parts) > 2 and parts[1] == "Volumes":
        mount = Path("/Volumes") / parts[2]
        if not os.path.ismount(mount):
            die(f"{mount} ist nicht eingehängt (Laufwerk angeschlossen?)")
    dest.mkdir(parents=True, exist_ok=True)


def main():
    ap = argparse.ArgumentParser(description="Paralleles adb-Backup der SD-Karte mit Verifikation")
    ap.add_argument("--dest", default=DEFAULT_DEST, help=f"Ziel (Standard: {DEFAULT_DEST})")
    ap.add_argument("--src", default=DEFAULT_SRC, help=f"Quelle auf dem Handy (Standard: {DEFAULT_SRC})")
    ap.add_argument("--threads", type=int, default=4, help="parallele Transfers (Standard: 4)")
    ap.add_argument("--retries", type=int, default=2, help="Wiederholungen pro Datei (Standard: 2)")
    ap.add_argument("--serial", help="adb-Seriennummer, falls mehrere Geräte verbunden sind")
    ap.add_argument("--exclude", action="append", default=[], metavar="MUSTER",
                    help="Pfadmuster relativ zur Quelle ausschließen, z. B. 'Android/data/*' (mehrfach möglich)")
    ap.add_argument("--verify-only", action="store_true", help="nur vergleichen, nichts kopieren")
    ap.add_argument("--deep", action="store_true", help="beim Vergleich zusätzlich MD5-Summen prüfen")
    ap.add_argument("--report", default=DEFAULT_REPORT, help=f"Berichtsdatei (Standard: {DEFAULT_REPORT})")
    args = ap.parse_args()

    dest = Path(args.dest).expanduser()
    serial = pick_device(args.serial)
    adb = adb_base(serial)
    print(f"Gerät: {serial}\nQuelle: {args.src}\nZiel:   {dest}\nThreads: {args.threads}")
    check_dest(dest)

    caffeinate = None
    try:
        caffeinate = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    except FileNotFoundError:
        pass  # kein macOS

    files = list_phone(adb, args.src, args.exclude)

    if not args.verify_only:
        need = sum(files.values())
        free = shutil.disk_usage(dest).free
        if free < need * 1.02:
            die(f"zu wenig Platz am Ziel: frei {human(free)}, benötigt ca. {human(need)}")
        failed = run_backup(adb, args.src, dest, files, args.threads, args.retries)
        if failed:
            print(f"{len(failed)} Dateien fehlgeschlagen, siehe Vergleich/Bericht.")

    problems = run_verify(adb, args.src, dest, args.exclude, args.deep, args.threads, args.report)

    if caffeinate:
        caffeinate.terminate()
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
