# Third-Party Licenses and Attributions

`llm-server-cockpit` is licensed under the [MIT License](LICENSE).

This project incorporates, depends upon, or interfaces with third-party software, libraries, and external tools. Below is an overview of their respective licenses and attribution notices.

---

## 1. Python Dependencies

| Package | License | Copyright / Upstream Project |
| :--- | :--- | :--- |
| **PyYAML** | MIT | Copyright (c) 2017-2020 Ingy döt Net, Copyright (c) 2006-2016 Kirill Simonov |
| **textual** | MIT | Copyright (c) 2021 Textualize, Inc. |

---

## 2. Upstream Concept & Architecture Credit

### `kyuz0/ai-toolbox-cockpit`
- **Repository:** [https://github.com/kyuz0/ai-toolbox-cockpit](https://github.com/kyuz0/ai-toolbox-cockpit)
- **Author:** kyuz0 / Jesper
- **Attribution Notice:**
  This project draws its core TUI architecture concept, Textual screen patterns, and hardware cockpit operational paradigms from `kyuz0/ai-toolbox-cockpit` by Jesper (`kyuz0`). While `llm-server-cockpit` is a clean-room, declarative implementation designed for bare-metal Linux hosts with multi-backend hardware acceleration, the user interface structure, screen workflow paradigms, and operator cockpit concept are directly inspired by Jesper's work. Sincere credit and appreciation are extended to the author for pioneering this interactive approach to local LLM server management.

---

## 3. Inference Infrastructure & Managed Binaries

The provisioning workflows in this repository compile, install, or interface with the following inference components:

### `llama.cpp`
- **License:** [MIT License](https://github.com/ggerganov/llama.cpp/blob/master/LICENSE)
- **Copyright:** Copyright (c) 2023-2026 Georgi Gerganov and contributors
- **Role:** High-performance LLM inference engine built from source per GPU backend (CUDA, ROCm, Vulkan, SYCL).

### `llama-swap`
- **License:** [MIT License](https://github.com/mostlygeek/llama-swap/blob/main/LICENSE)
- **Copyright:** Copyright (c) 2024 mostlygeek
- **Role:** Dynamic model swapping reverse proxy and process manager for llama.cpp servers.

### `huggingface_hub`
- **License:** [Apache License 2.0](https://github.com/huggingface/huggingface_hub/blob/main/LICENSE)
- **Copyright:** Copyright 2020-The HuggingFace team.
- **Role:** CLI and client library for authenticated declarative model snapshot downloads.

---

## 4. System Prerequisites (External Tools)

The following tools are external operating system binaries and utilities invoked out-of-process via standard operating system execution boundaries (`subprocess` / `exec`). They are NOT bundled, linked, or distributed within this repository:

- **`systemd`** ([LGPL v2.1+](https://www.gnu.org/licenses/old-licenses/lgpl-2.1.html)): Linux system and service manager used for supervising `llama-swap`, timers, and host services.
- **`ethtool`** ([GPL v2.0](https://www.gnu.org/licenses/old-licenses/gpl-2.0.html)): Network driver configuration utility invoked out-of-process to inspect and configure Wake-on-LAN (WoL) hardware status.
- **`iproute2`** (`ip`) ([GPL v2.0](https://www.gnu.org/licenses/old-licenses/gpl-2.0.html)): Linux networking toolkit invoked out-of-process to query network interfaces, IP addresses, and routing tables.
- **`pciutils`** (`lspci`) ([GPL v2.0](https://www.gnu.org/licenses/old-licenses/gpl-2.0.html)): PCI bus diagnostic utility invoked out-of-process to detect and enumerate installed GPU hardware accelerators.
