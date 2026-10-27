from dataclasses import dataclass, field


@dataclass
class Reply:
    text: str
    keyboard: list[list[dict[str, str]]] | None = None  # Bale inline keyboard rows
    files: list[tuple[str, bytes]] = field(default_factory=list)  # (name, content) to send after
    reply_keyboard: list[list[dict[str, str]]] | None = None  # Persistent bottom reply keyboard
