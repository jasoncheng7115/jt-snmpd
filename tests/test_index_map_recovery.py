"""index-map.json is the record of which ifIndex each adapter was given.

Losing it is the expensive failure. ifIndex is handed out from a counter and
stored here against the adapter's LUID — it is **not** derived from the LUID, so
an empty map means the next enumeration assigns indices in whatever order
GetIfTable2 happens to return. On a machine with one adapter the first one gets
1 either way, which is why a purge test never showed the problem. On a machine
with several, the ports are renumbered and a monitoring system treats the
renumbered ones as new: the graphs of every port whose index moved are orphaned.

The writer has always kept a .bak for exactly this, and until 1.1.0 the reader
never looked at it.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1] / "deploy" / "jt_agent.py"


def _loader(tmp_path, main, backup):
    """Run the real _load_index_map against files we control.

    Importing the agent fails on Linux, so the function is extracted. That is
    also why STATE_FILE is injected rather than patched.
    """
    tree = ast.parse(AGENT.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_load_index_map")
    logged: list[str] = []
    state = tmp_path / "index-map.json"
    if main is not None:
        state.write_text(main, encoding="utf-8")
    if backup is not None:
        (tmp_path / "index-map.json.bak").write_text(backup, encoding="utf-8")
    ns = {"json": json, "STATE_FILE": str(state),
          "log": lambda m, **k: logged.append(m)}
    exec(compile(ast.Module([fn], []), "<agent>", "exec"), ns)  # noqa: S102
    return ns["_load_index_map"](), logged


GOOD = json.dumps({"schema_version": 1, "next_if_index": 4,
                   "interfaces": {"0x1": {"if_index": 1}, "0x2": {"if_index": 2},
                                  "0x3": {"if_index": 3}}})
OLDER = json.dumps({"schema_version": 1, "next_if_index": 3,
                    "interfaces": {"0x1": {"if_index": 1}, "0x2": {"if_index": 2}}})


def test_a_healthy_file_is_used(tmp_path):
    got, logged = _loader(tmp_path, GOOD, OLDER)
    assert got["next_if_index"] == 4
    assert logged == [], "no news is no log line"


def test_a_truncated_file_falls_back_to_the_backup(tmp_path):
    """The realistic corruption: power lost mid-write, or a backup agent
    holding the file. Three adapters keep their indices instead of being
    renumbered from scratch."""
    got, logged = _loader(tmp_path, GOOD[: len(GOOD) // 2], OLDER)
    assert got["interfaces"]["0x2"]["if_index"] == 2
    assert any("index-map.json.bak" in m for m in logged), (
        "recovering silently hides that the main file is damaged")


def test_a_missing_file_falls_back_to_the_backup(tmp_path):
    got, _ = _loader(tmp_path, None, OLDER)
    assert got["next_if_index"] == 3


def test_json_that_is_not_a_map_is_rejected(tmp_path):
    """A file that parses but is the wrong shape is worse than one that does
    not parse, because it would be used."""
    got, _ = _loader(tmp_path, '["not", "a", "map"]', OLDER)
    assert got["interfaces"]["0x1"]["if_index"] == 1


def test_losing_both_says_what_it_will_cost(tmp_path):
    got, logged = _loader(tmp_path, None, None)
    assert got == {"schema_version": 1, "interfaces": {}, "next_if_index": 1}
    joined = " ".join(logged)
    assert "renumber" in joined, (
        "say what starting empty does, not just that it happened: on a "
        "multi-adapter host it renumbers ports")


def test_the_writer_still_keeps_the_backup_the_reader_depends_on():
    src = AGENT.read_text(encoding="utf-8")
    fn = next(ast.unparse(n) for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_save_index_map")
    assert ".bak" in fn, "the reader's fallback only exists if the writer keeps one"
    assert "fsync" in fn, "an unflushed write is what makes the .bak necessary"
