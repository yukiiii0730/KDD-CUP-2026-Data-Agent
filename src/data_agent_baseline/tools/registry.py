from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable

from data_agent_baseline.benchmark.schema import AnswerTable, PublicTask
from data_agent_baseline.tools.filesystem import (
    list_context_tree,
    read_csv_preview,
    read_doc_preview,
    read_json_preview,
    resolve_context_path,
)
from data_agent_baseline.tools.python_exec import execute_python_code
from data_agent_baseline.tools.sqlite import execute_read_only_sql, inspect_sqlite_schema

EXECUTE_PYTHON_TIMEOUT_SECONDS = 60


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    ok: bool
    content: dict[str, Any]
    is_terminal: bool = False
    answer: AnswerTable | None = None


ToolHandler = Callable[[PublicTask, dict[str, Any]], ToolExecutionResult]


def _list_context(task: PublicTask, action_input: dict[str, Any]) -> ToolExecutionResult:
    max_depth = int(action_input.get("max_depth", 4))
    return ToolExecutionResult(ok=True, content=list_context_tree(task, max_depth=max_depth))


def _read_csv(task: PublicTask, action_input: dict[str, Any]) -> ToolExecutionResult:
    path = str(action_input["path"])
    max_rows = int(action_input.get("max_rows", 50))
    return ToolExecutionResult(ok=True, content=read_csv_preview(task, path, max_rows=max_rows))


def _read_json(task: PublicTask, action_input: dict[str, Any]) -> ToolExecutionResult:
    path = str(action_input["path"])
    max_chars = int(action_input.get("max_chars", 6000))
    return ToolExecutionResult(ok=True, content=read_json_preview(task, path, max_chars=max_chars))


def _read_doc(task: PublicTask, action_input: dict[str, Any]) -> ToolExecutionResult:
    path = str(action_input["path"])
    max_chars = int(action_input.get("max_chars", 8000))
    return ToolExecutionResult(ok=True, content=read_doc_preview(task, path, max_chars=max_chars))


def _search_in_doc(task: PublicTask, action_input: dict[str, Any]) -> ToolExecutionResult:
    """Search for a keyword/pattern within a (potentially large) document, returning matching lines with context."""
    import re as _re
    path = str(action_input["path"])
    query = str(action_input["query"])
    context_lines = int(action_input.get("context_lines", 3))
    max_matches = int(action_input.get("max_matches", 20))

    from data_agent_baseline.tools.filesystem import resolve_context_path
    abs_path = resolve_context_path(task, path)
    try:
        text = abs_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        return ToolExecutionResult(ok=False, content={"error": str(exc)})

    lines = text.splitlines()
    file_size_chars = len(text)
    matches = []
    pattern = _re.compile(_re.escape(query), _re.IGNORECASE)
    for i, line in enumerate(lines):
        if pattern.search(line):
            start = max(0, i - context_lines)
            end = min(len(lines), i + context_lines + 1)
            snippet = "\n".join(f"{'>>>' if j==i else '   '} {lines[j]}" for j in range(start, end))
            matches.append({"line_no": i + 1, "snippet": snippet})
            if len(matches) >= max_matches:
                break

    return ToolExecutionResult(ok=True, content={
        "path": path,
        "file_size_chars": file_size_chars,
        "total_lines": len(lines),
        "query": query,
        "match_count": len(matches),
        "matches": matches,
    })


def _read_doc_page(task: PublicTask, action_input: dict[str, Any]) -> ToolExecutionResult:
    """Read a specific page/chunk of a large document by character offset.
    Use this to paginate through documents larger than 8000 chars."""
    path = str(action_input["path"])
    start_char = int(action_input.get("start_char", 0))
    max_chars = int(action_input.get("max_chars", 12000))

    from data_agent_baseline.tools.filesystem import resolve_context_path
    abs_path = resolve_context_path(task, path)
    try:
        text = abs_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        return ToolExecutionResult(ok=False, content={"error": str(exc)})

    total_chars = len(text)
    chunk = text[start_char: start_char + max_chars]
    next_start = start_char + max_chars
    has_more = next_start < total_chars

    return ToolExecutionResult(ok=True, content={
        "path": path,
        "total_chars": total_chars,
        "start_char": start_char,
        "end_char": start_char + len(chunk),
        "has_more": has_more,
        "next_start_char": next_start if has_more else None,
        "content": chunk,
    })


def _inspect_sqlite_schema(task: PublicTask, action_input: dict[str, Any]) -> ToolExecutionResult:
    path = resolve_context_path(task, str(action_input["path"]))
    return ToolExecutionResult(ok=True, content=inspect_sqlite_schema(path))


def _execute_context_sql(task: PublicTask, action_input: dict[str, Any]) -> ToolExecutionResult:
    path = resolve_context_path(task, str(action_input["path"]))
    sql = str(action_input["sql"])
    limit = int(action_input.get("limit", 500))
    return ToolExecutionResult(ok=True, content=execute_read_only_sql(path, sql, limit=limit))


def _execute_python(task: PublicTask, action_input: dict[str, Any]) -> ToolExecutionResult:
    code = str(action_input["code"])
    content = execute_python_code(
        context_root=task.context_dir,
        code=code,
        timeout_seconds=EXECUTE_PYTHON_TIMEOUT_SECONDS,
    )
    return ToolExecutionResult(ok=bool(content.get("success")), content=content)


def _query_csv_duckdb(task: PublicTask, action_input: dict[str, Any]) -> ToolExecutionResult:
    """Execute a DuckDB SQL query against CSV files in the context directory.

    File paths inside the SQL must be relative filenames (no directory prefix).
    The tool resolves them to the task context directory automatically.
    """
    sql = str(action_input["sql"])
    limit = int(action_input.get("limit", 500))

    # Build a Python snippet that runs the DuckDB query in the context directory
    # Using read_csv_auto() allows referencing files by bare filename.
    code = (
        "import duckdb, os, math, json, datetime\n"
        f"os.chdir({repr(str(task.context_dir))})\n"
        "conn = duckdb.connect()\n"
        f"result = conn.execute({repr(sql)}).fetchdf()\n"
        f"result = result.head({limit})\n"
        "def _safe(v):\n"
        "    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):\n"
        "        return None\n"
        "    if isinstance(v, (datetime.date, datetime.datetime)):\n"
        "        return v.isoformat()\n"
        "    try:\n"
        "        import pandas as _pd\n"
        "        if isinstance(v, _pd.Timestamp):\n"
        "            return v.isoformat() if not _pd.isna(v) else None\n"
        "        if isinstance(v, _pd.NaT.__class__) and _pd.isna(v):\n"
        "            return None\n"
        "    except Exception:\n"
        "        pass\n"
        "    return v\n"
        "rows = [[_safe(v) for v in row] for row in result.values.tolist()]\n"
        "print(json.dumps({'columns': list(result.columns), 'rows': rows}))\n"
    )
    raw_content = execute_python_code(
        context_root=task.context_dir,
        code=code,
        timeout_seconds=EXECUTE_PYTHON_TIMEOUT_SECONDS,
    )
    if not raw_content.get("success"):
        return ToolExecutionResult(ok=False, content=raw_content)

    try:
        import json as _json
        parsed = _json.loads(raw_content.get("output", ""))
        return ToolExecutionResult(ok=True, content=parsed)
    except Exception as exc:
        return ToolExecutionResult(ok=False, content={"error": str(exc), "raw": raw_content})


def _split_name_columns_if_applicable(
    task: PublicTask,
    columns: list[str],
    rows: list[list[Any]],
) -> tuple[list[str], list[list[Any]]]:
    """Auto-split a combined 'full_name' column into first_name/last_name if:
    - The answer has a column whose name matches 'full_name' or 'fullname'
    - All values in that column are "First Last" style (exactly 2 space-separated words)
    """
    import re as _re

    # Only match explicit full_name-style column names (not generic "name", "event_name", etc.)
    NAME_COL_PATTERN = _re.compile(r"^(full.?name)$", _re.IGNORECASE)

    name_col_idx = None
    for i, col in enumerate(columns):
        if NAME_COL_PATTERN.match(col):
            # Check if values look like "First Last" (exactly 2 space-separated words)
            if rows and all(
                isinstance(r[i], str) and len(r[i].split()) == 2
                for r in rows
            ):
                name_col_idx = i
                break

    if name_col_idx is None:
        return columns, rows

    # Split the combined column into first_name / last_name
    new_columns = list(columns)
    split_first = "first_name"
    split_last = "last_name"
    new_columns = (
        columns[:name_col_idx] + [split_first, split_last] + columns[name_col_idx + 1:]
    )
    new_rows = []
    for row in rows:
        full = str(row[name_col_idx])
        parts = full.split(" ", 1)
        first = parts[0] if len(parts) > 0 else ""
        last = parts[1] if len(parts) > 1 else ""
        new_rows.append(list(row[:name_col_idx]) + [first, last] + list(row[name_col_idx + 1:]))

    return new_columns, new_rows


def _answer(task: PublicTask, action_input: dict[str, Any]) -> ToolExecutionResult:
    columns = action_input.get("columns")
    rows = action_input.get("rows")
    if not isinstance(columns, list) or not columns or not all(isinstance(item, str) for item in columns):
        raise ValueError("answer.columns must be a non-empty list of strings.")
    if not isinstance(rows, list):
        raise ValueError("answer.rows must be a list.")

    normalized_rows: list[list[Any]] = []
    for row in rows:
        if not isinstance(row, list):
            raise ValueError("Each answer row must be a list.")
        if len(row) != len(columns):
            raise ValueError("Each answer row must match the number of columns.")
        normalized_rows.append(list(row))

    # Auto-fix: split combined "full_name" columns when source data has separate fields
    columns, normalized_rows = _split_name_columns_if_applicable(task, list(columns), normalized_rows)

    answer = AnswerTable(columns=list(columns), rows=normalized_rows)
    return ToolExecutionResult(
        ok=True,
        content={
            "status": "submitted",
            "column_count": len(columns),
            "row_count": len(normalized_rows),
        },
        is_terminal=True,
        answer=answer,
    )


@dataclass(slots=True)
class ToolRegistry:
    specs: dict[str, ToolSpec]
    handlers: dict[str, ToolHandler]

    def describe_for_prompt(self) -> str:
        lines = []
        for name in sorted(self.specs):
            spec = self.specs[name]
            lines.append(f"- {spec.name}: {spec.description}")
            lines.append(f"  input_schema: {spec.input_schema}")
        return "\n".join(lines)

    def execute(self, task: PublicTask, action: str, action_input: dict[str, Any]) -> ToolExecutionResult:
        if action not in self.handlers:
            raise KeyError(f"Unknown tool: {action}")
        return self.handlers[action](task, action_input)


def create_default_tool_registry() -> ToolRegistry:
    specs = {
        "answer": ToolSpec(
            name="answer",
            description="Submit the final answer table. This is the only valid terminating action.",
            input_schema={
                "columns": ["column_name"],
                "rows": [["value_1"]],
            },
        ),
        "execute_context_sql": ToolSpec(
            name="execute_context_sql",
            description="Run a read-only SQL query against a sqlite/db file inside context.",
            input_schema={"path": "relative/path/to/file.sqlite", "sql": "SELECT ...", "limit": 500},
        ),
        "execute_python": ToolSpec(
            name="execute_python",
            description=(
                "Execute arbitrary Python code with the task context directory as the "
                "working directory. Libraries available: pandas, polars, duckdb, numpy. "
                "The tool returns the code's captured stdout as `output`. "
                f"The execution timeout is fixed at {EXECUTE_PYTHON_TIMEOUT_SECONDS} seconds."
            ),
            input_schema={
                "code": "import pandas as pd\ndf = pd.read_csv('file.csv')\nprint(df.head())",
            },
        ),
        "inspect_sqlite_schema": ToolSpec(
            name="inspect_sqlite_schema",
            description="Inspect tables and columns in a sqlite/db file inside context.",
            input_schema={"path": "relative/path/to/file.sqlite"},
        ),
        "list_context": ToolSpec(
            name="list_context",
            description="List files and directories available under context.",
            input_schema={"max_depth": 4},
        ),
        "query_csv_duckdb": ToolSpec(
            name="query_csv_duckdb",
            description=(
                "Execute a DuckDB SQL query directly on CSV files in the context directory. "
                "Reference files by bare filename using read_csv_auto(). "
                "Supports JOIN, GROUP BY, HAVING, ORDER BY, DISTINCT, and all SQL aggregations. "
                "Example: SELECT a.ID, b.Name FROM read_csv_auto('file1.csv') a "
                "JOIN read_csv_auto('file2.csv') b ON a.id=b.id WHERE a.status='active'"
            ),
            input_schema={
                "sql": "SELECT col1, col2 FROM read_csv_auto('file.csv') WHERE condition",
                "limit": 500,
            },
        ),
        "read_csv": ToolSpec(
            name="read_csv",
            description="Read a preview of a CSV file inside context (first N rows).",
            input_schema={"path": "relative/path/to/file.csv", "max_rows": 50},
        ),
        "read_doc": ToolSpec(
            name="read_doc",
            description="Read the start of a text document. Returns a preview and total_chars. If total_chars > 8000, use read_doc_page for more content or search_in_doc to find specific information.",
            input_schema={"path": "relative/path/to/file.md", "max_chars": 8000},
        ),
        "read_doc_page": ToolSpec(
            name="read_doc_page",
            description=(
                "Read a specific chunk of a large document by character offset. "
                "Use when read_doc is truncated (has_more=True) or when the file is known to be large. "
                "Chain calls with start_char=previous end_char to read the full document in pages."
            ),
            input_schema={
                "path": "relative/path/to/file.md",
                "start_char": 0,
                "max_chars": 12000,
            },
        ),
        "read_json": ToolSpec(
            name="read_json",
            description="Read a preview of a JSON file inside context.",
            input_schema={"path": "relative/path/to/file.json", "max_chars": 6000},
        ),
        "search_in_doc": ToolSpec(
            name="search_in_doc",
            description=(
                "Search for a keyword or name within a (possibly large) document. "
                "Returns matching lines with surrounding context. "
                "Use this FIRST on large documents instead of reading the whole file — "
                "e.g. search_in_doc(path='doc/major.md', query='Angela Sanders') to find a person's record."
            ),
            input_schema={
                "path": "relative/path/to/file.md",
                "query": "keyword or name to search for",
                "context_lines": 3,
                "max_matches": 20,
            },
        ),
    }
    handlers = {
        "answer": _answer,
        "execute_context_sql": _execute_context_sql,
        "execute_python": _execute_python,
        "inspect_sqlite_schema": _inspect_sqlite_schema,
        "list_context": _list_context,
        "query_csv_duckdb": _query_csv_duckdb,
        "read_csv": _read_csv,
        "read_doc": _read_doc,
        "read_doc_page": _read_doc_page,
        "read_json": _read_json,
        "search_in_doc": _search_in_doc,
    }
    return ToolRegistry(specs=specs, handlers=handlers)
