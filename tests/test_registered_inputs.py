"""Inputs of registered research trials are frozen (risk_and_validation.md §5).

A registration binds a fee schedule by id and file path and does not bind the risk policy or the
instrument-spec contents (review finding 2026-11-08). Editing one of these files would silently
change what an already registered trial means while ``require_registered`` still accepts it.
So their contents are pinned here. To change costs, policy or instrument metadata, create a NEW
file (new schedule id / version) and register NEW trials; the paper runtime gets its own
instrument file rather than editing the research one.

Hashes are of the bytes with CRLF normalised to LF (``.gitattributes`` stores LF).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: file -> (sha256, registered trials that depend on it)
REGISTERED_INPUTS: dict[str, tuple[str, str]] = {
    "configs/fees/kraken_spot_tier1_usd.yaml": (
        "1c05dba643f3b22b3b6bbf23eafa416247433a8b2680f8029e3942800fc90ddf",
        "T-0001..T-0005 crypto_baseline_v1, T-0016 crypto_daily_v1",
    ),
    "configs/instruments/kraken_spot_usd.yaml": (
        "998768b3366a8600aaa559edf987f9ab4de5ebb205b937f9bc87aa1a34c6c493",
        "T-0001..T-0005 crypto_baseline_v1, T-0016 crypto_daily_v1",
    ),
    "configs/research/crypto_baseline_v1.yaml": (
        "127aad959b5a745d07a9742eabb1f08a4ace49d4fc0d59272337d7e0257b1394",
        "T-0001..T-0005 crypto_baseline_v1",
    ),
    "configs/fees/alpaca_us_equities_v2.yaml": (
        "cca9078ab337c9feadc39bb5b712f794961a900eda5812fe03759e0233c1bfb7",
        "T-0006..T-0010 stocks_baseline_v1, T-0013..T-0015 and T-0017 ETF studies",
    ),
    "configs/instruments/etfs_research.yaml": (
        "43b8e9f3f1dd80efda2d75fea471e3cb200fb2df6d3c59310cddf1342e28298a",
        "T-0013, T-0014 etf_daily_v1; T-0015 etf_trend_sized_v1; T-0017 etf_dip_sized_v1",
    ),
    "configs/research/etf_daily_v1.yaml": (
        "c60da776a8b430baffe25790bab364e5a89dccf72cb33dc97132f1a2894a147b",
        "T-0013, T-0014 etf_daily_v1",
    ),
    "configs/research/etf_trend_sized_v1.yaml": (
        "e6d631bbb8da873e1c863b8ba746270176f3214eb7bec3a0628152f09c1365d3",
        "T-0015 etf_trend_sized_v1",
    ),
    "configs/policy/trend_sized_v1.yaml": (
        "6040d1c366213013e9821fe178a29dd9c433dba82324e2a0d4e1e01b522b7b28",
        "T-0015 etf_trend_sized_v1, T-0016 crypto_daily_v1, T-0017 etf_dip_sized_v1 "
        "(research-only variant, ADR-0041)",
    ),
    "configs/research/etf_dip_sized_v1.yaml": (
        "9a8d9c6f487e673fe0c3ac2166afe6839634c723b13e07c95cb64c68e21f31e6",
        "T-0017 etf_dip_sized_v1",
    ),
    "configs/research/crypto_daily_v1.yaml": (
        "3003fd66af74b29a6aea61a0e1a8f991f19c9e8b5c18c531e668fcadfc3a2880",
        "T-0016 crypto_daily_v1",
    ),
    "configs/instruments/stocks_research.yaml": (
        "20f5a34f4b0647525e196b01a3834be990773cd55948f239869d5ec10ab6cb9a",
        "T-0006..T-0010 stocks_baseline_v1",
    ),
    "configs/research/stocks_baseline_v1.yaml": (
        "abbc56f0fe59cc65556d4d003c3c2adcaaa74507c076917f99ff54fb9322ba4d",
        "T-0006..T-0010 stocks_baseline_v1",
    ),
    "configs/policy/base.yaml": (
        "0ebe880ff22935766e4e72c9e2cb62fa7e20446f1334b1b09b76962d99a7836d",
        "T-0001..T-0014 (T-0015..T-0017 use trend_sized_v1)",
    ),
    # ADR-0052: the CFD studies (registered 2026-11-11)
    "configs/instruments/capital_demo_cfds_v1.yaml": (
        "84f04833ce60d4da434edfb0a2872a62db3df4e9d55941cc794e21c69caf8373",
        "T-0018 cfd_index_dip_v1, T-0019 cfd_crypto_momentum_v1",
    ),
    "configs/fees/capital_com_cfd_demo_v1.yaml": (
        "f9d746c9e0b021ad7df622bac934570c84cc83941200b1d86e3cdcdfc4befc82",
        "T-0018 cfd_index_dip_v1, T-0019 cfd_crypto_momentum_v1",
    ),
    "configs/policy/cfd_sleeve_v1.yaml": (
        "2dd59d3f89b00403d0010bdf5fe3ba263bd47cca9ebb4b87987ac3be540b12c2",
        "T-0018 cfd_index_dip_v1",
    ),
    "configs/policy/crypto_cfd_sleeve_v1.yaml": (
        "1ab522a510cfaf399a1de26d4def39fc1bfc641b768ca8bc6a0518db18e3161e",
        "T-0019 cfd_crypto_momentum_v1",
    ),
    "configs/research/cfd_index_dip_v1.yaml": (
        "7874f976b500612585c243d818a8617444b9dba06a0e483edf9bf8ba6d789cbc",
        "T-0018 cfd_index_dip_v1",
    ),
    "configs/research/cfd_crypto_momentum_v1.yaml": (
        "1808423960eb2ff6146375fb063ddb1dd904012ab6a04cd2e60988f8aba64482",
        "T-0019 cfd_crypto_momentum_v1",
    ),
    "src/trader/strategies/specs/cfd_index_dip_v1.yaml": (
        "fd016ac33cebb51e2f7259615c6d3894eaf2321a45b5cf6b31b20fcadf79df13",
        "T-0018 cfd_index_dip_v1",
    ),
    "src/trader/strategies/specs/cfd_crypto_weekly_momentum_v1.yaml": (
        "ee6c22559d12631c715d10872cf3ce9fb6692524cc1f3a966de80a1d1cc83d21",
        "T-0019 cfd_crypto_momentum_v1",
    ),
    # ADR-0055: the hourly channel trials also read the instrument specs, the fee schedule and
    # the two sleeve policies pinned above for T-0018 and T-0019.
    "configs/research/cfd_hourly_channel_v1.yaml": (
        "0a4ac9021ed66e4f103b2e7acc15b375dd0bcaf8d901970339bd63c326eca16f",
        "T-0021, T-0022 cfd_hourly_channel_v1",
    ),
    "configs/research/cfd_crypto_hourly_channel_v1.yaml": (
        "cbde1562d6fa3b7c9450ee7267bbaef607570e46eedbd683c30d5a1975ccd089",
        "T-0023, T-0024 cfd_crypto_hourly_channel_v1",
    ),
    "src/trader/strategies/specs/cfd_hourly_breakout_v1.yaml": (
        "be64b84ed1fa26c9963591d3012ed6e26c054d3a46cff53c2f58830994873a53",
        "T-0021 cfd_hourly_channel_v1",
    ),
    "src/trader/strategies/specs/cfd_hourly_fade_v1.yaml": (
        "0f718df2bf2617d1ddc9d12796d9d8e031782c3b52b5ee957e527248c997ab1b",
        "T-0022 cfd_hourly_channel_v1",
    ),
    "src/trader/strategies/specs/cfd_crypto_hourly_breakout_v1.yaml": (
        "780bd719dec03ab5a56120ea025d0110209465da6a935fdca3a928ce2ded596d",
        "T-0023 cfd_crypto_hourly_channel_v1",
    ),
    "src/trader/strategies/specs/cfd_crypto_hourly_fade_v1.yaml": (
        "fd21b44981b7a41ce31e7e8f426dbc619c9b374416009723c74fd2c5cd89752b",
        "T-0024 cfd_crypto_hourly_channel_v1",
    ),
    # ADR-0058: T-0025 also reads the instrument specs, the fee schedule and the CFD sleeve's
    # policy pinned above.
    "configs/research/cfd_weekly_momentum_v1.yaml": (
        "cf7c8cd68ddfc69279c581f5cc68630aab7745885247757af48610444e6db267",
        "T-0025 cfd_weekly_momentum_v1",
    ),
    "src/trader/strategies/specs/cfd_weekly_momentum_v1.yaml": (
        "1067152460133c021a70d009695ff274e754f78acf6f57af420577f2f4c8aea0",
        "T-0025 cfd_weekly_momentum_v1",
    ),
    # ADR-0059: T-0026 also binds T-0023's study and rule files, pinned above.
    "configs/research/cfd_crypto_breakout_filter_v1.yaml": (
        "80e8ace033a9736a3ec919eb1de3b5e9da147f6d2799013e8a0e2ab97449bad1",
        "T-0026 cfd_crypto_breakout_filter_v1",
    ),
}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


@pytest.mark.parametrize("rel", sorted(REGISTERED_INPUTS))
def test_registered_trial_inputs_are_unchanged(rel: str) -> None:
    expected, trials = REGISTERED_INPUTS[rel]
    assert _digest(ROOT / rel) == expected, (
        f"{rel} is an input of registered trials ({trials}). Do not edit it: create a new "
        "versioned file and register new trials (risk_and_validation.md section 5)."
    )
