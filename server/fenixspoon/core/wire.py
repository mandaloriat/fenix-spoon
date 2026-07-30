"""Models whose absent fields mean something (roadmap M2.5, issue #45).

Two payloads in this protocol are *selections*: `capability.describe` returns the sections
you asked for, and `result.get` returns the levels you asked for. For both, an unrequested
part must be **absent** from the JSON rather than present and null — the difference between
a compact answer and a compact-looking one, and the difference between "this capability has
no metrics" and "you did not ask about metrics".

Over HTTP that rule is `response_model_exclude_none=True` on the route. That was fine while
HTTP was the only transport; it stopped being fine the moment a second adapter had to
remember it, because a rule enforced at each binding is a rule each new binding can forget.
So the model carries it: :meth:`Selective.wire` is what every transport renders, and
`test_the_two_transports_agree_on_a_selective_payload` is what keeps the HTTP route's
declaration and this method from drifting.
"""

from typing import Any

from pydantic import BaseModel


class Selective(BaseModel):
    """A payload where a null field would be a different claim from a missing one."""

    def wire(self) -> dict[str, Any]:
        """This model as the protocol carries it: unrequested parts absent, not null.

        `mode="json"` for the same reason every other adapter uses it — a `datetime` has to
        become a string somewhere, and doing it here means no transport has to know.
        """
        return self.model_dump(mode="json", exclude_none=True)
