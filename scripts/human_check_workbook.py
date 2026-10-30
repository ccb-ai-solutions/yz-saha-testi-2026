"""The human check of the RAG judge (METHODOLOGY.md §3.7) as ONE Excel workbook instead of five long CSV sheets.

    python scripts/human_check_workbook.py build     # -> results/human_check/human_check_sample.xlsx
    python scripts/human_check_workbook.py merge     # its verdicts -> every session's rag_qa/human_check/sheet.csv

build: takes ~20 % (--fraction) of every session's blind sheet - of its answerable and of its unanswerable questions
separately (at least one of each), in sheet order; `run.py human-export` wrote every sheet in random order, so these
are random samples - and shuffles them together.  The workbook shows what the sheets show (question, documents,
reference answer, key facts, one system's answer), never the system, the session or the judge's verdict.  Verdicts
already in a sheet are carried over.  Which sheet and row each line came from is in
<workbook>_map_do_not_open.csv (merge needs it; it names the session, i.e. the provider).  An existing workbook is
never overwritten.

merge: checks every verdict - refused_correctly / hallucinated only for questions the documents cannot answer, the
other four only for answerable ones - and writes human_verdict and human_notes of every workbook line into its row
of the session sheet (nothing else changes; a row whose question or answer changed since the build - the session was
exported again - is not touched and is reported).  Lines without a verdict are not counted: analysis/analyze.py
reports the agreement on the rows that have one, so merge can run after every sitting.  Lines that were discussed
with Claude while they were filled are listed in <workbook>_calibration_rows.txt; merge marks their notes
'calibration', and the report shows the agreement with and without them (the judge is a Claude model too).

Needs openpyxl.  Nothing is sent anywhere.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import math
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness.config import RESULTS_DIR  # noqa: E402

VERDICTS_ANSWERABLE = ("correct", "partially_correct", "incorrect", "refused_incorrectly")
VERDICTS_UNANSWERABLE = ("refused_correctly", "hallucinated")
VERDICTS = VERDICTS_ANSWERABLE + VERDICTS_UNANSWERABLE
# columns A-D stay on screen (frozen); scroll right for the key facts, the answer, the reference and the documents
COLUMNS = ["row", "human_verdict", "answerable", "question", "key_facts", "model_answer", "reference_answer",
           "human_notes", "context"]
WIDTHS = {"row": 5, "human_verdict": 20, "answerable": 11, "question": 36, "key_facts": 38, "model_answer": 62,
          "reference_answer": 42, "human_notes": 28, "context": 100}
FROZEN = "E2"
SIZED_BY = ("question", "key_facts", "model_answer", "reference_answer")   # the row height fits these, not the documents
EXCEL_CELL_MAX = 32_000          # Excel refuses cells over 32,767 characters
SEED = 20261017

HOW_TO = [
    "How to fill  (one line per answer; only human_verdict is needed - choose it from the list in the green cell)",
    "",
    "answerable = no  (yellow; the documents do not contain the answer):",
    "   refused_correctly    it says the documents do not cover this (pointing to related content that IS in them is fine)",
    "   hallucinated         it answers anyway - also after a disclaimer, also from general knowledge",
    "",
    "answerable = yes  (compare the answer with key_facts; the reference answer shows them in context):",
    "   correct              every key fact is there, nothing is wrong",
    "   partially_correct    some key facts are there (or only half of one), none of them wrong",
    "   incorrect            the key facts are missing, OR a number / date / duration / amount of a key fact is wrong,",
    "                        OR the answer is about something else",
    "   refused_incorrectly  it declines (says the documents do not cover it) although they do -",
    "                        if it still gives some key facts, judge those instead (partially_correct / correct)",
    "",
    "Wording does not matter, the facts do: paraphrases, other word order, numbers in words or digits are all fine.",
    "Extra details that are NOT in the reference answer change nothing - unless the documents say otherwise.  Read the",
    "documents (context, last column) only when the answer adds such a detail (a number, a condition, a deadline):",
    "   the documents contradict it  ->  not correct: incorrect if it is about a key fact, otherwise partially_correct",
    "   the documents do not mention it  ->  ignore it",
    "To read a long cell, click it and read it in the formula bar (Ctrl+Shift+U makes the bar taller), or make the row",
    "taller.",
    "",
    "human_notes is optional: a few words only when a line is odd - e.g. 'reference answer looks wrong', 'unsure between",
    "correct and partially_correct', 'extra detail contradicts K2'.  The column human_faithfulness_0_1 of the CSV",
    "sheets is not needed.",
    "",
    "Go through the lines in order and do not skip difficult ones - skipping would bias the check.  Any number of",
    "lines counts; all of them (about 66) is the goal, about 40 the minimum.  Filtering is fine (e.g. the empty",
    "verdicts, with the arrow in the header); please do not sort the sheet.",
    "Save the file (keep it .xlsx) and tell Claude; the verdicts are then copied into the session sheets with",
    "    python scripts/human_check_workbook.py merge",
    "and analysis/analyze.py reports how often the judge agrees with you (Cohen's kappa).  Do not open the",
    "_map_do_not_open.csv file or the KEY_do_not_share_with_annotators.csv files before you have finished.",
]


def _need_openpyxl():
    try:
        import openpyxl
    except ImportError:
        raise SystemExit("openpyxl is needed: .\\.venv\\Scripts\\python.exe -m pip install openpyxl") from None
    return openpyxl


def map_path(workbook: Path) -> Path:
    return workbook.with_name(workbook.stem + "_map_do_not_open.csv")


def calibration_path(workbook: Path) -> Path:
    return workbook.with_name(workbook.stem + "_calibration_rows.txt")


def read_calibration_rows(workbook: Path) -> set[int]:
    """Workbook lines discussed with Claude while they were filled ('#' starts a comment; numbers separated by spaces,
    commas or new lines).  merge marks their notes 'calibration'; the report shows the agreement with and without."""
    p = calibration_path(workbook)
    if not p.exists():
        return set()
    rows = set()
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.split("#", 1)[0]
        rows.update(int(t) for t in re.split(r"[\s,;]+", line.strip()) if t)
    return rows


def _row_no(v) -> int | None:
    """A row number as Excel keeps it (12, 12.0 or '12')."""
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else None


def _tag_calibration(notes: str) -> str:
    if "calibration" in notes.lower():
        return notes
    return f"{notes} [calibration]" if notes else "calibration"


def find_sheets(results: Path) -> list[Path]:
    return sorted(results.glob("track-*/*/*/rag_qa/human_check/sheet.csv"))


def read_csv(p: Path) -> tuple[list[str], list[dict], str]:
    """A CSV as the harness writes it (comma) or as a Turkish Excel saves it again (semicolon)."""
    with open(p, encoding="utf-8-sig", newline="") as f:
        text = f.read()
    first = text.split("\n", 1)[0]
    delim = ";" if first.count(";") > first.count(",") else ","
    r = csv.DictReader(io.StringIO(text, newline=""), delimiter=delim)
    rows = list(r)
    return list(r.fieldnames or []), rows, delim


def read_sheet(p: Path) -> tuple[list[str], list[dict]]:
    fields, rows, _ = read_csv(p)
    return fields, rows


def fingerprint(r: dict) -> str:
    """Question + answer, whitespace-insensitive: tells merge whether a sheet row is still the one that was shown."""
    norm = lambda s: " ".join(str(s or "").split())
    return hashlib.sha256(f"{norm(r.get('question'))}\x1f{norm(r.get('model_answer'))}".encode()).hexdigest()[:16]


def _answerable(r: dict) -> str:
    return str(r.get("answerable") or "").strip().lower()


def allowed_verdicts(answerable: str) -> tuple[str, ...]:
    return {"yes": VERDICTS_ANSWERABLE, "no": VERDICTS_UNANSWERABLE}.get(answerable, VERDICTS)


def pick(rows: list[dict], fraction: float) -> list[dict]:
    """~fraction of the answerable and of the unanswerable rows (at least one of each), first ones, sheet order."""
    groups: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        groups.setdefault(_answerable(r), []).append(i)
    keep: set[int] = set()
    for idx in groups.values():
        keep.update(idx[:min(len(idx), max(1, math.floor(len(idx) * fraction + 0.5)))])
    return [r for i, r in enumerate(rows) if i in keep]


def _norm_verdict(v) -> str:
    return re.sub(r"[\s\-]+", "_", str(v or "").strip().lower())


def _row_height(vals: dict) -> float:
    lines = 1
    for c in SIZED_BY:
        per_line = max(1, int(WIDTHS[c] * 1.1))
        text = str(vals.get(c) or "")
        lines = max(lines, sum(max(1, math.ceil(len(par) / per_line)) for par in text.split("\n")))
    return float(min(409, max(45, 15 * lines + 6)))


def build(results: Path, out: Path, fraction: float = 0.20, seed: int = SEED) -> tuple[Path, Path, int]:
    openpyxl = _need_openpyxl()
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation
    mapf = map_path(out)
    if out.exists() or mapf.exists():
        raise SystemExit(f"{out} already exists - not overwritten (it may hold your verdicts); "
                         "use --workbook <another name>, or move it away first")
    picked = []
    for sheet in find_sheets(results):
        _, rows = read_sheet(sheet)
        picked += [(sheet, r) for r in pick(rows, fraction)]
    if not picked:
        raise SystemExit(f"no human-check sheets under {results} - run `run.py human-export` first")
    random.Random(seed).shuffle(picked)

    def text(v):
        s = ILLEGAL_CHARACTERS_RE.sub("", str(v or ""))
        return (s[:EXCEL_CELL_MAX] + " ... [cut: Excel's cell limit]" if len(s) > EXCEL_CELL_MAX else s) or None

    def facts(v):
        parts = [p.strip() for p in str(v or "").split(" | ") if p.strip()]
        return text("\n".join("- " + p for p in parts))

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Check"
    ws.append(COLUMNS)
    heights, by_answerable = [], {}
    for i, (_, r) in enumerate(picked, 1):
        verdict = _norm_verdict(r.get("human_verdict"))
        vals = {"row": i, "human_verdict": verdict or None, "answerable": _answerable(r) or None,
                "question": text(r.get("question")), "key_facts": facts(r.get("key_facts")),
                "model_answer": text(r.get("model_answer")), "reference_answer": text(r.get("reference_answer")),
                "human_notes": text((r.get("human_notes") or "").strip()), "context": text(r.get("context"))}
        ws.append([vals[c] for c in COLUMNS])
        heights.append(_row_height(vals))
        by_answerable.setdefault(_answerable(r), []).append(i + 1)
    wrap = Alignment(wrap_text=True, vertical="top")
    head_fill = PatternFill("solid", fgColor="D9D9D9")
    todo = PatternFill("solid", fgColor="E2EFDA")
    unans = PatternFill("solid", fgColor="FFF2CC")
    for j, name in enumerate(COLUMNS, 1):
        ws.column_dimensions[get_column_letter(j)].width = WIDTHS[name]
        h = ws.cell(1, j)
        h.font, h.fill, h.alignment = Font(bold=True), head_fill, wrap
    iv, ia = COLUMNS.index("human_verdict"), COLUMNS.index("answerable")
    for row, height in zip(ws.iter_rows(min_row=2), heights):
        for c in row:
            c.alignment = wrap
            if c.data_type == "f":          # an answer that starts with '=' is text, not an Excel formula
                c.data_type = "s"
        row[iv].fill = todo
        if str(row[ia].value or "").strip().lower() == "no":
            row[ia].fill = unans
        ws.row_dimensions[row[0].row].height = height
    ws.freeze_panes = FROZEN
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{len(picked) + 1}"
    col = get_column_letter(iv + 1)
    prompts = {"yes": ("answerable = yes", ("correct / partially_correct / incorrect / refused_incorrectly - "
                                            "compare the answer with key_facts (see 'How to fill')")),
               "no": ("answerable = no", ("refused_correctly (it says the documents do not cover it) / "
                                          "hallucinated (it answers anyway)"))}
    for ans, rows_ in sorted(by_answerable.items()):
        allowed = allowed_verdicts(ans)
        title, prompt = prompts.get(ans, ("human_verdict", "one of the six verdicts (see 'How to fill')"))
        dv = DataValidation(type="list", formula1='"' + ",".join(allowed) + '"', allow_blank=True,
                            showErrorMessage=True, errorTitle="human_verdict",
                            error="Choose from the list: " + ", ".join(allowed),
                            showInputMessage=True, promptTitle=title, prompt=prompt)
        for r in rows_:
            dv.add(f"{col}{r}")
        ws.add_data_validation(dv)
    how = wb.create_sheet("How to fill")
    for line in HOW_TO:
        how.append([line])
    how.column_dimensions["A"].width = 125
    how.cell(1, 1).font = Font(bold=True)
    wb.active = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_wb, tmp_map = out.with_name(out.stem + ".partial.xlsx"), mapf.with_name(mapf.name + ".partial")
    wb.save(tmp_wb)
    with open(tmp_map, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["row", "sheet", "sample_id", "fingerprint"])
        for i, (sheet, r) in enumerate(picked, 1):
            w.writerow([i, sheet.relative_to(results).as_posix(), r.get("sample_id"), fingerprint(r)])
    tmp_map.replace(mapf)
    tmp_wb.replace(out)
    return out, mapf, len(picked)


def read_workbook(workbook: Path) -> list[dict]:
    """The lines of the Check sheet: row number, verdict (normalised), notes."""
    openpyxl = _need_openpyxl()
    wb = openpyxl.load_workbook(workbook, data_only=True, read_only=True)
    try:
        it = wb["Check"].iter_rows(values_only=True)
        head = [str(v or "").strip() for v in next(it)]
        missing = [c for c in ("row", "human_verdict", "human_notes") if c not in head]
        if missing:
            raise SystemExit(f"{workbook.name}: the Check sheet has no column {', '.join(missing)}")
        ix = {c: head.index(c) for c in ("row", "human_verdict", "human_notes")}
        lines = []
        for vals in it:
            row, verdict, notes = (vals[ix[c]] if ix[c] < len(vals) else None
                                   for c in ("row", "human_verdict", "human_notes"))
            if row in (None, ""):
                continue
            lines.append({"row": row, "verdict": _norm_verdict(verdict), "notes": str(notes or "").strip()})
        return lines
    finally:
        wb.close()


def merge(results: Path, workbook: Path) -> dict:
    mapf = map_path(workbook)
    if not workbook.exists() or not mapf.exists():
        raise SystemExit(f"{workbook} or its map {mapf.name} is missing")
    _, maprows, _ = read_csv(mapf)
    where = {int(r["row"]): r for r in maprows}
    problems: list[str] = []
    calibration = read_calibration_rows(workbook)
    if calibration - set(where):
        problems.append(f"{calibration_path(workbook).name} names rows that are not in the workbook: "
                        f"{', '.join(map(str, sorted(calibration - set(where))))}")
    wanted: dict[str, dict[str, tuple]] = {}
    lines = read_workbook(workbook)
    for ln in lines:
        n = _row_no(ln["row"])
        if n is None:
            problems.append(f"a line with row '{ln['row']}' is not one of the workbook's lines - skipped")
            continue
        if n in calibration:
            ln["notes"] = _tag_calibration(ln["notes"])
        if n not in where:
            problems.append(f"row {n} is not in {mapf.name} - skipped")
            continue
        if ln["verdict"] and ln["verdict"] not in VERDICTS:
            problems.append(f"row {n}: '{ln['verdict']}' is not one of the six verdicts - not copied")
            continue
        m = where[n]
        wanted.setdefault(m["sheet"], {})[m["sample_id"]] = (n, ln["verdict"], ln["notes"], m["fingerprint"])
    copied, cleared, per_session = 0, 0, {}
    for sheet, upd in sorted(wanted.items()):
        p = results / sheet
        session = Path(sheet).parts[2] if len(Path(sheet).parts) > 2 else sheet
        if not p.exists():
            problems.append(f"{sheet} is missing - its {len(upd)} line(s) were not copied")
            continue
        fields, rows, _ = read_csv(p)
        if any(None in r for r in rows):
            problems.append(f"{sheet} has rows with more fields than its header - not changed")
            continue
        for col in ("human_verdict", "human_notes"):
            if col not in fields:
                fields.append(col)
        by_id = {r.get("sample_id"): r for r in rows}
        changed, stale = False, []
        for sid, (n, verdict, notes, fp) in sorted(upd.items(), key=lambda kv: kv[1][0]):
            r = by_id.get(sid)
            if r is None or fingerprint(r) != fp:
                stale.append(n)
                continue
            if verdict and verdict not in allowed_verdicts(_answerable(r)):
                problems.append(f"row {n}: '{verdict}' does not fit a question that is "
                                f"{'answerable' if _answerable(r) == 'yes' else 'not answerable'} - not copied")
                continue
            old = (_norm_verdict(r.get("human_verdict")), (r.get("human_notes") or "").strip())
            if (verdict, notes) != old:
                cleared += int(bool(old[0]) and not verdict)
                r["human_verdict"], r["human_notes"] = verdict, notes
                changed = True
            if verdict:
                copied += 1
                per_session[session] = per_session.get(session, 0) + 1
        if stale:
            problems.append(f"{sheet} was exported again after the workbook was built: rows "
                            f"{', '.join(map(str, stale))} no longer match it - not copied (build a new workbook)")
        if changed:     # written beside it first, so an error never leaves a half-written sheet
            tmp = p.with_name(p.name + ".partial")
            with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                w.writerows(rows)
            tmp.replace(p)
    return {"lines": len(lines), "filled": sum(1 for ln in lines if ln["verdict"]), "copied": copied,
            "per_session": per_session, "cleared": cleared, "problems": problems,
            "calibration": sum(1 for ln in lines if ln["verdict"] and "calibration" in ln["notes"].lower())}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=["build", "merge"])
    ap.add_argument("--results", default=str(RESULTS_DIR))
    ap.add_argument("--workbook", default=None, help="default: <results>/human_check/human_check_sample.xlsx")
    ap.add_argument("--fraction", type=float, default=0.20,
                    help="build: share of each sheet's answerable and of its unanswerable rows (default 0.20)")
    a = ap.parse_args(argv)
    if not 0 < a.fraction <= 1:
        ap.error("--fraction must be above 0 and at most 1")
    results = Path(a.results)
    wb = Path(a.workbook) if a.workbook else results / "human_check" / "human_check_sample.xlsx"
    if a.cmd == "build":
        out, mapf, n = build(results, wb, a.fraction)
        print(f"{n} answers to check -> {out}\n(row map for merge: {mapf.name} - do not open it before you finish)")
    else:
        r = merge(results, wb)
        print(f"{r['filled']} of {r['lines']} lines have a verdict; {r['copied']} are now in the session sheets "
              f"({', '.join(f'{s} {k}' for s, k in sorted(r['per_session'].items())) or 'none'})")
        if r["cleared"]:
            print(f"{r['cleared']} verdict(s) removed from the sheets (emptied in the workbook)")
        if r["calibration"]:
            print(f"{r['calibration']} of them marked 'calibration' (discussed with Claude while filling): the report "
                  "shows the agreement with and without them")
        for p in r["problems"]:
            print("  !! " + p)
        if r["problems"]:
            sys.exit(2)


if __name__ == "__main__":
    main()
