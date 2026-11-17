"""Grading for a document Q&A app. Pure functions, no app imports, no model calls.

Nothing here asks a model to judge an answer. A case is checked against the expected file and
page or section from the dataset, the numbers the answer cites, and a fixed list of phrases
for "the documents don't cover this".
"""

import re

CITED = re.compile(r"\[(\d+)\]")  # the same pattern the app's /api/ask uses

# What the prompt asks for ("say that the documents don't cover it") plus the app's own fixed
# no-answer replies, in English and Indonesian. Kept deliberately short and plain.
ABSTAIN_PATTERNS = [
    # English
    r"\b(?:do not|don't|does not|doesn't|did not|didn't) (?:cover|contain|mention|include|address|specify|provide)\b",
    r"\b(?:could not|couldn't|cannot|can't|can not|unable to|did not|didn't) (?:find|see)\b",
    r"\bnot (?:covered|mentioned|addressed|specified|included|found|stated)\b",
    r"\bno (?:information|mention|details?)\b",
    # Indonesian
    r"\btidak (?:menemukan|membahas|mencakup|menyebut|menyebutkan|menjelaskan|memuat|mengatur|ada informasi|terdapat)\b",
    r"\b(?:tidak|belum) (?:ada|tersedia) (?:informasi|keterangan|penjelasan)\b",
    r"\b(?:tidak|belum) (?:dibahas|disebutkan|dijelaskan|diatur|tercantum|dicakup)\b",
    r"\btidak (?:ditemukan|tercakup)\b",
]
_ABSTAIN = [re.compile(p, re.IGNORECASE) for p in ABSTAIN_PATTERNS]


def cited_numbers(answer: str) -> list[int]:
    """Distinct cited numbers in the order they first appear."""
    seen: list[int] = []
    for raw in CITED.findall(answer or ""):
        n = int(raw)
        if n not in seen:
            seen.append(n)
    return seen


def split_citations(answer: str, source_count: int) -> tuple[list[int], list[int]]:
    """(in range, out of range), where in range means 1..source_count like the app keeps."""
    numbers = cited_numbers(answer)
    return (
        [n for n in numbers if 1 <= n <= source_count],
        [n for n in numbers if not 1 <= n <= source_count],
    )


def matches(expected: dict, source: dict) -> bool:
    """Same rule as evals/run_eval.py in the app: the file, then the page if the case has one,
    otherwise the section if it has one."""
    if source.get("file_name") != expected.get("file"):
        return False
    if expected.get("page") is not None:
        return source.get("page") == expected["page"]
    return expected.get("section") is None or source.get("section") == expected["section"]


def retrieval_rank(expected: dict, sources: list[dict]) -> int | None:
    """1-based position of the first retrieved source that matches, None when it is missing."""
    for rank, source in enumerate(sources, start=1):
        if matches(expected, source):
            return rank
    return None


def citation_ok(expected: dict, answer: str, sources: list[dict]) -> bool:
    """At least one cited number, none out of range, and one cited source is the expected one."""
    in_range, out_of_range = split_citations(answer, len(sources))
    if not in_range or out_of_range:
        return False
    return any(matches(expected, sources[n - 1]) for n in in_range)


def says_not_covered(answer: str) -> bool:
    text = (answer or "").replace("’", "'").replace("‘", "'")
    return any(p.search(text) for p in _ABSTAIN)


def abstain_ok(answer: str, source_count: int) -> bool:
    """No in-range citation, and the answer says the documents don't cover it."""
    in_range, _ = split_citations(answer, source_count)
    return not in_range and says_not_covered(answer)


def grade_case(case: dict, answer: str, sources: list[dict]) -> dict:
    """The per-case record fields. `case` is a dataset line, `sources` are dicts with
    file_name, page and section, in the order the app numbered them."""
    in_range, out_of_range = split_citations(answer, len(sources))
    record = {
        "expect": "answer" if case.get("file") else "abstain",
        "n_sources": len(sources),
        "cited": in_range,
        "out_of_range": out_of_range,
    }
    if case.get("file"):
        rank = retrieval_rank(case, sources)
        record["rank"] = rank
        record["retrieval_hit"] = rank is not None
        record["citation_ok"] = citation_ok(case, answer, sources)
        record["correct"] = record["retrieval_hit"] and record["citation_ok"]
    else:
        record["abstain_ok"] = abstain_ok(answer, len(sources))
        record["correct"] = record["abstain_ok"]
    return record


def summarize(cases: list[dict]) -> dict:
    """The secondary numbers for one side, from its per-case records."""
    answerable = [c for c in cases if c.get("expect") == "answer"]
    abstain = [c for c in cases if c.get("expect") == "abstain"]

    def rate(k: int, n: int) -> float | None:
        return k / n if n else None

    return {
        "answerable": len(answerable),
        "abstain": len(abstain),
        "hit_rate": rate(sum(bool(c.get("retrieval_hit")) for c in answerable), len(answerable)),
        "mrr": (
            sum(1 / c["rank"] for c in answerable if c.get("rank")) / len(answerable)
            if answerable
            else None
        ),
        "citation_rate": rate(sum(bool(c.get("citation_ok")) for c in answerable), len(answerable)),
        "out_of_range": sum(len(c.get("out_of_range") or []) for c in cases),
        "abstain_rate": rate(sum(bool(c.get("abstain_ok")) for c in abstain), len(abstain)),
    }


def why(case: dict) -> str:
    """One line on why a case is wrong."""
    if case.get("status") == "error":
        return "crashed"
    if case.get("expect") == "abstain":
        if case.get("cited"):
            return "cited a source for a question the documents don't answer"
        return "answered instead of saying the documents don't cover it"
    if not case.get("retrieval_hit"):
        return "the expected passage was not retrieved"
    if case.get("out_of_range"):
        return "cited a source number that does not exist"
    if not case.get("cited"):
        return "no citation"
    return "cited the wrong source"
