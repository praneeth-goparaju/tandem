import pytest

from src.brain.followups.scrub import scrub_secrets


def assembled(*parts: str) -> str:
    return "".join(parts)


SECRETS = {
    "openai key": assembled("sk-", "proj", "A1b2" * 12),
    "slack token": assembled("xoxb-", "1234567890-", "A1b2" * 6),
    "github token": assembled("ghp_", "A1b2" * 9),
    "aws key id": assembled("AKIA", "ABCD" * 4),
    "google key": assembled("AIza", "A1b2" * 4, "-", "A1b2" * 4, "_", "A"),
    "private key block": "\n".join(
        [
            assembled("-----BEGIN ", "RSA PRIVATE KEY-----"),
            "A1b2" * 16,
            "A1b2" * 16,
            assembled("-----END ", "RSA PRIVATE KEY-----"),
        ]
    ),
    "jwt": ".".join(
        [assembled("eyJ", "A1b2" * 9), assembled("eyJ", "A1b2" * 12), "A1b2" * 10]
    ),
    "password assignment": "password: hunter2",
    "upper-case assignment": "API_KEY=abc123",
    "token assignment": "token = s3cr3t",
    "base64 run": "A1b2" * 8,
    "padded base64 run": "A1b2" * 10 + "==",
    "hex run": "deadbeef" * 5,
}


@pytest.mark.parametrize("secret", SECRETS.values(), ids=SECRETS.keys())
def test_each_secret_is_replaced_and_the_words_around_it_survive(secret):
    assert scrub_secrets(f"before {secret} after") == "before [redacted] after"


def test_a_31_character_token_and_a_plain_sentence_are_untouched():
    token = "A1b2" * 7 + "A1b"
    assert len(token) == 31
    text = f"Remind me to check the Garmin sync before Tuesday's call. Ref {token}."
    assert scrub_secrets(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "the risk-assessment doc",
        "see https://slack.com/archives/C0123456789/p1791957700123456 for context",
        "open Users/me/Documents/projects/brain/tests/followups/fixtures now",
        "Enter your password:\nThen click ok",
        "https://slack.com/archives/C0123ABCD/p1790391679423389",
        "/Users/me/Library/Application Support/Claude/local-agent-mode-sessions/0d2a5328",
    ],
    ids=[
        "word ending in sk-",
        "slack permalink",
        "slash path",
        "assignment at end of line",
        "permalink with a 40-character path",
        "application support path",
    ],
)
def test_text_that_only_looks_like_a_secret_is_left_alone(text):
    assert scrub_secrets(text) == text


def test_an_assignment_takes_its_value_but_nothing_from_the_next_line():
    assert scrub_secrets("password: hunter2\nnext line") == "[redacted]\nnext line"


def test_a_hash_between_slashes_is_still_redacted():
    path = "see /objects/" + "0123456789abcdef" * 2 + "/info"
    assert scrub_secrets(path) == "see /objects/[redacted]/info"


def test_a_40_character_key_with_slashes_is_redacted():
    key = "A1b2" * 3 + "/" + "C3d4" * 4 + "/" + "E5f6" * 2 + "E5"
    assert len(key) == 40 and key.count("/") == 2
    assert scrub_secrets(f"before {key} after") == "before [redacted] after"


def test_a_36_character_run_with_a_plus_and_a_slash_is_redacted():
    run = "A1b2" * 4 + "+" + "C3d4" * 3 + "/" + "E5f6" + "G7"
    assert len(run) == 36
    assert scrub_secrets(f"before {run} after") == "before [redacted] after"


def test_a_key_assigned_without_spaces_is_redacted():
    key = "A1b2" * 3 + "/" + "C3d4" * 4 + "/" + "E5f6" * 2 + "E5"
    assert len(key) == 40
    assert scrub_secrets(f"export AWS_SECRET_ACCESS_KEY={key}") == "export AWS_SECRET_[redacted]"
    assert scrub_secrets(f"PAYLOAD={key}") == "PAYLOAD=[redacted]"


def test_a_keyword_after_an_underscore_starts_an_assignment():
    assert scrub_secrets("session_token: abc123") == "session_[redacted]"


def test_a_keyword_inside_a_longer_word_does_not_start_an_assignment():
    assert scrub_secrets("mytoken=abc") == "mytoken=abc"


LEAKS = {
    "json password": ('{"password": "hunter2xyz"}', '{"[redacted]}'),
    "json api key": ('{"api_key": "abc123def456"}', '{"[redacted]}'),
    "dict token": ("{'token': 'tok_abc123'}", "{'[redacted]}"),
    "quoted value taken whole": ('password: "correct horse battery staple" ok', "[redacted] ok"),
    "single-quoted value": ("secret = 'two words' ok", "[redacted] ok"),
    "passphrase": ("passphrase: hunter2xyz", "[redacted]"),
    "postgres url": (
        "postgres://admin:S3cretPassw0rd@db.example.com:5432/app",
        "postgres://admin:[redacted]@db.example.com:5432/app",
    ),
    "redis url without a user": (
        "redis://:myredispass@localhost:6379/0",
        "redis://:[redacted]@localhost:6379/0",
    ),
    "password flag": ("mysql -u root --password hunter2xyz", "mysql -u root [redacted]"),
    "password flag with =": ("mysql --password=hunter2xyz", "mysql [redacted]"),
    "slack webhook": (
        assembled("see https://hooks.slack.com/services/", "T0000/B0000/", "X1y2" * 6),
        "see https://[redacted]",
    ),
    "legacy slack token": (assembled("xoxs-", "1234-5678"), "[redacted]"),
    "app token": (assembled("xapp-", "1-A0123-456-abc"), "[redacted]"),
    "authorization bearer": ("Authorization: Bearer abcdef1234567890", "[redacted]"),
    "authorization basic": ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "[redacted]"),
    "authorization in json": ('{"Authorization": "Bearer abcdef1234567890"}', '{"[redacted]}'),
    "private key without its end line": (
        "\n".join(["key:", assembled("-----BEGIN ", "PRIVATE KEY-----"), "MIIEvQ", "abc"]),
        "key:\n[redacted]",
    ),
}


@pytest.mark.parametrize(("text", "scrubbed"), LEAKS.values(), ids=LEAKS.keys())
def test_common_secret_shapes_are_redacted(text, scrubbed):
    assert scrub_secrets(text) == scrubbed


def test_a_key_block_with_its_end_line_keeps_the_text_after_it():
    block = SECRETS["private key block"]
    unended = assembled("-----BEGIN ", "RSA PRIVATE KEY-----") + "\nA1b2"
    assert scrub_secrets(f"one {block} two {unended}") == "one [redacted] two [redacted]"


@pytest.mark.parametrize(
    "text",
    [
        "https://slack.com/archives/C0123ABCD/p1790391679423389",
        "ssh://git@code.example.com:22/me/repo.git",
        "mailto:me@example.com",
        "the xoxo of it",
        "basic understanding of the bearer of bad news",
    ],
    ids=["permalink", "user without a password", "mail link", "xoxo", "basic and bearer as words"],
)
def test_urls_and_words_that_carry_no_credential_are_left_alone(text):
    assert scrub_secrets(text) == text


@pytest.mark.parametrize("secret", ["sk_live_4eC39HqLyjWDarjtT1zdp7dc", "GOCSPX-abcDEF123_ghi-JKL456"])
def test_stripe_and_google_client_secrets_are_redacted(secret):
    assert scrub_secrets(f"key {secret} here") == "key [redacted] here"
