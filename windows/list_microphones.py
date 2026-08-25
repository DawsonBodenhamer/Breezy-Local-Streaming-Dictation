"""List one dynamic MME representative for each physical Windows input."""

from __future__ import annotations

import sounddevice as sd

_SYSTEM_ALIASES = {
    "microsoft sound mapper - input",
    "primary sound capture driver",
}


def main() -> None:
    devices = sd.query_devices()
    host_apis = sd.query_hostapis()
    mme_indexes = {
        index
        for index, host_api in enumerate(host_apis)
        if str(host_api["name"]).casefold() == "mme"
    }

    candidates: list[tuple[int, str]] = []
    for index, device in enumerate(devices):
        if int(device["max_input_channels"]) <= 0:
            continue
        if mme_indexes and int(device["hostapi"]) not in mme_indexes:
            continue

        name = " ".join(str(device["name"]).split())
        if not name or name.casefold() in _SYSTEM_ALIASES:
            continue
        candidates.append((index, name))

    for index, name in candidates:
        print(f"{index}\t{name}")


if __name__ == "__main__":
    main()
