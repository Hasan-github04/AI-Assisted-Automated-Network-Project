# AI-Assisted Network Configuration System — NSSA443 Stage 2 MVP

An end-to-end pipeline that discovers GNS3 topologies, collects network intent via a browser UI, generates Cisco IOS configurations using Claude Sonnet AI, deploys them via Netmiko Telnet, and validates the results with closed-loop AI remediation.

---

## Prerequisites

| Requirement | Details |
|---|---|
| Python | 3.10 or later |
| GNS3 | Installed and running locally, REST API at `http://localhost:3080` |
| GNS3 VM | Running (required for device emulation) |
| Anthropic API Key | Get one at [console.anthropic.com](https://console.anthropic.com) |
| GNS3 Topology | Manually build: 1 router (c7200) + 1 switch (c2960) + 2 host nodes |

---

## Installation

### 1. Open a PowerShell terminal in the project folder

```powershell
cd C:\Users\Ahmed\Desktop\NSSA443
```

### 2. Create and activate a Python virtual environment

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

> If you get a script execution policy error, run:
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```

### 3. Install dependencies

```powershell
pip install -r requirements.txt
```

---

## Configuration

### Set the Anthropic API Key

**PowerShell (temporary — current session only):**
```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-your-key-here"
```

**Permanent (Windows environment variable):**
```powershell
[System.Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "sk-ant-your-key-here", "User")
```

### Optional — Change GNS3 URL

If GNS3 listens on a different address:
```powershell
$env:GNS3_URL = "http://localhost:3080/v2"
```

---

## Preparing GNS3

Before running the system:

1. **Start GNS3** and ensure the GNS3 VM is running.
2. **Create a new project** (e.g., `NSSA443-MVP`).
3. **Add devices manually** to the canvas:
   - 1 × Cisco c7200 router — name it `R1`
   - 1 × Cisco c2960 switch — name it `SW1`
   - 2 × VPCS or host nodes — name them `PC1`, `PC2`
4. **Connect links:**
   - R1 `Gi0/0` → SW1 `Fa0/24` (trunk)
   - SW1 `Fa0/1` → PC1
   - SW1 `Fa0/2` → PC2
5. **Start all devices** (right-click → Start all nodes).
6. Wait ~60 seconds for IOS to fully boot before running deployment.

---

## Running the Application

```powershell
python app.py
```

You will see:
```
  AI Network Config System — Stage 2 MVP
  Open your browser at: http://127.0.0.1:5000
  GNS3 API expected at: http://localhost:3080/v2
```

Open your browser and navigate to **http://127.0.0.1:5000**

---

## Browser UI Walkthrough

The pipeline progress bar at the top tracks which steps are complete.

### Step 1 — Topology Discovery
1. Click **Load GNS3 Projects** to see available projects.
2. Click your project to select it (auto-selects the opened project).
3. Click **Discover Topology** — the system queries the GNS3 REST API and saves `topology.json`.
4. Device cards and link connections appear below.

### Step 2 — Intent Wizard
Fill in all four sections:

| Section | What to enter |
|---|---|
| **VLANs** | VLAN ID (e.g. 10), name, and which switch ports belong to it |
| **IP Plan** | Per-interface IP assignments for R1 subinterfaces and hosts |
| **ACL** | ACL name, which interface to apply it to, permit/deny rules |
| **Routing** | Static routes (network + next-hop) or select OSPF |

Default values are pre-populated as a starting point — edit to match your topology.

Click **Save Intent & Continue** — saves `intent.json`.

### Step 3 — AI Config Generation
Click **Generate Configs with AI**. Claude Sonnet reads `intent.json` and returns structured Cisco IOS CLI commands. Configs are displayed per device in tabs with a Copy button. Saved to `configs.json`.

### Step 4 — Deployment
> **Ensure all GNS3 devices are started and booted before this step.**

Click **Deploy to Devices**. Netmiko opens Telnet connections to each device's GNS3 console port and pushes the commands. Per-device status cards show ✅ Success or ❌ Failed. Full logs saved to `deploy_logs.json`.

### Step 5 — Validation
Click **Run Validation**. The system:
- Runs `show ip interface brief`, `show ip route`, `show access-lists` on the router
- Runs `show vlan brief` on the switch
- Runs ping tests from the router to each host IP

Results appear in a Pass/Fail table. If checks fail, click **AI Retry Fix** to automatically send evidence to Claude, apply corrective commands, and re-validate (max 2 retries). Results saved to `validation.json`.

---

## Generated Files

| File | Description |
|---|---|
| `topology.json` | Discovered GNS3 nodes, links, and console ports |
| `intent.json` | Merged intent: VLANs, IPs, ACL, routing, constraints |
| `configs.json` | AI-generated Cisco IOS commands per device |
| `deploy_logs.json` | Netmiko output and status per device |
| `validation.json` | Pass/fail results for all checks |
| `pipeline_state.json` | Tracks which pipeline steps are complete |

---

## Troubleshooting

### GNS3 not reachable
```
Cannot reach GNS3 at http://localhost:3080/v2
```
- Confirm GNS3 is running and the GNS3 VM is started.
- Check GNS3 → Edit → Preferences → Server → Local server port (default: 3080).

### No projects found
- Open a project in GNS3 before clicking Discover Topology.
- The project status must be `opened`.

### Telnet connection timeout
```
Telnet connection to R1 (port XXXX) timed out
```
- Start all devices in GNS3 and wait 60 seconds for IOS to boot.
- Verify console port: check `topology.json` → `nodes[n].console`.
- Test manually: `telnet 127.0.0.1 <console_port>`

### AI returns invalid JSON
- The system automatically tries to extract a JSON block using regex.
- If it still fails, check that `ANTHROPIC_API_KEY` is set correctly.
- Retry config generation — Claude occasionally adds markdown fencing on first attempt.

### Validation check fails after deployment
- Use **AI Retry Fix** (up to 2 times) for automatic remediation.
- Check `deploy_logs.json` for command-level output.
- Verify interface names in your intent match GNS3 device interface names exactly.

---

## Project Structure

```
NSSA443/
├── app.py              # Flask server + all API routes
├── gns3_client.py      # GNS3 REST API client
├── intent_wizard.py    # Intent collection & validation
├── ai_generator.py     # Anthropic Claude integration
├── deployer.py         # Netmiko Telnet deployment
├── validator.py        # Validation + closed-loop retry
├── requirements.txt    # Pinned Python dependencies
├── templates/
│   └── index.html      # Browser UI
├── topology.json       # Auto-generated
├── intent.json         # Auto-generated
├── configs.json        # Auto-generated
├── deploy_logs.json    # Auto-generated
└── validation.json     # Auto-generated
```
