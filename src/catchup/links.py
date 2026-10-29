"""Links of an item: parsing the CLI form and inferring the link type from the URL."""

from dataclasses import dataclass
from urllib.parse import urlsplit

from catchup import CatchupError

LINK_TYPES = ("MR", "Slack", "Email", "Ticket", "Doc", "Artifact", "Web")


@dataclass(frozen=True)
class Link:
    """A labeled pointer to where the conversation or the work lives."""

    label: str
    url: str
    type: str | None = None


def _on(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def infer_type(url: str) -> str:
    """Return the link type for a URL, using the first rule from the design that matches."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path
    if _on(host, "slack.com"):
        return "Slack"
    if "merge_requests" in path or "/pull/" in path:
        return "MR"
    if host == "mail.google.com" or "outlook." in host:
        return "Email"
    if (
        _on(host, "linear.app")
        or (_on(host, "atlassian.net") and path.startswith("/browse"))
        or "/issues/" in path
    ):
        return "Ticket"
    if host == "claude.ai" and path.startswith(("/artifact", "/code/artifact")):
        return "Artifact"
    if host == "docs.google.com" or _on(host, "notion.so") or _on(host, "notion.site"):
        return "Doc"
    return "Web"


def resolve(link: Link) -> Link:
    """Return the link with its type set: the given one in canonical spelling, else the inferred one."""
    if link.type is None:
        return Link(link.label, link.url, infer_type(link.url))
    canonical = _canonical_type(link.type)
    if canonical is None:
        raise CatchupError(f"unknown link type {link.type!r}; use one of {', '.join(LINK_TYPES)}")
    return Link(link.label, link.url, canonical)


def parse_link(spec: str) -> Link:
    """Parse `[type:]label=url`; a prefix before `:` is a type only when it names a known type."""
    label, sep, url = spec.partition("=")
    label, url = label.strip(), url.strip()
    if not sep or not label or not url:
        raise CatchupError(f"link {spec!r} is not in the form [type:]label=url")
    if not urlsplit(url).scheme:
        raise CatchupError(
            f"link {spec!r} has no URL scheme in {url!r}; write it as [type:]label=https://..."
        )
    link_type = None
    prefix, colon, rest = label.partition(":")
    if colon and rest.strip() and _canonical_type(prefix.strip()):
        link_type, label = _canonical_type(prefix.strip()), rest.strip()
    return Link(label, url, link_type)


def _canonical_type(name: str) -> str | None:
    return next((t for t in LINK_TYPES if t.lower() == name.lower()), None)
