# AI-Assisted Network Configuration System — NSSA443 Stage 2 MVP

End-to-end pipeline: GNS3 topology discovery → browser intent wizard → local LLM (LM Studio) → Netmiko Telnet deployment → automated validation with closed-loop retry.

---

## Prerequisites

| Requirement | Details |
|---|---|
| Python | 3.10 or later |
| GNS3 | Running locally, REST API at `http://localhost:3080` |
| GNS3 VM | Running (required for device emulation) |
| LM Studio | Running with a model loaded on port 1234 |
| GNS3 Topology | 1 router (c7200) + 1 switch (c2691 w/ NM-16ESW) + 2 VPCS hosts |

**No cloud API key required** — the AI runs entirely locally through LM Studio.

---

## Installation

```powershell
cd C:\Users\Ahmed\Desktop\NSSA443
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> If you get a script execution policy error:
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```

---

## GNS3 Setup

1. Start GNS3 and ensure the GNS3 VM is running
2. Create a project and add devices:
   - **R1** — Cisco c7200 router
   - **ESW1** — Cisco c2691 router **with Slot 1 = NM-16ESW** (makes it a switch)
   - **PC1**, **PC2** — VPCS nodes
3. Connect links: R1 → ESW1 (trunk), ESW1 → PC1, ESW1 → PC2
4. **Start all nodes** and wait ~90 seconds for IOS to boot

> **GNS3 Authentication**: If GNS3 asks for a password and you can't find it, go to **Edit → Preferences → Server** and uncheck "Protect server with password", then restart GNS3.

---

## LM Studio Setup

1. Open LM Studio
2. Go to the **Local Server** tab (`↔` icon on the left)
3. Load any capable model (Mistral 7B, Llama 3, DeepSeek, etc.)
4. Click **Start Server** — confirm it says "Running on port 1234"

---

## Running the App

```powershell
python app.py
```

Open **http://127.0.0.1:5050** in your browser.

---

## Pipeline Walkthrough

### Step 1 — Topology Discovery
Click **Load GNS3 Projects** → select your project → **Discover Topology**.
Saves `topology.json` with all nodes, links, and console ports.

### Step 2 — Intent Wizard
Fill in VLANs, IP addressing, ACL rules, and routing. Click **Save Intent**.
Saves `intent.json`.

### Step 3 — Generate Configs
Click **Generate Configs with AI**. LM Studio generates Cisco IOS commands for the router/switch and VPCS commands (`ip x.x.x.x/prefix gateway`) for PC1/PC2.
Saves `configs.json`.

### Step 4 — Deploy
All devices must be **started and fully booted** before this step.
Click **Deploy to Devices**. The system:
- Sends IOS commands via Netmiko Telnet to R1 and ESW1
- Sends VPCS commands via raw socket to PC1 and PC2

Saves `deploy_logs.json`.

### Step 5 — Validation
Click **Run Validation**. Checks:
- Interface IPs (`show ip interface brief`)
- Routing table (`show ip route`)
- ACL presence (`show access-lists`)
- VLAN config (`show vlan brief`)
- Ping connectivity from R1 to each host

If checks fail, click **AI Retry Fix** (max 2 attempts) for automatic remediation.
Saves `validation.json`.

---

## Generated Files

| File | Description |
|---|---|
| `topology.json` | Discovered GNS3 nodes, links, console ports |
| `intent.json` | User-defined VLANs, IPs, ACL, routing |
| `configs.json` | AI-generated commands per device |
| `deploy_logs.json` | Netmiko output and status per device |
| `validation.json` | Pass/fail results for all checks |
| `pipeline_state.json` | Tracks completed pipeline steps |

---

## Troubleshooting

| Error | Fix |
|---|---|
| GNS3 API 401 Unauthorized | Disable "Protect server with password" in GNS3 Preferences → Server, then restart GNS3 |
| Connection refused on deploy | Ensure all GNS3 nodes are **started** and fully booted (~90 s for IOS) |
| Auth error on R1/ESW1 | Open GNS3 console for that device, press Enter, confirm you see the `Router>` prompt |
| Cannot reach LM Studio | Open LM Studio → Local Server tab → Start Server (port 1234) |
| Port 5000 forbidden | Already fixed — app runs on port **5050** (port 5000 is reserved by Hyper-V) |

---

## Project Structure

```
NSSA443/
├── app.py              # Flask server (port 5050) + 13 API routes
├── gns3_client.py      # GNS3 REST API client
├── intent_wizard.py    # Intent collection & validation
├── ai_generator.py     # LM Studio local LLM integration
├── deployer.py         # Netmiko (IOS) + socket (VPCS) deployment
├── validator.py        # Validation + closed-loop AI retry
├── requirements.txt    # Python dependencies
└── templates/
    └── index.html      # Browser UI
```

---

## GitHub

```powershell
git init
git add .
git commit -m "Initial commit: NSSA443 Stage 2 MVP"
git remote add origin https://github.com/YOUR_USERNAME/NSSA443-network-automation.git
git branch -M main
git push -u origin main
```

**Subsequent pushes:**
```powershell
git add .
git commit -m "your message"
git push
```
