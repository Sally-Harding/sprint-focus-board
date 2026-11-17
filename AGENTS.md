# AGENTS.md — install runbook for AI agents

This file is for an AI agent (Claude Code, Codex, Copilot, Cursor or similar) that a user asked to
**install Sprint Focus Board on their machine**. Humans: see [README.md](./README.md).

**Your goal:** the board runs on the user's computer at `http://localhost:<port>`, shows the user's
active sprint, and you have verified it.

**Who does what:**

- **You** check the machine, get the code, connect it to Jira, set the few things the board cannot
  ask about (reviewer fields, language, time zone, workflow statuses), start it and verify it.
- **The board's setup wizard** asks the user, in the browser, for the sprint, the team, the
  channels, the column prompts and the refresh rate. It opens by itself on the first start and
  saves to `settings.json`. Do not answer it for the user unless they ask you to and give you the
  answers.

Ask the user only for what you cannot find out yourself, offer a default with every question, and
accept "defaults" as an answer.

## Hard rules

1. **Never ask for the API token in chat.** Create `.env` with the site and email filled in and an
   empty `JIRA_API_TOKEN=`, then ask the user to paste the token into that file themselves. If the
   user pastes a token into the chat anyway, write it only to `.env`, never repeat it, and tell them
   they may want to revoke it and create a new one, because it is now in the chat history.
2. **Never put the token on a command line**, in `config.jsonc`, in a script, or in a commit. The
   board reads it from `.env` or the environment; `config.jsonc` refuses an `apiToken` key.
3. **Jira is read-only for you.** Use only the board's helper commands below. Do not create, edit or
   transition anything in Jira.
4. **Do not edit `board.py` or `board.html`** to fit the user's workflow. Team-specific behaviour goes
   into `config.jsonc` or the setup wizard. If something cannot be expressed there, tell the user.
5. **Ask before installing software** (Python, git, `tzdata`) and before creating launchers,
   shortcuts or autostart entries.
6. `.env`, `config.jsonc` and `settings.json` are git-ignored. Keep them that way; never commit them.

Use the user's shell. On Windows that is usually PowerShell: use `py -3` or `python`, and
`Copy-Item` instead of `cp`. Commands below work in any shell unless marked.

## Step 1 — Preflight

- Python ≥ 3.10: `python --version` (Windows: `py -3 --version`; macOS/Linux: `python3 --version`).
  If it is missing or older, tell the user and offer the install (python.org, `winget install Python.Python.3.12`,
  `brew install python`). Use whichever command works as `python` below.
- git: `git --version`. Without git, download the ZIP from
  `https://github.com/edgeetech/sprint-focus-board/archive/refs/heads/main.zip`.

## Step 2 — Get the code

Ask: **"Where should I put it?"** Default: a `sprint-focus-board` folder in their home directory.
Skip this if the user already downloaded the repository and you are in it.

```bash
git clone https://github.com/edgeetech/sprint-focus-board.git <folder>
cd <folder>
```

## Step 3 — Prove the machine can run it (demo, no Jira)

Start `python demo/demo.py --port 8765` in the background, then request
`http://localhost:8765/api/board`: it must return HTTP 200 with a JSON `rows` list. Stop the demo.
If the port is taken (`Cannot listen on 127.0.0.1:8765`), use another and remember it. This rules
out Python, firewall and browser problems before any Jira question.

## Step 4 — Connect to Jira (ask, batch 1)

Ask in one message:

1. **Jira site URL** — `https://<site>.atlassian.net` (Jira Cloud). Required.
2. **Atlassian account email** for that site. Required.

Then create `.env` from `.env.example` with those two values and `JIRA_API_TOKEN=` left empty, and
tell the user:

> Create an API token at https://id.atlassian.com/manage-profile/security/api-tokens
> (*Create API token*), paste it after `JIRA_API_TOKEN=` in `<folder>/.env`, save, and tell me when
> it is done. Please do not paste it here.

Open the file for them if your environment can. When they confirm, run:

```bash
python board.py --list-boards
```

- `Jira returned HTTP 401` → the email or token is wrong; ask the user to check `.env`.
- `Cannot reach Jira` / `HTTP 404` → the site URL is wrong.
- A list of boards → the connection works.

## Step 5 — What the wizard cannot ask (ask, batch 2)

1. **Reviewer roles.** Run `python board.py --list-fields` and ask which user-picker fields, if any,
   hold the people who review or approve after the assignee, in hand-off order (for example
   "Approver", then "Merge Approver"). Default: none; the board works without them.
2. **Language** of the page: `en` or `tr`. Default: `en`.
3. **Port.** Default: `8765` (or the one that worked in step 3).

Write `config.jsonc` with only these keys (start from `config.example.jsonc`; leave `board`,
`team`, `channels`, `prompts` and `ui.refreshSeconds` out, the wizard owns them):

```jsonc
{
  "reviewerRoles": [
    { "id": "approver", "field": "customfield_10001", "label": "Approver" }
  ],
  "ui": { "defaultLanguage": "en" },
  "server": { "port": 8765 }
}
```

## Step 6 — Start the board and hand the wizard to the user

1. Start `python board.py` in the background (or in a terminal the user keeps open). With no board
   set it prints `No board is set yet: open http://localhost:<port> and finish the setup there.`
2. Open `http://localhost:<port>` for the user (their browser, or yours if you have a browser tool
   and they want you to drive), and tell them what the five steps ask:
   - **Sprint**: search their board, pick its active sprint, and check the "Later sprints" prefix.
     It keeps the board on the right sprint when the next one starts; it matters when a board runs
     several active sprints at once.
   - **Team**: the people in the person picker. Suggestions come from the sprint; anyone else can be
     found by name. Empty means "everyone in the sprint".
   - **Channels** (optional): filter chips for work streams. The wizard lists the sprint's epics,
     labels and components with counts, accepts title keywords, and shows live how many tickets each
     channel and "Other" get. "No channels" hides the chips.
   - **Prompts**: the text each card's Prompt button copies, per column and per language. Here the
     user can point the prompts at their own commands or skills.
   - **Refresh**: how often the page reloads from Jira. Default: 1 minute.
3. Wait until the user says they saved. The page then shows the board.

If the user wants you to set channels or prompts from the chat instead, write them with the wizard's
format into `config.jsonc` (see `config.example.jsonc`) before they open the wizard. Once the wizard
saves, `settings.json` wins over `config.jsonc` for those keys.

## Step 7 — Fit the workflow (ask, batch 3)

Run:

```bash
python board.py --check
```

It prints the active sprint, every status in it with the column it lands in, how often each reviewer
field is filled, the issue types seen, the channel candidates and how the channels match. Show the
user the result and ask:

1. **Statuses.** For each status marked `NOT SHOWN`, which column should show it, if any? Should any
   default column be removed or renamed? Default: add the missing statuses to the closest column's
   `statuses` list.
2. **Workable issue types** for the To Do column. Default: `Sub-task`, `Bug`, `Spike`; suggest adding
   `Task` if `--check` shows it.
3. **Time zone** for the burndown days. Default: `local`. If they name an IANA zone on Windows, either
   offer `python -m pip install tzdata` or set `burndown.timezoneFallback` (an offset plus `eu`/`us`).
4. **Daily burndown target** in hours. Default: `6`. Turn the burndown off? Default: on.

Add the answers to `config.jsonc` (`columns`, `burndown`; see the comments in
`config.example.jsonc`; column `rule` types are `unassigned`, `assignee`, `involved`, `role`, `any`).
If you change `columns`, give every column a `statuses` list and keep the column ids the wizard's
prompts refer to. Run `--check` again until it prints `OK` without unshown statuses the user cares
about, then restart the board: config file changes need a restart, wizard changes do not.

## Step 8 — Verify

1. Startup prints no `Config error:` and no `No board is set yet`.
2. `http://localhost:<port>/api/board`: HTTP 200, `sprint.name` is the expected sprint, `rows` is not
   empty.
3. If the burndown is on, `http://localhost:<port>/api/burn?scope=sprint`: HTTP 200 and no `error` key.
   The first call may take a few seconds on a large sprint.
4. In the browser (yours or the user's): the header shows the user's name and the sprint, cards are in
   the expected columns, the person picker shows the team, the channel chips match the wizard, and the
   Burndown Contribution button shows a number rather than `–`.

| Symptom | Fix |
| --- | --- |
| `Config error: Missing JIRA_API_TOKEN` | `.env` is not next to `board.py` or the token line is empty. |
| `No active sprint on board N` | The sprint is not started, or the wizard's prefix does not match; reopen Settings. |
| `Time zone '…' is not installed` | `python -m pip install tzdata`, a `timezoneFallback`, or `"local"`. |
| `Cannot listen on 127.0.0.1:<port>` | The port is taken; pick another with `--port`. |
| `settings.json is not valid JSON` | Fix the file, or delete it to run the wizard again. |
| A column is empty but should not be | `--check` shows the status → column mapping; add the status to that column. |
| A reviewer never shows in the chain | The `reviewerRoles[].field` id is wrong; compare with `--list-fields`. |

## Step 9 — Hand over

Ask whether they want a launcher (for example a `start-board.ps1` or `start-board.sh` that runs
`python board.py --open` in the install folder). Create one only if they say yes.

Finish with a short summary for the user:

- the URL (`http://localhost:<port>`) and that it only listens on this computer;
- how to start it again (`python board.py --open` in `<folder>`) and stop it (Ctrl+C);
- **Settings** in the page header reopens the wizard (sprint, team, channels, prompts, refresh);
- where things live: `<folder>/.env` (credentials), `<folder>/config.jsonc` (your settings),
  `<folder>/settings.json` (the wizard's);
- how to update (`git pull` in `<folder>`, then restart);
- anything you left at a default they might want to change.

**Done means:** the server is running, `/api/board` answered 200 with the user's active sprint, and
the user or your browser tool has seen the board after the wizard.

## For agents changing the code

- No third-party dependencies; Python ≥ 3.10 standard library only. The page is one HTML file with
  no build step.
- Team-specific behaviour belongs in the config or the wizard. Keep the defaults neutral.
- Run `python -m unittest discover -s tests` and try the change against `python demo/demo.py`
  (`--first-run` starts it without a board, so the wizard opens).
- Retake the README screenshots from the demo if the UI changes.
