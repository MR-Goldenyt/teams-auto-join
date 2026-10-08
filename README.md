# teams-auto-join

Joins a Microsoft Teams meeting **with your own signed-in account**, in a real **Chrome / Edge / Brave / Firefox window** (your system default browser unless you choose otherwise), **muted with the camera off**, at an exact time or at a random moment inside a window. Right after you're in, it saves a screenshot named after the join time, stays for **2 hours**, and takes another one **every 20 minutes**, plus a last one just before leaving:

```
screenshots/
└── 2026-10-08/
    ├── 09-58-41.png        # HH-MM-SS of the moment you got in
    ├── 10-18-43.png        # +20 min
    ├── 10-38-45.png        # +40 min
    └── ...                 # up to +120 min, then it leaves
```
Change it with `STAY_MINUTES` / `SCREENSHOT_INTERVAL_MINUTES` (or `--stay-minutes` / `--screenshot-interval`).

Only use it for meetings you were invited to.

## Setup (once)

```bash
uv sync
uv run teams-join --login      # pick your browser profile, sign in to Teams (incl. MFA), press ENTER in the terminal
cp .env.example .env           # optional: lets you skip all terminal input
```

### Which browser is used?
`BROWSER=auto` (default, or `--browser auto`) reads your **operating system's default browser** (Windows registry, macOS Launch Services, Linux `xdg-settings`) and uses it. Supported: **Chrome, Edge, Brave, Firefox**. If your default is something else (Safari, Opera...) it falls back to the first installed one of Chrome, Brave, Edge, Firefox. Force one with `BROWSER=brave` or `--browser firefox`.

### Which profile is used?
The script lists **your existing profiles** of that browser (e.g. `Person 1 (you@gmail.com)`, `Work (you@company.com)`) and asks which one to use. Skip the question with `--profile "Work"` / `--profile "Profile 1"` / `BROWSER_PROFILE=...`; `--profile ask` asks again. The answer is remembered.

The chosen profile is **copied** into the project folder, `browser-profiles/<browser>/`, and the automation runs on the copy (Chrome/Edge/Brave refuse to be automated on their real data folder). Your real profile is only read. Change the location with `PROFILE_STORE=...` or `--profile-store`. **That folder contains your login cookies: it is in `.gitignore`; never commit, zip or share it.** For the freshest copy, close that browser while the first copy is made, and use `--refresh-profile` to copy again later.

**If you are not signed in to Teams in that profile** (or the copied session does not carry over - browsers encrypt cookies, so this can happen), run `uv run teams-join --login`: the window opens with your profile, you sign in, then press **ENTER** in the terminal. It is saved in the copy and reused from then on.

**Firefox note:** Playwright can only drive its own patched Firefox build, not the Firefox installed on your system. It is downloaded automatically the first time (or run `uv run playwright install firefox`) and uses its own profile, so it won't touch your everyday Firefox (your chosen profile is copied into it the same way). Chrome, Edge and Brave use your installed browser.

## Run

```bash
# everything from the terminal
uv run teams-join "https://teams.microsoft.com/meet/344131857453033?p=uwDbrDS36frSBiGsBB" --window 09:55-10:05

# exact time
uv run teams-join "<link>" --join-at 14:30

# everything from .env / or it asks you for what is missing
uv run teams-join

# just show what would happen
uv run teams-join --dry-run
```

Precedence: **CLI flag > real env var > `.env` > terminal prompt > default.** `uv run teams-join --help` lists everything.
Links expire, so give a fresh one each time (argument or `MEETING_URL`). You can paste a whole invite text, an Outlook Safe Links URL, a `launcher.html` link or an `msteams:` link.

### When to join
| Input | Meaning |
|---|---|
| `--window 09:55-10:05` | random second inside the window (overnight like `23:55-00:05` works) |
| `--join-at 14:30` / `2:30pm` / `14:30:15` | exact time today |
| `--join-at "2026-10-09 14:30"` | exact date and time |
| `--join-at +10m` | relative (`s`, `m`, `h`) |
| `--now` | immediately |

## How the "Open Microsoft Teams?" popup is avoided
That popup is Chrome's own UI (not part of the page), so a script can't click it. It appears when the link goes through `teams.microsoft.com/dl/launcher/launcher.html`. The script rewrites every link (including launcher links) to open the **web app directly** (`teams.microsoft.com/v2/?meetingjoin=true#/meet/...`), which skips that page. The original link is only a fallback; if the popup still shows up, press **Cancel**.

## Edge cases handled

| Situation | Behavior |
|---|---|
| Link has quotes, `<>`, trailing `.`, whole invite text, Safe Links, `msteams:`, launcher URL, `anon=true` | cleaned, extracted, converted to a direct web link (guest flag removed) |
| Not a Teams link / not a meeting link | rejected before anything starts |
| Not signed in yet / session expired / account picker / MFA | the window waits for you to sign in (up to 5 min); the preflight catches it **before** the long wait |
| Profile is not signed in and the page asks for a guest name | stops with instructions (`REQUIRE_LOGIN=false` allows guest join) |
| Wrong account / no permission for this meeting | detected, clear message |
| Link expired / meeting ended / meeting gone | detected, clear message, no pointless retries, debug screenshot saved |
| Time already passed (< `GRACE_MINUTES`) | joins immediately |
| Time passed long ago | error, or tomorrow if `ROLL_OVER=true` |
| Window already open | random time picked from what is left of it |
| Laptop sleeps during the wait | wall clock re-checked every few seconds; joins right after waking |
| Default browser unsupported / not installed | falls back to Chrome, Brave, Edge, Firefox, then Playwright Chromium (auto-installed) |
| Automation profile already open in another window / second copy running | clear error telling you to close it |
| Browser has no profiles yet | an empty dedicated profile is used |
| You close the browser window by hand | detected; the script ends cleanly |
| Cookie / "Got it" / "Continue without audio or video" popups | dismissed |
| Real mic and webcam | never touched (virtual devices only) |
| Mic/camera can't be verified OFF before joining | joins anyway (they are virtual devices) and force-mutes afterwards |
| Mic/camera found live after joining | switched off, re-checked every minute (max 2 corrective clicks per device, so it can never flip back and forth) |
| "Join now" click ignored | clicked again (up to 2 times) |
| Lobby / "meeting hasn't started" | waits up to `LOBBY_TIMEOUT_MINUTES` |
| Denied / removed / meeting full | detected and reported |
| CAPTCHA | you solve it in the window; it waits 5 min |
| Teams "something went wrong" / network blip | retried up to `MAX_ATTEMPTS` with backoff |
| Screenshot fails mid-meeting | logged, the next one is tried on schedule; no catch-up burst after a laptop sleep |
| Screenshot folder not writable / name collision | falls back to a temp folder / adds `_2`, `_3`... never overwrites |
| No display on Linux | clear error (visible browser is required) |
| Ctrl+C / `kill` (once) | clicks **Leave**, closes the browser, exit code 130. A second Ctrl+C force-quits |
| Meeting ends / you get removed while staying | detected, program exits |

Exit codes: `0` joined OK, `1` could not join, `2` bad input, `130` interrupted. Logs: `logs/teams-auto-join.log`.

## Notes
- The browser starts **one tab only**: the blank/new-tab pages a browser opens by itself are closed, and the saved tab session of your copied profile is never restored. The browser sandbox stays on, so there is no `--no-sandbox` warning (set `NO_SANDBOX=true` only on Linux containers / root).
- There is no headless mode anymore: a visible window is more reliable and is needed for sign-in/MFA. Leave the machine awake and don't close that window.
- Teams' web UI changes now and then. If a step breaks, look at the `failed_*.png` screenshot in the date folder and the log; the selectors are at the top of `joiner.py`.
- UI text matching assumes English; the browser is launched with the `en-US` locale. If your Teams account language is another one, set Teams to English.
