# Auto Clipper

Downloads new videos from sources you own or are licensed to clip, picks the best moments, renders 9:16 captioned Shorts, and uploads them to YouTube. Runs on a GitHub Actions schedule, so nothing needs to be open on your side.

## One-time setup (about 20 minutes)

1. **Create a GitHub repo** (public repos get unlimited free Actions minutes) and push this folder to it.
2. **YouTube API access**
   - Google Cloud Console: new project, enable "YouTube Data API v3".
   - OAuth consent screen: add your Google account as a test user.
   - Credentials: create an OAuth client ID of type Desktop app, download it as `client_secret.json` next to `get_refresh_token.py`.
   - Run `pip install google-auth-oauthlib` then `python get_refresh_token.py` on your own computer. Sign in with the channel's account.
3. **Add repository secrets** (Settings, Secrets and variables, Actions):
   - `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN` (printed by the script)
   - `ANTHROPIC_API_KEY` (optional; smarter clip selection. Without it a keyword heuristic is used)
   - `YT_COOKIES` (optional; Netscape-format cookies.txt contents, used if YouTube blocks downloads from GitHub's servers)
4. **Edit `config.yaml`**: set `sources` to your channel or playlist URLs and change `rights_confirmed` to `true` only when you own or are licensed to clip every source.
5. **First test**: Actions tab, run "clipper" manually with `dry_run` set to `1`. Then run it with `0`.

After that it runs every 6 hours by itself and records processed videos in `state.json`.

## Things that will bite you

- **Unaudited API projects upload as private.** YouTube locks videos uploaded through API projects created after July 2020 to private until the project passes an API compliance audit (request it via the YouTube API Services audit form). Until then, uploads need to be made public by hand.
- **Quota**: the default 10,000 units/day covers about 6 uploads (1,600 units each). `max_uploads_per_day` is set to 5.
- **GitHub IPs are often blocked by YouTube.** If downloads fail with a bot-check error, add `YT_COOKIES`, or run the script on a cheap VPS or home machine with a cron job instead.
- **Rights**: clipping other people's content without permission leads to strikes and demonetisation. Own channel, written permission, or licensed material only.
- **Income**: Shorts monetisation needs 1,000 subscribers and 10M Shorts views in 90 days (or the lower tier of 500 subscribers plus other thresholds). Expect a long ramp before any revenue.

## Tuning

`config.yaml` controls clip length, clips per video, Whisper model size, privacy, tags and description. To reprocess a video, remove its ID from `state.json`.
