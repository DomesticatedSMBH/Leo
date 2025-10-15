# Leo

## Project overview
Leo is a Discord bot that keeps an F1 community informed, organised, and entertained. It combines live Toto betting replication, a virtual FIT currency, and server utilities such as channel scheduling, moderation tooling, and countdown clocks for the next race weekend. The bot loads a configurable set of cogs on startup, enabling F1 session countdowns, Toto market mirroring, wallet management, scheduling utilities, and moderation helpers in a single deployment.【F:leo_bot/bot.py†L9-L41】

Key capabilities include:

- **F1 session clocks** – periodically renames configured channels to show the next session name, date, time, and countdown using FastF1 data.【F:leo_bot/cogs/f1_clock.py†L13-L102】
- **Toto market mirroring & betting** – pulls Toto odds into Discord embeds, lets members bet FITs on outcomes, and keeps market messages in sync with closures.【F:leo_bot/cogs/betting.py†L55-L220】【F:leo_bot/cogs/betting.py†L609-L812】
- **FIT wallets & transactions** – persists user balances, awards activity bonuses, supports transfers, and tracks bet history in SQLite.【F:leo_bot/betting.py†L231-L428】【F:leo_bot/cogs/betting.py†L490-L618】
- **Scheduling tools** – slash commands allow staff to queue announcements or polls that the scheduler loop posts when they fall due.【F:leo_bot/cogs/scheduler.py†L17-L130】【F:leo_bot/scheduler.py†L39-L97】
- **Moderation helpers** – auto-rewrite social links, provide message reporting context menus, and support staff cleanup workflows.【F:leo_bot/cogs/moderation.py†L13-L121】

## Setup

### Prerequisites
- Python 3.11 or later
- A Discord application with a bot token and privileged intents enabled
- Access to Toto F1 markets (the bot scrapes odds client-side)

### Installation
1. Clone the repository and create a virtual environment:
   ```bash
   git clone https://github.com/your-org/leo.git
   cd leo
   python -m venv .venv
   source .venv/bin/activate
   ```
2. Install the runtime dependencies:
   ```bash
   pip install discord.py python-dotenv fastf1 requests beautifulsoup4 pytz
   ```
3. Copy the example environment configuration and fill in the values (you can use `.env` for local development):
   ```bash
   cp .env.example .env  # create this file if it does not exist yet
   ```
4. Run the bot once configuration is in place:
   ```bash
   python -m main
   ```

### Configuration
Leo reads settings from environment variables (dotenv files are loaded automatically). Required values are listed below; optional entries have defaults or enable extra features.【F:leo_bot/config.py†L14-L118】

#### Required
| Variable | Purpose |
| --- | --- |
| `DISCORD_TOKEN` | Bot token used to connect to Discord.【F:leo_bot/config.py†L38-L59】 |
| `DISCORD_GUILD` | Primary guild ID for slash-command registration.【F:leo_bot/config.py†L94-L103】 |
| `DISCORD_TEST_GUILD` | Secondary guild used for staging/smoke tests.【F:leo_bot/config.py†L97-L103】 |
| `STAFF_ID1`, `STAFF_ID2` | Discord user IDs granted admin privileges in Leo.【F:leo_bot/config.py†L64-L83】 |

#### Recommended/optional
| Variable | Default | Purpose |
| --- | --- | --- |
| `READY_CHANNEL_ID` | `1421152351888085135` | Channel receiving the "Leo is ready" embed.【F:leo_bot/config.py†L99-L104】【F:leo_bot/bot.py†L32-L46】 |
| `REPORT_LOG_CHANNEL_ID` | `1421156169308700874` | Destination for reported message embeds.【F:leo_bot/config.py†L101-L105】【F:leo_bot/cogs/moderation.py†L90-L117】 |
| `F1_CHANNEL_EVENT` | `1425959683264610394` | Channel renamed with the upcoming session label.【F:leo_bot/config.py†L105-L111】【F:leo_bot/cogs/f1_clock.py†L53-L102】 |
| `F1_CHANNEL_DATE` | `1425959704307175494` | Channel renamed with the next session date/time.【F:leo_bot/config.py†L105-L111】【F:leo_bot/cogs/f1_clock.py†L53-L102】 |
| `F1_CHANNEL_COUNTDOWN` | `1425959786062545099` | Channel renamed with the countdown timer.【F:leo_bot/config.py†L105-L111】【F:leo_bot/cogs/f1_clock.py†L53-L102】 |
| `BETTING_CHANNEL` | – | Text channel that receives Toto market embeds and updates.【F:leo_bot/config.py†L108-L114】【F:leo_bot/cogs/betting.py†L84-L220】 |
| `SERVER_RANKINGS_CHANNEL` | – | Optional text channel for future server ranking summaries (used by wallet/betting recaps). |
| `SCHEDULES_PATH` | `schedules.json` | Location on disk where scheduled jobs are persisted.【F:leo_bot/config.py†L110-L112】【F:leo_bot/scheduler.py†L39-L65】 |
| `TOTO_F1_DB` | `toto_f1.sqlite` | Path to the Toto scraping database.【F:leo_bot/config.py†L111-L115】【F:toto_f1_api.py†L33-L133】 |
| `WALLET_DB_PATH` | `wallet.sqlite` | SQLite file used for wallet, bet, and market metadata.【F:leo_bot/config.py†L112-L116】【F:leo_bot/betting.py†L231-L328】 |
| `TOTO_REQUESTS_ONLY` | `false` | Force Toto refreshes to use cached HTML and skip Playwright fallbacks.【F:leo_bot/config.py†L113-L115】 |

## Command reference
Leo registers slash-command groups for wallets, betting, scheduling, and moderation. The sections below focus on FIT economy features.

### Wallet commands (`/wallet …`)
- `/wallet info` – Shows balance, recent transactions, and open bets in an ephemeral embed.【F:leo_bot/cogs/betting.py†L490-L538】
- `/wallet send <user> <amount>` – Transfers FITs to another member (bots are blocked).【F:leo_bot/cogs/betting.py†L540-L575】
- `/wallet generate <amount>` – Admin-only minting tool for awarding prizes or seeding balances.【F:leo_bot/cogs/betting.py†L577-L618】

### Betting commands (`/bet …`)
- `/bet new <instance> <arguments> <staking> <amount>` – Places one or more bets against the currently promoted market instance, splitting stakes according to the chosen strategy.【F:leo_bot/cogs/betting.py†L609-L788】
- `/bet cancel <bet_id>` – Cancels an open bet and refunds the stake if the market has not closed yet.【F:leo_bot/cogs/betting.py†L790-L818】

### Shop commands
Shop purchases will reuse wallet debits (`WalletStore.deduct_tokens`) and transaction logging, but no public slash commands are exposed yet. Administrators can build on the wallet helpers to add `/shop` interactions for merchandise or perks without altering core balance logic.【F:leo_bot/betting.py†L318-L362】

## Payouts and scheduling
- The betting cog marks markets as closed once Toto removes them from the feed, preventing new stakes and moving related bets into a `closed` state ready for manual review and payout calculation.【F:leo_bot/cogs/betting.py†L141-L220】【F:leo_bot/betting.py†L500-L534】
- Staff can use the `/schedule` command to queue payout reminders, announcement messages, or polls. Jobs persist to disk (`SCHEDULES_PATH`) and the scheduler loop checks for due tasks every 30 seconds, posting them into the configured channel.【F:leo_bot/cogs/scheduler.py†L25-L122】【F:leo_bot/scheduler.py†L39-L97】
- After settling results, use the wallet helpers (`add_tokens`, `deduct_tokens`) to credit winners or reclaim stakes while keeping transaction history consistent.【F:leo_bot/betting.py†L318-L362】【F:leo_bot/betting.py†L578-L620】

## Contribution & testing
1. Run unit tests with pytest before submitting changes:
   ```bash
   pytest
   ```
2. Use type hints consistently and favour async helpers like `run_in_thread` when accessing blocking I/O from cogs.【F:leo_bot/cogs/betting.py†L28-L68】
3. Validate wallet or scheduler changes against the SQLite structures using the included helper script:
   ```bash
   python -m scripts.dump_betting_db --db wallet.sqlite
   ```
4. Open pull requests with clear summaries of behaviour changes and mention any new environment variables or commands introduced.

