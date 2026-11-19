from vault_shared.grounding import PAGE_BREAK, best_passages, split_passages

CONTRACT = PAGE_BREAK.join(
    [
        "Services Agreement between Blarrow Ltd and Studio Nine.\nScope: website redesign.",
        "Timeline: delivery in twelve weeks from kickoff.",
        "Payment Terms\nThe client will pay a total fee of INR 2,50,000 in two instalments.",
    ]
)


def test_passages_remember_the_page_they_came_from() -> None:
    passages = split_passages(CONTRACT)

    assert [p.page for p in passages] == [1, 2, 3]


def test_text_without_page_breaks_has_no_page_numbers() -> None:
    passages = split_passages("Just one note.\n\nAnother paragraph.")

    assert all(p.page is None for p in passages)


def test_long_pages_are_split_into_bounded_passages() -> None:
    text = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(20))

    passages = split_passages(text, max_chars=500)

    assert len(passages) > 1
    assert all(len(p.text) <= 500 for p in passages)
    assert [p.index for p in passages] == list(range(len(passages)))


def test_the_passage_that_answers_the_question_ranks_first() -> None:
    [best] = best_passages(
        "What was the payment amount in the Blarrow agreement?",
        CONTRACT,
        file_name="Blarrow_Services_Agreement.pdf",
        limit=1,
    )

    assert best.page == 3
    assert "2,50,000" in best.text


def test_no_matching_words_means_no_passages() -> None:
    assert best_passages("quarterly revenue forecast", CONTRACT) == []


def test_common_words_alone_never_count_as_a_match() -> None:
    assert best_passages("what does this say", CONTRACT) == []
