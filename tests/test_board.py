import datetime
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import board  # noqa: E402

UTC = datetime.timezone.utc


def configured(**over):
    cfg = board.deep_merge(board.DEFAULTS, dict({"board": {"id": 1}}, **over))
    board.configure(cfg)
    return cfg


class ConfigFileTest(unittest.TestCase):
    def test_comments_are_stripped_but_strings_are_kept(self):
        text = '{\n // note\n "url": "https://x.example/a//b", /* block */ "re": "a\\\\/\\"b" }'
        self.assertEqual(json.loads(board.strip_comments(text)), {"url": "https://x.example/a//b", "re": 'a\\/"b'})

    def test_example_config_is_valid(self):
        path = os.path.join(board.HERE, "config.example.jsonc")
        cfg, used = board.read_config(path)
        self.assertEqual(used, path)
        self.assertEqual(board.configure(cfg), [])

    def test_token_in_config_is_refused(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonc", delete=False) as fh:
            fh.write('{"board": {"id": 1}, "jira": {"apiToken": "x"}}')
        try:
            with self.assertRaises(board.ConfigError):
                board.read_config(fh.name)
        finally:
            os.unlink(fh.name)

    def test_plain_http_only_on_loopback(self):
        cfg = board.deep_merge(board.DEFAULTS, {"jira": {"email": "a@example.com"}})
        with mock.patch.dict(os.environ, {"JIRA_API_TOKEN": "t", "JIRA_BASE_URL": "http://127.0.0.1:9999"}):
            board.connect(cfg)
            self.assertEqual(board.BASE, "http://127.0.0.1:9999")
        with mock.patch.dict(os.environ, {"JIRA_API_TOKEN": "t", "JIRA_BASE_URL": "http://jira.example.com"}):
            with self.assertRaises(board.ConfigError):
                board.connect(cfg)

    def test_missing_board_id_starts_the_setup(self):
        board.configure(board.deep_merge(board.DEFAULTS, {}))
        self.assertTrue(board.SETUP_REQUIRED)
        configured()
        self.assertFalse(board.SETUP_REQUIRED)

    def test_env_file_does_not_override_the_environment(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write('# c\nSFB_TEST_A="one"\nexport SFB_TEST_B=two\n')
        try:
            with mock.patch.dict(os.environ, {"SFB_TEST_B": "kept"}):
                board.load_env_file(fh.name)
                self.assertEqual(os.environ["SFB_TEST_A"], "one")
                self.assertEqual(os.environ["SFB_TEST_B"], "kept")
        finally:
            os.unlink(fh.name)


class ClockTest(unittest.TestCase):
    def local(self, clock, *utc):
        return clock(datetime.datetime(*utc, tzinfo=UTC)).strftime("%Y-%m-%d %H:%M")

    def test_eu_rule(self):
        clock = board.rule_clock(datetime.timedelta(0), "eu")
        self.assertEqual(self.local(clock, 2026, 3, 29, 0, 59), "2026-03-29 00:59")
        self.assertEqual(self.local(clock, 2026, 3, 29, 1, 0), "2026-03-29 02:00")
        self.assertEqual(self.local(clock, 2026, 10, 24, 23, 30), "2026-10-25 00:30")
        self.assertEqual(self.local(clock, 2026, 10, 25, 1, 0), "2026-10-25 01:00")

    def test_us_rule(self):
        clock = board.rule_clock(datetime.timedelta(hours=-5), "us")
        self.assertEqual(self.local(clock, 2026, 3, 8, 6, 59), "2026-03-08 01:59")
        self.assertEqual(self.local(clock, 2026, 3, 8, 7, 0), "2026-03-08 03:00")
        self.assertEqual(self.local(clock, 2026, 11, 1, 5, 59), "2026-11-01 01:59")
        self.assertEqual(self.local(clock, 2026, 11, 1, 6, 0), "2026-11-01 01:00")

    def test_fixed_offset(self):
        self.assertEqual(self.local(board.make_clock("+03:00"), 2026, 1, 1, 22, 0), "2026-01-02 01:00")

    def test_unknown_zone_without_fallback_explains_the_fix(self):
        with mock.patch("zoneinfo.ZoneInfo", side_effect=Exception("missing")):
            with self.assertRaises(board.ConfigError) as ctx:
                board.make_clock("Europe/Nowhere")
        self.assertIn("tzdata", str(ctx.exception))


class BoardTest(unittest.TestCase):
    def test_status_suffix_and_type_spelling(self):
        configured()
        self.assertEqual(board.clean_status("Reopened (migrated)"), "Reopened")
        self.assertEqual(board.columns_for("To Do", "Subtask"), ["todo"])
        self.assertEqual(board.columns_for("To Do", "Story"), [])
        self.assertEqual(board.columns_for("In Review", "Story"), [])
        self.assertEqual(board.columns_for("In Review", "Bug"), ["inreview"])

    def test_channels(self):
        configured(channels=[{"id": "web", "epics": ["E-1"], "labels": ["web-*"], "components": ["*site*"], "summary": r"\bUI\b"}])

        def issue(**f):
            return {"fields": dict({"summary": "x"}, **f)}
        parents = {"S-1": {"summary": "story", "parent": {"key": "E-1", "fields": {"summary": "epic"}}},
                   "S-2": {"summary": "story", "labels": ["WEB-team"]}}
        self.assertEqual(board.channels_of(issue(parent={"key": "S-1"}), parents), ["web"])
        self.assertEqual(board.channels_of(issue(parent={"key": "S-2"}), parents), ["web"])
        self.assertEqual(board.channels_of(issue(components=[{"name": "Website"}]), {}), ["web"])
        self.assertEqual(board.channels_of(issue(summary="Fix UI glitch"), {}), ["web"])
        self.assertEqual(board.channels_of(issue(summary="Build"), {}), [])

    def test_priority_levels(self):
        configured(priorities={"Showstopper": 1})
        self.assertEqual(board.prio_level("P2 - High"), 2)
        self.assertEqual(board.prio_level("Highest"), 1)
        self.assertEqual(board.prio_level("showstopper"), 1)
        self.assertIsNone(board.prio_level("Whatever"))

    def test_default_stage_follows_the_role(self):
        configured(reviewerRoles=[{"id": "approver", "field": "customfield_1"}, {"id": "mergeApprover", "field": "customfield_2"}])
        self.assertEqual({c["id"]: c["stage"] for c in board.COLUMNS},
                         {"todo": 0, "reopened": 0, "progress": 0, "blocked": 0, "toreview": 1, "inreview": 1, "merge": 2})

    def test_to_do_also_shows_the_persons_own_tickets(self):
        configured()
        self.assertTrue(next(c for c in board.COLUMNS if c["id"] == "todo")["rule"]["orAssignee"])
        configured(columns=[{"id": "todo", "name": "To Do", "rule": {"type": "unassigned"}}])
        self.assertFalse(board.COLUMNS[0]["rule"]["orAssignee"])

    def test_blocks_lists_only_open_tickets_this_one_holds_up(self):
        def link(name, side, key, cat):
            return {"type": {"name": name}, side: {"key": key, "fields": {
                "summary": key + " summary", "status": {"name": "S", "statusCategory": {"key": cat}}}}}
        f = {"issuelinks": [link("Blocks", "outwardIssue", "A-1", "new"),
                            link("Blocks", "outwardIssue", "A-2", "done"),
                            link("Blocks", "inwardIssue", "A-3", "new"),
                            link("Relates", "outwardIssue", "A-4", "new")]}
        self.assertEqual(board.blocked_by_this(f),
                         [{"key": "A-1", "summary": "A-1 summary", "status": "S", "cat": "new"}])
        self.assertEqual(board.blocked_by_this({}), [])


class BurnJobTest(unittest.TestCase):
    def test_burn_is_worked_out_in_the_background(self):
        import threading
        configured()
        gate = threading.Event()

        def slow_events(start, day, sprint):
            gate.wait(5)
            return {"events": [], "remaining": [], "doneLeft": []}
        with mock.patch.object(board, "active_sprint", return_value={"id": 1, "startDate": "2026-11-09T09:00:00.000Z"}), \
                mock.patch.object(board, "burn_events", side_effect=slow_events):
            board.forget_results()
            code, body = board.burn_json("2026-11-17")
            self.assertEqual((code, json.loads(body)), (202, {"pending": True, "stale": None}))
            self.assertEqual(board.burn_json("2026-11-17")[0], 202)
            self.assertEqual(list(board._jobs), ["burndown|2026-11-17"])
            gate.set()
            board._jobs["burndown|2026-11-17"].join(5)
            code, body = board.burn_json("2026-11-17")
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)["day"], "2026-11-17")
            gate.clear()
            code, body = board.burn_json("2026-11-17", force=True)
            self.assertEqual(code, 202)
            self.assertEqual(json.loads(body)["stale"]["day"], "2026-11-17")
            # After a forced refresh the old answer is only stale, so the page keeps asking until the new one.
            self.assertEqual(board.burn_json("2026-11-17")[0], 202)
            gate.set()
            board._jobs["burndown|2026-11-17"].join(5)
            self.assertEqual(board.burn_json("2026-11-17")[0], 200)

    def test_closed_connection_is_one_line_not_a_traceback(self):
        import io
        handler = board.Handler.__new__(board.Handler)
        handler.path = "/api/burn?scope=sprint"
        handler.request_version = "HTTP/1.0"
        handler.wfile = mock.Mock(write=mock.Mock(side_effect=ConnectionAbortedError(10053, "aborted")))
        handler._headers_buffer = []
        err = io.StringIO()
        with mock.patch.object(board.Handler, "log_request"), mock.patch("sys.stderr", err):
            handler.send(200, b"{}", "application/json")
        self.assertEqual(err.getvalue().strip(), "/api/burn: the browser closed the connection before the answer was sent.")


class PromptVarsAndCredentialsTest(unittest.TestCase):
    def test_prompt_vars_pick_the_first_matching_rule(self):
        configured(promptVars={"finish": [{"keys": ["WEB-*"], "value": "web-finish"}, {"types": ["Bug"], "value": {"en": "bug-fix", "tr": "hata"}},
                                          {"value": "api-finish"}]})
        self.assertEqual(board.prompt_vars_for("WEB-12", "Bug"), {"finish": "web-finish"})
        self.assertEqual(board.prompt_vars_for("API-3", "Bug"), {"finish": {"en": "bug-fix", "tr": "hata"}})
        self.assertEqual(board.prompt_vars_for("API-3", "Sub-task"), {"finish": "api-finish"})
        with self.assertRaises(board.ConfigError):
            board.compile_prompt_vars({"key": [{"value": "x"}]})

    def test_mcp_config_fills_missing_credentials_only(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"mcpServers": {"other": {"env": {}}, "atlassian": {"env": {
                "JIRA_URL": "https://x.example", "JIRA_USERNAME": "a@example.com", "JIRA_API_TOKEN": "t1"}}}}, fh)
        try:
            with mock.patch.dict(os.environ, {"JIRA_EMAIL": "kept@example.com"}, clear=True):
                board.load_mcp_credentials(fh.name)
                self.assertEqual((os.environ["JIRA_BASE_URL"], os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"]),
                                 ("https://x.example", "kept@example.com", "t1"))
            with self.assertRaises(board.ConfigError):
                board.load_mcp_credentials(fh.name + ".missing")
        finally:
            os.unlink(fh.name)


class SetupTest(unittest.TestCase):
    def test_sprint_prefix_suggestion(self):
        self.assertEqual(board.suggest_prefix("TEAM Sprint 42"), "TEAM")
        self.assertEqual(board.suggest_prefix("Mobile team - 7"), "Mobile team")
        self.assertEqual(board.suggest_prefix("Sprint 12"), "")

    def test_keywords_are_whole_words(self):
        chans = board.compile_channels([{"id": "pay", "keywords": ["pay", "Acme Client"]}])
        issue = lambda s: {"fields": {"summary": s}}  # noqa: E731
        self.assertEqual(board.channels_of(issue("Pay the invoice"), {}, chans), ["pay"])
        self.assertEqual(board.channels_of(issue("Fix the acme client login"), {}, chans), ["pay"])
        self.assertEqual(board.channels_of(issue("Payroll export"), {}, chans), [])

    def test_save_writes_settings_over_the_config(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "settings.json")
        base = board.deep_merge(board.DEFAULTS, {"ui": {"defaultLanguage": "tr"}})
        with mock.patch.dict(os.environ, {"BOARD_SETTINGS": path}), mock.patch.object(board, "BASE_CFG", base), \
                mock.patch.object(board, "burn_json"):
            board.configure(base)
            self.assertTrue(board.SETUP_REQUIRED)
            board.setup_save({"board": {"id": 5, "sprintNamePrefix": "TEAM"}, "team": [{"id": "u1", "name": "Ann"}],
                              "channels": [{"id": "web", "name": "Web", "keywords": ["web"]}],
                              "prompts": {"todo": {"prompt": "/start {key}"}}, "refreshSeconds": 120})
            saved = board.read_settings(path)
        self.assertFalse(board.SETUP_REQUIRED)
        self.assertEqual(saved["board"], {"id": 5, "sprintNamePrefix": "TEAM"})
        self.assertEqual(board.CFG["ui"], {"defaultLanguage": "tr", "refreshSeconds": 120})
        self.assertEqual([c["id"] for c in board.CHANNELS], ["web"])
        todo = [c for c in board.COLUMNS if c["id"] == "todo"][0]
        self.assertEqual(todo["prompt"], "/start {key}")
        self.assertEqual(todo["defaultPrompt"], board.PROMPTS["todo"])
        self.assertEqual(board.TEAM, [{"id": "u1", "name": "Ann", "avatar": ""}])

    def test_save_keeps_only_what_differs_from_the_config(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "settings.json")
        base = board.deep_merge(board.DEFAULTS, {
            "board": {"id": 7, "sprintNamePrefix": "TEAM"}, "team": ["u1"],
            "channels": [{"id": "web", "name": "Web", "labels": ["Web-*"], "summary": "\\bweb\\b"}],
            "prompts": {"todo": {"prompt": {"en": "/start {key}", "tr": "/basla {key}"}}}})
        with mock.patch.dict(os.environ, {"BOARD_SETTINGS": path}), mock.patch.object(board, "BASE_CFG", base), \
                mock.patch.object(board, "burn_json"):
            board.configure(base, base)
            setup = board.page_config()["setup"]
            todo = [c for c in board.COLUMNS if c["id"] == "todo"][0]
            self.assertEqual(todo["defaultPrompt"], {"en": "/start {key}", "tr": "/basla {key}"})
            # What the wizard sends when only the In Review prompt was edited.
            chans = [dict(c, epics=c.get("epics", []), components=[], keywords=[]) for c in setup["channels"]]
            board.setup_save({"board": setup["board"], "team": [{"id": "u1", "name": "Ann"}], "channels": chans,
                              "prompts": {"todo": {"prompt": todo["defaultPrompt"]}, "inreview": {"prompt": "/look {key}"}},
                              "refreshSeconds": 60})
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)
            self.assertEqual(saved, {"prompts": {"inreview": {"prompt": "/look {key}"}}})
            self.assertEqual([c["name"] for c in board.CHANNELS], ["Web"])
            # Turning the channels off is a change, so it is kept.
            board.setup_save({"board": setup["board"], "team": [], "channels": [], "prompts": {}, "refreshSeconds": 60})
            saved = board.read_settings(path)
        self.assertEqual(saved, {"team": [], "channels": []})
        self.assertEqual(board.CHANNELS, [])

    def test_post_needs_token_origin_and_json(self):
        import threading
        import urllib.error
        import urllib.request
        configured()
        srv = board.Server(("127.0.0.1", 0), board.Handler)
        port = srv.server_address[1]
        board.ALLOWED_HOSTS.update({f"localhost:{port}", f"127.0.0.1:{port}"})
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        def post(headers):
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/setup/preview", data=b'{"sprint": 1, "channels": []}', method="POST",
                                         headers=dict({"Content-Type": "application/json"}, **headers))
            try:
                return urllib.request.urlopen(req, timeout=5).status
            except urllib.error.HTTPError as ex:
                return ex.code
        try:
            with mock.patch.object(board, "sprint_issues", return_value=([], {})):
                self.assertEqual(post({}), 403)
                self.assertEqual(post({"X-Setup-Token": board.SETUP_TOKEN, "Origin": "https://evil.example"}), 403)
                self.assertEqual(post({"X-Setup-Token": board.SETUP_TOKEN, "Origin": f"http://localhost:{port}"}), 200)
        finally:
            srv.shutdown()
            srv.server_close()


def change(at, author, *items):
    return {"created": at, "author": {"accountId": author}, "items": list(items)}


class BurndownTest(unittest.TestCase):
    def run_burn(self, histories, comments=(), worklogs=None, **burndown):
        configured(burndown=dict({"timezone": "UTC"}, **burndown))
        issue = {"key": "T-1", "fields": {"summary": "s", "updated": "u", "assignee": {"accountId": "dev"},
                                          "timeestimate": 0, "issuetype": {"name": "Sub-task"}}}
        detail = (histories, board.estimate(list(comments)) if board.BURN["stages"] else None, worklogs or {})
        with mock.patch.object(board, "search", return_value=[issue]), mock.patch.object(board, "issue_detail", return_value=detail):
            return board.burn_events("2026-11-10", "2026-11-11", {"id": 1})

    def test_remaining_estimate_drops_and_worklog_day(self):
        out = self.run_burn([
            change("2026-11-10T09:00:00.000+0000", "dev", {"field": "timeestimate", "from": "14400", "to": "7200"}),
            change("2026-11-11T09:00:00.000+0000", "dev", {"field": "timeestimate", "from": "7200", "to": "10800"}),
            # A worklog entered on the 12th for work started on the 11th counts on the 11th.
            change("2026-11-12T08:00:00.000+0000", "dev", {"field": "WorklogId", "to": "77"},
                   {"field": "timeestimate", "from": "10800", "to": "3600"}),
        ], worklogs={"77": "2026-11-11T15:00:00.000+0000"})
        rows = [(r["day"], r["before"], r["after"], r["worklog"]) for r in out["remaining"]]
        self.assertEqual(rows, [("2026-11-10", 4.0, 2.0, False), ("2026-11-11", 2.0, 3.0, False), ("2026-11-11", 3.0, 1.0, True)])
        self.assertEqual(out["events"], [])

    def test_estimate_comment_module(self):
        comment = {"created": "2026-11-01T00:00:00.000+0000",
                   "body": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Dev: 2h | Rev: 30m | MR: 15m"}]}]}}
        out = self.run_burn([
            change("2026-11-10T09:00:00.000+0000", "dev", {"field": "status", "fromString": "In Progress", "toString": "To Review"}),
            change("2026-11-10T10:00:00.000+0000", "dev", {"field": "status", "fromString": "In Review", "toString": "For Merge Approval"}),
            change("2026-11-11T10:00:00.000+0000", "lead", {"field": "status", "fromString": "For Merge Approval", "toString": "Done (migrated)"}),
        ], comments=[comment], estimateComment={"enabled": True})
        got = [(e["stage"], e["author"], e["hours"], e["reason"]) for e in out["events"]]
        self.assertEqual(got, [("dev", "dev", 2.0, None), ("rev", "dev", 0, "own"), ("mr", "lead", 0.25, None)])


class DemoTest(unittest.TestCase):
    def test_demo_data_drives_the_burndown(self):
        sys.path.insert(0, os.path.join(board.HERE, "demo"))
        import demo
        data = demo.Data("http://127.0.0.1:1")
        cfg, _ = board.read_config(os.path.join(board.HERE, "demo", "config.jsonc"))
        board.configure(cfg)
        detail = {k: (data.histories[k], board.estimate(data.comments(k)), {w["id"]: w["started"] for w in data.worklogs.get(k, [])})
                  for k in data.histories}
        with mock.patch.object(board, "search", side_effect=lambda jql, f: data.search(jql, f)["issues"]), \
                mock.patch.object(board, "issue_detail", side_effect=lambda k, u: detail[k]):
            out = board.burn_events(board.today(), board.today(), {"id": 42})
        mine = [r for r in out["remaining"] if r["author"] == "demo-alex"]
        self.assertEqual(sum(max(0, r["before"] - r["after"]) for r in mine), 4.25)
        self.assertEqual(sorted(e["stage"] for e in out["events"] if e["author"] == "demo-alex"), ["dev", "mr", "rev"])
        self.assertEqual([r["key"] for r in out["doneLeft"]], ["DEMO-41"])

    def test_check_reports_channel_candidates_and_matches(self):
        import contextlib
        import io
        sys.path.insert(0, os.path.join(board.HERE, "demo"))
        import demo
        data = demo.Data("http://127.0.0.1:1")
        cfg, _ = board.read_config(os.path.join(board.HERE, "demo", "config.jsonc"))
        for channels in (cfg["channels"], []):
            board.configure(dict(cfg, channels=channels))
            buf = io.StringIO()
            with mock.patch.object(board, "search", side_effect=lambda jql, f: data.search(jql, f)["issues"]), \
                    mock.patch.object(board, "myself", return_value={"name": "Alex"}), \
                    mock.patch.object(board, "active_sprint", return_value=data.sprint()), contextlib.redirect_stdout(buf):
                self.assertEqual(board.check(), 0)
            text = buf.getvalue()
            self.assertIn("DEMO-1  Payments platform", text)
            if channels:
                self.assertIn("Channel 'Payments': ", text)
                self.assertIn("Channel 'Other (no channel)': ", text)
            else:
                self.assertIn("No channels configured", text)


if __name__ == "__main__":
    unittest.main()
