# Jev Doom

Watch [Jev](https://typesafe.ai/) make structured decisions in a real Freedoom game. A local dashboard shows the game, choices, probabilities, navigation, latency and cost. Change its standing orders while it plays.

Use **TypeSafe direct** or **Vercel AI Gateway**. This is an independent experiment, not an official TypeSafe, Vercel or Doom product.

## Quick start

Requires macOS or Linux, **Python 3.10–3.13**, **Node.js 22+**, and internet access for setup. Python 3.13 is recommended. Windows users can try WSL2; it has not been validated. Some systems need the native libraries listed in the [ViZDoom installation guide](https://vizdoom.farama.org/introduction/installation/).

```sh
git clone https://github.com/olivier-motium/jev-doom.git
cd jev-doom
./setup.sh
./run.sh
```

Open **http://127.0.0.1:8767**. The free preview loads a real engine frame without an API key. Setup creates an isolated Python environment, installs the locked Node dependencies, and downloads **Freedoom 0.13.0** from its official release. No proprietary Doom assets are needed or included.

If your default Python is newer, use `PYTHON=python3.13 ./setup.sh`. To use a different port: `PORT=8768 ./run.sh`.

## Connect Jev

1. Edit the generated `.env` file. Set one of the keys below. Keep the unused line blank.
2. Restart `./run.sh`, then select the provider in the dashboard.
3. Click **Check provider**. This makes one billed API request. Once it passes, click **Start fresh run**.

| Dashboard provider | Server environment variable | Get access |
| --- | --- | --- |
| TypeSafe direct | `TYPESAFE_API_KEY` | [TypeSafe console](https://console.typesafe.ai/) · [API quick start](https://docs.typesafe.ai/introduction/quickstart) |
| Vercel Gateway | `AI_GATEWAY_API_KEY` | [AI Gateway setup](https://vercel.com/docs/ai-gateway) · [Jev model](https://vercel.com/ai-gateway/models/jev) |

Direct requests use `https://api.typesafe.ai/v1/systemone` with `jev-latest`. Gateway requests use AI SDK's experimental evaluation API with `typesafe-ai/jev`. Provider access, availability and billing depend on your account. The keys stay in the Python/Node server processes; the browser receives only readiness flags. Do not put private information in standing orders: game state and orders are sent to the selected provider.

The default run lasts **90 seconds**. Pause, stop, change orders, switch maps between runs, or select the coherent-tactics controller. Fixed tactics use a deterministic selector but still make timing calls; that mode is not an unpaid demo.

## Spending and local data

The default **$15 cumulative charged-or-reserved cap** covers both providers, including checks and investigations, and persists across restarts in `runs/budget.json`. Each request reserves $0.05. When a valid reported cost arrives, it replaces the reservation. Unknown costs keep the reservation; launch-price estimates never replace it. The cap can therefore stop a run before $15 has actually been billed. The time selection is a maximum, not a guarantee: a fresh budget allows at most 300 unknown-cost requests, including checks, and high call frequency can exhaust it sooner. A request could cost more than its reservation, so also configure provider account limits.

Change `JEV_BUDGET_USD` in `.env` and restart to change the cap. Run traces, replies and checkpoints stay in the ignored `runs/` directory. Keep the budget file if you want spending continuity. No requests are made until you click a billed action.

## What this measures

The default **observable** mode sends structured engine observations: visible entity identities and positions, viewed floor cells and wall pieces, and remembered observations with ages. This is **not a pixels-only agent**. Evaluator kill, hit and damage counters are withheld from model state. Code builds legal choices and executes the chosen motors. The rendered map and evaluator statistics are for inspection.

Historical **assisted** mode is available through `/api/start` with `mode: "assisted"`. It uses full static-map navigation and selected scripted tactical assists. It is labelled in run metadata; do not compare it as if it had the same information as observable mode. The dashboard starts observable mode with no tactical assists.

The engine runs at 35 Hz. Real-time mode keeps advancing while normal requests are in flight; startup and explicit pause stop it; during provider stalls and recovery, an already-started real-time run continues with released controls. `pause_assisted` timing is a separate API option. Game time and wall time differ. MAP01 and MAP02 are supported; later maps are unvalidated. A terminal episode is not automatically a completed level. This repository makes no level-completion or model-performance claim.

## Architecture

`server.py` serves a loopback-only FastAPI dashboard. `harness.py` owns the ViZDoom engine and a serial provider worker. `navigation.py` builds navigation observations and paths. `tactics.py` constructs and measures options. `gateway.mjs` adapts both provider APIs. `index.html` polls state and displays the MJPEG stream. Provider replies are validated before use.

**Vercel support means AI Gateway access, not hosting this application on Vercel.** The native engine, long-lived process and local files require a local machine or a suitably managed server. This release binds only to loopback and has no multi-user authentication. Do not expose it directly to the internet.

## Troubleshooting

- **Python/Node version error:** install Python 3.13 and Node 22+, then make sure those versions are on your PATH. Remove only this project's `.venv` if recreating it with another Python.
- **No game frame:** inspect the terminal and dashboard error. Rerun `./setup.sh` for missing assets or dependencies. Linux may require ViZDoom's native system packages.
- **Provider check fails:** verify the selected key in `.env`, restart, and check account access or credit. The dashboard displays the provider error. A failed request may retain a cost reservation.
- **Budget prevents calls:** inspect `runs/budget.json` and your provider bill before deliberately increasing the cap. Restarting does not reset it.
- **Port busy:** use `PORT=8768 ./run.sh`. Only one server can use a checkout at a time, to protect its shared budget and checkpoints.

## License and attribution

Original harness code is released under [MIT](LICENSE). Third-party software and game data retain their own licenses; see [THIRD_PARTY.md](THIRD_PARTY.md). No engine binaries, game WADs, credentials or historical runs are committed.
