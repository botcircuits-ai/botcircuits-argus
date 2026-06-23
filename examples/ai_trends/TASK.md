# Workflow name:  `ai_trends`

---

# Instruction Prompt

> Create a **simple, linear** workflow that checks the current trends in AI,
> summarizes the findings, and writes them to a dated results file. This is an
> unattended run — never ask the user anything.
>
> **What it should do:**
>
> 1. **Start.**
>
> 2. **Fetch trends.** Query Google Trends for trending topics and rising search
>    interest related to artificial intelligence (AI). Collect the top trending
>    AI-related queries, their relative interest, and any notable rising terms
>    into a `trends_findings` slot.
>
> 3. **Analyze.** Using `trends_findings`, build a concise report into a
>    `trends_report` slot: list the top trending AI topics, note which are rising
>    vs. stable, and add a one-line takeaway about the overall direction of AI
>    interest.
>
> 4. **Save the results file.** Write the contents of `trends_report` to a text
>    file named **`find_<today>.txt`** in the current working directory, where
>    `<today>` is today's date in `YYYY-MM-DD` format (e.g. `find_2026-06-22.txt`).
>    Then end the flow.
>
> Keep it fully unattended — this run must never stop to ask a question.
