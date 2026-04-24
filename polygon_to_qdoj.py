#!/usr/bin/env python3
"""
Usage:
  python polygon_to_qdoj.py <input_polygon_zip1> [input_polygon_zip2 ...] <output_qdoj_zip>

- Extracts each Polygon package ZIP
- Reads problem.xml for limits, tests, and file patterns
- Reads statement-sections/english/*.tex for description/input/output + samples
- Builds problem.json (QDOJ-style) with display_id derived from problem URL
- Packages into <output_qdoj_zip> with multiple top-level directories:
    1/problem.json
    1/testcase/{01,01.a,02,02.a,...}
    2/problem.json
    2/testcase/{01,01.a,02,02.a,...}
    ...

Rules:
- display_id is derived from the Polygon problem URL path (with _<index> suffix for uniqueness)
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

# Pre-compiled patterns used in tex_to_html (called once per section, so worth caching).
_LATEX_SUBS = [
    (re.compile(r"\\leq"),   "&le;"),
    (re.compile(r"\\le"),    "&le;"),
    (re.compile(r"\\geq"),   "&ge;"),
    (re.compile(r"\\ge"),    "&ge;"),
    (re.compile(r"\\times"), "&times;"),
    (re.compile(r"\\cdot"),  "&middot;"),
    (re.compile(r"\\neq"),   "&ne;"),
    (re.compile(r"\\pm"),    "&plusmn;"),
    (re.compile(r"\\infty"), "&infin;"),
]
_RE_MATH       = re.compile(r"\$(.+?)\$", re.DOTALL)
_RE_MATH_SLOT  = re.compile(r"@@MATH(\d+)@@")
_RE_VERB       = re.compile(r"\\verb(.)(.*?)\1", re.DOTALL)
_RE_PARA_SPLIT = re.compile(r"\n[ \t]*\n")
_RE_HR_LINE    = re.compile(r"(?m)^[ \t]*-{3,}[ \t]*$")
_NESTED        = r"[^{}]*(?:\{[^{}]*\}[^{}]*)*"

_MIME = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".svg":  "image/svg+xml",
    ".webp": "image/webp",
}
_IMG_WARN_BYTES = 500_000


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
        cut = len(line)
        i = 0
        while i < len(line):
            if line[i] == '%':
                b, j = 0, i - 1
                while j >= 0 and line[j] == '\\':
                    b += 1
                    j -= 1
                if b % 2 == 0:
                    cut = i
                    break
            i += 1
        lines.append(line[:cut])
    return "\n".join(lines).strip()


def latex_inline(code: str) -> str:
    for pattern, repl in _LATEX_SUBS:
        code = pattern.sub(repl, code)
    return code


def tex_to_html(tex: str, img_dir: Optional[Path] = None, img_out_dir: Optional[Path] = None) -> str:
    if not tex:
        return ""
    s = strip_tex_comments(tex)

    # Protect inline math and apply special rules inside it.
    placeholders: list[str] = []
    def math_inline(m):
        content = m.group(1)
        content = content.replace("—", "-").replace("&mdash;", "-").replace("−", "-")
        content = re.sub(r"---", "-", content)
        content = latex_inline(content)
        placeholders.append(content)
        return f"@@MATH{len(placeholders)-1}@@"
    s = _RE_MATH.sub(math_inline, s)

    # \verbX...X
    s = _RE_VERB.sub(
        lambda m: "<code>" + (m.group(2) or "").replace("<", "&lt;").replace(">", "&gt;") + "</code>",
        s,
    )

    # \t{text} (Polygon shorthand for \texttt)
    s = re.sub(r"\\t\{([^{}]*)\}", r"<code>\1</code>", s)

    # Braced styles — allow one level of nested braces so \textbf{\emph{x}} works.
    def brace_tag(tag):
        return lambda m: f"<{tag}>{m.group(1)}</{tag}>"

    s = re.sub(r"\\texttt\{([^{}]*)\}",        brace_tag("code"),   s)
    s = re.sub(r"\\tt\{([^{}]*)\}",        brace_tag("code"),   s)
    s = re.sub(rf"\\textbf\{{({_NESTED})\}}",  brace_tag("strong"), s)
    s = re.sub(rf"\\bf\{{({_NESTED})\}}",      brace_tag("strong"), s)
    s = re.sub(r"\\textit\{([^{}]*)\}",        brace_tag("em"),     s)
    s = re.sub(r"\\it\{([^{}]*)\}",        brace_tag("em"),     s)
    s = re.sub(r"\\emph\{([^{}]*)\}",          brace_tag("em"),     s)

    s = re.sub(r"\\href\{([^{}]*)\}\{([^{}]*)\}", r'<a href="\1">\2</a>', s)
    s = re.sub(r"\\footnote\{([^{}]*)\}", r" <em>(\1)</em>", s)

    s = re.sub(r"``(.*?)''", r"<code>\1</code>", s, flags=re.DOTALL)
    s = re.sub(r"``(.*?)``", r"<code>\1</code>", s, flags=re.DOTALL)

    # Copy images to img_out_dir and reference by relative path, or strip if unavailable.
    def repl_img(m):
        filename = m.group(2).strip()
        if not img_dir:
            return ""
        src = img_dir / filename
        if not src.exists():
            print(f"WARNING: image not found: {src}", file=sys.stderr)
            return ""
        style = "max-width:100%"
        scale_m = re.search(r"scale\s*=\s*([\d.]+)", m.group(1) or "")
        if scale_m:
            style = f"max-width:{int(float(scale_m.group(1)) * 100)}%"
        if img_out_dir:
            shutil.copy2(src, img_out_dir / filename)
        return f'<img src="images/{filename}" style="{style}"/>'

    s = re.sub(r"\\includegraphics(?:\[([^\]]*)\])?\{([^{}]*)\}", repl_img, s)
    s = re.sub(r"\\(?:begin|end)\{center\}", "", s)
    s = re.sub(r"\\small\{([^{}]*)\}", r"\1", s)

    s = s.replace(r"\_", "_")

    s = re.sub(r"\\begin\{itemize\}",   "<ul>",  s)
    s = re.sub(r"\\end\{itemize\}",     "</ul>", s)
    s = re.sub(r"\\begin\{enumerate\}", "<ol>",  s)
    s = re.sub(r"\\end\{enumerate\}",   "</ol>", s)
    s = re.sub(r"\\item\s*",            "<li>",  s)

    s = re.sub(r"\\section\*?\{([^{}]*)\}",    r"<h2>\1</h2>", s)
    s = re.sub(r"\\subsection\*?\{([^{}]*)\}", r"<h3>\1</h3>", s)

    s = s.replace(r"\ldots", "&hellip;").replace(r"\dots", "&hellip;").replace("...", "&hellip;")

    # Mark standalone --- lines as HR separators before paragraph splitting
    # so they work even when adjacent text is on the next line with no blank gap.
    s = _RE_HR_LINE.sub("@@HR@@", s)

    paras = [p for p in _RE_PARA_SPLIT.split(s) if p.strip()]

    out_parts: list[str] = []
    for p in paras:
        # A paragraph may contain @@HR@@ if --- was on its own line mid-paragraph.
        segments = p.split("@@HR@@")
        for k, seg in enumerate(segments):
            if k > 0:
                out_parts.append("<hr/>")
            seg = seg.strip()
            if not seg:
                continue
            seg = re.sub(r"---", "&mdash;", seg)
            seg = re.sub(r"--",  "&ndash;", seg)
            out_parts.append(f"<p>{seg}</p>")

    html = "".join(out_parts)
    html = _RE_MATH_SLOT.sub(lambda m: f"${placeholders[int(m.group(1))]}$", html)
    html = re.sub(r"\\(</?[a-zA-Z]+>)", r"\1", html)
    html = re.sub(r"<p>\s*</p>", "", html)

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
    return sorted(cands, key=lambda p: len(p.parts))[0]


def warn_non_ascii(label: str, text: str):
    seen: set[str] = set()
    for i, ch in enumerate(text):
        if ord(ch) > 127 and ch not in seen:
            seen.add(ch)
            print(f"WARNING: non-ASCII char U+{ord(ch):04X} {repr(ch)} in {label} at position {i}", file=sys.stderr)


def fix_hr_spacing(text: str) -> str:
    # QDOJ renders <hr/> flush against the following block element; always insert
    # a spacer paragraph after every <hr/> so there is visible separation.
    return re.sub(r"<hr/>(<p>)", r"<hr/><p><br/></p>\1", text)


def close_li_tags(text: str) -> str:
    def close_in_list(match):
        open_tag, body, close_tag = match.groups()
        parts = re.split(r"(<li>)", body)
        out = [open_tag]
        current = None
        for part in parts:
            if part == "<li>":
                if current is not None:
                    out.append(current.rstrip() + "</li>")
                current = "<li>"
            else:
                if current is None:
                    out.append(part)
                else:
                    current += part
        if current is not None:
            out.append(current.rstrip() + "</li>")
        out.append(close_tag)
        return "".join(out)

    return re.sub(r"(?s)(<(?:ul|ol)>)(.*?)(</(?:ul|ol)>)", close_in_list, text)


def normalize_html(text: str) -> str:
    return close_li_tags(fix_hr_spacing(text))


def display_id_from_url(root) -> Optional[str]:
    url = root.get("url") or ""
    if not url:
        return None
    try:
        path = (urlparse(url).path or "").strip("/")
        first = path.split("/")[0] if path else None
        return first or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# problem.xml helpers
# ---------------------------------------------------------------------------

def _parse_title(root: ET.Element) -> Optional[str]:
    names = root.find("names")
    if names is None:
        return None
    for el in names.findall("name"):
        if el.get("language") == "english" and el.get("value"):
            return el.get("value")
    for el in names.findall("name"):
        if el.get("value"):
            return el.get("value")
    return None


def _parse_limits(testset: ET.Element):
    time_limit = int(testset.findtext("time-limit", default="2000"))
    mem_bytes  = int(testset.findtext("memory-limit", default=str(256 * 1024 * 1024)))
    input_pat  = testset.findtext("input-path-pattern",  default="tests/%02d")
    answer_pat = testset.findtext("answer-path-pattern", default="tests/%02d.a")
    return time_limit, memory_bytes_to_mb(mem_bytes), input_pat, answer_pat


def _parse_test_cases(testset: ET.Element, input_pat: str, answer_pat: str):
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
        input_name  = build_name_from_pattern(input_pat,  i)
        output_name = build_name_from_pattern(answer_pat, i)
        test_case_score.append({"score": score, "input_name": input_name, "output_name": output_name})
        print(f"Test {i}: {input_name} / {output_name} | {score} pts")
    return test_case_score, score_sum


def _parse_sections(sections_dir: Path, img_out_dir: Optional[Path] = None):
    """Returns (desc_html, input_html, output_html, hint_html, title_override, samples)."""
    desc_html = input_html = output_html = hint_html = title_override = None
    samples: list[dict] = []

    if not sections_dir.exists():
        return desc_html, input_html, output_html, hint_html, title_override, samples

    def load(filename: str) -> Optional[str]:
        f = sections_dir / filename
        return read_text(f) if f.exists() else None

    def convert(label: str, tex: Optional[str]) -> Optional[str]:
        if tex is None:
            return None
        print(f"Found {label}.")
        html = normalize_html(tex_to_html(tex, img_dir=sections_dir, img_out_dir=img_out_dir))
        warn_non_ascii(label, html)
        return html

    desc_html   = convert("legend", load("legend.tex"))
    input_html  = convert("input",  load("input.tex"))
    output_html = convert("output", load("output.tex"))
    hint_html   = convert("hint",   load("notes.tex"))

    name_tex = load("name.tex")
    if name_tex:
        print("Found name.")
        nm = re.sub(r"<.*?>", "", tex_to_html(name_tex)).strip()
        if nm:
            title_override = nm

    for ex in sorted(sections_dir.glob("example.*")):
        if ex.suffix == ".a":
            continue
        outp = sections_dir / (ex.name + ".a")
        inp_text = "\n".join(l.rstrip() for l in read_text(ex).splitlines())
        out_text = "\n".join(l.rstrip() for l in (read_text(outp) if outp.exists() else "").splitlines())
        warn_non_ascii(f"sample {ex.name} input",  inp_text)
        warn_non_ascii(f"sample {ex.name} output", out_text)
        samples.append({"input": inp_text, "output": out_text})
        print(f"Found example: {ex}")

    return desc_html, input_html, output_html, hint_html, title_override, samples


def _find_checker(root: ET.Element, base_dir: Path, in_zip: Path) -> Optional[dict]:
    checker_el = root.find("assets/checker") or root.find("checker")
    if checker_el is None:
        return None
    if checker_el.get("name", "").startswith("std::"):
        return None
    src_el = checker_el.find("source")
    if src_el is None:
        return None

    checker_path = src_el.get("path", "")
    checker_type = src_el.get("type", "")

    checker_src = base_dir / checker_path
    if not checker_src.exists():
        checker_src = base_dir / "files" / os.path.basename(checker_path)

    if checker_src.exists():
        code = read_text(checker_src)
    else:
        try:
            with zipfile.ZipFile(in_zip) as zf:
                code = zf.read(checker_path).decode("utf-8", errors="ignore")
        except Exception:
            code = None

    if not code:
        print(f"WARNING: checker source not found at {checker_path}", file=sys.stderr)
        return None

    if "cpp" in checker_type.lower() or checker_path.endswith((".cpp", ".cc")):
        language = "C++11"
    elif checker_path.endswith(".c"):
        language = "C"
    else:
        language = "C++11"

    print(f"Found checker: {checker_path} (language: {language})")
    return {"code": code, "language": language}


def _copy_testcases(test_case_score, input_pat, answer_pat, base_dir, testcase_dir, is_spj):
    def resolve_src(pattern: str, produced_name: str) -> Path:
        stem = os.path.splitext(produced_name)[0]
        return base_dir / re.sub(r'%0?\d*d', stem, pattern, count=1)

    any_copied = False
    for tc in test_case_score:
        src_in = resolve_src(input_pat, tc["input_name"])
        if src_in.exists():
            shutil.copy2(src_in, testcase_dir / tc["input_name"])
            any_copied = True
        else:
            print(f"WARNING: input file not found: {src_in}", file=sys.stderr)
        if not is_spj:
            src_out = resolve_src(answer_pat, tc["output_name"])
            if src_out.exists():
                shutil.copy2(src_out, testcase_dir / tc["output_name"])
            else:
                print(f"WARNING: answer file not found: {src_out}", file=sys.stderr)

    if not any_copied:
        tests_dir = base_dir / "tests"
        if tests_dir.exists():
            for f in sorted(tests_dir.iterdir()):
                if f.is_file():
                    shutil.copy2(f, testcase_dir / f.name)


def main():
    if len(sys.argv) < 3:
        print(__doc__.strip())
        sys.exit(2)

    inputs = [Path(p).resolve() for p in sys.argv[1:-1]]
    out_zip = Path(sys.argv[-1]).resolve()

    for p in inputs:
        if not p.exists():
            die(f"Input ZIP not found: {p}")

    tmp = Path(tempfile.mkdtemp(prefix="poly_"))
    print("Temporary dir:", tmp)
    try:
        staging_root = Path(tempfile.mkdtemp(prefix="qdoj_", dir=tmp))

        for index, in_zip in enumerate(inputs, start=1):
            print(f"\n=== Processing [{index}/{len(inputs)}]: {in_zip.name} ===")

            extract_dir = tmp / f"extract_{index}"
            extract_dir.mkdir()

            with zipfile.ZipFile(in_zip, "r") as zf:
                zf.extractall(extract_dir)

            xml_path = find_problem_xml(extract_dir)
            base_dir = xml_path.parent
            root = ET.parse(xml_path).getroot()

            title = _parse_title(root)
            print("Title:", title)

            judging = root.find("judging")
            testset = None
            if judging is not None:
                for ts in judging.findall("testset"):
                    if ts.get("name") == "tests":
                        testset = ts
                        break
                if testset is None:
                    testset = judging.find("testset")
            if testset is None:
                die("No <testset> in problem.xml")

            time_limit, memory_limit, input_pat, answer_pat = _parse_limits(testset)
            print(f"TL: {time_limit}ms  ML: {memory_limit}MB")

            test_case_score, score_sum = _parse_test_cases(testset, input_pat, answer_pat)
            if score_sum != 100:
                print(f"WARNING: test scores sum to {score_sum} (expected 100).", file=sys.stderr)

            spj_info = _find_checker(root, base_dir, in_zip)
            is_spj = spj_info is not None
            if is_spj:
                for tc in test_case_score:
                    tc["output_name"] = "-"

            base_display_id = display_id_from_url(root) or "default_display_id"
            display_id = f"{base_display_id}_{index}"

            top_name = str(index)
            top_dir = staging_root / top_name
            testcase_dir = top_dir / "testcase"
            testcase_dir.mkdir(parents=True)
            images_dir = top_dir / "images"
            images_dir.mkdir(parents=True)

            sections_dir = base_dir / "statement-sections" / "english"
            desc_html, input_html, output_html, hint_html, title_override, samples = \
                _parse_sections(sections_dir, img_out_dir=images_dir)
            if title_override:
                title = title_override

            out = {
                "display_id":         display_id,
                "title":              title or "Untitled Problem",
                "description":        {"format": "html", "value": desc_html    or ""},
                "input_description":  {"format": "html", "value": input_html   or ""},
                "output_description": {"format": "html", "value": output_html  or ""},
                "hint":               {"format": "html", "value": hint_html    or ""},
                "test_case_score":    test_case_score,
                "time_limit":         time_limit,
                "memory_limit":       memory_limit,
                "samples":            samples,
                "tags":               [today_tag()],
                "template":           {},
                "source":             "",
                "answers":            [],
                "spj":                spj_info,
            }

            (top_dir / "problem.json").write_text(
                json.dumps(out, ensure_ascii=False, indent=4), encoding="utf-8"
            )

            _copy_testcases(test_case_score, input_pat, answer_pat, base_dir, testcase_dir, is_spj)
            print(f"  -> Added as problem '{display_id}' under '{top_name}/'")

        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in staging_root.rglob("*"):
                if p.is_file():
                    zf.write(p, arcname=p.relative_to(staging_root))

        print(f"\nOK: wrote {out_zip} with {len(inputs)} problem(s)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
