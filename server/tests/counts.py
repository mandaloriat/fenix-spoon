"""Reading a count out of prose, for the suites that check one.

Two documents state how many methods this transport carries and four state how many tools the
MCP adapter exposes, and until #22 nothing checked any of them. The checks live in
`test_rpc.py` and `test_mcp.py` respectively — the tool half has to be there because
`mcp_adapter` cannot be imported without the extra — and the parsing lives here because both
need it and neither owns it.

A module rather than an import from one suite into the other, following `geometries.py`: the
tests directory is flat, so a test module importing another test module both re-collects it
and depends on its import side effects.
"""

import re

#: Number words the prose uses for counts, so a claim can be written either way — a heading
#: says "Fifteen tools, not twenty-eight" and a table cell says "28 methods", and a check that
#: understood one form would leave the other exactly as unguarded as it was.
#:
#: Small on purpose. The day a count leaves this range the failure says so, rather than
#: passing by silently matching nothing.
NUMBER_WORDS = {
    word: value
    for value, word in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen "
        "fourteen fifteen sixteen seventeen eighteen nineteen twenty".split()
    )
}
NUMBER_WORDS |= {
    "twenty-one": 21, "twenty-two": 22, "twenty-three": 23, "twenty-four": 24,
    "twenty-five": 25, "twenty-six": 26, "twenty-seven": 27, "twenty-eight": 28,
    "twenty-nine": 29, "thirty": 30, "thirty-one": 31, "thirty-two": 32,
}


def stated_count(text: str, pattern: str) -> int:
    """The number a document states, written as digits or as a word.

    The pattern must capture the number and nothing else. A pattern that stops matching is a
    failure rather than a skip, for the reason every tripwire here follows: a guard that
    quietly matches nothing passes forever while checking nothing.
    """
    found = re.search(pattern, text, re.I)
    assert found is not None, (
        f"the prose no longer states this count in the form the test reads ({pattern}); "
        "restore the phrasing or update the pattern, but do not delete the claim"
    )
    stated = found.group(1)
    if stated.isdigit():
        return int(stated)
    assert stated.lower() in NUMBER_WORDS, f"unknown number word {stated!r}"
    return NUMBER_WORDS[stated.lower()]
