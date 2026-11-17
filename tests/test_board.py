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

    def test_missing_board_id_is_an_error(self):
        with self.assertRaises(board.ConfigError):
            board.configure(board.deep_merge(board.DEFAULTS, {}))

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


if __name__ == "__main__":
    unittest.main()
