import json

import pytest

from evalgate import ragmetrics as rm
from evalgate.adapters.docsrag import is_infra, location_problems
from evalgate.gate import compare
from evalgate.report import render
from evalgate.runner import load_cases, run_cases


def src(file, page=None, section=None):
    return {"file_name": file, "page": page, "section": section}


PDF = "handbook.pdf"
MD = "policy.md"


def test_cited_numbers_are_distinct_and_ordered():
    assert rm.cited_numbers("A [2]. B [1][2] and [10].") == [2, 1, 10]
    assert rm.cited_numbers("no brackets (1) here") == []
    assert rm.cited_numbers("") == []


def test_split_citations_keeps_one_to_source_count():
    assert rm.split_citations("x [1] [3] [0] [4]", 3) == ([1, 3], [0, 4])
    assert rm.split_citations("x [1]", 0) == ([], [1])


def test_matches_prefers_page_then_section():
    page_case = {"file": PDF, "page": 3}
    assert rm.matches(page_case, src(PDF, page=3))
    assert not rm.matches(page_case, src(PDF, page=4))
    assert not rm.matches(page_case, src("other.pdf", page=3))
    section_case = {"file": MD, "section": "Akomodasi"}
    assert rm.matches(section_case, src(MD, section="Akomodasi"))
    assert not rm.matches(section_case, src(MD, section="Uang harian"))
    assert rm.matches({"file": MD}, src(MD, section="anything"))


def test_retrieval_rank_is_one_based():
    sources = [src(PDF, page=1), src(PDF, page=3), src(PDF, page=3)]
    assert rm.retrieval_rank({"file": PDF, "page": 3}, sources) == 2
    assert rm.retrieval_rank({"file": PDF, "page": 9}, sources) is None
    assert rm.retrieval_rank({"file": PDF, "page": 3}, []) is None


def test_citation_ok_needs_a_cite_in_range_and_the_right_source():
    case = {"file": PDF, "page": 3}
    sources = [src(PDF, page=1), src(PDF, page=3)]
    assert rm.citation_ok(case, "Fourteen days [2].", sources)
    assert rm.citation_ok(case, "Fourteen days [1][2].", sources)
    assert not rm.citation_ok(case, "Fourteen days.", sources)  # nothing cited
    assert not rm.citation_ok(case, "Fourteen days [1].", sources)  # wrong source
    assert not rm.citation_ok(case, "Fourteen days [2][7].", sources)  # one out of range
    assert not rm.citation_ok(case, "Fourteen days [2].", [])  # nothing retrieved


@pytest.mark.parametrize(
    "text",
    [
        "The documents don't cover that.",
        "The documents do not cover this topic.",
        "The document doesn’t mention stock options.",
        "I couldn't find anything about that in your documents, so I'd rather not guess.",
        "That is not covered in the sources.",
        "Saya tidak menemukan informasi itu di dokumen Anda, jadi saya tidak mau menebak.",
        "Dokumen tidak membahas harga produk.",
        "Informasi itu tidak disebutkan dalam dokumen.",
        "Tidak ada informasi tentang visa.",
    ],
)
def test_says_not_covered(text):
    assert rm.says_not_covered(text)


@pytest.mark.parametrize(
    "text",
    [
        "Employees get 14 days of annual leave [1].",
        "Karyawan mendapat 14 hari cuti tahunan.",
        "You do not have to ask for approval for local trips.",
        "",
    ],
)
def test_ordinary_answers_do_not_look_like_abstaining(text):
    assert not rm.says_not_covered(text)


def test_abstain_ok_fails_when_it_cites_a_source():
    assert rm.abstain_ok("The documents don't cover that.", 5)
    assert not rm.abstain_ok("The documents don't cover that [1].", 5)
    # a made-up number is out of range, so it is not a citation of a real source
    assert rm.abstain_ok("The documents don't cover that [9].", 5)
    assert not rm.abstain_ok("Fourteen days.", 5)


def test_grade_case_answerable_and_abstain():
    case = {"file": PDF, "page": 3}
    sources = [src(PDF, page=3)]
    ok = rm.grade_case(case, "Fourteen [1].", sources)
    assert (ok["correct"], ok["retrieval_hit"], ok["citation_ok"], ok["rank"]) == (True, True, True, 1)
    miss = rm.grade_case(case, "Fourteen [1].", [src(PDF, page=2)])
    assert (miss["correct"], miss["retrieval_hit"]) == (False, False)
    nocite = rm.grade_case(case, "Fourteen.", sources)
    assert (nocite["retrieval_hit"], nocite["citation_ok"], nocite["correct"]) == (True, False, False)
    ab = rm.grade_case({"file": None}, "The documents don't cover it.", sources)
    assert (ab["expect"], ab["abstain_ok"], ab["correct"]) == ("abstain", True, True)
    bad = rm.grade_case({"file": None}, "Fourteen [1].", sources)
    assert bad["correct"] is False and bad["cited"] == [1]


def test_summarize():
    cases = [
        {"expect": "answer", "retrieval_hit": True, "rank": 1, "citation_ok": True, "out_of_range": []},
        {"expect": "answer", "retrieval_hit": True, "rank": 2, "citation_ok": False, "out_of_range": [9]},
        {"expect": "answer", "retrieval_hit": False, "rank": None, "citation_ok": False, "out_of_range": []},
        {"expect": "abstain", "abstain_ok": True, "out_of_range": []},
        {"expect": "abstain", "abstain_ok": False, "out_of_range": [4, 5]},
    ]
    s = rm.summarize(cases)
    assert (s["answerable"], s["abstain"]) == (3, 2)
    assert s["hit_rate"] == pytest.approx(2 / 3)
    assert s["mrr"] == pytest.approx((1 + 0.5) / 3)
    assert s["citation_rate"] == pytest.approx(1 / 3)
    assert s["out_of_range"] == 3
    assert s["abstain_rate"] == 0.5
    assert rm.summarize([])["hit_rate"] is None


def test_location_problems():
    docs = [
        {"file_name": PDF, "page": 3, "section": None},
        {"file_name": MD, "page": None, "section": "Akomodasi"},
    ]
    good = [
        {"id": "a", "file": PDF, "page": 3},
        {"id": "b", "file": MD, "section": "Akomodasi"},
        {"id": "c", "file": None},
    ]
    assert location_problems(good, docs) == []
    bad = [
        {"id": "a", "file": PDF, "page": 9},
        {"id": "b", "file": MD, "section": "Nope"},
        {"id": "c", "file": "x.pdf", "page": 1},
        {"id": "d", "file": PDF},
        {"id": "e", "file": None, "page": 2},
    ]
    assert len(location_problems(bad, docs)) == 5


def test_is_infra():
    import httpx

    assert is_infra(httpx.ConnectError("down"))
    assert is_infra(ConnectionError("down"))
    assert not is_infra(ValueError("bad answer"))
    wrapped = RuntimeError("outer")
    wrapped.__cause__ = httpx.ReadTimeout("slow")
    assert is_infra(wrapped)


class FakeAdapter:
    def run_case(self, case):
        record = rm.grade_case(case, "Fourteen [1].", [src(PDF, page=3)])
        record["status"] = "ok"
        record["answer"] = "Fourteen [1]."
        return record


def test_load_cases_and_graded_run(tmp_path):
    path = tmp_path / "c.jsonl"
    rows = [
        {"id": "r01", "question": "q", "file": PDF, "page": 3, "lang": "en"},
        {"id": "r02", "question": "q2", "file": None, "lang": "en"},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    cases = load_cases(path)
    results = run_cases(cases, FakeAdapter(), log=lambda *_: None)
    assert [r["correct"] for r in results] == [True, False]
    assert results[0]["id"] == "r01" and results[0]["wall_s"] >= 0


def test_load_cases_needs_ids_for_documents(tmp_path):
    path = tmp_path / "c.jsonl"
    path.write_text(json.dumps({"question": "q", "file": None}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_cases(path)


def rag_side(sha, flags):
    cases = []
    for i, ok in enumerate(flags):
        base = {"id": f"r{i:02d}", "question": f"q{i}", "status": "ok", "correct": ok,
                "answer": "an answer", "out_of_range": [], "cited": [1] if ok else []}
        if i % 5 == 4:
            cases.append({**base, "expect": "abstain", "abstain_ok": ok})
        else:
            cases.append({**base, "expect": "answer", "retrieval_hit": ok or i % 2 == 0,
                          "rank": 1 if ok else None, "citation_ok": ok})
    return {"sha": sha, "model": "m", "model_digest": "d" * 64,
            "adapter": {"name": "docsrag"}, "cases": cases}


def test_rag_report_has_secondary_numbers_and_the_notes():
    base = rag_side("a" * 40, [True] * 30 + [False] * 10)
    head = rag_side("b" * 40, [False] * 8 + [True] * 22 + [False] * 10)
    result = compare(base, head)
    assert result["verdict"] == "FAIL"
    text = render(result, base, head)
    assert "Eval gate: FAIL" in text
    for needle in ("retrieval hit rate", "MRR", "citation rate", "out-of-range citations",
                   "abstain rate", "No model grades the answers", "only large drops",
                   "editing the dataset", "not the model the live app uses"):
        assert needle in text
    assert "SQL" not in text
    assert "now wrong (the expected passage was not retrieved)" in text or "now wrong (" in text


def test_rag_report_a_a_passes():
    base = rag_side("a" * 40, [True] * 30 + [False] * 10)
    head = rag_side("b" * 40, [True] * 30 + [False] * 10)
    result = compare(base, head)
    assert (result["b"], result["c"], result["verdict"]) == (0, 0, "PASS")
