# Card Game – Render Prototype

Multiplayer card game prototype using FastAPI + WebSockets.

## Rules implemented

- 60 cards: 52 numbered cards, 4 Wizards, 4 Fools
- 3–6 players
- At least one bot
- New 60-card deck every round
- Round 1 = 1 card/player, round 2 = 2 cards/player, etc.
- Starting player rotates clockwise each round
- Bidding starts with the starting player
- Wizard as revealed trump card: starting player chooses trump
- Fool as revealed trump card: no trump
- Wizard always wins; first Wizard wins if multiple Wizards are played
- Fool never wins, except a trick containing only Fools is won by the trick leader
- Must follow the lead color when possible
- Wizard and Fool may always be played
- Correct bid: 20 + 10 per trick
- Incorrect bid: -10 per trick difference

## Local run

```bash
pip install -r requirements.txt
uvicorn app:app --reload
```

Open http://127.0.0.1:8000

## Render

Connect the GitHub repository to Render as a Web Service.
Render can use `render.yaml`, or use:

Build:
`pip install -r requirements.txt`

Start:
`uvicorn app:app --host 0.0.0.0 --port $PORT`
