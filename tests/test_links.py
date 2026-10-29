import pytest

from catchup import CatchupError
from catchup.links import Link, infer_type, parse_link, resolve


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://rohlik.slack.com/archives/C1/p123", "Slack"),
        ("https://app.slack.com/client/T1/C1", "Slack"),
        ("https://gitlab.com/group/proj/-/merge_requests/12", "MR"),
        ("https://github.com/owner/repo/pull/3", "MR"),
        ("https://mail.google.com/mail/u/0/#inbox/abc", "Email"),
        ("https://outlook.office.com/mail/id/1", "Email"),
        ("https://linear.app/team/issue/CUEX-1", "Ticket"),
        ("https://acme.atlassian.net/browse/OPS-7", "Ticket"),
        ("https://github.com/owner/repo/issues/9", "Ticket"),
        ("https://claude.ai/artifact/abc", "Artifact"),
        ("https://claude.ai/code/artifact/uuid", "Artifact"),
        ("https://docs.google.com/document/d/1", "Doc"),
        ("https://www.notion.so/page-1", "Doc"),
        ("https://acme.notion.site/page", "Doc"),
        ("https://example.com/", "Web"),
        # Rule order: a Slack link wins over the path rules below it.
        ("https://slack.com/pull/1", "Slack"),
        ("https://notslack.com/x", "Web"),
        ("https://example.com/?next=slack.com", "Web"),
    ],
)
def test_infer_type(url: str, expected: str) -> None:
    assert infer_type(url) == expected


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("Spec=https://x.test/a", Link("Spec", "https://x.test/a")),
        ("doc:Spec=https://x.test/a", Link("Spec", "https://x.test/a", "Doc")),
        ("MR: Fix it=https://x.test/a", Link("Fix it", "https://x.test/a", "MR")),
        ("Re: hello=https://x.test/a", Link("Re: hello", "https://x.test/a")),
        ("Thread=https://x.test/a?b=c", Link("Thread", "https://x.test/a?b=c")),
    ],
)
def test_parse_link(spec: str, expected: Link) -> None:
    assert parse_link(spec) == expected


@pytest.mark.parametrize("spec", ["no-equals", "=https://x.test", "label=", "https://x.test/?a=b"])
def test_parse_link_rejects_malformed(spec: str) -> None:
    with pytest.raises(CatchupError):
        parse_link(spec)


def test_resolve_infers_missing_type_and_canonicalizes_given_one() -> None:
    assert resolve(Link("a", "https://github.com/o/r/pull/1")).type == "MR"
    assert resolve(Link("a", "https://example.com", "slack")).type == "Slack"
    with pytest.raises(CatchupError, match="unknown link type"):
        resolve(Link("a", "https://example.com", "wiki"))
