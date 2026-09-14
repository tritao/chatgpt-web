# chatgpt-web

Experimental terminal client for an authenticated ChatGPT web session. It uses
a dedicated Chrome instance to capture login credentials, a Node-based Sentinel
signer for per-turn write challenges, and a local Python HTTP client for
conversation reads, writes, and SSE streams.

This uses undocumented ChatGPT web endpoints. It may stop working when the web
application changes.

## Requirements

- Python 3.10 or newer
- `httpx`
- Node.js 22 or newer
- Chrome or Chromium for the initial interactive login

The installed launcher prefers `uv` and reads the PEP 723 dependency metadata
from the executable, creating and caching an isolated environment automatically.
Install manually with `pip install -r requirements.txt` when `uv` is unavailable.

Launch Chrome with a dedicated persistent profile:

```sh
./chatgpt-web/run login
```

The equivalent manual command is:

```sh
profile_dir="${XDG_DATA_HOME:-$HOME/.local/share}/chatgpt-web/chrome-profile"
mkdir -p "$profile_dir"
chmod 700 "$profile_dir"
google-chrome \
  --user-data-dir="$profile_dir" \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  https://chatgpt.com/
```

This profile persists the ChatGPT login across browser and daemon restarts. Do
not open the same profile in another Chrome process. Log in through the Chrome
window and start the daemon with an authenticated command. Chrome can then be
closed while the daemon remains running. Use:

```sh
chatgpt-web
./chatgpt-web/run auth
./chatgpt-web/run list
./chatgpt-web/run list --limit 100
./chatgpt-web/run list --all --output jsonl
./chatgpt-web/run show CONVERSATION_ID
./chatgpt-web/run show CONVERSATION_ID --output jsonl
./chatgpt-web/run new "Explain monads in one paragraph"
./chatgpt-web/run send CONVERSATION_ID "Continue, with an example"
./chatgpt-web/run resume CONVERSATION_ID
./chatgpt-web/run status
./chatgpt-web/run logout
```

Running `chatgpt-web` without a subcommand opens a fullscreen terminal client
with a scrollable, syntax-highlighted Markdown transcript and a multiline
prompt fixed to the bottom. Enter submits, Alt+Enter inserts a newline, Ctrl+C
stops an active response, Ctrl+D exits from an empty prompt, and PageUp/PageDown
scroll the transcript. New output follows the bottom automatically; PageUp
pauses following and End resumes it. `/resume` or Ctrl+R opens a centered,
searchable list of recent conversations; use Up/Down, Enter, and Escape to navigate it. Commands
also include `/new`, `/resume ID`, `/history`, `/clear`, `/help`, and `/exit`.
Typing `/` opens the command menu; use Up/Down and Tab or Enter to complete a
command, then Enter to run it.
`chatgpt-web resume CONVERSATION_ID` opens an existing conversation directly in
the fullscreen client.

Set `CHATGPT_WEB_CDP_URL` or pass `--cdp-url` when Chrome uses another local
debugging port.

The first authenticated command captures the browser session and atomically
saves it to a mode-0600 file at
`${XDG_STATE_HOME:-$HOME/.local/state}/chatgpt-web/session.json`. Later daemon
starts load that file with Chrome stopped. The daemon serves CLI invocations
through a mode-0600 Unix socket below `XDG_RUNTIME_DIR`. On HTTP 401 or 403 it
uses the saved cookies with `/api/auth/session` to refresh the access token. If
the login itself has expired, run `chatgpt-web login` followed by
`chatgpt-web auth --refresh`.

Use `chatgpt-web status` to inspect the daemon, saved-session, Chrome, and send
mode state. `chatgpt-web stop` stops the daemon but preserves credentials.
`chatgpt-web logout` stops it and deletes the saved credential file.

Authentication headers and cookies move directly from the Chrome helper to the
daemon through a pipe. They are never printed. The saved session contains
account credentials and must be protected like a password; the CLI creates its
directory as mode 0700 and the file as mode 0600.

For writes, `sentinel-node-probe.js` runs the current Sentinel proof and `dx`
interpreters in an isolated Node VM. Python calls Sentinel `prepare`, passes the
requirements to Node, attaches the resulting proof headers, constructs the
conversation request, and renders ordinary uncompressed SSE message snapshots.
Conversation reads and final response reconciliation also use the Python HTTP
client. `Ctrl+C` closes the direct response stream. Chrome does not need to be
running for this path once the daemon has captured an authenticated session.

Set `CHATGPT_WEB_SEND_MODE=recipe` when launching the daemon to have Chrome
construct the request while Python transports it. Set it to `chrome` for the
legacy browser-driven sender and DOM streaming path.

## Direct-send investigation

The `chrome-recipe.js` diagnostic established that a captured request can be
replayed with Python/httpx over HTTP/1.1. It remains available as a compatibility
fallback; its request recipe moves over a private pipe and is never persisted.

The frontend currently obtains per-turn values from
`/backend-api/sentinel/chat-requirements/prepare` and
`/backend-api/sentinel/chat-requirements/finalize`. The resulting send carries
the following dynamic headers:

- `openai-sentinel-chat-requirements-prepare-token`
- `openai-sentinel-proof-token`
- `openai-sentinel-turnstile-token`
- an optional `x-conduit-token`

In the verified session, every turn included a large Turnstile token. The
Sentinel SDK consumes its cached requirement/proof state when minting the turn
token and then prepares the next proof. This makes the SDK and browser challenge
the remaining browser dependency; copying a static set of headers is not a
stable browserless implementation.

`sentinel-node-probe.js` downloads the current Sentinel SDK and evaluates it in
an isolated Node VM with SDK network access disabled. The SDK's proof-of-work
engine and `turnstile.dx` bytecode interpreter run under the supplied Web API
shims. A real prepare response was successfully solved in Node, accepted by the
finalize endpoint, and accepted by the conversation endpoint through the
prepared three-header flow. Pass a captured requirements object through
`--requirements-stdin` to inspect compatibility without writing it to disk.

The SDK implementation is cached below
`${XDG_CACHE_HOME:-$HOME/.cache}/chatgpt-web/sentinel`. Cache files and their
manifest are mode 0600 inside a mode-0700 directory. Each load verifies the
recorded SHA-256. The bootstrap is checked at most once every six hours; a
verified stale copy remains usable when the asset host is unavailable. If a new
SDK no longer matches the supported proof/VM signatures, the CLI uses recipe
mode when an authenticated Chrome is already running, or asks the user to run
`chatgpt-web login` for that compatibility fallback.

The client does not retry HTTP 429 responses or unavailable composers. It
reports the rate limit and leaves the next attempt to the user, avoiding a
retry loop that could extend the limit.
