"""
ai_generator.py
===============
Local LLM integration via LM Studio (OpenAI-compatible API).

LM Studio runs a local server at http://localhost:1234/v1 that exposes
the same API as OpenAI.  No API key is required — just load a model in
LM Studio and keep it running before clicking "Generate Configs".

Configuration (optional environment variables):
  LM_STUDIO_URL   - Base URL of the LM Studio server  (default: http://localhost:1234/v1)
  LM_STUDIO_MODEL - Model name as shown in LM Studio  (default: loaded-model)

Workflow:
  1. Read intent.json
  2. Build a system + user prompt
  3. POST to LM Studio's /v1/chat/completions
  4. Parse the JSON response → configs.json
  5. Handle parse errors with a regex fallback
"""

import json
import logging
import os
import re
import time

from openai import OpenAI, APIConnectionError, APIStatusError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CONFIGS_FILE = "configs.json"
INTENT_FILE = "intent.json"

LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://localhost:1234/v1")

# LM Studio uses the currently-loaded model regardless of what name you pass,
# but sending a recognisable name avoids some client-side warnings.
LM_STUDIO_MODEL = os.environ.get("LM_STUDIO_MODEL", "loaded-model")

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a senior Cisco IOS network engineer with deep expertise in enterprise LAN/WAN design.

Your task is to generate complete configurations for every device listed in the intent document.

STRICT OUTPUT RULES:
1. Return ONLY a single, valid JSON object. No markdown, no code fences, no prose, no comments.
2. The JSON object keys are device names exactly as they appear in the topology (e.g. "R1", "SW1", "PC1").
3. The value for each key is an ordered JSON array of command strings.
4. Do NOT include device prompts (e.g. "Router>") in command strings.
5. All VLAN names, interface names, and ACL names must match exactly what is in the intent.

DEVICE-SPECIFIC COMMAND FORMATS:

--- Cisco IOS Router (dynamips, c7200, c2691, etc.) ---
- Use router-on-a-stick (802.1Q subinterfaces) for inter-VLAN routing.
  - Create one subinterface per VLAN (e.g. GigabitEthernet0/0.10 for VLAN 10).
  - Set "encapsulation dot1Q <vlan-id>" on each subinterface.
  - Assign the gateway IP to each subinterface from the intent.
  - Physical parent interface: "no ip address" + "no shutdown".
- Create a named extended ACL if defined in intent; apply to the correct subinterface.
- Generate static routes ("ip route") or OSPF (process 1, area 0) per intent routing type.
- Config MUST start with "conf t" and end with "end" then "write memory".

--- Cisco IOS Switch (dynamips c2691 with NM-16ESW, etc.) ---
- Create each VLAN: "vlan <id>" then "name <name>".
- Trunk port toward router: "switchport mode trunk".
- Host access ports: "switchport mode access" + "switchport access vlan <id>".
- "no shutdown" all used interfaces.
- Config MUST start with "conf t" and end with "end" then "write memory".

--- VPCS Host Nodes (PC1, PC2, etc.) ---
IMPORTANT: VPCS is NOT Cisco IOS. It uses its own simple CLI. DO NOT use IOS commands.
VPCS commands:
  ip <ip_address>/<prefix_length> <default_gateway>
  save
Example for PC1 at 192.168.10.2/24, gateway 192.168.10.1:
  ["ip 192.168.10.2/24 192.168.10.1", "save"]
Do NOT use "conf t", "interface", "no shutdown", or any IOS syntax for VPCS nodes.

Return ONLY the raw JSON object with ALL devices included. No other text whatsoever.
"""



def _build_messages(intent: dict, retry_context: str = "") -> list[dict]:
    """Build the chat messages list for the LM Studio API call."""
    user_content = (
        "Generate Cisco IOS CLI configurations for all devices based on the following "
        "network intent document.\n\n"
        f"INTENT DOCUMENT:\n{json.dumps(intent, indent=2)}\n\n"
        "Remember: return ONLY the JSON object — no markdown, no extra text."
    )
    if retry_context:
        user_content += (
            "\n\nVALIDATION FAILURE CONTEXT (delta fix required):\n"
            + retry_context
            + "\n\nGenerate ONLY the corrective commands needed to fix the failures above. "
            "Still return the full JSON structure with device names as keys."
        )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": user_content},
    ]


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> dict:
    """
    Try to parse text as JSON, with progressive fallbacks.
    Raises ConfigGenerationError if all attempts fail.
    """
    # Attempt 1: direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Attempt 2: strip markdown code fences and retry
    stripped = re.sub(r"```(?:json)?", "", text).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # Attempt 3: find the outermost { ... } block
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start: end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise ConfigGenerationError(
                "LLM response contained no valid JSON object.\n"
                f"Raw response (first 500 chars): {text[:500]}"
            ) from exc

    raise ConfigGenerationError(
        "LLM response contains no JSON object at all.\n"
        f"Raw response (first 500 chars): {text[:500]}"
    )


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class ConfigGenerationError(Exception):
    """Raised when the LLM response cannot be parsed into a valid configs dict."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_configs(intent: dict, retry_context: str = "") -> dict[str, list[str]]:
    """
    Call the local LM Studio model and return a configs dict.

    Args:
        intent:        The full intent dict (from intent.json).
        retry_context: Optional extra context for closed-loop retry.

    Returns:
        dict mapping device names to ordered lists of CLI commands.

    Raises:
        ConfigGenerationError: If the LLM output cannot be parsed as valid JSON.
        ConnectionError: If LM Studio is not running or not reachable.
    """
    client = OpenAI(
        base_url=LM_STUDIO_URL,
        api_key="lm-studio",   # LM Studio ignores this value; any non-empty string works
    )

    messages = _build_messages(intent, retry_context)
    logger.info(
        "Calling local LLM via LM Studio at %s (model=%s) ...",
        LM_STUDIO_URL,
        LM_STUDIO_MODEL,
    )

    # Retry up to 3 times on transient errors
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=LM_STUDIO_MODEL,
                messages=messages,
                temperature=0.1,       # low temp for deterministic CLI output
                max_tokens=4096,
            )
            break  # success — exit retry loop

        except APIConnectionError as exc:
            raise ConnectionError(
                f"Cannot reach LM Studio at {LM_STUDIO_URL}. "
                "Is LM Studio running with a model loaded? "
                "Start LM Studio → load a model → enable the local server (port 1234)."
            ) from exc

        except APIStatusError as exc:
            if exc.status_code in (429, 500, 503):
                wait = 2 ** attempt
                logger.warning(
                    "LM Studio returned %d (attempt %d/%d). Waiting %ds ...",
                    exc.status_code, attempt, max_retries, wait,
                )
                if attempt == max_retries:
                    raise
                time.sleep(wait)
            else:
                raise

    raw_text = response.choices[0].message.content or ""
    logger.info(
        "LLM responded. Finish reason: %s. Tokens: prompt=%s completion=%s.",
        response.choices[0].finish_reason,
        response.usage.prompt_tokens if response.usage else "?",
        response.usage.completion_tokens if response.usage else "?",
    )
    logger.debug("Raw LLM output:\n%s", raw_text)

    configs = _extract_json(raw_text)
    logger.info(
        "Configs parsed for %d device(s): %s",
        len(configs),
        list(configs.keys()),
    )

    # Validate: each value must be a list
    for device, cmds in configs.items():
        if not isinstance(cmds, list):
            raise ConfigGenerationError(
                f"Expected a list of commands for '{device}', got {type(cmds).__name__}."
            )
        configs[device] = [str(c) for c in cmds]

    return configs


def generate_delta_fix(
    intent: dict,
    validation_failures: list[dict],
) -> dict[str, list[str]]:
    """Ask the local LLM for corrective commands to fix validation failures."""
    context_lines = ["The following validation checks FAILED after deployment:\n"]
    for f in validation_failures:
        context_lines.append(
            f"  - [{f.get('device', '?')}] {f.get('check', '?')}: "
            f"EXPECTED={f.get('expected', '?')} | ACTUAL={f.get('actual', '?')}"
        )
    context_lines.append("\nProvide only the minimal corrective IOS commands.")
    context = "\n".join(context_lines)

    logger.info("Requesting delta fix for %d failure(s) ...", len(validation_failures))
    return generate_configs(intent, retry_context=context)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save_configs(configs: dict, path: str = CONFIGS_FILE) -> None:
    """Write configs dict to configs.json (pretty-printed)."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(configs, fh, indent=2)
    logger.info("Configs saved to %s", path)


def load_configs(path: str = CONFIGS_FILE) -> dict:
    """Load and return configs from configs.json."""
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"'{path}' not found. Run config generation first."
        )
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
