# plex-audiofix

Plex "Original quality" downloads copy the file as-is, and the offline player plays the **first**
audio track. This container makes your preferred-language track the first and default audio track
(MKV via mkvmerge, MP4 via ffmpeg; lossless remux, nothing re-encoded).

How it runs on its own:
- **New imports:** Sonarr/Radarr send a webhook, and the file is fixed seconds after import.
- **Everything else:** an hourly scan picks up new or changed files (the first run covers the whole library).
- **Seeding torrents:** files hardlinked to qBittorrent are left alone until the torrent is removed
  (qui share limits, cleanuperr, manual), then fixed on the next scan automatically.
- **Plex:** asked to rescan each fixed folder. Optional Discord/ntfy summary after scans that changed something.

## Install (Unraid template, recommended)
1. Create a GitHub repo named `plex-audiofix`, push this folder to `main`. The Actions workflow builds
   `ghcr.io/<you>/plex-audiofix:latest` (and rebuilds weekly for security updates).
2. GitHub > your profile > Packages > plex-audiofix > Package settings > change visibility to **Public**
   (so Unraid can pull without a login).
3. Copy `unraid/my-plex-audiofix.xml` to
   `/boot/config/plugins/dockerMan/templates-user/` on the Unraid flash drive.
4. Docker tab > Add Container > Template: **plex-audiofix**. Fill in Plex URL/token, leave Dry run = true, Apply.
5. Check the log (`would fix ...` lines). If it looks right, edit the container, set Dry run = false, Apply.

No GitHub? Copy the folder to `/mnt/user/appdata/plex-audiofix/src` and use `docker-compose.yml` with the
Compose Manager plugin (or `docker compose up -d --build`).

## Connect Sonarr and Radarr
Settings > Connect > + > **Webhook**
- Name: `plex-audiofix`
- Triggers: **On Import**, **On Upgrade** (On Rename optional)
- URL: `http://<UNRAID-IP>:8787` (or `http://plex-audiofix:8787` if they share a custom Docker network)
- Method: POST > Test > Save

Map `/data` the same way Sonarr/Radarr do (TRaSH layout: `/mnt/user/data` -> `/data`) and no path
mapping is needed. If they use `/tv` or `/movies`, set `ARR_PATH_MAP=/tv:/data/media/tv,/movies:/data/media/movies`.

## Prove it first (optional)
```bash
docker exec plex-audiofix python3 /app/audiofix.py probe "/data/media/tv/Show/Season 01/ep.mkv"
docker exec plex-audiofix python3 /app/audiofix.py fix   "/data/media/tv/Show/Season 01/ep.mkv"
```
Then delete the download on your phone, download again, and play in airplane mode.

## Notes
- The first real run rewrites every affected file (full read + write, parity writes). Let it run overnight.
- Needs free space equal to the file being fixed. Every remux is verified before the original is replaced.
- Files whose preferred-language track isn't tagged (`und`) are left alone (`no_preferred` in the DB).
- Torrents you cross-seed forever never drop their hardlink. Set `SKIP_HARDLINKED=false` to fix them
  anyway, at the cost of the seed copy and library copy using separate disk space.
- Changing languages, dry run or hardlink settings re-checks the whole library once. State: `/config/state.db`.
