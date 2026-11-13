"""OS-keyring secrets (ADR-0026): presence only, redacted, paper keys only, never printed."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import keyring
import keyring.backend
import keyring.errors
import pytest
from typer.testing import CliRunner

from trader.cli.main import app
from trader.config import secrets
from trader.config.secrets import (
    DEFAULT_ALPACA_SERVICE,
    SecretsError,
    load_alpaca,
    require_alpaca,
    service_name,
    status,
    store_alpaca,
)

PAPER_ID = "PKTESTKEYID0000000000"
SECRET = "s3cr3t-value-that-must-never-be-printed"


class MemoryKeyring(keyring.backend.KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.store:
            raise keyring.errors.PasswordDeleteError(username)
        del self.store[(service, username)]


@pytest.fixture
def ring(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[MemoryKeyring]:
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    monkeypatch.delenv(secrets.KEY_ENV, raising=False)
    monkeypatch.delenv(secrets.SECRET_ENV, raising=False)
    # Never read the developer's real .env: each test gets its own (absent until written).
    monkeypatch.setattr(secrets, "DOTENV_PATH", tmp_path / "repo.env")
    yield backend
    keyring.set_keyring(previous)


def test_round_trip_is_redacted(ring: MemoryKeyring) -> None:
    assert load_alpaca() is None
    assert store_alpaca(DEFAULT_ALPACA_SERVICE, f"  {PAPER_ID}\n", f"{SECRET} ") is None
    creds = require_alpaca()
    assert creds.headers() == {"APCA-API-KEY-ID": PAPER_ID, "APCA-API-SECRET-KEY": SECRET}
    assert SECRET not in repr(creds) and PAPER_ID not in repr(creds)
    st = status(DEFAULT_ALPACA_SERVICE)
    assert st.ok and st.key_id_present and st.secret_present and not st.env_override
    assert SECRET not in repr(st)


def test_live_key_is_refused_for_a_paper_service(ring: MemoryKeyring) -> None:
    with pytest.raises(SecretsError, match="LIVE"):
        store_alpaca(DEFAULT_ALPACA_SERVICE, "AKLIVEKEY", SECRET)
    assert ring.store == {}
    assert store_alpaca(DEFAULT_ALPACA_SERVICE, "XXOTHER", SECRET) is not None  # warning only


def test_live_key_planted_in_keyring_or_env_is_refused(
    ring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    ring.store[(DEFAULT_ALPACA_SERVICE, "key_id")] = "AKLIVE"
    ring.store[(DEFAULT_ALPACA_SERVICE, "secret_key")] = SECRET
    with pytest.raises(SecretsError):
        load_alpaca()
    monkeypatch.setenv(secrets.KEY_ENV, "AKLIVE2")
    monkeypatch.setenv(secrets.SECRET_ENV, SECRET)
    with pytest.raises(SecretsError):
        load_alpaca()


def test_environment_overrides_keyring(
    ring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_alpaca(DEFAULT_ALPACA_SERVICE, PAPER_ID, SECRET)
    monkeypatch.setenv(secrets.KEY_ENV, "PKFROMENV")
    monkeypatch.setenv(secrets.SECRET_ENV, "env-secret")
    creds = require_alpaca()
    assert creds.key_id == "PKFROMENV"
    assert status(DEFAULT_ALPACA_SERVICE).env_override


def test_missing_keys_fail_closed_with_owner_action(ring: MemoryKeyring) -> None:
    with pytest.raises(SecretsError, match="trader secrets set alpaca-paper"):
        require_alpaca()


def test_unknown_names_are_refused() -> None:
    assert service_name("alpaca-paper") == "ai-stock-trader/alpaca-paper"
    assert service_name("ai-stock-trader/alpaca-paper") == "ai-stock-trader/alpaca-paper"
    assert service_name("capital-demo") == "ai-stock-trader/capital-demo"
    for unknown in ("alpaca-live", "capital-live", "ai-stock-trader/capital-live", "capital"):
        with pytest.raises(SecretsError):
            service_name(unknown)


def test_cli_set_status_delete_never_print_values(
    ring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = iter([PAPER_ID, SECRET])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    runner = CliRunner()
    out = runner.invoke(app, ["secrets", "set", "alpaca-paper"])
    assert out.exit_code == 0, out.output
    assert ring.store[(DEFAULT_ALPACA_SERVICE, "secret_key")] == SECRET
    out2 = runner.invoke(app, ["secrets", "status", "alpaca-paper"])
    assert out2.exit_code == 0 and "alpaca-paper: OK" in out2.output
    out3 = runner.invoke(app, ["secrets", "delete", "alpaca-paper"])
    assert out3.exit_code == 0 and "removed 2" in out3.output
    out4 = runner.invoke(app, ["secrets", "status", "alpaca-paper"])
    assert out4.exit_code == 1
    for o in (out, out2, out3, out4):
        assert SECRET not in o.output and PAPER_ID not in o.output


def test_cli_refuses_live_key(ring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter(["AKLIVEKEY", SECRET])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    out = CliRunner().invoke(app, ["secrets", "set", "alpaca-paper"])
    assert out.exit_code == 2
    assert ring.store == {}
    assert SECRET not in out.output


# --------------------------------------------------------------------------- profiles and keys folder
def test_profiles_switch_the_active_pair(ring: MemoryKeyring) -> None:
    store_alpaca(DEFAULT_ALPACA_SERVICE, "PKONE", "secret-one", profile="paper-a")
    store_alpaca(DEFAULT_ALPACA_SERVICE, "PKTWO", "secret-two", profile="paper-b", activate=False)
    assert secrets.profiles(DEFAULT_ALPACA_SERVICE) == (["paper-a", "paper-b"], "paper-a")
    assert require_alpaca().key_id == "PKONE"
    secrets.use_profile(DEFAULT_ALPACA_SERVICE, "paper-b")
    assert require_alpaca().key_id == "PKTWO"
    assert status(DEFAULT_ALPACA_SERVICE).active_profile == "paper-b"
    with pytest.raises(SecretsError, match="no stored profile"):
        secrets.use_profile(DEFAULT_ALPACA_SERVICE, "paper-c")
    with pytest.raises(SecretsError, match="invalid profile"):
        store_alpaca(DEFAULT_ALPACA_SERVICE, "PKX", "s", profile="Bad Name")
    assert secrets.delete(DEFAULT_ALPACA_SERVICE, "paper-b") == 4  # slot + active pair
    assert secrets.profiles(DEFAULT_ALPACA_SERVICE) == (["paper-a"], None)
    assert load_alpaca() is None


def test_key_folder_import_moves_files_into_the_keyring(
    ring: MemoryKeyring, tmp_path: Path
) -> None:
    d = secrets.ensure_keys_dir(tmp_path / "keys")
    assert (d / "README.txt").exists() and (d / "TEMPLATE.env.example").exists()
    (d / "paper-main.env").write_text(
        f"ALPACA_KEY={PAPER_ID}\nALPACA_SECRET={SECRET}\n"
        "ENDPOINT=https://paper-api.alpaca.markets/v2\n",
        encoding="utf-8",
    )
    (d / "Paper-2.env").write_text(
        'export APCA_API_KEY_ID="PKSECOND"\nAPCA_API_SECRET_KEY=other\n', encoding="utf-8"
    )
    imported = secrets.import_key_files(DEFAULT_ALPACA_SERVICE, d)
    assert [i.profile for i in imported] == ["paper-2", "paper-main"]
    assert not (d / "paper-main.env").exists() and not (d / "Paper-2.env").exists()
    assert (d / "TEMPLATE.env.example").exists()
    assert secrets.profiles(DEFAULT_ALPACA_SERVICE) == (["paper-2", "paper-main"], "paper-2")
    secrets.use_profile(DEFAULT_ALPACA_SERVICE, "paper-main")
    assert require_alpaca().headers()["APCA-API-SECRET-KEY"] == SECRET


def test_key_folder_refuses_live_keys_and_endpoints_without_partial_import(
    ring: MemoryKeyring, tmp_path: Path
) -> None:
    d = secrets.ensure_keys_dir(tmp_path / "keys")
    (d / "good.env").write_text(f"APCA_API_KEY_ID={PAPER_ID}\nAPCA_API_SECRET_KEY=x\n")
    (d / "live.env").write_text(
        f"APCA_API_KEY_ID={PAPER_ID}\nAPCA_API_SECRET_KEY={SECRET}\n"
        "APCA_API_BASE_URL=https://api.alpaca.markets\n"
    )
    with pytest.raises(SecretsError, match="not the paper endpoint") as exc:
        secrets.import_key_files(DEFAULT_ALPACA_SERVICE, d)
    assert SECRET not in str(exc.value)
    assert ring.store == {} and (d / "good.env").exists()  # validated before storing anything
    (d / "live.env").write_text("APCA_API_KEY_ID=AKLIVE\nAPCA_API_SECRET_KEY=x\n")
    with pytest.raises(SecretsError, match="LIVE"):
        secrets.import_key_files(DEFAULT_ALPACA_SERVICE, d)
    (d / "live.env").write_text("APCA_API_SECRET_KEY=only-a-secret\n")
    with pytest.raises(SecretsError, match="needs a key ID"):
        secrets.import_key_files(DEFAULT_ALPACA_SERVICE, d)
    assert ring.store == {}


def test_child_environment_forces_paper_and_carries_keys(ring: MemoryKeyring) -> None:
    store_alpaca(DEFAULT_ALPACA_SERVICE, PAPER_ID, SECRET)
    env = secrets.child_environment(DEFAULT_ALPACA_SERVICE, {"ALPACA_PAPER_TRADE": "false"})
    assert env["ALPACA_PAPER_TRADE"] == "true"
    assert env["APCA_API_BASE_URL"] == "https://paper-api.alpaca.markets"
    assert env["ALPACA_API_KEY"] == PAPER_ID and env["ALPACA_SECRET_KEY"] == SECRET


def test_cli_exec_runs_a_command_with_keys_but_prints_none(
    ring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import subprocess

    store_alpaca(DEFAULT_ALPACA_SERVICE, PAPER_ID, SECRET)
    monkeypatch.setenv(secrets.KEYS_DIR_ENV, str(tmp_path / "keys"))
    seen: dict[str, object] = {}

    def fake_run(command: list[str], env: dict[str, str], check: bool) -> object:
        seen.update(command=command, env=env)
        return subprocess.CompletedProcess(command, 7)

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = CliRunner().invoke(app, ["secrets", "exec", "alpaca-paper", "--", "tool", "--flag"])
    assert out.exit_code == 7
    assert seen["command"] == ["tool", "--flag"]
    env = seen["env"]
    assert isinstance(env, dict) and env["ALPACA_SECRET_KEY"] == SECRET
    assert env["ALPACA_PAPER_TRADE"] == "true"
    assert Path(env["SSL_CERT_FILE"]).read_text(encoding="ascii").startswith("-----BEGIN")
    assert SECRET not in out.output and PAPER_ID not in out.output


# --------------------------------------------------------------------------- plain services (ADR-0036)
CAPITAL = "ai-stock-trader/capital-demo"
CAP_KEY = "cap-demo-key-that-must-never-be-printed"
CAP_LOGIN = "owner@mailbox.test"
CAP_PASSWORD = " exact pass word "  # an API password is kept byte-exact


def _store_capital() -> None:
    secrets.store_plain(
        "capital-demo",
        {"api_key": f"  {CAP_KEY}\n", "identifier": CAP_LOGIN, "api_password": CAP_PASSWORD},
    )


def test_plain_service_round_trip_trims_except_the_password(ring: MemoryKeyring) -> None:
    _store_capital()
    assert ring.store[(CAPITAL, "api_key")] == CAP_KEY
    assert ring.store[(CAPITAL, "api_password")] == CAP_PASSWORD
    assert [(f.user, ok) for f, ok in secrets.plain_status(CAPITAL)] == [
        ("api_key", True),
        ("identifier", True),
        ("api_password", True),
    ]
    assert secrets.delete(CAPITAL) == 3
    assert ring.store == {}


def test_plain_service_store_is_all_or_nothing(ring: MemoryKeyring) -> None:
    with pytest.raises(SecretsError, match="nothing was stored"):
        secrets.store_plain(
            "capital-demo", {"api_key": CAP_KEY, "identifier": "  ", "api_password": CAP_PASSWORD}
        )
    assert ring.store == {}


def test_capital_child_environment_is_demo_only_with_bounded_trading(ring: MemoryKeyring) -> None:
    """No inherited CAP_* variable reaches the server: the account stays demo, only the practice
    epics trade, every order needs explicit confirmation and the day is capped (ADR-0044)."""
    _store_capital()
    parent = {
        "PATH": "kept",
        "CAP_ALLOW_TRADING": "true",
        "CAP_ENV": "true",  # capital-mcp reads the string "true" as LIVE
        "cap_dry_run": "false",  # pydantic-settings matches names case-insensitively
        "CAP_ALLOWED_EPICS": "ALL",
        "CAP_DEFAULT_ACCOUNT_ID": "some-live-account",
        "VIRTUAL_ENV": "C:/repo/.venv",
    }
    env = secrets.child_environment("capital-demo", parent)
    spec = secrets.SPECS["capital-demo"]
    assert env["PATH"] == "kept" and "VIRTUAL_ENV" not in env
    assert env["CAP_ENV"] == "demo"
    assert env["CAP_ALLOW_TRADING"] == "true" and env["CAP_DRY_RUN"] == "false"
    assert env["CAP_REQUIRE_EXPLICIT_CONFIRM"] == "true"
    epics = env["CAP_ALLOWED_EPICS"].split(",")
    # US30 and RTY since ADR-0056: the index dip trades the four indices of its trial
    cfd_sleeve = ["GOLD", "SILVER", "OIL_CRUDE", "US500", "US100", "DE40", "EURUSD", "US30", "RTY"]
    cfd_sleeve += ["COPPER", "OIL_BRENT", "HEATINGOIL", "NATURALGAS"]  # practice (ADR-0060)
    assert epics == [*cfd_sleeve, "BTCUSD", "ETHUSD"]  # the crypto sleeve's two (ADR-0049)
    assert all(e.upper() != "ALL" for e in epics)
    assert env["CAP_MAX_ORDERS_PER_DAY"] == "16" and env["CAP_MAX_OPEN_POSITIONS"] == "4"
    assert env["CAP_MAX_POSITION_SIZE"] == env["CAP_MAX_WORKING_ORDER_SIZE"] == "100"
    assert env["CAP_WS_ENABLED"] == "false" and "CAP_DEFAULT_ACCOUNT_ID" not in env
    assert env["CAP_API_KEY"] == CAP_KEY and env["CAP_API_PASSWORD"] == CAP_PASSWORD
    cap_vars = {k for k in env if k.upper().startswith("CAP_")}
    assert cap_vars == {f.env for f in spec.fields} | {k for k, _ in spec.forced_env}


def test_a_config_secret_ref_outside_the_registry_keeps_key_pair_behaviour(
    ring: MemoryKeyring,
) -> None:
    """A mode config's ``secret_ref`` may name another key-pair service (configs/*.yaml)."""
    other = "ai-stock-trader/team-paper"  # paper rules apply to names ending in -paper
    store_alpaca(other, PAPER_ID, SECRET)
    assert require_alpaca(other).key_id == PAPER_ID
    assert secrets.child_environment(other, {})["ALPACA_PAPER_TRADE"] == "true"
    assert secrets.delete(other) == 2


def test_capital_child_environment_fails_closed_when_incomplete(ring: MemoryKeyring) -> None:
    with pytest.raises(SecretsError, match="trader secrets set capital-demo"):
        secrets.child_environment("capital-demo", {})


def test_plain_service_has_no_profiles_or_keys_folder(ring: MemoryKeyring, tmp_path: Path) -> None:
    refused = (
        lambda: secrets.profiles(CAPITAL),
        lambda: secrets.use_profile(CAPITAL, "other"),
        lambda: secrets.store_alpaca(CAPITAL, "key", "secret"),
        lambda: secrets.load_alpaca(CAPITAL),
        lambda: secrets.status(CAPITAL),
        lambda: secrets.delete(CAPITAL, "other"),
        lambda: secrets.import_key_files(CAPITAL, tmp_path),
    )
    for call in refused:
        with pytest.raises(SecretsError, match="only available for alpaca-paper"):
            call()
    with pytest.raises(SecretsError):
        secrets.plain_status(DEFAULT_ALPACA_SERVICE)
    assert ring.store == {}


def test_cli_plain_service_never_prints_values(
    ring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import subprocess

    monkeypatch.setenv(secrets.KEYS_DIR_ENV, str(tmp_path / "keys"))
    answers = iter([CAP_KEY, CAP_LOGIN, CAP_PASSWORD])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    runner = CliRunner()
    outputs = [runner.invoke(app, ["secrets", "set", "capital-demo"])]
    assert outputs[-1].exit_code == 0, outputs[-1].output
    outputs.append(runner.invoke(app, ["secrets", "status", "capital-demo"]))
    assert outputs[-1].exit_code == 0 and "capital-demo: OK" in outputs[-1].output
    for args in (
        ["list", "capital-demo"],
        ["use", "capital-demo", "other"],
        ["import", "capital-demo"],
        ["status", "capital-demo", "--verify"],
        ["set", "capital-demo", "--profile", "other"],
        ["delete", "capital-demo", "--profile", "other"],
    ):
        outputs.append(runner.invoke(app, ["secrets", *args]))
        assert outputs[-1].exit_code == 2, args
    assert not (tmp_path / "keys").exists()  # refused before the keys folder was created

    seen: dict[str, object] = {}

    def fake_run(command: list[str], env: dict[str, str], check: bool) -> object:
        seen.update(command=command, env=env)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("CAP_ENV", "live")
    monkeypatch.setenv("CAP_ALLOWED_EPICS", "ALL")
    outputs.append(runner.invoke(app, ["secrets", "exec", "capital-demo", "--", "tool"]))
    assert outputs[-1].exit_code == 0
    env = seen["env"]
    assert isinstance(env, dict)
    assert env["CAP_ENV"] == "demo"
    assert env["CAP_ALLOWED_EPICS"] == ",".join(secrets.CAPITAL_DEMO_EPICS)
    assert env["CAP_API_PASSWORD"] == CAP_PASSWORD and "ALPACA_SECRET_KEY" not in env
    outputs.append(runner.invoke(app, ["secrets", "delete", "capital-demo"]))
    assert outputs[-1].exit_code == 0 and "removed 3" in outputs[-1].output
    outputs.append(runner.invoke(app, ["secrets", "status", "capital-demo"]))
    assert outputs[-1].exit_code == 1
    for o in outputs:
        assert CAP_KEY not in o.output and CAP_PASSWORD.strip() not in o.output


# --------------------------------------------------------------------------- keys in .env (ADR-0039)
AV_KEY = "AVKEY1234567890Z"


def test_dotenv_value_wins_over_the_keyring(ring: MemoryKeyring) -> None:
    ring.store[("ai-stock-trader/alphavantage", "api_key")] = "from-keyring"
    assert [src for _, src in secrets.plain_sources("alphavantage")] == ["keyring"]
    secrets.DOTENV_PATH.write_text(
        f'TRADER_PG_USER=trader\n# a comment\nALPHA_VANTAGE_API_KEY="{AV_KEY}"\n', encoding="utf-8"
    )
    assert [src for _, src in secrets.plain_sources("alphavantage")] == [".env"]
    env = secrets.child_environment(
        "alphavantage", {"PATH": "kept", "VIRTUAL_ENV": "C:/repo/.venv"}
    )
    assert env["ALPHA_VANTAGE_API_KEY"] == AV_KEY
    assert env["PATH"] == "kept" and "VIRTUAL_ENV" not in env


def test_an_empty_dotenv_line_falls_back_to_the_keyring(ring: MemoryKeyring) -> None:
    secrets.DOTENV_PATH.write_text("ALPHA_VANTAGE_API_KEY=\n", encoding="utf-8")
    with pytest.raises(SecretsError, match=r"ALPHA_VANTAGE_API_KEY=\.\.\. to the repository \.env"):
        secrets.child_environment("alphavantage", {})
    ring.store[("ai-stock-trader/alphavantage", "api_key")] = "from-keyring"
    assert secrets.child_environment("alphavantage", {})["ALPHA_VANTAGE_API_KEY"] == "from-keyring"


def test_capital_keys_from_dotenv_cannot_reach_live_or_widen_trading(ring: MemoryKeyring) -> None:
    """Only the three credential names are read from .env; the forced settings still win."""
    secrets.DOTENV_PATH.write_text(
        f"CAP_API_KEY={CAP_KEY}\nCAP_IDENTIFIER={CAP_LOGIN}\nCAP_API_PASSWORD=pw\n"
        "CAP_ENV=live\nCAP_ALLOWED_EPICS=ALL\nCAP_MAX_ORDERS_PER_DAY=1000\n",
        encoding="utf-8",
    )
    env = secrets.child_environment("capital-demo", {"CAP_ENV": "true"})
    assert env["CAP_API_KEY"] == CAP_KEY and env["CAP_API_PASSWORD"] == "pw"
    assert env["CAP_ENV"] == "demo" and env["CAP_MAX_ORDERS_PER_DAY"] == "16"
    assert env["CAP_ALLOWED_EPICS"] == ",".join(secrets.CAPITAL_DEMO_EPICS)


def test_cli_status_names_the_source_and_never_the_value(ring: MemoryKeyring) -> None:
    runner = CliRunner()
    missing = runner.invoke(app, ["secrets", "status", "alphavantage"])
    assert missing.exit_code == 1 and "ALPHA_VANTAGE_API_KEY=..." in missing.output
    secrets.DOTENV_PATH.write_text(f"ALPHA_VANTAGE_API_KEY={AV_KEY}\n", encoding="utf-8")
    ok = runner.invoke(app, ["secrets", "status", "alphavantage"])
    assert (
        ok.exit_code == 0 and "api_key from .env" in ok.output and "alphavantage: OK" in ok.output
    )
    for o in (missing, ok):
        assert AV_KEY not in o.output


# --------------------------------------------------------------------------- SEC contact (ADR-0047)
SEC_CONTACT = "owner@mailbox.test"


def test_the_sec_contact_comes_from_dotenv_and_is_never_printed(ring: MemoryKeyring) -> None:
    with pytest.raises(SecretsError, match=r"SEC_EDGAR_CONTACT=\.\.\. to the repository \.env"):
        secrets.require_sec_edgar_contact()
    secrets.DOTENV_PATH.write_text(f"SEC_EDGAR_CONTACT= {SEC_CONTACT} \n", encoding="utf-8")
    contact = secrets.require_sec_edgar_contact()
    assert contact.email == SEC_CONTACT
    assert SEC_CONTACT not in repr(contact) and SEC_CONTACT not in str(contact)
    status_out = CliRunner().invoke(app, ["secrets", "status", "sec-edgar"])
    assert status_out.exit_code == 0 and "contact from .env" in status_out.output
    assert SEC_CONTACT not in status_out.output


@pytest.mark.parametrize(
    "line",
    [
        "not-an-address",
        "Owner Name <owner@mailbox.test>",
        "owner@mailbox.test, other@mailbox.test",
        "owner@mailbox.test extra words",
        "owner@mailbox",
    ],
)
def test_the_sec_contact_must_be_one_plain_address(ring: MemoryKeyring, line: str) -> None:
    secrets.DOTENV_PATH.write_text(f"SEC_EDGAR_CONTACT={line}\n", encoding="utf-8")
    with pytest.raises(SecretsError) as caught:
        secrets.require_sec_edgar_contact()
    assert "one plain email address" in str(caught.value) and line not in str(caught.value)


def test_the_real_dotenv_is_the_repository_root_file() -> None:
    assert Path(__file__).resolve().parents[1] / ".env" == secrets.DOTENV_PATH


# --------------------------------------------------------------------------- MCP configuration
#: Alpaca MCP tools that change orders, positions or account state (alpaca-mcp-server 2.4.0).
ALPACA_STATE_CHANGING_TOOLS = (
    "place_stock_order",
    "place_crypto_order",
    "place_option_order",
    "replace_order_by_id",
    "cancel_order_by_id",
    "cancel_all_orders",
    "close_position",
    "close_all_positions",
    "exercise_options_position",
    "do_not_exercise_options_position",
    "update_account_config",
    "create_locate",
)


def test_alpaca_mcp_server_is_paper_pinned_and_keyless() -> None:
    import json

    root = Path(__file__).resolve().parents[1]
    raw = (root / ".mcp.json").read_text(encoding="utf-8")
    server = json.loads(raw)["mcpServers"]["alpaca"]
    args = server["args"]
    launcher = args.index("trader.config.secrets_exec")  # the light launcher (ADR-0038)
    assert args[launcher + 1] == "alpaca-paper"  # keys come from the keyring
    assert "alpaca-mcp-server==2.4.0" in args  # pinned, reviewed release (ADR-0032)
    assert server["env"]["ALPACA_PAPER_TRADE"] == "true"
    for word in ("PK", "AK", "SECRET_KEY=", "API_KEY="):
        assert word not in raw.replace("ALPACA_PAPER_TRADE", "")


def test_claude_cannot_trade_through_the_alpaca_mcp_server() -> None:
    """Orders outside the bot would break its reconciliation and bypass the risk engine."""
    import json

    root = Path(__file__).resolve().parents[1]
    deny = json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8"))[
        "permissions"
    ]["deny"]
    missing = [t for t in ALPACA_STATE_CHANGING_TOOLS if f"mcp__alpaca__{t}" not in deny]
    assert missing == []


def test_mcp_launcher_never_loads_the_market_data_or_database_stack() -> None:
    """The launcher lives as long as each Claude Code session, so it must stay light (ADR-0038)."""
    import subprocess
    import sys

    probe = (
        "import sys, trader.config.secrets_exec; "
        "print(','.join(sorted(m for m in ('pandas', 'numpy', 'sqlalchemy', 'typer') "
        "if m in sys.modules)))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "", f"heavy modules imported by the MCP launcher: {out}"


def test_mcp_launcher_refuses_a_missing_command(capsys: pytest.CaptureFixture[str]) -> None:
    from trader.config import secrets_exec

    assert secrets_exec.main([]) == 2
    assert secrets_exec.main(["alpaca-paper", "--"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""  # stdout belongs to the MCP server's JSON-RPC
    assert "usage: trader secrets exec" in captured.err
