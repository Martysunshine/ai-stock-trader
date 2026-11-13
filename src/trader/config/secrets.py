"""Broker and data keys from the operating-system keyring (ADR-0026, ADR-0032).

Keys live in the OS keyring (Windows Credential Manager, macOS Keychain), under the service name a
mode configuration references as ``secret_ref`` (for example ``ai-stock-trader/alpaca-paper``).

* The **active** key pair is two entries under that service: usernames ``key_id`` and
  ``secret_key``; username ``profile`` names the profile it came from.
* **Profiles** let the owner keep several paper key pairs and switch with one command
  (``trader secrets use alpaca-paper <profile>``). Each profile is stored under the service
  ``<service>#<profile>``; ``<service>#profiles`` holds the list of profile names.
* The **keys folder** (``~/.ai-stock-trader/keys`` or ``TRADER_KEYS_DIR``, outside the repository)
  is an inbox: drop ``<profile>.env`` files there and ``trader secrets import`` moves them into the
  keyring and deletes the files (``--keep`` leaves them).
* The project never prints, logs or writes a key into the repository, a manifest or a report.
  Callers get presence (``True``/``False``) or a redacted credentials object, never the text.
* A key that looks like a **live** Alpaca key (``AK…``), or a key file naming a non-paper endpoint, is
  refused for a paper service. Live trading stays impossible by design.
* Session environment variables (``APCA_API_KEY_ID``/``APCA_API_SECRET_KEY``) still work and take
  precedence, so the documented M2 route keeps working.

Other secrets (ADR-0036) are **plain** services: one fixed set of named fields under
``ai-stock-trader/<name>`` (one keyring username per field), no profiles, no keys folder. They exist
only to launch an assistant-side research tool with ``trader secrets exec`` (for example the
Capital.com demo MCP server); the bot itself never reads them. A plain service can strip inherited
variables and force safety settings that the parent environment cannot override.

Owner decision 2026-11-09 (ADR-0039): a plain service's values may instead sit in the repository's
gitignored ``.env`` under each field's variable name (``ALPHA_VANTAGE_API_KEY=...``). ``.env`` wins
over the keyring; the Alpaca key pair is unchanged (keyring and profiles only).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import keyring
import keyring.errors

from trader.config.alpaca_keys import KEY_ENV, SECRET_ENV, AlpacaCredentials
from trader.domain.types import DomainError

SERVICE_PREFIX = "ai-stock-trader/"


@dataclass(frozen=True, slots=True)
class SecretField:
    """One value of a plain service: its keyring username, prompt label and child variable."""

    user: str
    prompt: str
    env: str
    #: Keep the value byte-exact (a password); other values are trimmed of copy/paste whitespace.
    exact: bool = False


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    """A secret ``trader secrets`` knows. ``alpaca`` = the profile-based Alpaca key pair."""

    name: str
    kind: Literal["alpaca", "plain"]
    fields: tuple[SecretField, ...] = ()
    #: Applied last in ``child_environment``: the parent environment can never override these.
    forced_env: tuple[tuple[str, str], ...] = ()
    #: Inherited variables removed (case-insensitive prefix match) before anything is set.
    strip_env_prefixes: tuple[str, ...] = ()
    #: Inherited variables removed by exact name (case-insensitive).
    strip_env_names: tuple[str, ...] = ()

    @property
    def service(self) -> str:
        return SERVICE_PREFIX + self.name


#: Capital.com DEMO epics the assistant may trade for practice: the CFD sleeve's nine (ADR-0044;
#: US30 and RTY since ADR-0056, the index dip's four indices; four practice commodities since ADR-0060; configs/practice/capital_demo.yaml)
#: and the crypto sleeve's two (ADR-0049,
#: configs/practice/crypto_cfd.yaml), in that order; checked by tests/test_practice_journal.py.
CAPITAL_DEMO_EPICS = (
    "GOLD",
    "SILVER",
    "OIL_CRUDE",
    "US500",
    "US100",
    "DE40",
    "EURUSD",
    "US30",
    "RTY",
    "COPPER",
    "OIL_BRENT",
    "HEATINGOIL",
    "NATURALGAS",
    "BTCUSD",
    "ETHUSD",
)
#: capital-mcp's single size cap (contract units, all epics): the largest per-epic practice maximum
#: (EURUSD 100 units). The guard enforces each epic's own, much smaller, maximum.
CAPITAL_DEMO_MAX_CONTRACTS = "100"

#: Every secret the project knows (ADR-0026, ADR-0032, ADR-0036).
SPECS: dict[str, ServiceSpec] = {
    "alpaca-paper": ServiceSpec("alpaca-paper", "alpaca"),
    # Capital.com DEMO keys for the assistant's MCP server (capital-mcp 0.5.0). Since ADR-0044 the
    # owner allows bounded DEMO trading for practice: the account is forced to demo, every order
    # needs explicit confirmation, only the practice epics are allowed and the daily order count is
    # capped. Every inherited CAP_* variable is dropped first, so a parent CAP_ENV=live or
    # CAP_ALLOWED_EPICS=ALL never arrives. The per-epic size, stop, risk and journal rules are
    # enforced by the PreToolUse guard (trader.practice.capital_guard), because capital-mcp at
    # 3207be4 reports but does not enforce its position caps.
    "capital-demo": ServiceSpec(
        "capital-demo",
        "plain",
        fields=(
            SecretField("api_key", "Capital.com demo API key", "CAP_API_KEY"),
            SecretField("identifier", "Capital.com login (email)", "CAP_IDENTIFIER"),
            SecretField("api_password", "API key custom password", "CAP_API_PASSWORD", exact=True),
        ),
        forced_env=(
            ("CAP_ENV", "demo"),
            ("CAP_ALLOW_TRADING", "true"),
            ("CAP_DRY_RUN", "false"),
            ("CAP_REQUIRE_EXPLICIT_CONFIRM", "true"),
            ("CAP_ALLOWED_EPICS", ",".join(CAPITAL_DEMO_EPICS)),
            ("CAP_MAX_POSITION_SIZE", CAPITAL_DEMO_MAX_CONTRACTS),
            ("CAP_MAX_WORKING_ORDER_SIZE", CAPITAL_DEMO_MAX_CONTRACTS),
            # two sleeves on the one demo account, each two open trades and four opens a day
            ("CAP_MAX_OPEN_POSITIONS", "4"),
            ("CAP_MAX_ORDERS_PER_DAY", "16"),
            ("CAP_WS_ENABLED", "false"),
        ),
        strip_env_prefixes=("CAP_",),
        strip_env_names=("VIRTUAL_ENV",),
    ),
    # Alpha Vantage key for its official local MCP server (marketdata-mcp-server 0.3.1): read-only
    # market, fundamentals and macro data, 25 requests a day on the free plan (ADR-0039).
    "alphavantage": ServiceSpec(
        "alphavantage",
        "plain",
        fields=(SecretField("api_key", "Alpha Vantage API key", "ALPHA_VANTAGE_API_KEY"),),
        strip_env_names=("VIRTUAL_ENV",),
    ),
    # The contact address the SEC asks automated EDGAR readers to name in their User-Agent
    # (ADR-0047). Not a key: it grants nothing, but it is the owner's address, so it lives in
    # .env or the keyring and never in a committed file or in output.
    "sec-edgar": ServiceSpec(
        "sec-edgar",
        "plain",
        fields=(SecretField("contact", "SEC EDGAR contact email", "SEC_EDGAR_CONTACT"),),
    ),
}
#: The repository's gitignored ``.env``; plain services read their fields here first (ADR-0039).
DOTENV_PATH = Path(__file__).resolve().parents[3] / ".env"
#: Short names accepted by ``trader secrets`` and the service each one maps to.
KNOWN_SERVICES = {name: spec.service for name, spec in SPECS.items()}
DEFAULT_ALPACA_SERVICE = KNOWN_SERVICES["alpaca-paper"]
KEY_ID_USER = "key_id"
SECRET_USER = "secret_key"
PROFILE_USER = "profile"
INDEX_USER = "index"
DEFAULT_PROFILE = "default"
LIVE_KEY_PREFIX = "AK"
PAPER_KEY_PREFIX = "PK"
PAPER_HOST = "paper-api.alpaca.markets"
KEYS_DIR_ENV = "TRADER_KEYS_DIR"
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

#: Variable names accepted in a key file (the first match wins, in this order).
KEY_FILE_KEY_NAMES = (
    "APCA_API_KEY_ID",
    "ALPACA_API_KEY_ID",
    "ALPACA_API_KEY",
    "ALPACA_KEY_ID",
    "ALPACA_KEY",
    "API_KEY_ID",
    "API_KEY",
    "KEY_ID",
)
KEY_FILE_SECRET_NAMES = (
    "APCA_API_SECRET_KEY",
    "ALPACA_SECRET_KEY",
    "ALPACA_API_SECRET",
    "ALPACA_SECRET",
    "API_SECRET_KEY",
    "API_SECRET",
    "SECRET_KEY",
    "SECRET",
)
KEY_FILE_ENDPOINT_NAMES = ("APCA_API_BASE_URL", "ALPACA_ENDPOINT", "ENDPOINT", "BASE_URL")


class SecretsError(DomainError):
    """A keyring problem or a refused key. Messages never contain key material."""


def spec_for(name: str) -> ServiceSpec:
    """The spec for a short name (``alpaca-paper``) or a full service name."""
    short = name[len(SERVICE_PREFIX) :] if name.startswith(SERVICE_PREFIX) else name
    spec = SPECS.get(short)
    if spec is None:
        raise SecretsError(f"unknown secret {name!r}; known: {', '.join(sorted(KNOWN_SERVICES))}")
    return spec


def service_name(name: str) -> str:
    """Map a short name (``alpaca-paper``) or a full service name to the keyring service."""
    return spec_for(name).service


def _plain(service: str) -> ServiceSpec | None:
    """The spec when ``service`` names a known plain service, else ``None``.

    Any other service name keeps the Alpaca key-pair behaviour it always had (a mode
    configuration's ``secret_ref`` may name a service the registry does not list)."""
    short = service[len(SERVICE_PREFIX) :] if service.startswith(SERVICE_PREFIX) else service
    spec = SPECS.get(short)
    return spec if spec is not None and spec.kind == "plain" else None


def require_alpaca_kind(service: str, action: str) -> None:
    """Profiles, the keys folder and Alpaca verification exist only for Alpaca key pairs."""
    spec = _plain(service)
    if spec is not None:
        raise SecretsError(
            f"{spec.name} stores one set of keys: {action} is only available for alpaca-paper"
        )


def check_profile(profile: str) -> str:
    if not _PROFILE_RE.match(profile):
        raise SecretsError(
            f"invalid profile name {profile!r}: use lowercase letters, digits, '-' or '_' "
            "(at most 40 characters, starting with a letter or digit)"
        )
    return profile


def _is_paper(service: str) -> bool:
    return service.endswith("-paper")


def check_key_id(service: str, key_id: str) -> str | None:
    """Refuse a live-looking key for a paper service; return a warning text or ``None``."""
    if _is_paper(service) and key_id.upper().startswith(LIVE_KEY_PREFIX):
        raise SecretsError(
            "this key ID looks like a LIVE Alpaca key (live keys start with AK); only paper keys "
            "(starting with PK) may be stored for a paper service. Nothing was stored."
        )
    if _is_paper(service) and not key_id.upper().startswith(PAPER_KEY_PREFIX):
        return "note: Alpaca paper key IDs usually start with PK; double-check it is a paper key"
    return None


def _profile_service(service: str, profile: str) -> str:
    return f"{service}#{profile}"


def _index_service(service: str) -> str:
    return f"{service}#profiles"


def _get(service: str, user: str) -> str | None:
    try:
        return keyring.get_password(service, user)
    except keyring.errors.KeyringError as exc:
        raise SecretsError(f"OS keyring unavailable: {type(exc).__name__}") from None


def _set(service: str, user: str, value: str) -> None:
    try:
        keyring.set_password(service, user, value)
    except keyring.errors.KeyringError as exc:
        raise SecretsError(f"could not write to the OS keyring: {type(exc).__name__}") from None


def _remove(service: str, user: str) -> bool:
    try:
        keyring.delete_password(service, user)
    except keyring.errors.PasswordDeleteError:
        return False
    except keyring.errors.KeyringError as exc:
        raise SecretsError(f"OS keyring unavailable: {type(exc).__name__}") from None
    return True


def profiles(service: str) -> tuple[list[str], str | None]:
    """Stored profile names (sorted) and the active profile, if any. Names only, never keys."""
    require_alpaca_kind(service, "profiles")
    index = _get(_index_service(service), INDEX_USER) or ""
    names = sorted({n for n in index.split(",") if n})
    return names, _get(service, PROFILE_USER)


def _write_index(service: str, names: list[str]) -> None:
    if names:
        _set(_index_service(service), INDEX_USER, ",".join(sorted(set(names))))
    else:
        _remove(_index_service(service), INDEX_USER)


def store_alpaca(
    service: str,
    key_id: str,
    secret: str,
    *,
    profile: str = DEFAULT_PROFILE,
    activate: bool = True,
) -> str | None:
    """Store a key pair as ``profile`` (and make it active); returns an optional warning."""
    require_alpaca_kind(service, "a key pair with profiles")
    check_profile(profile)
    key_id, secret = key_id.strip(), secret.strip()
    if not key_id or not secret:
        raise SecretsError("key ID and secret must both be non-empty; nothing was stored")
    warning = check_key_id(service, key_id)
    slot = _profile_service(service, profile)
    _set(slot, KEY_ID_USER, key_id)
    _set(slot, SECRET_USER, secret)
    names, _ = profiles(service)
    _write_index(service, [*names, profile])
    if activate:
        use_profile(service, profile)
    return warning


def use_profile(service: str, profile: str) -> None:
    """Make a stored profile the active key pair."""
    require_alpaca_kind(service, "switching profiles")
    check_profile(profile)
    slot = _profile_service(service, profile)
    key_id, secret = _get(slot, KEY_ID_USER), _get(slot, SECRET_USER)
    if not key_id or not secret:
        names, _ = profiles(service)
        raise SecretsError(
            f"no stored profile {profile!r} for {service}; stored: {', '.join(names) or 'none'}"
        )
    check_key_id(service, key_id)
    _set(service, KEY_ID_USER, key_id)
    _set(service, SECRET_USER, secret)
    _set(service, PROFILE_USER, profile)


def delete(service: str, profile: str | None = None) -> int:
    """Without ``profile``: clear the active pair. With it: remove that stored profile (and the
    active pair if it was active). Returns how many key entries were removed.

    A plain service has no profiles: its fields are removed and ``profile`` must be ``None``."""
    spec = _plain(service)
    if spec is not None:
        if profile is not None:
            require_alpaca_kind(service, "--profile")
        return sum(_remove(spec.service, f.user) for f in spec.fields)
    removed = 0
    names, active = profiles(service)
    if profile is not None:
        check_profile(profile)
        slot = _profile_service(service, profile)
        removed += sum(_remove(slot, user) for user in (KEY_ID_USER, SECRET_USER))
        _write_index(service, [n for n in names if n != profile])
        if active != profile:
            return removed
    removed += sum(_remove(service, user) for user in (KEY_ID_USER, SECRET_USER))
    _remove(service, PROFILE_USER)
    return removed


@dataclass(frozen=True, slots=True)
class SecretStatus:
    service: str
    key_id_present: bool
    secret_present: bool
    env_override: bool
    active_profile: str | None = None

    @property
    def ok(self) -> bool:
        return self.env_override or (self.key_id_present and self.secret_present)


def status(service: str) -> SecretStatus:
    require_alpaca_kind(service, "key-pair status")
    return SecretStatus(
        service=service,
        key_id_present=bool(_get(service, KEY_ID_USER)),
        secret_present=bool(_get(service, SECRET_USER)),
        env_override=bool(os.environ.get(KEY_ENV) and os.environ.get(SECRET_ENV)),
        active_profile=_get(service, PROFILE_USER),
    )


def load_alpaca(service: str = DEFAULT_ALPACA_SERVICE) -> AlpacaCredentials | None:
    """Session environment variables first, then the OS keyring; ``None`` when neither is set."""
    require_alpaca_kind(service, "loading Alpaca keys")
    key, secret = os.environ.get(KEY_ENV, ""), os.environ.get(SECRET_ENV, "")
    if key and secret:
        check_key_id(service, key)
        return AlpacaCredentials(key, secret)
    key_id, stored_secret = _get(service, KEY_ID_USER), _get(service, SECRET_USER)
    if not key_id or not stored_secret:
        return None
    check_key_id(service, key_id)
    return AlpacaCredentials(key_id, stored_secret)


def require_alpaca(service: str = DEFAULT_ALPACA_SERVICE) -> AlpacaCredentials:
    creds = load_alpaca(service)
    if creds is None:
        raise SecretsError(
            f"owner action: no Alpaca keys for {service}. Run `uv run trader secrets set "
            "alpaca-paper` (hidden prompt) or drop a key file into the keys folder and run "
            "`uv run trader secrets import` (docs/owner_setup.md §2)"
        )
    return creds


# --------------------------------------------------------------------------- plain services
def _plain_spec(service: str) -> ServiceSpec:
    spec = _plain(service)
    if spec is None:
        raise SecretsError(f"{service} is not a plain secret; use the key-pair commands")
    return spec


def store_plain(service: str, values: Mapping[str, str]) -> None:
    """Store every field of a plain service, all or nothing. ``values`` is keyed by field user."""
    spec = _plain_spec(service)
    cleaned: list[tuple[str, str]] = []
    for f in spec.fields:
        raw = values.get(f.user, "")
        value = raw if f.exact else raw.strip()
        if not value.strip():
            raise SecretsError(f"{f.prompt} must be non-empty; nothing was stored")
        cleaned.append((f.user, value))
    for user, value in cleaned:
        _set(spec.service, user, value)


def _dotenv() -> dict[str, str]:
    """``NAME=value`` pairs of the repository ``.env`` (names upper-cased); empty if unreadable."""
    try:
        return _parse_env_lines(DOTENV_PATH.read_text(encoding="utf-8-sig"))
    except OSError:
        return {}


def plain_sources(service: str) -> list[tuple[SecretField, str | None]]:
    """Where each field of a plain service comes from: ``".env"``, ``"keyring"`` or ``None``."""
    spec = _plain_spec(service)
    dotenv = _dotenv()

    def source(f: SecretField) -> str | None:
        if dotenv.get(f.env.upper()):
            return ".env"
        return "keyring" if _get(spec.service, f.user) else None

    return [(f, source(f)) for f in spec.fields]


def plain_status(service: str) -> list[tuple[SecretField, bool]]:
    """Which fields of a plain service are available. Presence only, never values."""
    return [(f, source is not None) for f, source in plain_sources(service)]


def _plain_values(spec: ServiceSpec) -> dict[str, str]:
    dotenv = _dotenv()
    values: dict[str, str] = {}
    for f in spec.fields:
        value = dotenv.get(f.env.upper()) or _get(spec.service, f.user)
        if not value:
            raise SecretsError(
                f"owner action: {spec.name} is incomplete ({f.user} missing). Add {f.env}=... to "
                f"the repository .env, or run `uv run trader secrets set {spec.name}` (hidden prompt)"
            )
        values[f.env] = value
    return values


@dataclass(frozen=True, slots=True)
class CapitalDemoCredentials:
    """The capital-demo login for the dashboard's price feed (ADR-0045). Never printed."""

    api_key: str = field(repr=False)
    identifier: str = field(repr=False)
    password: str = field(repr=False)

    def __repr__(self) -> str:
        return "CapitalDemoCredentials(<redacted>)"


def require_capital_demo() -> CapitalDemoCredentials:
    """The three capital-demo fields (``.env`` first, then the keyring; ADR-0039).

    Read by the market feed (``trader.market``, ADR-0045) and by research ingest
    (``trader ingest-history``, ADR-0052), which log in to the Capital.com **demo** host and read
    prices, and by the practice trader (``trader.practice``, ADR-0056), which also opens and
    closes demo positions through ``trader.practice.capital_orders``. Raises ``SecretsError``
    naming the missing field, never a value.
    """
    values = _plain_values(spec_for("capital-demo"))
    return CapitalDemoCredentials(
        api_key=values["CAP_API_KEY"],
        identifier=values["CAP_IDENTIFIER"],
        password=values["CAP_API_PASSWORD"],
    )


_CONTACT_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,180}\.[A-Za-z]{2,24}$")


@dataclass(frozen=True, slots=True)
class SecEdgarContact:
    """The owner's contact address for SEC EDGAR requests (ADR-0047). Never printed."""

    email: str = field(repr=False)

    def __repr__(self) -> str:
        return "SecEdgarContact(<redacted>)"


def require_sec_edgar_contact() -> SecEdgarContact:
    """The contact address for EDGAR's User-Agent (``.env`` first, then the keyring).

    Read only by the intel feed (``trader.intel``). Raises ``SecretsError`` when the line is
    missing or is not one plain email address; the message never repeats the value.
    """
    email = _plain_values(spec_for("sec-edgar"))["SEC_EDGAR_CONTACT"].strip()
    if not _CONTACT_EMAIL.fullmatch(email):
        raise SecretsError(
            "owner action: SEC_EDGAR_CONTACT must be one plain email address "
            "(name@example.com), nothing else on the line"
        )
    return SecEdgarContact(email)


def _stripped(spec: ServiceSpec, name: str) -> bool:
    upper = name.upper()
    if upper in {n.upper() for n in spec.strip_env_names}:
        return True
    return any(upper.startswith(p.upper()) for p in spec.strip_env_prefixes)


# --------------------------------------------------------------------------- keys folder
KEYS_README = """Keys inbox for ai-stock-trader (outside the git repository on purpose).

1. Copy TEMPLATE.env.example to <profile>.env, e.g. paper-main.env or paper-2.env.
   Profile names: lowercase letters, digits, '-' or '_'.
2. Put ONE Alpaca PAPER key pair in it (paper key IDs start with PK; live keys are refused).
3. Run in the project folder:   python -m uv run trader secrets import
   Each <profile>.env is stored in Windows Credential Manager and then DELETED from this folder
   (add --keep to leave the files). The first imported profile becomes active if none is.
4. Switch keys any time:          python -m uv run trader secrets use alpaca-paper <profile>
   List profiles:                 python -m uv run trader secrets list alpaca-paper

Never commit, share or paste these files. If a key leaks, revoke it in the Alpaca dashboard.
"""

KEYS_TEMPLATE = """# Alpaca PAPER keys for one profile. Rename this file to <profile>.env first.
APCA_API_KEY_ID=PK...
APCA_API_SECRET_KEY=...
# Optional; if given it must be the paper endpoint.
APCA_API_BASE_URL=https://paper-api.alpaca.markets
"""


def keys_dir() -> Path:
    custom = os.environ.get(KEYS_DIR_ENV)
    return Path(custom) if custom else Path.home() / ".ai-stock-trader" / "keys"


def ensure_keys_dir(directory: Path | None = None) -> Path:
    """Create the keys folder with a README and a template (never overwrites key files)."""
    d = directory or keys_dir()
    d.mkdir(parents=True, exist_ok=True)
    for name, text in (("README.txt", KEYS_README), ("TEMPLATE.env.example", KEYS_TEMPLATE)):
        target = d / name
        if not target.exists():
            target.write_text(text, encoding="utf-8", newline="\n")
    return d


def _parse_env_lines(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        name, value = line.split("=", 1)
        values[name.strip().upper()] = value.strip().strip('"').strip("'")
    return values


def parse_key_file(path: Path, service: str = DEFAULT_ALPACA_SERVICE) -> tuple[str, str]:
    """Return ``(key_id, secret)`` from a ``NAME=VALUE`` file. Errors never quote values."""
    values = _parse_env_lines(path.read_text(encoding="utf-8-sig"))
    key_id = next((values[n] for n in KEY_FILE_KEY_NAMES if values.get(n)), "")
    secret = next((values[n] for n in KEY_FILE_SECRET_NAMES if values.get(n)), "")
    if not key_id or not secret:
        raise SecretsError(
            f"{path.name}: needs a key ID ({KEY_FILE_KEY_NAMES[0]}) and a secret "
            f"({KEY_FILE_SECRET_NAMES[0]}); nothing was imported from it"
        )
    endpoint = next((values[n] for n in KEY_FILE_ENDPOINT_NAMES if values.get(n)), "")
    if endpoint and _is_paper(service) and PAPER_HOST not in endpoint.lower():
        raise SecretsError(
            f"{path.name}: its endpoint is not the paper endpoint ({PAPER_HOST}); live keys are "
            "refused. Nothing was imported from it."
        )
    check_key_id(service, key_id)
    return key_id, secret


@dataclass(frozen=True, slots=True)
class ImportedKey:
    profile: str
    file: str
    warning: str | None
    deleted: bool


def import_key_files(
    service: str,
    directory: Path | None = None,
    *,
    keep: bool = False,
    use: str | None = None,
) -> list[ImportedKey]:
    """Import every ``<profile>.env`` in the keys folder into the keyring (all or nothing per file)."""
    require_alpaca_kind(service, "the keys folder")
    d = directory or keys_dir()
    if not d.is_dir():
        raise SecretsError(f"keys folder {d} does not exist; run `trader secrets folder` first")
    files = sorted(p for p in d.glob("*.env") if p.is_file())
    parsed = []
    for path in files:  # validate everything before storing anything
        profile = check_profile(path.stem.lower())
        parsed.append((path, profile, *parse_key_file(path, service)))
    imported = []
    for path, profile, key_id, secret in parsed:
        warning = store_alpaca(service, key_id, secret, profile=profile, activate=False)
        if not keep:
            path.unlink()
        imported.append(ImportedKey(profile, path.name, warning, deleted=not keep))
    _, active = profiles(service)
    target = use or (imported[0].profile if imported and active is None else None)
    if target is not None:
        use_profile(service, target)
    return imported


def child_environment(service: str, base: Mapping[str, str]) -> dict[str, str]:
    """Environment for a local tool (e.g. an MCP server) with the service's keys.

    Alpaca: the active paper keys, with paper trading forced on. A plain service: the inherited
    variables its spec strips are dropped first, then its fields are added, then its forced
    settings, which nothing inherited can override. Nothing is printed; the caller passes the
    result to a subprocess only.
    """
    spec = _plain(service)
    if spec is not None:
        values = _plain_values(spec)
        env = {k: v for k, v in base.items() if not _stripped(spec, k)}
        env.update(values)
        env.update(dict(spec.forced_env))
        return env
    creds = require_alpaca(service)
    env = dict(base)
    key_id, secret = creds.key_id, creds.secret
    env.update(
        {
            KEY_ENV: key_id,
            SECRET_ENV: secret,
            "ALPACA_API_KEY": key_id,
            "ALPACA_SECRET_KEY": secret,
        }
    )
    if _is_paper(service):
        env["ALPACA_PAPER_TRADE"] = "true"
        env["APCA_API_BASE_URL"] = f"https://{PAPER_HOST}"
    return env
