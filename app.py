"""
app.py
======
Flask application — main entry point for the AI Network Config System.

Routes
------
  GET  /                    Serve the browser UI (index.html)
  GET  /api/projects        List all GNS3 projects
  POST /api/discover        Topology discovery (requires project_id in body)
  GET  /api/topology        Return topology.json contents
  POST /api/intent          Submit intent wizard form data → saves intent.json
  GET  /api/intent          Return intent.json contents
  POST /api/generate        Call Local LLM → saves configs.json
  GET  /api/configs         Return configs.json contents
  POST /api/deploy          via Netmiko → saves deploy_logs.json
  GET  /api/deploy          Return deploy_logs.json contents
  POST /api/validate        Run validation → saves validation.json
  GET  /api/validation      Return validation.json contents
  POST /api/retry           Trigger closed-loop retry (max 2 attempts)
  GET  /api/status          Return current pipeline state

Run with:
  python app.py
"""

import json
import logging
import os
import sys
from functools import wraps
from typing import Any

from flask import Flask, jsonify, render_template, request

# ---------------------------------------------------------------------------
# Logging — configure before importing sub-modules so their loggers propagate
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("app")

# ---------------------------------------------------------------------------
# Import project modules (after logging is configured)
# ---------------------------------------------------------------------------
import gns3_client
import intent_wizard
import ai_generator
import deployer
import validator

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False   # preserve insertion order in JSON responses

# ---------------------------------------------------------------------------
# Pipeline state tracker (in-memory; also written to pipeline_state.json)
# ---------------------------------------------------------------------------
PIPELINE_STATE_FILE = "pipeline_state.json"

PIPELINE_STEPS = [
    "topology_discovered",
    "intent_collected",
    "configs_generated",
    "deployed",
    "validated",
]


def _load_state() -> dict:
    """Load pipeline state from disk, or return a fresh state dict."""
    if os.path.exists(PIPELINE_STATE_FILE):
        try:
            with open(PIPELINE_STATE_FILE, encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            pass
    return {step: False for step in PIPELINE_STEPS}


def _save_state(state: dict) -> None:
    with open(PIPELINE_STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


pipeline_state = _load_state()


def _mark_step(step: str, done: bool = True) -> None:
    """Mark a pipeline step complete (or reset it)."""
    pipeline_state[step] = done
    _save_state(pipeline_state)


# ---------------------------------------------------------------------------
# Error response helper
# ---------------------------------------------------------------------------

def _err(message: str, status: int = 400) -> tuple:
    logger.error("API error (%d): %s", status, message)
    return jsonify({"success": False, "error": message}), status


def _ok(data: Any = None, message: str = "OK") -> tuple:
    payload = {"success": True, "message": message}
    if data is not None:
        payload["data"] = data
    return jsonify(payload), 200


# ---------------------------------------------------------------------------
# Routes — UI
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Serve the main browser UI."""
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Routes — Pipeline Status
# ---------------------------------------------------------------------------

@app.route("/api/status", methods=["GET"])
def api_status():
    """Return the current pipeline step completion state."""
    return _ok(pipeline_state, "Pipeline status retrieved")


# ---------------------------------------------------------------------------
# Routes — Step 1: Topology Discovery
# ---------------------------------------------------------------------------

@app.route("/api/projects", methods=["GET"])
def api_projects():
    """List all GNS3 projects."""
    try:
        projects = gns3_client.get_projects()
        return _ok(projects, f"Found {len(projects)} project(s)")
    except ConnectionError as exc:
        return _err(str(exc), 503)
    except Exception as exc:
        logger.exception("Unexpected error listing projects.")
        return _err(f"Unexpected error: {exc}", 500)


@app.route("/api/discover", methods=["POST"])
def api_discover():
    """
    Discover topology for the given project_id.
    Body (JSON): {"project_id": "<uuid>"}
    If project_id is omitted, auto-selects the currently opened project.
    """
    body = request.get_json(silent=True) or {}
    project_id = body.get("project_id")

    try:
        if not project_id:
            active = gns3_client.get_active_project()
            if not active:
                return _err(
                    "No open GNS3 project found. Please open a project in GNS3 first "
                    "or provide 'project_id' in the request body.",
                    404,
                )
            project_id = active["project_id"]
            logger.info("Auto-selected project: %s", project_id)

        topology = gns3_client.discover_topology(project_id)
        gns3_client.save_topology(topology)
        _mark_step("topology_discovered")
        # Reset downstream steps when topology changes
        for step in ["intent_collected", "configs_generated", "deployed", "validated"]:
            _mark_step(step, False)

        return _ok(topology, "Topology discovered and saved to topology.json")

    except ConnectionError as exc:
        return _err(str(exc), 503)
    except RuntimeError as exc:
        return _err(str(exc), 400)
    except Exception as exc:
        logger.exception("Unexpected error during topology discovery.")
        return _err(f"Unexpected error: {exc}", 500)


@app.route("/api/topology", methods=["GET"])
def api_topology():
    """Return the contents of topology.json."""
    try:
        topology = gns3_client.load_topology()
        return _ok(topology)
    except FileNotFoundError as exc:
        return _err(str(exc), 404)


# ---------------------------------------------------------------------------
# Routes — Step 2: Intent Collection
# ---------------------------------------------------------------------------

@app.route("/api/intent", methods=["POST"])
def api_submit_intent():
    """
    Accept intent form data from the wizard.
    Body (JSON): full intent object with vlans, ip_plan, acl, routing, constraints.
    """
    form_data = request.get_json(silent=True)
    if not form_data:
        return _err("Request body must be valid JSON.", 400)

    if not pipeline_state.get("topology_discovered"):
        return _err("Topology has not been discovered yet. Run topology discovery first.", 400)

    try:
        topology = gns3_client.load_topology()
        intent = intent_wizard.build_intent(form_data, topology)
        errors = intent_wizard.validate_intent(intent)
        if errors:
            return jsonify({
                "success": False,
                "error": "Intent validation failed",
                "validation_errors": errors,
            }), 422

        intent_wizard.save_intent(intent)
        _mark_step("intent_collected")
        for step in ["configs_generated", "deployed", "validated"]:
            _mark_step(step, False)

        return _ok(intent, "Intent saved to intent.json")

    except FileNotFoundError as exc:
        return _err(str(exc), 404)
    except Exception as exc:
        logger.exception("Unexpected error processing intent.")
        return _err(f"Unexpected error: {exc}", 500)


@app.route("/api/intent", methods=["GET"])
def api_get_intent():
    """Return the contents of intent.json."""
    try:
        intent = intent_wizard.load_intent()
        return _ok(intent)
    except FileNotFoundError as exc:
        return _err(str(exc), 404)


# ---------------------------------------------------------------------------
# Routes — Step 3: AI Config Generation
# ---------------------------------------------------------------------------

@app.route("/api/generate", methods=["POST"])
def api_generate():
    """Call Claude Sonnet to generate Cisco IOS configs from intent.json."""
    if not pipeline_state.get("intent_collected"):
        return _err("Intent has not been collected yet. Complete the intent wizard first.", 400)

    try:
        intent = intent_wizard.load_intent()
        configs = ai_generator.generate_configs(intent)
        ai_generator.save_configs(configs)
        _mark_step("configs_generated")
        for step in ["deployed", "validated"]:
            _mark_step(step, False)

        return _ok(configs, f"Configs generated for {list(configs.keys())}")

    except EnvironmentError as exc:
        return _err(str(exc), 500)
    except ai_generator.ConfigGenerationError as exc:
        return _err(f"Config generation failed: {exc}", 500)
    except FileNotFoundError as exc:
        return _err(str(exc), 404)
    except Exception as exc:
        logger.exception("Unexpected error during config generation.")
        return _err(f"Unexpected error: {exc}", 500)


@app.route("/api/configs", methods=["GET"])
def api_configs():
    """Return the contents of configs.json."""
    try:
        configs = ai_generator.load_configs()
        return _ok(configs)
    except FileNotFoundError as exc:
        return _err(str(exc), 404)


# ---------------------------------------------------------------------------
# Routes — Step 4: Deployment
# ---------------------------------------------------------------------------

@app.route("/api/deploy", methods=["POST"])
def api_deploy():
    """Deploy configs from configs.json to all devices via Netmiko Telnet."""
    if not pipeline_state.get("configs_generated"):
        return _err("Configs have not been generated yet. Run config generation first.", 400)

    try:
        configs = ai_generator.load_configs()
        topology = gns3_client.load_topology()

        logs = deployer.deploy_all(configs, topology)
        deployer.save_deploy_logs(logs)

        all_ok = all(r["status"] == "success" for r in logs)
        _mark_step("deployed", all_ok)
        _mark_step("validated", False)

        return _ok(
            logs,
            "Deployment complete — all succeeded" if all_ok
            else "Deployment complete with some failures",
        )

    except FileNotFoundError as exc:
        return _err(str(exc), 404)
    except Exception as exc:
        logger.exception("Unexpected error during deployment.")
        return _err(f"Unexpected error: {exc}", 500)


@app.route("/api/deploy", methods=["GET"])
def api_deploy_logs():
    """Return the contents of deploy_logs.json."""
    try:
        logs = deployer.load_deploy_logs()
        return _ok(logs)
    except FileNotFoundError as exc:
        return _err(str(exc), 404)


# ---------------------------------------------------------------------------
# Routes — Step 5: Validation
# ---------------------------------------------------------------------------

@app.route("/api/validate", methods=["POST"])
def api_validate():
    """Run validation checks against all deployed devices."""
    if not pipeline_state.get("deployed"):
        return _err(
            "Deployment has not completed successfully. Run deployment first.", 400
        )

    try:
        intent = intent_wizard.load_intent()
        topology = gns3_client.load_topology()

        results = validator.validate_all(intent, topology)
        validator.save_validation(results)

        _mark_step("validated", results["passed"])

        return _ok(results, "Validation complete")

    except FileNotFoundError as exc:
        return _err(str(exc), 404)
    except Exception as exc:
        logger.exception("Unexpected error during validation.")
        return _err(f"Unexpected error: {exc}", 500)


@app.route("/api/validation", methods=["GET"])
def api_validation():
    """Return the contents of validation.json."""
    try:
        results = validator.load_validation()
        return _ok(results)
    except FileNotFoundError as exc:
        return _err(str(exc), 404)


@app.route("/api/retry", methods=["POST"])
def api_retry():
    """
    Trigger closed-loop retry: send failures to Claude, re-deploy, re-validate.
    Body (JSON): {"attempt": 1}  (1 or 2, max)
    """
    body = request.get_json(silent=True) or {}
    attempt = int(body.get("attempt", 1))

    if attempt > validator.MAX_RETRY_ATTEMPTS:
        return _err(
            f"Maximum retry attempts ({validator.MAX_RETRY_ATTEMPTS}) reached.", 400
        )

    try:
        intent = intent_wizard.load_intent()
        topology = gns3_client.load_topology()

        results = validator.closed_loop_retry(intent, topology, attempt=attempt)
        _mark_step("validated", results.get("passed", False))

        return _ok(results, f"Retry attempt {attempt} complete")

    except FileNotFoundError as exc:
        return _err(str(exc), 404)
    except Exception as exc:
        logger.exception("Unexpected error during retry.")
        return _err(f"Unexpected error: {exc}", 500)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Check for Anthropic API key at startup (warn if missing)
    lm_url = os.environ.get("LM_STUDIO_URL", "http://localhost:1234/v1")
    logger.info(
        "ℹ️  Using local LLM via LM Studio at %s — no API key required. "
        "Make sure LM Studio is running with a model loaded before clicking Generate.",
        lm_url,
    )

    logger.info("=" * 60)
    logger.info("  AI Network Config System — Stage 2 MVP")
    logger.info("  Open your browser at: http://127.0.0.1:5050")
    logger.info("  GNS3 API expected at: http://localhost:3080/v2")
    logger.info("=" * 60)

    app.run(host="127.0.0.1", port=5050, debug=False)
