#!/usr/bin/env python3
"""plex-audiofix: make the preferred-language audio track the FIRST and DEFAULT audio track,
so Plex "original quality" downloads (which play the first track) use the right language.

Usage:
  audiofix.py                 run forever: scan every INTERVAL_MINUTES + fix instantly on Sonarr/Radarr webhook
  audiofix.py --once          one scan, then exit
  audiofix.py probe FILE...   show audio/track order of files (proof step)
  audiofix.py fix FILE...     fix specific files now (ignores DRY_RUN)
"""
import http.server, json, logging, os, queue, shutil, sqlite3, subprocess, sys, threading, time, urllib.parse, urllib.request

E = os.environ.get
flag = lambda k, d: E(k, d).strip().lower() in ("1", "true", "yes")
csv = lambda k, d: [x.strip() for x in E(k, d).split(",") if x.strip()]

ALIASES = {"en": "eng", "english": "eng", "ja": "jpn", "japanese": "jpn", "es": "spa", "spanish": "spa",
           "fr": "fre", "fra": "fre", "french": "fre", "de": "ger", "deu": "ger", "german": "ger",
           "it": "ita", "italian": "ita", "pt": "por", "ko": "kor", "korean": "kor", "zh": "chi",
           "zho": "chi", "chinese": "chi", "ru": "rus", "nl": "dut", "nld": "dut", "hi": "hin"}
norm = lambda lang: (lambda l: ALIASES.get(l, l))((lang or "und").lower().split("-")[0])

MEDIA_DIRS = csv("MEDIA_DIRS", "/media")
PREFERRED = [norm(l) for l in csv("PREFERRED_LANGUAGES", "eng")]
EXTS = tuple("." + x.lower().lstrip(".") for x in csv("EXTENSIONS", "mkv,mp4,m4v"))
INTERVAL = int(E("INTERVAL_MINUTES", "60")) * 60
MIN_AGE = int(E("MIN_FILE_AGE_MINUTES", "30")) * 60
DRY_RUN = flag("DRY_RUN", "true")
SKIP_HARDLINKED = flag("SKIP_HARDLINKED", "true")
DB = E("STATE_DB", "/config/state.db")
PLEX_URL, PLEX_TOKEN = E("PLEX_URL", "").rstrip("/"), E("PLEX_TOKEN", "")
PATH_MAP = [p.split(":", 1) for p in csv("PLEX_PATH_MAP", "")]  # container_path:plex_path
ARR_PATH_MAP = [p.split(":", 1) for p in csv("ARR_PATH_MAP", "")]  # arr_path:container_path
WEBHOOK_PORT = int(E("WEBHOOK_PORT", "8787"))
NOTIFY_URL = E("NOTIFY_URL", "")  # Discord webhook or ntfy topic URL
TMP = ".audiofix-"
Q = queue.Queue()
log = logging.getLogger("audiofix")
remap = lambda p, m: next((dst + p[len(src):] for src, dst in m if p.startswith(src)), p)


def notify(msg):
    if not NOTIFY_URL:
        return
    try:
        body, hdr = (json.dumps({"content": msg}).encode(), {"Content-Type": "application/json"}) if "discord" in NOTIFY_URL else (msg.encode(), {})
        urllib.request.urlopen(urllib.request.Request(NOTIFY_URL, data=body, headers=hdr), timeout=15).close()
    except Exception as e:
        log.warning("notify failed: %s", e)


def run(cmd, ok=(0,)):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode not in ok:
        raise RuntimeError(f"{cmd[0]} failed: {(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout


def track(id, type, lang, default, commentary, title=""):
    return dict(id=id, type=type, lang=norm(lang), default=bool(default),
                commentary=bool(commentary) or "comment" in (title or "").lower())


def probe(path):
    if path.lower().endswith(".mkv"):
        tracks = json.loads(run(["mkvmerge", "-J", path])).get("tracks", [])
        return [track(t["id"], t["type"], p.get("language_ietf") or p.get("language"), p.get("default_track"),
                      p.get("flag_commentary"), p.get("track_name")) for t in tracks for p in [t.get("properties", {})]]
    kinds = {"video": "video", "audio": "audio", "subtitle": "subtitles"}
    streams = json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", path])).get("streams", [])
    return [track(s["index"], kinds.get(s.get("codec_type"), "other"), g.get("language"), d.get("default"),
                  d.get("comment"), g.get("title")) for s in streams for g, d in [(s.get("tags", {}), s.get("disposition", {}))]]


def plan(tracks):
    """-> (status, audio tracks in desired order or None)"""
    audio = [t for t in tracks if t["type"] == "audio"]
    if len(audio) < 2:
        return "single_audio", None
    pick = lambda allow_comm: next((t for l in PREFERRED for t in audio if t["lang"] == l and (allow_comm or not t["commentary"])), None)
    pref = pick(False) or pick(True)
    if not pref:
        return "no_preferred", None
    if audio[0] is pref and pref["default"] and not any(t["default"] for t in audio[1:]):
        return "ok", None
    return "needs_fix", [pref] + [t for t in audio if t is not pref]


def fix(path, tracks, audio):
    st, tmp = os.stat(path), os.path.join(os.path.dirname(path), TMP + os.path.basename(path))
    if shutil.disk_usage(os.path.dirname(path)).free < st.st_size * 1.1:
        raise RuntimeError("not enough free space to remux")
    order = [t for t in tracks if t["type"] == "video"] + audio + [t for t in tracks if t["type"] not in ("video", "audio")]
    if path.lower().endswith(".mkv"):
        cmd = ["mkvmerge", "-q", "-o", tmp, "--track-order", ",".join(f"0:{t['id']}" for t in order)]
        cmd += [x for i, t in enumerate(audio) for x in ("--default-track-flag", f"{t['id']}:{int(i == 0)}")] + [path]
    else:  # MP4/M4V: copy video/audio/subs (data/timecode streams can't always be copied)
        order = [t for t in order if t["type"] != "other"]
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", path, *[x for t in order for x in ("-map", f"0:{t['id']}")],
               "-map_metadata", "0", "-map_chapters", "0", "-c", "copy", "-movflags", "+faststart"]
        cmd += [x for i in range(len(audio)) for x in (f"-disposition:a:{i}", "+default" if i == 0 else "-default")] + [tmp]
    try:
        run(cmd, ok=(0, 1))  # mkvmerge exit 1 = warnings only
        new = probe(tmp)
        if len(new) != len(order) or plan(new)[0] != "ok" or os.path.getsize(tmp) < st.st_size * 0.9:
            raise RuntimeError("verification of remuxed file failed; original left untouched")
        os.chmod(tmp, st.st_mode & 0o7777)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except PermissionError:
            pass
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def check(path, st):
    try:
        tracks = probe(path)
        status, audio = plan(tracks)
        langs = "/".join(t["lang"] for t in tracks if t["type"] == "audio")
        if status != "needs_fix":
            return status, langs
        if SKIP_HARDLINKED and st.st_nlink > 1:
            log.warning("skip (hardlinked, e.g. still seeding): %s [%s]", path, langs)
            return "hardlinked", langs
        if DRY_RUN:
            log.info("[dry run] would fix %s [%s]", path, langs)
            return "would_fix", langs
        fix(path, tracks, audio)
        log.info("fixed %s [%s -> %s]", path, langs, "/".join(t["lang"] for t in audio))
        return "fixed", langs
    except Exception as e:
        log.error("%s: %s", path, e)
        return "error", str(e)[:500]


def plex(path):
    sep = "&" if "?" in path else "?"
    req = urllib.request.Request(f"{PLEX_URL}{path}{sep}X-Plex-Token={PLEX_TOKEN}", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
    return json.loads(body) if body.strip().startswith(b"{") else {}


def refresh_plex(dirs):
    if not (dirs and PLEX_URL and PLEX_TOKEN):
        return
    try:
        sections = [(s["key"], l["path"]) for s in plex("/library/sections")["MediaContainer"].get("Directory", [])
                    for l in s.get("Location", [])]
        for d in (remap(d, PATH_MAP) for d in dirs):
            for key, loc in sections:
                if d.startswith(loc):
                    plex(f"/library/sections/{key}/refresh?path={urllib.parse.quote(d)}")
                    log.info("asked Plex to rescan %s", d)
    except Exception as e:
        log.warning("Plex refresh failed: %s", e)


def scan(c):
    cfg = f"{PREFERRED}|{DRY_RUN}|{SKIP_HARDLINKED}"  # settings changed -> re-check everything
    if (c.execute("SELECT v FROM meta WHERE k='cfg'").fetchone() or [None])[0] != cfg:
        c.execute("DELETE FROM files")
        c.execute("REPLACE INTO meta VALUES('cfg', ?)", (cfg,))
        c.commit()
    counts, fixed_dirs = {}, set()
    for root in MEDIA_DIRS:
        for dirpath, _, names in os.walk(root):
            for n in names:
                if n.lower().endswith(EXTS) and not n.startswith(TMP):
                    s = process(c, os.path.join(dirpath, n))
                    if s:
                        counts[s] = counts.get(s, 0) + 1
                    if s == "fixed":
                        fixed_dirs.add(dirpath)
    log.info("scan done: %s", counts or "no new or changed files")
    refresh_plex(fixed_dirs)
    if counts.get("fixed") or counts.get("error"):
        notify(f"plex-audiofix: {counts}")


def process(c, p, from_hook=False):
    """Check/fix one file if new, changed, retryable, or no longer hardlinked. Returns status or None if skipped."""
    try:
        st = os.stat(p)
    except OSError:
        return None
    k = c.execute("SELECT size, mtime, status, tries FROM files WHERE path=?", (p,)).fetchone()
    retry = k and ((k[2] == "error" and k[3] < 3) or (k[2] == "hardlinked" and st.st_nlink == 1))
    if (not from_hook and time.time() - st.st_mtime < MIN_AGE) or (k and k[:2] == (st.st_size, st.st_mtime) and not retry):
        return None  # still being written, or unchanged since last check
    status, detail = check(p, st)
    tries = (k[3] if k and k[2] == "error" else 0) + 1 if status == "error" else 0
    if status == "fixed":
        st = os.stat(p)
    if status == "error" and from_hook:
        notify(f"plex-audiofix error: {p}: {detail}")
    c.execute("REPLACE INTO files VALUES(?,?,?,?,?,?,?)", (p, st.st_size, st.st_mtime, status, detail, tries, time.time()))
    c.commit()
    return status


class Hook(http.server.BaseHTTPRequestHandler):
    """Sonarr/Radarr 'Webhook' connection (On Import / On Upgrade): queue every media file path in the payload."""
    def do_POST(self):
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            data = {}
        paths = [p for p in (remap(p, ARR_PATH_MAP) for p in find_paths(data)) if any(p.startswith(d) for d in MEDIA_DIRS)]
        for p in paths:
            Q.put(p)
        log.info("webhook %s: queued %s", data.get("eventType", "?"), paths or "nothing")
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


def find_paths(o):
    if isinstance(o, list):
        return [p for v in o for p in find_paths(v)]
    if isinstance(o, dict):
        return [p for k, v in o.items() for p in ([v] if k == "path" and isinstance(v, str) and v.lower().endswith(EXTS) else find_paths(v))]
    return []


def main():
    global DRY_RUN
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = sys.argv[1:]
    if args[:1] == ["probe"]:
        for p in args[1:]:
            tracks = probe(p)
            print(p)
            for t in tracks:
                print(f"  #{t['id']:<3}{t['type']:<10}{t['lang']:<5}{'DEFAULT' if t['default'] else '':<8}{'commentary' if t['commentary'] else ''}")
            print("  ->", plan(tracks)[0])
        return
    if args[:1] == ["fix"]:
        DRY_RUN = False
        for p in args[1:]:
            print(p, "->", *check(p, os.stat(p)))
        return
    os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, size INT, mtime REAL, status TEXT, detail TEXT, tries INT, checked REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    log.info("watching %s | preferred=%s | dry_run=%s | webhook port=%s", MEDIA_DIRS, PREFERRED, DRY_RUN, WEBHOOK_PORT)
    if "--once" in args:
        return scan(c)
    if WEBHOOK_PORT:
        threading.Thread(target=http.server.ThreadingHTTPServer(("", WEBHOOK_PORT), Hook).serve_forever, daemon=True).start()
    next_scan = 0
    while True:  # one worker: hourly scans + webhook files, never two remuxes at once
        if time.time() >= next_scan:
            scan(c)
            next_scan = time.time() + max(INTERVAL, 60)
        try:
            p = Q.get(timeout=max(1, next_scan - time.time()))
        except queue.Empty:
            continue
        if process(c, p, from_hook=True) == "fixed":
            refresh_plex({os.path.dirname(p)})


if __name__ == "__main__":
    main()
