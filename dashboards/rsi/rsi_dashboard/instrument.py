"""Read the survey and check one respondent's submission."""
import math
import re
from pathlib import Path

SOURCE = Path(__file__).with_name("rsi_forecasting_survey.md")


def parse(text):
    parts = re.split(r"^## (\d+)\. (.+)$", text, flags=re.M)
    sections = []
    for number, title, body in zip(parts[1::3], parts[2::3], parts[3::3], strict=True):
        chunks = re.split(r"^### (Q\d+\.\d+) (.+)$", body, flags=re.M)
        questions = []
        for qid, label, content in zip(chunks[1::3], chunks[2::3], chunks[3::3], strict=True):
            prompt, answer = content.strip().split("\n\nAnswer: ")
            answer, resolution = answer.split("\n\nResolution: ")
            questions.append(dict(id=qid, label=label, prompt=prompt.removeprefix("**").replace("**", ""),
                                  answer=answer, resolution=resolution.strip()))
        sections.append(dict(id=number, title=title, intro=chunks[0].strip(), questions=questions))
    expected = [4, 3, 4, 3, 4]
    if len(sections) != len(expected):
        raise ValueError("Expected five RSI sections")
    for i, (section, count) in enumerate(zip(sections, expected), 1):
        if section["id"] != str(i) or [q["id"] for q in section["questions"]] != [
            f"Q{i}.{j}" for j in range(1, count + 1)
        ]:
            raise ValueError(f"Unexpected questions in section {i}")
    return sections


def selected_sections(sections, excluded):
    result = []
    for section in sections:
        questions = [q for q in section["questions"] if q["id"] not in excluded]
        if questions:
            result.append(dict(section, questions=questions))
    return result


def survey_prompt(source, excluded):
    if not excluded:
        return source.rstrip()
    sections = selected_sections(parse(source), excluded)
    intro = source.split("\n## 1.", 1)[0].replace("18 forecasting questions in 5 sections",
        f"{sum(len(s['questions']) for s in sections)} forecasting questions in {len(sections)} sections")
    blocks = [intro.rstrip()]
    for s in sections:
        blocks.extend([f"## {s['id']}. {s['title']}", s["intro"]])
        for q in s["questions"]:
            blocks.extend([f"### {q['id']} {q['label']}", q["prompt"], "Answer: " + q["answer"],
                           "Resolution: " + q["resolution"]])
    # Other questions can refer back to a resolved question's definitions.
    for section in parse(source):
        for q in section["questions"]:
            if q["id"] in excluded and any(q["id"] in text for text in blocks):
                blocks.append(f"Reference criteria for {q['id']}: {q['resolution']}")
    return "\n\n".join(blocks)


def fields(question):
    if question["answer"] == "a probability from 0 to 100.":
        return ("probability",)
    if question["answer"].startswith("P10, P50, and P90,"):
        return ("p10", "p50", "p90")
    raise ValueError(f"Unknown answer format for {question['id']}")


def validate(sections, response):
    questions = [q for s in sections for q in s["questions"]]
    answers = response["answers"]
    reasons = response["rationales"]
    if set(answers) != {q["id"] for q in questions} or set(reasons) != {s["id"] for s in sections}:
        raise ValueError("Expected every question and section exactly once")
    if any(not isinstance(reason, str) or not reason.strip() for reason in reasons.values()):
        raise ValueError("Each section requires a rationale")
    for q in questions:
        qid = q["id"]
        values = answers[qid]
        names = fields(q)
        if set(values) != set(names):
            raise ValueError(f"{qid} requires {names}")
        numbers = [values[key] for key in names]
        if any(type(n) not in (int, float) or not math.isfinite(n) for n in numbers):
            raise ValueError(f"{qid} requires finite numbers")
        low, high = -math.inf, math.inf
        if names == ("probability",) or "in percent" in q["answer"]:
            low, high = 0, 100
        elif qid == "Q1.3":
            low, high = 1, 6
        elif qid in {"Q2.3", "Q3.3", "Q4.1", "Q4.2", "Q4.3"}:
            low = 0
        if numbers != sorted(numbers) or numbers[0] < low or numbers[-1] > high:
            raise ValueError(f"{qid} values out of order or range")


def synthetic(sections):
    answers = {}
    for section in sections:
        for q in section["questions"]:
            names = fields(q)
            answers[q["id"]] = dict(zip(names, [50] if len(names) == 1 else [1, 2, 3], strict=True))
    return {"answers": answers, "rationales": {s["id"]: "Synthetic test rationale." for s in sections}}
