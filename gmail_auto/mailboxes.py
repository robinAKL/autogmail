"""The mailboxes that are connected right now: Gmail, Outlook, or both."""

from gmail_auto.errors import ConfigError, NotConnected

PROVIDERS = {"gmail": "Gmail", "outlook": "Outlook"}


def client_for(provider: str, allow_browser: bool = False):
    if provider == "outlook":
        from gmail_auto.outlook_client import OutlookClient

        return OutlookClient()
    from gmail_auto.gmail_client import GmailClient

    return GmailClient(allow_browser=allow_browser)


def connected_clients() -> tuple[list, list[str]]:
    """Every mailbox that can be opened, plus a message for each one that is connected but failing."""
    clients = []
    problems = []
    for provider, label in PROVIDERS.items():
        try:
            clients.append(client_for(provider))
        except NotConnected:
            continue
        except ConfigError as exc:
            problems.append(f"{label}: {exc}")
        except Exception:
            problems.append(f"{label}: could not be opened.")
    return clients, problems
