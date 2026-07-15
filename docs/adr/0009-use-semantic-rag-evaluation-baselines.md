# Use semantic RAG evaluation baselines

The first Evaluation Baseline contains 10–20 human-accepted RAG Evaluation Cases
derived from real vault questions. Each case records expected retrieved notes,
provenance, and answer claims, while deliberately avoiding exact generated prose;
retrieval metrics and independent Quality Assessor judgments therefore catch
both search and grounded-answer regressions without binding the system to one
model's wording. Models may propose new cases, but proposed cases do not enter the
baseline until accepted as durable vault-owned quality data.
