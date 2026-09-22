import json
import os
import re
import urllib.request
from html.parser import HTMLParser

import firebase_admin
from firebase_admin import credentials, messaging


EVENTS_URL = "https://www.kamea-club.de/termine.html"
BASE_URL = "https://www.kamea-club.de"
STATE_FILE = "known_events.json"
TOPIC = "events"


class EventLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.events = {}

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return

        attributes = dict(attrs)
        href = attributes.get("href")

        if not href:
            return

        match = re.search(
            r"/events/.*?/(\d+)-([^/?#]+)\.html",
            href,
            re.IGNORECASE,
        )

        if not match:
            match = re.search(
                r"/events/(\d+)-([^/?#]+)\.html",
                href,
                re.IGNORECASE,
            )

        if not match:
            return

        event_id = match.group(1)

        if href.startswith("http"):
            url = href
        elif href.startswith("/"):
            url = BASE_URL + href
        else:
            url = BASE_URL + "/" + href

        self.events[event_id] = {
            "id": event_id,
            "url": url,
        }


class EventTitleParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_heading = False
        self.heading_tag = None
        self.current_text = []
        self.headings = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()

        if tag in ("h1", "h2"):
            self.in_heading = True
            self.heading_tag = tag
            self.current_text = []

    def handle_data(self, data):
        if self.in_heading:
            self.current_text.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()

        if (
            self.in_heading
            and self.heading_tag == tag
        ):
            text = " ".join(
                "".join(self.current_text).split()
            ).strip()

            if text:
                self.headings.append(text)

            self.in_heading = False
            self.heading_tag = None
            self.current_text = []


def download_page(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "KAMEA-Event-Push/1.0",
            "Accept": "text/html,application/xhtml+xml",
        },
    )

    with urllib.request.urlopen(
        request,
        timeout=30,
    ) as response:
        return response.read().decode(
            "utf-8",
            errors="ignore",
        )


def load_current_events():
    html = download_page(EVENTS_URL)

    parser = EventLinkParser()
    parser.feed(html)

    return parser.events


def load_event_title(event):
    try:
        html = download_page(event["url"])

        parser = EventTitleParser()
        parser.feed(html)

        for heading in parser.headings:
            title = clean_event_title(heading)

            if title:
                return title

    except Exception as error:
        print(
            f"Titel für Event {event['id']} "
            f"konnte nicht geladen werden: {error}"
        )

    return "Neue KAMEA Veranstaltung"


def clean_event_title(title):
    title = " ".join(title.split()).strip()

    title = re.sub(
        r"^\[(?:Kamea|Helenesee|Bellevue|Bad Saarow)\]\s*",
        "",
        title,
        flags=re.IGNORECASE,
    )

    blocked_terms = (
        "casino",
        "online casino",
        "gambling",
        "slot",
        "betting",
    )

    lower_title = title.lower()

    if any(
        term in lower_title
        for term in blocked_terms
    ):
        return None

    if not title:
        return None

    return title


def load_known_events():
    if not os.path.exists(STATE_FILE):
        return set()

    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8",
        ) as file:
            data = json.load(file)

        return set(
            str(value)
            for value in data
        )

    except Exception:
        return set()


def save_known_events(event_ids):
    sorted_ids = sorted(
        event_ids,
        key=lambda value: int(value),
    )

    with open(
        STATE_FILE,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            sorted_ids,
            file,
            ensure_ascii=False,
            indent=2,
        )


def initialize_firebase():
    service_account = os.environ.get(
        "FIREBASE_SERVICE_ACCOUNT"
    )

    if not service_account:
        raise RuntimeError(
            "FIREBASE_SERVICE_ACCOUNT fehlt."
        )

    service_account_data = json.loads(
        service_account
    )

    cred = credentials.Certificate(
        service_account_data
    )

    firebase_admin.initialize_app(cred)


def send_event_push(event):
    title = load_event_title(event)

    print(
        f"Veranstaltungstitel: {title}"
    )

    message = messaging.Message(
        notification=messaging.Notification(
            title="Neue KAMEA Veranstaltung 🎉",
            body=title,
        ),
        data={
            "type": "event",
            "event_id": event["id"],
            "event_url": event["url"],
            "event_title": title,
        },
        topic=TOPIC,
    )

    message_id = messaging.send(message)

    print(
        f"Push für Event {event['id']} "
        f"gesendet: {message_id}"
    )


def main():
    print("KAMEA Event-Checker gestartet.")

    current_events = load_current_events()

    if not current_events:
        raise RuntimeError(
            "Keine Veranstaltungen auf der "
            "KAMEA-Webseite gefunden."
        )

    current_ids = set(
        current_events.keys()
    )

    known_ids = load_known_events()

    print(
        f"Aktuelle Events: {len(current_ids)}"
    )

    print(
        f"Bereits bekannte Events: {len(known_ids)}"
    )

    # Beim ersten Lauf werden vorhandene
    # Veranstaltungen nur gespeichert.
    if not known_ids:
        save_known_events(current_ids)

        print(
            "Erster Lauf: vorhandene Events wurden "
            "als bekannt gespeichert."
        )

        print(
            "Es wurde absichtlich kein Push gesendet."
        )

        return

    new_ids = current_ids - known_ids

    if not new_ids:
        print(
            "Keine neue Veranstaltung gefunden."
        )

        return

    initialize_firebase()

    successfully_processed = set()

    for event_id in sorted(
        new_ids,
        key=lambda value: int(value),
    ):
        event = current_events[event_id]

        try:
            send_event_push(event)
            successfully_processed.add(event_id)

            # Nach jedem erfolgreich gesendeten Push
            # sofort speichern. So vermeiden wir
            # doppelte Pushs bei einem späteren Fehler.
            save_known_events(
                known_ids
                | successfully_processed
            )

        except Exception as error:
            print(
                f"FEHLER bei Event {event_id}: "
                f"{error}"
            )

            raise

    print(
        f"{len(successfully_processed)} neue "
        f"Veranstaltung(en) verarbeitet."
    )


if __name__ == "__main__":
    main()
