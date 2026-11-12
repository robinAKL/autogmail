from dataclasses import dataclass, field


@dataclass
class Mail:
    id: str
    thread_id: str
    from_name: str
    from_email: str
    reply_email: str
    subject: str
    date: str
    body: str
    headers: dict[str, str] = field(default_factory=dict)
    label_ids: list[str] = field(default_factory=list)
    snippet: str = ""
    html: str = ""
    provider: str = "gmail"
    account: str = ""
    received_ts: float = 0.0

    @property
    def key(self) -> str:
        """Record key. Gmail keeps its bare id so older records still match."""
        if self.provider == "gmail":
            return self.id
        return f"{self.provider}:{self.id}"


@dataclass
class Draft:
    """What the model decided for one letter."""

    action: str = "reply"  # reply | ask | skip
    reply: str = ""
    reason: str = ""
    summary: str = ""
    sources: list[str] = field(default_factory=list)
