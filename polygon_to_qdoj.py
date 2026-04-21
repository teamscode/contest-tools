#!/usr/bin/env python3
"""
Usage:
  python polygon_to_qdoj.py <input_polygon_zip> <output_qdoj_zip>

- Extracts the Polygon package ZIP
- Reads problem.xml for limits, tests, and file patterns
- Reads statement-sections/english/*.tex for description/input/output + samples
- Builds problem.json (QDOJ-style) with display_id derived from problem URL
- Packages into <output_qdoj_zip> with a single top-level directory:
    <top>/problem.json
    <top>/testcase/{01,01.a,02,02.a,...}

Rules:
- display_id is derived from the Polygon problem URL path
- tags is a single element list with today's date (America/New_York) in YYYY-MM-DD
- Replace '---' with a horizontal line <hr/> when it's the only thing on a line; otherwise with an em dash (&mdash;)
- If there is an em dash within $...$ math, change it to a hyphen '-'
- Warn if the sum of test-case scores is not exactly 100
- Warn if any non-ASCII characters appear in converted HTML or sample I/O
"""

import os, re, json, zipfile, tempfile, shutil, sys
from pathlib import Path
from datetime import datetime
from typing import Optional
try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("America/New_York")
except Exception:
    _TZ = None
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

def die(msg: str, code: int = 1):
    print(f"[error] {msg}", file=sys.stderr)
    sys.exit(code)

def today_tag() -> str:
    if _TZ is not None:
        return datetime.now(_TZ).date().isoformat()
    return datetime.now().date().isoformat()

def strip_tex_comments(s: str) -> str:
    lines = []
    for line in s.splitlines():
        i = 0
        cut = len(line)
        while i < len(line):
            if line[i] == '%':
                # count backslashes before %
                b = 0
                j = i - 1
                while j >= 0 and line[j] == '\\':
                    b += 1
                    j -= 1
                if b % 2 == 0:  # unescaped
                    cut = i
                    break
            i += 1
        lines.append(line[:cut])
    return "\n".join(lines).strip()

def latex_inline(code: str) -> str:
    rep = {
        r"\\leq": "&le;",
        r"\\le": "&le;",
        r"\\geq": "&ge;",
        r"\\ge": "&ge;",
        r"\\times": "&times;",
        r"\\cdot": "&middot;",
        r"\\neq": "&ne;",
        r"\\pm": "&plusmn;",
        r"\\infty": "&infin;",
    }
    for k, v in rep.items():
        code = re.sub(k, v, code)
    return code

def tex_to_html(tex: str) -> str:
    if not tex:
        return ""
    s = strip_tex_comments(tex)

    # First, protect inline math and apply special dash rules inside math.
    placeholders = []
    def math_inline(m):
        content = m.group(1)
        # Replace em-dash variants within math with a single hyphen
        content = content.replace("—", "-")
        content = content.replace("&mdash;", "-")
        content = re.sub(r"---", "-", content)
        # Now apply simple latex->entities inside math
        content = latex_inline(content)
        placeholders.append(content)
        return f"@@MATH{len(placeholders)-1}@@"
    s = re.sub(r"\$(.+?)\$", math_inline, s, flags=re.DOTALL)

    # \verbX...X
    def repl_verb(m):
        return "<code>" + (m.group(2) or "").replace("<","&lt;").replace(">","&gt;") + "</code>"
    s = re.sub(r"\\verb(.)(.*?)\1", repl_verb, s, flags=re.DOTALL)

    # braced styles
    def brace_repl(tag):
        def _r(m):
            inner = m.group(1)
            return f"<{tag}>" + inner + f"</{tag}>"
        return _r
    s = re.sub(r"\\texttt\{([^{}]*)\}", brace_repl("code"), s)
    s = re.sub(r"\\textbf\{([^{}]*)\}", brace_repl("strong"), s)
    s = re.sub(r"\\textit\{([^{}]*)\}", brace_repl("em"), s)
    s = re.sub(r"\\emph\{([^{}]*)\}", brace_repl("em"), s)

    # lists
    s = re.sub(r"\\begin\{itemize\}", "<ul>", s)
    s = re.sub(r"\\end\{itemize\}", "</ul>", s)
    s = re.sub(r"\\begin\{enumerate\}", "<ol>", s)
    s = re.sub(r"\\end\{enumerate\}", "</ol>", s)
    s = re.sub(r"\\item\s*", "<li>", s)

    # headings
    s = re.sub(r"\\section\*?\{([^{}]*)\}", r"<h2>\1</h2>", s)
    s = re.sub(r"\\subsection\*?\{([^{}]*)\}", r"<h3>\1</h3>", s)

    # Symbols (keep dash handling for non-math until paragraph stage)
    s = s.replace(r"\ldots", "&hellip;").replace(r"\dots", "&hellip;")

    # Split into paragraphs (blank-line delimited)
    paras = [p for p in re.split(r"\n\s*\n", s) if p.strip()]

    out_parts = []
    for p in paras:
        raw = p.strip()

        # If paragraph is just a line with '---' (possibly with spaces), make <hr/>
        if re.fullmatch(r"-{3}", raw) or re.fullmatch(r"\s*-{3}\s*", p):
            out_parts.append("<hr/>")
            continue

        # For other text, handle dash replacements: '---' -> &mdash;, '--' -> &ndash;
        replaced = re.sub(r"---", "&mdash;", p)
        replaced = re.sub(r"--", "&ndash;", replaced)

        out_parts.append(f"<p>{replaced.strip()}</p>")

    html = "".join(out_parts)

    # Restore math placeholders
    def restore_math(m):
        idx = int(m.group(1))
        return f"${placeholders[idx]}$"
    html = re.sub(r"@@MATH(\d+)@@", restore_math, html)

    return html

def build_name_from_pattern(pattern: str, idx: int) -> str:
    m = re.search(r'%0?(\d*)d', pattern)
    width = int(m.group(1)) if m and m.group(1) else 0
    num_str = str(idx).zfill(width) if width > 0 else str(idx)
    path = re.sub(r'%0?\d*d', num_str, pattern, count=1)
    return os.path.basename(path)

def memory_bytes_to_mb(b: int) -> int:
    return int(round(b / (1024 * 1024)))

def read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""

def find_problem_xml(extract_dir: Path) -> Path:
    cands = list(extract_dir.rglob("problem.xml"))
    if not cands:
        die("problem.xml not found in ZIP")
    # pick shallowest
    return sorted(cands, key=lambda p: len(p.parts))[0]


def warn_non_ascii(label: str, text: str):
    seen = {}
    for i, ch in enumerate(text):
        if ord(ch) > 127 and ch not in seen:
            seen[ch] = i
            print(f"WARNING: non-ASCII char U+{ord(ch):04X} {repr(ch)} in {label} at position {i}", file=sys.stderr)


def display_id_from_url(root) -> Optional[str]:
    url = root.get("url") or ""
    if not url:
        return None
    try:
        u = urlparse(url)
        path = (u.path or "").strip("/")
        first = path.split("/")[0] if path else None
        return first or None
    except Exception:
        return None


def main():
    if len(sys.argv) != 3:
        print(__doc__.strip())
        sys.exit(2)
    in_zip = Path(sys.argv[1]).resolve()
    out_zip = Path(sys.argv[2]).resolve()
    if not in_zip.exists():
        die(f"Input ZIP not found: {in_zip}")

    tmp = Path(tempfile.mkdtemp(prefix="poly_", dir=None))
    print("Temporary dir:", tmp)
    try:
        # Extract
        with zipfile.ZipFile(in_zip, "r") as zf:
            zf.extractall(tmp)

        xml_path = find_problem_xml(tmp)
        base_dir = xml_path.parent

        # Parse problem.xml
        root = ET.parse(xml_path).getroot()

        # Title (may be overridden by name.tex)
        title = None
        names = root.find("names")
        if names is not None:
            pref = None
            for el in names.findall("name"):
                if el.get("language") == "english" and el.get("value"):
                    pref = el.get("value"); break
            if not pref:
                for el in names.findall("name"):
                    if el.get("value"): pref = el.get("value"); break
            title = pref
        print("Title:", title)

        # Judging/testset
        judging = root.find("judging")
        testset = None
        if judging is not None:
            for ts in judging.findall("testset"):
                if ts.get("name") == "tests":
                    testset = ts; break
            if testset is None:
                testset = judging.find("testset")
        if testset is None:
            die("No <testset> in problem.xml")

        time_limit = int(testset.findtext("time-limit", default="2000"))
        mem_bytes = int(testset.findtext("memory-limit", default=str(256*1024*1024)))
        memory_limit = memory_bytes_to_mb(mem_bytes)
        input_pat = testset.findtext("input-path-pattern", default="tests/%02d")
        answer_pat = testset.findtext("answer-path-pattern", default="tests/%02d.a")

        print("TL:", time_limit, "ML:", memory_limit)

        tests_el = testset.find("tests")
        tests = tests_el.findall("test") if tests_el is not None else []

        test_case_score = []
        score_sum = 0
        for i, t in enumerate(tests, start=1):
            pts = t.get("points")
            try:
                score = int(round(float(pts))) if pts is not None else 0
            except (ValueError, TypeError):
                score = 0
            score_sum += score
            input_name = build_name_from_pattern(input_pat, i)
            output_name = build_name_from_pattern(answer_pat, i)
            test_case_score.append({"score": score, "input_name": input_name, "output_name": output_name})
            print("Test case:", input_name, output_name, " | ", score)

        if score_sum != 100:
            print(f"WARNING: test scores sum to {score_sum} (expected 100).", file=sys.stderr)

        # Sections (TeX -> HTML)
        sections_dir = base_dir / "statement-sections" / "english"
        desc_html = input_html = output_html = hint_html = None
        samples = []
        if sections_dir.exists():
            # Read name/legend/input/output/hint
            files = {
                "name": sections_dir / "name.tex",
                "legend": sections_dir / "legend.tex",
                "input": sections_dir / "input.tex",
                "output": sections_dir / "output.tex",
                "hint": sections_dir / "notes.tex",
            }
            if files["legend"].exists():
                print("Found legend.")
                desc_html = tex_to_html(read_text(files["legend"]))
                warn_non_ascii("description", desc_html)
            if files["input"].exists():
                print("Found input.")
                input_html = tex_to_html(read_text(files["input"]))
                warn_non_ascii("input_description", input_html)
            if files["output"].exists():
                print("Found output.")
                output_html = tex_to_html(read_text(files["output"]))
                warn_non_ascii("output_description", output_html)
            if files["hint"].exists():
                print("Found hint.")
                hint_html = tex_to_html(read_text(files["hint"]))
                warn_non_ascii("hint", hint_html)
            if files["name"].exists():
                print("Found name.")
                nm = re.sub(r"<.*?>", "", tex_to_html(read_text(files["name"]))).strip()
                if nm: title = nm

            # Samples: example.* + example.*.a
            for ex in sorted(sections_dir.glob("example.*")):
                if ex.suffix == ".a":
                    continue
                outp = sections_dir / (ex.name + ".a")
                inp_text = read_text(ex)
                out_text = read_text(outp) if outp.exists() else ""
                warn_non_ascii(f"sample {ex.name} input", inp_text)
                warn_non_ascii(f"sample {ex.name} output", out_text)
                samples.append({"input": inp_text, "output": out_text})
                print("Found example:", ex)

        # Detect SPJ checker — polygon puts it under <assets> or directly under root
        spj_info = None
        checker_el = root.find("assets/checker") or root.find("checker")
        if checker_el is not None:
            checker_name = checker_el.get("name", "")
            # Standard checkers (e.g. std::wcmp.checker) are not custom SPJ
            if not checker_name.startswith("std::"):
                src_el = checker_el.find("source")
                if src_el is not None:
                    checker_path = src_el.get("path", "")
                    checker_type = src_el.get("type", "")
                    # Try extracted path, then as-is relative to base_dir
                    checker_src = base_dir / checker_path
                    if not checker_src.exists():
                        checker_src = base_dir / "files" / os.path.basename(checker_path)
                    if checker_src.exists():
                        code = read_text(checker_src)
                    else:
                        # Fall back: read directly from ZIP
                        try:
                            with zipfile.ZipFile(in_zip) as zf:
                                code = zf.read(checker_path).decode("utf-8", errors="ignore")
                        except Exception:
                            code = None
                    if code:
                        if "cpp" in checker_type.lower() or checker_path.endswith((".cpp", ".cc")):
                            language = "C++11"
                        elif checker_path.endswith(".c"):
                            language = "C"
                        else:
                            language = "C++11"
                        spj_info = {"code": code, "language": language}
                        print(f"Found checker: {checker_path} (language: {language})")
                    else:
                        print(f"WARNING: checker source not found at {checker_path}", file=sys.stderr)

        is_spj = spj_info is not None

        # For SPJ problems, output_name is unused — set to "-"
        if is_spj:
            for tc in test_case_score:
                tc["output_name"] = "-"

        # Build JSON (with fixed display_id and date tag)
        out = {
            "display_id": (display_id_from_url(root) or "default_display_id"),
            "title": title or "Untitled Problem",
            "description": {"format": "html", "value": desc_html or ""},
            "input_description": {"format": "html", "value": input_html or ""},
            "output_description": {"format": "html", "value": output_html or ""},
            "hint": {"format": "html", "value": hint_html or ""},
            "test_case_score": test_case_score,
            "time_limit": time_limit,
            "memory_limit": memory_limit,
            "samples": samples,
            "tags": [today_tag()],
            "template": {},
            "source": "",
            "answers": [],
        }
        if spj_info:
            out["spj"] = spj_info

        top_name = "1"
        staging_root = Path(tempfile.mkdtemp(prefix="qdoj_", dir=tmp))
        top_dir = staging_root / top_name
        testcase_dir = top_dir / "testcase"
        testcase_dir.mkdir(parents=True, exist_ok=True)

        # Write problem.json
        (top_dir / "problem.json").write_text(json.dumps(out, ensure_ascii=False, indent=4), encoding="utf-8")

        # Copy test files according to patterns
        def resolve_src(pattern: str, produced_name: str) -> Path:
            stem = os.path.splitext(produced_name)[0]
            rel = re.sub(r'%0?\d*d', stem, pattern, count=1)
            return base_dir / rel

        for tc in test_case_score:
            src_in = resolve_src(input_pat, tc["input_name"])
            dst_in = testcase_dir / tc["input_name"]
            if src_in.exists(): shutil.copy2(src_in, dst_in)
            if not is_spj:
                src_out = resolve_src(answer_pat, tc["output_name"])
                dst_out = testcase_dir / tc["output_name"]
                if src_out.exists(): shutil.copy2(src_out, dst_out)

        # If nothing copied, copy everything under tests/
        if not any(testcase_dir.iterdir()):
            tests_dir = base_dir / "tests"
            if tests_dir.exists():
                for f in sorted(tests_dir.iterdir()):
                    if f.is_file():
                        shutil.copy2(f, testcase_dir / f.name)

        # Zip it
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in top_dir.rglob("*"):
                if p.is_file():
                    arc = f"{top_name}/{p.relative_to(top_dir)}"
                    zf.write(p, arcname=arc)

        print(f"OK: wrote {out_zip}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    main()
