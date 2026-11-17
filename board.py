#!/usr/bin/env python3
"""Sprint Focus Board: a local board that shows one person's slice of a Jira Cloud active sprint.

Run:   python board.py [--config PATH] [--port 8765] [--open]
Open:  http://localhost:8765

Credentials come from environment variables, or from a .env file next to this script:
  JIRA_BASE_URL   https://your-site.atlassian.net
  JIRA_EMAIL      the email of the Atlassian account
  JIRA_API_TOKEN  an API token for that account
They stay in this process; the page never sees them. See README.md and config.example.jsonc.
"""
import argparse
import base64
import concurrent.futures
import copy
import datetime
import fnmatch
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_FILE = os.path.join(HERE, "board.html")
CACHE_SECONDS = 20
JIRA_CONNECTIONS = 8
UTC = datetime.timezone.utc
RULE_TYPES = ("unassigned", "assignee", "involved", "role", "any")
ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------------------------------------
# Defaults. A config file only needs the keys it changes; see config.example.jsonc.

PROMPTS = {
    "todo": {
        "en": "Pick up {key}: {summary}\n{url}\n\nRead the ticket and its acceptance criteria, then plan the change, implement it with tests "
              "and open a pull request. Show me the plan before you start, and a draft before you post a comment or change the ticket status.",
        "tr": "{key} işini al: {summary}\n{url}\n\nTicket'ı ve kabul kriterlerini oku; değişikliği planla, testleriyle uygula ve pull request aç. "
              "Başlamadan önce planı, yorum yazmadan ya da ticket durumunu değiştirmeden önce taslağı göster.",
    },
    "reopened": {
        "en": "{key} was reopened: {summary}\n{url}\n\nRead the latest comments to find out why it was reopened and what is missing or wrong. "
              "Fix it, then update the pull request. Show me a draft before you post a comment or change the ticket status.",
        "tr": "{key} yeniden açıldı: {summary}\n{url}\n\nNeden yeniden açıldığını, neyin eksik ya da yanlış olduğunu son yorumlardan çıkar. "
              "Düzelt ve pull request'i güncelle. Yorum yazmadan ya da ticket durumunu değiştirmeden önce taslağı göster.",
    },
    "progress": {
        "en": "Continue {key}: {summary}\n{url}\n\nWork out where it stands: branches, pull request, CI and reviews. Then finish what is missing. "
              "Show me a draft before you post a comment or change the ticket status.",
        "tr": "{key} işine devam et: {summary}\n{url}\n\nDurumu çıkar: branch'ler, pull request, CI ve review'lar. Sonra eksikleri tamamla. "
              "Yorum yazmadan ya da ticket durumunu değiştirmeden önce taslağı göster.",
    },
    "blocked": {
        "en": "{key} is blocked: {summary}\n{url}\n\nRead the blocking links and the latest comments, and check whether the blocker is resolved. "
              "If it is, continue the work. If not, summarise who we are waiting for and what is needed.",
        "tr": "{key} bloke: {summary}\n{url}\n\nBloke eden bağlantıları ve son yorumları oku, engel çözüldü mü kontrol et. "
              "Çözüldüyse işe devam et. Çözülmediyse kimi beklediğimizi ve ne gerektiğini özetle.",
    },
    "blockedReviewer": {
        "en": "{key} is blocked and I am one of its reviewers: {summary}\n{url}\n\nRead the blocking links and the latest comments, and check "
              "whether the blocker is resolved. Summarise whether it affects the review order and whether anything is expected from me. "
              "Read only: do not change code.",
        "tr": "{key} bloke ve ben reviewer'larından biriyim: {summary}\n{url}\n\nBloke eden bağlantıları ve son yorumları oku, engel çözüldü mü "
              "kontrol et. Review sırasını etkiliyor mu, benden bir şey bekleniyor mu, özetle. Sadece oku: koda dokunma.",
    },
    "toreview": {
        "en": "Review {key}: {summary}\n{url}\n\nRead the ticket and its acceptance criteria, then review the pull request: correctness, tests, "
              "CI status and open threads. Read only: do not change code. Summarise the findings and show me a draft before you comment anywhere.",
        "tr": "{key} işini review et: {summary}\n{url}\n\nTicket'ı ve kabul kriterlerini oku, sonra pull request'i incele: doğruluk, testler, "
              "CI durumu ve açık thread'ler. Sadece oku: koda dokunma. Bulguları özetle; bir yere yorum yazmadan önce taslağı göster.",
    },
    "inreview": {
        "en": "Continue reviewing {key}: {summary}\n{url}\n\nI am already its reviewer. Check what changed since the last review, the CI status "
              "and open threads. Read only: do not change code. Summarise the findings and show me a draft before you comment anywhere.",
        "tr": "{key} review'una devam et: {summary}\n{url}\n\nReviewer zaten benim. Son review'dan beri neyin değiştiğine, CI durumuna ve açık "
              "thread'lere bak. Sadece oku: koda dokunma. Bulguları özetle; bir yere yorum yazmadan önce taslağı göster.",
    },
    "merge": {
        "en": "Final review of {key} before merge: {summary}\n{url}\n\nCheck the logic against the acceptance criteria and look for gaps. Confirm "
              "that CI is green and no review threads are open. Do not merge; report the outcome and show me a draft before you comment anywhere.",
        "tr": "{key} için merge öncesi son review: {summary}\n{url}\n\nMantığı kabul kriterlerine göre kontrol et, boşlukları ara. CI yeşil mi, "
              "açık review thread'i var mı doğrula. Merge etme; sonucu bildir ve bir yere yorum yazmadan önce taslağı göster.",
    },
    "generic": {
        "en": "Look at {key}: {summary}\n{url}\n\nIt is in {status}. Work out what it needs from me next and summarise that before you change anything.",
        "tr": "{key} işine bak: {summary}\n{url}\n\nDurumu: {status}. Benden sıradaki beklentiyi çıkar ve bir şey değiştirmeden önce özetle.",
    },
}

DEFAULT_COLUMNS = [
    {"id": "todo", "name": "To Do", "color": "#8A93A6", "rule": {"type": "unassigned"}, "types": ["Sub-task", "Bug", "Spike"]},
    {"id": "reopened", "name": "Reopened", "color": "#E5484D", "rule": {"type": "assignee"}},
    {"id": "progress", "name": "In Progress", "color": "#2B8CFF", "rule": {"type": "assignee"}},
    {"id": "blocked", "name": "Blocked", "color": "#D9822B", "rule": {"type": "involved"}},
    {"id": "toreview", "name": "To Review", "color": "#7A5AF8", "excludeTypes": ["Story"],
     "rule": {"type": "role", "role": "approver", "notAssignee": True}},
    {"id": "inreview", "name": "In Review", "color": "#5B6BFF", "excludeTypes": ["Story"],
     "rule": {"type": "role", "role": "approver"}},
    {"id": "merge", "name": "For Merge Approval", "color": "#12A594", "excludeTypes": ["Story"],
     "rule": {"type": "role", "role": "mergeApprover", "notAssignee": True, "emptyExcludes": ["approver"]}},
]

DEFAULT_STAGES = [
    {"id": "dev", "label": "Dev", "from": "In Progress", "to": "To Review", "by": "assignee"},
    {"id": "rev", "label": "Rev", "from": "In Review", "to": "For Merge Approval", "by": "others"},
    {"id": "mr", "label": "MR", "from": "For Merge Approval", "to": "Done", "by": "others"},
]

DEFAULTS = {
    "jira": {"baseUrl": "", "email": ""},
    "board": {"id": None, "sprintNamePrefix": "", "ignoreStatusSuffixes": ["(migrated)"]},
    "server": {"port": 8765},
    "ui": {"defaultLanguage": "en", "refreshSeconds": 60},
    "reviewerRoles": [],
    "columns": DEFAULT_COLUMNS,
    "channels": [],
    "priorities": {"highest": 1, "blocker": 1, "critical": 1, "high": 2, "major": 2, "medium": 3,
                   "low": 4, "minor": 4, "lowest": 5, "trivial": 5},
    "burndown": {
        "enabled": True,
        "timezone": "local",
        "timezoneFallback": None,
        "dailyTargetHours": 6,
        "thresholds": {"low": 2, "mid": 4},
        "weekendDays": [6, 7],
        "excludeTypes": ["Story", "Epic"],
        "doneStatuses": ["Done"],
        "estimateComment": {"enabled": False, "stages": DEFAULT_STAGES},
    },
}


# ---------------------------------------------------------------------------------------------
# Config files: JSON with // and /* */ comments.

def strip_comments(text):
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def read_config(path=None):
    """Return (merged config, path used). An explicit path that does not exist is an error."""
    explicit = path or os.environ.get("BOARD_CONFIG")
    candidates = [explicit] if explicit else [os.path.join(HERE, "config.jsonc"), os.path.join(HERE, "config.json")]
    for p in candidates:
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8-sig") as fh:
                    user = json.loads(strip_comments(fh.read()))
            except json.JSONDecodeError as ex:
                raise ConfigError(f"{p} is not valid JSON (line {ex.lineno}, column {ex.colno}): {ex.msg}")
            if not isinstance(user, dict):
                raise ConfigError(f"{p} must hold a JSON object")
            if "apiToken" in (user.get("jira") or {}):
                raise ConfigError("Do not put the API token in the config file. Set JIRA_API_TOKEN in the environment or in .env.")
            return deep_merge(DEFAULTS, user), p
    if explicit:
        raise ConfigError(f"Config file not found: {explicit}")
    return copy.deepcopy(DEFAULTS), None


def load_env_file(path):
    """Read KEY=VALUE lines into os.environ without overriding variables that are already set."""
    if not path or not os.path.isfile(path):
        return False
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k.startswith("export "):
                k = k[7:].strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            os.environ.setdefault(k, v)
    return True


# ---------------------------------------------------------------------------------------------
# Time zones. zoneinfo needs a tz database; Windows has none unless the tzdata package is installed.

def parse_offset(text):
    m = re.fullmatch(r"(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?", (text or "").strip(), re.I)
    if not m:
        return None
    delta = datetime.timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
    return -delta if m.group(1) == "-" else delta


def _sunday(year, month, nth):
    """nth Sunday of a month (1-based), or the last one when nth is -1."""
    if nth == -1:
        d = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1) if month < 12 else datetime.date(year, 12, 31)
        return d - datetime.timedelta(days=(d.weekday() + 1) % 7)
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=nth - 1)


def _at(day, hour, minus):
    return datetime.datetime(day.year, day.month, day.day, hour, tzinfo=UTC) - minus


def rule_clock(std, dst):
    """A fixed standard offset plus an optional EU or US daylight-saving rule."""
    hour = datetime.timedelta(hours=1)

    def in_dst(utc):
        y = utc.year
        if dst == "eu":  # 01:00 UTC, last Sunday of March to last Sunday of October
            return _at(_sunday(y, 3, -1), 1, datetime.timedelta(0)) <= utc < _at(_sunday(y, 10, -1), 1, datetime.timedelta(0))
        if dst == "us":  # 02:00 local, second Sunday of March to first Sunday of November
            return _at(_sunday(y, 3, 2), 2, std) <= utc < _at(_sunday(y, 11, 1), 2, std + hour)
        return False

    def to_local(utc):
        off = std + (hour if in_dst(utc) else datetime.timedelta(0))
        return utc.astimezone(datetime.timezone(off))
    return to_local


def make_clock(name, fallback=None):
    """Return a function that turns an aware UTC datetime into the configured local time."""
    name = (name or "local").strip()
    if name.lower() == "local":
        return lambda utc: utc.astimezone()
    if name.upper() in ("UTC", "GMT", "Z"):
        return lambda utc: utc.astimezone(UTC)
    off = parse_offset(name)
    if off is not None:
        tz = datetime.timezone(off)
        return lambda utc: utc.astimezone(tz)
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(name)
        return lambda utc: utc.astimezone(tz)
    except Exception:
        pass
    if fallback:
        std = parse_offset(str(fallback.get("utcOffset", "")))
        dst = str(fallback.get("dst", "none")).lower()
        if std is None or dst not in ("eu", "us", "none"):
            raise ConfigError('burndown.timezoneFallback needs "utcOffset" like "+01:00" and "dst" of "eu", "us" or "none"')
        print(f"Time zone {name!r} is not installed; using the fallback {fallback.get('utcOffset')} with the {dst} daylight-saving rule.",
              file=sys.stderr)
        return rule_clock(std, dst)
    raise ConfigError(
        f"Time zone {name!r} is not installed on this machine (Windows has no time zone database by default). "
        "Run `python -m pip install tzdata`, or set burndown.timezoneFallback (see config.example.jsonc), "
        'or use "local" or a fixed offset such as "+01:00".')


def parse_ts(ts):
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.datetime.strptime(ts, fmt)
        except ValueError:
            pass
    raise ValueError(f"unexpected timestamp {ts!r}")


# ---------------------------------------------------------------------------------------------
# Runtime state, filled by configure() and connect().

CFG = copy.deepcopy(DEFAULTS)
BASE = ""
AUTH = ""
ROLES = []
COLUMNS = []
CHANNELS = []
PRIORITIES = {}
SUFFIXES = []
BURN = {}
CLOCK = make_clock("UTC")
FIELDS = ""
PARENT_FIELDS = "summary,parent,labels,components"

_cache = {"at": 0.0, "body": b""}
_me = {}
_icons = {}
_burn = {}
_issue_cache = {}
_lock = threading.Lock()
_jira_slots = threading.BoundedSemaphore(JIRA_CONNECTIONS)
_flights = {}


def norm(s):
    return re.sub(r"\s+", " ", s or "").strip().lower()


def norm_type(s):
    # "Sub-task", "Subtask" and "sub task" are the same issue type.
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def clean_status(s):
    s = (s or "").strip()
    for suffix in SUFFIXES:
        if s.lower().endswith(suffix):
            s = s[: -len(suffix)].strip()
    return s


def localized(v, what):
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, dict) and v and all(isinstance(x, str) for x in v.values()):
        return v
    raise ConfigError(f"{what} must be a string or an object of language: text")


def str_list(v, what):
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ConfigError(f"{what} must be a list of strings")
    return v


def configure(cfg):
    """Validate the merged config and set the module state. Returns a list of warnings."""
    global CFG, ROLES, COLUMNS, CHANNELS, PRIORITIES, SUFFIXES, BURN, CLOCK, FIELDS
    warnings = []
    board = cfg["board"]
    if board.get("id") in (None, ""):
        raise ConfigError("Set board.id in the config (the number in the board URL, .../boards/<id>) or pass --board.")
    try:
        board["id"] = int(board["id"])
    except (TypeError, ValueError):
        raise ConfigError("board.id must be a number")
    suffixes = [norm(s) for s in str_list(board.get("ignoreStatusSuffixes"), "board.ignoreStatusSuffixes") if norm(s)]

    roles, seen = [], set()
    for i, r in enumerate(cfg.get("reviewerRoles") or []):
        if not isinstance(r, dict) or not r.get("id") or not r.get("field"):
            raise ConfigError(f"reviewerRoles[{i}] needs an id and a field (for example customfield_10001)")
        if not ID_RE.match(str(r["id"])) or r["id"] in seen:
            raise ConfigError(f"reviewerRoles[{i}].id {r['id']!r} must be unique and use letters, digits, _ or -")
        seen.add(r["id"])
        roles.append({"id": r["id"], "field": str(r["field"]), "label": localized(r.get("label"), f"reviewerRoles[{i}].label") or r["id"]})
    role_index = {r["id"]: i for i, r in enumerate(roles)}

    def check_role(rid, where):
        if rid not in role_index and roles:
            warnings.append(f"{where} refers to reviewer role {rid!r}, which is not in reviewerRoles; it counts as always empty.")

    columns, seen = [], set()
    if not isinstance(cfg.get("columns"), list) or not cfg["columns"]:
        raise ConfigError("columns must be a non-empty list")
    for i, c in enumerate(cfg["columns"]):
        where = f"columns[{i}]"
        if not isinstance(c, dict) or not c.get("id") or not ID_RE.match(str(c["id"])) or c["id"] in seen:
            raise ConfigError(f"{where} needs a unique id made of letters, digits, _ or -")
        seen.add(c["id"])
        rule = c.get("rule") or {}
        if isinstance(rule, str):
            rule = {"type": rule}
        if rule.get("type") not in RULE_TYPES:
            raise ConfigError(f"{where}.rule.type must be one of {', '.join(RULE_TYPES)}")
        rule = {
            "type": rule["type"],
            "role": rule.get("role"),
            "roles": str_list(rule.get("roles"), f"{where}.rule.roles") if rule.get("roles") is not None else None,
            "notAssignee": bool(rule.get("notAssignee")),
            "emptyExcludes": str_list(rule.get("emptyExcludes"), f"{where}.rule.emptyExcludes"),
            "whenEmpty": rule.get("whenEmpty", "anyone"),
        }
        if rule["whenEmpty"] not in ("anyone", "nobody"):
            raise ConfigError(f'{where}.rule.whenEmpty must be "anyone" or "nobody"')
        if rule["type"] == "role":
            if not rule["role"]:
                raise ConfigError(f"{where}.rule needs a role (a reviewerRoles id)")
            check_role(rule["role"], where)
        for rid in rule["emptyExcludes"] + (rule["roles"] or []):
            check_role(rid, where)
        stage = c.get("stage")
        if stage is None:
            stage = role_index[rule["role"]] + 1 if rule["type"] == "role" and rule["role"] in role_index else (
                len(roles) + 1 if rule["type"] == "role" else 0)
        elif stage == "assignee":
            stage = 0
        elif isinstance(stage, str):
            if stage not in role_index:
                raise ConfigError(f"{where}.stage must be \"assignee\", a reviewerRoles id or a number")
            stage = role_index[stage] + 1
        name = c.get("name") or c["id"]
        prompt = localized(c.get("prompt"), f"{where}.prompt") or PROMPTS.get(c["id"]) or PROMPTS["generic"]
        other = localized(c.get("promptIfNotAssignee"), f"{where}.promptIfNotAssignee")
        if other is None and rule["type"] == "involved" and "prompt" not in c:
            other = PROMPTS["blockedReviewer"]
        columns.append({
            "id": c["id"],
            "name": name,
            "color": c.get("color") or "#8A93A6",
            "statuses": {norm(s) for s in (str_list(c.get("statuses"), f"{where}.statuses") or [name])},
            "types": {norm_type(t) for t in str_list(c.get("types"), f"{where}.types")},
            "typeNames": str_list(c.get("types"), f"{where}.types"),
            "excludeTypes": {norm_type(t) for t in str_list(c.get("excludeTypes"), f"{where}.excludeTypes")},
            "rule": rule,
            "channels": bool(c.get("channels", rule["type"] in ("unassigned", "role", "any"))),
            "stage": int(stage),
            "description": localized(c.get("description"), f"{where}.description"),
            "prompt": prompt,
            "promptIfNotAssignee": other,
        })

    channels, seen = [], {"other"}
    for i, ch in enumerate(cfg.get("channels") or []):
        where = f"channels[{i}]"
        if not isinstance(ch, dict) or not ch.get("id") or not ID_RE.match(str(ch["id"])) or ch["id"] in seen:
            raise ConfigError(f'{where} needs a unique id made of letters, digits, _ or - (and not "other")')
        seen.add(ch["id"])
        try:
            summary = re.compile(ch["summary"], re.I) if ch.get("summary") else None
        except re.error as ex:
            raise ConfigError(f"{where}.summary is not a valid regular expression: {ex}")
        channels.append({
            "id": ch["id"],
            "name": ch.get("name") or ch["id"],
            "color": ch.get("color") or "#7FA0FF",
            "epics": set(str_list(ch.get("epics"), f"{where}.epics")),
            "labels": [p.lower() for p in str_list(ch.get("labels"), f"{where}.labels")],
            "components": [p.lower() for p in str_list(ch.get("components"), f"{where}.components")],
            "summary": summary,
        })
        if not (channels[-1]["epics"] or channels[-1]["labels"] or channels[-1]["components"] or summary):
            warnings.append(f"{where} has no epics, labels, components or summary, so no issue will match it.")

    priorities = {}
    for k, v in (cfg.get("priorities") or {}).items():
        if v is not None:
            priorities[norm(k)] = int(v)

    b = cfg["burndown"]
    est = b.get("estimateComment") or {}
    stages = []
    if b.get("enabled") and est.get("enabled"):
        for i, s in enumerate(est.get("stages") or []):
            where = f"burndown.estimateComment.stages[{i}]"
            if not all(s.get(k) for k in ("id", "label", "from", "to")):
                raise ConfigError(f"{where} needs id, label, from and to")
            if s.get("by", "assignee") not in ("assignee", "others"):
                raise ConfigError(f'{where}.by must be "assignee" or "others"')
            stages.append({"id": s["id"], "label": s["label"], "from": norm(s["from"]), "to": norm(s["to"]), "by": s.get("by", "assignee")})
        if not stages:
            raise ConfigError("burndown.estimateComment is enabled but has no stages")
    thresholds = b.get("thresholds") or {}
    burn = {
        "enabled": bool(b.get("enabled")),
        "target": float(b.get("dailyTargetHours") or 0),
        "low": float(thresholds.get("low", 2)),
        "mid": float(thresholds.get("mid", 4)),
        "weekendDays": [int(d) for d in b.get("weekendDays") or []],
        "excludeTypes": {norm_type(t) for t in str_list(b.get("excludeTypes"), "burndown.excludeTypes")},
        "done": {norm(s) for s in str_list(b.get("doneStatuses"), "burndown.doneStatuses")},
        "stages": stages,
    }
    if stages:
        dur = r"((?:\d+(?:\.\d+)?\s*[hm]\s*)+)"
        burn["full"] = re.compile(r"\|\s*".join(re.escape(s["label"]) + r":\s*" + dur for s in stages), re.I)
        burn["part"] = re.compile(r"(?<!\w)(" + "|".join(re.escape(s["label"]) for s in stages) + r"):\s*" + dur, re.I)
        burn["byLabel"] = {s["label"].lower(): s["id"] for s in stages}
        burn["moves"] = {(s["from"], s["to"]): s for s in stages}
    clock = make_clock(b.get("timezone"), b.get("timezoneFallback")) if burn["enabled"] else make_clock("UTC")

    lang = cfg["ui"].get("defaultLanguage", "en")
    if lang not in ("en", "tr"):
        raise ConfigError('ui.defaultLanguage must be "en" or "tr"')

    CFG, ROLES, COLUMNS, CHANNELS, PRIORITIES, SUFFIXES, BURN, CLOCK = cfg, roles, columns, channels, priorities, suffixes, burn, clock
    FIELDS = ",".join(["summary", "status", "priority", "issuetype", "parent", "assignee", "labels", "components"] + [r["field"] for r in roles])
    return warnings


def connect(cfg):
    """Read the Jira site and credentials from the environment (or the config, for the non-secret parts)."""
    global BASE, AUTH
    base = os.environ.get("JIRA_BASE_URL") or os.environ.get("JIRA_URL") or cfg["jira"].get("baseUrl") or ""
    email = os.environ.get("JIRA_EMAIL") or os.environ.get("JIRA_USERNAME") or cfg["jira"].get("email") or ""
    token = os.environ.get("JIRA_API_TOKEN") or ""
    missing = [n for n, v in (("JIRA_BASE_URL", base), ("JIRA_EMAIL", email), ("JIRA_API_TOKEN", token)) if not v]
    if missing:
        raise ConfigError("Missing " + ", ".join(missing) + ". Set them in the environment or in a .env file next to board.py (see .env.example).")
    base = base.strip().rstrip("/")
    if not re.match(r"^https://[^/\s]+$", base):
        raise ConfigError(f"JIRA_BASE_URL must look like https://your-site.atlassian.net, not {base!r}")
    BASE = base
    AUTH = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()


# ---------------------------------------------------------------------------------------------
# Jira access

def describe(ex):
    if isinstance(ex, urllib.error.HTTPError):
        detail = ""
        try:
            body = json.loads(ex.read() or b"{}")
            detail = "; ".join(body.get("errorMessages") or []) or "; ".join(f"{k}: {v}" for k, v in (body.get("errors") or {}).items())
        except Exception:
            pass
        hint = {401: "check JIRA_EMAIL and JIRA_API_TOKEN",
                403: "this account cannot see the board or project",
                404: "check JIRA_BASE_URL and board.id"}.get(ex.code, "")
        return f"Jira returned HTTP {ex.code}" + (f": {detail}" if detail else "") + (f" ({hint})" if hint else "")
    if isinstance(ex, urllib.error.URLError):
        return f"Cannot reach Jira: {ex.reason}"
    return str(ex) or ex.__class__.__name__


def fetch(url, accept="application/json"):
    req = urllib.request.Request(url, headers={"Authorization": AUTH, "Accept": accept, "User-Agent": "sprint-focus-board"})
    # Parallel page loads share one small pool of Jira connections; more than that makes Jira drop requests.
    with _jira_slots:
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.read(), resp.headers.get("Content-Type", "")
            except urllib.error.HTTPError as ex:
                if attempt == 2 or not (ex.code >= 500 or ex.code == 429):
                    raise
                if ex.code == 429:
                    try:
                        wait = float(ex.headers.get("Retry-After") or 2)
                    except ValueError:
                        wait = 2
                    time.sleep(min(wait, 10))
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt == 2:
                    raise


def get(url):
    return json.loads(fetch(url)[0])


def single_flight(key):
    # Concurrent callers for the same key wait for the first one instead of repeating its Jira calls.
    with _lock:
        return _flights.setdefault(key, threading.Lock())


def search(jql, fields):
    out, token = [], None
    while True:
        params = {"jql": jql, "fields": fields, "maxResults": 100}
        if token:
            params["nextPageToken"] = token
        d = get(BASE + "/rest/api/3/search/jql?" + urllib.parse.urlencode(params))
        out += d["issues"]
        token = d.get("nextPageToken")
        if not token or d.get("isLast"):
            return out


def paged(url, key):
    out, start = [], 0
    while True:
        d = get(f"{url}{'&' if '?' in url else '?'}startAt={start}&maxResults=100")
        vals = d.get(key) or []
        out += vals
        start += len(vals)
        if not vals or start >= d.get("total", 0):
            return out


def person(u):
    if not isinstance(u, dict) or not u.get("accountId"):
        return None
    return {"id": u["accountId"], "name": u.get("displayName", ""), "avatar": (u.get("avatarUrls") or {}).get("48x48", "")}


def people(v):
    return [p for p in map(person, v if isinstance(v, list) else [v]) if p]


def myself():
    with _lock:
        if _me:
            return dict(_me)
    me = person(get(BASE + "/rest/api/3/myself"))
    with _lock:
        _me.update(me)
    return me


def active_sprint():
    board, prefix = CFG["board"]["id"], CFG["board"].get("sprintNamePrefix") or ""
    sprints = get(f"{BASE}/rest/agile/1.0/board/{board}/sprint?state=active")["values"]
    for s in sprints:
        if s["name"].startswith(prefix):
            return s
    raise LookupError(f"No active sprint on board {board}" + (f" whose name starts with {prefix!r}" if prefix else ""))


def icon_path(url):
    # Issue-type icons live on the Jira site and need auth, so the page loads them through /icon.
    return "/icon?u=" + urllib.parse.quote(url, safe="") if url and url.startswith(BASE + "/") else None


def icon(url):
    if not url.startswith(BASE + "/"):
        return 400, b"", "text/plain"
    with _lock:
        hit = _icons.get(url)
    if hit is None:
        body, ctype = fetch(url, accept="image/*")
        hit = (body, ctype or "image/svg+xml")
        with _lock:
            _icons[url] = hit
    return 200, hit[0], hit[1]


# ---------------------------------------------------------------------------------------------
# Board

def prio_level(name):
    m = re.match(r"\s*P(\d)\b", name or "", re.I)
    if m:
        return int(m.group(1))
    return PRIORITIES.get(norm(name))


def glob_any(name, patterns):
    name = (name or "").lower()
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def channels_of(issue, parents):
    # A ticket belongs to a channel through its own fields or its parent's (labels, components),
    # its parent or grandparent key (epics), or its own, parent or epic summary.
    f = issue["fields"]
    p = f.get("parent") or {}
    pf = parents.get(p.get("key"), {}) if p else {}
    epic = pf.get("parent") or {}
    keys = {k for k in (p.get("key"), epic.get("key")) if k}
    texts = [f.get("summary") or "", pf.get("summary") or (p.get("fields") or {}).get("summary") or "", (epic.get("fields") or {}).get("summary") or ""]
    labels = list(f.get("labels") or []) + list(pf.get("labels") or [])
    comps = [c.get("name", "") for c in (f.get("components") or []) + (pf.get("components") or [])]
    out = []
    for ch in CHANNELS:
        if (keys & ch["epics"]
                or any(glob_any(x, ch["labels"]) for x in labels)
                or any(glob_any(x, ch["components"]) for x in comps)
                or (ch["summary"] and any(ch["summary"].search(t) for t in texts if t))):
            out.append(ch["id"])
    return out


def columns_for(status, type_name):
    s, t = norm(status), norm_type(type_name)
    return [c["id"] for c in COLUMNS if s in c["statuses"] and (not c["types"] or t in c["types"]) and t not in c["excludeTypes"]]


def fetch_parents(issues):
    keys = sorted({i["fields"]["parent"]["key"] for i in issues if i["fields"].get("parent")})
    parents = {}
    for n in range(0, len(keys), 100):
        for p in search("key in (%s)" % ",".join(keys[n:n + 100]), PARENT_FIELDS):
            parents[p["key"]] = p["fields"]
    return parents


def sprint_info(s):
    prefix = CFG["board"].get("sprintNamePrefix") or ""
    name = s["name"]
    shown = name[len(prefix):].strip(" -–:") if prefix and name.startswith(prefix) else name
    return dict({k: s.get(k) for k in ("id", "name", "startDate", "endDate", "goal")}, displayName=shown or name)


def load_board():
    me = myself()
    sprint = active_sprint()
    issues = search(f"sprint = {sprint['id']} AND statusCategory != Done ORDER BY rank ASC", FIELDS)
    parents = fetch_parents(issues) if CHANNELS else {}
    rows = []
    for rank, i in enumerate(issues):
        f = i["fields"]
        p = f.get("parent")
        prio = (f.get("priority") or {}).get("name", "")
        status = clean_status(f["status"]["name"])
        rows.append({
            "key": i["key"],
            "summary": f.get("summary") or "",
            "status": status,
            "type": f["issuetype"]["name"],
            "typeIcon": icon_path(f["issuetype"].get("iconUrl")),
            "prio": prio_level(prio),
            "prioName": prio,
            "assignee": person(f.get("assignee")),
            "roles": {r["id"]: people(f.get(r["field"])) for r in ROLES},
            "channels": channels_of(i, parents),
            "parent": {"key": p["key"], "summary": parents.get(p["key"], {}).get("summary") or (p.get("fields") or {}).get("summary", "")} if p else None,
            "rank": rank,
            "cols": columns_for(status, f["issuetype"]["name"]),
        })
    return {"me": me, "base": BASE, "sprint": sprint_info(sprint), "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "rows": rows}


def board_json(force=False):
    with single_flight("board"):
        with _lock:
            fresh = not force and time.time() - _cache["at"] <= CACHE_SECONDS and _cache["body"]
            if fresh:
                return 200, _cache["body"]
        try:
            body = json.dumps(load_board()).encode("utf-8")
        except Exception as ex:
            print("Board:", describe(ex), file=sys.stderr)
            return 502, json.dumps({"error": describe(ex)}).encode("utf-8")
        with _lock:
            _cache.update(at=time.time(), body=body)
        return 200, body


# ---------------------------------------------------------------------------------------------
# Burndown: the main figure is the drop in Remaining Estimate a person makes on a local day.
# Optionally, for comparison, their first-time stage transitions are valued from an estimate comment
# such as "Dev: 2h | Rev: 30m | MR: 15m" (the newest full line wins, else the newest single values).

def local_day(ts):
    local = CLOCK(parse_ts(ts).astimezone(UTC))
    return local.strftime("%Y-%m-%d"), local.strftime("%H:%M")


def today():
    return CLOCK(datetime.datetime.now(UTC)).strftime("%Y-%m-%d")


def hours(text):
    return sum(float(n) / (60 if u.lower() == "m" else 1) for n, u in re.findall(r"(\d+(?:\.\d+)?)\s*([hm])", text, re.I))


def adf_text(node):
    if isinstance(node, dict):
        own = node.get("text", "")
        inner = "".join(adf_text(c) for c in node.get("content", []))
        return own + inner + ("\n" if node.get("type") in ("paragraph", "heading", "listItem", "tableRow") else "")
    if isinstance(node, list):
        return "".join(adf_text(c) for c in node)
    return node if isinstance(node, str) else ""


def estimate(comments):
    full, part = None, {}
    for c in sorted(comments, key=lambda c: c["created"]):
        text = adf_text(c.get("body"))
        m = BURN["full"].search(text)
        if m:
            full = {s["id"]: hours(m.group(n + 1)) for n, s in enumerate(BURN["stages"])}
        for label, dur in BURN["part"].findall(text):
            part[BURN["byLabel"][label.lower()]] = hours(dur)
    return full or part or None


def issue_detail(key, updated):
    with single_flight("issue|" + key):
        with _lock:
            hit = _issue_cache.get(key)
        if hit and hit[0] == updated:
            return hit[1]
        histories = sorted(paged(f"{BASE}/rest/api/3/issue/{key}/changelog", "values"), key=lambda h: h["created"])
        worklogs = {}
        if any(it.get("field") == "WorklogId" for h in histories for it in h.get("items", [])):
            worklogs = {w["id"]: w["started"] for w in paged(f"{BASE}/rest/api/3/issue/{key}/worklog", "worklogs")}
        est = estimate(paged(f"{BASE}/rest/api/3/issue/{key}/comment?orderBy=created", "comments")) if BURN["stages"] else None
        detail = (histories, est, worklogs)
        with _lock:
            _issue_cache[key] = (updated, detail)
        return detail


def secs(v):
    try:
        return int(v or 0)
    except ValueError:
        return 0


def burn_events(day_from, day_to, sprint):
    since = (datetime.date.fromisoformat(day_from) - datetime.timedelta(days=1)).isoformat()
    issues = search(f'sprint = {sprint["id"]} AND updated >= "{since}"', "summary,assignee,updated,timeestimate,issuetype")
    issues = [i for i in issues if norm_type(i["fields"]["issuetype"]["name"]) not in BURN["excludeTypes"]]
    with concurrent.futures.ThreadPoolExecutor(JIRA_CONNECTIONS) as pool:
        details = list(pool.map(lambda i: issue_detail(i["key"], i["fields"]["updated"]), issues))
    events, remaining, done_left = [], [], []
    moves = BURN.get("moves") or {}
    for issue, (histories, est, worklogs) in zip(issues, details):
        f = issue["fields"]
        items = [it for h in histories for it in h["items"]]
        handovers = [it for it in items if it["field"] == "assignee"]
        assignee = handovers[0].get("from") if handovers else (f.get("assignee") or {}).get("accountId")
        re_moves = [it for it in items if it["field"] == "timeestimate"]
        left = secs(re_moves[0].get("from")) if re_moves else secs(f.get("timeestimate"))
        seen, timeline, dones = set(), [], []
        for h in histories:
            d, at = local_day(h["created"])
            when = parse_ts(h["created"])
            author = (h.get("author") or {}).get("accountId")
            row = {"key": issue["key"], "summary": f.get("summary") or "", "day": d, "at": at, "author": author}
            inside = day_from <= d <= day_to
            # A worklog lowers the remaining estimate when it is entered; book that drop on the day the work was done.
            wl = next((worklogs.get(it.get("to")) for it in h["items"] if it["field"] == "WorklogId"), None)
            for it in h["items"]:
                if it["field"] == "assignee":
                    assignee = it.get("to")
                elif it["field"] == "timeestimate":
                    old, new = secs(it.get("from")), secs(it.get("to"))
                    timeline.append((when, new))
                    wd, wat = local_day(wl) if wl else (d, at)
                    if day_from <= wd <= day_to and old != new:
                        remaining.append(dict(row, day=wd, at=wat, before=old / 3600, after=new / 3600, worklog=bool(wl)))
                elif it["field"] == "status":
                    if inside and norm(clean_status(it.get("toString"))) in BURN["done"]:
                        dones.append((when, row))
                    stage = moves.get((norm(clean_status(it.get("fromString"))), norm(clean_status(it.get("toString")))))
                    if not stage:
                        continue
                    first = stage["id"] not in seen
                    seen.add(stage["id"])
                    if not inside:
                        continue
                    reason = None if first else "notFirst"
                    if reason is None and (stage["by"] == "assignee") != (author == assignee):
                        reason = "notAssignee" if stage["by"] == "assignee" else "own"
                    if reason is None and not (est and stage["id"] in est):
                        reason = "noEstimate"
                    events.append(dict(row, stage=stage["id"], hours=est[stage["id"]] if reason is None else 0, reason=reason))
        # The remaining estimate is often zeroed in a separate changelog entry right after the Done move.
        for when, row in dones:
            later = [v for t, v in timeline if t <= when + datetime.timedelta(minutes=2)]
            rest = later[-1] if later else left
            if rest:
                done_left.append(dict(row, left=rest / 3600))
    return {"events": events, "remaining": remaining, "doneLeft": done_left}


def burn_json(day, whole_sprint=False, force=False):
    key = ("sprint|" if whole_sprint else "") + day
    with single_flight("burn|" + key):
        with _lock:
            hit = _burn.get(key)
        if hit and not force and time.time() - hit[0] < CACHE_SECONDS:
            return 200, hit[1]
        try:
            sprint = active_sprint()
            start = local_day(sprint["startDate"])[0] if whole_sprint else day
            body = json.dumps(dict(burn_events(start, day, sprint), day=day, start=start, target=BURN["target"])).encode("utf-8")
        except Exception as ex:
            print("Burndown:", describe(ex), file=sys.stderr)
            return 502, json.dumps({"error": describe(ex)}).encode("utf-8")
        with _lock:
            _burn[key] = (time.time(), body)
        return 200, body


# ---------------------------------------------------------------------------------------------
# Page and HTTP server

def page_config():
    cols = [{k: c[k] for k in ("id", "name", "color", "rule", "channels", "stage", "description", "prompt", "promptIfNotAssignee")}
            for c in COLUMNS]
    for out, c in zip(cols, COLUMNS):
        out["types"] = c["typeNames"]
    return {
        "columns": cols,
        "roles": [{"id": r["id"], "label": r["label"]} for r in ROLES],
        "channels": [{"id": c["id"], "name": c["name"], "color": c["color"]} for c in CHANNELS],
        "ui": {"defaultLanguage": CFG["ui"].get("defaultLanguage", "en"), "refreshSeconds": int(CFG["ui"].get("refreshSeconds") or 60)},
        "burndown": {"enabled": BURN["enabled"], "target": BURN["target"], "low": BURN["low"], "mid": BURN["mid"],
                     "weekendDays": BURN["weekendDays"], "stages": [{"id": s["id"], "label": s["label"]} for s in BURN["stages"]]},
    }


def page():
    with open(PAGE_FILE, encoding="utf-8") as fh:
        html = fh.read()
    cfg = json.dumps(page_config()).replace("<", "\\u003c")
    return html.replace("/*__BOARD_CONFIG__*/null", cfg, 1).encode("utf-8")


ALLOWED_HOSTS = set()


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if self.path.startswith("/icon") and code == 200 else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Only answer to localhost names, so a web page cannot reach this server through DNS rebinding.
        if (self.headers.get("Host") or "").lower() not in ALLOWED_HOSTS:
            self.send(403, b"Unknown host", "text/plain")
            return
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        force = q.get("force", [""])[0] == "1"
        if url.path == "/":
            self.send(200, page(), "text/html; charset=utf-8")
        elif url.path == "/api/board":
            self.send(*board_json(force), "application/json; charset=utf-8")
        elif url.path == "/api/burn":
            if not BURN["enabled"]:
                self.send(404, b'{"error":"The burndown is turned off in the config."}', "application/json; charset=utf-8")
                return
            day = q.get("day", [""])[0]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                day = today()
            self.send(*burn_json(day, q.get("scope", [""])[0] == "sprint", force), "application/json; charset=utf-8")
        elif url.path == "/icon":
            try:
                code, body, ctype = icon(q.get("u", [""])[0])
            except Exception:
                code, body, ctype = 502, b"", "text/plain"
            self.send(code, body, ctype)
        else:
            self.send_error(404)

    def log_message(self, *a):
        pass


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="A local, person-focused board for the active sprint of a Jira Cloud board.")
    ap.add_argument("--config", help="config file (default: $BOARD_CONFIG, else config.jsonc or config.json next to board.py)")
    ap.add_argument("--env-file", help="file with the JIRA_* variables (default: $BOARD_ENV_FILE, else .env next to board.py)")
    ap.add_argument("--port", type=int, help="port on 127.0.0.1 (default: server.port, 8765)")
    ap.add_argument("--board", type=int, help="board id; overrides board.id")
    ap.add_argument("--sprint-prefix", help="overrides board.sprintNamePrefix")
    ap.add_argument("--open", action="store_true", help="open the board in the default browser")
    args = ap.parse_args(argv)
    try:
        cfg, path = read_config(args.config)
        if args.board is not None:
            cfg["board"]["id"] = args.board
        if args.sprint_prefix is not None:
            cfg["board"]["sprintNamePrefix"] = args.sprint_prefix
        env_file = args.env_file or os.environ.get("BOARD_ENV_FILE")
        if env_file and not os.path.isfile(env_file):
            raise ConfigError(f"Env file not found: {env_file}")
        load_env_file(env_file or os.path.join(HERE, ".env"))
        warnings = configure(cfg)
        connect(cfg)
    except ConfigError as ex:
        print(f"Config error: {ex}", file=sys.stderr)
        return 2
    for w in warnings:
        print(f"Warning: {w}", file=sys.stderr)
    port = args.port or int(cfg["server"].get("port") or 8765)
    ALLOWED_HOSTS.update({f"localhost:{port}", f"127.0.0.1:{port}"})
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}"
    print(f"Sprint Focus Board: {url}  ({'config ' + path if path else 'built-in defaults'}; Ctrl+C to stop)")
    if BURN["enabled"]:
        # The sprint-wide burndown reads every changelog once; warm that cache before the page asks for it.
        threading.Thread(target=burn_json, args=(today(), True), daemon=True).start()
    if args.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
