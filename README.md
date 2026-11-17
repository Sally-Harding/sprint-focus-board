# Sprint Focus Board

A small local web board that shows **one person's slice of a Jira Cloud active sprint**: what they can pick up, what they are working on, what waits for their review, and how much Remaining Estimate they burned today.

It is one Python file and one HTML page. It runs on your machine, talks to Jira with your own API token, and serves the page on `127.0.0.1` only.

## What it shows

- **Columns per status**, filtered for the person picked in the header. With the default rules:
  - *To Do*: unassigned work of the workable types (Sub-task, Bug, Spike)
  - *Reopened*, *In Progress*: the person's own work
  - *Blocked*: tickets where the person is the assignee or a reviewer
  - *To Review*, *In Review*, *For Merge Approval*: tickets waiting for a reviewer role that is this person or still empty
- **A person picker**: everybody who is an assignee or a reviewer in the sprint. The board opens on the owner of the API token.
- **A hand-off chain on each card**: Assignee, then each configured reviewer role (for example Approver, then Merge Approver). The stage the ticket is in is highlighted.
- **Channel chips** (optional): filter the board by team stream, defined by epics, labels, components and a summary pattern.
- **A prompt button on each card** that copies a ready-made prompt for an AI coding assistant, built from a per-column template.
- **Burndown contribution**: today's drop in Remaining Estimate made by the person, a sprint-to-date chart, and the changes behind both.
- Search (`/` to focus), hide and restore cards, columns that fold when empty or on request, English and Turkish, priority and issue-type icons, and an optional refresh animation (it follows the system "reduce motion" setting; the Motion button turns it on or off).

## Requirements

- Python 3.10 or newer. No third-party packages.
- A Jira Cloud site and a board with sprints (Scrum board).
- Optional: `tzdata` (`python -m pip install tzdata`) if you name an IANA time zone on Windows. See [Time zones](#time-zones).

## Setup

1. **Get the code**

   ```bash
   git clone https://github.com/edgeetech/sprint-focus-board.git
   cd sprint-focus-board
   ```

2. **Create a Jira API token**

   Open <https://id.atlassian.com/manage-profile/security/api-tokens>, choose *Create API token*, give it a name and copy it. The board uses it with Basic auth (email + token) and only reads data.

3. **Store the credentials**

   Copy `.env.example` to `.env` and fill it in. Git ignores `.env`.

   ```ini
   JIRA_BASE_URL=https://your-site.atlassian.net
   JIRA_EMAIL=you@your-company.com
   JIRA_API_TOKEN=your-token
   ```

   Real environment variables win over `.env`. You can point at another file with `--env-file PATH` or `BOARD_ENV_FILE`. `JIRA_URL` and `JIRA_USERNAME` are accepted as aliases. The token is never read from the config file and never sent to the page.

4. **Configure the board**

   Copy `config.example.jsonc` to `config.jsonc` (git ignores it) and set at least `board.id`, the number in your board URL (`.../boards/12`). Everything else has a default. See [Configuration](#configuration).

5. **Run it**

   ```bash
   python board.py --open
   ```

   The board is at <http://localhost:8765>. Options: `--port`, `--config PATH` (or `BOARD_CONFIG`), `--board ID`, `--sprint-prefix TEXT`, `--open`.

## Configuration

The config file is JSON with `//` and `/* */` comments. `config.example.jsonc` documents every option; this is the overview.

| Key | What it controls |
| --- | --- |
| `jira.baseUrl`, `jira.email` | Site and account, if you prefer them in the file. The environment wins. |
| `board.id` | The board whose active sprint is shown. Required. |
| `board.sprintNamePrefix` | Pick the active sprint whose name starts with this text, for boards that run several sprints at once. |
| `board.ignoreStatusSuffixes` | Suffixes stripped from status names, such as `(migrated)`. |
| `server.port` | Port on 127.0.0.1 (default 8765). |
| `ui.defaultLanguage`, `ui.refreshSeconds` | `en` or `tr`; how often the page reloads the board. |
| `reviewerRoles` | 0..n user-picker fields that form the hand-off chain, each with an `id`, a `field` id and a `label`. |
| `columns` | The columns, their statuses, issue-type filters, rule, colour, description and prompt templates. |
| `channels` | Optional chips; each channel lists `epics`, `labels`, `components` (glob patterns) and a `summary` regex. |
| `priorities` | Priority name to level 1..5 for the icons and the sort order. |
| `burndown` | Time zone, daily target, colour thresholds, weekend days, excluded types and the optional estimate-comment comparison. |

### Column rules

A ticket goes to the first column whose statuses and issue types match and whose rule holds for the person picked in the header.

| Rule | Shows a ticket when |
| --- | --- |
| `unassigned` | nobody is assigned |
| `assignee` | the person is the assignee |
| `involved` | the person is the assignee or holds any reviewer role (or the roles listed in `roles`) |
| `role` | the person holds `role`; when the role is empty, anyone may take it (`whenEmpty: "anyone"`) except people in `emptyExcludes`, or nobody (`whenEmpty: "nobody"`). `notAssignee: true` hides the person's own tickets. |
| `any` | always |

With no reviewer roles configured, every role counts as empty: the review columns then show other people's tickets in those statuses, and the hand-off chain shows the assignee only.

### Prompts

Each column has a prompt template, in one language or per language (`{ "en": "...", "tr": "..." }`). `promptIfNotAssignee` is used when the picked person is not the assignee. Placeholders: `{key}`, `{summary}`, `{url}`, `{type}`, `{status}`, `{parentKey}`, `{parentSummary}`, `{assignee}`. The built-in templates are neutral and assume no particular tool; replace them with your own workflow.

## How the burndown is calculated

The **burndown contribution** of a person on a day is the sum of the drops in **Remaining Estimate** (the `timeestimate` field) that this person made on that day, read from each ticket's changelog.

- **Who**: the author of the changelog entry. A drop counts in full for the person who made it, whoever the assignee is.
- **When**: the local day of the change, in the configured time zone. When the drop comes from logging work (the changelog entry carries a `WorklogId`), it counts on the worklog's *started* day instead of the day it was entered.
- **Increases** are listed as "estimate raised" and are not subtracted.
- **Scope**: tickets in the active sprint updated since the day before the range, without the `burndown.excludeTypes` (Story and Epic by default, whose estimates usually roll up from sub-tasks).
- **Done with time left**: a move to a done status that leaves Remaining Estimate is listed for reference. It does not count, because no one burned that time.

The button shows today's total against `burndown.dailyTargetHours` and turns red below `thresholds.low`, amber below `thresholds.mid`, and green above. The popover adds the sprint-to-date chart (working days from the sprint start to today, against target × working days) and the list of changes.

### Optional: estimate-comment comparison

Teams that write an estimate comment such as `Dev: 4h | Rev: 1h | MR: 30m` can turn on `burndown.estimateComment`. Each configured stage is a status move (for example *In Review* to *For Merge Approval*). The first time a ticket makes that move, the person who made it is credited with that stage's hours, if the move is made by the right person (`by`: the assignee, or anyone else). The newest comment with the full line wins; without one, the newest single `Rev: 1h`-style values are used. This figure is shown next to the main one for comparison only. The module is off by default and costs one extra Jira call per ticket.

### Time zones

`burndown.timezone` accepts `local` (this computer, the default), `UTC`, a fixed offset such as `+02:00`, or an IANA name such as `Europe/Berlin`. IANA names need a time zone database. Linux and macOS have one; Windows does not, so either run `python -m pip install tzdata` or set `burndown.timezoneFallback` to a standard offset plus the `eu` or `us` daylight-saving rule. Without either, the board stops at startup and says so.

## How it talks to Jira

- `GET /rest/api/3/myself`, `/rest/agile/1.0/board/{id}/sprint?state=active`, `/rest/api/3/search/jql` for the board.
- Per ticket, for the burndown: the changelog, the worklogs (only when the changelog has worklog entries) and, if the estimate-comment module is on, the comments. Results are cached per ticket until the ticket's `updated` time changes.
- At most 8 Jira connections at a time; concurrent requests for the same data wait for the first one; a timed-out or 5xx request is retried once, and a 429 after its `Retry-After` (up to 10 s).
- Board data is cached for 20 seconds. `?force=1` on the API, or the Refresh button, skips the cache. The sprint burndown is warmed up in the background at startup.

Local endpoints: `/` (the page), `/api/board`, `/api/burn` (`?day=YYYY-MM-DD`, `?scope=sprint`, `?force=1`) and `/icon?u=` (a proxy for issue-type icons on your Jira site, which need authentication).

## Security

- The server listens on `127.0.0.1` only and answers only to `localhost` / `127.0.0.1` host names, which blocks DNS-rebinding pages.
- The API token stays in the Python process. The page gets issue data, never credentials.
- The icon proxy only fetches URLs on your own Jira site.
- Anyone who can use your computer's loopback interface can read the board while it runs; stop it when you do not need it.

## Development

```bash
python -m unittest discover -s tests
```

The page is plain HTML, CSS and JavaScript with no build step. `board.py` reloads `board.html` on every request, so page edits show on refresh; config changes need a restart.

## Licence

MIT. See [LICENSE](LICENSE).

Jira is a trademark of Atlassian. This project is not affiliated with or endorsed by Atlassian.
